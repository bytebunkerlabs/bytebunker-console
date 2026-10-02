#!/usr/bin/env python3
"""ByteBunker Console — one-file server. Stdlib only, Python 3.9+.

Successor to chatserve: serves the console UI and provides the small API the
UI needs. Nothing leaves the rack — the only outbound calls are to the
upstream OpenAI-compatible endpoint (your gateway or vLLM) and, optionally,
your own Prometheus.

  python3 server.py                 # reads config.json next to this file
  python3 server.py --port 8765

API:
  GET  /                     the console
  GET  /api/config           UI-facing config (node names, identity, rates)
  GET  /api/models           proxied upstream /v1/models
  POST /api/chat             proxied streaming /v1/chat/completions (SSE)
  GET  /api/telemetry        Prometheus-backed node cards (or {"nodes": []})
  GET  /api/sessions         list sessions  |  POST save  |  DELETE ?id=
  POST /api/archive          file away compressed turns  |  GET /api/archive/<name>
  POST /api/rate             thumbs up/down on a turn, into the trace log
  GET  /api/traces           trace log stats  |  GET /api/export?...  training JSONL
  GET  /api/skills           skill catalog  |  GET /api/skills/<name>  full body
  GET  /api/plugins          installed plugins  |  POST enable/disable/rescan
  GET  /api/agents           harness status + recent runs  |  POST run a goal (SSE)
  POST /api/usage-event      client-reported completion stats -> usage.jsonl
  GET  /api/usage            14-day aggregates for the Usage screen
  POST /api/video            multipart passthrough -> H3 /v1/videos (job id)
  GET  /api/video[/id[/content]]   job list / status / the finished MP4
  DELETE /api/video/<id>     drop a job and its stored output
"""
import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from traces import TraceLog, StreamCapture, export_lines
import skills as skillmod
import agents as agentmod
import recipes as recipemod
import mcp_catalog as catmod
import jobs as jobmod

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)
# Every model request, tool run, rating and archive, append-only, forever:
# data/traces/<day>.jsonl (gzipped after the day ends). See traces.py.
TRACE = TraceLog(DATA)

DEFAULT_CONFIG = {
    "bind": "127.0.0.1",
    "port": 8765,
    "upstream_url": "http://127.0.0.1:8000/v1",
    "upstream_key": "bb-local",
    "prometheus_url": "",
    "sparkdash_url": "",        # e.g. http://127.0.0.1:15555 (ssh tunnel to the head Spark's sparkDash)
    "sparkdash_open_url": "",   # where a browser can open sparkDash itself (tailnet URL), for the Cluster link
    "telemetry_source": "",     # "prometheus" | "sparkdash"; blank = sparkdash if only that is set
    "h3_url": "",
    "netcheck_ssh": "",
    "nodes": [
        {"name": "spark-1", "instance": "spark-1"},
        {"name": "spark-2", "instance": "spark-2"},
    ],
    "identity": {"user": "mo@bunker", "host": "local"},
    "frontier_rates_per_mtok": {"input": 3.0, "output": 15.0},
    # Safe by default: the harness launcher is inert until you enable it and
    # point dir/ssh at a dedicated worker host (see agents.py).
    "agents": {"enabled": False, "ssh": "", "dir": "", "python": "uv run",
               "script": "scripts/run_master.py", "run_timeout_s": 10800},
}


def agent_run_timeout(cfg):
    """Wall-clock cap on one delegated goal. A multi-step goal on a single
    Spark is a 1-2 hour affair; the old fixed 3600 s killed the master while
    it was writing its synthesis after every agent had delivered."""
    try:
        return max(300, min(86400, int((cfg.get("agents") or {}).get("run_timeout_s") or 10800)))
    except (TypeError, ValueError):
        return 10800


# What each model can actually do. An OpenAI-compatible gateway normalises the
# envelope, not the behaviour: two models behind the same LiteLLM will disagree
# about whether tools are accepted, whether thinking is on by default, which
# effort values are legal, and whether prior reasoning must be resent or
# stripped. Guessing produces a 400 that reads like a crash, so the client asks
# instead. Keys are matched as substrings of the model id, longest match
# wins (so "deepseek-v4-vision" beats "deepseek-v4"); override or extend via
# "model_capabilities" in config.json.
#
#   tools           send the tools array at all
#   effort          legal reasoning-effort values; [] hides the dial entirely
#   ctk             merged into chat_template_kwargs on every request
#   strip_reasoning drop prior <think> from resent history
#   ctx             context window the engine is SERVING (--max-model-len),
#                   not what the weights could do: it drives the meter and the
#                   max_tokens clamp. If the engine's window turns out smaller,
#                   the client learns the real figure from its overflow error
#                   and uses that for the rest of the session.
CAPS_FALLBACK = {"tools": True, "effort": [], "ctk": {}, "strip_reasoning": True,
                 "ctx": 131072, "vision": False}
DEFAULT_CAPS = {
    # vLLM defaults DeepSeek-V4 thinking OFF (DeepSeek's own API defaults it
    # ON at high) — so it must be asked for explicitly. The tokenizer wrapper
    # coerces any unrecognized effort string ('low', 'medium', ...) to 'high',
    # and only 'max' changes the prompt — so 'max' is the only value worth
    # offering. (The encoder one layer down does assert on 'low', but the
    # wrapper is its only caller, so the assert is unreachable via requests.)
    # strip_reasoning is False because DeepSeek 400s if reasoning_content is
    # missing from a tool exchange.
    "deepseek-v4": {"tools": True, "effort": ["max"], "strip_reasoning": False,
                    "ctk": {"thinking": True, "reasoning_effort": "max"},
                    "ctx": 1048576},   # native YaRN 1M; recipe serves full window
    # The vision recipe is served at 262k on both Sparks (--max-model-len,
    # measured 2026-09-24). Same caps otherwise. Re-serve it wider and this
    # line (or a model_capabilities override) must follow.
    "deepseek-v4-vision": {"tools": True, "effort": ["max"], "strip_reasoning": False,
                           "ctk": {"thinking": True, "reasoning_effort": "max"},
                           "ctx": 262144, "vision": True},
    # Inkling's renderer accepts none/minimal/low/medium/high/xhigh/max —
    # 'minimal', not 'min': an unknown name resolves to None and the template
    # falls back to its 0.9 default, i.e. the dial silently stops working.
    "inkling": {"tools": True,
                "effort": ["none", "minimal", "low", "medium", "high", "xhigh"],
                "strip_reasoning": True, "ctk": {}, "ctx": 262144},
}


def caps_for(model_id):
    table = dict(DEFAULT_CAPS)
    table.update(CFG.get("model_capabilities") or {})
    mid = (model_id or "").lower()
    hits = [k for k in table if k.lower() in mid]
    if hits:
        merged = dict(CAPS_FALLBACK)
        merged.update(table[max(hits, key=len)])
        return merged
    return dict(CAPS_FALLBACK)


def upstream_message(detail):
    """The sentence inside an upstream error body.

    A proxy chain wraps errors in envelopes — litellm puts vLLM's message
    inside {"error": {"message": ...}} and the console used to wrap that
    again — so the browser ended up rendering 400 characters of escaped JSON.
    Peel the envelopes; hand back the original text if they don't parse."""
    obj = detail
    for _ in range(3):
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except ValueError:
                break
        if isinstance(obj, dict):
            inner = obj.get("error", obj.get("message", obj.get("detail")))
            if isinstance(inner, dict):
                inner = inner.get("message")
            if not isinstance(inner, str):
                break
            obj = inner
            continue
        break
    return obj if isinstance(obj, str) else detail


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()
_LOCK = threading.Lock()

# Skills live in the repo's skills/, in any dir listed in config's skills_dirs
# (point one at the harness's skills/ to share them), and inside enabled
# plugins. Plugins live in the repo's plugins/ and in config's plugins_dirs.
BUILTIN_SKILLS = os.path.join(ROOT, "skills")
BUILTIN_PLUGINS = os.path.join(ROOT, "plugins")


def _plugin_state():
    return CFG.setdefault("plugins", {})


def discover_plugins():
    dirs = [BUILTIN_PLUGINS] + list(CFG.get("plugins_dirs") or [])
    found, warnings = skillmod.discover_plugins(dirs)
    return found, warnings


def enabled_plugins():
    found, _ = discover_plugins()
    st = _plugin_state()
    return {n: p for n, p in found.items() if st.get(n, {}).get("enabled")}


def skill_roots():
    """Ranked (dir, source) list; earlier wins duplicate names. Enabled
    plugins first, then configured dirs, then the repo's own skills/."""
    roots = []
    for name, p in sorted(enabled_plugins().items()):
        if p.skills_dir:
            roots.append((p.skills_dir, "plugin:" + name))
    for d in (CFG.get("skills_dirs") or []):
        roots.append((d, d))
    roots.append((BUILTIN_SKILLS, "built-in"))
    return roots


def skill_catalog():
    return skillmod.load_catalog(skill_roots())


def user_skills_dir():
    """Where a skill created in the UI is written: the first configured
    skills dir (on a deployed console that is the harness mirror), else the
    repo's own skills/."""
    for d in (CFG.get("skills_dirs") or []):
        d = os.path.expanduser(d)
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    os.makedirs(BUILTIN_SKILLS, exist_ok=True)
    return BUILTIN_SKILLS


def plugins_install_dir():
    for d in (CFG.get("plugins_dirs") or []):
        d = os.path.expanduser(d)
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    os.makedirs(BUILTIN_PLUGINS, exist_ok=True)
    return BUILTIN_PLUGINS


# ------------------------------------------------------------------ rack --
# The Sparks' serving layer is `rack` (dgx-spark-serve): recipes/*.env decide
# solo vs tensor-parallel and the gateway name; `rack up` launches and
# registers with litellm. The console drives it over ssh and shows its
# output — it never composes a docker command for a Spark itself.
def rack_cfg():
    r = CFG.get("rack") or {}
    return {"ssh": (r.get("ssh") or "").strip(), "dir": r.get("dir") or "~/dgx/dgx-spark-serve"}


def rack_cmd(args):
    r = rack_cfg()
    inner = "export PATH=$HOME/.local/bin:$PATH; cd %s && ./rack %s" % (
        agentmod._rq(r["dir"]), " ".join(shlex_quote(a) for a in args))
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r["ssh"], inner]


def shlex_quote(a):
    import shlex
    return shlex.quote(str(a))


_rack_cache = {"at": 0, "val": None}


def rack_overview(max_age=15):
    """recipes + status, cached briefly: `rack status` sshes to both nodes."""
    import subprocess
    r = rack_cfg()
    if not r["ssh"]:
        return {"ok": False, "enabled": False, "error": "set rack.ssh (and rack.dir) in config.json"}
    now = time.time()
    if _rack_cache["val"] is not None and now - _rack_cache["at"] < max_age:
        return _rack_cache["val"]
    val = {"ok": True, "enabled": True, "host": r["ssh"], "dir": r["dir"]}
    try:
        out = subprocess.run(rack_cmd(["recipes"]), capture_output=True, text=True, timeout=25)
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        val["recipes_raw"] = ansi.sub("", out.stdout + out.stderr).strip()
        recipes = []
        for ln in ansi.sub("", out.stdout).splitlines():
            tok = ln.strip().split()
            # "  dsv4-vision-ab   TP=2   orcarouter/DeepSeek-V4-Flash-Vision-Uncensored"
            if len(tok) >= 2 and re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", tok[0]) and tok[0] != "TEMPLATE":
                recipes.append({"name": tok[0].replace(".env", ""), "mode": tok[1],
                                "model": tok[2] if len(tok) > 2 else ""})
        val["recipes"] = recipes
    except Exception as e:   # noqa: BLE001
        val.update(ok=False, error=str(e)[:160])
        _rack_cache.update(at=now, val=val)
        return val
    try:
        out = subprocess.run(rack_cmd(["status"]), capture_output=True, text=True, timeout=45)
        val["status_raw"] = ansi.sub("", out.stdout + out.stderr).strip()[-4000:]
        m = re.search(r"serving:\s*(\S+)", val["status_raw"])
        val["serving"] = m.group(1) if m else ""
    except Exception as e:   # noqa: BLE001
        val["status_raw"] = "rack status failed: %s" % str(e)[:120]
    _rack_cache.update(at=now, val=val)
    return val


# ---------------------------------------------------------------- uploads --
# Files attached in the composer. Images travel to the model as base64 in the
# request; everything is also saved here so the tools (filesystem, terminal)
# can read it and the Files tab can open it. Default under data/; point
# `uploads_dir` at the filesystem server's root to make attachments reachable
# by the model's tools.
UPLOAD_MAX = 25 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def uploads_root():
    root = os.path.expanduser(CFG.get("uploads_dir") or os.path.join(DATA, "uploads"))
    os.makedirs(root, exist_ok=True)
    return root


def save_upload(session, name, data):
    sess = _SAFE_NAME.sub("_", str(session or "nosession"))[:48] or "nosession"
    base = _SAFE_NAME.sub("_", os.path.basename(str(name or "file")))[:120] or "file"
    d = os.path.join(uploads_root(), sess)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, base)
    stem, ext = os.path.splitext(base)
    n = 1
    while os.path.exists(path):          # never overwrite an earlier attachment
        n += 1
        path = os.path.join(d, "%s-%d%s" % (stem, n, ext))
    with open(path, "wb") as f:
        f.write(data)
    return {"name": os.path.basename(path), "path": path, "size": len(data),
            "url": "/api/uploads/%s/%s" % (sess, os.path.basename(path))}


def trace_safe(payload):
    """The request as logged: image data URLs replaced by their size, so a
    screenshot does not become 300 KB of base64 in every trace line."""
    try:
        msgs = payload.get("messages")
        if not isinstance(msgs, list):
            return payload
        out = dict(payload)
        new_msgs = []
        for m in msgs:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, list):
                parts = []
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = str((part.get("image_url") or {}).get("url") or "")
                        if url.startswith("data:"):
                            head = url.split(",", 1)[0]
                            part = {"type": "image_url", "image_url": {"url": "%s,<%d bytes>" % (head, len(url))}}
                    parts.append(part)
                m = dict(m, content=parts)
            new_msgs.append(m)
        out["messages"] = new_msgs
        return out
    except Exception:   # noqa: BLE001 - logging must never break the chat
        return payload


# ------------------------------------------------------------------ jobs --
# Scheduled work: the console wakes up and has the model do a task, with the
# same tools, skills and model caps a chat turn would use, or hands a goal
# to the agent harness. The scheduler thread starts with the server.
JOBS = jobmod.JobStore(DATA)
SCHED = None


def upstream_json(path, payload, timeout=900):
    req = upstream_request(path, payload, "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def run_job(job):
    """One run. chat: a bounded tool loop against the gateway, like the
    Playground does but server-side. agent: a harness goal. Returns
    {output, hops, tokens}."""
    if job.get("kind") == "agent":
        st = agentmod.status(CFG)
        if not st.get("enabled"):
            raise RuntimeError("agents are disabled in config.json")
        cmd, cwd = agentmod.build_command(CFG, job["prompt"])
        lines = []
        stop = threading.Event()
        code, killed = agentmod.run_streaming(cmd, cwd, lambda t: lines.append(t), stop,
                                             timeout_s=agent_run_timeout(CFG))
        TRACE.log("agent_run", id="job-" + job["id"], goal=job["prompt"][:8000], host=st["host"], mode=st["mode"],
                  isolated=st["isolated"], exit=code, killed=killed or False, lines=len(lines), output=lines[-400:],
                  via="job")
        if code != 0:
            raise RuntimeError("harness exited %s%s\n%s" % (code, " (" + killed + ")" if killed else "", "\n".join(lines[-20:])))
        return {"output": "\n".join(lines), "hops": None, "tokens": None}

    model = job.get("model") or ""
    if not model:
        try:
            with urllib.request.urlopen(upstream_request("/models"), timeout=10) as r:
                ids = [m["id"] for m in json.load(r).get("data", [])]
            model = ids[0] if ids else ""
        except Exception:   # noqa: BLE001
            pass
    if not model:
        raise RuntimeError("no model configured for the job and none offered upstream")
    caps = caps_for(model)
    sys_parts = []
    bodies = skill_catalog().bodies(job.get("skills") or [])
    for name, body in bodies.items():
        if body:
            sys_parts.append("# Skill: " + name + "\n\n" + body)
    if job.get("system"):
        sys_parts.append(job["system"])
    sys_parts.append("You are running as a scheduled job named %r at %s. There is no human in the loop: do the task "
                     "with the tools you have, then write the result as your final message." % (job["name"], time.strftime("%Y-%m-%d %H:%M")))
    msgs = [{"role": "system", "content": "\n\n".join(sys_parts)}, {"role": "user", "content": job["prompt"]}]
    tools = None
    if job.get("tools", True) and caps.get("tools", True):
        try:
            tools = mcp_host().openai_tools() or None
        except Exception:   # noqa: BLE001
            tools = None
    total_tokens = 0
    hops = 0
    for hop in range(max(1, int(job.get("max_hops") or 12))):
        payload = {"model": model, "messages": msgs, "max_tokens": 8000, "temperature": 0.3}
        if caps.get("ctk"):
            payload["chat_template_kwargs"] = dict(caps["ctk"])
        if tools:
            payload["tools"] = tools
        resp = upstream_json("/chat/completions", payload)
        TRACE.log("chat", status=200, request=trace_safe(payload), response=resp, purpose="job", job=job["id"])
        usage = resp.get("usage") or {}
        total_tokens += int(usage.get("total_tokens") or 0)
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = msg.get("tool_calls") or []
        hops += 1
        if not calls:
            return {"output": msg.get("content") or "", "hops": hops, "tokens": total_tokens}
        entry = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        if not caps.get("strip_reasoning", True) and msg.get("reasoning_content"):
            entry["reasoning_content"] = msg["reasoning_content"]
        msgs.append(entry)
        for tc in calls:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            text, is_err = mcp_host().call(fn.get("name") or "", args)
            TRACE.log("tool", name=fn.get("name"), args=args, result=str(text)[:4000], is_error=bool(is_err), purpose="job", job=job["id"])
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"), "content": str(text)[:20000]})
    return {"output": "(stopped after %d tool hops without a final answer)" % hops, "hops": hops, "tokens": total_tokens}


def start_scheduler():
    global SCHED
    if SCHED is None:
        SCHED = jobmod.Scheduler(JOBS, run_job, log=print)
        SCHED.start()
    return SCHED


def render_skill_md(meta, body):
    """SKILL.md text from the UI form: the same flat frontmatter the harness
    parses, so one file serves both the console and the agents."""
    lines = ["---", "name: " + meta["name"], "description: " + meta["description"].replace("\n", " ")]
    if meta.get("whenToUse"):
        lines.append("whenToUse: " + meta["whenToUse"].replace("\n", " "))
    if meta.get("tools"):
        lines.append("tools: [" + ", ".join(meta["tools"]) + "]")
    if meta.get("network"):
        lines.append("network: true")
    if meta.get("model"):
        lines.append("model: " + meta["model"])
    lines += ["---", "", body.strip(), ""]
    return "\n".join(lines)


def effective_mcp_servers():
    """config.json servers plus every enabled plugin's servers. A plugin
    server whose name collides with a configured one does not override it."""
    servers = dict(CFG.get("mcp_servers") or {})
    for name, p in sorted(enabled_plugins().items()):
        for sname, spec in (p.mcp_servers or {}).items():
            servers.setdefault(sname, dict(spec, _plugin=name))
    return servers

# MCP servers start lazily on first use: a console that never opens a tool
# should not spawn subprocesses, and a broken server config must not stop the
# console from booting.
_MCP = None
_MCP_LOCK = threading.Lock()


def mcp_host():
    global _MCP
    with _MCP_LOCK:
        if _MCP is None:
            from mcp import MCPHost
            _MCP = MCPHost(effective_mcp_servers())
        return _MCP


def mcp_reload():
    """Tear down every server and start from the current config. Called after
    any edit so changes take effect without restarting the console."""
    global _MCP
    with _MCP_LOCK:
        if _MCP is not None:
            _MCP.stop_all()
        from mcp import MCPHost
        _MCP = MCPHost(effective_mcp_servers())
        return _MCP


def save_config():
    """Persist CFG back to config.json, preserving formatting sanity. Written
    atomically so a crash mid-write cannot leave the console unbootable."""
    path = os.path.join(ROOT, "config.json")
    tmp = path + ".tmp"
    with _LOCK:
        with open(tmp, "w") as f:
            json.dump(CFG, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)


# ------------------------------------------------------------------ archive --
# Compression moves the older part of a conversation out of the model's
# window and replaces it with a summary. The originals are not thrown away:
# each compression writes one file here, full fidelity (reasoning, tool
# calls, tool results) as JSON, with a readable Markdown transcript beside
# it. The transcript marker keeps the file name so it can link back.
ARCHIVE = os.path.join(DATA, "archive")
_ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9_-]+\.(json|md)$")


def write_archive(body):
    session = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("session") or ""))[:40] or "nosession"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(ARCHIVE, exist_ok=True)
    name, n = "%s_%s" % (session, stamp), 0
    while os.path.exists(os.path.join(ARCHIVE, name + ".json")):
        n += 1   # two compressions in one second must not overwrite each other
        name = "%s_%s-%d" % (session, stamp, n)
    msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
    rec = {"session": session, "model": str(body.get("model") or ""),
           "created": time.time(), "summary": str(body.get("summary") or ""),
           "messages": msgs}
    with open(os.path.join(ARCHIVE, name + ".json"), "w") as f:
        json.dump(rec, f, indent=1)
    with open(os.path.join(ARCHIVE, name + ".md"), "w") as f:
        f.write(archive_markdown(rec))
    TRACE.log("archive", session=session, file=name, count=len(msgs), model=rec["model"])
    return name


def read_archives():
    out = []
    if not os.path.isdir(ARCHIVE):
        return out
    for name in sorted(os.listdir(ARCHIVE)):
        if name.endswith(".json"):
            try:
                with open(os.path.join(ARCHIVE, name)) as f:
                    rec = json.load(f)
                rec["id"] = name[:-5]
                out.append(rec)
            except (OSError, ValueError):
                continue
    return out


def archive_markdown(rec):
    out = ["# Archived conversation \u2014 %s" % rec["session"], "",
           "model: %s  " % rec["model"],
           "archived: %s  " % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["created"])),
           "messages: %d" % len(rec["messages"]), "",
           "## Summary that replaced these messages", "", rec["summary"] or "(none)", "",
           "## Transcript", ""]
    for m in rec["messages"]:
        if not isinstance(m, dict):
            continue
        role = "User" if m.get("role") == "user" else "Assistant"
        if m.get("kind") == "summary":
            role = "Context summary (an earlier compression)"
        elif m.get("kind") == "summary-ack":
            continue
        out.append("### " + role)
        if m.get("reasoning"):
            out += ["", "<details><summary>reasoning</summary>", "", str(m["reasoning"]), "", "</details>"]
        out += ["", str(m.get("content") or ""), ""]
        for t in (m.get("toolUse") or []):
            if not isinstance(t, dict):
                continue
            out += ["**tool: %s**" % t.get("name", ""), "", "```json", str(t.get("args") or "{}"),
                    "```", "", "```", str(t.get("result", ""))[:20000], "```", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- sessions --
def _sessions_path():
    return os.path.join(DATA, "sessions.json")


def read_sessions():
    try:
        with open(_sessions_path()) as f:
            return json.load(f)
    except Exception:
        return []


def write_sessions(sessions):
    tmp = _sessions_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sessions, f)
    os.replace(tmp, _sessions_path())


# ------------------------------------------------------------------- usage --
def sanitize_usage(body):
    """The ledger is permanent; one garbage line must never poison every read.
    Coerce to known-good types here, and drop the event if nothing survives."""
    if not isinstance(body, dict):
        return None
    def as_int(v):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return None
    def as_float(v):
        try:
            return round(float(v), 3)
        except (TypeError, ValueError):
            return None
    evt = {
        "model": str(body.get("model") or "unknown")[:200],
        "prompt_tokens": as_int(body.get("prompt_tokens")),
        "completion_tokens": as_int(body.get("completion_tokens")),
        "ttft_s": as_float(body.get("ttft_s")),
        "decode_tok_s": as_float(body.get("decode_tok_s")),
        "estimated": bool(body.get("estimated")),
    }
    return evt if evt["completion_tokens"] else None


def append_usage(evt):
    evt["ts"] = int(time.time())
    with _LOCK:
        with open(os.path.join(DATA, "usage.jsonl"), "a") as f:
            f.write(json.dumps(evt) + "\n")


def usage_summary():
    now = time.time()
    horizon = now - 14 * 86400
    days = {}
    by_model = {}
    tps = []
    tot_in = tot_out = 0
    try:
        with open(os.path.join(DATA, "usage.jsonl")) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(e, dict) or e.get("ts", 0) < horizon:
                    continue
                try:  # old or hand-edited lines must not poison the ledger
                    out = int(e.get("completion_tokens") or 0)
                    inn = int(e.get("prompt_tokens") or 0)
                    dec = float(e["decode_tok_s"]) if e.get("decode_tok_s") else None
                except (TypeError, ValueError):
                    continue
                tot_out += out
                tot_in += inn
                day = time.strftime("%d", time.localtime(e["ts"]))
                days[day] = days.get(day, 0) + out
                m = str(e.get("model") or "unknown")
                by_model[m] = by_model.get(m, 0) + out
                if dec and not e.get("estimated"):
                    tps.append(dec)
    except FileNotFoundError:
        pass
    tps.sort()
    rates = CFG.get("frontier_rates_per_mtok", {})
    saved = (tot_in / 1e6) * float(rates.get("input", 0)) + \
            (tot_out / 1e6) * float(rates.get("output", 0))
    # the agent plane's tokens live on the worker, not in this ledger
    agents = agentmod.agent_usage(CFG)
    agents_saved = 0.0
    if agents and not agents.get("error"):
        a_in = agents.get("slave_prompt", 0) + agents.get("master_prompt", 0)
        a_out = agents.get("slave_completion", 0) + agents.get("master_completion", 0)
        # older spawn records carry only a total: price it as input (conservative)
        unsplit = max(0, agents.get("slave_tokens", 0) - agents.get("slave_prompt", 0) - agents.get("slave_completion", 0))
        a_in += unsplit + agents.get("panel_tokens", 0)
        agents_saved = (a_in / 1e6) * float(rates.get("input", 0)) + (a_out / 1e6) * float(rates.get("output", 0))
        agents["total"] = agents.get("slave_tokens", 0) + agents.get("master_prompt", 0) + agents.get("master_completion", 0) + agents.get("panel_tokens", 0)
        agents["frontier_saved_usd"] = round(agents_saved, 2)
    return {
        "total_out": tot_out,
        "median_tok_s": tps[len(tps) // 2] if tps else None,
        "requests": sum(1 for _ in tps) or None,
        "frontier_saved_usd": round(saved + agents_saved, 2),
        "chat_saved_usd": round(saved, 2),
        "days": days,
        "by_model": by_model,
        "agents": agents,
    }


# -------------------------------------------------------------- prometheus --
def prom_query(q):
    base = CFG.get("prometheus_url", "").rstrip("/")
    if not base:
        return None
    url = base + "/api/v1/query?" + urllib.parse.urlencode({"query": q})
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            d = json.load(r)
        if d.get("status") == "success":
            return d["data"]["result"]
    except Exception:
        return None
    return None


# Queries assume utkuozdemir/nvidia_gpu_exporter + node-exporter, one pair per
# node, labelled by instance. Adjust to your labels; every query failing just
# renders as an em-dash in the UI, never a fake number.
def _prom_query(q):
    base = CFG.get("prometheus_url", "").rstrip("/")
    url = base + "/api/v1/query?query=" + urllib.parse.quote(q)
    with urllib.request.urlopen(url, timeout=3) as r:
        d = json.load(r)
    vals = [float(s["value"][1]) for s in d.get("data", {}).get("result", [])]
    return sum(vals) if vals else None


def engine_stats():
    """The engine's own word on whether it is working. Some tool parsers
    (measured: deepseek_v4 — 64s of silent wire, then a whole file in 15
    bursts) buffer a tool call server-side while the GPU streams into a
    buffer. A dead stream and a busy-but-buffered stream are identical from
    the client, so the console asks Prometheus, which scrapes vLLM directly.
    Colons are legal in Prometheus metric names but not in PromQL bare
    selectors — hence the __name__ form."""
    if not CFG.get("prometheus_url"):
        return {"ok": False}
    try:
        rate = _prom_query('sum(rate({__name__="vllm:generation_tokens_total"}[20s]))')
        prompt = _prom_query('sum(rate({__name__="vllm:prompt_tokens_total"}[20s]))')
        running = _prom_query('sum({__name__="vllm:num_requests_running"})')
        if rate is None and prompt is None and running is None:
            return {"ok": False, "error": "prometheus has no vllm metrics"}
        return {"ok": True, "rate": rate or 0.0, "prompt_rate": prompt or 0.0,
                "running": -1 if running is None else running}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


# ---------------------------------------------------------------- netcheck --
# "Is my model truly local?" deserves a measurement, not an assurance. The
# head node's rack net audits every serving container from inside its own
# pid namespace (host-side ss -p silently loses root-owned sockets without
# sudo) and tags each established connection LOCAL or INTERNET. This runs it
# over SSH and caches the answer — a verdict is stable for minutes, and a
# page refresh should not fork ssh.
_NET = {"at": 0, "result": None}
_NET_LOCK = threading.Lock()


def netcheck(fresh=False):
    target = CFG.get("netcheck_ssh", "")
    if not target:
        return {"ok": False, "error": "netcheck_ssh not configured"}
    with _NET_LOCK:
        if not fresh and _NET["result"] and time.time() - _NET["at"] < 300:
            return _NET["result"]
        import subprocess
        try:
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
                 "cd dgx/dgx-spark-serve && ./rack net"],
                capture_output=True, text=True, timeout=60)
            out = re.sub(r"\x1b\[[0-9;]*m", "", (p.stdout + p.stderr)).strip()
            res = {"ok": p.returncode == 0 or "VERDICT" in out,
                   "at": int(time.time()),
                   "lines": out.splitlines()[:40]}
        except subprocess.TimeoutExpired:
            res = {"ok": False, "at": int(time.time()),
                   "error": "audit timed out after 60s"}
        except Exception as e:
            res = {"ok": False, "at": int(time.time()), "error": str(e)[:200]}
        _NET.update(at=time.time(), result=res)
        return res


_sd_cache = {"at": 0, "val": None}


def sparkdash_telemetry():
    """Node cards from sparkDash (MiaAI-Lab/sparkDash) instead of Prometheus:
    one GET for the unit list, one per unit for its snapshot. sparkDash is
    loopback-only on the head Spark; the console reaches it through the same
    ssh tunnel it uses for Prometheus (sparkdash_url, e.g. 127.0.0.1:15555).
    Its snapshot also carries the unit's LLM probe — model, tok/s, KV cache,
    queue, TTFT — which Prometheus never gave the cards."""
    now = time.time()
    if _sd_cache["val"] is not None and now - _sd_cache["at"] < 2:
        return _sd_cache["val"]
    base = CFG.get("sparkdash_url", "").rstrip("/")

    def get(path):
        with urllib.request.urlopen(urllib.request.Request(base + path), timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    try:
        units = get("/api/sparks")
        units = units if isinstance(units, list) else (units.get("sparks") or [])
    except Exception as e:   # noqa: BLE001
        val = {"nodes": [], "source": "sparkdash", "error": "sparkDash unreachable: %s" % str(e)[:100]}
        _sd_cache.update(at=now, val=val)
        return val
    out = []
    for u in units:
        uid = u.get("id")
        if not uid:
            continue
        try:
            d = get("/api/sparks/%s/metrics" % urllib.parse.quote(str(uid)))
        except Exception:   # noqa: BLE001
            d = dict(u, metrics={}, online=False)
        m = d.get("metrics") or {}
        gpu = m.get("gpu") or {}
        vram = gpu.get("vram") or {}
        cpu = m.get("cpu") or {}
        power = gpu.get("power") or {}
        kind = d.get("kind") or u.get("kind") or "spark"
        online = bool(d.get("online", u.get("online", True)))
        node = {
            "name": d.get("name") or u.get("name") or uid,
            "id": uid, "kind": kind, "role": d.get("role") or u.get("role"),
            "online": online, "source": "sparkdash",
            "util": gpu.get("usage") if online else None,
            "mem_used_gb": round(vram["used"] / 1024, 1) if online and vram.get("used") is not None else None,
            "mem_total_gb": round(vram["total"] / 1024) if vram.get("total") else None,
            "mem_label": "VRAM" if kind == "host" else "Unified memory",
            "temp": gpu.get("temperature") if online else None,
            "power": power.get("draw") if online else None,
            "cpu": cpu.get("usage") if online else None,
            "uptime_s": d.get("uptime") if online else None,
            "hardware": (d.get("hardware") or {}).get("device"),
            "throttle": (gpu.get("throttle") or {}).get("reason"),
        }
        llms = m.get("llm") or []
        if isinstance(llms, dict):
            llms = [llms]
        live = [x for x in llms if isinstance(x, dict) and x.get("available")]
        if live:
            x = live[0]
            node["llm"] = {
                "model": x.get("modelId"), "backend": x.get("backend"),
                "tps": x.get("generationTps"), "prefill_tps": x.get("prefillTps"),
                "kv": x.get("kvCacheUsage"), "running": x.get("requestsRunning"),
                "waiting": x.get("requestsWaiting"), "ttft_p95": x.get("ttftP95Seconds"),
                "prefix_hit": x.get("prefixCacheHitRate"), "context": x.get("contextLength"),
                "ports": len(live),
            }
        out.append(node)
    val = {"nodes": out, "source": "sparkdash"}
    _sd_cache.update(at=now, val=val)
    return val


def telemetry_source():
    src = (CFG.get("telemetry_source") or "").strip().lower()
    if src in ("sparkdash", "prometheus"):
        return src
    if CFG.get("sparkdash_url") and not CFG.get("prometheus_url"):
        return "sparkdash"
    return "prometheus"


def telemetry():
    if telemetry_source() == "sparkdash" and CFG.get("sparkdash_url"):
        return sparkdash_telemetry()
    out = []
    for node in CFG.get("nodes", []):
        inst = node.get("instance", node["name"])
        def one(q):
            r = prom_query(q % {"i": inst})
            try:
                return float(r[0]["value"][1])
            except (TypeError, IndexError, KeyError, ValueError):
                return None
        mem_total = one('node_memory_MemTotal_bytes{instance=~"%(i)s.*"}')
        mem_avail = one('node_memory_MemAvailable_bytes{instance=~"%(i)s.*"}')
        used_gb = None
        if mem_total and mem_avail:
            used_gb = round((mem_total - mem_avail) / 2**30, 1)
        util = one('nvidia_smi_utilization_gpu_ratio{instance=~"%(i)s.*"}')
        out.append({
            "name": node["name"],
            # exporter reports a 0-1 ratio; the UI speaks percent
            "util": round(util * 100, 1) if util is not None else None,
            "mem_used_gb": used_gb,
            "mem_total_gb": round(mem_total / 2**30) if mem_total else None,
            "temp": one('nvidia_smi_temperature_gpu{instance=~"%(i)s.*"}'),
            "power": one('nvidia_smi_power_draw_watts{instance=~"%(i)s.*"}'),
            "cpu": one('100 - avg(rate(node_cpu_seconds_total{mode="idle",instance=~"%(i)s.*"}[1m])) * 100'),
            "uptime_s": one('time() - node_boot_time_seconds{instance=~"%(i)s.*"}'),
        })
    return {"nodes": out}


# ---------------------------------------------------------------- upstream --
def upstream_request(path, payload=None, method="GET"):
    url = CFG["upstream_url"].rstrip("/") + path
    headers = {
        "Authorization": "Bearer " + CFG.get("upstream_key", "none"),
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    return urllib.request.Request(url, data=data, headers=headers, method=method)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # Loopback binding stops remote packets, not the operator's own browser.
    # A hostile page can reach 127.0.0.1 via DNS rebinding (its origin becomes
    # this host) or fire preflight-free "simple" POSTs cross-origin. So: only
    # accept our own Host, reject foreign Origins, and require JSON POSTs.
    def _guard(self):
        hostname = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if hostname not in ("127.0.0.1", "localhost", "::1", CFG.get("bind", "")):
            self._json({"error": "bad host"}, 403, close=True)
            return False
        origin = self.headers.get("Origin")
        if origin:
            ohost = urllib.parse.urlparse(origin).hostname
            if ohost not in ("127.0.0.1", "localhost", "::1", CFG.get("bind", "")):
                self._json({"error": "cross-origin denied"}, 403, close=True)
                return False
        if self.command in ("POST", "DELETE"):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            # /api/video carries file uploads; everything else stays JSON-only.
            # Multipart is a "simple request" a hostile form could fire without
            # preflight — but form POSTs carry Origin, and the check above
            # already rejected foreign ones.
            path = urllib.parse.urlparse(self.path).path
            want = "multipart/form-data" if path == "/api/video" else "application/json"
            if self.command == "POST" and ctype != want:
                self._drain()
                self._json({"error": "expected " + want}, 415, close=True)
                return False
        return True

    def _drain(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            while n > 0:
                n -= len(self.rfile.read(min(n, 65536)) or b"x")
        except (ValueError, OSError):
            self.close_connection = True

    def do_OPTIONS(self):
        self._json({"error": "forbidden"}, 403, close=True)

    # ---- helpers ----
    def _json(self, obj, code=200, close=False):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        fs = os.path.realpath(os.path.join(PUBLIC, path.lstrip("/")))
        if not fs.startswith(os.path.realpath(PUBLIC)) or not os.path.isfile(fs):
            self._json({"error": "not found"}, 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css",
            ".js": "text/javascript",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(fs)[1], "application/octet-stream")
        with open(fs, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # revalidate every load: a fix to console.js must not wait on a
        # heuristic browser cache to expire
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _export(self, q):
        """Training data as JSONL: ?from=YYYY-MM-DD&to=...&model=substr
        &rated=up|any&errors=1&redact=0&source=all|traces|sessions
        &purpose=chat|compress. Streams; there is no telling the size first."""
        one = lambda k, d=None: (q.get(k) or [d])[0]
        with _LOCK:
            sessions = read_sessions()
        gen = export_lines(
            TRACE, sessions, read_archives(),
            day_from=one("from"), day_to=one("to"), model=one("model"),
            rated=one("rated"), include_errors=one("errors") == "1",
            do_redact=one("redact") != "0", source=one("source", "all"), purpose=one("purpose"))
        self.send_response(200)
        self.send_header("Content-Type", "application/jsonl; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="bytebunker-train-%s.jsonl"' % time.strftime("%Y%m%d-%H%M"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        n = 0
        try:
            for line in gen:
                self.wfile.write(line.encode("utf-8"))
                n += 1
        except (BrokenPipeError, ConnectionResetError):
            return
        TRACE.log("export", examples=n, query={k: v[0] for k, v in q.items()})

    def _archive_get(self, name):
        if not _ARCHIVE_NAME.match(name):
            self._json({"error": "not found"}, 404)
            return
        fs = os.path.join(ARCHIVE, name)
        if not os.path.isfile(fs):
            self._json({"error": "not found"}, 404)
            return
        with open(fs, "rb") as f:
            body = f.read()
        self.send_response(200)
        # text/plain for the .md: the browser shows it instead of downloading
        self.send_header("Content-Type", "application/json" if name.endswith(".json")
                         else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """Read and parse the JSON body; None (plus a 400 already sent) on garbage."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "invalid JSON body"}, 400, close=True)
            return None

    # ---- video (MiniMax-H3 via vLLM-Omni) ----
    # /v1/videos is multipart form in, MP4 out, with async job polling. The
    # engine binds to loopback on spark-1 and is reached through the same SSH
    # tunnel that carries Prometheus — this proxy adds only the hop, plus the
    # host/origin guard every other route gets. Nothing new opens on the LAN.
    def _video_post(self):
        base = (CFG.get("h3_url") or "").rstrip("/")
        if not base:
            self._drain()
            self._json({"error": "no h3_url configured"}, 503)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= 80 * 1024 * 1024:  # engine rejects >64 MB; fail fast here
            self._drain()
            self._json({"error": "body missing or over 80 MB"}, 413)
            return
        req = urllib.request.Request(
            base + "/v1/videos", data=self.rfile.read(n), method="POST",
            headers={"Content-Type": self.headers.get("Content-Type") or ""})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                self._json(json.load(r))
        except urllib.error.HTTPError as e:
            self._json({"error": e.read().decode("utf-8", "replace")[:500]}, e.code)
        except Exception as e:
            self._json({"error": str(e)[:300]}, 502)

    def _video_get(self, path):
        base = (CFG.get("h3_url") or "").rstrip("/")
        if not base:
            self._json({"error": "no h3_url configured", "data": []}, 503)
            return
        parts = [p for p in path[len("/api/video"):].split("/") if p]
        bad = (len(parts) > 2 or (len(parts) == 2 and parts[1] != "content")
               or (parts and not re.fullmatch(r"[\w.-]+", parts[0])))
        if bad:
            self._json({"error": "not found"}, 404)
            return
        url = base + "/v1/videos" + "".join("/" + p for p in parts)
        try:
            if len(parts) == 2:  # the MP4 itself — stream it through
                with urllib.request.urlopen(url, timeout=600) as r:
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     r.headers.get("Content-Type") or "video/mp4")
                    clen = r.headers.get("Content-Length")
                    if clen:
                        self.send_header("Content-Length", clen)
                    else:  # unknown length can't keep-alive on HTTP/1.1
                        self.send_header("Connection", "close")
                        self.close_connection = True
                    self.end_headers()
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            else:  # job status, or the job list
                with urllib.request.urlopen(url, timeout=30) as r:
                    self._json(json.load(r))
        except urllib.error.HTTPError as e:
            self._json({"error": e.read().decode("utf-8", "replace")[:500]}, e.code)
        except Exception as e:
            self._json({"error": str(e)[:300]}, 502)

    # ---- routes ----
    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            self._json({
                # spec is optional operator-declared hardware copy; the UI
                # renders it verbatim or omits the line — it never invents one
                "nodes": [{"name": n["name"], "spec": n.get("spec", "")}
                          for n in CFG.get("nodes", [])],
                "identity": CFG.get("identity", {}),
                "upstream": CFG.get("upstream_url", ""),
                "telemetry": bool(CFG.get("prometheus_url") or CFG.get("sparkdash_url")),
                "telemetry_source": telemetry_source(),
                "sparkdash_open_url": CFG.get("sparkdash_open_url") or "",
                "mcp": bool(CFG.get("mcp_servers")),
                "video": bool(CFG.get("h3_url")),
                "netcheck": bool(CFG.get("netcheck_ssh")),
            })
        elif path == "/api/models":
            try:
                with urllib.request.urlopen(upstream_request("/models"), timeout=8) as r:
                    body = json.load(r)
                for m in body.get("data") or []:
                    m["caps"] = caps_for(m.get("id"))
                self._json(body)
            except Exception as e:
                self._json({"error": str(e), "data": []}, 502)
        elif path == "/api/telemetry":
            self._json(telemetry())
        elif path == "/api/engine":
            self._json(engine_stats())
        elif path == "/api/netcheck":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(netcheck(fresh=bool(q.get("fresh"))))
        elif path == "/api/tools":
            try:
                h = mcp_host()
                defs = h.openai_tools()
                body = {"servers": h.status, "tools": [
                    {"name": t["function"]["name"],
                     "description": t["function"]["description"][:200]}
                    for t in defs]}
                if urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("full"):
                    body["defs"] = defs   # real JSON Schemas for the model
                body["config"] = CFG.get("mcp_servers", {})
                self._json(body)
            except Exception as e:
                self._json({"servers": {}, "tools": [], "error": str(e)[:300]})
        elif path == "/api/sessions":
            self._json(read_sessions())
        elif path.startswith("/api/archive/"):
            self._archive_get(path[len("/api/archive/"):])
        elif path == "/api/agents":
            st = agentmod.status(CFG)
            a = CFG.get("agents") or {}
            st["master_name"] = a.get("master_name") or ""
            st["master_instructions"] = a.get("master_instructions") or ""
            st["run_timeout_s"] = agent_run_timeout(CFG)
            runs = []
            for e in TRACE.events(time.strftime("%Y-%m-%d",
                                  time.localtime(time.time() - 14 * 86400))):
                if e.get("kind") == "agent_run":
                    runs.append({"id": e.get("id"), "ts": e.get("ts"), "goal": e.get("goal"),
                                 "exit": e.get("exit"), "killed": e.get("killed"),
                                 "lines": e.get("lines"), "host": e.get("host")})
            st["recent"] = runs[-40:][::-1]
            self._json(st)
        elif path == "/api/agents/log":
            rid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
            found = None
            for e in TRACE.events(time.strftime("%Y-%m-%d",
                                  time.localtime(time.time() - 30 * 86400))):
                if e.get("kind") == "agent_run" and e.get("id") == rid:
                    found = e
            if not found:
                self._json({"error": "run not found"}, 404)
            else:
                self._json({"id": rid, "goal": found.get("goal"), "output": found.get("output") or [],
                            "exit": found.get("exit"), "killed": found.get("killed"),
                            "host": found.get("host"), "ts": found.get("ts")})
        elif path == "/api/agents/slaves":
            self._json(agentmod.recent_slaves(CFG))
        elif path == "/api/agents/stats":
            self._json(agentmod.stats(CFG))
        elif path == "/api/agents/slave":
            sid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
            self._json(agentmod.slave_detail(CFG, sid))
        elif path == "/api/skills":
            cat = skill_catalog()
            self._json({"skills": cat.summaries(), "warnings": cat.warnings})
        elif path.startswith("/api/skills/"):
            name = path[len("/api/skills/"):]
            sk = skill_catalog().get(name)
            if not sk:
                self._json({"error": "no such skill"}, 404)
            else:
                self._json({"name": sk.name, "body": sk.body(), **sk.summary()})
        elif path == "/api/plugins":
            found, warnings = discover_plugins()
            st = _plugin_state()
            self._json({"plugins": [found[n].info(bool(st.get(n, {}).get("enabled")))
                                    for n in sorted(found)], "warnings": warnings})
        elif path == "/api/rack":
            self._json(rack_overview())
        elif path.startswith("/api/uploads/"):
            rel = path[len("/api/uploads/"):]
            parts = [urllib.parse.unquote(x) for x in rel.split("/") if x]
            if len(parts) != 2 or any(_SAFE_NAME.sub("_", x) != x for x in parts):
                self._json({"error": "bad path"}, 400)
                return
            fp = os.path.join(uploads_root(), parts[0], parts[1])
            if not os.path.isfile(fp):
                self._json({"error": "no such file"}, 404)
                return
            import mimetypes
            ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "inline; filename=\"%s\"" % parts[1])
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/jobs":
            self._json({"jobs": JOBS.list(), "running": SCHED.running if SCHED else None})
        elif path == "/api/jobs/runs":
            jid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
            self._json({"id": jid, "runs": JOBS.runs(jid, 30)})
        elif path == "/api/mcp/catalog":
            try:
                st = mcp_host().status
            except Exception as e:   # noqa: BLE001
                st = {"_error": str(e)[:200]}
            self._json({"catalog": catmod.catalog_view(CFG.get("mcp_servers", {})),
                        "runtimes": catmod.runtimes(), "servers": st,
                        "config": CFG.get("mcp_servers", {})})
        elif path == "/api/recipes":
            a = CFG.get("agents") or {}
            lt = CFG.get("litellm") or {}
            self._json({"recipes": recipemod.catalog(),
                        "defaults": {"host": a.get("ssh") or "", "lan_ip": ""},
                        "litellm": {"configured": bool(lt.get("ssh") and lt.get("config_path")),
                                    "ssh": lt.get("ssh") or "", "container": lt.get("container") or ""}})
        elif path == "/api/traces":
            self._json(TRACE.stats())
        elif path == "/api/export":
            self._export(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
        elif path == "/api/usage":
            self._json(usage_summary())
        elif path == "/api/video" or path.startswith("/api/video/"):
            self._video_get(path)
        else:
            self._static(path)

    def do_DELETE(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        self._drain()
        if u.path == "/api/sessions":
            sid = urllib.parse.parse_qs(u.query).get("id", [None])[0]
            with _LOCK:
                cur = read_sessions()
                for gone in cur:
                    if gone.get("id") == sid:
                        TRACE.log("session_deleted", session=sid, record=gone)
                write_sessions([s for s in cur if s.get("id") != sid])
            self._json({"ok": True})
        elif u.path.startswith("/api/video/"):
            base = (CFG.get("h3_url") or "").rstrip("/")
            vid = u.path.rsplit("/", 1)[1]
            if not base or not re.fullmatch(r"[\w.-]+", vid):
                self._json({"error": "not found"}, 404)
                return
            try:
                req = urllib.request.Request(base + "/v1/videos/" + vid,
                                             method="DELETE")
                with urllib.request.urlopen(req, timeout=30):
                    self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)[:300]}, 502)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/video":  # multipart, not JSON — handled whole
            self._video_post()
            return
        if path not in ("/api/chat", "/api/sessions", "/api/usage-event",
                        "/api/tool-call", "/api/mcp", "/api/archive", "/api/rate",
                        "/api/plugins", "/api/skills", "/api/recipes", "/api/rack", "/api/upload", "/api/jobs", "/api/agents", "/api/agents/slave"):
            self._drain()  # unread bodies desync HTTP/1.1 keep-alive
            self._json({"error": "not found"}, 404)
            return
        body = self._body()
        if body is None:
            return
        if path == "/api/chat":
            self._chat(body)
        elif path == "/api/tool-call":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            t0 = time.time()
            text, is_err = mcp_host().call(body.get("name") or "", body.get("arguments") or {})
            TRACE.log("tool", session=self.headers.get("X-BB-Session"),
                      turn=self.headers.get("X-BB-Turn"), name=body.get("name") or "",
                      arguments=body.get("arguments") or {}, result=str(text)[:200000],
                      is_error=bool(is_err), ms=int((time.time() - t0) * 1000))
            self._json({"content": text, "isError": is_err})
        elif path == "/api/mcp":
            self._mcp_admin(body)
        elif path == "/api/plugins":
            self._plugin_admin(body)
        elif path == "/api/skills":
            self._skill_admin(body)
        elif path == "/api/recipes":
            self._recipes(body)
        elif path == "/api/jobs":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            action = body.get("action") or "save"
            try:
                if action == "save":
                    job = JOBS.upsert(body.get("job") or body)
                    TRACE.log("job", action="save", id=job["id"], name=job["name"], schedule=job["schedule"], kind=job["kind"])
                    self._json({"ok": True, "job": job, "jobs": JOBS.list()})
                elif action == "delete":
                    JOBS.delete(str(body.get("id") or ""))
                    TRACE.log("job", action="delete", id=body.get("id"))
                    self._json({"ok": True, "jobs": JOBS.list()})
                elif action == "toggle":
                    j = JOBS.get(str(body.get("id") or ""))
                    if not j:
                        self._json({"error": "no such job"}, 404)
                        return
                    JOBS.set_enabled(j["id"], not j.get("enabled", True))
                    self._json({"ok": True, "jobs": JOBS.list()})
                elif action == "run_now":
                    j = JOBS.get(str(body.get("id") or ""))
                    if not j:
                        self._json({"error": "no such job"}, 404)
                        return
                    if SCHED and SCHED.running:
                        self._json({"error": "a job is already running (%s); try again when it finishes" % SCHED.running}, 409)
                        return
                    threading.Thread(target=lambda: start_scheduler().run_job(j, "manual"), daemon=True).start()
                    self._json({"ok": True, "started": j["id"]})
                else:
                    self._json({"error": "unknown action"}, 400)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif path == "/api/upload":
            import base64
            if not isinstance(body, dict) or not body.get("data"):
                self._json({"error": "expected {session, name, data(base64)}"}, 400)
                return
            try:
                raw = base64.b64decode(str(body["data"]).split(",", 1)[-1], validate=False)
            except Exception:   # noqa: BLE001
                self._json({"error": "data is not base64"}, 400)
                return
            if len(raw) > UPLOAD_MAX:
                self._json({"error": "file larger than %d MB" % (UPLOAD_MAX // 2**20)}, 413)
                return
            try:
                info = save_upload(body.get("session"), body.get("name"), raw)
            except OSError as e:
                self._json({"error": "could not save: %s" % e}, 500)
                return
            TRACE.log("upload", session=body.get("session"), name=info["name"], size=info["size"], path=info["path"])
            self._json(dict(info, ok=True))
        elif path == "/api/rack":
            self._rack(body)
        elif path == "/api/agents":
            self._agents_run(body)
        elif path == "/api/agents/slave":
            if not isinstance(body, dict) or body.get("action") != "kill":
                self._json({"error": "expected {action: kill, id}"}, 400)
                return
            res = agentmod.kill_slave(CFG, str(body.get("id") or ""))
            TRACE.log("slave_kill", id=body.get("id"), result=res.get("result"), ok=res.get("ok"))
            self._json(res)
        elif path == "/api/rate":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            try:
                rating = max(-1, min(1, int(body.get("rating") or 0)))
            except (TypeError, ValueError):
                rating = 0
            TRACE.log("rating", session=body.get("session"), turn=body.get("turn"),
                      traces=[str(t)[:32] for t in (body.get("traces") or [])][:64],
                      model=body.get("model"), rating=rating)
            self._json({"ok": True})
        elif path == "/api/archive":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            try:
                self._json({"ok": True, "file": write_archive(body)})
            except OSError as e:
                self._json({"error": "could not write archive: " + str(e)[:200]}, 500)
        elif path == "/api/sessions":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            with _LOCK:
                cur = [x for x in read_sessions() if x.get("id") != body.get("id")]
                cur.insert(0, body)
                for old in cur[200:]:   # aged out of the UI, not out of the record
                    TRACE.log("session_evicted", session=old.get("id"), record=old)
                write_sessions(cur[:200])
            self._json({"ok": True})
        elif path == "/api/usage-event":
            evt = sanitize_usage(body)
            if evt:
                append_usage(evt)
            self._json({"ok": bool(evt)})

    # ---- MCP server management ----
    # add / remove / toggle / restart, persisted to config.json and applied
    # live. Command strings are never shell-parsed — they go straight to
    # Popen as argv, so there is no shell-injection surface here.
    def _agents_run(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        if body.get("action") == "config":
            # Save the master's name + instructions into the agents block.
            a = CFG.setdefault("agents", {})
            a["master_name"] = str(body.get("master_name") or "")[:120]
            a["master_instructions"] = str(body.get("master_instructions") or "")[:8000]
            if body.get("run_timeout_s") is not None:
                try:
                    # 5 min .. 24 h; a multi-step goal on one Spark is a 1-2 h affair
                    a["run_timeout_s"] = max(300, min(86400, int(body.get("run_timeout_s"))))
                except (TypeError, ValueError):
                    pass
            save_config()
            self._json({"ok": True, "master_name": a["master_name"],
                        "master_instructions": a["master_instructions"],
                        "run_timeout_s": agent_run_timeout(CFG)})
            return
        goal = body.get("goal") or ""
        try:
            cmd = agentmod.build_command(CFG, goal)
        except agentmod.AgentConfigError as e:
            self._json({"error": str(e)}, 400)
            return
        st = agentmod.status(CFG)
        cwd = None if st["mode"] == "ssh" else os.path.expanduser((CFG.get("agents") or {}).get("dir") or ".")
        started = time.time()
        rid = TRACE.new_id()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        stop = threading.Event()
        lines = [0]
        captured = []   # full master output, stored so a past run can be reopened
        wlock = threading.Lock()   # emitter and heartbeat share one socket

        def emit(obj):
            try:
                with wlock:
                    self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                    self.wfile.flush()
            except OSError:
                stop.set()   # client went away — stop the run

        emit({"phase": "start", "id": rid, "host": st["host"], "mode": st["mode"], "isolated": st["isolated"]})

        # A run that prints nothing for minutes would never notice its client
        # left (a dropped connection only shows up on the next write). An SSE
        # comment every 10 s is invisible to the client's parser and makes the
        # write fail promptly, which sets `stop` and kills the remote run.
        def heartbeat():
            while not stop.wait(5):
                try:
                    with wlock:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except OSError:
                    stop.set()
        threading.Thread(target=heartbeat, daemon=True).start()

        def on_line(text):
            lines[0] += 1
            if len(captured) < 4000:
                captured.append(text)
            emit({"line": text})

        try:
            code, killed = agentmod.run_streaming(cmd, cwd, on_line, stop,
                                                  timeout_s=agent_run_timeout(CFG))
        except FileNotFoundError as e:
            emit({"error": "could not launch the harness: %s" % e})
            code, killed = -1, "error"
        except Exception as e:   # noqa: BLE001 - report anything to the client
            emit({"error": str(e)[:300]})
            code, killed = -1, "error"
        emit({"phase": "done", "exit": code, "killed": killed})
        TRACE.log("agent_run", id=rid, goal=goal[:8000], host=st["host"], mode=st["mode"],
                  isolated=st["isolated"], exit=code, killed=killed or False,
                  lines=lines[0], output=captured, ms=int((time.time() - started) * 1000))

    def _rack(self, body):
        """show <recipe>: the recipe's .env; up/down/logs/bench: run rack and
        stream its output (SSE). `up` and `down` change what the Sparks
        serve — the UI confirms first; the trace log records who did what."""
        import subprocess
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        r = rack_cfg()
        if not r["ssh"]:
            self._json({"error": "rack is not configured (rack.ssh in config.json)"}, 400)
            return
        action = str(body.get("action") or "")
        recipe = str(body.get("recipe") or "").strip()
        if recipe and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", recipe):
            self._json({"error": "bad recipe name"}, 400)
            return
        if action == "show":
            if not recipe:
                self._json({"error": "recipe required"}, 400)
                return
            inner = "cd %s && cat %s" % (agentmod._rq(r["dir"]), shlex_quote("recipes/%s.env" % recipe))
            out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r["ssh"], inner],
                                 capture_output=True, text=True, timeout=20)
            self._json({"ok": out.returncode == 0, "recipe": recipe, "text": (out.stdout or out.stderr)[-20000:]})
            return
        if action not in ("up", "down", "logs", "status", "bench", "preflight", "gateway"):
            self._json({"error": "unknown action"}, 400)
            return
        args = [action] + ([recipe] if recipe and action in ("up", "bench", "gateway") else [])
        if action == "logs":
            args = ["logs"]
        _rack_cache["at"] = 0                      # status changes after this
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")      # the stream has no length: closing is how the client learns it ended
        self.end_headers()
        self.close_connection = True
        wlock = threading.Lock()
        stop = threading.Event()

        def emit(obj):
            try:
                with wlock:
                    self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                    self.wfile.flush()
            except OSError:
                stop.set()
        emit({"phase": "start", "host": r["ssh"], "cmd": "rack " + " ".join(args)})
        captured = []

        def on_line(text):
            if len(captured) < 2000:
                captured.append(text)
            emit({"line": text})
        try:
            code, killed = agentmod.run_streaming(rack_cmd(args), ROOT, on_line, stop, timeout_s=3600)
        except Exception as e:   # noqa: BLE001
            emit({"error": str(e)})
            code, killed = -1, "error"
        emit({"phase": "done", "exit": code, "killed": killed})
        TRACE.log("rack", action=action, recipe=recipe, host=r["ssh"], exit=code,
                  killed=killed or False, output=captured[-200:])

    def _recipes(self, body):
        """render: the exact files a recipe would run; deploy: run its script
        over ssh on the target host and stream the output (SSE, like an
        agent run); register: append the litellm entry on the gateway host
        and restart the gateway."""
        import subprocess, shlex as _shlex
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action") or "render"
        if action == "register":
            lt = CFG.get("litellm") or {}
            entry = str(body.get("entry") or "")
            if not (lt.get("ssh") and lt.get("config_path")):
                self._json({"error": "litellm is not configured for the console: set litellm.ssh, litellm.config_path, litellm.container in config.json", "entry": entry}, 400)
                return
            if "model_name:" not in entry:
                self._json({"error": "entry must be a litellm model_list item"}, 400)
                return
            script = recipemod.litellm_register_script(lt["config_path"], lt.get("container") or "litellm", entry)
            try:
                r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", lt["ssh"], "bash -c " + _shlex.quote(script)],
                                   capture_output=True, text=True, timeout=90)
                out = (r.stdout + r.stderr).strip()
                TRACE.log("recipe", action="register", entry=entry[:400], ok=r.returncode == 0, out=out[:800])
                self._json({"ok": r.returncode == 0, "output": out[-2000:]})
            except (OSError, subprocess.TimeoutExpired) as e:
                self._json({"error": "register failed: %s" % e}, 500)
            return
        try:
            rendered = recipemod.render(str(body.get("id") or ""), body.get("params") or {})
        except KeyError:
            self._json({"error": "no such recipe"}, 404)
            return
        except (TypeError, ValueError) as e:
            self._json({"error": "bad parameter: %s" % e}, 400)
            return
        if action == "render":
            self._json({"ok": True, **{k: v for k, v in rendered.items() if k != "deploy_script"},
                        "deployable": bool(rendered.get("deploy_script"))})
            return
        if action != "deploy":
            self._json({"error": "unknown action"}, 400)
            return
        script = rendered.get("deploy_script")
        host = str(rendered["params"].get("host") or "").strip()
        if not script or not host:
            self._json({"error": "this recipe is manual (no ssh target) — run the printed steps yourself"}, 400)
            return
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "bash -c " + _shlex.quote(script)]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")      # the stream has no length: closing is how the client learns it ended
        self.end_headers()
        self.close_connection = True
        wlock = threading.Lock()
        stop = threading.Event()

        def emit(obj):
            try:
                with wlock:
                    self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                    self.wfile.flush()
            except OSError:
                stop.set()
        emit({"phase": "start", "host": host, "recipe": body.get("id")})
        captured = []

        def on_line(text):
            if len(captured) < 2000:
                captured.append(text)
            emit({"line": text})
        try:
            code, killed = agentmod.run_streaming(cmd, ROOT, on_line, stop, timeout_s=3600)
        except Exception as e:   # noqa: BLE001
            emit({"error": str(e)})
            code, killed = -1, "error"
        emit({"phase": "done", "exit": code, "killed": killed})
        TRACE.log("recipe", action="deploy", id=body.get("id"), host=host, params=rendered["params"],
                  exit=code, killed=killed or False, output=captured[-200:])

    def _skill_admin(self, body):
        """Create or delete a skill from the UI. The file is written in the
        console's user skills dir and mirrored to the agent worker, so the
        Sultan can attach it on the next run."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action") or "create"
        name = str(body.get("name") or "").strip().lower()
        if not skillmod.NAME_RE.match(name):
            self._json({"error": "name must be kebab-case: letters, digits, dashes"}, 400)
            return
        if action == "delete":
            sk = skill_catalog().get(name)
            if not sk:
                self._json({"error": "no such skill"}, 404)
                return
            if sk.source.startswith("plugin:"):
                self._json({"error": "that skill belongs to a plugin; disable the plugin instead"}, 400)
                return
            try:
                os.remove(sk.path)
                d = os.path.dirname(sk.path)
                if os.path.basename(sk.path) == "SKILL.md" and not os.listdir(d):
                    os.rmdir(d)
            except OSError as e:
                self._json({"error": "could not delete: %s" % e}, 500)
                return
            worker = agentmod.remove_skill_remote(CFG, name)
            TRACE.log("skill", action="delete", name=name, worker=worker)
            self._json({"ok": True, "worker": worker, "skills": skill_catalog().summaries()})
            return
        if action != "create":
            self._json({"error": "unknown action"}, 400)
            return
        description = str(body.get("description") or "").strip()
        skill_body = str(body.get("body") or "").strip()
        if not description or not skill_body:
            self._json({"error": "description and instructions are required"}, 400)
            return
        tools = body.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        meta = {"name": name, "description": description[:skillmod.MAX_DESC],
                "whenToUse": str(body.get("whenToUse") or "").strip(),
                "tools": [str(t) for t in tools][:20], "network": bool(body.get("network")),
                "model": str(body.get("model") or "").strip()}
        text = render_skill_md(meta, skill_body)
        root = user_skills_dir()
        target = os.path.join(root, name, "SKILL.md")
        if (os.path.exists(target) or os.path.exists(os.path.join(root, name + ".md"))) and not body.get("overwrite"):
            self._json({"error": "a skill named %s already exists; tick overwrite to replace it" % name}, 409)
            return
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            self._json({"error": "could not write: %s" % e}, 500)
            return
        worker = agentmod.push_skill(CFG, name, text)
        TRACE.log("skill", action="create", name=name, path=target, worker=worker)
        self._json({"ok": True, "path": target, "worker": worker,
                    "skills": skill_catalog().summaries()})

    def _plugin_admin(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action")
        name = (body.get("name") or "").strip()
        found, warnings = discover_plugins()
        if action in ("add", "create", "remove"):
            err = self._plugin_install(action, body, found)
            if err:
                self._json({"error": err}, 400)
                return
            found, warnings = discover_plugins()
        elif action in ("enable", "disable"):
            if name not in found:
                self._json({"error": "no such plugin"}, 404)
                return
            _plugin_state().setdefault(name, {})["enabled"] = (action == "enable")
            save_config()
            TRACE.log("plugin", action=action, name=name,
                      contributes=found[name].info(action == "enable"))
            mcp_reload()   # a plugin's MCP servers come or go with it
        elif action != "rescan":
            self._json({"error": "unknown action"}, 400)
            return
        st = _plugin_state()
        self._json({"ok": True, "plugins": [found[n].info(bool(st.get(n, {}).get("enabled")))
                                            for n in sorted(found)], "warnings": warnings})

    def _plugin_install(self, action, body, found):
        """add: git URL or local path into the plugins dir; create: a new
        empty plugin (manifest + optional MCP servers); remove: delete one the
        console installed. Returns an error string or None."""
        import shutil, subprocess
        root = plugins_install_dir()
        if action == "remove":
            name = (body.get("name") or "").strip()
            p = found.get(name)
            if not p:
                return "no such plugin"
            if os.path.dirname(os.path.abspath(p.root)) != os.path.abspath(root):
                return "that plugin was not installed by the console; remove it on disk"
            if os.path.islink(p.root):
                os.unlink(p.root)
            else:
                shutil.rmtree(p.root, ignore_errors=True)
            _plugin_state().pop(name, None)
            save_config()
            TRACE.log("plugin", action="remove", name=name)
            mcp_reload()
            return None
        if action == "create":
            name = str(body.get("name") or "").strip().lower()
            if not skillmod.NAME_RE.match(name):
                return "name must be kebab-case"
            dest = os.path.join(root, name)
            if os.path.exists(dest):
                return "a plugin named %s already exists" % name
            servers = body.get("mcp_servers") or {}
            if isinstance(servers, str):
                try:
                    servers = json.loads(servers) if servers.strip() else {}
                except ValueError:
                    return "MCP servers must be a JSON object"
            if not isinstance(servers, dict):
                return "MCP servers must be a JSON object"
            os.makedirs(os.path.join(dest, "skills"), exist_ok=True)
            manifest = {"name": name, "description": str(body.get("description") or "").strip(),
                        "version": "0.1.0", "skills": "skills"}
            if servers:
                manifest["mcp_servers"] = servers
            with open(os.path.join(dest, "plugin.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            TRACE.log("plugin", action="create", name=name)
            return None
        # add
        source = str(body.get("source") or "").strip()
        if not source:
            return "source is required: a git URL or a local path"
        name = str(body.get("name") or "").strip().lower() or \
            re.sub(r"\.git$", "", source.rstrip("/").rsplit("/", 1)[-1]).lower()
        name = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
        if not skillmod.NAME_RE.match(name):
            return "could not derive a plugin name from the source; give one"
        dest = os.path.join(root, name)
        if os.path.exists(dest):
            return "a plugin named %s already exists" % name
        if source.startswith(("http://", "https://", "git@", "ssh://")):
            try:
                r = subprocess.run(["git", "clone", "--depth", "1", source, dest],
                                   capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as e:
                return "git clone failed: %s" % e
            if r.returncode != 0:
                shutil.rmtree(dest, ignore_errors=True)
                return "git clone failed: " + (r.stderr or "").strip()[-300:]
        else:
            src = os.path.abspath(os.path.expanduser(source))
            if not os.path.isdir(src):
                return "no such directory: " + source
            os.symlink(src, dest)
        if skillmod._plugin_manifest(dest) is None:
            if os.path.islink(dest):
                os.unlink(dest)
            else:
                shutil.rmtree(dest, ignore_errors=True)
            return "not a plugin: no plugin.json (or .claude-plugin/plugin.json) and no skills/ dir"
        TRACE.log("plugin", action="add", name=name, source=source)
        return None

    def _mcp_admin(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action")
        servers = CFG.setdefault("mcp_servers", {})
        name = (body.get("name") or "").strip()

        if action == "install":
            self._mcp_install(body)
            return
        if action == "catalog_add":
            entry = catmod.get(str(body.get("id") or ""))
            if not entry:
                self._json({"error": "no such catalog entry"}, 404)
                return
            name = name or entry["id"]
            if not re.match(r"^[A-Za-z0-9_-]{1,32}$", name):
                self._json({"error": "name must be 1-32 chars: letters, digits, - or _"}, 400)
                return
            cmd, args = catmod.render(entry, body.get("params") or {})
            env = {k: str(v) for k, v in (body.get("env") or {}).items() if str(v).strip()}
            for e in entry.get("env", []):
                if e.get("default") and e["name"] not in env:
                    env[e["name"]] = os.path.expanduser(e["default"]) if str(e["default"]).startswith("~") else e["default"]
            servers[name] = {"command": cmd, "args": args, "env": env, "enabled": True, "catalog": entry["id"]}
            TRACE.log("mcp", action="catalog_add", name=name, catalog=entry["id"])
            action = "restart"
        elif action == "set_env":
            if name not in servers:
                self._json({"error": "no such server"}, 404)
                return
            env = servers[name].setdefault("env", {})
            for k, v in (body.get("env") or {}).items():
                if str(v).strip():
                    env[str(k)] = str(v)
                else:
                    env.pop(str(k), None)
            action = "restart"

        if action == "add":
            if not name or not re.match(r"^[A-Za-z0-9_-]{1,32}$", name):
                self._json({"error": "name must be 1-32 chars: letters, digits, - or _"}, 400)
                return
            cmd = (body.get("command") or "").strip()
            if not cmd:
                self._json({"error": "command is required"}, 400)
                return
            args = body.get("args")
            if isinstance(args, str):
                args = [a for a in args.split() if a]
            servers[name] = {
                "command": cmd,
                "args": list(args or []),
                "env": body.get("env") or {},
                "enabled": True,
            }
        elif action == "remove":
            if name not in servers:
                self._json({"error": "no such server"}, 404)
                return
            servers.pop(name)
        elif action == "toggle":
            if name not in servers:
                self._json({"error": "no such server"}, 404)
                return
            servers[name]["enabled"] = not servers[name].get("enabled", True)
        elif action == "restart":
            pass  # config unchanged; the reload below does the work
        else:
            self._json({"error": "unknown action"}, 400)
            return

        save_config()
        h = mcp_reload()
        self._json({"ok": True, "servers": h.status,
                    "config": CFG.get("mcp_servers", {})})

    def _mcp_install(self, body):
        """Pre-install a catalog server on this host (npm -g / uv tool /
        browser downloads), streaming the output, then add it to the config
        and start it. The model never sees a half-installed server: the add
        happens only when every install step exited 0."""
        entry = catmod.get(str(body.get("id") or ""))
        if not entry:
            self._json({"error": "no such catalog entry"}, 404)
            return
        name = (body.get("name") or entry["id"]).strip()
        if not re.match(r"^[A-Za-z0-9_-]{1,32}$", name):
            self._json({"error": "bad name"}, 400)
            return
        cmd, args = catmod.render(entry, body.get("params") or {})
        env = {k: str(v) for k, v in (body.get("env") or {}).items() if str(v).strip()}
        for e in entry.get("env", []):
            if e.get("default") and e["name"] not in env:
                env[e["name"]] = os.path.expanduser(e["default"]) if str(e["default"]).startswith("~") else e["default"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")      # the stream has no length: closing is how the client learns it ended
        self.end_headers()
        self.close_connection = True
        wlock = threading.Lock()
        stop = threading.Event()

        def emit(obj):
            try:
                with wlock:
                    self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                    self.wfile.flush()
            except OSError:
                stop.set()
        shell_env = catmod.shell_env()
        emit({"phase": "start", "id": entry["id"], "name": name, "steps": entry.get("install", [])})
        code = 0
        for step in entry.get("install", []):
            emit({"line": "$ " + step})
            wrapped = ["env", "PATH=" + shell_env["PATH"], "bash", "-c", step]
            try:
                code, killed = agentmod.run_streaming(wrapped, ROOT, lambda t: emit({"line": t}), stop, timeout_s=1800)
            except Exception as e:   # noqa: BLE001
                emit({"error": str(e)}); code = -1; killed = "error"
            if code != 0:
                emit({"phase": "done", "exit": code, "killed": killed, "added": False})
                TRACE.log("mcp", action="install", name=name, catalog=entry["id"], exit=code, step=step)
                return
        servers = CFG.setdefault("mcp_servers", {})
        servers[name] = {"command": cmd, "args": args, "env": env, "enabled": True, "catalog": entry["id"]}
        save_config()
        h = mcp_reload()
        st = h.status.get(name) or {}
        emit({"line": "added %s: %s %s" % (name, cmd, " ".join(args))})
        emit({"line": "server %s: %s" % (name, (str(st.get("tools")) + " tools") if st.get("state") == "ready" else st.get("state", "?") + (" — " + st["error"] if st.get("error") else ""))})
        emit({"phase": "done", "exit": 0, "killed": False, "added": True, "status": st})
        TRACE.log("mcp", action="install", name=name, catalog=entry["id"], exit=0, status=st)

    # ---- streaming chat proxy ----
    RETRY_STRIP = ("reasoning_effort", "top_k", "repetition_penalty", "stream_options")

    def _chat(self, payload):
        if not isinstance(payload, dict):
            self._json({"error": "expected object"}, 400)
            return
        payload["stream"] = True
        payload.setdefault("stream_options", {"include_usage": True})
        # who this request belongs to, for the trace — headers, so nothing
        # extra travels upstream
        tid = TRACE.new_id()
        who = {"id": tid, "session": self.headers.get("X-BB-Session"),
               "turn": self.headers.get("X-BB-Turn"),
               "purpose": self.headers.get("X-BB-Purpose") or "chat"}
        started = time.time()

        def attempt(p):
            req = upstream_request("/chat/completions", p, "POST")
            return urllib.request.urlopen(req, timeout=600)

        try:
            try:
                resp = attempt(payload)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                # Pure-OpenAI upstreams reject vLLM extras; strip and retry once.
                if e.code == 400 and any(k in detail for k in self.RETRY_STRIP):
                    for k in self.RETRY_STRIP:
                        payload.pop(k, None)
                    resp = attempt(payload)
                else:
                    msg = upstream_message(detail)[:1000]
                    TRACE.log("chat", status=e.code, request=trace_safe(payload), response=None, error=msg,
                              ms=int((time.time() - started) * 1000), **who)
                    self._json({"error": msg}, e.code)
                    return
        except Exception as e:
            TRACE.log("chat", status=502, request=trace_safe(payload), response=None, error=str(e)[:500],
                      ms=int((time.time() - started) * 1000), **who)
            self._json({"error": str(e)}, 502)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-BB-Trace", tid)
        self.close_connection = True
        self.end_headers()
        cap = StreamCapture()
        # Forward raw bytes as they arrive — the client parses SSE framing.
        # (A readline-per-event loop holds each event's terminating blank line
        # hostage until the NEXT event arrives: the stream renders one token
        # late, permanently.) read1 returns whatever the socket has.
        try:
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    cap.feed(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                cap.error = cap.error or "client disconnected"  # user hit Stop, or left
            except Exception as e:
                # upstream died mid-stream: without this, the client sees a
                # clean EOF and silently renders a truncated reply as complete
                cap.error = "upstream stream failed: " + str(e)[:200]
                try:
                    msg = json.dumps({"error": cap.error})
                    self.wfile.write(("data: " + msg + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    pass
        finally:
            resp.close()
            TRACE.log("chat", status=200, request=trace_safe(payload), response=cap.result(started),
                      ms=int((time.time() - started) * 1000), **who)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=CFG.get("port", 8765))
    ap.add_argument("--bind", default=CFG.get("bind", "127.0.0.1"))
    args = ap.parse_args()
    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        # There is no auth on any route. Loopback-only is the security model;
        # a wider bind turns the rack into an open inference gateway for the
        # whole LAN. Refuse rather than warn — reach it over SSH or the overlay.
        raise SystemExit(
            "refusing to bind %s: the console has no auth and is loopback-only "
            "by design. Reach it via SSH tunnel or overlay network." % args.bind)
    CFG["bind"] = args.bind
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    start_scheduler()
    print("ByteBunker Console on http://%s:%d  (upstream %s)  jobs: %d" %
          (args.bind, args.port, CFG.get("upstream_url"), len(JOBS.jobs)))
    srv.serve_forever()


if __name__ == "__main__":
    main()
