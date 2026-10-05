"""The event bus: one ordered stream of everything that happens.

Every event gets a global sequence number and lands in a ring of recent
events; events that belong to a run are also appended to that run's own file
(data/runs/<id>.jsonl) so a client can replay a run from its start long
after it left the ring. GET /api/events serves the stream as SSE with the
sequence number as the event id, so a browser that loses the connection
resumes exactly where it stopped (Last-Event-ID), and a client that fell
further behind than the ring is told to reload ("reset").

    bus.publish("sessions", "updated", {"id": ...})
    bus.publish("output", "out", {"line": ...}, run=run_id)
"""
import collections
import json
import os
import threading
import time

TOPICS = ("sessions", "runs", "output", "jobs", "usage", "cluster", "gateways", "models", "agents", "deploy",
          "approvals", "config", "mcp", "system")


class EventBus:
    def __init__(self, runs_dir=None, ring=5000):
        self.runs_dir = runs_dir
        self.ring = collections.deque(maxlen=ring)
        self.seq = 0
        self.cond = threading.Condition()
        self._files = {}
        self._flock = threading.Lock()
        if runs_dir:
            os.makedirs(runs_dir, exist_ok=True)

    def publish(self, topic, type_, data=None, run=None):
        with self.cond:
            self.seq += 1
            evt = {"seq": self.seq, "t": round(time.time(), 3), "topic": topic, "type": type_,
                   "data": data if data is not None else {}}
            if run:
                evt["run"] = run
            self.ring.append(evt)
            self.cond.notify_all()
        if run and self.runs_dir:
            self._append_run(run, evt)
        return evt

    def _append_run(self, run, evt):
        line = json.dumps(evt, ensure_ascii=False) + "\n"
        with self._flock:
            path = os.path.join(self.runs_dir, run + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)

    def oldest(self):
        with self.cond:
            return self.ring[0]["seq"] if self.ring else self.seq + 1

    def scan(self, after, topics=None, run=None):
        """Events with seq > after (from the ring), filtered. Returns
        (events, gap, head): gap means some events after `after` already left
        the ring and the caller should reload its state; head is the newest
        seq, which a follower waits on next, so events it filtered out never
        wake it again."""
        with self.cond:
            gap = bool(self.ring) and after < self.ring[0]["seq"] - 1
            out = []
            for e in reversed(self.ring):          # newest first; stop at the cursor
                if e["seq"] <= after:
                    break
                if (not topics or e["topic"] in topics) and (run is None or e.get("run") == run):
                    out.append(e)
            head = self.seq
        out.reverse()
        return out, gap, head

    def since(self, after, topics=None, run=None):
        out, gap, _ = self.scan(after, topics, run)
        return out, gap

    def wait(self, after, timeout):
        """Block until there is an event newer than `after`, or timeout."""
        with self.cond:
            if self.seq > after:
                return True
            self.cond.wait(timeout)
            return self.seq > after

    def run_events(self, run, after=0):
        """A run's events from its own file (complete, unlike the ring)."""
        if not self.runs_dir:
            return []
        out = []
        try:
            with open(os.path.join(self.runs_dir, run + ".jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("seq", 0) > after:
                        out.append(e)
        except OSError:
            pass
        out.sort(key=lambda e: e.get("seq", 0))   # two writers can land out of order
        return out


def sse_frame(evt):
    return ("id: %d\nevent: %s\ndata: %s\n\n" % (evt["seq"], evt["topic"],
                                                json.dumps(evt, ensure_ascii=False))).encode("utf-8")
