#!/usr/bin/env python3
"""Built-in MCP server: the console's scheduled jobs as tools for the chat.

Runs as a subprocess of the console (stdio JSON-RPC, like mcp_terminal.py)
and talks back to the console's own HTTP API on loopback. The model in the
Playground can then turn "every morning, summarise the trace log" into a job
on the Jobs screen, list what is scheduled, run one now, or delete one.
Stdlib only.
"""

import json
import os
import sys
import urllib.request

BASE = os.environ.get("BB_CONSOLE_URL") or "http://127.0.0.1:%s" % os.environ.get("BB_CONSOLE_PORT", "8765")

TOOLS = [
    {"name": "job_create",
     "description": "File a recurring job with the console. schedule_cron is a 5-field cron ('0 9 * * mon-fri'); "
                    "or give every_min for an interval. kind: 'chat' runs the prompt with the console's tools; "
                    "'agent' hands the prompt to the agent harness as a goal.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "prompt": {"type": "string", "description": "what each run should do, self-contained"},
         "schedule_cron": {"type": "string"}, "every_min": {"type": "integer"},
         "kind": {"type": "string", "enum": ["chat", "agent"]}, "model": {"type": "string"},
         "tools": {"type": "boolean"}, "skills": {"type": "array", "items": {"type": "string"}},
         "reason": {"type": "string"}}, "required": ["name", "prompt"]}},
    {"name": "job_list", "description": "List the scheduled jobs with their schedule, next run and last result.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "job_runs", "description": "Recent runs of one job, with their outputs.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["id"]}},
    {"name": "job_run_now", "description": "Run a job immediately (it also keeps its schedule).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "job_toggle", "description": "Pause or resume a job.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "job_delete", "description": "Delete a job and its history. Ask the human first unless they asked for it.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
]


def api(path, payload=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json", "Host": "127.0.0.1"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def call(name, args):
    if name == "job_create":
        sched = ({"kind": "interval", "every_min": int(args["every_min"])} if args.get("every_min")
                 else {"kind": "cron", "cron": str(args.get("schedule_cron") or "")})
        job = {"name": args.get("name"), "prompt": args.get("prompt"), "kind": args.get("kind") or "chat",
               "schedule": sched, "model": args.get("model") or "", "tools": args.get("tools", True),
               "skills": args.get("skills") or [], "reason": args.get("reason") or "", "created_by": "playground"}
        d = api("/api/jobs", {"action": "save", "job": job})
        if d.get("error"):
            return d["error"], True
        j = d["job"]
        return "filed job %s (%s): %s. The human sees it on the Jobs screen." % (j["id"], j["name"], describe(j)), False
    if name == "job_list":
        d = api("/api/jobs")
        rows = ["%s  %s  %s  next=%s  last=%s" % (j["id"], j["name"], j.get("schedule_text"), j.get("next_run"),
                 (j.get("last") or {}).get("ok")) for j in d.get("jobs", [])]
        return "\n".join(rows) or "no jobs scheduled", False
    if name == "job_runs":
        d = api("/api/jobs/runs?id=" + str(args.get("id")))
        out = []
        for r in d.get("runs", [])[: int(args.get("limit") or 5)]:
            out.append("%s %s ok=%s %sms\n%s" % (r.get("ts"), r.get("trigger"), r.get("ok"), r.get("ms"), (r.get("output") or r.get("error") or "")[:3000]))
        return "\n\n".join(out) or "no runs yet", False
    if name in ("job_run_now", "job_toggle", "job_delete"):
        d = api("/api/jobs", {"action": name.replace("job_", ""), "id": str(args.get("id"))})
        return json.dumps(d)[:2000], bool(d.get("error"))
    return "unknown tool " + name, True


def describe(j):
    s = j.get("schedule") or {}
    return "every %s min" % s.get("every_min") if s.get("kind") == "interval" else "cron " + str(s.get("cron"))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id"); method = msg.get("method")
        if method == "initialize":
            out = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "bytebunker-jobs", "version": "1"}}
        elif method == "tools/list":
            out = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text, err = call(p.get("name"), p.get("arguments") or {})
            except Exception as e:   # noqa: BLE001
                text, err = "jobs tool failed: %s" % e, True
            out = {"content": [{"type": "text", "text": text}], "isError": err}
        elif mid is None:
            continue
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}}) + "\n"); sys.stdout.flush()
            continue
        if mid is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": out}) + "\n"); sys.stdout.flush()


if __name__ == "__main__":
    main()
