#!/usr/bin/env python3
"""Terminal access as an MCP server: one tool, `run`, that executes a shell
command on the console host and returns what it printed.

Stdlib only, stdio JSON-RPC 2.0, the same dialect mcp.py speaks — so it is
added like any other server (panel → Tools · MCP → "terminal" preset), or in
config.json:

  "mcp_servers": {
    "terminal": {"command": "python3", "args": ["mcp_terminal.py", "~/rack"]}
  }

The optional argument is the starting directory. The working directory then
follows the model's own `cd`s from call to call, like a real terminal.

Trust model, plainly: this is a shell running as the console's user, with
that user's files, keys and network. There is no sandbox here — the timeout
and the output cap are about keeping the model loop alive, not about safety.
Enable it because you want the model to have a terminal; disable it (one
click in the panel) when you don't.
"""
import json
import os
import shutil
import subprocess
import sys

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
OUTPUT_CAP = 30000          # chars handed back per call; the middle is elided
CWD_MARK = "\n<<__BB_CWD__>>"   # trailer the wrapper prints so `cd` persists

STATE = {"cwd": os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~")}
if not os.path.isdir(STATE["cwd"]):
    STATE["cwd"] = os.path.expanduser("~")

SHELL = shutil.which("zsh") or shutil.which("bash") or "/bin/sh"

TOOLS = [{
    "name": "run",
    "description": (
        "Run a shell command on the console host (macOS, %s) and return its exit "
        "code, stdout and stderr. The working directory persists between calls, so "
        "`cd` works. Non-interactive: nothing can answer a prompt, so pass -y / "
        "--no-input style flags. Default timeout 60 s (timeout_s up to 600). Output "
        "beyond %d characters is elided in the middle." % (os.path.basename(SHELL), OUTPUT_CAP)
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line, as you would type it."},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT,
                          "description": "Seconds to wait before the command is killed."},
        },
        "required": ["command"],
    },
}]


def elide(s, cap=OUTPUT_CAP):
    if len(s) <= cap:
        return s
    half = cap // 2
    return s[:half] + "\n\n[... %d characters elided ...]\n\n" % (len(s) - cap) + s[-half:]


def run(args):
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return "run: command is required", True
    try:
        timeout = int(args.get("timeout_s") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(MAX_TIMEOUT, timeout))
    # The wrapper runs the command, then prints the shell's final directory
    # after a NUL marker, so a `cd` in this call is where the next one starts.
    script = "%s\n__rc=$?\nprintf '\\n<<__BB_CWD__>>%%s' \"$PWD\"\nexit $__rc" % cmd
    try:
        p = subprocess.run(
            [SHELL, "-c", script], cwd=STATE["cwd"], capture_output=True,
            text=True, errors="replace", timeout=timeout, start_new_session=True,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        err = (e.stderr or b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        text = "killed after %ds (timeout_s)\n" % timeout
        if out.strip():
            text += "--- stdout ---\n" + elide(out)
        if err.strip():
            text += "\n--- stderr ---\n" + elide(err)
        return text, True
    except OSError as e:
        return "run: could not start the shell: %s" % e, True
    stdout = p.stdout or ""
    i = stdout.rfind(CWD_MARK)
    if i >= 0:
        new_cwd = stdout[i + len(CWD_MARK):].strip()
        stdout = stdout[:i]
        if new_cwd and os.path.isdir(new_cwd):
            STATE["cwd"] = new_cwd
    text = "exit %d  (cwd: %s)\n" % (p.returncode, STATE["cwd"])
    if stdout.strip():
        text += "--- stdout ---\n" + elide(stdout)
    if (p.stderr or "").strip():
        text += ("\n" if stdout.strip() else "") + "--- stderr ---\n" + elide(p.stderr)
    return text, p.returncode != 0


def reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if rid is None:
            continue   # notifications need no answer
        if method == "initialize":
            reply(rid, {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bytebunker-terminal", "version": "1"},
            })
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            if params.get("name") != "run":
                reply(rid, {"content": [{"type": "text", "text": "unknown tool: %s" % params.get("name")}],
                            "isError": True})
                continue
            text, is_err = run(params.get("arguments") or {})
            reply(rid, {"content": [{"type": "text", "text": text}], "isError": is_err})
        elif method == "ping":
            reply(rid, {})
        else:
            reply(rid, error={"code": -32601, "message": "method not found: %s" % method})


if __name__ == "__main__":
    main()
