#!/usr/bin/env python3
"""bb: ByteBunker in a terminal.

bb is a client of the same local server the app uses. It never calls a
model or writes a log itself: every chat, tool call, agent goal and job it
starts runs on the server, lands in the same sessions, trace log and usage
ledger, and shows up in the app as it happens.

    bb                          chat here (slash commands: /help)
    bb ask "question"           one answer; piped input is attached
    bb ask -p Fast -e off "…"   with a profile, or an effort level
    bb sessions ls              recent sessions, the app's and bb's
    bb agents "goal"            give the agents a goal and follow it
    bb doctor                   what is reachable, and what to do if not
    bb help                     every command

Exit codes: 0 ok, 1 the model or a run failed, 2 usage, 3 cancelled,
4 approval needed but nobody to ask, 5 no server.
"""
import argparse
import base64
import http.client
import json
import mimetypes
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))     # the code, from a checkout or inside the app
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import approvals as approvalsmod  # noqa: E402
import commands as cmdtable  # noqa: E402
import instance as instancemod  # noqa: E402
import paths  # noqa: E402
from version import VERSION  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CANCELLED, EXIT_APPROVAL, EXIT_NO_SERVER = 0, 1, 2, 3, 4, 5
EFFORTS = ("off", "low", "medium", "high", "max")


class BBError(Exception):
    def __init__(self, message, code=EXIT_FAILED):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ terminal --
def _enable_windows_vt():
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        ok = True
        for handle in (-11, -12):            # stdout, stderr
            h = k.GetStdHandle(handle)
            mode = ctypes.c_uint32()
            if not k.GetConsoleMode(h, ctypes.byref(mode)):
                ok = False
                continue
            k.SetConsoleMode(h, mode.value | 0x0004)    # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return ok
    except Exception:   # noqa: BLE001
        return False


class Out:
    """Answers on stdout; everything about the answer (thinking, tools,
    notes, the meta line) on stderr, so a pipe gets only the answer."""

    def __init__(self, color=None):
        tty = sys.stderr.isatty()
        self.color = (tty and not os.environ.get("NO_COLOR") and _enable_windows_vt()) if color is None else color
        self.tty = tty
        self.status_on = False

    def _c(self, code, text):
        return "\033[%sm%s\033[0m" % (code, text) if self.color else text

    def dim(self, t):
        return self._c("2", t)

    def warn(self, t):
        return self._c("33", t)

    def bad(self, t):
        return self._c("31", t)

    def good(self, t):
        return self._c("32", t)

    def bold(self, t):
        return self._c("1", t)

    def status(self, text):
        if not self.tty:
            return
        sys.stderr.write("\r\033[2K" + (self.dim(text[:max(10, _cols() - 1)]) if text else ""))
        sys.stderr.flush()
        self.status_on = bool(text)

    def clear_status(self):
        if self.status_on:
            self.status(None)

    def err(self, text=""):
        self.clear_status()
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    def say(self, text=""):
        self.clear_status()
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def _cols():
    try:
        return os.get_terminal_size(sys.stderr.fileno()).columns
    except OSError:
        return 100


def can_prompt():
    """Is there a person to ask? A terminal on stdin, or a controlling
    terminal behind a pipe. BB_INTERACTIVE=0 says no (scripts, CI)."""
    if os.environ.get("BB_INTERACTIVE") == "0":
        return False
    if sys.stdin.isatty():
        return True
    if os.name != "nt":
        try:
            with open("/dev/tty"):
                return True
        except OSError:
            return False
    return False


def ask_user(out, question):
    """A line from the person at the terminal, or None."""
    out.clear_status()
    try:
        if sys.stdin.isatty():
            return input(question)
        if os.name != "nt":
            with open("/dev/tty", "r+") as tty:
                tty.write(question)
                tty.flush()
                return tty.readline().strip()
    except (OSError, EOFError):
        return None
    return None


def table(rows, headers):
    """Plain columns; the last one takes what is left."""
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for r in rows:
        lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)).rstrip())
    return "\n".join(lines)


def ago(ts_ms):
    if not ts_ms:
        return ""
    d = time.time() - (ts_ms / 1000.0 if ts_ms > 1e11 else ts_ms)
    if d < 60:
        return "just now"
    if d < 3600:
        return "%d min ago" % (d // 60)
    if d < 86400:
        return "%d h ago" % (d // 3600)
    return "%d d ago" % (d // 86400)


# ------------------------------------------------------------------- server --
class Server:
    def __init__(self, info):
        self.info = info
        self.port = int(info["port"])
        self.headers = {"Host": "127.0.0.1:%d" % self.port, "X-BB-Client": "cli"}
        if info.get("token"):
            self.headers["X-BB-Token"] = info["token"]

    def _conn(self, timeout=60):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def request(self, method, path, body=None, timeout=60):
        c = self._conn(timeout)
        h = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
        except OSError as e:
            raise BBError("the server stopped answering: %s" % e, EXIT_NO_SERVER)
        finally:
            c.close()
        try:
            out = json.loads(raw or b"null")
        except ValueError:
            out = {"error": raw.decode("utf-8", "replace")[:300]}
        if r.status >= 400:
            msg = out.get("error") if isinstance(out, dict) else None
            e = BBError(msg or "HTTP %d" % r.status, EXIT_USAGE if r.status in (400, 404, 409) else EXIT_FAILED)
            e.status, e.body = r.status, out
            raise e
        return out

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})

    def stream(self, method, path, body=None):
        """Yield SSE frames ({"id", "event", "data"}) as they come."""
        c = self._conn(timeout=None)
        h = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        if r.status >= 400:
            raw = r.read()
            c.close()
            try:
                out = json.loads(raw)
            except ValueError:
                out = {"error": raw.decode("utf-8", "replace")[:300]}
            e = BBError(out.get("error") or "HTTP %d" % r.status, EXIT_USAGE if r.status in (400, 404) else EXIT_FAILED)
            e.status, e.body = r.status, out
            raise e
        try:
            cur = {}
            while True:
                line = r.fp.readline()
                if not line:
                    return
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if "data" in cur:
                        yield cur
                    cur = {}
                    continue
                if line.startswith(":"):
                    continue
                k, _, v = line.partition(":")
                cur[k] = v[1:] if v.startswith(" ") else v
        finally:
            c.close()


def server_command():
    """How to start a headless server, from wherever bb runs."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--headless"]          # the app's own binary (bb runs as it, --cli)
    return [sys.executable, os.path.join(ROOT, "desktop", "app.py"), "--headless"]


IDLE_EXIT = "1800"      # a server bb starts leaves after 30 idle minutes (Settings can keep it)


def connect(start=True, out=None):
    """The running server for this user's data folder, started if needed."""
    data = paths.data_dir()
    info = instancemod.find(data)
    if info:
        return Server(info)
    if not start:
        raise BBError("no ByteBunker server is running for %s. Start the app, or run: bb serve" % data, EXIT_NO_SERVER)
    if os.environ.get("BYTEBUNKER_DATA") and not os.environ.get("BYTEBUNKER_HOME"):
        cmd = [sys.executable, os.path.join(ROOT, "server.py"), "--port", "0", "--idle-exit", IDLE_EXIT]
    else:
        cmd = server_command() + ["--idle-exit", IDLE_EXIT]
    os.makedirs(data, exist_ok=True)
    log = open(os.path.join(data, "server.log"), "ab")
    kw = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # detached, own group, no window
    else:
        kw["start_new_session"] = True
    if out:
        out.status("starting the ByteBunker server…")
    subprocess.Popen(cmd, **kw)
    deadline = time.time() + 30
    while time.time() < deadline:
        info = instancemod.find(data)
        if info:
            if out:
                out.clear_status()
            return Server(info)
        time.sleep(0.2)
    raise BBError("started a server but it did not answer within 30 s; see %s" % os.path.join(data, "server.log"),
                  EXIT_NO_SERVER)


# -------------------------------------------------------------------- turns --
def new_session_id():
    n, digits, s = int(time.time() * 1000), "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while n:
        n, r = divmod(n, 36)
        s = digits[r] + s
    return "s-" + s + uuid.uuid4().hex[:2]


def attachment_for(srv, sid, path, name=None, data=None):
    """Upload a file to the server's uploads and describe it the way the app does."""
    if data is None:
        with open(path, "rb") as f:
            data = f.read()
    name = name or os.path.basename(path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    info = srv.post("/api/upload", {"session": sid, "name": name, "data": base64.b64encode(data).decode("ascii")})
    att = {"name": name, "size": len(data), "mime": mime, "url": info.get("url"), "path": info.get("path")}
    if mime.startswith("image/"):
        att["kind"] = "image"
    else:
        try:
            text = data.decode("utf-8")
            if len(text) <= 200000 and "\x00" not in text:
                att.update(kind="text", text=text)
            else:
                att["kind"] = "file"
        except UnicodeDecodeError:
            att["kind"] = "file"
    return att


class TurnView:
    """Draws one run's events: the answer to stdout, the rest to stderr.
    With a workspace it also runs the calls the model makes to bb's own
    tools, and answers approvals at the terminal."""

    def __init__(self, out, show_thinking=True, quiet=False, srv=None, rid=None, ws=None, yes=False):
        self.out, self.show_thinking, self.quiet = out, show_thinking, quiet
        self.srv, self.rid, self.ws, self.yes = srv, rid, ws, yes
        self.allowed = set()          # "always" for bb's own tools, for this bb
        self.approval_needed = False
        self.wrote = ""
        self.thinking = False
        self.message = None
        self.final = None
        self.run = None
        self.seq = 0             # the last event drawn: a re-attach resumes after it

    def _end_thinking(self):
        if self.thinking:
            sys.stderr.write("\n")
            self.thinking = False

    def event(self, e):
        t, d = e.get("type"), e.get("data") or {}
        o = self.out
        if t == "status":
            if not self.quiet and not self.thinking:
                o.status(d.get("text") if d else None)
        elif t == "delta":
            o.clear_status()
            if d.get("reasoning") and self.show_thinking and not self.quiet and o.tty:
                if not self.thinking:
                    sys.stderr.write(o.dim("thinking: "))
                    self.thinking = True
                sys.stderr.write(o.dim(d["reasoning"]))
                sys.stderr.flush()
            if d.get("content"):
                self._end_thinking()
                sys.stdout.write(d["content"])
                sys.stdout.flush()
                self.wrote += d["content"]
        elif t == "tool_args":
            if not self.quiet:
                o.status("writing a tool call · %s · %s chars" % (d.get("name") or "", format(d.get("chars") or 0, ",")))
        elif t == "tool_call":
            self._end_thinking()
            self._newline()
            if not self.quiet:
                args = (d.get("args") or "")
                o.err(o.dim("⚙ %s %s" % (d.get("name"), args if len(args) < 120 else args[:117] + "…")))
        elif t == "tool_result":
            if not self.quiet:
                o.err(o.bad("  ↳ error: %s" % (d.get("content") or "")[:200]) if d.get("is_error")
                      else o.dim("  ↳ %s chars" % format(d.get("chars") or 0, ",")))
        elif t == "notice":
            self._end_thinking()
            self._newline()
            o.err(o.warn("note: " + (d.get("text") or "")))
        elif t in ("compress", "compressed"):
            if not self.quiet:
                o.err(o.dim(d.get("text") or "compressed %d messages%s" % (
                    d.get("count") or 0, " into " + d["archive"] if d.get("archive") else "")))
        elif t == "done":
            self._end_thinking()
            self.message = d.get("message") or {}
            self._newline()
            if self.message.get("error"):
                o.err(o.bad(self.message["error"]))
            if not self.quiet and d.get("meta"):
                o.err(o.dim(d["meta"]))
        elif t == "client_call":
            self._client_call(d)
        elif t == "approval":
            self._approval(d)
        elif t in ("approved", "denied"):
            if not self.quiet and d.get("by") not in ("cli",):
                o.err(o.dim("  %s %s (%s)" % (d.get("tool"), "allowed" if t == "approved" else "not allowed",
                                              d.get("by") or "")))
        elif t == "finished":
            self.final = d
            if (d.get("result") or {}).get("approval_needed"):
                self.approval_needed = True
        elif t == "started":
            self.run = d.get("id")

    def _client_call(self, d):
        """The model called one of bb's tools: run it here, send the result back."""
        name, args, cid = d.get("name"), d.get("args") or {}, d.get("call_id")
        o = self.out
        if self.ws is None or self.srv is None:
            return                     # another client's call: not ours to run
        pol = approvalsmod.policy_for(name, self.ws.annotations(name))
        if pol == "ask" and (self.yes or name in self.allowed):
            pol = "allow"
        if pol == "ask":
            if not can_prompt():
                self.approval_needed = True
                o.err(o.bad("%s asks before it runs, and there is nobody to ask: rerun with --yes to allow it" % name))
                self.srv.post("/api/runs/%s/tool-results" % self.rid,
                              {"call_id": cid, "content": "not allowed: nobody could approve it", "is_error": True})
                self.srv.post("/api/runs/%s/cancel" % self.rid)
                return
            ans = (ask_user(o, o.warn("allow %s %s? [y]es, [n]o, [a]lways: " % (name, _brief(args)))) or "").lower()
            if ans.startswith("a"):
                self.allowed.add(name)
            pol = "allow" if ans[:1] in ("y", "a") else "deny"
        if pol == "allow":
            text, is_err = self.ws.call(name, args)
        else:
            text, is_err = "the user did not allow this call", True
        self.srv.post("/api/runs/%s/tool-results" % self.rid, {"call_id": cid, "content": text, "is_error": is_err})

    def _approval(self, d):
        """A server tool asks before it runs: answer from here (the app can too; the first answer wins)."""
        o = self.out
        if self.srv is None:
            return
        if self.yes:
            decision = "allow"
        elif not can_prompt():
            return                     # the app may answer; after 10 minutes it is a no
        else:
            ans = (ask_user(o, o.warn("allow %s %s? [y]es, [n]o, [a]lways: " % (d.get("tool"), _brief(d.get("args"))))) or "").lower()
            decision = {"y": "allow", "a": "always"}.get(ans[:1], "deny")
        try:
            self.srv.post("/api/approvals", {"id": d.get("id"), "decision": decision})
        except BBError:
            pass                       # answered elsewhere first

    def _newline(self):
        if self.wrote and not self.wrote.endswith("\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.wrote += "\n"


def _drain(srv, rid, view):
    for f in srv.stream("GET", "/api/runs/%s/events?after=%d" % (rid, view.seq)):
        try:
            e = json.loads(f["data"])
        except ValueError:
            continue
        view.seq = max(view.seq, e.get("seq") or 0)
        view.event(e)
        if view.final is not None:
            return


def _brief(args):
    t = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
    return t if len(t) <= 160 else t[:157] + "…"


def follow(srv, rid, view, out):
    """Follow a run to its end. Ctrl-C cancels it on the server (a second
    Ctrl-C leaves at once). Returns the exit code."""
    cancelled = False
    try:
        _drain(srv, rid, view)
    except KeyboardInterrupt:
        cancelled = True
        out.err(out.warn("\nstopping… (Ctrl-C again to leave now)"))
        try:
            srv.post("/api/runs/%s/cancel" % rid)
            _drain(srv, rid, view)
        except KeyboardInterrupt:
            return EXIT_CANCELLED
        except BBError:
            pass
    state = (view.final or {}).get("state")
    if getattr(view, "approval_needed", False):
        return EXIT_APPROVAL
    if cancelled or state == "cancelled":
        return EXIT_CANCELLED
    if state == "error" or (view.message or {}).get("error"):
        return EXIT_FAILED
    return EXIT_OK


def run_turn(srv, out, sid, req, json_out=False, quiet=False, show_thinking=True, ws=None, yes=False):
    if ws is not None:
        req["client_tools"] = ws.defs()
    req["interactive"] = can_prompt()
    if yes:
        req["yes"] = True
    try:
        r = srv.post("/api/sessions/%s/turns" % sid, req)
    except BBError as e:
        if getattr(e, "status", 0) == 409:
            raise BBError("a turn is already running in this session (run %s); bb runs watch %s, or bb runs stop %s"
                          % (e.body.get("run"), e.body.get("run"), e.body.get("run")))
        raise
    view = TurnView(out, show_thinking=show_thinking and not json_out, quiet=quiet or json_out,
                    srv=srv, rid=r["run"], ws=ws, yes=yes)
    if json_out:
        saved = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            code = follow(srv, r["run"], view, out)
        finally:
            sys.stdout.close()
            sys.stdout = saved
        print(json.dumps({"session": r["session"], "run": r["run"], "message": view.message,
                          "state": (view.final or {}).get("state")}, ensure_ascii=False))
        return code, r["session"]
    return follow(srv, r["run"], view, out), r["session"]


# ------------------------------------------------------------------ commands --
def cmd_ask(a, out):
    srv = connect(out=out)
    text = " ".join(a.text).strip()
    sid = a.session or new_session_id()
    atts = []
    if not sys.stdin.isatty():
        piped = sys.stdin.buffer.read()
        if piped:
            if text:
                atts.append(attachment_for(srv, sid, None, name=a.name or "stdin.txt", data=piped))
            else:
                text = piped.decode("utf-8", "replace")
    for f in a.attach or []:
        atts.append(attachment_for(srv, sid, f))
    if not text and not atts:
        raise BBError("nothing to ask: bb ask \"your question\" (or pipe something in)", EXIT_USAGE)
    req = {"text": text, "attachments": atts, "cwd": os.getcwd()}
    for k in ("model", "effort", "profile", "system"):
        if getattr(a, k, None):
            req[k] = getattr(a, k)
    if a.no_tools:
        req["tools"] = False
    if a.max_tokens:
        req["params"] = {"max_tokens": a.max_tokens}
    ws = None if a.no_tools or a.no_workspace else workspace_here()
    code, _ = run_turn(srv, out, sid, req, json_out=a.json, quiet=a.quiet, show_thinking=not a.no_thinking,
                       ws=ws, yes=a.yes)
    return code


def workspace_here():
    import workspace
    return workspace.Workspace(os.getcwd())


def _models(srv):
    return [m["id"] for m in srv.get("/api/models").get("data") or []]


def cmd_chat(a, out):
    """The REPL: one session, slash commands from the command table."""
    srv = connect(out=out)
    st = {"sid": a.session or None, "model": a.model, "effort": a.effort, "profile": a.profile, "tools": True,
          "skills": [], "atts": []}
    ws = None if a.no_workspace else workspace_here()
    if st["sid"]:
        sess = srv.get("/api/sessions?id=" + urllib.parse.quote(st["sid"]))
        st["model"] = st["model"] or sess.get("model")
        out.err(out.dim("continuing %s · %s · %d messages" % (st["sid"], sess.get("title") or "", len(sess.get("messages") or []))))
    try:
        import readline  # noqa: F401  (history and line editing where Python has it)
    except ImportError:
        pass
    out.err(out.dim("ByteBunker %s · %s · /help for commands, Ctrl-D to leave" % (
        VERSION, st["model"] or "default model")))
    while True:
        try:
            line = input(out.bold("› ") if out.color else "> ")
        except EOFError:
            out.err()
            return EXIT_OK
        except KeyboardInterrupt:
            out.err()
            continue
        line = line.strip()
        if line == '"""':
            buf = []
            while True:
                try:
                    l2 = input("")
                except EOFError:
                    break
                if l2.strip() == '"""':
                    break
                buf.append(l2)
            line = "\n".join(buf)
        if not line:
            continue
        if line.startswith("/") and not line.startswith("//"):
            word, _, rest = line.partition(" ")
            res = slash(word, rest.strip(), st, srv, out)
            if res == "quit":
                return EXIT_OK
            continue
        if line.startswith("//"):
            line = line[1:]
        if not st["sid"]:
            st["sid"] = new_session_id()
        req = {"text": line, "attachments": st["atts"], "cwd": os.getcwd(), "skills": st["skills"]}
        for k in ("model", "effort", "profile"):
            if st[k]:
                req[k] = st[k]
        if not st["tools"]:
            req["tools"] = False
        st["atts"] = []
        try:
            run_turn(srv, out, st["sid"], req, ws=ws if st["tools"] else None, yes=a.yes)
        except BBError as e:
            out.err(out.bad(str(e)))


def slash(word, rest, st, srv, out):
    known = {c["slash"] for c in cmdtable.slash_commands()}
    if word not in known:
        out.err(out.warn("unknown command %s; /help lists them" % word))
        return None
    try:
        if word == "/quit":
            return "quit"
        if word == "/help":
            out.err(table([[c["slash"], c["args"], c["help"]] for c in cmdtable.slash_commands()],
                          ["command", "arguments", ""]))
        elif word == "/model":
            models = _models(srv)
            if rest:
                if rest not in models:
                    out.err(out.warn("no gateway lists %s; /model shows what they do" % rest))
                else:
                    st["model"] = rest
                    out.err(out.dim("model: " + rest))
            else:
                for m in models:
                    out.err(("* " if m == st["model"] else "  ") + m)
        elif word == "/effort":
            if not rest:
                out.err("effort: %s (one of %s, or default)" % (st["effort"] or "default", ", ".join(EFFORTS)))
            elif rest == "default":
                st["effort"] = None
            elif rest in EFFORTS:
                st["effort"] = rest
            else:
                out.err(out.warn("effort is one of %s, or default" % ", ".join(EFFORTS)))
        elif word == "/profile":
            profs = srv.get("/api/profiles")
            if rest:
                if rest not in profs["profiles"]:
                    out.err(out.warn("no profile %r; there are: %s" % (rest, ", ".join(profs["profiles"]))))
                else:
                    st["profile"] = rest
            else:
                cur = st["profile"] or profs.get("default")
                for name, p in profs["profiles"].items():
                    out.err(("* " if name == cur else "  ") + name + out.dim("  " + (p.get("description") or "")))
        elif word == "/tools":
            if rest in ("on", "off"):
                st["tools"] = rest == "on"
            d = srv.get("/api/tools")
            out.err("tools %s · %d available" % ("on" if st["tools"] else "off", len(d.get("tools") or [])))
            for t in d.get("tools") or []:
                out.err("  " + t["name"] + out.dim("  " + (t.get("description") or "")[:80]))
        elif word == "/skills":
            if rest:
                st["skills"] = rest.replace(",", " ").split()
                out.err(out.dim("skills: " + ", ".join(st["skills"])))
            else:
                for s in srv.get("/api/skills").get("skills") or []:
                    out.err(("* " if s["name"] in st["skills"] else "  ") + s["name"] + out.dim("  " + (s.get("description") or "")[:80]))
        elif word == "/attach":
            if not st["sid"]:
                st["sid"] = new_session_id()
            for f in rest.split():
                st["atts"].append(attachment_for(srv, st["sid"], os.path.expanduser(f)))
                out.err(out.dim("attached " + f))
        elif word == "/compress":
            if not st["sid"]:
                out.err(out.warn("nothing to compress yet"))
                return None
            r = srv.post("/api/sessions/%s/compress" % st["sid"], {"model": st["model"]} if st["model"] else {})
            follow(srv, r["run"], TurnView(out), out)
        elif word == "/new":
            st["sid"] = None
            out.err(out.dim("new session"))
        elif word == "/open":
            sess = srv.get("/api/sessions?id=" + urllib.parse.quote(rest))
            st["sid"] = rest
            st["model"] = st["model"] or sess.get("model")
            out.err(out.dim("continuing %s · %s" % (rest, sess.get("title") or "")))
        elif word == "/sessions":
            print_sessions(srv, out, 15)
        elif word == "/models":
            for m in _models(srv):
                out.err("  " + m)
        elif word == "/usage":
            print_usage(srv, out)
        elif word == "/workflows":
            cmd_workflows(None, out)
    except BBError as e:
        out.err(out.bad(str(e)))
    return None


def print_sessions(srv, out, n):
    rows = [[s["id"], (s.get("title") or "")[:48], s.get("model") or "", s.get("source") or "app",
             ago(s.get("updated"))] for s in srv.get("/api/sessions")[:n]]
    out.say(table(rows, ["session", "title", "model", "from", "active"]) if rows else "no sessions yet")


def print_usage(srv, out):
    u = srv.get("/api/usage")
    out.say("tokens generated, 14 days: %s" % format(u.get("total_out") or 0, ","))
    if u.get("median_tok_s"):
        out.say("median throughput: %.1f tok/s over %s requests" % (u["median_tok_s"], u.get("requests")))
    for m, n in sorted((u.get("by_model") or {}).items(), key=lambda x: -x[1]):
        out.say("  %-40s %s" % (m, format(n, ",")))


def cmd_sessions(a, out):
    srv = connect(out=out)
    if a.action == "ls":
        print_sessions(srv, out, a.n)
        return EXIT_OK
    if not a.id:
        raise BBError("bb sessions show SESSION", EXIT_USAGE)
    sess = srv.get("/api/sessions?id=" + urllib.parse.quote(a.id))
    out.say(out.bold(sess.get("title") or a.id))
    for m in sess.get("messages") or []:
        who = "you" if m.get("role") == "user" else (m.get("model") or "model")
        out.say(out.dim("— " + who))
        out.say(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content")))
        if m.get("error"):
            out.say(out.bad(m["error"]))
    return EXIT_OK


def cmd_list(a, out):
    srv = connect(out=out)
    what = a.cmd
    if what == "models":
        d = srv.get("/api/models")
        rows = [[m["id"], m.get("gateway") or "", (m.get("caps") or {}).get("ctx") or ""] for m in d.get("data") or []]
        out.say(table(rows, ["model", "gateway", "window"]) if rows else "no models: add a model server on the Gateways screen")
    elif what == "gateways":
        d = srv.get("/api/gateways")
        out.say(table([[g["name"], g["url"], "ok" if g.get("ok") else (g.get("error") or "down")[:60],
                        len(g.get("models") or [])] for g in d.get("gateways") or []], ["gateway", "url", "state", "models"]))
    elif what == "monitors":
        d = srv.get("/api/monitors")
        out.say(table([[m.get("name"), m.get("url"), "on" if m.get("enabled", True) else "off"]
                       for m in d.get("monitors") or []], ["monitor", "url", ""]))
    elif what == "cluster":
        d = srv.get("/api/cluster")
        rows = []
        for n in d.get("nodes") or []:
            engines = ", ".join("%s:%s" % (e.get("kind"), e.get("port")) for e in n.get("engines") or [])
            rows.append([n.get("name"), n.get("role") or "", "ok" if n.get("ok", True) else "down", engines])
        out.say(table(rows, ["node", "role", "state", "engines"]) if rows else "no nodes: add a rack monitor")
    elif what == "usage":
        print_usage(srv, out)
    elif what == "skills":
        out.say(table([[s["name"], s.get("source") or "", (s.get("description") or "")[:70]]
                       for s in srv.get("/api/skills").get("skills") or []], ["skill", "from", ""]))
    elif what == "mcp":
        d = srv.get("/api/tools")
        out.say(table([[n, s.get("state"), s.get("tools") or 0, (s.get("error") or "")[:60]]
                       for n, s in (d.get("servers") or {}).items()], ["server", "state", "tools", ""]))
    return EXIT_OK


def cmd_runs(a, out):
    srv = connect(out=out)
    if a.action == "ls":
        q = "?limit=30" + ("&kind=" + urllib.parse.quote(a.kind) if a.kind else "")
        rows = [[r["id"], r["kind"], r["state"], r.get("source") or "", (r.get("title") or "")[:50], ago(r.get("started"))]
                for r in srv.get("/api/runs" + q)["runs"]]
        out.say(table(rows, ["run", "kind", "state", "from", "title", "started"]) if rows else "no runs yet")
        return EXIT_OK
    if not a.id:
        raise BBError("bb runs %s RUN" % a.action, EXIT_USAGE)
    if a.action == "stop":
        srv.post("/api/runs/%s/cancel" % a.id)
        out.err("stopping " + a.id)
        return EXIT_OK
    return follow(srv, a.id, GenericView(out), out)


class GenericView(TurnView):
    """Any run: chat turns as a turn, other runs' output lines as they come."""

    def event(self, e):
        if e.get("topic") == "output" and e.get("type") == "out":
            d = e.get("data") or {}
            if d.get("line") is not None:
                self.out.say(d["line"])
            elif d.get("error"):
                self.out.err(self.out.bad("error: " + d["error"]))
            elif d.get("phase") == "done":
                self.out.err(self.out.dim("finished: %s" % (d.get("killed") or "exit %s" % d.get("exit"))))
            elif d.get("job"):
                self.out.err(self.out.dim("job filed: %s (%s)" % (d["job"].get("name"), d["job"].get("id"))))
            return
        super().event(e)


def cmd_agents(a, out):
    srv = connect(out=out)
    if a.goal and a.goal[0] in ("ls", "stop") and len(a.goal) <= 2:
        if a.goal[0] == "ls":
            a2 = argparse.Namespace(action="ls", kind="agents", id=None)
            return cmd_runs(a2, out)
        rid = a.goal[1] if len(a.goal) > 1 else None
        if not rid:
            live = [r for r in srv.get("/api/runs?kind=agents&limit=10")["runs"] if r["state"] in ("running", "queued")]
            if not live:
                out.err("no goal is running")
                return EXIT_OK
            rid = live[0]["id"]
        srv.post("/api/runs/%s/cancel" % rid)
        out.err("stopping " + rid)
        return EXIT_OK
    goal = " ".join(a.goal).strip()
    if not goal:
        raise BBError('bb agents "goal" (or: bb agents ls, bb agents stop)', EXIT_USAGE)
    rid = None
    try:
        frames = srv.stream("POST", "/api/agents", {"goal": goal})
        for f in frames:
            d = json.loads(f["data"])
            if d.get("run"):
                rid = d["run"]
                if a.detach:
                    out.say(rid)
                    out.err(out.dim("running on the server; follow it with: bb runs watch " + rid))
                    return EXIT_OK
                break
    except BBError as e:
        if getattr(e, "status", 0) == 409 and (e.body or {}).get("run"):
            raise BBError("a goal is already running (%s): bb runs watch %s, or bb agents stop" % (e.body["run"], e.body["run"]))
        raise
    if not rid:
        raise BBError("the server did not start the goal")
    return follow(srv, rid, GenericView(out), out)


def cmd_jobs(a, out):
    srv = connect(out=out)
    if a.action == "ls":
        d = srv.get("/api/jobs")
        running = d.get("running") or {}
        rows = [[j["id"], j["name"][:36], j["kind"], j.get("schedule_text") or "",
                 "running" if j["id"] in running else ("on" if j.get("enabled", True) else "paused"),
                 ("ok" if (j.get("last") or {}).get("ok") else "failed") if j.get("last") else "never"]
                for j in d.get("jobs") or []]
        out.say(table(rows, ["job", "name", "kind", "schedule", "state", "last"]) if rows else "no jobs yet")
        return EXIT_OK
    if not a.id:
        raise BBError("bb jobs run JOB", EXIT_USAGE)
    r = srv.post("/api/jobs", {"action": "run_now", "id": a.id})
    return follow(srv, r["run"], GenericView(out), out)


def cmd_run(a, out):
    """bb run WORKFLOW -p key=value: a chat workflow runs as a turn with bb's
    tools in the workflow's folder (or here); an agents workflow as a goal."""
    srv = connect(out=out)
    params = {}
    for kv in a.param or []:
        k, sep, v = kv.partition("=")
        if not sep:
            raise BBError("-p takes KEY=VALUE, not %r" % kv, EXIT_USAGE)
        params[k.strip()] = v
    wfs = srv.get("/api/workflows").get("workflows") or {}
    wf = wfs.get(a.workflow)
    if wf is None:
        raise BBError("no workflow named %r%s" % (a.workflow, " (there are: %s)" % ", ".join(sorted(wfs)) if wfs else
                                                   "; make one in the app (Workflows)"), EXIT_USAGE)
    body = {"params": params}
    view = GenericView(out)
    if wf.get("kind") != "agents":
        folder = os.path.expanduser(wf.get("folder") or "") or os.getcwd()
        if not os.path.isdir(folder):
            raise BBError("the workflow's folder %s does not exist here" % folder, EXIT_USAGE)
        import workspace
        ws = workspace.Workspace(folder)
        body.update(client_tools=ws.defs(), interactive=can_prompt(), cwd=folder)
        if a.yes:
            body["yes"] = True
        if a.session:
            body["session"] = a.session
        view = GenericView(out, srv=srv, ws=ws, yes=a.yes)
    try:
        r = srv.post("/api/workflows/%s/run" % urllib.parse.quote(a.workflow), body)
    except BBError as e:
        if getattr(e, "status", 0) == 409 and (e.body or {}).get("run"):
            raise BBError("%s: bb runs watch %s" % (e, e.body["run"]))
        raise
    view.rid = r["run"]
    if a.detach:
        out.say(r["run"])
        return EXIT_OK
    return follow(srv, r["run"], view, out)


def cmd_workflows(a, out):
    srv = connect(out=out)
    wfs = srv.get("/api/workflows").get("workflows") or {}
    if not wfs:
        out.say("no workflows yet: make one in the app (Workflows), then: bb run NAME -p key=value")
        return EXIT_OK
    rows = [[n, w.get("kind"), w.get("profile") or "", " ".join("%s=" % p["name"] for p in w.get("params") or []),
             (w.get("description") or "")[:50]] for n, w in sorted(wfs.items())]
    out.say(table(rows, ["workflow", "kind", "profile", "takes", ""]))
    return EXIT_OK


def cmd_profiles(a, out):
    srv = connect(out=out)
    d = srv.get("/api/profiles")
    rows = [[("* " if n == d.get("default") else "  ") + n, p.get("effort") or "", (p.get("params") or {}).get("max_tokens") or "",
             p.get("description") or ""] for n, p in d["profiles"].items()]
    out.say(table(rows, ["profile", "effort", "max tokens", ""]))
    return EXIT_OK


def cmd_doctor(a, out):
    """What is reachable, and the one thing to do when something is not."""
    data = paths.data_dir()
    info = instancemod.find(data)
    ok = True

    def line(good, what, fix=""):
        out.say(("%s %s" % (out.good("✓") if good else out.bad("✗"), what)) + (out.dim("  → " + fix) if fix and not good else ""))

    line(bool(info), "server for %s%s" % (data, " (pid %s, port %s)" % (info["pid"], info["port"]) if info else ""),
         "start the app, or: bb serve")
    if not info:
        return EXIT_NO_SERVER
    srv = Server(info)
    hello = srv.get("/api/hello")
    line(True, "ByteBunker %s" % hello.get("version"))
    gws = srv.get("/api/gateways").get("gateways") or []
    up = [g for g in gws if g.get("ok")]
    line(bool(up), "gateways: %d of %d answer" % (len(up), len(gws)),
         "add a model server on the Gateways screen" if not gws else "check the ones that do not: bb gateways")
    ok = ok and bool(up)
    models = srv.get("/api/models").get("data") or []
    line(bool(models), "models: %d" % len(models), "a gateway that answers but lists nothing serves no model yet")
    mons = srv.get("/api/monitors").get("monitors") or []
    line(True, "rack monitors: %d" % len(mons))
    cfg = srv.get("/api/config")
    for w in cfg.get("warnings") or []:
        line(False, w)
    return EXIT_OK if ok else EXIT_FAILED


def cmd_serve(a, out):
    cmd = server_command()
    if os.name == "nt":
        return subprocess.call(cmd)
    os.execv(cmd[0], cmd)


def cmd_help(a, out):
    out.say(__doc__.split("\n\n")[0])
    out.say()
    out.say(table([["bb " + c["cli"], c["args"], c["help"]] for c in cmdtable.cli_commands()], ["command", "arguments", ""]))
    out.say()
    out.say("In a chat: " + " ".join(c["slash"] for c in cmdtable.slash_commands()))
    return EXIT_OK


# ---------------------------------------------------------------------- main --
def parser():
    p = argparse.ArgumentParser(prog="bb", description="ByteBunker in a terminal.", add_help=True)
    p.add_argument("--version", action="version", version="bb (ByteBunker) " + VERSION)
    sub = p.add_subparsers(dest="cmd")

    def turn_opts(sp):
        sp.add_argument("-m", "--model")
        sp.add_argument("-e", "--effort", choices=EFFORTS)
        sp.add_argument("-p", "--profile")
        sp.add_argument("-s", "--session", help="continue this session")
        sp.add_argument("--system", help="system prompt for this turn")
    a = sub.add_parser("ask", help="one question")
    a.add_argument("text", nargs="*")
    turn_opts(a)
    a.add_argument("-a", "--attach", action="append", metavar="FILE")
    a.add_argument("--name", help="a name for piped input when it is attached")
    a.add_argument("--json", action="store_true", help="print the answer as JSON")
    a.add_argument("-q", "--quiet", action="store_true", help="only the answer")
    a.add_argument("--no-thinking", action="store_true")
    a.add_argument("--no-tools", action="store_true")
    a.add_argument("--max-tokens", type=int)
    a.add_argument("--yes", action="store_true", help="allow tool calls that would ask")
    a.add_argument("--no-workspace", action="store_true", help="no tools in this folder (server tools only)")
    c = sub.add_parser("chat", help="chat in this terminal")
    c.add_argument("session", nargs="?")
    turn_opts(c)
    c.add_argument("--yes", action="store_true", help="allow tool calls that would ask")
    c.add_argument("--no-workspace", action="store_true", help="no tools in this folder (server tools only)")
    s = sub.add_parser("sessions")
    s.add_argument("action", choices=("ls", "show"), nargs="?", default="ls")
    s.add_argument("id", nargs="?")
    s.add_argument("-n", type=int, default=20)
    for name in ("models", "gateways", "monitors", "cluster", "usage", "skills", "mcp"):
        sub.add_parser(name)
    r = sub.add_parser("runs")
    r.add_argument("action", choices=("ls", "watch", "stop"), nargs="?", default="ls")
    r.add_argument("id", nargs="?")
    r.add_argument("-k", "--kind")
    ag = sub.add_parser("agents")
    ag.add_argument("goal", nargs="*")
    ag.add_argument("--detach", action="store_true", help="start it and return; it runs on the server")
    j = sub.add_parser("jobs")
    j.add_argument("action", choices=("ls", "run"), nargs="?", default="ls")
    j.add_argument("id", nargs="?")
    w = sub.add_parser("run", help="run a workflow")
    w.add_argument("workflow")
    w.add_argument("-p", "--param", action="append", metavar="KEY=VALUE")
    w.add_argument("-s", "--session", help="run it in this session")
    w.add_argument("--yes", action="store_true", help="allow tool calls that would ask")
    w.add_argument("--detach", action="store_true", help="start it and return its run id")
    sub.add_parser("workflows")
    sub.add_parser("profiles")
    sub.add_parser("doctor")
    sub.add_parser("serve")
    sub.add_parser("help")
    return p


HANDLERS = {"ask": cmd_ask, "chat": cmd_chat, "sessions": cmd_sessions, "runs": cmd_runs, "agents": cmd_agents,
            "jobs": cmd_jobs, "doctor": cmd_doctor, "serve": cmd_serve, "help": cmd_help, "run": cmd_run,
            "workflows": cmd_workflows, "profiles": cmd_profiles,
            "models": cmd_list, "gateways": cmd_list, "monitors": cmd_list, "cluster": cmd_list,
            "usage": cmd_list, "skills": cmd_list, "mcp": cmd_list}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = Out()
    p = parser()
    if not argv:
        argv = ["chat"]
    elif argv[0] not in HANDLERS and not argv[0].startswith("-"):
        argv = ["ask"] + argv               # bb "question" is bb ask "question"
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    if os.name != "nt":
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # bb ask … | head stays quiet
    for stream in (sys.stdout, sys.stderr):
        # a pipe on Windows is cp1252, and a C locale is ASCII: write UTF-8,
        # and never crash on a character the other side cannot show
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    try:
        return HANDLERS[a.cmd](a, out) or EXIT_OK
    except BBError as e:
        out.err(out.bad("bb: " + str(e)))
        return e.code
    except KeyboardInterrupt:
        out.err()
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
