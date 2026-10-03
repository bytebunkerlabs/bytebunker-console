#!/usr/bin/env python3
"""Smoke-test a running ByteBunker (packaged or not) over HTTP.

    python desktop/ci_smoke.py http://127.0.0.1:18765

Checks the things a broken bundle gets wrong: static files present, the
desktop first-run config, bundled skills, the built-in jobs MCP server
started through the app itself (--mcp) and calling back to it."""
import json
import sys
import time
import urllib.request

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
checks.append(("desktop config", cfg.get("desktop") is True and cfg.get("gateways") == []))
checks.append(("index.html", get("/")[0] == 200))
checks.append(("console.js", get("/console.js")[0] == 200 and len(get("/console.js")[1]) > 50000))
skills = json.loads(get("/api/skills")[1]).get("skills", [])
checks.append(("bundled skills", any(s.get("source") == "built-in" for s in skills)))
servers = json.loads(get("/api/tools")[1]).get("servers", {})
checks.append(("jobs MCP server via --mcp", (servers.get("jobs") or {}).get("state") == "ready"))
call = post("/api/tool-call", {"name": "jobs__job_list", "arguments": {}})
checks.append(("jobs tool calls back", call.get("isError") is False))
gws = post("/api/gateways", {"action": "add", "url": "127.0.0.1:9", "name": "unreachable"})
checks.append(("gateway add + probe", gws.get("ok") is True))
post("/api/gateways", {"action": "remove", "name": "unreachable"})

for name, ok in checks:
    print("%-28s %s" % (name, "ok" if ok else "FAIL"))
if not all(ok for _, ok in checks):
    print("servers:", json.dumps(servers))
    sys.exit(1)
print("all checks passed")
