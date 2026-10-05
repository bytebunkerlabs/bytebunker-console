#!/usr/bin/env python3
"""Smoke-test a running ByteBunker (packaged or not) over HTTP.

    python desktop/ci_smoke.py http://127.0.0.1:18765

Checks the things a broken bundle gets wrong: static files present, the
desktop first-run config, bundled skills, the built-in jobs MCP server
started through the app itself (--mcp) and calling back to it, and the
Cluster screen's path: a (fake) rack monitor added with its token, read
back through the server, feeding the engine-busy hint."""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18765").rstrip("/")


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read()


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


deadline = time.time() + 90
while True:
    try:
        get("/api/config", 3)
        break
    except Exception as e:   # noqa: BLE001
        if time.time() > deadline:
            sys.exit("server never answered: %s" % e)
        time.sleep(1)

checks = []
cfg = json.loads(get("/api/config")[1])
checks.append(("desktop config", cfg.get("desktop") is True and cfg.get("gateways") == [] and cfg.get("monitors") == 0))
checks.append(("index.html", get("/")[0] == 200))
checks.append(("console.js", get("/console.js")[0] == 200 and len(get("/console.js")[1]) > 50000))
skills = json.loads(get("/api/skills")[1]).get("skills", [])
checks.append(("bundled skills", any(s.get("source") == "built-in" for s in skills)))
servers = json.loads(get("/api/tools")[1]).get("servers", {})
checks.append(("jobs MCP server via --mcp", (servers.get("jobs") or {}).get("state") == "ready"))
call = post("/api/tool-call", {"name": "jobs__job_list", "arguments": {}})
checks.append(("jobs tool calls back", call.get("isError") is False))
hello = json.loads(get("/api/hello")[1])
checks.append(("hello", hello.get("service") == "bytebunker" and bool(hello.get("pid"))))
post("/api/sessions", {"id": "s-smoke", "title": "smoke", "updated": int(time.time() * 1000),
                       "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]})
rows = json.loads(get("/api/sessions")[1])
one = json.loads(get("/api/sessions?id=s-smoke")[1])
checks.append(("sessions: list + one", any(r.get("id") == "s-smoke" and "messages" not in r for r in rows)
               and len(one.get("messages") or []) == 2))
with urllib.request.urlopen(BASE + "/api/events?topics=sessions&after=0", timeout=10) as r:
    frame = b""
    while not frame.endswith(b"\n\n") or b"data:" not in frame:
        frame += r.readline()
checks.append(("event stream", b'"s-smoke"' in frame))
gws = post("/api/gateways", {"action": "add", "url": "127.0.0.1:9", "name": "unreachable"})
checks.append(("gateway add + probe", gws.get("ok") is True))
post("/api/gateways", {"action": "remove", "name": "unreachable"})


class FakeMonitor(BaseHTTPRequestHandler):
    """Answers like rackmon.py: /v1/hello open, /v1/cluster behind a token."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/hello":
            body = {"service": "rack-monitor", "version": "1.0.0", "schema": 1, "name": "ci-node",
                    "role": "head", "cluster": "ci", "peers": 0, "auth": "bearer"}
        elif self.path.startswith("/v1/cluster") and self.headers.get("Authorization") == "Bearer ci-token":
            body = {"service": "rack-monitor", "version": "1.0.0", "schema": 1, "cluster": "ci", "head": "ci-node",
                    "nodes": [{"schema": 1, "name": "ci-node", "role": "head", "ok": True, "gpus": [],
                               "engines": [{"kind": "vllm", "port": 8888, "ok": True, "gen_tps": 12.5,
                                            "prompt_tps": 0, "running": 1}]}]}
        else:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


mon = ThreadingHTTPServer(("127.0.0.1", 0), FakeMonitor)
threading.Thread(target=mon.serve_forever, daemon=True).start()
mport = mon.server_address[1]
try:
    post("/api/monitors", {"action": "add", "url": "127.0.0.1:%d" % mport, "token": "wrong"})
    refused = False
except urllib.error.HTTPError as e:
    refused = e.code == 422
checks.append(("monitor: wrong token refused", refused))
added = post("/api/monitors", {"action": "add", "url": "http://rack:ci-token@127.0.0.1:%d" % mport})
cluster = json.loads(get("/api/cluster?history=10")[1])
checks.append(("monitor: add + cluster", added.get("ok") is True and [n.get("name") for n in cluster.get("nodes", [])] == ["ci-node"]))
engine = json.loads(get("/api/engine")[1])
checks.append(("monitor: engine stats", engine.get("ok") is True and engine.get("rate") == 12.5))
post("/api/monitors", {"action": "remove", "name": added.get("name", "")})
mon.shutdown()

for name, ok in checks:
    print("%-28s %s" % (name, "ok" if ok else "FAIL"))
if not all(ok for _, ok in checks):
    print("servers:", json.dumps(servers))
    sys.exit(1)
print("all checks passed")
