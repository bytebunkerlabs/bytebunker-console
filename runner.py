"""The chat engine, on the server: one turn of a conversation, from the
user's message to the last token, with tools, for any client (the app, bb,
a job, a workflow), recorded once in the same session file, trace log and
usage ledger whoever started it.

A port of the Playground's loop (console.js send, streamTurn, buildMsgs,
compress), kept in step with it until the Playground moves onto this:
the same context fitting and overflow retries, the same message rebuild,
the same tool hops, the same records. Where the two must differ the
comment says why.

    r = Runner(deps)
    run = runs.start("chat", source, lambda run: r.turn(run, emit, sid, request))

The turn reports what happens through emit(type, data) as it happens:
  status {text, slow}       connecting, prefilling, a quiet wire, a tool call being written
  delta {content|reasoning} tokens as they arrive
  tool_args {name, chars}   a tool call's arguments streaming in
  tool_call {id, name, args}
  tool_result {id, name, content, chars, is_error}
  notice {text}             clamped max tokens, a dropped truncated call, a pause
  done {message, meta}      the finished assistant message, as saved
"""
import base64
import json
import math
import mimetypes
import os
import re
import socket
import threading
import time
import uuid

import upstream
from traces import StreamCapture

# the browser's effort scale; "high" was its default and was never sent
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh")
ABSTRACT_EFFORT = ("off", "low", "medium", "high", "max")
STALL_PREFILL = 45
STALL_STREAM = 10
KEEP_RECENT = 2              # user turns compression keeps verbatim
SUMMARY_TOKENS = 8192        # output budget for a summary
TOOL_TEXT_TO_MODEL = 20000   # characters of a tool result the model sees
TOOL_TEXT_STORED = 65536     # characters of it the session keeps
CAPS_FALLBACK = {"tools": True, "effort": [], "ctk": {}, "strip_reasoning": True, "ctx": 131072}

COMPRESS_SYS = (
    "You are compressing a conversation so that it can continue in a smaller context window. "
    "Write a dense, factual summary a colleague could continue from without the original. "
    "Keep: the user's goals and constraints; decisions made and why; facts, numbers, file paths, "
    "commands, identifiers, URLs and error messages, quoted exactly when they will be needed again; "
    "results of tool calls that still matter; what was tried and failed; open questions and next "
    "steps; standing instructions about tone or format. Drop pleasantries and superseded drafts. "
    "Plain text with short headed sections. No preamble, no commentary, no offer to help.")
COMPRESS_ASK = ("Compress everything above into that summary now. Detail over brevity — "
                "up to about 3,000 words.")


class Cancelled(Exception):
    """The client stopped the turn (Stop, Ctrl-C): not an error."""


class TurnError(Exception):
    pass


# ------------------------------------------------------------------ helpers --
def _js_len(s):
    """String length as JavaScript counts it (UTF-16 code units)."""
    return len(s.encode("utf-16-le")) // 2


def _js_round(x):
    """Math.round: halves go up (Python's round goes to even)."""
    return int(math.floor(x + 0.5))


def _js_json(obj):
    """JSON.stringify's shape: no spaces, unicode as is."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _parses(s):
    """JSON.parse would accept it (no NaN or Infinity, unlike Python's default)."""
    def refuse(c):
        raise ValueError(c)
    try:
        json.loads(s, parse_constant=refuse)
        return True
    except ValueError:
        return False


def strip_think(s):
    """History resent to the model carries no think blocks."""
    return re.sub(r"<think>[\s\S]*?(</think>|\Z)", "", s or "").strip()


def parse_ctx_error(text):
    """vLLM's refusal of prompt + max_tokens > window: {win, prompt, bound}.
    bound: the newer phrasing's "at least N" is a floor, not the prompt."""
    win = re.search(r"maximum context length is (\d+)", text or "")
    if not win:
        return None
    m = re.search(r"prompt contains (at least )?(\d+) input tokens", text)
    if m:
        return {"win": int(win.group(1)), "prompt": int(m.group(2)), "bound": bool(m.group(1))}
    m = re.search(r"\((\d+) in the messages", text)
    return {"win": int(win.group(1)), "prompt": int(m.group(1)), "bound": False} if m else None


def _prompt_shape(msgs):
    out = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            m = dict(m, content=[{"type": "image", "placeholder": "x" * 4000}
                                 if isinstance(p, dict) and p.get("type") == "image_url" else p for p in c])
        out.append(m)
    return out


def estimate_prompt(msgs, tools, calib, model):
    """Tokens in a prompt: characters / 3.2, or calibrated against the
    engine's own count of the last prompt it saw for this model. margin is
    what to leave unspent."""
    chars = _js_len(_js_json({"m": _prompt_shape(msgs), "t": tools or None}))
    k = calib or {}
    cal = bool(k and k.get("model") == model and k.get("tokens", 0) > 1000 and k.get("chars", 0) > 4000)
    ratio = min(6, max(2, k["chars"] / k["tokens"])) if cal else 3.2
    tokens = math.ceil(chars / ratio)
    fresh = max(0, chars - k["chars"]) if cal else 0
    margin = 256 + math.ceil(tokens * (0.01 if cal else 0.02)) + math.ceil(fresh / ratio * 0.25)
    return {"tokens": tokens, "margin": margin, "calibrated": cal}


def compress_limit(win, max_tokens):
    """Where the prompt has to stay: room for the answer, never past 80%."""
    return max(math.floor(win * 0.3), min(math.floor(win * 0.8), win - max_tokens - 4096))


def effort_for(effort, caps):
    """The model's own effort level for an abstract one (off, low, medium,
    high, max), or a level the model names itself; None sends nothing.
    Only levels the model accepts are sent: DeepSeek's encoder asserts on
    the value, so a wrong one is a 500, not a no-op."""
    levels = caps.get("effort") or []
    if not effort or not levels:
        return None
    if effort in levels:
        return effort
    nearest = {"off": ("none", "minimal"), "low": ("minimal",), "medium": ("low", "high"),
               "high": ("xhigh", "medium"), "max": ("xhigh", "max", "high")}.get(effort, ())
    return next((lv for lv in nearest if lv in levels), None)


def _fmt_bytes(n):
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576)
    if n >= 1024:
        return "%d KB" % round(n / 1024)
    return "%d B" % n


def user_content(m):
    """Attachments go to the model three ways: images as image parts, text
    files inline, everything else as the path it was saved under."""
    atts = m.get("attachments") or []
    if not atts:
        return m.get("content")
    parts = []
    text = m.get("content") or ""
    for a in atts:
        if a.get("kind") == "image":
            if a.get("dataURL"):
                parts.append({"type": "image_url", "image_url": {"url": a["dataURL"]}})
            else:
                text += ("\n\n[image attached: %s — not available in this session; re-attach to show it "
                         "to the model]" % a.get("name"))
        elif a.get("kind") == "text" and a.get("text") is not None:
            text += "\n\n--- attached file: %s%s ---\n%s\n--- end of %s ---" % (
                a.get("name"), " (saved at %s)" % a["path"] if a.get("path") else "", a["text"], a.get("name"))
        else:
            text += "\n\n[attached file: %s%s%s]" % (
                a.get("name"), ", " + _fmt_bytes(a["size"]) if a.get("size") else "",
                ", saved at %s — read it with the filesystem or terminal tools" % a["path"] if a.get("path") else "")
    parts.insert(0, {"type": "text", "text": text.strip() or "(see attachments)"})
    return parts


def build_msgs(messages, caps, system):
    """The transcript the way the model wants it: system first, prior
    thinking stripped or carried per the capability table, every tool hop
    replayed (a model shown only its last hop redoes the others), broken
    tool calls scrubbed on every rebuild."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in messages:
        if m.get("role") == "bot":
            if m.get("kind") == "compress":
                continue                      # a summary in flight, or one that failed
            hops = m.get("hops") or ([{"content": "", "reasoning": m.get("reasoning") or "",
                                       "tool_calls": m["tool_calls"], "results": m.get("toolResults") or []}]
                                     if m.get("tool_calls") else [])
            for h in hops:
                ok_calls = [tc for tc in (h.get("tool_calls") or [])
                            if _parses((tc.get("function") or {}).get("arguments") or "{}")]
                text = strip_think(h.get("content") or "") if caps.get("strip_reasoning") else (h.get("content") or "")
                if not ok_calls:
                    if text:
                        msgs.append({"role": "assistant", "content": text})
                    continue
                e = {"role": "assistant", "content": text, "tool_calls": ok_calls}
                # DeepSeek 400s when a tool exchange arrives without the
                # thinking that produced it; most models must never see it
                if not caps.get("strip_reasoning") and h.get("reasoning"):
                    e["reasoning_content"] = h["reasoning"]
                msgs.append(e)
                ok_ids = set(tc.get("id") for tc in ok_calls)
                for t in h.get("results") or []:
                    if t.get("id") in ok_ids:
                        msgs.append({"role": "tool", "tool_call_id": t["id"], "content": t.get("content")})
            answer = strip_think(m.get("content")) if caps.get("strip_reasoning") else m.get("content")
            if answer:
                msgs.append({"role": "assistant", "content": answer})
        else:
            msgs.append({"role": "user", "content": user_content(m)})
    return msgs


def serialize_msg(m):
    """A message as a session stores it (the Playground's serializeMsg)."""
    o = {"role": m.get("role"), "content": m.get("content"), "reasoning": m.get("reasoning") or "",
         "meta": m.get("meta") or "", "model": m.get("model") or "", "effort": m.get("effort"),
         "error": m.get("error") or "", "toolUse": m.get("toolUse") or [],
         "tool_calls": m.get("tool_calls") or None, "toolResults": m.get("toolResults") or [],
         "hops": m.get("hops") or None}
    if m.get("kind"):
        o.update(kind=m["kind"], archive=m.get("archive") or "", count=m.get("count") or 0)
    for k in ("turn", "traces", "rating", "notice"):
        if m.get(k):
            o[k] = m[k]
    if m.get("skills"):
        o["skills"] = m["skills"]
    if m.get("attachments"):
        o["attachments"] = [{"kind": a.get("kind"), "name": a.get("name"), "size": a.get("size"),
                             "mime": a.get("mime"), "url": a.get("url"), "path": a.get("path"),
                             "text": (a.get("text") or "")[:120000] if a.get("kind") == "text" else None}
                            for a in m["attachments"]]
        for a in o["attachments"]:
            for k in [k for k, v in a.items() if v is None]:
                del a[k]
    return o


def new_turn_id():
    return "t-%s%s" % (format(int(time.time() * 1000), "x"), uuid.uuid4().hex[:4])


# ------------------------------------------------------------------- runner --
class Runner:
    """deps: registry (gateways), caps_for, facts (ModelFacts), mcp (a
    function returning the MCP host), trace, record_usage(evt, **extra),
    sessions, on_session(summary), skill_bodies(names) -> {name: body},
    write_archive, uploads_root(), engine_stats()."""

    def __init__(self, deps):
        self.d = deps
        self.calib = {}              # session id -> {model, tokens, chars}: the engine's count of its last prompt

    # ------------------------------------------------------------ pieces
    def caps(self, model):
        c = dict(CAPS_FALLBACK)
        c.update(self.d.caps_for(model) or {})
        return self.d.facts.caps(model, c)

    def system_prompt(self, req):
        parts = []
        bodies = self.d.skill_bodies(req.get("skills") or [])
        for name in req.get("skills") or []:
            if bodies.get(name):
                parts.append("# Skill: " + name + "\n\n" + bodies[name])
        if (req.get("system") or "").strip():
            parts.append(req["system"].strip())
        return "\n\n---\n\n".join(parts)

    def tool_defs(self, caps, req):
        if not caps.get("tools") or req.get("tools") is False:
            return None
        try:
            defs = self.d.mcp().openai_tools()
        except Exception:   # noqa: BLE001 - no tools is better than no chat
            defs = []
        allow = req.get("tools")
        if isinstance(allow, list):
            defs = [t for t in defs if t["function"]["name"] in allow]
        defs = defs + list(req.get("client_tools") or [])
        return defs or None

    def hydrate_images(self, messages):
        """Saved images are files; a request carries them as data URLs."""
        root = os.path.realpath(self.d.uploads_root())
        for m in messages:
            for a in m.get("attachments") or []:
                if a.get("kind") != "image" or a.get("dataURL") or not a.get("path"):
                    continue
                p = os.path.realpath(os.path.expanduser(a["path"]))
                if os.path.commonpath([p, root]) != root or not os.path.isfile(p):
                    continue
                mime = mimetypes.guess_type(p)[0] or a.get("mime") or "application/octet-stream"
                with open(p, "rb") as f:
                    a["dataURL"] = "data:%s;base64,%s" % (mime, base64.b64encode(f.read()).decode("ascii"))

    # ------------------------------------------------------------ one request
    def stream_turn(self, run, emit, sid, msgs, bot, body0, purpose="chat"):
        """One model request, streamed into bot. Returns what the hop needs:
        calls, broken_calls, usage, finish_reason, chunks, ttft, span."""
        t0 = time.time()
        st = {"phase": "connect", "first": None, "last": None, "kind": "text", "tool_chars": 0,
              "tool_name": "", "shown": ""}
        calls = {}
        usage, finish, chunks = None, None, 0
        done = threading.Event()

        def heartbeat():
            # which phase the turn is in and for how long: "the model is
            # thinking" and "the connection died" must not look the same
            eng, tick = None, 0
            while not done.wait(0.5):
                el = time.time() - t0
                slow = False
                if st["phase"] == "connect":
                    text = "connecting to %s…" % (bot.get("model") or "model")
                    slow = el > 15
                elif st["first"] is None:
                    text = "prefilling · %.1fs" % el
                    if el > 20 and tick % 4 == 0:
                        eng = self._engine()
                    tick += 1
                    if eng and (eng.get("prompt_rate") or 0) > 50:
                        text += " · engine chewing %s tok/s of prompt" % format(round(eng["prompt_rate"]), ",")
                    elif el > STALL_PREFILL:
                        slow = True
                        text += " — no first token yet; Stop to cancel"
                else:
                    gap = time.time() - st["last"]
                    if gap >= STALL_STREAM:
                        if tick % 4 == 0:
                            eng = self._engine()
                        tick += 1
                        if eng and (eng.get("rate") or 0) > 0.5:
                            text = ("wire quiet %ds · engine generating %d tok/s — output is buffered "
                                    "upstream (usually a tool call being assembled)" % (gap, eng["rate"]))
                            slow = gap > 180
                        elif eng and (eng.get("rate") or 0) <= 0.5 and eng.get("running") == 0:
                            slow = True
                            text = "no tokens for %ds — nothing running on the engine. Stop and retry." % gap
                        else:
                            slow = True
                            text = "no tokens for %ds — stream may have stalled" % gap
                    else:
                        eng, tick = None, 0
                        text = ("writing a tool call%s · %s chars of arguments" % (
                            " · " + st["tool_name"] if st["tool_name"] else "", format(st["tool_chars"], ","))
                                if st["kind"] == "tool" else "")
                if text != st["shown"]:
                    st["shown"] = text
                    emit("status", {"text": text, "slow": slow} if text else None)

        threading.Thread(target=heartbeat, name="hb-" + run.id, daemon=True).start()
        resp = None
        try:
            mid = bot.get("model")
            caps = self.caps(mid)
            ctx_win = caps.get("ctx") or 131072
            est = estimate_prompt(msgs, body0.get("tools"), self.calib.get(sid), mid)
            room = ctx_win - est["tokens"] - est["margin"]
            if room < 256:
                raise TurnError("context is full: the prompt is ~%s tokens of a %s-token window. Type /compress "
                                "to fold older turns into a summary, or start a New chat." % (
                                    format(est["tokens"], ","), format(ctx_win, ",")))
            body1 = dict(body0, messages=msgs)
            if body1.get("max_tokens") and body1["max_tokens"] > room:
                body1["max_tokens"] = room
                self._notice(emit, bot, "Max tokens clamped to %s for this hop — the prompt already uses ~%s "
                             "of %s context tokens." % (format(room, ","), format(est["tokens"], ","),
                                                        format(ctx_win, ",")))
            trace_id = self.d.trace.new_id()
            who = {"id": trace_id, "session": sid, "turn": bot.get("turn"), "purpose": purpose}
            started = time.time()
            attempt = 0
            while True:
                if run.cancelled:
                    raise Cancelled()
                try:
                    payload = json.loads(json.dumps(body1))      # the proxy's view: its own copy, changed in place
                    resp, gw = upstream.open_chat(self.d.registry, payload)
                    who["gateway"] = gw["name"] if gw else None
                    break
                except upstream.UpstreamError as e:
                    who["gateway"] = e.gateway["name"] if e.gateway else None
                    self.d.trace.log("chat", status=e.status, request=upstream.trace_safe(payload), response=None,
                                     error=e.message[:1000], ms=int((time.time() - started) * 1000), **who)
                    err = e.message[:800]
                over = parse_ctx_error(err)
                if over:
                    # the engine names the window it really serves: believe it
                    if over["win"] != ctx_win:
                        self.d.facts.learn_ctx(mid, over["win"])
                        ctx_win = over["win"]
                    if attempt >= 4:
                        raise TurnError("still over the context window after 4 retries — type /compress to "
                                        "fold older turns into a summary. Engine said: " + err[:300])
                    # "at least N" is a floor: back off below it geometrically
                    left = over["win"] - over["prompt"]
                    margin = (min(math.ceil(over["win"] * 0.005 * (4 ** attempt)), left // 2)
                              if over["bound"] else 64)
                    fit = left - margin
                    if fit < 128:
                        raise TurnError("context is full: the prompt is %s%s tokens of a %s-token window. Type "
                                        "/compress, or start a New chat." % (
                                            "over " if over["bound"] else "", format(over["prompt"], ","),
                                            format(over["win"], ",")))
                    body1["max_tokens"] = min(body1.get("max_tokens") or fit, fit)
                    self._notice(emit, bot, "Max tokens clamped to %s — the prompt is %s%s of %s context "
                                 "tokens." % (format(fit, ","), "over " if over["bound"] else "",
                                              format(over["prompt"], ","), format(over["win"], ",")))
                    attempt += 1
                    who["id"] = trace_id = self.d.trace.new_id()
                    continue
                # served without --enable-auto-tool-choice: answer without tools, and say why
                if body1.get("tools") and re.search(r"enable-auto-tool-choice|tool-call-parser|tool choice", err, re.I):
                    body1.pop("tools", None)
                    body1.pop("tool_choice", None)
                    self._notice(emit, bot, "This model is not served with tool support (needs "
                                 "--enable-auto-tool-choice and --tool-call-parser). Answered without tools.")
                    attempt += 1               # every retry counts, as the Playground's loop counts them
                    who["id"] = trace_id = self.d.trace.new_id()
                    continue
                raise TurnError(err[:400])
            st["phase"] = "stream"
            bot["traces"] = (bot.get("traces") or []) + [trace_id]
            cap = StreamCapture()
            buf = b""
            cancel_watch = threading.Thread(target=self._close_on_cancel, args=(run, resp, done), daemon=True)
            cancel_watch.start()
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    cap.feed(chunk)
                    buf += chunk
                    lines = buf.split(b"\n")
                    buf = lines.pop()
                    for raw in lines:
                        line = raw.decode("utf-8", "replace")
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        if obj.get("error"):
                            raise TurnError(obj["error"] if isinstance(obj["error"], str) else json.dumps(obj["error"]))
                        if obj.get("usage"):
                            usage = obj["usage"]
                        c0 = (obj.get("choices") or [None])[0]
                        if c0 and c0.get("finish_reason"):
                            finish = c0["finish_reason"]
                        delta = (c0 or {}).get("delta")
                        if not delta:
                            continue
                        for tc in delta.get("tool_calls") or []:
                            i = tc.get("index") or 0
                            c = calls.setdefault(i, {"id": "", "name": "", "args": ""})
                            fn = tc.get("function") or {}
                            if tc.get("id"):
                                c["id"] = tc["id"]
                            if fn.get("name"):
                                c["name"] += fn["name"]
                            if fn.get("arguments"):
                                c["args"] += fn["arguments"]
                        if delta.get("tool_calls"):
                            # argument fragments ARE tokens: a model writing a
                            # file into a call streams for minutes with no text
                            now = time.time()
                            if st["first"] is None:
                                st["first"] = now
                            st["last"] = now
                            st["kind"] = "tool"
                            st["tool_chars"] = sum(len(c["args"]) for c in calls.values())
                            last = calls[max(calls)] if calls else {}
                            st["tool_name"] = last.get("name") or st["tool_name"]
                            emit("tool_args", {"name": st["tool_name"], "chars": st["tool_chars"]})
                        got = (delta.get("content") or "") + (delta.get("reasoning_content") or "") + (delta.get("reasoning") or "")
                        if got:
                            now = time.time()
                            if st["first"] is None:
                                st["first"] = now
                            st["last"] = now
                            chunks += 1
                            st["kind"] = "text"
                            out = {}
                            r = (delta.get("reasoning_content") or "") + (delta.get("reasoning") or "")
                            if r:
                                bot["reasoning"] = (bot.get("reasoning") or "") + r
                                out["reasoning"] = r
                            if delta.get("content"):
                                bot["content"] = (bot.get("content") or "") + delta["content"]
                                out["content"] = delta["content"]
                            emit("delta", out)
                if run.cancelled:
                    raise Cancelled()
                if not cap.error and cap.finish is None and not cap.done:
                    cap.error = ("the model server closed the stream before the reply finished (%d chunks "
                                 "received)" % cap.chunks)
                    raise TurnError(cap.error)
            except (TurnError, Cancelled):
                raise
            except Exception as e:   # noqa: BLE001 - the wire failed, or Stop shut it
                if run.cancelled:
                    raise Cancelled()
                cap.error = cap.error or "upstream stream failed: " + str(e)[:200]
                raise TurnError(cap.error)
            finally:
                if run.cancelled:
                    cap.error = cap.error or "client disconnected"
                self.d.trace.log("chat", status=200, request=upstream.trace_safe(payload),
                                 response=cap.result(started), ms=int((time.time() - started) * 1000), **who)
            ok_calls, broken = [], []
            for i in sorted(calls):
                c = calls[i]
                (ok_calls if _parses(c["args"] or "{}") else broken).append(c)
            # the engine counted this prompt: that count calibrates the next estimate
            if usage and (usage.get("prompt_tokens") or 0) > 0:
                self.calib[sid] = {"model": mid, "tokens": usage["prompt_tokens"],
                                   "chars": _js_len(_js_json({"m": msgs, "t": body0.get("tools") or None}))}
            return {"calls": ok_calls, "broken_calls": broken, "usage": usage, "finish_reason": finish,
                    "chunks": chunks, "ttft": (st["first"] - t0) if st["first"] else None,
                    "span": (st["last"] - st["first"]) if st["first"] and st["last"] and st["last"] > st["first"] else None}
        finally:
            done.set()
            emit("status", None)
            if resp is not None:
                try:
                    resp.close()
                except Exception:   # noqa: BLE001
                    pass

    @staticmethod
    def _close_on_cancel(run, resp, done):
        """Stop means stop now. Closing a response from another thread does
        not wake a read blocked on its socket (Linux); shutting the socket
        down does."""
        while not done.is_set():
            if run.cancel_event.wait(0.2):
                try:
                    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.shutdown(socket.SHUT_RDWR)
                except Exception:   # noqa: BLE001
                    pass
                try:
                    resp.close()
                except Exception:   # noqa: BLE001
                    pass
                return

    def _engine(self):
        try:
            return self.d.engine_stats() or {}
        except Exception:   # noqa: BLE001
            return {}

    @staticmethod
    def _notice(emit, bot, text):
        bot["notice"] = text
        emit("notice", {"text": text})

    # ------------------------------------------------------------ a turn
    def turn(self, run, emit, sid, req):
        """One user turn on session sid. req: text, attachments, model,
        params {temperature, top_p, top_k, repetition_penalty, max_tokens,
        seed, json, stop}, effort, system, skills, tools, client_tools,
        max_hops, auto_compress, source. Returns the run's result."""
        sess = self.d.sessions.get(sid) or {"id": sid, "messages": [], "created": int(time.time() * 1000)}
        messages = sess.setdefault("messages", [])
        text = (req.get("text") or "").strip()
        atts = list(req.get("attachments") or [])
        model = req.get("model") or sess.get("model")
        if not model:
            raise TurnError("no model: name one, or pick one in the app first")
        if not text and not atts:
            raise TurnError("nothing to send")
        params = dict(req.get("params") or {})
        max_tok = int(params.get("max_tokens") or 8192)
        self.hydrate_images(messages + [{"attachments": atts}])
        if re.match(r"^/compress\b", text, re.I):
            return self.compress(run, emit, sid, sess, req, "manual")
        if req.get("auto_compress", True):
            # fold older turns away BEFORE this one joins, so it fits with room to answer
            for _ in range(3):
                c0 = self.caps(model)
                probe = build_msgs(messages + [{"role": "user", "content": text, "attachments": atts}], c0,
                                   self.system_prompt(req))
                est = estimate_prompt(probe, self.tool_defs(c0, req), self.calib.get(sid), model)
                if est["tokens"] <= compress_limit(c0.get("ctx") or 131072, max_tok):
                    break
                did = self.compress(run, emit, sid, sess, req, "auto", est["tokens"])
                if did.get("aborted"):
                    raise Cancelled()
                if not did.get("ok"):
                    break
                messages = sess["messages"]
        user = {"role": "user", "content": text, "attachments": atts}
        bot = {"role": "bot", "content": "", "reasoning": "", "meta": "", "model": model,
               "effort": req.get("effort"), "turn": new_turn_id(), "skills": list(req.get("skills") or [])}
        messages.extend([user, bot])
        emit("turn", {"turn": bot["turn"], "model": model, "user": serialize_msg(user)})

        caps = self.caps(model)
        msgs = build_msgs(messages[:-1], caps, self.system_prompt(req))
        body = {"model": model, "temperature": params.get("temperature", 0.7), "top_p": params.get("top_p", 0.95),
                "max_tokens": max_tok, "top_k": params.get("top_k", 40),
                "repetition_penalty": params.get("repetition_penalty", 1.05)}
        if params.get("seed"):
            body["seed"] = params["seed"]
        if params.get("json"):
            body["response_format"] = {"type": "json_object"}
        if params.get("stop"):
            body["stop"] = [s for s in params["stop"] if s]
        eff = effort_for(req.get("effort"), caps)
        if eff:
            body["reasoning_effort"] = eff
        if caps.get("ctk"):
            body["chat_template_kwargs"] = caps["ctk"]
        tools = self.tool_defs(caps, req)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        usage, finish, ttft, span, chunks = None, None, None, None, 0
        tot_toks = tot_rtoks = 0
        max_hops = max(1, int(req.get("max_hops") or 40))
        last_sig, repeats = "", 0
        try:
            for hop in range(max_hops):
                r_len = len(bot["reasoning"])
                r = self.stream_turn(run, emit, sid, msgs, bot, body)
                usage = r["usage"] or usage
                if r["usage"]:
                    tot_toks += r["usage"].get("completion_tokens") or 0
                    tot_rtoks += (r["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
                finish = r["finish_reason"]
                chunks += r["chunks"]
                if ttft is None:
                    ttft = r["ttft"]
                if r["span"]:
                    span = (span or 0) + r["span"]
                if r["broken_calls"]:
                    b = r["broken_calls"][0]
                    self._notice(emit, bot, "Dropped a truncated tool call — %s arrived with %s chars of "
                                 "arguments and no closing brace. %s" % (
                                     b["name"] or "unnamed", format(len(b["args"] or ""), ","),
                                     "It hit the Max tokens ceiling: raise Max tokens and ask again."
                                     if r["finish_reason"] == "length" else "The stream was cut mid-call: just ask again."))
                if not r["calls"]:
                    break
                sig = "\n".join(c["name"] + ":" + (c["args"] or "") for c in r["calls"])
                repeats = repeats + 1 if sig == last_sig else 0
                last_sig = sig
                hop_rec = {"content": bot["content"] or "",
                           "tool_calls": [{"id": c["id"], "type": "function",
                                           "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                          for c in r["calls"]],
                           "results": []}
                if not self.caps(model).get("strip_reasoning") and len(bot["reasoning"]) > r_len:
                    hop_rec["reasoning"] = bot["reasoning"][r_len:]
                bot["hops"] = (bot.get("hops") or []) + [hop_rec]
                hop_msg = {"role": "assistant", "content": hop_rec["content"], "tool_calls": hop_rec["tool_calls"]}
                if hop_rec.get("reasoning"):
                    hop_msg["reasoning_content"] = hop_rec["reasoning"]
                msgs.append(hop_msg)
                for c in r["calls"]:
                    if run.cancelled:
                        raise Cancelled()
                    try:
                        args = json.loads(c["args"] or "{}")
                    except ValueError:
                        args = {}
                    bot["toolUse"] = (bot.get("toolUse") or []) + [
                        {"name": c["name"], "args": c["args"] or "{}", "result": "running…", "error": False}]
                    emit("tool_call", {"id": c["id"], "name": c["name"], "args": c["args"] or "{}"})
                    content, is_err = self.call_tool(run, emit, sid, bot, c, args, req)
                    full = str(content)
                    bot["toolUse"][-1]["result"] = (full[:TOOL_TEXT_STORED] + "\n…[stored copy truncated: %d "
                                                    "characters in total]" % len(full)
                                                    if len(full) > TOOL_TEXT_STORED else full)
                    bot["toolUse"][-1]["error"] = bool(is_err)
                    shown = full[:TOOL_TEXT_TO_MODEL]
                    hop_rec["results"].append({"id": c["id"], "content": shown})
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": shown})
                    emit("tool_result", {"id": c["id"], "name": c["name"], "content": full[:2000],
                                         "chars": len(full), "is_error": bool(is_err)})
                bot["content"] = ""          # the next hop writes the real answer
                emit("hop", {"n": hop + 1})
                if repeats >= 2:
                    self._notice(emit, bot, "Stopped: the model made the identical tool call three times in a row "
                                 "(%s). Tell it what to do differently, or send \"continue\"." % r["calls"][0]["name"])
                    break
                if hop == max_hops - 1:
                    self._notice(emit, bot, "Paused after %d tool hops. Send \"continue\" to keep going, or raise "
                                 "Tool hops." % max_hops)
        except Cancelled:
            pass
        except TurnError as e:
            bot["error"] = "upstream error: " + str(e)
        except Exception as e:   # noqa: BLE001 - a turn's failure is part of its record
            bot["error"] = "upstream error: " + str(e)[:400]

        exact = tot_toks > 0
        toks = tot_toks if exact else chunks
        approx = "" if exact else "~"
        decode = (toks - 1) / span if span and toks > 1 else None
        rtok = tot_rtoks or ((usage or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
        bits = [model, "%s%d tok%s" % (approx, toks, " (%d thinking)" % rtok if rtok else ""),
                "%s%.1f tok/s" % (approx, decode) if decode else None,
                "ttft %d ms" % _js_round(ttft * 1000) if ttft is not None else None]
        n_tools = len(bot.get("toolUse") or [])
        if n_tools:
            bits.append("%d tool call%s" % (n_tools, "s" if n_tools > 1 else ""))
        if bot.get("skills"):
            bits.append("%d skill%s: %s" % (len(bot["skills"]), "s" if len(bot["skills"]) > 1 else "",
                                             ", ".join(bot["skills"])))
        if finish == "length":
            bits.append("⚠ stopped at Max tokens — thinking shares the budget; raise it in the panel")
        bot["meta"] = "  ·  ".join(b for b in bits if b)
        self.save(sid, sess, model, req)
        self.d.record_usage({"model": model, "prompt_tokens": (usage or {}).get("prompt_tokens"),
                             "completion_tokens": toks, "ttft_s": ttft, "decode_tok_s": decode,
                             "estimated": not exact}, source=req.get("source") or "app", session=sid)
        stored = serialize_msg(bot)
        emit("done", {"message": stored, "meta": bot["meta"], "usage": usage})
        return {"ok": not bot.get("error"), "error": bot.get("error") or None, "session": sid,
                "turn": bot["turn"], "cancelled": run.cancelled, "tokens": toks}

    def call_tool(self, run, emit, sid, bot, call, args, req):
        """Run one tool call. Returns (content, is_error)."""
        t0 = time.time()
        try:
            text, is_err = self.d.mcp().call(call["name"], args)
        except Exception as e:   # noqa: BLE001
            text, is_err = "console could not reach the tool: %s" % e, True
        self.d.trace.log("tool", session=sid, turn=bot.get("turn"), name=call["name"], arguments=args,
                         result=str(text)[:200000], is_error=bool(is_err), ms=int((time.time() - t0) * 1000))
        return text, is_err

    def save(self, sid, sess, model, req):
        msgs = sess["messages"]
        first = next((m for m in msgs if m.get("role") == "user" and ((m.get("content") or "").strip()
                                                                       or m.get("attachments"))), None)
        if first:
            title = ((first.get("content") or "").strip()
                     or "\U0001f4c4 " + ", ".join(a.get("name") or "" for a in first.get("attachments") or []))[:80]
        else:
            title = "untitled"
        rec = {"id": sid, "title": title, "model": model, "turns": len(msgs),
               "chars": sum(_js_len(m.get("content") or "") if isinstance(m.get("content"), str) else 0 for m in msgs),
               "activeSkills": list(req.get("skills") or []), "updated": int(time.time() * 1000),
               "source": req.get("source") or sess.get("source") or "app",
               "messages": [serialize_msg(m) for m in msgs]}
        if sess.get("cwd") or req.get("cwd"):
            rec["cwd"] = req.get("cwd") or sess.get("cwd")
        summary = self.d.sessions.put(rec)
        self.d.on_session(summary)

    # ------------------------------------------------------------ compression
    def compress(self, run, emit, sid, sess, req, why, est_tokens=None):
        """The model writes a dense summary of the older part of the
        conversation; the originals go to the archive; the summary takes
        their place. The newest KEEP_RECENT user turns stay verbatim."""
        model = req.get("model") or sess.get("model")
        caps = self.caps(model)
        win = caps.get("ctx") or 131072
        lst = [m for m in sess["messages"] if m.get("kind") != "compress"]
        cut, seen = len(lst), 0
        for i in range(len(lst) - 1, -1, -1):
            if lst[i].get("role") == "user":
                seen += 1
                if seen == KEEP_RECENT:
                    cut = i
                    break
        old = lst[:cut]
        if not old:
            emit("notice", {"text": "Nothing older than the last %d turns to compress." % KEEP_RECENT})
            return {"ok": False}
        while True:
            convo = build_msgs(old, caps, COMPRESS_SYS)
            e = estimate_prompt(convo, None, self.calib.get(sid), model)
            if e["tokens"] + e["margin"] + SUMMARY_TOKENS <= win:
                break
            if len(old) <= 1:
                emit("notice", {"text": "cannot compress: even one message is too big for the summarizer's window"})
                return {"ok": False}
            old = lst[:self._cut_at_user(lst, math.ceil(len(old) / 2))]
        last = convo[-1] if convo else None
        if last and last.get("role") == "user":
            convo[-1] = {"role": "user", "content": str(last.get("content")) + "\n\n" + COMPRESS_ASK}
        else:
            convo.append({"role": "user", "content": COMPRESS_ASK})
        note = ("Context is at ~%s of %s tokens: " % (format(est_tokens, ","), format(win, ","))
                if why == "auto" and est_tokens else "") + "compressing %d older messages into a summary…" % len(old)
        tmp = {"role": "bot", "kind": "compress", "content": "", "reasoning": "", "meta": "", "model": model,
               "effort": req.get("effort"), "turn": new_turn_id()}
        emit("compress", {"text": note, "count": len(old)})
        body = {"model": model, "temperature": 0.2, "top_p": 0.9, "max_tokens": SUMMARY_TOKENS}
        if caps.get("ctk"):
            body["chat_template_kwargs"] = caps["ctk"]
        for lv in ("low", "minimal", "none"):       # transcription, not reasoning
            if lv in (caps.get("effort") or []):
                body["reasoning_effort"] = lv
                break
        try:
            self.stream_turn(run, emit, sid, convo, tmp, body, purpose="compress")
            summary = strip_think(tmp["content"]).strip()
            if not summary:
                raise TurnError("the model returned an empty summary")
            try:
                filed = self.d.write_archive({"session": sid, "model": model, "summary": summary,
                                              "messages": [serialize_msg(m) for m in old]})
            except OSError:
                filed = ""
            marker = {"role": "user", "kind": "summary", "count": len(old), "archive": filed or "",
                      "content": "[Earlier conversation compressed. What follows is a summary of %d messages%s.]"
                                 "\n\n%s" % (len(old), ", archived as " + filed if filed else "", summary)}
            ack = {"role": "bot", "kind": "summary-ack", "model": model,
                   "content": "Understood. I have the summary of the earlier conversation and will continue from it."}
            sess["messages"] = [marker, ack] + lst[len(old):]
            self.calib.pop(sid, None)
            self.save(sid, sess, model, req)
            emit("compressed", {"count": len(old), "archive": filed or ""})
            return {"ok": True, "count": len(old), "archive": filed or ""}
        except Cancelled:
            return {"ok": False, "aborted": True}
        except Exception as e:   # noqa: BLE001
            # a failed compression stays in the transcript, as the Playground keeps it
            tmp["error"] = "compression failed: " + str(e)[:300]
            sess["messages"].append(tmp)
            self.save(sid, sess, model, req)
            emit("notice", {"text": tmp["error"]})
            return {"ok": False, "error": tmp["error"]}

    def compress_session(self, run, emit, sid, req):
        """POST /api/sessions/<id>/compress."""
        sess = self.d.sessions.get(sid)
        if sess is None:
            raise TurnError("no such session")
        res = self.compress(run, emit, sid, sess, req, "manual")
        return dict(res, ok=bool(res.get("ok")) or not res.get("error"))

    @staticmethod
    def _cut_at_user(lst, j):
        for i in range(min(j, len(lst) - 1), 0, -1):
            if lst[i].get("role") == "user":
                return i
        return j
