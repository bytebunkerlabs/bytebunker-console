"""Everything the console did, written down — and turned into training data.

The trace log is append-only JSONL under data/traces/, one file per day,
yesterday's gzipped. Every model request goes in with the exact payload the
engine saw (system prompt, tools, sampling) and the response assembled from
the stream (text, reasoning, tool calls, finish reason, usage, timings, or
the error). Every tool run goes in with its arguments and result. Ratings,
archives, and sessions that were deleted or aged out of the sessions file go
in too, so nothing the console ever saw is lost by pruning the UI.

Nothing here is ever deleted by the console. It is your data, on your host.

The export walks the log and the older sessions/archives and writes one
example per turn in the chat format most fine-tuning stacks accept —
{"messages": [...], "tools": [...]} with reasoning_content on assistant
turns and OpenAI-shape tool_calls — with a heuristic pass that redacts
things that look like keys or passwords, because terminal output ends up in
here.
"""
import gzip
import json
import os
import re
import shutil
import threading
import time
import uuid


class TraceLog:
    def __init__(self, root):
        self.dir = os.path.join(root, "traces")
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self._day = None
        self._fh = None
        threading.Thread(target=self.compress_old, daemon=True).start()

    # ---- writing ----
    @staticmethod
    def new_id():
        return uuid.uuid4().hex[:16]

    def log(self, kind, **fields):
        evt = {"ts": round(time.time(), 3), "kind": kind}
        evt.update(fields)
        line = json.dumps(evt, ensure_ascii=False) + "\n"
        day = time.strftime("%Y-%m-%d")
        rolled = False
        with self._lock:
            if day != self._day:
                if self._fh:
                    self._fh.close()
                self._fh = open(os.path.join(self.dir, day + ".jsonl"), "a", encoding="utf-8")
                rolled, self._day = self._day is not None, day
            self._fh.write(line)
            self._fh.flush()
        if rolled:
            threading.Thread(target=self.compress_old, daemon=True).start()
        return evt

    def compress_old(self):
        """gzip every day but today. Roughly 10x smaller: each tool hop resends
        the whole conversation, and that repeats beautifully."""
        today = time.strftime("%Y-%m-%d")
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".jsonl") or name[:-6] >= today:
                continue
            src = os.path.join(self.dir, name)
            try:
                with open(src, "rb") as f, gzip.open(src + ".gz", "wb") as g:
                    shutil.copyfileobj(f, g)
                os.remove(src)
            except OSError:
                pass

    # ---- reading ----
    def files(self, day_from=None, day_to=None):
        out = []
        for name in sorted(os.listdir(self.dir)):
            day = name[:10]
            if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
                continue
            if day_from and day < day_from:
                continue
            if day_to and day > day_to:
                continue
            out.append(os.path.join(self.dir, name))
        return out

    def events(self, day_from=None, day_to=None):
        for path in self.files(day_from, day_to):
            opener = gzip.open if path.endswith(".gz") else open
            try:
                with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue
            except OSError:
                continue

    def stats(self):
        files = self.files()
        days = sorted(set(os.path.basename(p)[:10] for p in files))
        size = sum(os.path.getsize(p) for p in files)
        today = time.strftime("%Y-%m-%d")
        today_events = 0
        p = os.path.join(self.dir, today + ".jsonl")
        if os.path.exists(p):
            with open(p, "rb") as f:
                today_events = sum(1 for _ in f)
        return {"days": len(days), "first": days[0] if days else None, "last": days[-1] if days else None,
                "bytes": size, "today_events": today_events, "dir": self.dir}


class StreamCapture:
    """Assemble one chat completion from the SSE bytes as they pass through
    the proxy, without slowing the pass-through: partial lines are buffered,
    complete `data:` lines are parsed, deltas are folded together."""

    def __init__(self):
        self.buf = b""
        self.content = []
        self.reasoning = []
        self.calls = {}
        self.finish = None
        self.usage = None
        self.chunks = 0
        self.first_at = None
        self.error = None

    def feed(self, chunk):
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if obj.get("error"):
                self.error = obj["error"] if isinstance(obj["error"], str) else json.dumps(obj["error"])[:500]
            if obj.get("usage"):
                self.usage = obj["usage"]
            c0 = (obj.get("choices") or [None])[0]
            if not c0:
                continue
            if c0.get("finish_reason"):
                self.finish = c0["finish_reason"]
            d = c0.get("delta") or {}
            got = False
            if d.get("content"):
                self.content.append(d["content"]); got = True
            r = d.get("reasoning_content") or d.get("reasoning")
            if r:
                self.reasoning.append(r); got = True
            for tc in d.get("tool_calls") or []:
                got = True
                i = tc.get("index") or 0
                slot = self.calls.setdefault(i, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if got:
                self.chunks += 1
                if self.first_at is None:
                    self.first_at = time.time()

    def result(self, started):
        calls = [{"id": c["id"], "type": "function",
                  "function": {"name": c["name"], "arguments": c["arguments"]}}
                 for _, c in sorted(self.calls.items())]
        return {
            "content": "".join(self.content),
            "reasoning": "".join(self.reasoning),
            "tool_calls": calls,
            "finish_reason": self.finish,
            "usage": self.usage,
            "chunks": self.chunks,
            "ttft_ms": int((self.first_at - started) * 1000) if self.first_at else None,
            "error": self.error,
        }


# ------------------------------------------------------------------ export --
_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)((?:api[_-]?key|secret|token|passwd|password|pwd)(?:\\\")?\s*[:=]\s*(?:\\\")?)([^\s\\\"',;]{6,})"),
]


def redact(text):
    """Heuristic. Catches the common shapes of keys and passwords in prose and
    terminal output; it will miss some and may blank an odd hash. Applied to
    the serialized line so it covers every field at once."""
    for pat in _SECRETS:
        if pat.groups >= 1:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


def _ok_call(tc):
    try:
        json.loads(((tc.get("function") or {}).get("arguments")) or "{}")
        return True
    except (ValueError, AttributeError):
        return False


def session_turn_messages(m):
    """One saved bot message -> the assistant/tool messages it stands for, in
    order: each recorded hop (text + calls, then results), then the answer.
    Mirrors buildMsgs in console.js, minus the per-model thinking rules: the
    export keeps reasoning wherever it exists."""
    out = []
    hops = m.get("hops") or ([{"content": "", "reasoning": m.get("reasoning") or "",
                                "tool_calls": m["tool_calls"], "results": m.get("toolResults") or []}]
                              if m.get("tool_calls") else [])
    for h in hops:
        calls = [tc for tc in (h.get("tool_calls") or []) if _ok_call(tc)]
        text = h.get("content") or ""
        if not calls:
            if text:
                out.append({"role": "assistant", "content": text})
            continue
        e = {"role": "assistant", "content": text or None, "tool_calls": calls}
        if h.get("reasoning"):
            e["reasoning_content"] = h["reasoning"]
        out.append(e)
        ids = set(tc.get("id") for tc in calls)
        for t in (h.get("results") or []):
            if t.get("id") in ids:
                out.append({"role": "tool", "tool_call_id": t["id"], "content": t.get("content") or ""})
    content = m.get("content") or ""
    if content or not hops:
        final = {"role": "assistant", "content": content}
        if m.get("reasoning") and not m.get("hops"):
            final["reasoning_content"] = m["reasoning"]
        out.append(final)
    return out


def examples_from_session(sess, source, skip_turns=(), include_errors=False):
    """Walk a saved session (or an archive slice) and emit one example per
    bot turn: the history before it, then the turn itself."""
    history = []
    for m in sess.get("messages") or []:
        role = m.get("role")
        if role == "user":
            history.append({"role": "user", "content": m.get("content") or ""})
            continue
        if role != "bot" or m.get("kind") == "compress":
            continue
        turn = session_turn_messages(m)
        if not turn:
            continue
        failed = bool(m.get("error")) and not (m.get("content") or "").strip()
        skip = m.get("turn") in skip_turns or (failed and not include_errors) \
            or m.get("kind") == "summary-ack"
        if not skip and history:
            yield {
                "messages": history + turn,
                "model": m.get("model") or sess.get("model") or "",
                "meta": {"source": source, "session": sess.get("id") or sess.get("session"),
                         "turn": m.get("turn"), "rating": m.get("rating") or 0,
                         "error": m.get("error") or None},
            }
        history = history + turn


def examples_from_traces(events, include_errors=False):
    """Group chat events by turn; the last successful hop holds the whole
    trajectory, and earlier hops lend their reasoning to the assistant
    messages the final request replays without it."""
    groups = {}
    order = []
    ratings = {}
    for e in events:
        k = e.get("kind")
        if k == "rating":
            for tid in e.get("traces") or []:
                ratings[("trace", tid)] = e.get("rating") or 0
            if e.get("turn"):
                ratings[("turn", e["turn"])] = e.get("rating") or 0
            continue
        if k != "chat":
            continue
        key = ("turn", e.get("turn")) if e.get("turn") else ("trace", e.get("id"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    for key in order:
        hops = sorted(groups[key], key=lambda e: e.get("ts") or 0)
        good = [e for e in hops if e.get("status") == 200 and e.get("response")
                and not (e["response"].get("error"))]
        pick = good[-1] if good else (hops[-1] if include_errors else None)
        if not pick or not pick.get("request"):
            continue
        req = pick["request"]
        msgs = json.loads(json.dumps(req.get("messages") or []))
        base = len((hops[0].get("request") or {}).get("messages") or msgs)
        earlier = [i for i, m in enumerate(msgs) if i >= base and m.get("role") == "assistant"]
        for k, i in enumerate(earlier):
            if k < len(good) - 1:
                r = (good[k].get("response") or {}).get("reasoning")
                if r and "reasoning_content" not in msgs[i]:
                    msgs[i]["reasoning_content"] = r
        resp = pick.get("response") or {}
        final = {"role": "assistant", "content": resp.get("content") or None}
        if resp.get("reasoning"):
            final["reasoning_content"] = resp["reasoning"]
        if resp.get("tool_calls"):
            final["tool_calls"] = resp["tool_calls"]
        msgs.append(final)
        rating = ratings.get(key, 0)
        if not rating:
            for e in hops:
                rating = ratings.get(("trace", e.get("id")), 0) or rating
        yield {
            "messages": msgs,
            "tools": req.get("tools") or [],
            "model": req.get("model") or "",
            "meta": {"source": "trace", "session": pick.get("session"), "turn": pick.get("turn"),
                     "purpose": pick.get("purpose") or "chat", "ts": pick.get("ts"),
                     "finish_reason": resp.get("finish_reason"), "usage": resp.get("usage"),
                     "hops": len(hops), "rating": rating, "error": resp.get("error") or pick.get("error")},
        }


def export_lines(trace, sessions, archives, day_from=None, day_to=None, model=None,
                 rated=None, include_errors=False, do_redact=True, source="all", purpose=None):
    """Yield JSONL lines. `sessions` and `archives` are lists of records in
    the sessions-file shape; turns that the trace log already covers are
    skipped there so nothing is emitted twice."""
    seen_turns = set()
    exs = []
    if source in ("all", "traces"):
        for ex in examples_from_traces(trace.events(day_from, day_to), include_errors):
            if ex["meta"].get("turn"):
                seen_turns.add(ex["meta"]["turn"])
            exs.append(ex)
    if source in ("all", "sessions"):
        for s in sessions or []:
            exs.extend(examples_from_session(s, "session", seen_turns, include_errors))
        for a in archives or []:
            exs.extend(examples_from_session(a, "archive", seen_turns, include_errors))
    for ex in exs:
        if model and model.lower() not in (ex.get("model") or "").lower():
            continue
        if rated == "up" and (ex["meta"].get("rating") or 0) <= 0:
            continue
        if rated == "any" and not ex["meta"].get("rating"):
            continue
        if purpose and (ex["meta"].get("purpose") or "chat") != purpose:
            continue
        line = json.dumps(ex, ensure_ascii=False)
        if do_redact:
            line = redact(line)
        yield line + "\n"
