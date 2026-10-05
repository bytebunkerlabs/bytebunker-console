"""A stdio MCP server for tests that answers OUT OF ORDER.

Tools: echo(text) [read-only], slow(seconds), crash(), ping_client().
Replies to tools/call come from worker threads after a random delay, so a
client that assumes in-order replies gets them crossed. Cancellation
notifications are appended to $FAKE_MCP_LOG when set.
"""
import json
import os
import random
import sys
import threading
import time

out_lock = threading.Lock()
pending_pings = {}


def send(msg):
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


TOOLS = [
    {"name": "echo", "description": "echo text", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "slow", "description": "sleep", "annotations": {"destructiveHint": False},
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "crash", "description": "exit the server", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ping_client", "description": "ping the client", "inputSchema": {"type": "object", "properties": {}}},
]


def handle_call(rid, params):
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "echo":
        time.sleep(random.random() * 0.03)
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "echo:" + str(args.get("text"))}]}})
    elif name == "slow":
        time.sleep(float(args.get("seconds") or 1))
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "slept"}]}})
    elif name == "crash":
        os._exit(3)
    elif name == "ping_client":
        ev = threading.Event()
        pid = "srv-%d" % rid
        pending_pings[pid] = ev
        send({"jsonrpc": "2.0", "id": pid, "method": "ping"})
        ok = ev.wait(5)
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "pong-ok" if ok else "pong-missing"}]}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such tool"}})


def main():
    print("this line is not JSON and must be ignored", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method is None:                       # a reply to our ping
            ev = pending_pings.pop(msg.get("id"), None)
            if ev:
                ev.set()
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS}})
        elif method == "tools/call":
            threading.Thread(target=handle_call, args=(msg["id"], msg.get("params") or {}), daemon=True).start()
        elif method == "notifications/cancelled":
            log = os.environ.get("FAKE_MCP_LOG")
            if log:
                with open(log, "a") as f:
                    f.write(json.dumps(msg.get("params")) + "\n")


if __name__ == "__main__":
    main()
