"""The audit log: who changed what in ByteBunker, and when.

The trace log (traces.py) keeps everything the app did, model requests included;
this keeps only the changes and the decisions: settings, access, tools,
machines, agents, jobs, deletions and exports. One line each, in
data/audit/audit.jsonl, append-only and never pruned by the app.

Each line carries the hash of the line before it (sha256 over the line's own
fields), so an edited, removed or reordered line breaks the chain from there
on: verify() finds where. It is tamper-evident, not tamper-proof: someone who
can write the file can rewrite it all, but cannot change one line quietly.

Secrets never reach it: a field whose name says key, token, password or
secret is written as "***", and so is every value of an env or headers block.

    record(category, action, target=None, ok=None, who=None, **detail)
    entries(category=None, q=None, before=None, limit=200)   newest first
    verify()                                                 {"ok", "entries", "broken_at"}

Who did it: "app" (the window), "cli" (bb), a run's source ("job:<id>",
"agent"), set per thread with set_actor().
"""
import hashlib
import json
import os
import re
import threading
import time

CATEGORIES = ("access", "settings", "tools", "serving", "agents", "runs", "data")
OPAQUE = ("env", "headers")          # values here are often credentials under any name
SECRET = re.compile(r"(^|_)(key|api_key|token|password|passwd|secret|private)s?$", re.I)
GENESIS = "0" * 64
_who = threading.local()


def set_actor(name):
    """Who acts on this thread from now on: a request's client, or a run's source."""
    _who.name = str(name or "app")[:40]


def actor():
    return getattr(_who, "name", None) or "app"


def redact(v, depth=0):
    if depth > 4:
        return "…"
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if str(k) in OPAQUE and isinstance(x, dict):
                out[k] = {n: "***" for n in x}
            elif SECRET.search(str(k)) and isinstance(x, str) and x:
                out[k] = "***"
            else:
                out[k] = redact(x, depth + 1)
        return out
    if isinstance(v, (list, tuple)):
        return [redact(x, depth + 1) for x in list(v)[:50]]
    if isinstance(v, str) and len(v) > 2000:
        return v[:2000] + "…"
    return v


def _digest(entry):
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, root, bus=None):
        self.dir = os.path.join(root, "audit")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "audit.jsonl")
        self.bus = bus
        self._lock = threading.Lock()
        self._seq, self._last = 0, GENESIS
        tail = self._tail()
        if tail:
            self._seq, self._last = int(tail.get("seq") or 0), tail.get("hash") or GENESIS

    def _tail(self):
        """The last whole line of the file, read from its end."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                lines = [x for x in f.read().splitlines() if x.strip()]
        except OSError:
            return None
        for raw in reversed(lines):
            try:
                return json.loads(raw)
            except ValueError:
                continue
        return None

    def record(self, category, action, target=None, ok=None, who=None, **detail):
        cat = category if category in CATEGORIES else "settings"
        with self._lock:
            entry = {"seq": self._seq + 1, "ts": round(time.time(), 3), "actor": who or actor(),
                     "category": cat, "action": str(action)[:80]}
            if target not in (None, ""):
                entry["target"] = str(target)[:300]
            if ok is not None:
                entry["ok"] = bool(ok)
            detail = redact({k: v for k, v in detail.items() if v not in (None, "")})
            if detail:
                entry["detail"] = detail
            entry["prev"] = self._last
            entry["hash"] = _digest(entry)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._seq, self._last = entry["seq"], entry["hash"]
        if self.bus is not None:
            try:
                self.bus.publish("audit", "entry", entry)
            except Exception:   # noqa: BLE001 - the line is written; the live view is a nicety
                pass
        return entry

    def _lines(self):
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except ValueError:
                            yield {"_bad": line[:200]}
        except OSError:
            return

    def entries(self, category=None, q=None, before=None, limit=200, who=None):
        q = (q or "").strip().lower()
        out = []
        for e in self._lines():
            if "_bad" in e:
                continue
            if category and e.get("category") != category:
                continue
            if who and not str(e.get("actor", "")).startswith(who):
                continue
            if before and int(e.get("seq") or 0) >= int(before):
                continue
            if q and q not in json.dumps(e, ensure_ascii=False).lower():
                continue
            out.append(e)
        out.reverse()
        return out[:max(1, min(int(limit or 200), 2000))]

    def verify(self):
        """Walk the chain: every line's hash matches its fields and names the line before."""
        prev, n = GENESIS, 0
        for e in self._lines():
            n += 1
            if "_bad" in e:
                return {"ok": False, "entries": n, "broken_at": n, "why": "line %d is not readable" % n}
            if e.get("prev") != prev:
                return {"ok": False, "entries": n, "broken_at": e.get("seq", n),
                        "why": "entry %s does not follow the one before it (removed, reordered or inserted)" % e.get("seq", n)}
            if _digest(e) != e.get("hash"):
                return {"ok": False, "entries": n, "broken_at": e.get("seq", n),
                        "why": "entry %s was changed after it was written" % e.get("seq", n)}
            prev = e["hash"]
        return {"ok": True, "entries": n, "broken_at": None}

    def stats(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        return {"entries": self._seq, "bytes": size, "path": self.path}
