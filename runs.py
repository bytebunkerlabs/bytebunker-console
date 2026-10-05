"""Server-owned runs: work that outlives the HTTP request that started it.

A run is anything that takes a while and streams progress: a chat turn, an
agents goal, a deploy, a job, an MCP install. It runs on its own thread and
keeps going when the browser tab that started it closes, reloads or sleeps.
Its lifecycle (started, state, cancelling, finished) goes on the event bus
under topic "runs"; its output under topic "output"; both tagged with its
id. Any client can attach to it later (replay from its file, then follow
live) or cancel it.

A target that returns {"ok": False, "error": ...} ends the run in "error"
without raising, after reporting the failure in its own words.

States: queued, running, waiting_approval, done, error, cancelled, and
interrupted (a run that was still going when the server stopped).
"""
import json
import os
import threading
import time
import uuid

FINAL = ("done", "error", "cancelled", "interrupted")


class Busy(Exception):
    """A run with the same key is still going."""

    def __init__(self, run):
        super().__init__("%s is already running (%s)" % (run.title or run.kind, run.id))
        self.run = run


class Run:
    def __init__(self, kind, source, title="", meta=None, key=None):
        self.id = "%s-%s" % (kind, uuid.uuid4().hex[:12])
        self.kind, self.source, self.title, self.key = kind, source, title, key
        self.meta = dict(meta or {})
        self.state = "queued"
        self.started = time.time()
        self.ended = None
        self.result = None
        self.error = None
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()

    def summary(self):
        return {"id": self.id, "kind": self.kind, "source": self.source, "title": self.title,
                "state": self.state, "started": round(self.started, 3),
                "ended": round(self.ended, 3) if self.ended else None,
                "meta": self.meta, "result": self.result, "error": self.error}

    @property
    def cancelled(self):
        return self.cancel_event.is_set()


class RunRegistry:
    def __init__(self, bus, index_path=None, keep=200):
        self.bus = bus
        self.index_path = index_path
        self.keep = keep
        self.runs = {}
        self.order = []
        self.lock = threading.Lock()
        self._recover()

    # ------------------------------------------------------------ history
    def _recover(self):
        """Runs the previous server left running are marked interrupted."""
        if not self.index_path or not os.path.exists(self.index_path):
            return
        try:
            with open(self.index_path, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
        except (OSError, ValueError):
            return
        latest = {}
        for r in rows:
            latest[r["id"]] = r
        changed = False
        for r in latest.values():
            if r.get("state") not in FINAL:
                r["state"] = "interrupted"
                r["ended"] = r.get("ended") or time.time()
                self._write_index(r)
                changed = True
        if changed:
            self.bus.publish("runs", "recovered", {"interrupted": [r["id"] for r in latest.values()
                                                                  if r["state"] == "interrupted"]})

    def _write_index(self, summary):
        if not self.index_path:
            return
        os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    def history(self, limit=100, kind=None):
        """Recent runs, including those from before this server started."""
        rows = {}
        if self.index_path and os.path.exists(self.index_path):
            try:
                with open(self.index_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        rows[r["id"]] = r
            except OSError:
                pass
        with self.lock:
            for rid in self.order:
                rows[rid] = self.runs[rid].summary()
        out = [r for r in rows.values() if kind is None or r.get("kind") == kind]
        out.sort(key=lambda r: r.get("started") or 0, reverse=True)
        return out[:limit]

    # ------------------------------------------------------------ running
    def start(self, kind, source, target, title="", meta=None, key=None):
        """Run target(run) on its own thread. target emits progress with
        registry.emit(run, type, data) and returns a result dict (or raises).
        With a key, a second run with the same key is refused (Busy) while
        the first is going: two deploys to one rack, two agent goals."""
        run = Run(kind, source, title, meta, key)
        with self.lock:
            if key is not None:
                for r in self.runs.values():
                    if r.key == key and r.state not in FINAL:
                        raise Busy(r)
            self.runs[run.id] = run
            self.order.append(run.id)
            while len(self.order) > self.keep:
                old = self.order.pop(0)
                r = self.runs.get(old)
                if r and r.state in FINAL:
                    self.runs.pop(old, None)
                else:
                    self.order.insert(0, old)
                    break

        def body():
            run.state = "running"
            self._write_index(run.summary())
            self.bus.publish("runs", "started", run.summary(), run=run.id)
            try:
                res = target(run)
                run.result = res if isinstance(res, dict) else ({"value": res} if res is not None else None)
                failed = isinstance(res, dict) and res.get("ok") is False
                if failed:
                    run.error = str(res.get("error") or "failed")[:500]
                run.state = "cancelled" if run.cancelled else ("error" if failed else "done")
            except Exception as e:   # noqa: BLE001 - a run's failure is its result, not the server's
                run.error = str(e)[:500]
                run.state = "cancelled" if run.cancelled else "error"
            run.ended = time.time()
            self._write_index(run.summary())
            self.bus.publish("runs", "finished", run.summary(), run=run.id)
            run.done_event.set()

        threading.Thread(target=body, name="run-" + run.id, daemon=True).start()
        return run

    def emit(self, run, type_, data=None):
        """Output and progress: topic "output", so the app-wide stream of
        what changed stays light while a run's own stream has every line."""
        return self.bus.publish("output", type_, data, run=run.id)

    def set_state(self, run, state):
        run.state = state
        self.bus.publish("runs", "state", {"state": state}, run=run.id)

    def get(self, rid):
        with self.lock:
            return self.runs.get(rid)

    def active(self, kind=None):
        with self.lock:
            return [r for r in self.runs.values() if r.state not in FINAL and (kind is None or r.kind == kind)]

    def cancel(self, rid):
        run = self.get(rid)
        if not run or run.state in FINAL:
            return False
        run.cancel_event.set()
        self.bus.publish("runs", "cancelling", {"id": rid}, run=rid)
        return True
