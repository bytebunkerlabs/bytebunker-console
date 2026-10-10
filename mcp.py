"""Minimal MCP host: stdio JSON-RPC clients, tool discovery, tool calls.

Stdlib only, deliberately small — MCP over stdio is newline-delimited JSON-RPC
2.0, so a full SDK is not needed to list and call tools. What this supports:
initialize -> tools/list -> tools/call, per server, with a per-call timeout.

Servers are declared in config.json:

  "mcp_servers": {
    "fs": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/mo/rack"],
      "env": {},
      "enabled": true
    }
  }

Tools are exposed to the model as "<server>__<tool>" so two servers can both
have a "read_file" without colliding.

Trust model: an MCP server is a local process with whatever access its args
grant it — the filesystem server can write anywhere under the roots you pass.
Scope those roots deliberately; this host does not sandbox them.
"""
import collections
import json
import subprocess
import threading
import time

SEP = "__"          # server/tool name separator in the flattened tool name
BUILTIN_SCRIPTS = ("mcp_terminal.py", "mcp_jobs.py")


def extra_path():
    """Install roots a GUI-launched process does not see on its PATH."""
    import os
    import sys
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        return [os.path.join(os.environ.get("APPDATA", ""), "npm"), os.path.join(home, ".local", "bin"),
                r"C:\Program Files\nodejs"]
    return ["/opt/homebrew/bin", "/usr/local/bin", os.path.join(home, ".local", "bin")]
START_TIMEOUT = 25  # server boot + initialize
CALL_TIMEOUT = 120  # one tools/call


class _Waiter:
    __slots__ = ("event", "msg")

    def __init__(self):
        self.event = threading.Event()
        self.msg = None


class MCPServer:
    """One stdio MCP server subprocess, spoken to over JSON-RPC 2.0.

    One reader thread owns the server's stdout and hands each reply to the
    request that is waiting for its id, so any number of calls can be in
    flight at once (the console, a job and the CLI share a server). When the
    server exits, every pending call fails at once instead of timing out."""

    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.proc = None
        self.tools = []
        self.error = None
        self._id = 0
        self._wlock = threading.Lock()       # writes to stdin, id allocation
        self._plock = threading.Lock()       # the pending table
        self._pending = {}
        self._reader = None
        self._dead = None                    # why the server stopped answering
        self._stderr = collections.deque(maxlen=20)   # its last words, for the reason it stopped
        self._err_reader = None
        self.created = []                    # folders made for it before it started

    # ---- transport ----
    def _write(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _send(self, method, params=None, want_reply=True):
        """Write one message. Returns (id, waiter) for requests; the waiter is
        registered before the write so even an instant reply finds it."""
        with self._wlock:
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            rid, w = None, None
            if want_reply:
                self._id += 1
                rid = self._id
                msg["id"] = rid
                w = _Waiter()
                with self._plock:
                    if self._dead:
                        raise RuntimeError(self._dead)
                    self._pending[rid] = w
            try:
                self._write(msg)
            except (OSError, ValueError) as e:
                if rid is not None:
                    with self._plock:
                        self._pending.pop(rid, None)
                raise RuntimeError("server is not accepting input: %s" % e)
            return rid, w

    def _read_loop(self):
        """The only reader of stdout: route replies, answer pings, skip noise."""
        reason = "server closed its stdout"
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue      # some servers log to stdout; ignore non-JSON
                if not isinstance(msg, dict):
                    continue
                if "method" in msg:
                    if "id" in msg:   # a request from the server: we implement ping only
                        reply = {"jsonrpc": "2.0", "id": msg["id"]}
                        if msg["method"] == "ping":
                            reply["result"] = {}
                        else:
                            reply["error"] = {"code": -32601, "message": "not supported by this client"}
                        try:
                            with self._wlock:
                                self._write(reply)
                        except (OSError, ValueError):
                            pass
                    continue      # notifications: nothing to do
                with self._plock:
                    w = self._pending.pop(msg.get("id"), None)
                if w is not None:
                    w.msg = msg
                    w.event.set()
        except (OSError, ValueError) as e:
            reason = "server stdout failed: %s" % e
        reason = self._why(reason)
        with self._plock:
            self._dead = reason
            waiting = list(self._pending.values())
            self._pending.clear()
        for w in waiting:
            w.msg = {"error": reason}
            w.event.set()

    def _err_loop(self):
        """Drain stderr (a full pipe would stall the server) and keep its tail."""
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if line.strip():
                    self._stderr.append(line)
        except (OSError, ValueError):
            pass

    def _why(self, reason):
        """What the server said before it went away, when it said anything."""
        try:
            self.proc.wait(timeout=1)
        except Exception:        # still running: its stdout broke on its own
            pass
        if self._err_reader is not None:
            self._err_reader.join(timeout=1)
        said = [l.strip() for l in list(self._stderr)[-2:]]
        if not said:
            return reason
        code = self.proc.poll() if self.proc else None
        return ("exited%s: " % ("" if code is None else " (%d)" % code)) + " · ".join(said)

    def _rpc(self, method, params=None, timeout=CALL_TIMEOUT):
        rid, w = self._send(method, params)
        if not w.event.wait(timeout):
            with self._plock:
                self._pending.pop(rid, None)
            try:                          # tell the server to stop working on it
                self._send("notifications/cancelled", {"requestId": rid, "reason": "timeout"}, want_reply=False)
            except RuntimeError:
                pass
            raise TimeoutError("no reply to request %s in %ss" % (rid, timeout))
        msg = w.msg or {}
        if "error" in msg:
            raise RuntimeError(str(msg["error"])[:400])
        return msg.get("result", {})

    @property
    def alive(self):
        return bool(self.proc) and self.proc.poll() is None and not self._dead

    # ---- lifecycle ----
    def start(self):
        import os
        import shutil
        import sys
        env = dict(os.environ)
        # launchd and Finder hand us a minimal PATH, so `npx`/`uvx` are
        # invisible even when installed. Search the usual install roots.
        env["PATH"] = env.get("PATH", "") + os.pathsep + os.pathsep.join(extra_path())
        env.update(self.spec.get("env") or {})
        # a folder it is pointed at that is not there yet is made, not refused
        import mcp_catalog
        for folder in mcp_catalog.folders_for(self.spec):
            if not os.path.isdir(folder):
                try:
                    os.makedirs(folder, exist_ok=True)
                except OSError as e:
                    raise RuntimeError("cannot create %s: %s" % (folder, e.strerror or e))
                self.created.append(folder)
        # `~` in an argument means the user's home, as it would in a shell —
        # Popen passes it literally, and the filesystem server then roots
        # itself at a directory called "~" that does not exist.
        args = [os.path.expanduser(a) if a.startswith("~") else a
                for a in self.spec.get("args", [])]
        here = os.path.dirname(os.path.abspath(__file__))
        if args and os.path.basename(args[0]) in BUILTIN_SCRIPTS and \
                os.path.basename(self.spec["command"]).lower().startswith("python"):
            # Servers shipped with the console run on the console's own
            # interpreter: no reliance on a `python3` being on the PATH (on
            # Windows that name can be a Store stub). In a packaged app the
            # interpreter is the app itself, entered through --mcp.
            script = os.path.basename(args[0])
            if getattr(sys, "frozen", False):
                exe = sys.executable
                if sys.platform == "win32":
                    # the windowed exe may have no usable stdio; its console twin does
                    cli = os.path.join(os.path.dirname(sys.executable), "ByteBunker-cli.exe")
                    if os.path.exists(cli):
                        exe = cli
                cmd = [exe, "--mcp", script] + args[1:]
            else:
                cmd = [sys.executable, os.path.join(here, script)] + args[1:]
        else:
            exe = shutil.which(self.spec["command"], path=env["PATH"]) or self.spec["command"]
            cmd = [exe] + args
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env, cwd=here,
            encoding="utf-8", errors="replace",
        )
        self._err_reader = threading.Thread(target=self._err_loop, name="mcp-err-" + self.name, daemon=True)
        self._err_reader.start()
        self._reader = threading.Thread(target=self._read_loop, name="mcp-" + self.name, daemon=True)
        self._reader.start()
        self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "bytebunker-console", "version": "1"},
        }, timeout=START_TIMEOUT)
        self._send("notifications/initialized", {}, want_reply=False)
        self.tools = (self._rpc("tools/list", {}, timeout=START_TIMEOUT) or {}).get("tools", [])

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        for stream in ("stdin", "stdout", "stderr"):
            try:
                getattr(self.proc, stream).close()
            except Exception:
                pass

    def call(self, tool, args, timeout=CALL_TIMEOUT):
        return self._rpc("tools/call", {"name": tool, "arguments": args or {}}, timeout=timeout)


class MCPHost:
    """Owns every configured server; flattens their tools into one namespace.

    Servers start in parallel. sync() applies a new configuration by touching
    only what changed: an edited, added or re-enabled server (re)starts, a
    removed or disabled one stops, and every other server keeps running with
    its calls in flight."""

    def __init__(self, servers_cfg=None):
        self.servers = {}
        self.status = {}
        self.specs = {}
        self._lock = threading.Lock()
        if servers_cfg is not None:
            self.sync(servers_cfg)

    def sync_in_background(self, servers_cfg, restart=()):
        """Start or update servers without holding up the caller; status
        shows "starting" until each one is ready."""
        for name, spec in (servers_cfg or {}).items():
            if spec.get("enabled") is not False and name not in self.servers:
                self.status.setdefault(name, {"state": "starting", "tools": 0})
        t = threading.Thread(target=self.sync, args=(servers_cfg, restart), name="mcp-sync", daemon=True)
        t.start()
        return t

    @staticmethod
    def _key(spec):
        return json.dumps({k: spec.get(k) for k in ("command", "args", "env")}, sort_keys=True)

    def _start_one(self, name, spec):
        s = MCPServer(name, spec)
        try:
            s.start()
            st = {"state": "ready", "tools": len(s.tools)}
            if s.created:
                st["created"] = s.created
            return name, s, st
        except Exception as e:
            s.stop()
            return name, None, {"state": "error", "tools": 0, "error": str(e)[:300]}

    def sync(self, servers_cfg, restart=()):
        """Make the running servers match servers_cfg. Names in `restart` are
        restarted even when their configuration did not change."""
        servers_cfg = servers_cfg or {}
        with self._lock:
            to_start = []
            for name in list(self.servers):
                spec = servers_cfg.get(name)
                if spec is None or spec.get("enabled") is False or name in restart or \
                        self._key(spec) != self._key(self.specs.get(name) or {}):
                    self.servers.pop(name).stop()
                    self.specs.pop(name, None)
            for name in list(self.status):
                if name not in servers_cfg:
                    self.status.pop(name, None)
            for name, spec in servers_cfg.items():
                if spec.get("enabled") is False:
                    self.status[name] = {"state": "disabled", "tools": 0}
                    continue
                if name not in self.servers:
                    to_start.append((name, spec))
                    self.status[name] = {"state": "starting", "tools": 0}
            threads, results = [], []
            for name, spec in to_start:
                t = threading.Thread(target=lambda n=name, sp=spec: results.append(self._start_one(n, sp)), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            for name, server, st in results:
                if server is not None:
                    self.servers[name] = server
                    self.specs[name] = dict(servers_cfg[name])
                self.status[name] = st
        return self.status

    def restart(self, name, servers_cfg):
        return self.sync(servers_cfg, restart=(name,))

    def tool_annotations(self, flat_name):
        """MCP tool annotations (readOnlyHint, destructiveHint, …) for approvals."""
        if SEP not in flat_name:
            return {}
        srv, tool = flat_name.split(SEP, 1)
        s = self.servers.get(srv)
        for t in (s.tools if s else []):
            if t.get("name") == tool:
                return t.get("annotations") or {}
        return {}

    def openai_tools(self, allow=None):
        """Tool definitions in OpenAI function-calling shape."""
        out = []
        for name, s in list(self.servers.items()):   # a sync may be adding servers right now
            for t in s.tools:
                flat = name + SEP + t["name"]
                if allow and flat not in allow:
                    continue
                out.append({
                    "type": "function",
                    "function": {
                        "name": flat,
                        "description": (t.get("description") or "")[:1024],
                        "parameters": t.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    },
                })
        return out

    def call(self, flat_name, args):
        """Run one tool. Returns (text, is_error) — never raises, because the
        model needs a result string either way to continue the loop."""
        if SEP not in flat_name:
            return "unknown tool: %s" % flat_name, True
        srv, tool = flat_name.split(SEP, 1)
        s = self.servers.get(srv)
        if not s:
            return "no such MCP server: %s" % srv, True
        if not s.alive:
            self.status[srv] = {"state": "error", "tools": 0, "error": s._dead or "server exited"}
            return "MCP server %s is not running (%s); restart it on the MCP screen" % (srv, s._dead or "exited"), True
        try:
            res = s.call(tool, args)
        except Exception as e:
            return "tool error: %s" % str(e)[:400], True
        # MCP content blocks -> plain text for the model
        parts = []
        for c in (res.get("content") or []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                r = c.get("resource") or {}
                parts.append(r.get("text") or ("[resource %s]" % r.get("uri", "")))
            else:
                parts.append("[%s content]" % c.get("type"))
        text = "\n".join(p for p in parts if p) or json.dumps(res)[:2000]
        return text, bool(res.get("isError"))

    def stop_all(self):
        with self._lock:
            for s in self.servers.values():
                s.stop()
            self.servers.clear()
            self.specs.clear()
