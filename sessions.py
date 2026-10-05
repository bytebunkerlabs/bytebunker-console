"""Sessions on disk: one file per session, plus an index for the list.

    data/sessions/<id>.jsonl   the session's events, oldest first
    data/sessions/index.json   one summary per session (rebuilt from the files if lost)

Event types:
  snapshot  {"session": {...}}       a client's whole-session save (the browser's
                                     Playground today); it supersedes everything
                                     before it, so the file is rewritten with it
  meta      {"title"?, "model"?, "source"?, "cwd"?, "profile"?}
  msg       {"message": {...}}       a message appended by the server runner
  patch     {"index": i, "message"}  a message replaced in place

A session is its last snapshot with every later event applied. Writes for
one session are serialized; different sessions never contend, and no write
touches more than one session's file (the old store rewrote every session,
48 MB on hermes, after every turn).
"""
import json
import os
import re
import threading
import time

_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
STORED_TOOL_TEXT = 65536     # the model sees 20k of a tool result, the screen at most 20k


def compact(sess):
    """Cap the stored copy of tool arguments and results. A tool that
    returned 45 MB made one session 45 MB (measured on hermes) and every save
    rewrote it; the model only ever saw 20k characters of it."""
    for m in sess.get("messages") or []:
        for t in m.get("toolUse") or []:
            for k in ("result", "args"):
                v = t.get(k)
                if isinstance(v, str) and len(v) > STORED_TOOL_TEXT:
                    t[k] = v[:STORED_TOOL_TEXT] + "\n\u2026[stored copy truncated: %d characters in total]" % len(v)
    return sess


def _now():
    return int(time.time() * 1000)         # milliseconds, like the browser's Date.now()


def _content_chars(m):
    c = m.get("content")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(len(p.get("text") or "") for p in c if isinstance(p, dict))
    return 0


class SessionStore:
    def __init__(self, dir_path, legacy_path=None):
        self.dir = dir_path
        self.legacy = legacy_path
        self.index_path = os.path.join(dir_path, "index.json")
        self._lock = threading.Lock()           # the index
        self._locks = {}                        # per session
        os.makedirs(dir_path, exist_ok=True)
        self.index = self._load_index()
        self.migrated = self._migrate()

    # ------------------------------------------------------------ helpers
    def _path(self, sid):
        if not _ID.match(sid or ""):
            raise ValueError("bad session id")
        return os.path.join(self.dir, sid + ".jsonl")

    def _slock(self, sid):
        with self._lock:
            return self._locks.setdefault(sid, threading.Lock())

    @staticmethod
    def summarize(sess):
        msgs = sess.get("messages") or []
        return {"id": sess.get("id"), "title": sess.get("title") or "",
                "model": sess.get("model") or "", "source": sess.get("source") or "app",
                "cwd": sess.get("cwd"), "created": sess.get("created") or sess.get("updated") or _now(),
                "updated": sess.get("updated") or _now(),
                "turns": sess.get("turns") if isinstance(sess.get("turns"), int)
                else sum(1 for m in msgs if m.get("role") == "user"),
                "chars": sess.get("chars") if isinstance(sess.get("chars"), int)
                else sum(_content_chars(m) for m in msgs)}

    def _write_atomic(self, path, text):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def _save_index(self):
        self._write_atomic(self.index_path, json.dumps(self.index, ensure_ascii=False))

    def _load_index(self):
        try:
            with open(self.index_path, encoding="utf-8") as f:
                idx = json.load(f)
            if isinstance(idx, dict):
                return idx
        except (OSError, ValueError):
            pass
        return self._rebuild_index()

    def _rebuild_index(self):
        idx = {}
        for name in os.listdir(self.dir):
            if name.endswith(".jsonl"):
                sess = self._read(name[:-6])
                if sess:
                    idx[sess["id"]] = self.summarize(sess)
        self.index = idx
        self._save_index()
        return idx

    # ------------------------------------------------------------ migration
    def _migrate(self):
        """Split the old single sessions.json into one file per session, once.
        The original is kept beside it as sessions.json.migrated-<time>."""
        if not self.legacy or not os.path.exists(self.legacy):
            return 0
        try:
            with open(self.legacy, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            return 0
        n = 0
        for sess in reversed(old if isinstance(old, list) else []):   # oldest first
            if isinstance(sess, dict) and sess.get("id") and _ID.match(str(sess["id"])):
                have = self.index.get(str(sess["id"]))
                if have and (have.get("updated") or 0) > (sess.get("updated") or 0):
                    continue                   # the file here is newer (an older app wrote the legacy one)
                self._write_snapshot(sess)
                n += 1
        os.replace(self.legacy, self.legacy + ".migrated-" + time.strftime("%Y%m%d-%H%M%S"))
        return n

    # ------------------------------------------------------------ reading
    def _read(self, sid):
        try:
            with open(self._path(sid), encoding="utf-8") as f:
                lines = f.readlines()
        except (OSError, ValueError):
            return None
        sess = None
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue                       # a torn last line never loses the rest
            t = e.get("type")
            if t == "snapshot":
                sess = dict(e.get("session") or {})
            elif sess is None:
                sess = {"id": sid, "messages": [], "created": e.get("t")}
            if t == "meta":
                for k, v in e.items():
                    if k not in ("type", "t"):
                        sess[k] = v
            elif t == "msg":
                sess.setdefault("messages", []).append(e.get("message") or {})
            elif t == "patch":
                msgs = sess.setdefault("messages", [])
                i = e.get("index")
                if isinstance(i, int) and 0 <= i < len(msgs):
                    msgs[i] = e.get("message") or {}
            if t in ("meta", "msg", "patch"):
                sess["updated"] = e.get("t") or sess.get("updated")
        if sess is not None:
            sess["id"] = sid
        return sess

    def get(self, sid):
        with self._slock(sid):
            return self._read(sid)

    def list(self):
        with self._lock:
            rows = list(self.index.values())
        rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
        return rows

    def all(self):
        """Every session in full (export and search only: it reads every file)."""
        return [s for s in (self.get(r["id"]) for r in self.list()) if s]

    # ------------------------------------------------------------ writing
    def _write_snapshot(self, sess):
        sid = str(sess["id"])
        sess = compact(dict(sess))
        sess.setdefault("updated", _now())
        line = json.dumps({"type": "snapshot", "t": _now(), "session": sess}, ensure_ascii=False) + "\n"
        self._write_atomic(self._path(sid), line)
        summary = self.summarize(sess)
        with self._lock:
            self.index[sid] = summary
            self._save_index()
        return summary

    def put(self, sess):
        """A client's whole-session save."""
        if not isinstance(sess, dict) or not _ID.match(str(sess.get("id") or "")):
            raise ValueError("a session needs an id of letters, digits, . _ -")
        with self._slock(str(sess["id"])):
            return self._write_snapshot(sess)

    def append(self, sid, type_, **fields):
        """Append one event (meta, msg, patch) to a session, creating it if new."""
        with self._slock(sid):
            evt = {"type": type_, "t": _now()}
            evt.update(fields)
            with open(self._path(sid), "a", encoding="utf-8") as f:
                f.write(json.dumps(evt, ensure_ascii=False) + "\n")
            summary = self.summarize(self._read(sid) or {"id": sid})
        with self._lock:
            self.index[sid] = summary
            self._save_index()
        return summary

    def delete(self, sid):
        """Remove a session. Returns its last state (for the trace) or None."""
        with self._slock(sid):
            sess = self._read(sid)
            try:
                os.remove(self._path(sid))
            except OSError:
                pass
        with self._lock:
            self.index.pop(sid, None)
            self._save_index()
        return sess
