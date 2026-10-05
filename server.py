#!/usr/bin/env python3
"""ByteBunker Console — one-file server. Stdlib only, Python 3.9+.

Successor to chatserve: serves the console UI and provides the small API the
UI needs. Nothing leaves the rack — the only outbound calls are to the
OpenAI-compatible gateways you configure and, optionally, the rack monitors
the Cluster screen reads (monitors.py).

  python3 server.py                 # reads config.json next to this file
  python3 server.py --port 8765

API:
  GET  /                     the console
  GET  /api/config           UI-facing config (node names, identity, rates)
  GET  /api/models           proxied upstream /v1/models
  POST /api/chat             proxied streaming /v1/chat/completions (SSE)
  GET  /api/cluster          every node from every rack monitor (?history=N samples)
  GET  /api/monitors         configured monitors  |  POST add/update/toggle/remove/test/discover
  GET  /api/sessions         session summaries, newest first; ?id= one session in full
                             |  POST save  |  DELETE ?id=
  GET  /api/events           every event as SSE (?topics=a,b&after=N, or Last-Event-ID)
  GET  /api/runs             runs, newest first (?kind=)  |  /api/runs/<id>/events  SSE replay + live
  POST /api/runs/<id>/cancel
  GET  /api/hello            {service, version, pid}: how bb and the app find this server
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
import getpass
import json
import platform
import sys
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
import gateways as gwmod
import monitors as monmod
import instance as instancemod
import events as eventsmod
import runs as runsmod
import sessions as sessmod
import upstream as upstreammod
from version import VERSION

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
# The code directory is read-only inside a packaged app, so data and config
# live wherever the launcher says (desktop/app.py sets these per OS). The
# classic install keeps both beside the code.
DATA = os.path.abspath(os.path.expanduser(os.environ.get("BYTEBUNKER_DATA") or os.path.join(ROOT, "data")))
CONFIG_PATH = os.path.abspath(os.path.expanduser(os.environ.get("BYTEBUNKER_CONFIG") or os.path.join(ROOT, "config.json")))
os.makedirs(DATA, exist_ok=True)
# Every model request, tool run, rating and archive, append-only, forever:
# data/traces/<day>.jsonl (gzipped after the day ends). See traces.py.
TRACE = TraceLog(DATA)
# One ordered stream of everything that happens (GET /api/events), the runs
# that outlive the request that started them, and one file per session.
BUS = eventsmod.EventBus(runs_dir=os.path.join(DATA, "runs"))
RUNS = runsmod.RunRegistry(BUS, os.path.join(DATA, "runs", "index.jsonl"))
SESSIONS = sessmod.SessionStore(os.path.join(DATA, "sessions"), legacy_path=os.path.join(DATA, "sessions.json"))

DEFAULT_CONFIG = {
    "bind": "127.0.0.1",
    "port": 8765,
    "upstream_url": "http://127.0.0.1:8000/v1",
    "upstream_key": "bb-local",
    # rack monitors for the Cluster screen: [{"name", "url", "token", "enabled"}]
    "monitors": [],
    # optional, legacy: a Prometheus that scrapes vLLM, asked only when no
    # monitor reports an engine (the playground's "engine is busy" hint)
    "prometheus_url": "",
    "h3_url": "",
    "netcheck_ssh": "",
    "identity": None,           # who and where, shown in the sidebar: this machine's user and name
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
    # GLM-5.3-Flash on the two-Spark rack: served at 131072 (fp8 KV + MTP,
    # 2026-10-05). Thinking model; tools yes; prior <think> stripped on resend.
    "glm": {"ctx": 131072},
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


def _default_identity():
    try:
        user = getpass.getuser()
    except Exception:   # noqa: BLE001 - no USER and no passwd entry
        user = "you"
    return {"user": user, "host": (platform.node() or "this machine").split(".")[0]}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg["identity"] = _default_identity()
    path = CONFIG_PATH
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()
_LOCK = threading.Lock()
# A console used to have one upstream. Now it has gateways: litellm in front
# of the Sparks, a vLLM on a workstation GPU, Ollama on a laptop — merged
# into one model list and routed per request by the model's name.
GW = gwmod.Registry(CFG, caps_for)
# The Cluster screen's source: rack monitors (`rack monitor up` on a head
# node), each one URL + one token for every node behind it.
MON = monmod.Monitors(CFG)

# Skills live in the repo's skills/, in any dir listed in config's skills_dirs
# (point one at the harness's skills/ to share them), and inside enabled
# plugins. Plugins live in the repo's plugins/ and in config's plugins_dirs.
BUILTIN_SKILLS = os.path.join(ROOT, "skills")
BUILTIN_PLUGINS = os.path.join(ROOT, "plugins")


def _plugin_state():
    return CFG.setdefault("plugins", {})


# What the user adds lands in the data folder unless config names a writable
# folder: never in the app's own skills/ and plugins/, which are read-only
# inside the app and replaced by every update.
USER_SKILLS = os.path.join(DATA, "skills")
USER_PLUGINS = os.path.join(DATA, "plugins")


def _with_user_dir(dirs, user_dir):
    seen = {os.path.abspath(os.path.expanduser(d)) for d in dirs}
    return list(dirs) + ([user_dir] if os.path.abspath(user_dir) not in seen else [])


def discover_plugins():
    dirs = [BUILTIN_PLUGINS] + _with_user_dir(CFG.get("plugins_dirs") or [], USER_PLUGINS)
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
    for d in _with_user_dir(CFG.get("skills_dirs") or [], USER_SKILLS):
        roots.append((d, d))
    roots.append((BUILTIN_SKILLS, "built-in"))
    return roots


def skill_catalog():
    return skillmod.load_catalog(skill_roots())


def user_skills_dir():
    """Where a skill created in the UI is written: the first writable
    configured skills dir (on a deployed console that is the harness
    mirror), else <data>/skills."""
    for d in (CFG.get("skills_dirs") or []):
        d = os.path.expanduser(d)
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    os.makedirs(USER_SKILLS, exist_ok=True)
    return USER_SKILLS


def plugins_install_dir():
    for d in (CFG.get("plugins_dirs") or []):
        d = os.path.expanduser(d)
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    os.makedirs(USER_PLUGINS, exist_ok=True)
    return USER_PLUGINS


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


trace_safe = upstreammod.trace_safe         # image data URLs logged as their size


# ------------------------------------------------------------------ jobs --
# Scheduled work: the console wakes up and has the model do a task, with the
# same tools, skills and model caps a chat turn would use, or hands a goal
# to the agent harness. The scheduler thread starts with the server.
JOBS = jobmod.JobStore(DATA)
SCHED = None


def last_used_model():
    """The most recent model in the usage ledger that a gateway still lists."""
    known = {m["id"] for m in GW.models}
    try:
        with open(os.path.join(DATA, "usage.jsonl"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for ln in reversed(lines):
        try:
            m = str(json.loads(ln).get("model") or "")
        except ValueError:
            continue
        if m in known:
            return m
    return ""


AGENT_SLOT = threading.Lock()   # one goal at a time: the harness keys a goal's state by its directory


def agent_goal(run, emit, goal, via="app"):
    """One goal on the agents worker, its lines streamed through emit. Shared
    by the Agents screen and agent-kind jobs. Returns (result, lines)."""
    cmd = agentmod.build_command(CFG, goal)              # AgentConfigError when not set up
    st = agentmod.status(CFG)
    cwd = None if st["mode"] == "ssh" else os.path.expanduser((CFG.get("agents") or {}).get("dir") or ".")
    if not AGENT_SLOT.acquire(blocking=False):
        msg = "another agents goal is running; they run one at a time"
        emit({"error": msg})
        emit({"phase": "done", "exit": None, "killed": "busy"})
        return {"ok": False, "error": msg}, []
    started = time.time()
    captured = []   # full master output, stored so a past run can be reopened
    try:
        emit({"phase": "start", "id": run.id, "host": st["host"], "mode": st["mode"], "isolated": st["isolated"]})

        def on_line(text):
            if text.startswith("BB_RUN "):
                # the harness names the goal it started (its directory on the worker)
                try:
                    run.meta["goal_id"] = str(json.loads(text[7:]).get("goal_id") or "")[:80]
                except (ValueError, AttributeError):
                    pass
                return
            if len(captured) < 4000:
                captured.append(text)
            # The master files recurring work by printing one structured line;
            # the agent plane has no other way to reach the console, by design.
            if text.startswith("BB_JOB "):
                try:
                    spec = json.loads(text[7:])
                    spec["created_by"] = "agents:" + str(spec.get("created_by") or "sultan")[:30]
                    job = JOBS.upsert(spec)
                    TRACE.log("job", action="filed_by_agent", id=job["id"], name=job["name"], run=run.id,
                              schedule=job["schedule"], job_kind=job["kind"], by=spec["created_by"])
                    BUS.publish("jobs", "saved", {"id": job["id"], "by": spec["created_by"]})
                    emit({"job": {"id": job["id"], "name": job["name"], "schedule": job["schedule"], "kind": job["kind"]}})
                    emit({"line": "%s> \U0001f5d3 filed job \"%s\" (%s) \u2014 see the Jobs screen" % (
                        (CFG.get("agents") or {}).get("master_name") or "Master", job["name"], jobmod.describe_schedule(job))})
                except (ValueError, KeyError) as e:
                    emit({"line": "console: could not file the job the master asked for: %s" % str(e)[:160]})
                return
            emit({"line": text})

        try:
            code, killed = agentmod.run_streaming(cmd, cwd, on_line, run.cancel_event,
                                                  timeout_s=agent_run_timeout(CFG))
        except FileNotFoundError as e:
            emit({"error": "could not launch the harness: %s" % e})
            code, killed = -1, "error"
        except Exception as e:   # noqa: BLE001 - report anything to the client
            emit({"error": str(e)[:300]})
            code, killed = -1, "error"
        emit({"phase": "done", "exit": code, "killed": killed})
        TRACE.log("agent_run", id=run.id, goal=goal[:8000], host=st["host"], mode=st["mode"],
                  isolated=st["isolated"], exit=code, killed=killed or False, via=via,
                  lines=len(captured), output=captured, ms=int((time.time() - started) * 1000))
        return ({"ok": code == 0, "exit": code, "killed": killed or False, "lines": len(captured),
                 "error": None if code == 0 else "the harness exited %s%s" % (code, " (%s)" % killed if killed else "")},
                captured)
    finally:
        AGENT_SLOT.release()


def run_job(job, run):
    """One run of a job, inside its run. chat: a bounded tool loop against the
    gateway, like the Playground does but server-side. agent: a harness goal.
    Returns {output, hops, tokens}; raises when the job failed."""
    emit = lambda obj: RUNS.emit(run, "out", obj)   # noqa: E731
    if job.get("kind") == "agent":
        if not agentmod.status(CFG).get("enabled"):
            raise RuntimeError("agents are not set up: connect a worker on the Agents screen")
        res, lines = agent_goal(run, emit, job["prompt"], via="job:" + job["id"])
        if not res.get("ok"):
            raise RuntimeError((res.get("error") or "failed") + ("\n" + "\n".join(lines[-20:]) if lines else ""))
        return {"output": "\n".join(lines), "hops": None, "tokens": None}

    model = job.get("model") or ""
    if not model:
        # a gateway's list is what it is configured for, not what is up (litellm
        # lists every backend, served or not): the model you last used is the
        # best guess, then the first one listed
        GW.refresh()
        model = last_used_model() or (GW.models[0]["id"] if GW.models else "")
    if not model:
        raise RuntimeError("no model configured for the job and no gateway offers one")
    model, job_gw = GW.resolve(model)
    if job_gw is None:
        raise RuntimeError("no gateway serves %s" % model)
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
    emit({"line": "%s on %s%s" % (model, job_gw["name"], " \u00b7 %d tools" % len(tools) if tools else "")})
    total_tokens = 0
    hops = 0
    for hop in range(max(1, int(job.get("max_hops") or 12))):
        if run.cancelled:
            raise RuntimeError("cancelled")
        payload = {"model": model, "messages": msgs, "max_tokens": 8000, "temperature": 0.3}
        if caps.get("ctk"):
            payload["chat_template_kwargs"] = dict(caps["ctk"])
        if tools:
            payload["tools"] = tools
        req = upstream_request("/chat/completions", payload, "POST", gateway=job_gw)
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=900) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        TRACE.log("chat", status=200, request=trace_safe(payload), response=resp, purpose="job", job=job["id"],
                  run=run.id, ms=int((time.time() - t0) * 1000))
        usage = resp.get("usage") or {}
        total_tokens += int(usage.get("total_tokens") or 0)
        if usage.get("completion_tokens"):
            # the Usage screen counts every turn, the Playground's and the jobs'
            append_usage({"model": model, "gateway": job_gw["name"], "source": "job", "job": job["id"],
                          "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                          "completion_tokens": int(usage.get("completion_tokens") or 0),
                          "ttft_s": None, "decode_tok_s": None, "estimated": False})
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = msg.get("tool_calls") or []
        hops += 1
        if not calls:
            return {"output": msg.get("content") or "", "hops": hops, "tokens": total_tokens,
                    "gateway": job_gw["name"], "model": model}
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
            emit({"line": "hop %d: %s" % (hops, fn.get("name") or "?")})
            text, is_err = mcp_host().call(fn.get("name") or "", args)
            TRACE.log("tool", name=fn.get("name"), args=args, result=str(text)[:4000], is_error=bool(is_err), purpose="job",
                      job=job["id"], run=run.id)
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"), "content": str(text)[:20000]})
    return {"output": "(stopped after %d tool hops without a final answer)" % hops, "hops": hops, "tokens": total_tokens}


_JOB_SLOTS = None


def job_slots():
    """Jobs share the engine with everything else: one at a time unless
    config says otherwise (jobs_parallel, 1..8). A job waits for a slot,
    queued; it is never skipped for want of one."""
    global _JOB_SLOTS
    if _JOB_SLOTS is None:
        _JOB_SLOTS = threading.Semaphore(max(1, min(8, _int(CFG.get("jobs_parallel"), 1))))
    return _JOB_SLOTS


def launch_job(job, trigger):
    """Start one run of a job and return it at once. A job whose last run is
    still going is not started again: runsmod.Busy."""
    def target(run):
        RUNS.set_state(run, "queued")
        slots = job_slots()
        while not slots.acquire(timeout=1):
            if run.cancelled:
                return {"ok": False, "error": "cancelled before it started"}
        try:
            RUNS.set_state(run, "running")
            t0 = time.time()
            BUS.publish("jobs", "started", {"id": job["id"], "run": run.id, "trigger": trigger})
            rec = {"ts": t0, "trigger": trigger, "kind": job["kind"], "ok": False, "ms": 0, "output": "",
                   "error": None, "run": run.id}
            try:
                out = run_job(job, run)
                rec.update(ok=True, output=str(out.get("output") or "")[:40000], hops=out.get("hops"),
                           tokens=out.get("tokens"), model=out.get("model"), gateway=out.get("gateway"))
            except Exception as e:   # noqa: BLE001 - a job's failure is its record, not the server's
                rec["error"] = str(e)[:1000]
            rec["ms"] = int((time.time() - t0) * 1000)
            JOBS.record_run(job["id"], rec)
            BUS.publish("jobs", "finished", {"id": job["id"], "run": run.id, "ok": rec["ok"]})
            return {"ok": rec["ok"], "error": (rec["error"] or "")[:300] or None, "ms": rec["ms"],
                    "tokens": rec.get("tokens")}
        finally:
            slots.release()
    return RUNS.start("job", trigger, target, title=job["name"], meta={"job": job["id"], "trigger": trigger},
                      key="job:" + job["id"])


def running_jobs():
    """{job id: {"run", "state"}} for jobs with a run going."""
    return {r.meta.get("job"): {"run": r.id, "state": r.state} for r in RUNS.active("job")}


def start_scheduler():
    global SCHED
    if SCHED is None:
        SCHED = jobmod.Scheduler(JOBS, launch_job, log=print)
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
    """The MCP host, created on first use. Servers start in the background:
    a request never waits for every server to boot, it sees the ones that
    are ready and "starting" for the rest."""
    global _MCP
    with _MCP_LOCK:
        if _MCP is None:
            from mcp import MCPHost
            _MCP = MCPHost()
            _MCP.sync_in_background(effective_mcp_servers())
        return _MCP


def mcp_reload(restart=(), wait=True):
    """Apply the current config to the running servers. Only what changed is
    touched: an edited or re-enabled server restarts, a removed or disabled
    one stops, the rest keep running with their calls in flight. `restart`
    names servers to restart even when unchanged."""
    h = mcp_host()
    if wait:
        h.sync(effective_mcp_servers(), restart)
    else:
        h.sync_in_background(effective_mcp_servers(), restart)
    return h


def save_config():
    """Persist CFG back to config.json, preserving formatting sanity. Written
    atomically so a crash mid-write cannot leave the console unbootable."""
    path = CONFIG_PATH
    tmp = path + ".tmp"
    with _LOCK:
        with open(tmp, "w") as f:
            json.dump(CFG, f, indent=2)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)      # gateway keys live here; a replace must not widen it
        except OSError:
            pass
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


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- sessions --
def read_sessions():
    """Every session in full, newest first (export only: it reads every file)."""
    return SESSIONS.all()


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
    by_gateway = {}
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
                base, _, pin = m.rpartition("@")
                if base and GW.gateway(pin):
                    m = base          # "id@gateway" is a route, not another model
                by_model[m] = by_model.get(m, 0) + out
                gname = str(e.get("gateway") or GW.model_map.get(m) or "upstream")
                by_gateway[gname] = by_gateway.get(gname, 0) + out + inn
                if dec and not e.get("estimated"):
                    tps.append(dec)
    except FileNotFoundError:
        pass
    tps.sort()
    rates = CFG.get("frontier_rates_per_mtok", {})
    saved = (tot_in / 1e6) * float(rates.get("input", 0)) + \
            (tot_out / 1e6) * float(rates.get("output", 0))
    # the agent plane's tokens live on the worker, not in this ledger; they
    # arrive later (an event) when the worker is slow or away
    agents = agentmod.agent_usage(CFG, on_update=lambda: BUS.publish("usage", "agents"))
    agents_saved = 0.0
    if agents and not agents.get("error") and not agents.get("pending"):
        a_in = agents.get("slave_prompt", 0) + agents.get("master_prompt", 0)
        a_out = agents.get("slave_completion", 0) + agents.get("master_completion", 0)
        # older spawn records carry only a total: price it as input (conservative)
        unsplit = max(0, agents.get("slave_tokens", 0) - agents.get("slave_prompt", 0) - agents.get("slave_completion", 0))
        a_in += unsplit + agents.get("panel_tokens", 0)
        agents_saved = (a_in / 1e6) * float(rates.get("input", 0)) + (a_out / 1e6) * float(rates.get("output", 0))
        agents["total"] = agents.get("slave_tokens", 0) + agents.get("master_prompt", 0) + agents.get("master_completion", 0) + agents.get("panel_tokens", 0)
        agents["frontier_saved_usd"] = round(agents_saved, 2)
        # the agents name models; the gateway that serves each one is known here
        agw = {}
        for m, n in (agents.get("by_model") or {}).items():
            gname = GW.model_map.get(m) or "unknown"
            agw[gname] = agw.get(gname, 0) + int(n or 0)
        agents["by_gateway"] = agw
        for gname, n in agw.items():
            by_gateway[gname] = by_gateway.get(gname, 0) + n
    return {
        "total_out": tot_out,
        "median_tok_s": tps[len(tps) // 2] if tps else None,
        "requests": sum(1 for _ in tps) or None,
        "frontier_saved_usd": round(saved + agents_saved, 2),
        "chat_saved_usd": round(saved, 2),
        "days": days,
        "by_model": by_model,
        "by_gateway": by_gateway,
        "agents": agents,
    }


# ------------------------------------------------------------ engine stats --
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
    the client, so the console asks the engines themselves: the rack
    monitors read every engine's /metrics. A Prometheus that scrapes vLLM is
    the fallback. (Colons are legal in Prometheus metric names but not in
    PromQL bare selectors, hence the __name__ form below.)"""
    if MON.list():
        st = MON.engine_stats()
        if st.get("ok") or not CFG.get("prometheus_url"):
            return st
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


# ---------------------------------------------------------------- upstream --
def upstream_request(path, payload=None, method="GET", model=None, gateway=None):
    """A request to the gateway that serves the model (see upstream.request)."""
    return upstreammod.request(GW, path, payload, method, model, gateway)


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
        root = os.path.realpath(PUBLIC)
        fs = os.path.realpath(os.path.join(PUBLIC, path.lstrip("/")))
        if os.path.commonpath([fs, root]) != root or not os.path.isfile(fs):
            self._json({"error": "not found"}, 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css",
            ".js": "text/javascript",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
            ".txt": "text/plain; charset=utf-8",
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

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b"retry: 3000\n\n")
        self.wfile.flush()

    def _events(self, q):
        """GET /api/events?topics=a,b&after=N  (or Last-Event-ID): the event
        stream as SSE. Resumes after a dropped connection; a client that fell
        behind the ring gets a "reset" event and should reload its state."""
        topics = set(t for t in ",".join(q.get("topics") or []).split(",") if t) or None
        # a reconnecting browser sends Last-Event-ID; it wins over the URL's after=
        pos = _int(self.headers.get("Last-Event-ID") or (q.get("after") or [""])[0], BUS.seq)
        self._sse_start()
        try:
            while True:
                evts, gap, head = BUS.scan(pos, topics)
                if gap:
                    self.wfile.write(b"id: %d\nevent: reset\ndata: {}\n\n" % head)
                    evts = []
                for e in evts:
                    self.wfile.write(eventsmod.sse_frame(e))
                pos = head
                self.wfile.flush()
                if STOPPING.is_set():
                    return
                if not BUS.wait(pos, 15):
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except OSError:
            return

    def _stream_run(self, rid, after=0, legacy=False, cancel_on_leave=False):
        """A run as SSE: everything from its start (or after N) from its file,
        then live until it finishes. Any client can attach, any time.
        legacy: the data: frames the app's screens read ({"line"}, {"phase"},
        {"error"}), for the routes that start a run and stream it in one
        request. cancel_on_leave: the run ends with this viewer (a log tail)
        instead of going on without it (a deploy, a goal)."""
        if not re.match(r"^[A-Za-z0-9_-]{1,80}$", rid or ""):
            self._json({"error": "bad run id"}, 400)
            return
        run = RUNS.get(rid)
        past = BUS.run_events(rid, after)
        if run is None and not past:
            self._json({"error": "no such run"}, 404)
            return
        self._sse_start()
        saw_done = [False]

        def frames(e):
            if not legacy:
                return [eventsmod.sse_frame(e)]
            d = e.get("data") or {}
            if e["topic"] == "output" and e["type"] == "out":
                saw_done[0] = saw_done[0] or d.get("phase") == "done"
                objs = [d]
            elif e["topic"] == "runs" and e["type"] == "started":
                objs = [{"run": rid, "kind": d.get("kind")}]
            elif e["topic"] == "runs" and e["type"] == "finished" and not saw_done[0]:
                # the work raised before reporting its own end
                objs = ([{"error": d["error"]}] if d.get("error") else []) + [
                    {"phase": "done", "exit": None, "killed": False if d.get("state") == "done" else d.get("state")}]
            else:
                objs = []
            return [("data: " + json.dumps(o) + "\n\n").encode() for o in objs]

        def send(evts, sent):
            for e in evts:
                if e["seq"] > sent:
                    for f in frames(e):
                        self.wfile.write(f)
                    sent = e["seq"]
            self.wfile.flush()
            return sent

        try:
            sent = send(past, after)
            pos = max(sent, after)
            while run is not None and not run.done_event.is_set() and not STOPPING.is_set():
                woke = BUS.wait(pos, 15)
                evts, gap, pos = BUS.scan(sent, run=rid)
                if gap:                                # fell out of the ring: the run's file has it all
                    evts = BUS.run_events(rid, sent)
                if not woke:
                    self.wfile.write(b": keepalive\n\n")
                sent = send(evts, sent)
            send(BUS.run_events(rid, sent), sent)      # through the finish event
        except OSError:
            if cancel_on_leave and run is not None:
                RUNS.cancel(rid)

    def _client(self):
        """Who started this: "app" unless a client says otherwise (bb sends cli)."""
        c = (self.headers.get("X-BB-Client") or "app").strip().lower()
        return c if re.match(r"^[a-z][a-z0-9-]{0,15}$", c) else "app"

    def _run_streamed(self, kind, title, work, meta=None, key=None, detach=True):
        """Start work(run, emit) as a server-owned run and stream it to this
        client in the frames the app's screens read. With detach (deploys,
        goals, installs) the run goes on when this client leaves and stops
        only through POST /api/runs/<id>/cancel; without (log tails) it ends
        with its viewer. A run with the same key still going: 409 and its id,
        so the client can attach to it instead."""
        try:
            run = RUNS.start(kind, self._client(), lambda run: work(run, lambda obj: RUNS.emit(run, "out", obj)),
                             title=title, meta=meta, key=key)
        except runsmod.Busy as e:
            self._json({"error": str(e), "run": e.run.id}, 409)
            return
        self._stream_run(run.id, legacy=True, cancel_on_leave=not detach)

    def _export(self, q):
        """Training data as JSONL: ?from=YYYY-MM-DD&to=...&model=substr
        &rated=up|any&errors=1&redact=0&source=all|traces|sessions
        &purpose=chat|compress. Streams; there is no telling the size first."""
        one = lambda k, d=None: (q.get(k) or [d])[0]
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
        if path == "/api/hello":
            # how clients (the window, the CLI) confirm they found the right server
            self._json({"service": "bytebunker", "version": app_version(), "pid": os.getpid(),
                        "data": DATA, "desktop": bool(os.environ.get("BYTEBUNKER_DESKTOP")),
                        "started": round(STARTED, 3)})
            return
        if path == "/api/config":
            self._json({
                "identity": CFG.get("identity", {}),
                "upstream": CFG.get("upstream_url", ""),
                "gateways": [g["name"] for g in GW.gateways()],
                "gateway_urls": {g["name"]: g["url"] for g in GW.gateways()},
                "desktop": bool(os.environ.get("BYTEBUNKER_DESKTOP")),
                "version": app_version(),
                "platform": sys.platform,
                "monitors": len(MON.list()),
                "mcp": bool(CFG.get("mcp_servers")),
                "video": bool(CFG.get("h3_url")),
                "netcheck": bool(CFG.get("netcheck_ssh")),
            })
        elif path == "/api/models":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                GW.refresh(force=bool(q.get("refresh")))
                self._json({"object": "list", "data": GW.models, "gateways": GW.status,
                            "last_used": last_used_model()})
            except Exception as e:   # noqa: BLE001
                self._json({"error": str(e), "data": []}, 502)
        elif path == "/api/settings":
            self._json({"version": app_version(), "desktop": bool(os.environ.get("BYTEBUNKER_DESKTOP")),
                        "platform": sys.platform, "data_dir": DATA, "config_path": CONFIG_PATH,
                        "home_dir": os.path.dirname(CONFIG_PATH),
                        "uploads_dir": os.path.expanduser(CFG.get("uploads_dir") or os.path.join(DATA, "uploads")),
                        "identity": CFG.get("identity") or {}, "rates": CFG.get("frontier_rates_per_mtok") or {},
                        "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False))})
        elif path == "/api/gateways":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            GW.refresh(force=bool(q.get("refresh")))
            gws = []
            for g in GW.gateways(enabled_only=False):
                st = GW.status.get(g["name"]) or {}
                gws.append({"name": g["name"], "url": g["url"], "kind": g.get("kind") or st.get("kind") or "",
                            "enabled": g.get("enabled", True), "has_key": bool(g.get("key")),
                            "ok": st.get("ok"), "models": [m["id"] for m in GW.models if m["gateway"] == g["name"]],
                            "ms": st.get("ms"), "error": st.get("error")})
            body = {"gateways": gws, "models": len(GW.models)}
            if q.get("live"):
                body["live"] = GW.live()
            self._json(body)
        elif path == "/api/cluster":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                hist = int((q.get("history") or ["0"])[0])
            except ValueError:
                hist = 0
            self._json(MON.cluster(hist))
        elif path == "/api/monitors":
            recent = MON.latest(max_age=30) or (MON.cluster(0) if MON.list() else {})
            st = {m["name"]: m for m in recent.get("monitors", [])}
            self._json({"monitors": [{"name": m["name"], "url": m["url"], "enabled": m["enabled"],
                                      "has_token": bool(m["token"]), "status": st.get(m["name"])}
                                     for m in MON.list()]})
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
            # the list is summaries; one session in full with ?id=
            sid = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id") or [None])[0]
            if sid:
                sess = SESSIONS.get(sid) if sessmod._ID.match(sid) else None
                if sess is None:
                    self._json({"error": "no such session"}, 404)
                else:
                    self._json(sess)
            else:
                self._json(SESSIONS.list())
        elif path == "/api/events":
            self._events(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
        elif path == "/api/runs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json({"runs": RUNS.history(limit=max(1, min(1000, _int((q.get("limit") or [""])[0], 100))),
                                             kind=(q.get("kind") or [None])[0]),
                        "seq": BUS.seq})
        elif path.startswith("/api/runs/") and path.endswith("/events"):
            # ?format=data: the frames the app's screens read, to re-attach to a run
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._stream_run(path[len("/api/runs/"):-len("/events")], _int((q.get("after") or [""])[0], 0),
                             legacy=(q.get("format") or [""])[0] == "data")
        elif path.startswith("/api/archive/"):
            self._archive_get(path[len("/api/archive/"):])
        elif path == "/api/agents":
            st = agentmod.status(CFG)
            a = CFG.get("agents") or {}
            st["master_name"] = a.get("master_name") or ""
            st["master_instructions"] = a.get("master_instructions") or ""
            st["run_timeout_s"] = agent_run_timeout(CFG)
            st["master_model"] = a.get("master_model") or ""
            st["thinking_model"] = a.get("thinking_model") or ""
            st["slave_model"] = a.get("slave_model") or ""
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
            self._json({"jobs": JOBS.list(), "running": running_jobs()})
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
            if not sid or not sessmod._ID.match(sid):
                self._json({"error": "bad session id"}, 400)
                return
            gone = SESSIONS.delete(sid)
            if gone is not None:
                TRACE.log("session_deleted", session=sid, record=gone)
                BUS.publish("sessions", "deleted", {"id": sid})
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
        if path.startswith("/api/runs/") and path.endswith("/cancel"):
            self._drain()
            rid = path[len("/api/runs/"):-len("/cancel")]
            ok = RUNS.cancel(rid)
            self._json({"ok": ok, "id": rid}, 200 if ok else 409)
            return
        if path not in ("/api/chat", "/api/sessions", "/api/usage-event",
                        "/api/tool-call", "/api/mcp", "/api/archive", "/api/rate",
                        "/api/plugins", "/api/skills", "/api/recipes", "/api/rack", "/api/upload", "/api/jobs", "/api/gateways", "/api/monitors", "/api/settings", "/api/agents", "/api/agents/slave"):
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
                    TRACE.log("job", action="save", id=job["id"], name=job["name"], schedule=job["schedule"], job_kind=job["kind"])
                    BUS.publish("jobs", "saved", {"id": job["id"]})
                    self._json({"ok": True, "job": job, "jobs": JOBS.list()})
                elif action == "delete":
                    jid = str(body.get("id") or "")
                    going = running_jobs().get(jid)
                    if going:
                        RUNS.cancel(going["run"])
                    JOBS.delete(jid)
                    TRACE.log("job", action="delete", id=jid)
                    BUS.publish("jobs", "deleted", {"id": jid})
                    self._json({"ok": True, "jobs": JOBS.list()})
                elif action == "toggle":
                    j = JOBS.get(str(body.get("id") or ""))
                    if not j:
                        self._json({"error": "no such job"}, 404)
                        return
                    JOBS.set_enabled(j["id"], not j.get("enabled", True))
                    BUS.publish("jobs", "toggled", {"id": j["id"]})
                    self._json({"ok": True, "jobs": JOBS.list()})
                elif action == "run_now":
                    j = JOBS.get(str(body.get("id") or ""))
                    if not j:
                        self._json({"error": "no such job"}, 404)
                        return
                    try:
                        run = launch_job(j, "manual")
                    except runsmod.Busy as e:
                        self._json({"error": "this job is already running", "run": e.run.id}, 409)
                        return
                    self._json({"ok": True, "started": j["id"], "run": run.id})
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
            try:
                summary = SESSIONS.put(body)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            BUS.publish("sessions", "updated", summary)
            self._json({"ok": True})
        elif path == "/api/usage-event":
            evt = sanitize_usage(body)
            if evt:
                mid = str(evt.get("model") or "").rsplit("@", 1)
                evt["gateway"] = (mid[1] if len(mid) == 2 else None) or GW.model_map.get(mid[0]) or ((GW.gateways() or [{}])[0].get("name") or "upstream")
                append_usage(evt)
            self._json({"ok": bool(evt)})
        elif path == "/api/gateways":
            self._gateways_admin(body)
        elif path == "/api/monitors":
            self._monitors_admin(body)
        elif path == "/api/settings":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            ident = CFG.setdefault("identity", {})
            if "user" in body:
                ident["user"] = str(body.get("user") or "").strip()[:60]
            if "host" in body:
                ident["host"] = str(body.get("host") or "").strip()[:60]
            if isinstance(body.get("rates"), dict):
                r = CFG.setdefault("frontier_rates_per_mtok", {})
                for k in ("input", "output"):
                    try:
                        r[k] = max(0.0, min(1000.0, float(body["rates"].get(k, r.get(k, 0)))))
                    except (TypeError, ValueError):
                        pass
            save_config()
            self._json({"ok": True})

    # ---- MCP server management ----
    # add / remove / toggle / restart, persisted to config.json and applied
    # live. Command strings are never shell-parsed — they go straight to
    # Popen as argv, so there is no shell-injection surface here.
    def _agents_run(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        if body.get("action") in ("test_worker", "setup", "disable"):
            a = CFG.setdefault("agents", {})
            ssh = str(body.get("ssh") or "").strip()[:200]
            d = str(body.get("dir") or "~/bytebunker-harness").strip()[:300]
            py = str(body.get("python") or "uv run").strip()[:200]
            if body["action"] == "disable":
                a["enabled"] = False
                save_config()
                TRACE.log("agents", action="disable")
                self._json({"ok": True, "enabled": False})
                return
            if not ssh:
                self._json({"error": "the worker's ssh host is required (an alias from ~/.ssh/config, or user@host)"}, 400)
                return
            res = agentmod.test_worker(ssh, d, py)
            if body["action"] == "setup" and res.get("ok"):
                a.update(enabled=True, ssh=ssh, dir=d, python=py)
                a.setdefault("script", "scripts/run_master.py")
                save_config()
                TRACE.log("agents", action="setup", ssh=ssh, dir=d)
                res["enabled"] = True
            self._json(res)
            return
        if body.get("action") == "config":
            # Save the master's name + instructions into the agents block.
            a = CFG.setdefault("agents", {})
            a["master_name"] = str(body.get("master_name") or "")[:120]
            a["master_instructions"] = str(body.get("master_instructions") or "")[:8000]
            for key in ("master_model", "thinking_model", "slave_model"):
                if key in body:
                    a[key] = str(body.get(key) or "")[:120]
            if body.get("run_timeout_s") is not None:
                try:
                    # 5 min .. 24 h; a multi-step goal on one Spark is a 1-2 h affair
                    a["run_timeout_s"] = max(300, min(86400, int(body.get("run_timeout_s"))))
                except (TypeError, ValueError):
                    pass
            save_config()
            self._json({"ok": True, "master_name": a["master_name"],
                        "master_instructions": a["master_instructions"],
                        "run_timeout_s": agent_run_timeout(CFG),
                        "master_model": a.get("master_model") or "", "thinking_model": a.get("thinking_model") or "",
                        "slave_model": a.get("slave_model") or ""})
            return
        goal = body.get("goal") or ""
        try:
            agentmod.build_command(CFG, goal)        # a setup problem is a 400, not a failed run
        except agentmod.AgentConfigError as e:
            self._json({"error": str(e)}, 400)
            return
        self._run_streamed("agents", goal.strip()[:120] or "agents goal",
                           lambda run, emit: agent_goal(run, emit, goal)[0], key="agents")

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

        def work(run, emit):
            emit({"phase": "start", "host": r["ssh"], "cmd": "rack " + " ".join(args)})
            captured = []

            def on_line(text):
                if len(captured) < 2000:
                    captured.append(text)
                emit({"line": text})
            try:
                code, killed = agentmod.run_streaming(rack_cmd(args), ROOT, on_line, run.cancel_event, timeout_s=3600)
            except Exception as e:   # noqa: BLE001
                emit({"error": str(e)})
                code, killed = -1, "error"
            emit({"phase": "done", "exit": code, "killed": killed})
            TRACE.log("rack", action=action, recipe=recipe, host=r["ssh"], exit=code, run=run.id,
                      killed=killed or False, output=captured[-200:])
            return {"ok": code == 0, "exit": code, "killed": killed or False,
                    "error": None if code == 0 else "rack %s exited %s" % (action, code)}
        # a deploy goes on without its viewer; a log tail or a status read does not
        viewing = action in ("logs", "status")
        self._run_streamed("rack", "rack " + " ".join(args), work,
                           meta={"action": action, "recipe": recipe, "host": r["ssh"]},
                           key=None if viewing else "rack:" + r["ssh"], detach=not viewing)

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

        def work(run, emit):
            emit({"phase": "start", "host": host, "recipe": body.get("id")})
            captured = []

            def on_line(text):
                if len(captured) < 2000:
                    captured.append(text)
                emit({"line": text})
            try:
                code, killed = agentmod.run_streaming(cmd, ROOT, on_line, run.cancel_event, timeout_s=3600)
            except Exception as e:   # noqa: BLE001
                emit({"error": str(e)})
                code, killed = -1, "error"
            emit({"phase": "done", "exit": code, "killed": killed})
            TRACE.log("recipe", action="deploy", id=body.get("id"), host=host, params=rendered["params"], run=run.id,
                      exit=code, killed=killed or False, output=captured[-200:])
            return {"ok": code == 0, "exit": code, "killed": killed or False,
                    "error": None if code == 0 else "deploy exited %s" % code}
        self._run_streamed("deploy", "deploy %s on %s" % (body.get("id"), host), work,
                           meta={"recipe": body.get("id"), "host": host}, key="deploy:" + host)

    def _gateways_admin(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action") or "add"
        gws = gwmod.normalize(CFG)
        if action == "discover":
            hosts = body.get("hosts") or []
            if isinstance(hosts, str):
                hosts = [h.strip() for h in hosts.split(",") if h.strip()]
            res = gwmod.discover(CFG, extra_hosts=hosts, scan_lan=bool(body.get("scan_lan")),
                                 include_tailnet=body.get("tailnet", True))
            TRACE.log("gateway", action="discover", found=len(res["found"]), hosts=res["hosts_probed"], ms=res["ms"])
            self._json(dict(res, ok=True))
            return
        if action == "refresh":
            GW.refresh(force=True)
            self._json({"ok": True, "status": GW.status, "models": len(GW.models)})
            return
        name = str(body.get("name") or "").strip()[:60]
        if action == "add":
            url = gwmod._norm_url(body.get("url"))
            if not url:
                self._json({"error": "url is required (host:port or http://host:port/v1)"}, 400)
                return
            if any(g["url"] == url for g in gws):
                self._json({"error": "that gateway is already configured"}, 409)
                return
            name = name or url.split("//")[-1].split("/")[0]
            if any(g["name"] == name for g in gws):
                name = name + "-" + str(len(gws) + 1)
            gws.append({"name": name, "url": url, "key": str(body.get("key") or ""), "enabled": True,
                        "kind": str(body.get("kind") or "")})
        else:
            g = next((x for x in gws if x["name"] == name), None)
            if not g:
                self._json({"error": "no such gateway"}, 404)
                return
            if action == "remove":
                gws.remove(g)
            elif action == "toggle":
                g["enabled"] = not g.get("enabled", True)
            elif action == "update":
                if body.get("url"):
                    g["url"] = gwmod._norm_url(body["url"])
                if "key" in body:
                    g["key"] = str(body.get("key") or "")
                if body.get("kind") is not None:
                    g["kind"] = str(body.get("kind") or "")
                if body.get("new_name"):
                    g["name"] = str(body["new_name"]).strip()[:60]
            else:
                self._json({"error": "unknown action"}, 400)
                return
        CFG["gateways"] = gws
        gwmod.normalize(CFG)          # mirrors the first enabled gateway into upstream_url
        save_config()
        GW.refresh(force=True)
        TRACE.log("gateway", action=action, name=name)
        self._json({"ok": True, "status": GW.status, "models": len(GW.models),
                    "gateways": [{"name": g["name"], "url": g["url"], "enabled": g.get("enabled", True)} for g in gws]})

    def _monitors_admin(self, body):
        """Add, edit, toggle, remove, test or find rack monitors. Add checks
        the endpoint first (it must be a rack monitor and the token must
        work) unless force is set, so a typo is caught here, not later as a
        blank Cluster screen."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action") or "add"
        if action == "discover":
            hosts = body.get("hosts") or []
            if isinstance(hosts, str):
                hosts = [h.strip() for h in hosts.split(",") if h.strip()]
            res = monmod.discover(CFG, extra_hosts=hosts, include_tailnet=body.get("tailnet", True))
            TRACE.log("monitor", action="discover", found=len(res["found"]), hosts=res["hosts_probed"], ms=res["ms"])
            self._json(dict(res, ok=True))
            return
        mons = monmod.normalize(CFG)
        name = str(body.get("name") or "").strip()[:60]
        if action in ("add", "test"):
            url, url_token = monmod.parse_url(body.get("url"))
            if not url:
                self._json({"error": "endpoint is required: host:port or http://host:9177"}, 400)
                return
            token = str(body.get("token") or "").strip() or url_token
            if action == "add" and any(m["url"] == url for m in mons):
                self._json({"error": "that monitor is already configured"}, 409)
                return
            res = monmod.check(url, token)
            if action == "test":
                self._json(dict(res, url=url))
                return
            if not res["ok"] and not body.get("force"):
                self._json(dict(res, url=url), 422)
                return
            name = name or (res.get("cluster") if res.get("ok") else "") or url.split("//")[-1]
            if any(m["name"] == name for m in mons):
                name = "%s-%d" % (name, len(mons) + 1)
            mons.append({"name": name, "url": url, "token": token, "enabled": True})
        else:
            m = next((x for x in mons if x["name"] == name), None)
            if not m:
                self._json({"error": "no such monitor"}, 404)
                return
            if action == "remove":
                mons.remove(m)
            elif action == "toggle":
                m["enabled"] = not m["enabled"]
            elif action == "update":
                if body.get("url"):
                    url, url_token = monmod.parse_url(body["url"])
                    if not url:
                        self._json({"error": "bad endpoint"}, 400)
                        return
                    m["url"] = url
                    if url_token and "token" not in body:
                        m["token"] = url_token
                if "token" in body:
                    m["token"] = str(body.get("token") or "").strip()
                if body.get("new_name"):
                    m["name"] = str(body["new_name"]).strip()[:60]
            else:
                self._json({"error": "unknown action"}, 400)
                return
        CFG["monitors"] = mons
        save_config()
        MON.invalidate()
        TRACE.log("monitor", action=action, name=name)
        self._json({"ok": True, "name": name,
                    "monitors": [{"name": m["name"], "url": m["url"], "enabled": m["enabled"],
                                  "has_token": bool(m["token"])} for m in mons]})

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
            if name and name not in servers:
                self._json({"error": "no such server"}, 404)
                return
        else:
            self._json({"error": "unknown action"}, 400)
            return

        save_config()
        # only the server this edit touched restarts; the others keep running
        h = mcp_reload(restart=(name,) if action == "restart" and name else ())
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
        def work(run, emit):
            shell_env = catmod.shell_env()
            emit({"phase": "start", "id": entry["id"], "name": name, "steps": entry.get("install", [])})
            for step in entry.get("install", []):
                emit({"line": "$ " + step})
                wrapped = ["env", "PATH=" + shell_env["PATH"], "bash", "-c", step]
                try:
                    code, killed = agentmod.run_streaming(wrapped, ROOT, lambda t: emit({"line": t}), run.cancel_event,
                                                          timeout_s=1800)
                except Exception as e:   # noqa: BLE001
                    emit({"error": str(e)})
                    code, killed = -1, "error"
                if code != 0:
                    emit({"phase": "done", "exit": code, "killed": killed, "added": False})
                    TRACE.log("mcp", action="install", name=name, catalog=entry["id"], exit=code, step=step, run=run.id)
                    return {"ok": False, "exit": code, "error": "install step failed: " + step[:200]}
            servers = CFG.setdefault("mcp_servers", {})
            servers[name] = {"command": cmd, "args": args, "env": env, "enabled": True, "catalog": entry["id"]}
            save_config()
            h = mcp_reload(restart=(name,))
            st = h.status.get(name) or {}
            emit({"line": "added %s: %s %s" % (name, cmd, " ".join(args))})
            emit({"line": "server %s: %s" % (name, (str(st.get("tools")) + " tools") if st.get("state") == "ready"
                                             else st.get("state", "?") + (" \u2014 " + st["error"] if st.get("error") else ""))})
            emit({"phase": "done", "exit": 0, "killed": False, "added": True, "status": st})
            TRACE.log("mcp", action="install", name=name, catalog=entry["id"], exit=0, status=st, run=run.id)
            return {"ok": True, "added": name}
        self._run_streamed("mcp-install", "install " + name, work, meta={"catalog": entry["id"], "name": name},
                           key="mcp-install:" + name)

    # ---- streaming chat proxy ----
    def _chat(self, payload):
        if not isinstance(payload, dict):
            self._json({"error": "expected object"}, 400)
            return
        # who this request belongs to, for the trace: headers, so nothing
        # extra travels upstream
        tid = TRACE.new_id()
        who = {"id": tid, "session": self.headers.get("X-BB-Session"),
               "turn": self.headers.get("X-BB-Turn"),
               "purpose": self.headers.get("X-BB-Purpose") or "chat"}
        started = time.time()
        try:
            resp, gw = upstreammod.open_chat(GW, payload)
        except upstreammod.UpstreamError as e:
            who["gateway"] = e.gateway["name"] if e.gateway else None
            TRACE.log("chat", status=e.status, request=trace_safe(payload), response=None, error=e.message[:1000],
                      ms=int((time.time() - started) * 1000), **who)
            self._json({"error": e.message}, e.status)
            return
        who["gateway"] = gw["name"] if gw else None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-BB-Trace", tid)
        self.close_connection = True
        self.end_headers()
        cap = StreamCapture()

        def write(chunk):
            self.wfile.write(chunk)
            self.wfile.flush()
        try:
            upstreammod.relay(resp, write, cap)
        finally:
            resp.close()
            TRACE.log("chat", status=200, request=trace_safe(payload), response=cap.result(started),
                      ms=int((time.time() - started) * 1000), **who)


def serve(bind=None, port=None):
    """Build the HTTP server (not yet serving) and start the job scheduler.
    Used by main() and by the desktop app, which runs it on a thread behind
    a native window. port 0 picks a free port; read it from
    srv.server_address[1]."""
    bind = bind or CFG.get("bind", "127.0.0.1")
    if bind not in ("127.0.0.1", "localhost", "::1"):
        # There is no auth on any route. Loopback-only is the security model;
        # a wider bind turns the rack into an open inference gateway for the
        # whole LAN. Refuse rather than warn — reach it over SSH or the overlay.
        raise SystemExit(
            "refusing to bind %s: the console has no auth and is loopback-only "
            "by design. Reach it via SSH tunnel or overlay network." % bind)
    CFG["bind"] = bind
    # one server per data folder: a second one would run a second job
    # scheduler and write the same sessions and traces
    global INSTANCE
    inst = instancemod.Instance(DATA)
    if not inst.acquire():
        info = instancemod.read(DATA) or {}
        raise SystemExit("another ByteBunker server is already using %s%s" % (
            DATA, (": http://127.0.0.1:%s/ (pid %s)" % (info.get("port"), info.get("pid"))) if info else ""))
    try:
        srv = ThreadingHTTPServer((bind, int(CFG.get("port", 8765) if port is None else port)), Handler)
    except OSError:
        inst.release()
        raise
    INSTANCE = inst
    inst.publish(srv.server_address[1], version=app_version(), desktop=bool(os.environ.get("BYTEBUNKER_DESKTOP")))
    import atexit
    atexit.register(release_instance)
    # the built-in jobs MCP server calls the console back on this port
    os.environ["BB_CONSOLE_PORT"] = str(srv.server_address[1])
    start_scheduler()
    mcp_host()      # servers start in the background, ready before the first chat
    return srv


INSTANCE = None
STARTED = time.time()
STOPPING = threading.Event()


def app_version():
    return os.environ.get("BYTEBUNKER_VERSION") or VERSION


def release_instance():
    """The server is going away: end the event streams and free the data folder."""
    global INSTANCE
    if not STOPPING.is_set():
        STOPPING.set()
        BUS.publish("system", "stopping")        # wakes every stream's wait
    if INSTANCE is not None:
        INSTANCE.release()
        INSTANCE = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=CFG.get("port", 8765))
    ap.add_argument("--bind", default=CFG.get("bind", "127.0.0.1"))
    args = ap.parse_args()
    srv = serve(args.bind, args.port)
    print("ByteBunker Console on http://%s:%d  gateways: %s  jobs: %d" %
          (args.bind, srv.server_address[1], ", ".join(g["name"] for g in GW.gateways()) or "none",
           len(JOBS.jobs)))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        release_instance()


if __name__ == "__main__":
    main()
