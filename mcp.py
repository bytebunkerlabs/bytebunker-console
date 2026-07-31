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
import json
import subprocess
import threading
import time

SEP = "__"          # server/tool name separator in the flattened tool name
START_TIMEOUT = 25  # server boot + initialize
CALL_TIMEOUT = 120  # one tools/call


class MCPServer:
    """One stdio MCP server subprocess, spoken to over JSON-RPC 2.0."""

    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.proc = None
        self.tools = []
        self.error = None
        self._id = 0
        self._lock = threading.Lock()

    # ---- transport ----
    def _send(self, method, params=None, want_reply=True):
        with self._lock:
            self._id += 1
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            if want_reply:
                msg["id"] = self._id
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            return self._id if want_reply else None

    def _read_reply(self, want_id, timeout):
        """Read lines until the reply with want_id arrives. Notifications and
        server->client requests we do not implement are skipped."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed its stdout")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # some servers log to stdout; ignore non-JSON
            if msg.get("id") == want_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:400])
                return msg.get("result", {})
        raise TimeoutError("no reply to request %s in %ss" % (want_id, timeout))

    def _rpc(self, method, params=None, timeout=CALL_TIMEOUT):
        rid = self._send(method, params)
        return self._read_reply(rid, timeout)

    # ---- lifecycle ----
    def start(self):
        import os
        import shutil
        env = dict(os.environ)
        # launchd hands us a minimal PATH, so `npx`/`uvx` are invisible even
        # when installed. Search the usual install roots before giving up.
        env["PATH"] = env.get("PATH", "") + ":" + ":".join([
            "/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"),
        ])
        env.update(self.spec.get("env") or {})
        exe = shutil.which(self.spec["command"], path=env["PATH"]) or self.spec["command"]
        cmd = [exe] + list(self.spec.get("args", []))
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
        )
        self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "bytebunker-console", "version": "1"},
        }, timeout=START_TIMEOUT)
        self._send("notifications/initialized", {}, want_reply=False)
        self.tools = (self._rpc("tools/list", {}, timeout=START_TIMEOUT) or {}).get("tools", [])

    def stop(self):
        try:
            if self.proc:
                self.proc.terminate()
        except Exception:
            pass

    def call(self, tool, args):
        return self._rpc("tools/call", {"name": tool, "arguments": args or {}})


class MCPHost:
    """Owns every configured server; flattens their tools into one namespace."""

    def __init__(self, servers_cfg):
        self.servers = {}
        self.status = {}
        for name, spec in (servers_cfg or {}).items():
            if spec.get("enabled") is False:
                self.status[name] = {"state": "disabled", "tools": 0}
                continue
            s = MCPServer(name, spec)
            try:
                s.start()
                self.servers[name] = s
                self.status[name] = {"state": "ready", "tools": len(s.tools)}
            except Exception as e:
                s.stop()
                self.status[name] = {"state": "error", "tools": 0,
                                     "error": str(e)[:300]}

    def openai_tools(self, allow=None):
        """Tool definitions in OpenAI function-calling shape."""
        out = []
        for name, s in self.servers.items():
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
        for s in self.servers.values():
            s.stop()
