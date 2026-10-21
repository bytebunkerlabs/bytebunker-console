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
import hmac
import http.cookies
import json
import secrets
import platform
import sys
import os
import re
import shutil
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
import racks as racksmod
import vault as vaultmod
import machines as machinesmod
import instance as instancemod
import events as eventsmod
import runs as runsmod
import sessions as sessmod
import cmdbox
import upstream as upstreammod
import runner as runnermod
import model_facts as factsmod
import approvals as approvalsmod
import audit as auditmod
import changelog
import updater as updatemod
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
# Who changed what, when: settings, access, tools, machines, agents, deletions.
# data/audit/audit.jsonl, hash-chained, never pruned (audit.py; the Audit screen).
AUDIT = auditmod.AuditLog(DATA, BUS)
RUNS = runsmod.RunRegistry(BUS, os.path.join(DATA, "runs", "index.jsonl"))
SESSIONS = sessmod.SessionStore(os.path.join(DATA, "sessions"), legacy_path=os.path.join(DATA, "sessions.json"))

DEFAULT_CONFIG = {
    "bind": "127.0.0.1",
    "port": 8765,
    "upstream_url": "http://127.0.0.1:8000/v1",
    "upstream_key": "bb-local",
    # rack monitors for the Cluster screen: [{"name", "url", "token", "enabled"}]
    "monitors": [],
    # machines serving with dgx-serve, paired over ssh (racks.py):
    # [{"name", "ssh", "paired", "app", "gateway", "monitor"}]
    "racks": [],
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
               "script": "scripts/run_master.py", "run_timeout_s": 3600},
}


def agent_run_timeout(cfg):
    """Wall-clock cap on one delegated goal. A multi-step goal on a single
    Spark is a 1-2 hour affair; the old fixed 3600 s killed the master while
    it was writing its synthesis after every agent had delivered."""
    return agentmod.agent_timeout(cfg)


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
    """What a model can do. The window, best source first: what its engine
    said when it refused a prompt (model_facts), what its engine says it
    serves (vLLM's max_model_len), then the capability table."""
    table = dict(DEFAULT_CAPS)
    table.update(CFG.get("model_capabilities") or {})
    mid = (model_id or "").lower()
    hits = [k for k in table if k.lower() in mid]
    merged = dict(CAPS_FALLBACK)
    if hits:
        merged.update(table[max(hits, key=len)])
    base = (model_id or "").rsplit("@", 1)[0]
    # what dgx-serve says it serves this model with (the monitor's serving block)
    sv = (MON.serving().get(model_id) or MON.serving().get(base)) if "MON" in globals() else None
    if sv:
        if isinstance(sv.get("context"), int) and sv["context"] > 0:
            merged["ctx"] = sv["context"]
        for k in ("tools", "vision"):
            if sv.get(k) is not None:
                merged[k] = bool(sv[k])
        dia = sv.get("dialect") or {}
        if str(dia.get("strip_reasoning")) in ("1", "true", "True"):
            merged["strip_reasoning"] = True
        if str(dia.get("echo_reasoning")) in ("1", "true", "True"):
            merged["strip_reasoning"] = False
        if dia.get("effort") and "." not in str(dia["effort"]):
            merged["effort"] = str(dia["effort"]).replace(",", " ").split()
        if dia.get("thinking"):
            merged["thinking_switch"] = str(dia["thinking"])
        if str(dia.get("min_max_tokens") or "").isdigit():
            merged["min_max_tokens"] = int(dia["min_max_tokens"])
        merged["served_by"] = sv.get("node")
    served = (GW.served.get(model_id) or GW.served.get(base)) if "GW" in globals() else None
    if served:
        merged["ctx"] = served
    learned = FACTS.ctx(model_id) if "FACTS" in globals() else None
    if learned:
        merged["ctx"] = learned
    return merged


def _default_identity():
    try:
        user = getpass.getuser()
    except Exception:   # noqa: BLE001 - no USER and no passwd entry
        user = "you"
    return {"user": user, "host": (platform.node() or "this machine").split(".")[0]}


# config.json has one writer, this server. Its shape is versioned: an older
# file is migrated on start (the original kept beside it), and an edit made
# by hand while the server runs is picked up before the next change, so a
# save never undoes an edit it has not seen.
CONFIG_VERSION = 3
CONFIG_WARNINGS = []             # shown in the app: what happened to the file and where the original is
_CFG_STAMP = [None]              # (mtime, size) of config.json as this server last read or wrote it
_CFG_MIGRATED = []


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _set_aside(path, why):
    """Keep a copy of config.json before it is replaced; returns its name."""
    kept = "%s.%s-%s" % (path, why, time.strftime("%Y%m%d-%H%M%S"))
    try:
        import shutil
        shutil.copy2(path, kept)
        os.chmod(kept, 0o600)
    except OSError:
        pass
    return kept


def _read_config_file(path):
    """The file's object, or None when there is no file; ValueError when it is not valid JSON."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("it must hold one JSON object")
    return data


def migrate_config(cfg):
    """Bring a config to CONFIG_VERSION, in order. Returns the steps taken."""
    steps = []
    v = cfg.get("config_version") or 1
    if v < 2:
        # 2 (0.3): model servers are gateways, not one upstream_url; the
        # sparkDash era's keys (replaced by rack monitors in 0.2) go
        gwmod.normalize(cfg)
        for k in [k for k in cfg if k.startswith("sparkdash") or k in ("telemetry_source", "nodes")]:
            cfg.pop(k, None)
        steps.append("2: gateways")
    if v < 3:
        # 3 (0.4.2): the one `rack` becomes the first of `racks`, which Add a rack pairs
        racksmod.migrate(cfg)
        steps.append("3: racks")
    cfg["config_version"] = CONFIG_VERSION
    return steps


def _fresh_config(disk):
    cfg = dict(DEFAULT_CONFIG)
    cfg["identity"] = _default_identity()
    cfg.update(disk or {})
    return cfg


def load_config():
    try:
        disk = _read_config_file(CONFIG_PATH)
    except (OSError, ValueError) as e:
        kept = _set_aside(CONFIG_PATH, "unreadable")
        CONFIG_WARNINGS.append("config.json could not be read (%s), so the app started with defaults. "
                               "Your file is kept as %s." % (str(e)[:160], os.path.basename(kept)))
        disk = None
    cfg = _fresh_config(disk)
    if disk is not None and (disk.get("config_version") or 1) < CONFIG_VERSION:
        _CFG_MIGRATED.extend(migrate_config(cfg))
    else:
        cfg["config_version"] = CONFIG_VERSION
    # an unreadable file is already kept aside: the first save may replace it
    _CFG_STAMP[0] = _stamp(CONFIG_PATH)
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
# Machines serve models with dgx-serve's `rack`: its recipes decide the
# engine, solo or tensor-parallel, and `rack up` serves one. The console
# drives rack over ssh (racks.py: a paired rack with this app's own key,
# allowed to run rack and nothing else) and shows its output; it never
# composes a docker command for a node itself.
KEYS = racksmod.Keys(os.path.dirname(CONFIG_PATH))
agentmod.EXTRA_SSH[:] = ["-o", KEYS._known()]     # a worker found by Machines is known by its pinned key
PAIRINGS = racksmod.Pending()
# credentials the app keeps but never shows: git host tokens live here, 0600,
# beside config but not in it (config is shown and may be shared)
VAULT_DIR = os.path.dirname(CONFIG_PATH)
VAULT = vaultmod.Vault(os.path.join(VAULT_DIR, "secrets.json"))


def git_ls_remote(url, token_rec=None):
    """Reach a git remote without cloning, to tell public from private and to
    test a token. Returns (returncode or None, stderr/stdout)."""
    import subprocess
    env = vaultmod.git_env(VAULT_DIR, token_rec)
    try:
        r = subprocess.run(["git", "ls-remote", "--heads", url], capture_output=True, text=True,
                           timeout=25, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    return r.returncode, (r.stderr or r.stdout or "")


def github_login(token):
    """The @login a GitHub token belongs to, for display, or '' — also a cheap
    validity check. Only called for github.com."""
    import urllib.request
    req = urllib.request.Request("https://api.github.com/user",
                                 headers={"Authorization": "Bearer " + token,
                                          "Accept": "application/vnd.github+json", "User-Agent": "ByteBunker"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}").get("login") or ""
    except Exception:   # noqa: BLE001 — a login lookup must never block connecting
        return ""


def racks():
    """The racks in config (this computer is not one of them: see rack_named)."""
    return racksmod.configured(CFG)


def rack_named(name=None):
    """A rack by name, or the first one; with none configured, this computer
    (racks.LOCAL), which is always there to name."""
    rs = racks()
    if name == racksmod.LOCAL:
        return racksmod.local_entry()
    if not name:
        return rs[0] if rs else racksmod.local_entry()
    return next((r for r in rs if r["name"] == name), None)


def rack_cmd(args, rack):
    return racksmod.rack_argv(KEYS, rack, args)


def shlex_quote(a):
    import shlex
    return shlex.quote(str(a))


_rack_cache = {}                 # rack name -> (time, overview)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")        # terminal colour and cursor codes


def rack_recipes(text):
    """The recipes a rack lists. dgx-serve 1.0 answers `rack recipes --json`
    with each recipe's model, platforms and files (yours live outside the
    checkout); a rack from before 1.0 prints a table, read by its columns."""
    try:
        d = json.loads(text)
    except ValueError:
        d = None
    if isinstance(d, dict) and isinstance(d.get("recipes"), list):
        out = []
        for r in d["recipes"]:
            vs = r.get("variants") or {}
            plat = d.get("here") if d.get("here") in vs else next(iter(r.get("platforms") or []), None)
            v = vs.get(plat) or {}
            tp = v.get("tensor_parallel") or 1
            files = [v.get("file") or ""]
            if r.get("layout") == "v2" and r.get("location"):
                files.insert(0, r["location"].rstrip("/") + "/model.env")   # what the variant sources
            out.append({"name": r.get("name") or "", "mode": "TP=%d" % tp if tp > 1 else "solo",
                        "model": r.get("model") or v.get("model") or v.get("artifact") or "",
                        "platforms": r.get("platforms") or [], "files": [f for f in files if f],
                        "here": bool(d.get("here")) and d.get("here") in vs, "fits": v.get("fits"),
                        "weights_gb": v.get("weights_gb"), "engine": v.get("engine") or "",
                        "roles": r.get("roles") or []})
        return out
    recipes = []
    for ln in _ANSI.sub("", text).splitlines():
        tok = ln.strip().split()
        # "  dsv4            TP=2   deepseek-ai/DeepSeek-V4-Flash-0731"
        if len(tok) >= 2 and re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", tok[0]) and tok[0] != "TEMPLATE":
            recipes.append({"name": tok[0].replace(".env", ""), "mode": tok[1],
                            "model": tok[2] if len(tok) > 2 else ""})
    return recipes


def rack_serving(status):
    """The recipe `rack status` says is serving: 1.0 names it under "serving"
    ("glm53 (org/model) with vllm under docker ..."), older racks after "serving:"."""
    m = re.search(r"^serving\s*\n\s+([A-Za-z0-9][A-Za-z0-9._-]*) \(", status, re.M) \
        or re.search(r"serving:\s*(\S+)", status)
    return m.group(1) if m else ""


_machines = {"at": 0, "list": [], "here": None}


def annotate_machines(found):
    """Mark the machines this app already drives (a rack, by any of its names)."""
    rs = racks()
    for m in found:
        names = set(m.get("aliases") or []) | {m.get("target"), m.get("hostname")}
        r = next((r for r in rs if racksmod.host_of(r["ssh"]) in names or r["ssh"] in names), None)
        m["rack"] = m.get("rack") or ""
        m["paired_as"] = r["name"] if r else ""
        m["worker_of"] = next((head for head, ws in _machines.get("workers", {}).items()
                               if names & ws or (m.get("hostname") or "") in ws), "")
    return found


def rack_workers():
    """{rack name: the names its worker nodes go by}, from each paired rack's inventory."""
    import subprocess
    out = {}
    for r in racks():
        if not r["paired"]:
            continue
        try:
            p = subprocess.run(rack_cmd(["nodes", "--json"], r), capture_output=True, text=True, timeout=20)
            nodes = json.loads(p.stdout).get("nodes") or []
        except Exception:   # noqa: BLE001  (not reachable now: nothing to mark)
            continue
        names = set()
        for n in nodes:
            if n.get("role") == "worker":
                names |= {x for x in (n.get("name"), n.get("ssh"), racksmod.host_of(n.get("ssh") or "")) if x}
        if names:
            out[r["name"]] = names
    return out


def deploy_chain(run, emit, target, recipe):
    """One click on a machine: dgx-serve there if missing, paired, the recipe
    served, its engine added as a gateway. The steps say what they do."""
    import subprocess
    say = lambda t: emit({"line": t})
    local = target == racksmod.LOCAL

    def stream(argv, timeout):
        def on_line(text):
            text = _ANSI.sub("", text)
            if text.startswith("BB_ERR "):
                emit({"error": text[7:]})
            elif text != "BB_INSTALLED":
                say(text)
        return agentmod.run_streaming(argv, ROOT, on_line, run.cancel_event, timeout_s=timeout)

    def fail(msg, code=1):
        emit({"error": msg})
        emit({"phase": "done", "exit": code, "killed": False})
        TRACE.log("machine", action="deploy", target=target, recipe=recipe, ok=False, error=msg[:300], run=run.id)
        return {"ok": False, "error": msg}

    rack = racksmod.local_entry() if local else next(
        (r for r in racks() if r["paired"] and (r["ssh"] == target or racksmod.host_of(r["ssh"]) == target)), None)
    if local and not rack["installed"] or not local and rack is None:
        if local or not machinesmod.probe(KEYS, target).get("rack"):
            say("== dgx-serve on %s" % ("this computer" if local else target))
            code, killed = stream(racksmod.install_argv(KEYS, target), 1800)
            if code != 0:
                return fail("the install did not finish (exit %s)" % code, code or 1)
        if not local:
            say("== pairing with %s" % target)
            try:
                kind, got = racksmod.ask(KEYS, target, say)
            except racksmod.PairError as e:
                return fail(str(e))
            if kind == "needs_key":
                emit({"needs_key": got})
                emit({"phase": "done", "exit": 2, "killed": False})
                return {"ok": False, "error": "needs the app's key on %s first" % target}
            engine_url, monitor_url, _ = racksmod.find(target, got)
            rack = racksmod.apply(CFG, {"target": target, "info": got, "engine_url": engine_url,
                                        "monitor_url": monitor_url, "app": KEYS.app_name()}, gw_normalize=gwmod.normalize)
            save_config()
            GW.refresh(force=True)
            MON.invalidate()
            rack = rack_named(rack["name"])
            say("paired: %s" % rack["name"])
        else:
            rack = racksmod.local_entry()
    say("== serving %s on %s" % (recipe, "this computer" if local else rack["name"]))
    _rack_cache.pop(rack["name"], None)
    code, killed = stream(rack_cmd(["up", recipe], rack), 3600)
    if code != 0:
        return fail("rack up %s exited %s: the log above says why" % (recipe, code), code or 1)
    ensure_monitor(rack, say)
    if local:
        added = connect_local(emit)
    else:
        added = {}
        try:
            out = subprocess.run(rack_cmd(["pair", "--json"], rack), capture_output=True, text=True, timeout=30)
            info = racksmod.parse(out.stdout)
            engine_url, monitor_url, _ = racksmod.find(rack["ssh"], info)
            racksmod.apply(CFG, {"target": rack["ssh"], "info": info, "engine_url": engine_url,
                                 "monitor_url": monitor_url, "app": rack.get("app") or KEYS.app_name()},
                           gw_normalize=gwmod.normalize)
            save_config()
            GW.refresh(force=True)
            MON.invalidate()
            r2 = rack_named(rack["name"]) or {}
            added = {k: r2.get(k) for k in ("gateway", "monitor") if r2.get(k)}
            if not engine_url:
                say("its engine does not answer from here yet: Gateways adds it when it does")
        except (racksmod.PairError, OSError, subprocess.SubprocessError) as e:
            say("could not read its engine to add it: %s" % e)
    if added:
        say("this app talks to it now: " + ", ".join("%s %s" % kv for kv in sorted(added.items())))
    BUS.publish("config", "racks", {"racks": [x["name"] for x in racks()]})
    emit({"deployed": {"target": target, "rack": rack["name"], "recipe": recipe}})
    emit({"phase": "done", "exit": 0, "killed": False})
    TRACE.log("machine", action="deploy", target=target, recipe=recipe, ok=True, run=run.id)
    return {"ok": True}


# ------------------------------------------------------------ agent plane --
# Where agents work: any machine Machines found, or this computer (sandboxed
# in podman's Linux VM on a Mac: an exception to the separate-host rule, and
# labelled so). A machine that serves models is the model plane: agents there
# would slow its model down, so it is shown and not offered.
PLANE_CHECK = r'''
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
echo "system: $(uname -sm)"
RT=""
bwrap_ok() {
  command -v bwrap >/dev/null 2>&1 && systemctl --user is-system-running 2>/dev/null | grep -qE '^(running|degraded)' || return 1
  B="--ro-bind /usr /usr"
  for d in /bin /sbin /lib /lib32 /lib64; do
    if [ -L "$d" ]; then B="$B --symlink $(readlink "$d") $d"; elif [ -d "$d" ]; then B="$B --ro-bind $d $d"; fi
  done
  bwrap --unshare-all --die-with-parent $B --proc /proc --dev /dev true 2>/dev/null
}
case "$(uname -s)" in
  Darwin)
    # a Mac: Seatbelt, macOS's own sandbox (no VM, no image, nothing to install)
    if command -v sandbox-exec >/dev/null 2>&1; then RT=seatbelt; fi ;;
  Linux)
    if bwrap_ok; then RT=bwrap
    elif ! command -v bwrap >/dev/null 2>&1; then
      # bubblewrap is one package: installed now when sudo needs no password, else you run one line
      if command -v apt-get >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        echo "installing bubblewrap"
        sudo -n apt-get install -y bubblewrap >/dev/null 2>&1 && bwrap_ok && RT=bwrap
      fi
      [ -n "$RT" ] || echo "BB_NEED bubblewrap"
    fi ;;
esac
if [ -z "$RT" ]; then
  if command -v podman >/dev/null 2>&1; then RT=podman; elif command -v docker >/dev/null 2>&1; then RT=docker; fi
  [ -n "$RT" ] && echo "note: using $RT until bubblewrap is there"
fi
if [ -z "$RT" ]; then
  echo "BB_ERR no sandbox here yet: install bubblewrap (one line, needs your password), then press Work here again"
  exit 4
fi
case "$RT" in
  seatbelt) echo "sandbox: Seatbelt, macOS's own: nothing to install" ;;
  bwrap) echo "sandbox: bubblewrap $(bwrap --version 2>/dev/null | awk '{print $2}'), limits by systemd: no image to build" ;;
  *) echo "sandbox: $($RT --version 2>/dev/null | head -1)" ;;
esac
if ! command -v uv >/dev/null 2>&1; then
  echo "installing uv (runs the harness's Python) into ~/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || { echo "BB_ERR could not install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 4; }
fi
echo "uv: $(uv --version 2>/dev/null)"
if [ "$(uname -s)" = Linux ] && command -v loginctl >/dev/null 2>&1 && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
  if sudo -n true 2>/dev/null; then sudo -n loginctl enable-linger "$USER" && echo "linger on: agents keep working after you log out"
  else echo "BB_NEED linger"; fi
fi
echo "BB_RUNTIME=$RT"
'''

PLANE_BUILD = r'''
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$BB_DIR" || exit 4
RUNNER=$BB_RT; case "$BB_RT" in bwrap|seatbelt) RUNNER=auto ;; esac
if [ ! -f config/config.yaml ] && [ -f config/config.example.yaml ]; then
  sed "s/^\(  *runner:\).*/\1 $RUNNER/" config/config.example.yaml > config/config.yaml
  echo "config/config.yaml made from the example (runner: $RUNNER)"
elif [ -f config/config.yaml ] && ! grep -qE "^ +runner: *$RUNNER( |$)" config/config.yaml; then
  sed "s/^\(  *runner:\).*/\1 $RUNNER/" config/config.yaml > config/config.yaml.new && mv config/config.yaml.new config/config.yaml
  echo "config/config.yaml: sandbox runner set to $RUNNER"
fi
case "$BB_RT" in bwrap|seatbelt) echo "the sandbox runs the agents' code directly: no image"; exit 0 ;; esac
echo "building the sandbox image (the first time takes a few minutes)"
"$BB_RT" build -q -f docker/Dockerfile.slave -t bytebunker-slave:latest . || exit 5
echo "sandbox image: bytebunker-slave:latest"
'''


# What a machine may need that only you can install (it asks for your password): the exact line,
# opened in a terminal by "Install in a terminal". Nothing else is ever run this way. Which ones a
# machine lacks comes from Find machines (machines.needs).
PLANE_NEEDS = {
    "bubblewrap": {"command": "sudo apt-get install -y bubblewrap",
                   "why": "bubblewrap, the sandbox agents run in on Linux"},
    "linger": {"command": "sudo loginctl enable-linger \"$USER\"",
               "why": "linger, so agents keep working after you log out there"},
    "python3": {"command": "sudo apt-get install -y python3 git curl",
                "why": "python3, git and curl, which dgx-serve installs and runs with"},
    "docker": {"command": "sudo apt-get install -y docker.io && sudo usermod -aG docker \"$USER\"",
               "why": "Docker, which serves the models there (log out and in again after it, so you may use it)"},
    "mac-tools": {"command": "xcode-select --install",
                  "why": "Apple's command line tools (python3 and git), which dgx-serve runs with"},
}
NEEDS = PLANE_NEEDS


def install_terminal_line(target, what):
    """The one line that installs `what` on target, as this computer's terminal runs it."""
    import shlex
    need = NEEDS[what]
    if target != racksmod.LOCAL:
        return "ssh -t %s %s" % (shlex.quote(target), shlex.quote(need["command"]))
    if sys.platform == "win32":
        return "wsl.exe -e bash -lc %s" % shlex.quote(need["command"])     # this computer serves through WSL2
    return need["command"]


def open_install_terminal(target, what):
    """A terminal on this computer that runs one of NEEDS there (over ssh with a terminal, so sudo
    can ask for the password) and waits for Enter. A server with no desktop returns the line."""
    import shutil
    import subprocess
    need = NEEDS[what]
    line = install_terminal_line(target, what)
    if not os.environ.get("BYTEBUNKER_DESKTOP"):
        return {"opened": False, "command": line}
    where = (" on " + target) if target != racksmod.LOCAL else ""
    d = os.path.join(os.path.dirname(CONFIG_PATH), "terminal")
    os.makedirs(d, exist_ok=True)
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        script = os.path.join(d, "install-%s.ps1" % what)
        with open(script, "w", encoding="utf-8") as f:
            f.write("# opened from ByteBunker: %s\nWrite-Host 'ByteBunker installs %s%s.'\nWrite-Host ''\n%s\n"
                    "Write-Host ''\nRead-Host 'Done. Press Enter, then look again in ByteBunker'\n" % (
                        need["why"], need["why"].replace("'", "''"), where, line))
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                         creationflags=0x00000010, **quiet)          # CREATE_NEW_CONSOLE: a window of its own
        return {"opened": True, "command": line}
    script = os.path.join(d, "install-%s.command" % what)
    with open(script, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n# opened from ByteBunker: %s\necho 'ByteBunker installs %s%s.'\necho\n%s\n"
                "echo\nread -r -p 'Done. Press Enter, then look again in ByteBunker. ' _\n" % (
                    need["why"], need["why"].replace("'", ""), where, line))
    os.chmod(script, 0o700)
    if sys.platform == "darwin":
        subprocess.Popen(["open", script], **quiet)
        return {"opened": True, "command": line}
    term = next((t for t in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm") if shutil.which(t)), None)
    if not term:
        return {"opened": False, "command": line}
    subprocess.Popen([term, "--", script] if term == "gnome-terminal" else [term, "-e", script], **quiet)
    return {"opened": True, "command": line}


def harness_source():
    """A harness checkout on this computer to ship to a worker, or None.
    (Release builds will carry it as a bundle: plan decision 4.)"""
    for d in (os.environ.get("BYTEBUNKER_HARNESS_SRC"), "~/bb/bytebunker-harness", "~/bytebunker-harness"):
        if d and os.path.isfile(os.path.join(os.path.expanduser(d), "scripts", "run_master.py")):
            return os.path.expanduser(d)
    return None


def agent_planes():
    """Every machine agents could work on, from the last Find, with why or why not."""
    gw_hosts = set()
    for g in CFG.get("gateways") or []:
        try:
            gw_hosts.add(urllib.parse.urlparse(g.get("url") or "").hostname or "")
        except ValueError:
            pass
    a = CFG.get("agents") or {}
    current = (a.get("ssh") or racksmod.LOCAL) if a.get("enabled") else ""
    # the worker's harness predates data access and the light sandboxes: Work here again updates it
    outdated = bool(current) and not (a.get("harness") or {}).get("access")
    out = []
    here = _machines.get("here") or {}
    out.append({"target": racksmod.LOCAL, "name": "This computer", "description": here.get("description") or "",
                "kind": "this computer", "selectable": True, "current": current == racksmod.LOCAL,
                "note": ("ready: Seatbelt, macOS's own sandbox, nothing to install; the exception: agents beside this app"
                         if sys.platform == "darwin" else "the exception: agents beside this app, each task in its own sandbox")})
    for m in annotate_machines(_machines.get("list") or []):
        if not m.get("ok"):
            continue
        names = set(m.get("aliases") or []) | {m.get("target"), m.get("hostname")}
        serves = bool(m.get("paired_as") or m.get("worker_of") or names & gw_hosts)
        out.append({"target": m["target"], "name": m["target"], "description": m.get("description") or "",
                    "kind": "model plane" if serves else "machine", "selectable": not serves,
                    "current": current in names,
                    "has": m.get("has") or [],
                    "note": "serves models: agents there would slow them down" if serves else
                            ("ready: Seatbelt, nothing to install" if m.get("platform") == "mac" else
                             "bubblewrap is there" if "bwrap" in (m.get("has") or []) else
                             "bubblewrap gets installed (your password, once)")})
    if current and current != racksmod.LOCAL and not any(p["current"] for p in out):
        out.append({"target": current, "name": current, "description": "", "kind": "machine", "selectable": True,
                    "current": True, "note": "the worker set before; Find machines to see it here"})
    for p in out:
        if p.get("current"):
            p["outdated"] = outdated
    return out


def agent_plane_setup(run, emit, target):
    """Make target the agent plane: check it (container runtime, uv), bring
    the harness there and build its sandbox image, test, then enable."""
    import subprocess
    say = lambda t: emit({"line": t})
    local = target == racksmod.LOCAL
    a = CFG.setdefault("agents", {})
    rt = {}

    def remote(script, env=""):
        cmd = env + script
        if local:
            return ["/bin/bash", "-c", cmd]
        return agentmod.ssh_base() + ["-o", "ConnectTimeout=10", target, "bash -c " + shlex_quote(cmd)]

    def stream(argv, timeout=1800):
        def on_line(text):
            text = _ANSI.sub("", text)
            if text.startswith("BB_ERR "):
                emit({"error": text[7:]})
            elif text.startswith("BB_RUNTIME="):
                rt["name"] = text.split("=", 1)[1].strip()
            elif text.startswith("BB_NEED "):
                what = text[8:].strip()
                if what in PLANE_NEEDS:
                    emit({"need": what, "command": PLANE_NEEDS[what]["command"], "why": PLANE_NEEDS[what]["why"]})
            else:
                say(text)
        return agentmod.run_streaming(argv, ROOT, on_line, run.cancel_event, timeout_s=timeout)

    def done(ok, msg=""):
        if not ok:
            emit({"error": msg}) if msg else None
        emit({"phase": "done", "exit": 0 if ok else 1, "killed": False})
        TRACE.log("agents", action="plane", target=target, ok=ok, error=(msg or "")[:300], run=run.id)
        return {"ok": ok, "error": msg or None}

    say("== %s: what agents need" % ("this computer" if local else target))
    code, _ = stream(remote(PLANE_CHECK), 600)
    if code != 0 or not rt.get("name"):
        return done(False)
    src = harness_source()
    if local:
        if not src:
            return done(False, "the agents harness is not on this computer (it ships with the app in a later release)")
        d = src
        say("harness: %s" % d)
    else:
        d = a.get("dir") if a.get("ssh") == target and a.get("dir") else "~/bytebunker-harness"
        if src:
            say("== the harness to %s:%s (from %s)" % (target, d, src))
            git = subprocess.run(["git", "-C", src, "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
            arch = subprocess.Popen(["git", "-C", src, "archive", "--format=tar", "HEAD"], stdout=subprocess.PIPE)
            put = subprocess.run(agentmod.ssh_base() + ["-o", "ConnectTimeout=10", target,
                                 "mkdir -p %s && tar -x -C %s" % (agentmod._rq(d), agentmod._rq(d))],
                                 stdin=arch.stdout, capture_output=True, text=True, timeout=300)
            arch.stdout.close()
            arch.wait()
            if put.returncode != 0:
                return done(False, "could not copy the harness there: %s" % racksmod._last(put.stderr))
            say("harness %s there (its config.yaml, if any, is kept)" % git.stdout.strip())
        else:
            say("no harness on this computer to bring: using the one already at %s:%s" % (target, d))
    say("== the sandbox")
    code, _ = stream(remote(PLANE_BUILD, "BB_DIR=%s; BB_RT=%s; " % (
        shlex_quote(d) if not d.startswith("~/") else '"$HOME"/' + shlex_quote(d[2:]), rt["name"])), 3600)
    if code != 0:
        return done(False, "the sandbox image did not build (exit %s): the log above says why" % code)
    if not local:
        res = agentmod.test_worker(target, d, "uv run")
        if not res.get("ok"):
            return done(False, "%s is not ready: %s" % (target, res.get("error") or ", ".join(
                k for k, v in (res.get("checks") or {}).items() if not v)))
    a.update(enabled=True, ssh="" if local else target, dir=d, python="uv run")
    a.setdefault("script", "scripts/run_master.py")
    save_config()
    BUS.publish("config", "agents", {"plane": target})
    say("agents work on %s now" % ("this computer" if local else target))
    return done(True)


def relink_rack(rack):
    """Re-read a paired rack's engine and monitor (rack pair --json) and keep them: after serving, so a
    monitor started then reaches Cluster without pairing again. Returns what the app uses now."""
    import subprocess
    try:
        out = subprocess.run(rack_cmd(["pair", "--json"], rack), capture_output=True, text=True, timeout=30)
        info = racksmod.parse(out.stdout)
        engine_url, monitor_url, _ = racksmod.find(rack["ssh"], info)
        racksmod.apply(CFG, {"target": rack["ssh"], "info": info, "engine_url": engine_url,
                             "monitor_url": monitor_url, "app": rack.get("app") or KEYS.app_name()},
                       gw_normalize=gwmod.normalize)
    except (racksmod.PairError, OSError, subprocess.SubprocessError, KeyError):
        return {}
    save_config()
    GW.refresh(force=True)
    MON.invalidate()
    r2 = rack_named(rack["name"]) or {}
    return {k: r2.get(k) for k in ("gateway", "monitor") if r2.get(k)}


def ensure_monitor(rack, say=lambda t: None):
    """The machine's rack monitor, started when it is not running: what puts it on Cluster (GPU, memory,
    the engine's served tokens). Not fatal: serving works without it."""
    import subprocess
    try:
        st = subprocess.run(rack_cmd(["monitor", "status"], rack), capture_output=True, text=True, timeout=60)
        if "not running" not in (st.stdout + st.stderr) and st.returncode == 0:
            return True
        say("== the rack monitor (Cluster's GPU, memory and served tokens)")
        up = subprocess.run(rack_cmd(["monitor", "up"], rack), capture_output=True, text=True, timeout=600)
        for ln in (up.stdout or "").splitlines()[-6:]:
            say(_ANSI.sub("", ln))
        if up.returncode != 0:
            say("the monitor did not start (rack monitor up there): %s" % racksmod._last(up.stderr or up.stdout))
            return False
        return True
    except (OSError, subprocess.SubprocessError) as e:
        say("the monitor did not start: %s" % e)
        return False


def connect_local(emit=None):
    """This computer's engine (and monitor) as a gateway, after rack up here."""
    import subprocess
    try:
        out = subprocess.run(rack_cmd(["pair", "--json"], racksmod.local_entry()), capture_output=True, text=True, timeout=30)
        info = racksmod.parse(out.stdout)
    except (racksmod.PairError, OSError, subprocess.SubprocessError) as e:
        if emit:
            emit({"line": "could not read this computer's engine to add it: %s" % e})
        return {}
    added = racksmod.apply_local(CFG, info, gw_normalize=gwmod.normalize)
    if added:
        save_config()
        GW.refresh(force=True)
        MON.invalidate()
        BUS.publish("config", "racks", {"racks": [x["name"] for x in racks()]})
    return added


def rack_overview(name=None, max_age=15):
    """recipes + status, cached briefly: `rack status` sshes to every node."""
    import subprocess
    r = rack_named(name)
    here = racksmod.local_entry()
    listing = [{"name": x["name"], "ssh": x["ssh"], "paired": x["paired"], "local": x["local"]} for x in racks()] + \
        [{"name": here["name"], "ssh": "", "paired": False, "local": True, "installed": here["installed"]}]
    if r is not None and r.get("local") and not r.get("installed"):
        return {"ok": False, "enabled": True, "name": r["name"], "local": True, "installed": False, "host": "this computer",
                "paired": False, "dir": "", "racks": listing,
                "platform": local_platform(), "app_version": app_version(),
                "error": "dgx-serve is not on this computer yet"}
    if r is None:
        if name:
            return {"ok": False, "enabled": bool(listing), "racks": listing, "error": "no rack named %r" % name}
        return {"ok": False, "enabled": False, "racks": [],
                "error": "no machine yet: Add a machine pairs one over ssh"}
    now = time.time()
    hit = _rack_cache.get(r["name"])
    if hit and now - hit[0] < max_age:
        return dict(hit[1], racks=listing)
    drives = r["paired"] or r.get("local")
    val = {"ok": True, "enabled": True, "name": r["name"], "host": "this computer" if r.get("local") else r["ssh"],
           "paired": r["paired"], "local": bool(r.get("local")), "installed": True, "app_version": app_version(),
           "dir": "" if drives else (r["dir"] or "~/dgx/dgx-spark-serve"), "racks": listing}
    try:
        out = subprocess.run(rack_cmd(["recipes", "--json"], r), capture_output=True, text=True, timeout=25)
        recipes = rack_recipes(out.stdout)
        try:
            d = json.loads(out.stdout)
            val.update(here=d.get("here") or "", budget_mb=d.get("budget_mb"))
        except ValueError:
            pass
        if not recipes and out.returncode != 0 and not drives:   # a rack from before 1.0 that refuses --json
            out = subprocess.run(rack_cmd(["recipes"], r), capture_output=True, text=True, timeout=25)
            recipes = rack_recipes(out.stdout)
        if not recipes and out.returncode != 0:
            raise RuntimeError(racksmod._last(out.stderr) or "rack recipes exited %d" % out.returncode)
        val["recipes"] = recipes
    except Exception as e:   # noqa: BLE001
        val.update(ok=False, error=str(e)[:200])
        _rack_cache[r["name"]] = (now, val)
        return val
    try:
        out = subprocess.run(rack_cmd(["status"], r), capture_output=True, text=True, timeout=45)
        val["status_raw"] = _ANSI.sub("", out.stdout + out.stderr).strip()[-4000:]
        val["serving"] = rack_serving(val["status_raw"])
    except Exception as e:   # noqa: BLE001
        val["status_raw"] = "rack status failed: %s" % str(e)[:120]
    if drives:
        try:
            out = subprocess.run(rack_cmd(["platform", "--json"], r), capture_output=True, text=True, timeout=25)
            p = json.loads(out.stdout)
            val["platform"] = {k: p.get(k) for k in ("platform", "description", "supported", "support_note", "model_budget_mb")}
        except Exception:   # noqa: BLE001  (an older rack, or no answer: the screen goes without)
            pass
        try:
            out = subprocess.run(rack_cmd(["version", "--json"], r), capture_output=True, text=True, timeout=25)
            try:
                v = json.loads(out.stdout) or {}
                val["rack_version"] = (str(v.get("version") or "") + (" " + v["commit"] if v.get("commit") else "")).strip()
            except ValueError:                                  # "rack 1.0.0 ..." from a rack without --json
                val["rack_version"] = (out.stdout.split() + ["", ""])[1] if out.stdout.startswith("rack ") else ""
        except Exception:   # noqa: BLE001
            pass
    _rack_cache[r["name"]] = (now, val)
    return val


def local_platform():
    """What this computer is, for the Deploy screen before dgx-serve is here."""
    import platform as _pf
    mem = 0
    try:
        if sys.platform == "darwin":
            import subprocess
            mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip() or 0)
        elif hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        mem = 0
    kind = {"darwin": "mac", "win32": "windows"}.get(sys.platform, "linux")
    name = {"mac": "Mac", "windows": "Windows PC", "linux": "Linux machine"}[kind]
    arch = _pf.machine()
    desc = "%s (%s%s)" % (name, arch, ", %d GiB memory" % round(mem / 2 ** 30) if mem else "")
    note = ""
    if kind == "mac" and arch != "arm64":
        note = "dgx-serve serves on Apple Silicon Macs, not on Intel ones"
    elif kind == "windows":
        note = "on Windows, dgx-serve runs inside WSL2 (Ubuntu 24.04), with the GPU passed through"
    return {"platform": kind, "description": desc, "supported": not note or kind == "windows", "support_note": note}


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


AUDIT_SKIP = ("/api/chat", "/api/handoff", "/api/tool-call", "/api/usage-event", "/api/rate", "/api/upload", "/api/video",
              "/api/update/check", "/api/netcheck")
AUDIT_CATEGORY = {"settings": "settings", "profiles": "settings", "update": "settings", "config": "settings",
                  "mcp": "tools", "plugins": "tools", "skills": "tools", "tools": "tools", "git": "access",
                  "gateways": "serving", "monitors": "serving", "racks": "serving", "rack": "serving",
                  "recipes": "serving", "machines": "serving", "engine": "serving",
                  "agents": "agents", "jobs": "runs", "workflows": "runs", "runs": "runs",
                  "approvals": "access", "terminal": "access",
                  "sessions": "data", "projects": "data", "archive": "data", "export": "data"}


def audit_action(method, path, body, query):
    """What an API request changed, as (category, action, target, detail); None when it changed
    nothing worth an audit line (chat traffic, tool calls, a session's own autosave)."""
    body = body if isinstance(body, dict) else {}
    act = str(body.get("action") or "")[:40]
    q1 = lambda k: (query.get(k) or [None])[0]
    if method == "DELETE":
        return ("data", "session.delete", q1("id"), {}) if path == "/api/sessions" else None
    if path in AUDIT_SKIP or path.endswith(("/turns", "/compress", "/tool-results")):
        return None
    if path == "/api/sessions" and act not in ("access", "rename", "set_project"):
        return None                                  # the chat saving itself
    if path == "/api/settings" and act == "update_seen":
        return None
    if path == "/api/rack" and act in ("show", "status", "logs", "recipes", "list", "ls"):
        return None                                  # reading what a machine serves changes nothing
    if path in ("/api/gateways", "/api/monitors") and act == "refresh":
        return None
    if path == "/api/git" and act == "probe":
        return None                                  # probing a URL changes nothing
    parts = [p for p in path.split("/")[2:] if p]
    if not parts:
        return None
    cat = AUDIT_CATEGORY.get(parts[0], "settings")
    if path.startswith("/api/runs/") and path.endswith("/cancel"):
        return "runs", "run.cancel", parts[1] if len(parts) > 1 else None, {}
    if path.startswith("/api/workflows/") and path.endswith("/run"):
        return "runs", "workflow.run", parts[1] if len(parts) > 1 else None, {"params": body.get("params")}
    if path == "/api/sessions" and act == "access":
        cat = "access"
    if path == "/api/audit":
        return "data", "audit.export", None, {}
    if path == "/api/approvals":
        return "access", "approval." + str(body.get("decision") or "answer")[:20], body.get("id"), {}
    if path == "/api/agents" and not act:
        act = "run" if body.get("goal") else "settings"
    if path in ("/api/gateways", "/api/monitors") and not act:
        act = "add"
    action = ".".join(parts).replace("-", "_") + ("." + act if act else "")
    target = next((str(body[k]) for k in ("id", "name", "target", "rack", "ssh", "session", "host") if body.get(k)), None) or q1("id")
    # secrets (token, key, password...) are masked to *** by audit.py's SECRET filter
    detail = {k: v for k, v in body.items() if k not in ("action", "messages", "data", "content")}
    return cat, action, target, detail


AUDIT_OUTCOMES = {"rack": "serving", "machine": "serving", "recipe": "serving", "agents": "agents",
                  "mcp": "tools", "update": "settings"}


def audit_from_trace(evt):
    """How the long jobs a request started ended (a deploy, a pairing, an install, a goal), and the
    training-data export, which is a GET: from the trace log into the audit log."""
    kind = evt.get("kind")
    if kind == "export":
        AUDIT.record("data", "export.training_data", None, True, examples=evt.get("examples"), query=evt.get("query"))
    elif kind == "agent_run":
        AUDIT.record("agents", "agents.run.finished", evt.get("id"), evt.get("exit") == 0 and not evt.get("killed"),
                     goal=(evt.get("goal") or "")[:300], host=evt.get("host"), mode=evt.get("mode"),
                     exit=evt.get("exit"), stopped=evt.get("killed") or None, via=evt.get("via"), ms=evt.get("ms"))
    elif kind in AUDIT_OUTCOMES and evt.get("run") and ("ok" in evt or "exit" in evt):
        f = {k: v for k, v in evt.items() if k not in ("ts", "kind", "action", "ok", "out", "output")}
        ok = evt["ok"] if "ok" in evt else evt.get("exit") == 0
        target = next((str(evt[k]) for k in ("target", "rack", "name", "id") if evt.get(k)), None)
        AUDIT.record(AUDIT_OUTCOMES[kind], "%s.%s.finished" % (kind, evt.get("action") or "run"), target, ok, **f)


TRACE.on_log = audit_from_trace


def agents_spec():
    """This run's models for the harness (--spec): master, thinking and fast,
    from the Agents screen's choices, else the app's roles (big, thinking,
    fast); each with its gateway's address and key. An engine on this
    machine's loopback is left to a remote worker's own config (it cannot
    reach it) but given to agents on this computer, which can; a gateway can
    name the address agents use (agents_url)."""
    a = CFG.get("agents") or {}
    remote = bool((a.get("ssh") or "").strip())
    roles = CFG.get("roles") or {}
    pick = {"master": a.get("master_model") or roles.get("big"),
            "thinking": a.get("thinking_model") or roles.get("thinking"),
            "fast": a.get("slave_model") or roles.get("fast")}
    out = {}
    for role, model in pick.items():
        if not model:
            continue
        try:
            mid, gw = GW.resolve(model) if model_served(model) else (model, None)
        except Exception:   # noqa: BLE001
            mid, gw = model, None
        r = {"model": mid}
        if gw:
            url = gw.get("agents_url") or gw["url"]
            if not remote or (urllib.parse.urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
                r["base_url"] = url
                if gw.get("key"):
                    r["api_key"] = gw["key"]
        if a.get(role + "_effort"):
            r["effort"] = a[role + "_effort"]
        if role == "master" and a.get("master_plan_effort") and (a.get("harness") or {}).get("plan"):
            r["plan_effort"] = a["master_plan_effort"]       # how hard it thinks while planning and spawning
        out[role] = r
    pool = minion_pool(a, pick.get("fast"), remote)
    if pool and len(pool) > 1 and (a.get("harness") or {}).get("pool"):
        # the router: the minions spread over these engines (the harness's engine_pool)
        fast = {"pool": pool}
        if out.get("fast", {}).get("effort"):
            fast["effort"] = out["fast"]["effort"]
        out["fast"] = fast
    court = (CFG.get("agents") or {}).get("court") or {}
    spec = {"version": 1}
    if out:
        spec["roles"] = out
    caps = a.get("harness") or {}
    if (court.get("roles") or court.get("concurrency") or court.get("access")) and caps.get("court"):
        spec["court"] = agentmod.court_for_run(court, caps, CFG.get("mcp_servers") or {})
    return spec if len(spec) > 1 else None


EFFORTS = ("off", "low", "medium", "high", "max")
ROLE_LABELS = {"master_model": "the master", "thinking_model": "the thinking roles",
               "slave_model": "the minions"}


def model_served(model):
    """Whether a gateway serves this model right now ("id", or "id@gateway" for one
    gateway's copy). A model nobody serves must never fall to the first gateway."""
    if not model:
        return True
    GW.refresh()
    ids = {m["id"] for m in GW.models}
    if model in ids or model in GW.model_map:
        return True
    GW.refresh(force=True)
    return model in {m["id"] for m in GW.models} or model in GW.model_map


def unserved_roles(a=None):
    """[(who, model)] for each role whose chosen model no gateway serves (a blank
    choice is the worker's own default, not checked here)."""
    a = a if a is not None else (CFG.get("agents") or {})
    out = [(ROLE_LABELS[k], a[k]) for k in ("master_model", "thinking_model", "slave_model")
           if (a.get(k) or "").strip() and not model_served(a[k].strip())]
    out += [("a minion engine", e["model"]) for e in (a.get("minion_pool") or [])
            if isinstance(e, dict) and e.get("model") and not model_served(e["model"])]
    return out


def unserved_worker_defaults(a=None):
    """[(who, model)] for each role this run leaves to the worker's config.yaml
    (no model chosen here, or a harness that takes no run spec) whose model no
    gateway serves: such a goal would fail at its first call to that role."""
    a = a if a is not None else (CFG.get("agents") or {})
    roles = CFG.get("roles") or {}
    takes_spec = bool((a.get("harness") or {}).get("spec"))
    chosen = {"master": a.get("master_model") or roles.get("big"),
              "thinking": a.get("thinking_model") or roles.get("thinking"),
              "fast": a.get("slave_model") or roles.get("fast")}
    left = [r for r in ("master", "thinking", "fast") if not (takes_spec and (chosen[r] or "").strip())]
    if not left:
        return []
    defaults = agentmod.worker_models(CFG)
    who = {"master": "the master (the worker's config.yaml)", "thinking": "the thinking roles (the worker's config.yaml)",
           "fast": "the minions (the worker's config.yaml)"}
    return [(who[r], defaults[r]) for r in left if defaults.get(r) and not model_served(defaults[r])]


def agents_ready(goal):
    """AgentConfigError unless a goal can start: agents set up, and every chosen
    model served somewhere now (a goal that starts anyway spends its workers on one
    error; measured: six on an HTTP 404)."""
    agentmod.build_command(CFG, goal)
    missing = unserved_roles() + unserved_worker_defaults()
    if missing:
        raise agentmod.AgentConfigError(
            "not started: " + "; ".join("%s' model %s is not served anywhere right now" % (w, m) for w, m in missing)
            + ". Pick served models in Agents > Settings > Models, or serve them on the Deploy screen."
            + ("" if ((CFG.get("agents") or {}).get("harness") or {}).get("spec") or not any(
                "config.yaml" in w for w, _ in missing) else
               " This worker's harness is too old to take the models picked here: Work here updates it."))


def minion_pool(a, first, remote):
    """The engines the minions spread over: the minion model's own, then each one
    added in Agents > Settings (agents.minion_pool: [{model, slots}], a model as
    "id" or "id@gateway" for the same model on another machine). An engine on this
    machine's loopback is left out for a remote worker, which cannot reach it."""
    entries = ([{"model": first, "slots": a.get("minion_slots") or 4}] if first else []) + \
        [e for e in (a.get("minion_pool") or []) if isinstance(e, dict) and e.get("model")]
    out, seen = [], set()
    for e in entries:
        if not model_served(e["model"]):
            continue                    # served nowhere: never the first gateway by default
        try:
            mid, gw = GW.resolve(e["model"])
        except Exception:   # noqa: BLE001
            continue
        if not gw:
            continue
        url = gw.get("agents_url") or gw["url"]
        if remote and (urllib.parse.urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1"):
            continue
        if (url, mid) in seen:
            continue
        seen.add((url, mid))
        try:
            slots = max(1, min(64, int(e.get("slots") or 4)))
        except (TypeError, ValueError):
            slots = 4
        gname = gw.get("name") or urllib.parse.urlparse(url).hostname or "engine"
        name = gname if gname not in [x["name"] for x in out] else "%s-%s" % (gname, mid)
        eng = {"name": name[:40], "model": mid, "base_url": url, "slots": slots}
        if gw.get("key"):
            eng["api_key"] = gw["key"]
        out.append(eng)
    return out


def agent_goal(run, emit, goal, via="app"):
    """One goal on the agents worker, its lines streamed through emit. Shared
    by the Agents screen and agent-kind jobs. Returns (result, lines)."""
    agents_ready(goal)                                   # AgentConfigError when not set up
    known = bool((CFG.get("agents") or {}).get("harness"))
    caps = agentmod.harness_caps(CFG)
    if caps and not known:
        save_config()                                     # asked once, remembered
    st = agentmod.status(CFG)
    cwd = None if st["mode"] == "ssh" else os.path.expanduser((CFG.get("agents") or {}).get("dir") or ".")
    spec = json.dumps(agents_spec()) if caps.get("spec") and agents_spec() else ""
    spec_path = None
    if spec and st["mode"] != "ssh":
        import tempfile
        fd, spec_path = tempfile.mkstemp(prefix="bb-spec-", suffix=".json")
        os.chmod(spec_path, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(spec)
    # Wrap up (the Master tab's button): an event the run's watchdog turns into the master's signal
    run.wrap_event = threading.Event()
    wrap_file = None
    if st["mode"] != "ssh":
        import tempfile
        fd, wrap_file = tempfile.mkstemp(prefix="bb-wrap-")
        os.close(fd)
    cmd = agentmod.build_command(CFG, goal, run_id=run.id, caps=caps,
                                 spec_len=len(spec.encode("utf-8")) if st["mode"] == "ssh" else 0,
                                 spec_path=spec_path, wrap_file=wrap_file)
    if not AGENT_SLOT.acquire(blocking=False):
        if spec_path:
            os.remove(spec_path)
        msg = "another agents goal is running; they run one at a time"
        emit({"error": msg})
        emit({"phase": "done", "exit": None, "killed": "busy"})
        return {"ok": False, "error": msg}, []
    started = time.time()
    captured = []   # full master output, stored so a past run can be reopened
    end = {}        # protocol 2: run_end says how the goal ended
    try:
        emit({"phase": "start", "id": run.id, "host": st["host"], "mode": st["mode"], "isolated": st["isolated"]})

        def file_job(spec):
            try:
                spec = dict(spec)
                spec["created_by"] = "agents:" + str(spec.get("created_by") or "master")[:30]
                job = JOBS.upsert(spec)
                TRACE.log("job", action="filed_by_agent", id=job["id"], name=job["name"], run=run.id,
                          schedule=job["schedule"], job_kind=job["kind"], by=spec["created_by"])
                BUS.publish("jobs", "saved", {"id": job["id"], "by": spec["created_by"]})
                emit({"job": {"id": job["id"], "name": job["name"], "schedule": job["schedule"], "kind": job["kind"]}})
                emit({"line": "%s> \U0001f5d3 filed job \"%s\" (%s) \u2014 see the Jobs screen" % (
                    (CFG.get("agents") or {}).get("master_name") or "Master", job["name"], jobmod.describe_schedule(job))})
            except (ValueError, KeyError, TypeError) as e:
                emit({"line": "console: could not file the job the master asked for: %s" % str(e)[:160]})

        def on_line(text):
            if text.startswith("BB_PROTO "):
                return
            if text.startswith("BB_EVENT "):
                # protocol 2: what happens, as data; the human lines carry on alongside
                try:
                    evt = json.loads(text[9:])
                except ValueError:
                    return
                if not isinstance(evt, dict):
                    return
                RUNS.emit(run, "agent_event", evt)
                if evt.get("type") == "job_filed" and isinstance(evt.get("job"), dict):
                    file_job(evt["job"])
                elif evt.get("type") == "master_round":
                    # where the master is: the Master tab's progress line
                    run.meta["progress"] = {k: evt.get(k) for k in ("round", "elapsed_s", "deadline_s", "spawned",
                                                                   "done", "running", "wrapping")}
                elif evt.get("type") == "wrap_up":
                    run.meta.setdefault("progress", {})["wrapping"] = True
                elif evt.get("type") == "run_end":
                    end.update(outcome=evt.get("outcome"), exit_code=evt.get("exit_code"), reason=evt.get("reason"))
                return
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
                # protocol 1: the master files recurring work as one structured line
                try:
                    file_job(json.loads(text[7:]))
                except ValueError as e:
                    emit({"line": "console: could not file the job the master asked for: %s" % str(e)[:160]})
                return
            emit({"line": text})

        try:
            code, killed = agentmod.run_streaming(cmd, cwd, on_line, run.cancel_event,
                                                  timeout_s=agent_run_timeout(CFG),
                                                  stdin_first=spec if st["mode"] == "ssh" else "",
                                                  wrap_event=run.wrap_event, wrap_file=wrap_file)
        except FileNotFoundError as e:
            emit({"error": "could not launch the harness: %s" % e})
            code, killed = -1, "error"
        except Exception as e:   # noqa: BLE001 - report anything to the client
            emit({"error": str(e)[:300]})
            code, killed = -1, "error"
        if end and end.get("outcome") != "answered":
            # say how it ended, so a goal with no answer is never just silence
            emit({"line": "%s> \u26a0\ufe0f no answer: the goal %s%s" % (
                (CFG.get("agents") or {}).get("master_name") or "Master", end.get("outcome") or "ended",
                " (%s)" % end["reason"] if end.get("reason") else "")})
        emit({"phase": "done", "exit": code, "killed": killed})
        TRACE.log("agent_run", id=run.id, goal=goal[:8000], host=st["host"], mode=st["mode"],
                  isolated=st["isolated"], exit=code, killed=killed or False, via=via,
                  lines=len(captured), output=captured, ms=int((time.time() - started) * 1000))
        ok = code == 0 if not end else end.get("outcome") == "answered"
        error = None if ok else (
            "the goal ended %s%s" % (end.get("outcome"), ": " + end["reason"] if end.get("reason") else "") if end
            else "the harness exited %s%s" % (code, " (%s)" % killed if killed else ""))
        return ({"ok": ok, "exit": code, "killed": killed or False, "lines": len(captured), "error": error,
                 "outcome": end.get("outcome")}, captured)
    finally:
        AGENT_SLOT.release()
        for path in (spec_path, wrap_file):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


def run_job(job, run):
    """One run of a job, inside its run: a chat turn on the runner (no
    session file, nobody to ask), or a goal for the agents. A job may name a
    workflow, which gives it its prompt, kind and profile. Returns {output,
    hops, tokens}; raises when the job failed."""
    kind, prompt, profile = job.get("kind"), job.get("prompt") or "", job.get("profile") or None
    if job.get("workflow"):
        wf, prompt = render_workflow(job["workflow"], job.get("params") or {})
        kind = "agent" if wf.get("kind") == "agents" else "chat"
        profile = wf.get("profile") or profile
    if kind == "agent":
        if not agentmod.status(CFG).get("enabled"):
            raise RuntimeError("agents are not set up: connect a worker on the Agents screen")
        res, lines = agent_goal(run, lambda obj: RUNS.emit(run, "out", obj), prompt, via="job:" + job["id"])
        if not res.get("ok"):
            raise RuntimeError((res.get("error") or "failed") + ("\n" + "\n".join(lines[-20:]) if lines else ""))
        return {"output": "\n".join(lines), "hops": None, "tokens": None}
    system = "\n\n".join(x for x in (
        job.get("system") or "",
        "You are running as a scheduled job named %r at %s. There is no human in the loop: do the task with the "
        "tools you have, then write the result as your final message." % (job["name"], time.strftime("%Y-%m-%d %H:%M"))) if x)
    req = apply_profile({
        "text": prompt, "model": job.get("model") or None, "profile": profile, "skills": job.get("skills") or [],
        "system": system, "tools": job.get("tools", True), "max_hops": int(job.get("max_hops") or 12),
        "params": {"max_tokens": 8000, "temperature": 0.3}, "auto_compress": False,
        "unattended": True, "ephemeral": True, "interactive": False, "source": "job", "job": job["id"]})
    req["model"] = resolve_model(req.get("model")) or None
    out = {}
    res = runner().turn(run, lambda t, d=None: RUNS.emit(run, t, d), "job-" + job["id"], req, out=out)
    msg = out.get("message") or {}
    if msg.get("error"):
        raise RuntimeError(msg["error"])
    model = msg.get("model") or ""
    return {"output": msg.get("content") or "", "hops": len(msg.get("hops") or []) + 1, "tokens": res.get("tokens"),
            "model": model, "gateway": GW.model_map.get(model)}


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
    atomically so a crash mid-write cannot leave the console unbootable. A
    file changed on disk since this server last read it (an edit it could
    not load) is kept aside first, never silently replaced."""
    path = CONFIG_PATH
    tmp = path + ".tmp"
    with _LOCK:
        st = _stamp(path)
        if st is not None and st != _CFG_STAMP[0]:
            kept = _set_aside(path, "replaced")
            CONFIG_WARNINGS.append("config.json was changed outside the app in a way it could not read; "
                                   "that version is kept as %s." % os.path.basename(kept))
        with open(tmp, "w") as f:
            json.dump(CFG, f, indent=2)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)      # gateway keys live here; a replace must not widen it
        except OSError:
            pass
        os.replace(tmp, path)
        _CFG_STAMP[0] = _stamp(path)


def config_fresh():
    """Pick up an edit made to config.json while the server runs. Called
    before anything changes the config, so the change lands on top of the
    edit. A file that does not parse (half-saved, or a typo) is left for
    save_config to keep aside. Returns True when it reloaded."""
    st = _stamp(CONFIG_PATH)
    if st is None or st == _CFG_STAMP[0]:
        return False
    with _LOCK:
        if _stamp(CONFIG_PATH) == _CFG_STAMP[0]:
            return False
        try:
            disk = _read_config_file(CONFIG_PATH)
        except (OSError, ValueError):
            return False
        fresh = _fresh_config(disk)
        migrate_config(fresh)
        for k in [k for k in CFG if k not in fresh]:
            CFG.pop(k, None)
        CFG.update(fresh)              # the same dict: every registry holding it sees the edit
        _CFG_STAMP[0] = _stamp(CONFIG_PATH)
    GW.at = 0                          # gateways may have changed: list models afresh
    TRACE.log("config", action="reloaded", reason="edited outside the app")
    BUS.publish("config", "reloaded")
    mcp_reload(wait=False)
    return True


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
    target, rack = CFG.get("netcheck_ssh", ""), (racks() or [None])[0]
    if not target and not rack:
        return {"ok": False, "error": "no rack to ask: Add a machine on Deploy (or set netcheck_ssh)"}
    with _NET_LOCK:
        if not fresh and _NET["result"] and time.time() - _NET["at"] < 300:
            return _NET["result"]
        import subprocess
        try:
            # the first rack's own rack net; a netcheck_ssh from before racks keeps its old command
            cmd = rack_cmd(["net"], rack) if rack else \
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, "cd dgx/dgx-spark-serve && ./rack net"]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
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


# -------------------------------------------------------------------- help --
# The Help center: guides in docs/help/*.md, front matter first (id, title,
# screen, order, summary, platforms), shipped with the app, read offline.
HELP_DIR = os.path.join(ROOT, "docs", "help")


def _front_matter(text):
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                k, sep, v = line.partition(":")
                if sep:
                    meta[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return meta, body


def help_guides():
    out = []
    try:
        names = sorted(os.listdir(HELP_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(HELP_DIR, name), encoding="utf-8") as f:
                meta, body = _front_matter(f.read())
        except OSError:
            continue
        gid = meta.get("id") or name[:-3]
        out.append({"id": gid, "title": meta.get("title") or gid, "screen": meta.get("screen") or "",
                    "summary": meta.get("summary") or "", "order": _int(meta.get("order"), 100),
                    "platforms": [p.strip() for p in (meta.get("platforms") or "").split(",") if p.strip()],
                    "body": body})
    out.sort(key=lambda g: (g["order"], g["title"]))
    return out


def setup_checklist():
    """What is set up, measured, each with the one thing to do next."""
    import shutil
    GW.refresh()
    gws = GW.gateways()
    up = [g for g in gws if (GW.status.get(g["name"]) or {}).get("ok")]
    try:
        tools = mcp_host().status
    except Exception:   # noqa: BLE001
        tools = {}
    ready_tools = [n for n, st in tools.items() if st.get("state") == "ready"]
    bindir = os.path.join(os.environ.get("LOCALAPPDATA") or "", "ByteBunker", "bin") if os.name == "nt" \
        else os.path.expanduser("~/.local/bin")
    has_bb = any(os.path.exists(os.path.join(bindir, n)) for n in ("bb", "bytebunker", "bb.cmd", "bytebunker.cmd")) \
        or bool(shutil.which("bytebunker"))
    items = [
        {"id": "gateway", "title": "A model server answers", "ok": bool(up),
         "detail": ("%d of %d answer" % (len(up), len(gws))) if gws else "none added yet",
         "action": {"label": "Gateways", "screen": "gateways"}, "help": "gateways"},
        {"id": "models", "title": "A model to talk to", "ok": bool(GW.models),
         "detail": "%d model%s" % (len(GW.models), "" if len(GW.models) == 1 else "s"),
         "action": {"label": "Models", "screen": "models"}, "help": "gateways"},
        {"id": "rack", "title": "A machine to serve on", "optional": True,
         "ok": any(r["paired"] for r in racks()) or racksmod.local_entry()["installed"],
         "detail": ", ".join((["this computer"] if racksmod.local_entry()["installed"] else []) +
                             [r["name"] for r in racks() if r["paired"]]) or (
             "%s, reached with your own ssh: pair it to drive it with this app's key" % racks()[0]["name"] if racks()
             else "dgx-serve on this computer, or a machine you add"),
         "action": {"label": "Deploy", "screen": "recipes"}, "help": "recipes"},
        {"id": "monitor", "title": "A rack monitor", "optional": True, "ok": bool(MON.list()),
         "detail": "%d monitor%s" % (len(MON.list()), "" if len(MON.list()) == 1 else "s"),
         "action": {"label": "Cluster", "screen": "cluster"}, "help": "cluster"},
        {"id": "tools", "title": "Tools for the model", "optional": True, "ok": bool(ready_tools),
         "detail": ", ".join(ready_tools) or "no MCP server running",
         "action": {"label": "MCP", "screen": "mcp"}, "help": "mcp"},
        {"id": "cli", "title": "bb in your terminal", "optional": True, "ok": has_bb,
         "detail": "installed" if has_bb else "not installed",
         "action": {"label": "Settings", "screen": "settings"}, "help": "cli"},
        {"id": "agents", "title": "An agents worker", "optional": True, "ok": bool((CFG.get("agents") or {}).get("enabled")),
         "detail": (CFG.get("agents") or {}).get("ssh") or "not connected",
         "action": {"label": "Agents", "screen": "agents"}, "help": "agents"},
    ]
    if CONFIG_WARNINGS:
        items.insert(0, {"id": "config", "title": "config.json", "ok": False, "detail": CONFIG_WARNINGS[-1],
                         "action": {"label": "Settings", "screen": "settings"}, "help": "settings"})
    return items


# ------------------------------------------------------------------ runner --
# Chat turns run on the server (runner.py): the app, bb, jobs and workflows
# share one engine, one session file, one trace log and one usage ledger.
FACTS = factsmod.ModelFacts(os.path.join(DATA, "model_facts.json"))
APPROVALS = approvalsmod.Approvals(BUS)
CLIENT_RESULTS = runnermod.ClientResults()
_RUNNER = []


def tls_info():
    """Where HTTPS gets its trusted certificates, and how many: what broke the update check (CI checks it)."""
    import ssl
    paths = ssl.get_default_verify_paths()
    try:
        n = ssl.create_default_context().cert_store_stats().get("x509_ca", 0)
    except Exception:   # noqa: BLE001
        n = 0
    return {"cafile": os.environ.get("SSL_CERT_FILE") or paths.cafile or paths.openssl_cafile, "ca_certs": n}


# ------------------------------------------------- a chat's command sandbox --
ACCESS_RANK = {"local": 0, "web": 1, "full": 2}


def terminal_tools():
    """The flat names of the built-in terminal's run tool (one per terminal server)."""
    out = set()
    for n, sv in (CFG.get("mcp_servers") or {}).items():
        args = sv.get("args") or []
        if args and os.path.basename(str(args[0])) == "mcp_terminal.py":
            out.add(n + "__run")
    return out


class _CallRun:
    """A Playground tool call has no server run; an approval still needs one to wait on."""
    def __init__(self):
        self.id = "call-" + uuid.uuid4().hex[:10]
        self.cancelled = False


def note_access(sid, used=None, current=None):
    """Remember a chat's mode, and the most it ever had (the Sessions dot)."""
    if not sid or not sessmod._ID.match(str(sid)) or SESSIONS.get(sid) is None:
        return
    sess = SESSIONS.get(sid) or {}
    fields = {}
    if current in cmdbox.MODES and sess.get("access") != current:
        fields["access"] = current
    top = sess.get("max_access") or "local"
    for m in (used, current):
        if m in ACCESS_RANK and ACCESS_RANK[m] > ACCESS_RANK.get(top, 0):
            top = m
    if top != (sess.get("max_access") or "local"):
        fields["max_access"] = top
    if fields:
        BUS.publish("sessions", "updated", SESSIONS.append(sid, "meta", **fields))


def terminal_call(args, sid, access, ask):
    """A terminal command in the chat's mode: ((error_text, True), None) when you said no,
    else (None, args for the terminal). ask(args_shown, kind) -> allow | always | deny."""
    args = {k: v for k, v in dict(args or {}).items() if not str(k).startswith("__bb_")}   # never from the model
    sess = SESSIONS.get(sid) if sid and sessmod._ID.match(str(sid)) else None
    mode = access if access in cmdbox.MODES else ((sess or {}).get("access") or "local")
    wants_out = bool(args.pop("outside_sandbox", False))
    ok_out, how = False, "full access" if mode == "full" else None
    if mode != "full" and (wants_out or cmdbox.kind() is None):
        if (sess or {}).get("outside_always"):
            ok_out, how = True, "allowed every time in this chat"
        else:
            decision = ask({"command": args.get("command"), "mode": mode,
                            "why": "asks to run outside the sandbox" if wants_out else
                                   "this computer has no sandbox for commands"}, "outside")
            if decision == "always":
                ok_out, how = True, "allowed every time in this chat (from now on)"
                if sess is not None:
                    SESSIONS.append(sid, "meta", outside_always=True)
            elif decision == "allow":
                ok_out, how = True, "allowed once"
            else:
                note_access(sid, current=mode)
                AUDIT.record("access", "command.outside_sandbox", sid, False,
                             command=str(args.get("command") or "")[:1000], mode=mode, how="not allowed")
                return ("you did not allow this command outside the sandbox", True), None
    if how:
        # every command that runs without the sandbox, and why it could
        AUDIT.record("access", "command.outside_sandbox", sid, True,
                     command=str(args.get("command") or "")[:1000], mode=mode, how=how)
    args["__bb_mode"], args["__bb_outside_ok"] = mode, ok_out
    note_access(sid, used="full" if ok_out or mode == "full" else mode, current=mode)
    return None, args


def always_allow(name):
    """"Always allow": the tool runs without asking from now on (config tool_policy)."""
    config_fresh()
    CFG.setdefault("tool_policy", {})[name] = "allow"
    save_config()


def record_usage(body, **extra):
    evt = sanitize_usage(body)
    if evt:
        evt.update({k: v for k, v in extra.items() if v})
        append_usage(evt)


def install_cli():
    """Put bb (and bytebunker, its long name) on this user's PATH: a small
    script that runs this app with --cli, or bb.py from a checkout. bb is
    also Babashka's command: a bb that is not ours is left alone and only
    bytebunker is written."""
    import shutil
    import stat
    frozen = getattr(sys, "frozen", False)
    if os.name == "nt":
        bindir = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ByteBunker", "bin")
        exe = sys.executable
        twin = os.path.join(os.path.dirname(exe), "ByteBunker-cli.exe")    # the console build: a terminal needs it
        if frozen and os.path.exists(twin):
            exe = twin
        target = ('"%s" --cli %%*' % exe) if frozen else ('"%s" "%s" %%*' % (exe, os.path.join(ROOT, "bb.py")))
        body, ext = "@echo off\r\nrem ByteBunker's command line (written by the app)\r\n" + target + "\r\n", ".cmd"
    else:
        bindir = os.path.expanduser("~/.local/bin")
        target = ('"%s" --cli "$@"' % sys.executable) if frozen else ('"%s" "%s" "$@"' % (sys.executable, os.path.join(ROOT, "bb.py")))
        body, ext = "#!/bin/sh\n# ByteBunker's command line (written by the app)\nexec " + target + "\n", ""
    os.makedirs(bindir, exist_ok=True)
    written, skipped = [], []
    for name in ("bytebunker", "bb"):
        path = os.path.join(bindir, name + ext)
        other = shutil.which(name)
        ours = False
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                ours = "ByteBunker" in f.read(4000)
        if name == "bb" and other and os.path.realpath(other) != os.path.realpath(path) and not ours:
            skipped.append("bb (%s is already another program's)" % other)
            continue
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(body)
        if os.name != "nt":
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written.append(path)
    on_path = any(os.path.normcase(os.path.realpath(p)) == os.path.normcase(os.path.realpath(bindir))
                  for p in (os.environ.get("PATH") or "").split(os.pathsep) if p)
    hint = None
    if not on_path:
        hint = ('add %s to your PATH (System settings, Environment Variables)' % bindir if os.name == "nt"
                else 'add it to your PATH: echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.%src' % (
                    "zsh" if "zsh" in (os.environ.get("SHELL") or "") else "bash"))
    return {"ok": True, "written": written, "skipped": skipped, "dir": bindir, "on_path": on_path, "hint": hint}


# Roles: what each kind of work runs on, set once in Settings. A model
# written as role:fast (in a profile, a workflow, a job, bb -m) resolves to
# the model the role names, and only while a gateway lists it.
ROLES = ("big", "fast", "vision", "thinking")


def resolve_model(model):
    """role:NAME to the model it names; anything else as it is. ValueError
    when the role is unset, or its model is not served right now."""
    if not isinstance(model, str) or not model.startswith("role:"):
        return model
    role = model[5:]
    if role not in ROLES:
        raise ValueError("there is no %r role (there are %s)" % (role, ", ".join(ROLES)))
    mid = (CFG.get("roles") or {}).get(role)
    if not mid:
        raise ValueError("the %s role names no model yet: set it in Settings, Roles" % role)
    GW.refresh()
    if not any(m["id"] == mid for m in GW.models):
        raise ValueError("the %s role is %s, and no gateway serves it right now" % (role, mid))
    return mid


def default_model():
    """No model named: the one last used that a gateway still lists, else the first listed."""
    GW.refresh()
    return last_used_model() or (GW.models[0]["id"] if GW.models else "")


# Profiles: named sets of turn settings, chosen in the app or with bb -p.
# A request's own settings win over its profile's.
DEFAULT_PROFILES = {
    "Default": {"description": "the model's own defaults"},
    "Fast": {"description": "no thinking, shorter answers, few tool hops",
             "effort": "off", "max_hops": 8, "params": {"max_tokens": 4096}},
    "Deep": {"description": "the most thinking the model offers, long answers",
             "effort": "max", "params": {"max_tokens": 32768}},
}
PROFILE_KEYS = ("model", "effort", "params", "system", "skills", "tools", "max_hops", "auto_compress", "tool_policy")


def profiles():
    out = {k: dict(v, builtin=True) for k, v in DEFAULT_PROFILES.items()}
    for name, p in (CFG.get("profiles") or {}).items():
        if isinstance(p, dict) and isinstance(name, str):
            base = dict(out.get(name) or {})
            base.update(p)
            base["builtin"] = name in DEFAULT_PROFILES
            base["saved"] = True             # in config: a built-in one edited, or the user's own
            out[name] = base
    return out


def apply_profile(body):
    """The request's settings over its profile's. ValueError for an unknown profile."""
    name = body.get("profile") or CFG.get("default_profile") or "Default"
    prof = profiles().get(name)
    if prof is None:
        raise ValueError("no profile named %r (bb has: %s)" % (name, ", ".join(sorted(profiles()))))
    out = {k: v for k, v in prof.items() if k in PROFILE_KEYS}
    params = dict(prof.get("params") or {})
    params.update(body.get("params") or {})
    out.update({k: v for k, v in body.items() if v not in (None, "") and k != "params"})
    out["params"] = params
    out["profile"] = name
    return out


# Workflows: a named prompt with {{param}} placeholders, a kind (a chat
# turn, or a goal for the agents), a profile and an optional folder (bb
# works there). Run from the app, from bb run, or by a job.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


def workflows():
    return {n: w for n, w in (CFG.get("workflows") or {}).items() if isinstance(w, dict) and isinstance(n, str)}


def workflow_params(wf):
    """Declared params, plus any placeholder the prompt uses without declaring it."""
    declared = [p for p in wf.get("params") or [] if isinstance(p, dict) and p.get("name")]
    names = [p["name"] for p in declared]
    for k in _PLACEHOLDER.findall(wf.get("prompt") or ""):
        if k not in names:
            names.append(k)
            declared.append({"name": k})
    return declared


def render_workflow(name, params):
    """(workflow, prompt). ValueError names what is missing and how to give it."""
    wf = workflows().get(name)
    if wf is None:
        raise ValueError("no workflow named %r%s" % (name, (" (there are: %s)" % ", ".join(sorted(workflows())))
                                                     if workflows() else ""))
    declared = {p["name"]: p for p in workflow_params(wf)}
    values = {str(k): str(v) for k, v in (params or {}).items() if v is not None}
    missing = []

    def fill(m):
        k = m.group(1)
        if k in values:
            return values[k]
        d = declared.get(k, {}).get("default")
        if d not in (None, ""):
            return str(d)
        missing.append(k)
        return m.group(0)
    text = _PLACEHOLDER.sub(fill, wf.get("prompt") or "")
    if missing:
        ks = sorted(set(missing))
        raise ValueError("workflow %s needs %s (bb run %s %s)" % (
            name, ", ".join(ks), name, " ".join("-p %s=..." % k for k in ks)))
    return wf, text


# Projects: related work kept together. A project is a name, a folder (where
# bb works, and where a chat opened in a terminal starts; made when missing),
# instructions every chat in it follows, and a default profile. Its chats,
# workflows and runs carry its id, and bb joins the project whose folder it
# runs in.
def projects():
    return {pid: p for pid, p in (CFG.get("projects") or {}).items() if isinstance(p, dict) and isinstance(pid, str)}


def _realdir(path):
    return os.path.realpath(os.path.expanduser(str(path))) if path else None


def project_for_folder(cwd):
    """The project whose folder holds cwd: the innermost one, when folders nest."""
    here = _realdir(cwd)
    if not here:
        return None
    best = None
    for pid, p in projects().items():
        root = _realdir(p.get("folder"))
        if root and (here == root or here.startswith(root.rstrip(os.sep) + os.sep)):
            if best is None or len(root) > len(best[1]):
                best = (pid, root)
    return best[0] if best else None


def project_named(ref):
    """A project by its id or its name; ValueError names the ones there are."""
    ps = projects()
    if ref in ps:
        return ref
    hit = [pid for pid, p in ps.items() if (p.get("name") or "").lower() == str(ref).lower()]
    if hit:
        return hit[0]
    raise ValueError("no project named %r (there are: %s)" % (
        ref, ", ".join(sorted(p.get("name") or k for k, p in ps.items())) or "none yet"))


def project_of(body):
    """The project a piece of work belongs to: the one named, else the one its folder is in."""
    if body.get("project"):
        return project_named(body["project"])
    return project_for_folder(body.get("cwd"))


def project_system(pid):
    p = projects().get(pid) or {}
    text = (p.get("instructions") or "").strip()
    return "# Project: %s\n\n%s" % (p.get("name") or pid, text) if text else ""


def project_list():
    counts, last = {}, {}
    for s in SESSIONS.list():
        pid = s.get("project")
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
            last[pid] = max(last.get(pid, 0), s.get("updated") or 0)
    out = []
    for pid, p in projects().items():
        out.append(dict(p, id=pid, chats=counts.get(pid, 0), updated=last.get(pid) or p.get("created") or 0,
                        workflows=sorted(n for n, w in workflows().items() if w.get("project") == pid)))
    return sorted(out, key=lambda x: -(x["updated"] or 0))


def open_terminal(folder, session=None, project=None):
    """bb in a terminal window on this computer, in folder, continuing session
    (or a new chat in project). Where there is no window to open (a server
    reached from a browser), the command to run instead."""
    import shlex
    import subprocess
    folder = os.path.realpath(os.path.expanduser(folder or "~"))
    os.makedirs(folder, exist_ok=True)                 # a project folder that is not there yet is made
    args = ["chat"] + (["-P", project] if project and not session else []) + ([session] if session else [])
    shown = ("cd %s; bb %s" if sys.platform == "win32" else "cd %s && bb %s") % (shlex.quote(folder), " ".join(args))
    if not os.environ.get("BYTEBUNKER_DESKTOP") or sys.platform not in ("darwin", "win32"):
        return {"opened": False, "command": shown, "folder": folder}
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if sys.platform == "win32":
            twin = os.path.join(os.path.dirname(exe), "ByteBunker-cli.exe")
            exe = twin if os.path.exists(twin) else exe
        cli = [exe, "--cli"]
    else:
        cli = [sys.executable, os.path.join(ROOT, "bb.py")]
    if sys.platform == "darwin":
        # a .command file opens in the user's terminal app, with no permission to ask for
        d = os.path.join(os.path.dirname(CONFIG_PATH), "terminal")
        os.makedirs(d, exist_ok=True)
        script = os.path.join(d, "bb-%s.command" % (session or project or "chat"))
        with open(script, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n# opened from ByteBunker\nexport BYTEBUNKER_DATA=%s BYTEBUNKER_CONFIG=%s\ncd %s || exit 1\nexec %s\n" % (
                shlex.quote(DATA), shlex.quote(CONFIG_PATH), shlex.quote(folder), " ".join(shlex.quote(a) for a in cli + args)))
        os.chmod(script, 0o700)
        subprocess.Popen(["open", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        q = lambda s: "'" + str(s).replace("'", "''") + "'"
        ps = ("$host.UI.RawUI.WindowTitle = 'ByteBunker'; $env:BYTEBUNKER_DATA=%s; $env:BYTEBUNKER_CONFIG=%s; "
              "Set-Location -LiteralPath %s; & %s") % (q(DATA), q(CONFIG_PATH), q(folder), " ".join(q(a) for a in cli + args))
        # its own console window (CREATE_NEW_CONSOLE); the app's no-window default must not apply here
        subprocess.Popen(["powershell", "-NoExit", "-Command", ps], creationflags=0x00000010)
    return {"opened": True, "command": shown, "folder": folder}


def start_turn(sid, body, source):
    """A chat turn as a server-owned run: (run, sid). Busy when the session
    already has one going; ValueError for an unknown profile."""
    if sid == "new":
        sid = new_session_id()
    body = dict(body, source=source)
    pid = project_of(body)
    if pid:
        body["project"] = pid
        if not body.get("profile") and projects()[pid].get("profile"):
            body["profile"] = projects()[pid]["profile"]
    body = apply_profile(body)
    if pid and project_system(pid):
        body["system"] = "\n\n---\n\n".join(x for x in (project_system(pid), (body.get("system") or "").strip()) if x)
    body["model"] = resolve_model(body.get("model")) or None
    run = RUNS.start("chat", source, lambda run: runner().turn(run, lambda t, d=None: RUNS.emit(run, t, d), sid, body),
                     title=(body.get("title") or body.get("text") or "").strip()[:80] or "turn",
                     key="session:" + sid, meta={"session": sid, "model": body.get("model") or "", "what": "turns",
                                                 "workflow": body.get("workflow") or None, "project": pid})
    return run, sid


def start_goal(goal, source, title=None, meta=None):
    """An agents goal as a server-owned run (one at a time)."""
    agents_ready(goal)                             # AgentConfigError: not set up, or a model not served
    return RUNS.start("agents", source, lambda run: agent_goal(run, lambda obj: RUNS.emit(run, "out", obj), goal)[0],
                      title=title or goal.strip()[:120], key="agents", meta=meta or {})


def runner():
    if not _RUNNER:
        import types
        _RUNNER.append(runnermod.Runner(types.SimpleNamespace(
            registry=GW, caps_for=caps_for, facts=FACTS, mcp=mcp_host, trace=TRACE,
            record_usage=record_usage, sessions=SESSIONS,
            on_session=lambda summary: BUS.publish("sessions", "updated", summary),
            skill_bodies=lambda names: skill_catalog().bodies(names),
            write_archive=write_archive, uploads_root=uploads_root, engine_stats=engine_stats,
            default_model=default_model, approvals=APPROVALS, client_results=CLIENT_RESULTS,
            tool_policy=lambda: CFG.get("tool_policy") or {}, always_allow=always_allow,
            terminal_tools=terminal_tools, terminal_call=terminal_call)))
    return _RUNNER[0]


def new_session_id():
    n, digits, out = int(time.time() * 1000), "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return "s-" + out + uuid_hex(2)


def uuid_hex(n):
    import uuid
    return uuid.uuid4().hex[:n]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # Every request goes through _safe: a bug in a handler answers 500 with
    # what went wrong (and lands in the trace log) instead of dropping the
    # connection, which a client can only report as "the server stopped".
    def send_response(self, code, message=None):
        self._responded = True
        self._code = code
        super().send_response(code, message)

    def _safe(self, handler):
        self._responded = False
        self._code, self._audit_body, self._audit_reply = None, None, None
        IDLE["last"] = time.time()
        auditmod.set_actor(self._client())
        try:
            handler()
            self._audit_request()
        except (BrokenPipeError, ConnectionResetError):
            pass                                   # the client left
        except Exception as e:   # noqa: BLE001
            import traceback
            where = traceback.extract_tb(e.__traceback__)[-1]
            TRACE.log("error", method=self.command, path=urllib.parse.urlparse(self.path).path,
                      error="%s: %s" % (type(e).__name__, str(e)[:300]),
                      at="%s:%d" % (os.path.basename(where.filename), where.lineno))
            traceback.print_exc()
            if not self._responded:
                try:
                    self._json({"error": "internal error in the ByteBunker server (%s: %s). It is in the trace "
                                         "log; please report it." % (type(e).__name__, str(e)[:200])}, 500, close=True)
                except OSError:
                    pass
            self.close_connection = True

    def do_GET(self):
        self._safe(self._do_GET)

    def do_POST(self):
        self._safe(self._do_POST)

    def do_DELETE(self):
        self._safe(self._do_DELETE)

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
        if not self._token_ok():
            self._drain()
            self._json({"error": "this app's API wants its access token: open ByteBunker from its window, or run "
                                 "`bb open` on this computer", "locked": True}, 401, close=True)
            return False
        return True

    def _token_ok(self):
        """Other accounts on this computer reach 127.0.0.1 too: the API answers
        only with this server's token (instance.json, 0600), sent as X-BB-Token
        by bb and the built-in tools, or as the cookie a one-time link set."""
        token = local_token()
        path = urllib.parse.urlparse(self.path).path
        if not token or not path.startswith("/api/") or path in OPEN_API:
            return True
        sent = self.headers.get("X-BB-Token") or ""
        if not sent:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except http.cookies.CookieError:
                pass
            sent = jar[TOKEN_COOKIE].value if TOKEN_COOKIE in jar else ""
        return hmac.compare_digest(sent.encode(), token.encode())

    def _handoff(self, query):
        """GET /handoff?code=: a one-time link from the window or `bb open`
        becomes this browser's cookie, then the app."""
        code = (urllib.parse.parse_qs(query).get("code") or [""])[0]
        token = local_token()
        if not token or not take_handoff(code):
            body = ("<!doctype html><meta charset=utf-8><title>ByteBunker</title><body style=\"font:15px system-ui;"
                    "padding:40px\"><h1>This link has been used or has expired</h1><p>Open ByteBunker from its "
                    "window, or run <code>bb open</code> on this computer for a new one.</p>").encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (TOKEN_COOKIE, token))
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _audit_request(self):
        """Every change made through the API, one audit line: what was asked, by whom, and whether it
        worked. Chat traffic, tool calls and the app's own bookkeeping are not changes and stay out."""
        if self.command not in ("POST", "DELETE") or self._code is None:
            return
        u = urllib.parse.urlparse(self.path)
        what = audit_action(self.command, u.path, self._audit_body, urllib.parse.parse_qs(u.query))
        if not what:
            return
        cat, action, target, detail = what
        ok = self._code < 400
        if not ok and self._audit_reply:
            detail["error"] = str(self._audit_reply.get("error") or "")[:300]
        try:
            AUDIT.record(cat, action, target, ok, **detail)
        except OSError as e:
            print("audit log: %s" % e, file=sys.stderr)

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
        if code >= 400 and isinstance(obj, dict):
            self._audit_reply = obj
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
        with _LISTEN_LOCK:
            _LISTENING[0] += 1
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
        finally:
            with _LISTEN_LOCK:
                _LISTENING[0] -= 1

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
        saw_done, finished = [False], [False]

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
                    # the run's last event: done_event is set a moment after it is published,
                    # and a stream that checked in between would wait out another 15 s
                    finished[0] = finished[0] or (e["topic"] == "runs" and e["type"] == "finished")
            self.wfile.flush()
            return sent

        try:
            sent = send(past, after)
            pos = max(sent, after)
            while run is not None and not finished[0] and not run.done_event.is_set() and not STOPPING.is_set():
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

    def _turn(self, path, body):
        """POST /api/sessions/<id>/turns: one chat turn, run on the server.
        202 and the run's id at once; the turn streams on
        /api/runs/<id>/events and is saved to the session when it ends.
        <id> "new" starts a session. One turn per session at a time.
        POST /api/sessions/<id>/compress: fold its older turns into a summary."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        sid, _, what = path[len("/api/sessions/"):].rpartition("/")
        if sid != "new" and not sessmod._ID.match(sid or ""):
            self._json({"error": "bad session id"}, 400)
            return
        try:
            if what == "turns":
                run, sid = start_turn(sid, body, self._client())
            else:
                if SESSIONS.get(sid) is None:
                    self._json({"error": "no such session"}, 404)
                    return
                req = dict(apply_profile(dict(body, source=self._client())))
                run = RUNS.start("chat", req["source"],
                                 lambda run: runner().compress_session(run, lambda t, d=None: RUNS.emit(run, t, d), sid, req),
                                 title="compress", key="session:" + sid, meta={"session": sid, "what": "compress"})
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        except runsmod.Busy as e:
            self._json({"error": "a turn is already running in this session", "run": e.run.id, "session": sid}, 409)
            return
        self._json({"ok": True, "run": run.id, "session": sid}, 202)

    def _workflows(self, path, body):
        """POST /api/workflows {action: save|delete, name, workflow};
        POST /api/workflows/<name>/run {params, and for chat: client_tools,
        interactive, yes, cwd, session}."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        if path.endswith("/run"):
            name = urllib.parse.unquote(path[len("/api/workflows/"):-len("/run")])
            try:
                wf, prompt = render_workflow(name, body.get("params") or {})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            source = self._client()
            try:
                if wf.get("kind") == "agents":
                    run = start_goal(prompt, source, title=name, meta={"workflow": name, "project": wf.get("project") or None})
                    TRACE.log("workflow", name=name, workflow_kind="agents", run=run.id, source=source)
                    self._json({"ok": True, "run": run.id, "kind": "agents"}, 202)
                    return
                req = {k: body.get(k) for k in ("client_tools", "interactive", "yes", "cwd", "model", "effort")
                       if body.get(k) is not None}
                req.update(text=prompt, profile=wf.get("profile") or body.get("profile"), title=name, workflow=name,
                           project=wf.get("project") or body.get("project") or None)
                run, sid = start_turn(body.get("session") or "new", req, source)
            except agentmod.AgentConfigError as e:
                self._json({"error": str(e)}, 400)
                return
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            except runsmod.Busy as e:
                self._json({"error": str(e), "run": e.run.id}, 409)
                return
            TRACE.log("workflow", name=name, workflow_kind="chat", run=run.id, session=sid, source=source)
            self._json({"ok": True, "run": run.id, "session": sid, "kind": "chat", "folder": wf.get("folder") or ""}, 202)
            return
        name = str(body.get("name") or "").strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$", name):
            self._json({"error": "a workflow name is letters, digits, . _ - (48 at most), no spaces: it is typed in bb run"}, 400)
            return
        mine = CFG.setdefault("workflows", {})
        action = body.get("action") or "save"
        if action == "delete":
            if mine.pop(name, None) is None:
                self._json({"error": "no workflow named %r" % name}, 404)
                return
        elif action == "save":
            wf = body.get("workflow") if isinstance(body.get("workflow"), dict) else {}
            kind = wf.get("kind") or "chat"
            if kind not in ("chat", "agents"):
                self._json({"error": "kind is chat or agents"}, 400)
                return
            if not (wf.get("prompt") or "").strip():
                self._json({"error": "a workflow needs a prompt (a goal, for agents)"}, 400)
                return
            if wf.get("profile") and wf["profile"] not in profiles():
                self._json({"error": "no profile named %r" % wf["profile"]}, 400)
                return
            if wf.get("project") and wf["project"] not in projects():
                self._json({"error": "no such project"}, 400)
                return
            params = [{"name": str(p.get("name")), "help": str(p.get("help") or "")[:200],
                       "default": str(p.get("default") or "")[:2000]}
                      for p in wf.get("params") or [] if isinstance(p, dict) and re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", str(p.get("name") or ""))]
            mine[name] = {"description": str(wf.get("description") or "")[:300], "kind": kind,
                          "profile": wf.get("profile") or "", "prompt": str(wf["prompt"])[:20000], "params": params,
                          "folder": str(wf.get("folder") or "")[:500], "project": wf.get("project") or ""}
        else:
            self._json({"error": "unknown action"}, 400)
            return
        save_config()
        BUS.publish("config", "workflows", {"name": name})
        self._json({"ok": True, "workflows": {n: dict(w, params=workflow_params(w)) for n, w in workflows().items()}})

    def _profiles_admin(self, body):
        """{action: save, name, profile} | {action: delete, name} | {action: default, name}.
        A built-in profile can be changed, and deleting the change restores it."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        name = str(body.get("name") or "").strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$", name):
            self._json({"error": "a profile name is letters, digits, spaces, . _ - (40 at most)"}, 400)
            return
        mine = CFG.setdefault("profiles", {})
        action = body.get("action") or "save"
        if action == "save":
            prof = body.get("profile") if isinstance(body.get("profile"), dict) else {}
            clean = {k: prof[k] for k in PROFILE_KEYS + ("description",) if k in prof}
            if "effort" in clean and clean["effort"] not in runnermod.ABSTRACT_EFFORT + runnermod.EFFORT_LEVELS + (None,):
                self._json({"error": "effort is one of off, low, medium, high, max"}, 400)
                return
            mine[name] = clean
        elif action == "delete":
            if name not in mine:
                self._json({"error": "no saved profile named %r" % name}, 404)
                return
            mine.pop(name)
        elif action == "default":
            if name not in profiles():
                self._json({"error": "no profile named %r" % name}, 404)
                return
            CFG["default_profile"] = name
        else:
            self._json({"error": "unknown action"}, 400)
            return
        save_config()
        BUS.publish("config", "profiles", {"name": name})
        self._json({"ok": True, "profiles": profiles(), "default": CFG.get("default_profile") or "Default"})

    def _client(self):
        """Who started this: "app" unless a client says otherwise (bb sends cli)."""
        c = (self.headers.get("X-BB-Client") or "app").strip().lower()
        return c if re.match(r"^[a-z][a-z0-9-]{0,15}$", c) else "app"

    def _projects_admin(self, body):
        """POST /api/projects {action: save, project: {id?, name, folder, instructions, profile}} or {action: delete, id}."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        mine = CFG.setdefault("projects", {})
        action = body.get("action") or "save"
        if action == "delete":
            pid = str(body.get("id") or "")
            if mine.pop(pid, None) is None:
                self._json({"error": "no such project"}, 404)
                return
            save_config()
            BUS.publish("config", "projects", {"id": pid})
            TRACE.log("project", action="delete", id=pid)
            self._json({"ok": True, "projects": project_list()})
            return
        if action != "save":
            self._json({"error": "unknown action"}, 400)
            return
        p = body.get("project") if isinstance(body.get("project"), dict) else {}
        name = str(p.get("name") or "").strip()
        if not name or len(name) > 60:
            self._json({"error": "a project needs a name (60 characters at most)"}, 400)
            return
        pid = str(p.get("id") or "") or ("p-" + uuid_hex(10))
        if not re.match(r"^p-[0-9a-f]{4,16}$", pid):
            self._json({"error": "bad project id"}, 400)
            return
        if any((q.get("name") or "").lower() == name.lower() and k != pid for k, q in mine.items()):
            self._json({"error": "there is already a project named %r" % name}, 400)
            return
        folder = str(p.get("folder") or "").strip()
        if folder:
            folder = os.path.normpath(folder)                     # ~/a/../b reads as ~/b
            if not os.path.isabs(os.path.expanduser(folder)):
                self._json({"error": "the folder is a full path, or starts with ~"}, 400)
                return
            try:
                os.makedirs(os.path.expanduser(folder), exist_ok=True)     # made when it is not there yet
            except OSError as e:
                self._json({"error": "cannot create %s: %s" % (folder, e.strerror or e)}, 400)
                return
        prof = str(p.get("profile") or "")
        if prof and prof not in profiles():
            self._json({"error": "no profile named %r" % prof}, 400)
            return
        old = mine.get(pid) or {}
        mine[pid] = {"name": name, "folder": folder, "instructions": str(p.get("instructions") or "")[:20000],
                     "profile": prof, "created": old.get("created") or int(time.time() * 1000)}
        save_config()
        BUS.publish("config", "projects", {"id": pid})
        TRACE.log("project", action="save", id=pid, name=name)
        self._json({"ok": True, "id": pid, "projects": project_list()})

    def _terminal(self, body):
        """POST /api/terminal {session?, project?, folder?}: bb in a terminal, or the command for one."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        sid = body.get("session") or None
        if sid and not sessmod._ID.match(str(sid)):
            self._json({"error": "bad session id"}, 400)
            return
        try:
            pid = project_named(body["project"]) if body.get("project") else None
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        sess = SESSIONS.get(sid) if sid else None
        if sess and not pid:
            pid = sess.get("project") if sess.get("project") in projects() else None
        # where bb last ran for this chat (a subfolder, say), else the project's folder
        cwd = (sess or {}).get("cwd")
        folder = str(body.get("folder") or "").strip() or (cwd if cwd and os.path.isdir(cwd) else None) \
            or (projects().get(pid) or {}).get("folder") or "~"
        try:
            res = open_terminal(folder, sid if sess else None, pid)
        except OSError as e:
            self._json({"error": "could not open a terminal: %s" % e}, 500)
            return
        TRACE.log("terminal", session=sid, project=pid, folder=res["folder"], opened=res["opened"])
        self._json(res)

    def _update_check(self):
        try:
            self._json(updatemod.check(app_version(), bool(os.environ.get("BYTEBUNKER_DESKTOP")),
                                       updatemod.app_location()))
        except urllib.error.HTTPError as e:
            if e.code == 404:          # GitHub has no latest release until one is published
                self._json({"current": app_version(), "latest": None, "newer": False, "can_update": False,
                            "why_not": None, "notes": "", "page": "", "none_published": True})
            else:
                self._json({"error": "GitHub answered %s" % e.code}, 502)
        except Exception as e:   # noqa: BLE001
            self._json({"error": "could not reach GitHub: %s" % str(e)[:200]}, 502)

    def _update_apply(self):
        """Download the newer version, check it, unpack it beside this one and
        hand over to the swap helper; the page then closes the window (or this
        server leaves by itself) and the new version opens."""
        location = updatemod.app_location()
        try:
            info = updatemod.check(app_version(), bool(os.environ.get("BYTEBUNKER_DESKTOP")), location)
        except Exception as e:   # noqa: BLE001
            self._json({"error": "could not reach GitHub: %s" % str(e)[:200]}, 502)
            return
        if not info["newer"]:
            self._json({"error": "this is the latest version (%s)" % info["current"]}, 409)
            return
        if not info["can_update"]:
            self._json({"error": info["why_not"], "page": info["page"]}, 409)
            return

        def work(run, emit):
            import shutil
            home = update_home()
            os.makedirs(home, exist_ok=True)
            zpath = os.path.join(home, info["asset"]["name"])
            staging = os.path.join(home, "staging")

            def progress(got, total):
                emit({"phase": "download", "got": got, "total": total or info["asset"].get("size") or 0})
                if run.cancelled:
                    raise RuntimeError("cancelled: nothing was changed")
            try:
                emit({"phase": "download", "got": 0, "total": info["asset"].get("size") or 0})
                got = updatemod.download(info["asset"]["url"], zpath, progress)
                emit({"phase": "verify"})
                if got != updatemod.expected_sha256(info["checksum_url"], info["asset"]["name"]):
                    raise RuntimeError("the download does not match the checksum its release publishes: nothing was changed")
                emit({"phase": "unpack"})
                new_app = updatemod.unpack(zpath, staging)
                if not updatemod.signature_ok(new_app):
                    raise RuntimeError("the new app's signature does not verify: nothing was changed")
                said = updatemod.unpacked_version(new_app)
                if said != info["latest"]:
                    raise RuntimeError("the new app says it is %s, not %s: nothing was changed" % (said, info["latest"]))
            except Exception as e:   # noqa: BLE001
                shutil.rmtree(staging, ignore_errors=True)
                emit({"error": str(e)})
                return {"ok": False, "error": str(e)}
            finally:
                for p in (zpath, zpath + ".part"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            with open(os.path.join(home, "pending.json"), "w", encoding="utf-8") as f:
                json.dump({"from": app_version(), "to": info["latest"], "page": info["page"], "at": round(time.time())}, f)
            try:
                os.remove(os.path.join(home, "updater.log"))
            except OSError:
                pass
            updatemod.start_swap(os.getpid(), location, new_app, os.path.join(home, "previous", os.path.basename(location)),
                                 os.path.join(home, "updater.log"), updatemod.relaunch_command(location), home)
            TRACE.log("update", from_version=app_version(), to_version=info["latest"], run=run.id)
            emit({"phase": "ready", "version": info["latest"]})
            # the page closes the window now; a server with no window of its own leaves by itself
            t = threading.Timer(8.0, _leave_for_update)
            t.daemon = True              # a window that closes ends the process at once
            t.start()
            return {"ok": True, "version": info["latest"]}
        self._run_streamed("update", "update to %s" % info["latest"], work, key="update")

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
            self._audit_body = json.loads(self.rfile.read(n) or b"{}")
            return self._audit_body
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
    def _do_GET(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/handoff":
            self._handoff(u.query)
            return
        if path == "/api/hello":
            # how clients (the window, the CLI) confirm they found the right server
            self._json({"service": "bytebunker", "version": app_version(), "pid": os.getpid(),
                        "data": DATA, "desktop": bool(os.environ.get("BYTEBUNKER_DESKTOP")),
                        "started": round(STARTED, 3)})
            return
        if path == "/api/config":
            config_fresh()
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
                "warnings": CONFIG_WARNINGS[-5:],
                "features": CFG.get("features") or {},
                "roles": CFG.get("roles") or {},
            })
        elif path == "/api/models":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                GW.refresh(force=bool(q.get("refresh")))
                serving = MON.serving()
                data = GW.models
                if serving:
                    # a monitor knows what is really served: a router's other routes are only routes
                    data = [dict(m, served=(m.get("base_id") or m["id"]) in serving
                                 or (GW.status.get(m["gateway"]) or {}).get("kind") not in ("litellm",))
                            for m in GW.models]
                self._json({"object": "list", "data": data, "gateways": GW.status,
                            "last_used": last_used_model()})
            except Exception as e:   # noqa: BLE001
                self._json({"error": str(e), "data": []}, 502)
        elif path == "/api/settings":
            self._json({"version": app_version(), "desktop": bool(os.environ.get("BYTEBUNKER_DESKTOP")),
                        "platform": sys.platform, "data_dir": DATA, "config_path": CONFIG_PATH,
                        "home_dir": os.path.dirname(CONFIG_PATH),
                        "uploads_dir": os.path.expanduser(CFG.get("uploads_dir") or os.path.join(DATA, "uploads")),
                        "identity": CFG.get("identity") or {}, "rates": CFG.get("frontier_rates_per_mtok") or {},
                        "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False)),
                        "keep_running": bool(CFG.get("keep_running")), "roles": CFG.get("roles") or {},
                        "update_check_at_start": bool(CFG.get("update_check_at_start")),
                        "updated": UPDATE_STATE.get("updated"), "update_failed": UPDATE_STATE.get("update_failed"),
                        "tls": tls_info(),
                        "sandbox": {"kind": cmdbox.kind(), "wsl": cmdbox.wsl_status(),
                                    "wsl_restart_pending": bool(CFG.get("wsl_restart_pending"))}})
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
                proj = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("project") or [None])[0]
                items = SESSIONS.list()
                if proj == "none":
                    items = [s for s in items if not s.get("project")]
                elif proj:
                    items = [s for s in items if s.get("project") == proj]
                self._json(items)
        elif path == "/api/projects":
            self._json({"projects": project_list()})
        elif path == "/api/events":
            self._events(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
        elif path == "/api/profiles":
            self._json({"profiles": profiles(), "default": CFG.get("default_profile") or "Default"})
        elif path == "/api/approvals":
            self._json({"pending": APPROVALS.list()})
        elif path == "/api/workflows":
            self._json({"workflows": {n: dict(w, params=workflow_params(w)) for n, w in workflows().items()}})
        elif path == "/api/help":
            self._json({"guides": [{k: v for k, v in g.items() if k != "body"} for g in help_guides()]})
        elif path.startswith("/api/help/"):
            gid = path[len("/api/help/"):]
            g = next((g for g in help_guides() if g["id"] == gid), None)
            if g is None:
                self._json({"error": "no guide %r" % gid}, 404)
            else:
                self._json(g)
        elif path == "/api/setup":
            self._json({"items": setup_checklist()})
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
            st["minion_pool"] = a.get("minion_pool") or []
            for k in ("master_effort", "thinking_effort", "fast_effort", "master_plan_effort"):
                st[k] = a.get(k) or ""
            st["unserved"] = {k: a[k] for k in ("master_model", "thinking_model", "slave_model")
                              if (a.get(k) or "").strip() and not model_served(a[k].strip())}
            st["minion_slots"] = a.get("minion_slots") or 4
            # the engines a run's minions would spread over now, as the run spec would name them
            st["pool_engines"] = [{k: e[k] for k in ("name", "model", "slots")}
                                  for e in minion_pool(a, a.get("slave_model") or (CFG.get("roles") or {}).get("fast"),
                                                       bool((a.get("ssh") or "").strip()))]
            # known once probed (a run, or Test); a harness on this computer is read on the spot
            caps = (a.get("harness") or {}) if (a.get("ssh") or "").strip() or not a.get("dir") \
                else agentmod.harness_caps(CFG)
            st["pool_supported"] = bool(caps.get("pool"))
            runs = []
            for e in TRACE.events(time.strftime("%Y-%m-%d",
                                  time.localtime(time.time() - 14 * 86400))):
                if e.get("kind") == "agent_run":
                    runs.append({"id": e.get("id"), "ts": e.get("ts"), "goal": e.get("goal"),
                                 "exit": e.get("exit"), "killed": e.get("killed"),
                                 "lines": e.get("lines"), "host": e.get("host")})
            st["recent"] = runs[-40:][::-1]
            self._json(st)
        elif path == "/api/agents/court":
            a = CFG.get("agents") or {}
            self._json({"defaults": agentmod.COURT_DEFAULTS, "court": a.get("court") or {"roles": {}},
                        "harness_knows": bool((a.get("harness") or {}).get("court")),
                        "harness_access": bool((a.get("harness") or {}).get("access")),
                        "mcp": agentmod.shareable_mcp(CFG.get("mcp_servers") or {}),
                        "master_name": a.get("master_name") or "Sultan"})
        elif path == "/api/agents/planes":
            self._json({"at": _machines["at"], "planes": agent_planes()})
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
            # the goal on screen is the running one, when the harness named it (BB_RUN)
            going = RUNS.active("agents")
            out = agentmod.recent_slaves(CFG, goal_id=going[0].meta.get("goal_id") if going else None)
            out["running"] = bool(going)             # whether the master is working now, not just its last events
            out["progress"] = going[0].meta.get("progress") if going else None
            self._json(out)
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
                                    for n in sorted(found)], "warnings": warnings,
                        "git_hosts": VAULT.git_hosts()})
        elif path == "/api/git":
            self._json({"hosts": VAULT.git_hosts()})
        elif path == "/api/rack":
            self._json(rack_overview((urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("rack") or [None])[0]))
        elif path == "/api/machines":
            self._json({"at": _machines["at"], "machines": annotate_machines(_machines["list"]), "here": _machines["here"],
                        "need_info": {k: v["why"] for k, v in NEEDS.items()}})
        elif path == "/api/racks":
            self._json({"racks": racks(), "local": racksmod.local_entry(),
                        "app": {"key": os.path.exists(KEYS.key), "known_hosts": KEYS.known_hosts}})
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
        elif path == "/api/update/check":
            self._update_check()
        elif path == "/api/changelog":
            self._json({"current": app_version(), "sections": changelog.sections()})
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
        elif path == "/api/audit":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            g = lambda k: (q.get(k) or [None])[0]
            try:
                items = AUDIT.entries(category=g("category") or None, q=g("q"), before=g("before"),
                                      limit=int(g("limit") or 200), who=g("who") or None)
            except ValueError:
                self._json({"error": "bad limit or before"}, 400)
                return
            self._json({"entries": items, "stats": AUDIT.stats(), "categories": list(auditmod.CATEGORIES)})
        elif path == "/api/audit/verify":
            self._json(AUDIT.verify())
        elif path == "/api/audit/export":
            # the whole file, as written, for someone else's records
            try:
                with open(AUDIT.path, "rb") as f:
                    data = f.read()
            except OSError:
                data = b""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Disposition", 'attachment; filename="bytebunker-audit-%s.jsonl"' % time.strftime("%Y%m%d"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            AUDIT.record("data", "audit.export", None, True, entries=AUDIT.stats()["entries"])
        elif path == "/api/video" or path.startswith("/api/video/"):
            self._video_get(path)
        else:
            self._static(path)

    def _do_DELETE(self):
        if not self._guard():
            return
        config_fresh()
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

    def _do_POST(self):
        if not self._guard():
            return
        config_fresh()
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/video":  # multipart, not JSON — handled whole
            self._video_post()
            return
        if path.startswith("/api/sessions/") and path.endswith(("/turns", "/compress")):
            body = self._body()
            if body is not None:
                self._turn(path, body)
            return
        if path == "/api/workflows" or (path.startswith("/api/workflows/") and path.endswith("/run")):
            body = self._body()
            if body is not None:
                self._workflows(path, body)
            return
        if path == "/api/approvals":
            body = self._body()
            if body is None:
                return
            try:
                done = APPROVALS.answer(str((body or {}).get("id") or ""), (body or {}).get("decision"), by=self._client())
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            if done is None:
                self._json({"error": "already answered, or nothing is asking"}, 409)
                return
            TRACE.log("approval", id=done["id"], tool=done["tool"], decision=done["decision"], by=done["by"],
                      session=done.get("session"), run=done.get("run"))
            self._json({"ok": True, "approval": done})
            return
        if path.startswith("/api/runs/") and path.endswith("/tool-results"):
            body = self._body()
            if body is None:
                return
            rid = path[len("/api/runs/"):-len("/tool-results")]
            ok = isinstance(body, dict) and CLIENT_RESULTS.deliver(rid, str(body.get("call_id") or ""),
                                                                   body.get("content") or "", body.get("is_error"))
            self._json({"ok": ok}, 200 if ok else 404)
            return
        if path.startswith("/api/runs/") and path.endswith("/cancel"):
            self._drain()
            rid = path[len("/api/runs/"):-len("/cancel")]
            ok = RUNS.cancel(rid)
            self._json({"ok": ok, "id": rid}, 200 if ok else 409)
            return
        if path == "/api/profiles":
            body = self._body()
            if body is not None:
                self._profiles_admin(body)
            return
        if path not in ("/api/chat", "/api/sessions", "/api/usage-event",
                        "/api/tool-call", "/api/mcp", "/api/archive", "/api/rate",
                        "/api/plugins", "/api/git", "/api/skills", "/api/recipes", "/api/rack", "/api/racks", "/api/machines", "/api/upload", "/api/jobs", "/api/gateways", "/api/monitors", "/api/settings", "/api/agents", "/api/agents/slave",
                        "/api/update/apply", "/api/projects", "/api/terminal", "/api/audit", "/api/handoff"):
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
            name, args = body.get("name") or "", body.get("arguments") or {}
            if name in terminal_tools():
                sid = self.headers.get("X-BB-Session")
                denied, args = terminal_call(args, sid, self.headers.get("X-BB-Access"),
                                             lambda shown, kind: APPROVALS.ask(_CallRun(), name, shown, session=sid,
                                                                               kind=kind))
                if denied:
                    self._json({"content": denied[0], "isError": True})
                    return
            text, is_err = mcp_host().call(name, args)
            TRACE.log("tool", session=self.headers.get("X-BB-Session"),
                      turn=self.headers.get("X-BB-Turn"), name=body.get("name") or "",
                      arguments=body.get("arguments") or {}, result=str(text)[:200000],
                      is_error=bool(is_err), ms=int((time.time() - t0) * 1000))
            self._json({"content": text, "isError": is_err})
        elif path == "/api/mcp":
            self._mcp_admin(body)
        elif path == "/api/update/apply":
            self._update_apply()
        elif path == "/api/projects":
            self._projects_admin(body)
        elif path == "/api/terminal":
            self._terminal(body)
        elif path == "/api/plugins":
            self._plugin_admin(body)
        elif path == "/api/git":
            self._git_admin(body)
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
        elif path == "/api/racks":
            self._racks_admin(body)
        elif path == "/api/audit":
            if body.get("action") != "export":
                self._json({"error": "expected {action: export}"}, 400)
                return
            # a copy in Downloads: a download link does nothing inside the desktop window
            folder = os.path.expanduser("~/Downloads")
            folder = folder if os.path.isdir(folder) else os.path.expanduser("~")
            dest = os.path.join(folder, "bytebunker-audit-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
            try:
                shutil.copyfile(AUDIT.path, dest)
            except FileNotFoundError:
                open(dest, "w").close()
            except OSError as e:
                self._json({"error": str(e)[:200]}, 500)
                return
            self._json({"ok": True, "path": dest, "entries": AUDIT.stats()["entries"]})
        elif path == "/api/machines":
            self._machines_admin(body)
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
            if body.get("action") == "access":
                sid, mode = str(body.get("id") or ""), body.get("access")
                if not sessmod._ID.match(sid) or SESSIONS.get(sid) is None or mode not in cmdbox.MODES:
                    self._json({"error": "a session and local, web or full"}, 400)
                    return
                note_access(sid, used=body.get("used") if body.get("used") in cmdbox.MODES else None, current=mode)
                TRACE.log("session", action="access", id=sid, access=mode, used=body.get("used"))
                self._json({"ok": True})
                return
            if body.get("action") == "rename":
                sid = str(body.get("id") or "")
                if not sessmod._ID.match(sid) or SESSIONS.get(sid) is None:
                    self._json({"error": "no such session"}, 404)
                    return
                name = " ".join(str(body.get("name") or "").split())[:120]    # empty: back to its first message
                summary = SESSIONS.append(sid, "meta", name=name or None)
                BUS.publish("sessions", "updated", summary)
                self._json({"ok": True, "session": summary})
                return
            if body.get("action") == "set_project":
                sid, pid = str(body.get("id") or ""), body.get("project") or None
                if not sessmod._ID.match(sid) or SESSIONS.get(sid) is None:
                    self._json({"error": "no such session"}, 404)
                    return
                if pid and pid not in projects():
                    self._json({"error": "no such project"}, 400)
                    return
                SESSIONS.append(sid, "meta", project=pid)
                summary = next((s for s in SESSIONS.list() if s.get("id") == sid), {"id": sid})
                BUS.publish("sessions", "updated", summary)
                self._json({"ok": True, "session": summary})
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
        elif path == "/api/handoff":
            # a one-time link that opens this app in a browser with its token (bb open, a second window)
            self._json({"url": handoff_url(self.server.server_address[1])})
        elif path == "/api/gateways":
            self._gateways_admin(body)
        elif path == "/api/monitors":
            self._monitors_admin(body)
        elif path == "/api/settings":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            if body.get("action") == "install_cli":
                try:
                    res = install_cli()
                except OSError as e:
                    self._json({"error": "could not write the command: %s" % e}, 500)
                    return
                TRACE.log("settings", action="install_cli", written=res["written"], skipped=res["skipped"])
                self._json(res)
                return
            if isinstance(body.get("roles"), dict):
                roles = CFG.setdefault("roles", {})
                for k, v in body["roles"].items():
                    if k in ROLES:
                        if v:
                            roles[k] = str(v)[:200]
                        else:
                            roles.pop(k, None)
                save_config()
                BUS.publish("config", "roles", roles)
                self._json({"ok": True, "roles": roles})
                return
            if body.get("action") == "enable_wsl":
                if sys.platform != "win32":
                    self._json({"error": "WSL2 is Windows'"}, 400)
                    return
                cmdbox.wsl_enable()
                CFG["wsl_restart_pending"] = True
                save_config()
                TRACE.log("settings", action="enable_wsl")
                self._json({"ok": True})
                return
            if body.get("action") == "finish_wsl":
                if sys.platform != "win32":
                    self._json({"error": "WSL2 is Windows'"}, 400)
                    return
                lines = []
                ok = cmdbox.wsl_finish(lines.append)
                if ok:
                    CFG.pop("wsl_restart_pending", None)
                    save_config()
                TRACE.log("settings", action="finish_wsl", ok=ok)
                self._json({"ok": ok, "lines": lines})
                return
            if body.get("action") == "update_seen":
                clear_update_state()
                self._json({"ok": True})
                return
            if "update_check_at_start" in body:
                CFG["update_check_at_start"] = bool(body["update_check_at_start"])
                save_config()
                self._json({"ok": True, "update_check_at_start": CFG["update_check_at_start"]})
                return
            if "keep_running" in body:
                CFG["keep_running"] = bool(body["keep_running"])
                save_config()
                self._json({"ok": True, "keep_running": CFG["keep_running"]})
                return
            if isinstance(body.get("features"), dict):
                # server_runner: the Playground's turns run on the server (runner.py)
                feats = CFG.setdefault("features", {})
                for k, v in body["features"].items():
                    if k in ("server_runner",):
                        feats[k] = bool(v)
                save_config()
                BUS.publish("config", "features", feats)
                self._json({"ok": True, "features": feats})
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
        if body.get("action") == "court":
            try:
                court = agentmod.clean_court(body.get("court") or {})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            CFG.setdefault("agents", {})["court"] = court
            save_config()
            TRACE.log("agents", action="court", roles=sorted(court["roles"]))
            self._json({"ok": True, "court": court})
            return
        if body.get("action") == "wrapup":
            going = [r for r in RUNS.active("agents") if getattr(r, "wrap_event", None) is not None]
            if not going:
                self._json({"error": "no goal is running"}, 409)
                return
            going[0].wrap_event.set()
            RUNS.emit(going[0], "out", {"line": "console: wrap up asked: the master answers with what it has"})
            TRACE.log("agents", action="wrapup", run=going[0].id)
            self._json({"ok": True})
            return
        if body.get("action") == "plane_install":
            target, what = str(body.get("target") or "").strip(), str(body.get("what") or "")
            if what not in PLANE_NEEDS or (target != racksmod.LOCAL and not racksmod.TARGET.match(target)):
                self._json({"error": "nothing to install by that name"}, 400)
                return
            TRACE.log("agents", action="plane_install", target=target, what=what)
            self._json(open_install_terminal(target, what))
            return
        if body.get("action") == "plane":
            target = str(body.get("target") or "").strip()
            if target != racksmod.LOCAL and not racksmod.TARGET.match(target):
                self._json({"error": "an ssh destination, or local"}, 400)
                return
            p = next((p for p in agent_planes() if p["target"] == target), None)
            if p and not p["selectable"]:
                self._json({"error": "%s %s: pick another machine for agents" % (p["name"], p["note"])}, 409)
                return
            self._run_streamed("agents-plane", "agents on " + ("this computer" if target == racksmod.LOCAL else target),
                               lambda run, emit: agent_plane_setup(run, emit, target), key="agents-plane", detach=True)
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
            for key, allowed in (("master_effort", EFFORTS), ("thinking_effort", EFFORTS), ("fast_effort", EFFORTS),
                                 ("master_plan_effort", EFFORTS + ("same",))):
                if key in body:
                    v = str(body.get(key) or "").strip().lower()
                    if v in allowed:
                        a[key] = v
                    else:
                        a.pop(key, None)             # blank: the model's (or the worker's) default
            if isinstance(body.get("minion_pool"), list):
                # more engines for the minions: [{model ("id" or "id@gateway"), slots}]
                pool = []
                for e in body["minion_pool"][:16]:
                    if isinstance(e, dict) and str(e.get("model") or "").strip():
                        try:
                            slots = max(1, min(64, int(e.get("slots") or 4)))
                        except (TypeError, ValueError):
                            slots = 4
                        pool.append({"model": str(e["model"]).strip()[:160], "slots": slots})
                a["minion_pool"] = pool
            if body.get("minion_slots") is not None:
                try:
                    a["minion_slots"] = max(1, min(64, int(body["minion_slots"])))
                except (TypeError, ValueError):
                    pass
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
                        "slave_model": a.get("slave_model") or "", "minion_pool": a.get("minion_pool") or [],
                        **{k: a.get(k) or "" for k in ("master_effort", "thinking_effort", "fast_effort",
                                                        "master_plan_effort")},
                        "minion_slots": a.get("minion_slots") or 4})
            return
        goal = body.get("goal") or ""
        try:
            agents_ready(goal)                       # a setup problem is a 400, not a failed run
        except agentmod.AgentConfigError as e:
            self._json({"error": str(e)}, 400)
            return
        try:
            pid = project_of(body)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        self._run_streamed("agents", goal.strip()[:120] or "agents goal",
                           lambda run, emit: agent_goal(run, emit, goal)[0], key="agents", meta={"project": pid})

    def _rack(self, body):
        """show <recipe>: the files the recipe runs; up/down/logs/status/bench:
        run rack and stream its output. `rack` names the rack (else the first).
        `up` and `down` change what a rack serves: the UI confirms first, and
        the trace log records who did what."""
        import subprocess
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        r = rack_named(body.get("rack"))
        if r is None:
            self._json({"error": ("no rack named %r" % body["rack"]) if body.get("rack")
                        else "no machine yet: Add a machine pairs one over ssh"}, 400)
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
            if r["paired"] or r.get("local"):
                try:
                    cmd, files = rack_cmd(["recipes", "show", recipe], r), None
                except racksmod.PairError as e:
                    self._json({"error": str(e)}, 400)
                    return
            else:
                # where rack says the recipe is (1.0: yours live outside the checkout), else the old place
                files = next((x.get("files") for x in rack_overview(r["name"]).get("recipes") or []
                              if x.get("name") == recipe and x.get("files")), None)
                if files:
                    inner = " && ".join("echo %s && cat %s" % (shlex_quote("# " + f), shlex_quote(f)) for f in files)
                else:
                    files = ["recipes/%s.env" % recipe]
                    inner = "cd %s && cat %s" % (agentmod._rq(r["dir"] or "~/dgx/dgx-spark-serve"), shlex_quote(files[0]))
                cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r["ssh"], inner]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            text = out.stdout or out.stderr
            if files is None:          # rack recipes show heads each file with "# <path>"
                files = [l[2:] for l in text.splitlines() if l.startswith(("# /", "# ~")) and l.endswith(".env")] or [recipe]
            self._json({"ok": out.returncode == 0, "recipe": recipe, "file": files[-1], "rack": r["name"],
                        "text": text[-20000:]})
            return
        if action not in ("up", "down", "logs", "status", "bench", "preflight", "gateway"):
            self._json({"error": "unknown action"}, 400)
            return
        args = [action] + ([recipe] if recipe and action in ("up", "bench", "gateway") else [])
        if action == "logs":
            args = ["logs"]
        try:
            rack_cmd(args, r)
        except racksmod.PairError as e:
            self._json({"error": str(e)}, 400)
            return
        _rack_cache.pop(r["name"], None)           # status changes after this

        def work(run, emit):
            emit({"phase": "start", "host": r["ssh"], "rack": r["name"], "cmd": "rack " + " ".join(args)})
            captured = []

            def on_line(text):
                text = _ANSI.sub("", text)        # a terminal's colours are noise in the app
                if len(captured) < 2000:
                    captured.append(text)
                emit({"line": text})
            try:
                code, killed = agentmod.run_streaming(rack_cmd(args, r), ROOT, on_line, run.cancel_event, timeout_s=3600)
            except Exception as e:   # noqa: BLE001
                emit({"error": str(e)})
                code, killed = -1, "error"
            if code == 0 and action == "up":
                ensure_monitor(r, lambda t: emit({"line": t}))
                if not r.get("local"):
                    # its engine and monitor as the rack says now: a monitor started just now joins Cluster
                    got = relink_rack(r)
                    if got:
                        emit({"line": "this app talks to it now: " + ", ".join("%s %s" % kv for kv in sorted(got.items()))})
            if code == 0 and action == "up" and r.get("local"):
                added = connect_local(emit)
                if added:
                    emit({"line": "this app talks to it now: " + ", ".join("%s %s" % kv for kv in sorted(added.items()))})
            emit({"phase": "done", "exit": code, "killed": killed})
            TRACE.log("rack", action=action, recipe=recipe, rack=r["name"], host=r["ssh"], exit=code, run=run.id,
                      killed=killed or False, output=captured[-200:])
            return {"ok": code == 0, "exit": code, "killed": killed or False,
                    "error": None if code == 0 else "rack %s exited %s" % (action, code)}
        # a deploy goes on without its viewer; a log tail or a status read does not
        viewing = action in ("logs", "status")
        self._run_streamed("rack", "rack " + " ".join(args) + " on " + r["name"], work,
                           meta={"action": action, "recipe": recipe, "rack": r["name"], "host": r["ssh"]},
                           key=None if viewing else "rack:" + r["ssh"], detach=not viewing)

    def _machines_admin(self, body):
        """find: ask every ssh host and tailnet machine what it is (read-only,
        your own ssh, never a password); deploy {target, recipe}: one click."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action")
        if action == "find":
            cat = machinesmod.load_catalog()
            _machines.update(at=int(time.time() * 1000), list=machinesmod.find(KEYS, cat), here=machinesmod.here(cat),
                             workers=rack_workers())
            TRACE.log("machine", action="find", answered=sum(1 for m in _machines["list"] if m.get("ok")),
                      asked=len(_machines["list"]))
            self._json({"at": _machines["at"], "machines": annotate_machines(_machines["list"]), "here": _machines["here"],
                        "need_info": {k: v["why"] for k, v in NEEDS.items()}})
            return
        if action == "install":
            # one of NEEDS, in a terminal on this computer (ssh -t there), where sudo can ask
            target, what = str(body.get("target") or "").strip(), str(body.get("what") or "")
            if what not in NEEDS or (target != racksmod.LOCAL and not racksmod.TARGET.match(target)):
                self._json({"error": "nothing to install by that name"}, 400)
                return
            TRACE.log("machine", action="install", target=target, what=what)
            self._json(open_install_terminal(target, what))
            return
        if action == "deploy":
            target = str(body.get("target") or "").strip()
            recipe = str(body.get("recipe") or "").strip()
            if target != racksmod.LOCAL and not racksmod.TARGET.match(target):
                self._json({"error": "an ssh destination: user@host, or a host from ~/.ssh/config"}, 400)
                return
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", recipe):
                self._json({"error": "which recipe?"}, 400)
                return
            m = next((m for m in annotate_machines(_machines["list"]) if m.get("target") == target), None)
            if m and m.get("worker_of"):
                self._json({"error": "%s is a worker in %s's rack: deploy on %s, which serves across both"
                                     % (target, m["worker_of"], m["worker_of"])}, 409)
                return
            if m and m.get("share"):
                self._json({"error": "%s runs its share of a model served from another machine (a rack's worker): "
                                     "deploy on that rack's head" % target}, 409)
                return
            where = "this computer" if target == racksmod.LOCAL else target
            self._run_streamed("deploy", "serve %s on %s" % (recipe, where),
                               lambda run, emit: deploy_chain(run, emit, target, recipe),
                               meta={"target": target, "recipe": recipe}, key="deploy:" + target, detach=True)
            return
        self._json({"error": "unknown action"}, 400)

    def _racks_admin(self, body):
        """pair {target}: ask the machine over ssh (a streamed run: what it
        did, then what it found, or the one command to run there when your
        ssh needs a password); save {pairing, gateway, monitor}: keep what was
        found; remove {name, unpair}: forget a rack, and take this app's key
        back from it."""
        import subprocess
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action")
        if action == "pair":
            target = str(body.get("target") or "").strip()
            if not racksmod.TARGET.match(target):
                self._json({"error": "an ssh destination: user@host, or a host from ~/.ssh/config"}, 400)
                return

            def work(run, emit):
                say = lambda t: emit({"line": t})
                try:
                    kind, got = racksmod.ask(KEYS, target, say)
                except racksmod.PairError as e:
                    emit({"error": str(e)})
                    emit({"phase": "done", "exit": 1, "killed": False})
                    TRACE.log("rack", action="pair", target=target, ok=False, error=str(e)[:300], run=run.id)
                    return {"ok": False, "error": str(e)}
                if kind == "needs_key":
                    emit({"needs_key": got})
                    emit({"phase": "done", "exit": 2, "killed": False})
                    TRACE.log("rack", action="pair", target=target, ok=False, needs_key=True, run=run.id)
                    return {"ok": False, "error": "needs the app's key on %s first" % target}
                say("found %s (%s, dgx-serve %s); looking for its engine and monitor from here"
                    % (got.get("name"), got.get("platform"), got.get("rack_version")))
                engine_url, monitor_url, tried = racksmod.find(target, got)
                found = racksmod.summary(target, got, engine_url, monitor_url)
                found["pairing"] = PAIRINGS.put({"target": target, "info": got, "engine_url": engine_url,
                                                 "monitor_url": monitor_url, "app": KEYS.app_name()})
                found["tried"] = tried
                emit({"found": found})
                emit({"phase": "done", "exit": 0, "killed": False})
                TRACE.log("rack", action="pair", target=target, ok=True, engine=engine_url, monitor=monitor_url, run=run.id)
                return {"ok": True}
            self._run_streamed("pair", "pair " + target, work, meta={"target": target}, key="pair:" + target, detach=False)
            return
        if action == "install":
            target = str(body.get("target") or racksmod.LOCAL).strip()
            if target != racksmod.LOCAL and not racksmod.TARGET.match(target):
                self._json({"error": "an ssh destination: user@host, or a host from ~/.ssh/config"}, 400)
                return
            where = "this computer" if target == racksmod.LOCAL else target

            def work(run, emit):
                say = lambda t: emit({"line": t})
                say("installing dgx-serve on %s" % where)
                errors = []

                def on_line(text):
                    text = _ANSI.sub("", text)
                    if text.startswith("BB_ERR "):
                        errors.append(text[7:])
                        emit({"error": text[7:]})
                    elif text != "BB_INSTALLED":
                        say(text)
                try:
                    code, killed = agentmod.run_streaming(racksmod.install_argv(KEYS, target), ROOT, on_line,
                                                          run.cancel_event, timeout_s=1800)
                except Exception as e:   # noqa: BLE001
                    code, killed = -1, "error"
                    errors.append(str(e))
                    emit({"error": str(e)})
                _rack_cache.pop(racksmod.LOCAL, None)
                if code != 0:
                    msg = errors[0] if errors else ("could not reach %s" % target if code == 255 else
                                                    "the install stopped (exit %s)" % code)
                    if not errors and code != 255:
                        emit({"error": msg})
                    emit({"phase": "done", "exit": code or 1, "killed": killed})
                    TRACE.log("rack", action="install", target=target, ok=False, exit=code, error=msg[:300], run=run.id)
                    return {"ok": False, "error": msg}
                TRACE.log("rack", action="install", target=target, ok=True, run=run.id)
                if target == racksmod.LOCAL:
                    say("dgx-serve is on this computer: pick a recipe and Serve")
                    emit({"installed": {"target": target}})
                    emit({"phase": "done", "exit": 0, "killed": False})
                    BUS.publish("config", "racks", {"racks": [x["name"] for x in racks()]})
                    return {"ok": True}
                say("now pairing with %s" % target)          # the same as Add a rack, from here on
                try:
                    kind, got = racksmod.ask(KEYS, target, say)
                except racksmod.PairError as e:
                    emit({"error": str(e)})
                    emit({"phase": "done", "exit": 1, "killed": False})
                    return {"ok": False, "error": str(e)}
                if kind == "needs_key":
                    emit({"needs_key": got})
                    emit({"phase": "done", "exit": 2, "killed": False})
                    return {"ok": False, "error": "needs the app's key on %s first" % target}
                engine_url, monitor_url, tried = racksmod.find(target, got)
                found = racksmod.summary(target, got, engine_url, monitor_url)
                found["pairing"] = PAIRINGS.put({"target": target, "info": got, "engine_url": engine_url,
                                                 "monitor_url": monitor_url, "app": KEYS.app_name()})
                found["tried"] = tried
                emit({"found": found})
                emit({"phase": "done", "exit": 0, "killed": False})
                return {"ok": True}
            self._run_streamed("install", "install dgx-serve on " + where, work, meta={"target": target},
                               key="install:" + target, detach=True)
            return
        if action == "save":
            item = PAIRINGS.take(str(body.get("pairing") or ""))
            if not item:
                self._json({"error": "that pairing is gone (15 minutes, or a restart): pair again"}, 410)
                return
            rack = racksmod.apply(CFG, item, gateway=body.get("gateway", True) is not False,
                                  monitor=body.get("monitor", True) is not False, gw_normalize=gwmod.normalize)
            save_config()
            GW.refresh(force=True)
            MON.invalidate()
            _rack_cache.pop(rack["name"], None)
            BUS.publish("config", "racks", {"racks": [x["name"] for x in racks()]})
            TRACE.log("rack", action="save", rack=rack["name"], target=rack["ssh"], gateway=rack.get("gateway"),
                      monitor=rack.get("monitor"))
            self._json({"ok": True, "rack": rack})
            return
        if action == "remove":
            r = rack_named(str(body.get("name") or "")) if body.get("name") else None
            if r is None:
                self._json({"error": "no such rack"}, 404)
                return
            note = ""
            if r["paired"] and body.get("unpair", True) is not False:
                try:
                    out = subprocess.run(rack_cmd(["unpair"], r), capture_output=True, text=True, timeout=30)
                    note = "its key is off %s" % r["ssh"] if out.returncode == 0 else \
                        "could not take the key back (%s): on %s, rack unpair --name %s" % (
                            racksmod._last(out.stderr) or "exit %d" % out.returncode, r["ssh"], r["app"] or "<app>")
                except (OSError, subprocess.SubprocessError) as e:
                    note = "could not take the key back (%s): on %s, rack unpair --name %s" % (e, r["ssh"], r["app"] or "<app>")
            CFG["racks"] = [x for x in (CFG.get("racks") or []) if not (isinstance(x, dict) and x.get("name") == r["name"])]
            if body.get("gateway") and r["gateway"]:
                CFG["gateways"] = [g for g in (CFG.get("gateways") or []) if not (isinstance(g, dict) and g.get("name") == r["gateway"])]
                gwmod.normalize(CFG)
            if body.get("monitor") and r["monitor"]:
                CFG["monitors"] = [m for m in (CFG.get("monitors") or []) if not (isinstance(m, dict) and m.get("name") == r["monitor"])]
            save_config()
            GW.refresh(force=True)
            MON.invalidate()
            _rack_cache.pop(r["name"], None)
            BUS.publish("config", "racks", {"racks": [x["name"] for x in racks()]})
            TRACE.log("rack", action="remove", rack=r["name"], target=r["ssh"], note=note)
            self._json({"ok": True, "note": note})
            return
        self._json({"error": "unknown action"}, 400)

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
                text = _ANSI.sub("", text)        # a terminal's colours are noise in the app
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

    def _git_admin(self, body):
        """Connect a git host (store a token in the vault), disconnect it, or
        probe a URL to tell public from private. Tokens are write-only: they go
        in, they never come back out of the API."""
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action") or ""
        if action == "probe":
            url = str(body.get("url") or "").strip()
            if not url:
                self._json({"error": "a repository URL is required"}, 400)
                return
            host = vaultmod.host_of(url)
            local = not url.startswith(("http://", "https://", "git@", "ssh://", "file://"))
            if local:
                self._json({"host": "", "local": True, "reachable": os.path.isdir(os.path.expanduser(url)),
                            "needs_auth": False, "connected": False, "error": ""})
                return
            rc, err = git_ls_remote(url, VAULT.git_token(host))
            reachable = rc == 0
            self._json({"host": host, "local": False, "reachable": reachable,
                        "connected": bool(VAULT.git_token(host)),
                        "needs_auth": (not reachable) and (vaultmod.looks_like_auth(err) or not VAULT.git_token(host)),
                        "error": "" if reachable else (err or "").strip()[-200:]})
            return
        if action == "connect":
            url = str(body.get("url") or "").strip()
            token = str(body.get("token") or "").strip()
            host = (str(body.get("host") or "").strip() or vaultmod.host_of(url)).lower()
            user = str(body.get("user") or "").strip()
            if not host:
                self._json({"error": "could not tell the host; add the repository URL"}, 400)
                return
            if not token:
                self._json({"error": "a token is required"}, 400)
                return
            rec = {"token": token, "user": user, "kind": "pat"}
            if url:        # validate against the very repo they are adding
                rc, err = git_ls_remote(url, rec)
                if rc != 0:
                    if err and not vaultmod.looks_like_auth(err):
                        self._json({"error": "could not reach that repository: " + err.strip()[-160:]}, 400)
                    else:
                        self._json({"error": "the token did not work for that repository"}, 400)
                    return
            if not user and host == "github.com":
                user = github_login(token)
            VAULT.set_git(host, token, user=user, kind="pat")
            TRACE.log("git", action="connect", host=host, user=user)   # the token is never logged
            self._json({"ok": True, "host": host, "user": user, "hosts": VAULT.git_hosts()})
            return
        if action == "disconnect":
            host = str(body.get("host") or "").strip().lower()
            if not VAULT.delete_git(host):
                self._json({"error": "not connected"}, 404)
                return
            TRACE.log("git", action="disconnect", host=host)
            self._json({"ok": True, "hosts": VAULT.git_hosts()})
            return
        self._json({"error": "unknown action"}, 400)

    def _plugin_admin(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action")
        name = (body.get("name") or "").strip()
        found, warnings = discover_plugins()
        mirror = None
        touched = name          # the plugin whose skills may need mirroring
        if action in ("add", "create", "remove", "sync"):
            before = set(found)
            err = self._plugin_install(action, body, found)
            if err:
                self._json({"error": err}, 400)
                return
            found, warnings = discover_plugins()
            if action == "add" and not touched:          # name derived from the git URL
                touched = next(iter(set(found) - before), "")
        elif action in ("enable", "disable"):
            if name not in found:
                self._json({"error": "no such plugin"}, 404)
                return
            _plugin_state().setdefault(name, {})["enabled"] = (action == "enable")
            save_config()
            TRACE.log("plugin", action=action, name=name,
                      contributes=found[name].info(action == "enable"))
            # a plugin's MCP servers come or go with it, in the background: one that takes
            # its time to start (npx fetching a package) must not hold the button
            mcp_reload(wait=False)
        elif action != "rescan":
            self._json({"error": "unknown action"}, 400)
            return
        st = _plugin_state()
        # an enabled bundle's private skills follow it to the agent worker, so
        # the Sultan can attach them on the next run (only when agents are on)
        if action in ("add", "sync", "enable") and touched in found:
            p = found[touched]
            if p.skills_dir and st.get(touched, {}).get("enabled"):
                mirror = agentmod.mirror_skills_dir(CFG, p.skills_dir)
        self._json({"ok": True, "plugins": [found[n].info(bool(st.get(n, {}).get("enabled")))
                                            for n in sorted(found)], "warnings": warnings,
                    "mirror": mirror})

    def _plugin_install(self, action, body, found):
        """add: git URL or local path into the plugins dir; sync: git pull a
        plugin the console cloned; create: a new empty plugin (manifest +
        optional MCP servers); remove: delete one the console installed.
        Returns an error string or None."""
        import shutil, subprocess
        root = plugins_install_dir()
        if action == "sync":
            name = (body.get("name") or "").strip()
            p = found.get(name)
            if not p:
                return "no such plugin"
            if not os.path.isdir(os.path.join(p.root, ".git")):
                return "%s is not a git checkout; to sync it, remove it and add it again from a git URL" % name
            remote = (skillmod.git_status(p.root) or {}).get("remote") or ""
            env = vaultmod.git_env(VAULT_DIR, VAULT.git_token(vaultmod.host_of(remote)))
            try:
                subprocess.run(["git", "-C", p.root, "fetch", "origin"],
                               capture_output=True, text=True, timeout=120, env=env)
                r = subprocess.run(["git", "-C", p.root, "pull", "--ff-only"],
                                   capture_output=True, text=True, timeout=120, env=env)
            except (OSError, subprocess.TimeoutExpired) as e:
                return "git pull failed: %s" % e
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or "").strip()[-300:]
                if vaultmod.looks_like_auth(detail):
                    return "git pull failed: the token for %s no longer works — reconnect it on the Plugins page" % (
                        vaultmod.host_of(remote) or "this host")
                return "git pull failed: " + (detail or "the folder has diverged from its remote; resolve it on disk")
            TRACE.log("plugin", action="sync", name=name, source=(skillmod.git_status(p.root) or {}).get("remote"))
            mcp_reload(wait=False)      # its servers restart in the background, as on enable
            return None
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
            mcp_reload(wait=False)
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
        if source.startswith(("http://", "https://", "git@", "ssh://", "file://")):
            env = vaultmod.git_env(VAULT_DIR, VAULT.git_token(vaultmod.host_of(source)))
            try:
                # a full clone (not --depth 1) so a later `sync` is a clean
                # `git pull --ff-only`; skills/MCP repos are small
                r = subprocess.run(["git", "clone", source, dest],
                                   capture_output=True, text=True, timeout=120, env=env)
            except (OSError, subprocess.TimeoutExpired) as e:
                return "git clone failed: %s" % e
            if r.returncode != 0:
                shutil.rmtree(dest, ignore_errors=True)
                detail = (r.stderr or "").strip()[-300:]
                if vaultmod.looks_like_auth(detail):
                    return ("that repo needs sign-in: connect %s on the Plugins page (paste a token), then add it again"
                            % (vaultmod.host_of(source) or "the host"))
                return "git clone failed: " + detail
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
            params = body.get("params") or {}
            why = catmod.problem(entry, params)
            if why:
                self._json({"error": why}, 400)
                return
            cmd, args = catmod.render(entry, params)
            builtin = catmod.builtin_terminal(servers) if entry.get("single") else None
            if builtin:
                # one terminal: adding it turns the built-in on, in the folder asked for
                servers[builtin]["args"] = [servers[builtin]["args"][0]] + args[1:]
                servers[builtin]["enabled"] = True
                name = builtin
                TRACE.log("mcp", action="catalog_add", name=name, catalog=entry["id"], builtin=True)
                save_config()
                h = mcp_reload(restart=(name,))
                self._json({"ok": True, "name": name, "servers": h.status, "config": CFG.get("mcp_servers", {})})
                return
            name = name or entry["id"]
            if not re.match(r"^[A-Za-z0-9_-]{1,32}$", name):
                self._json({"error": "name must be 1-32 chars: letters, digits, - or _"}, 400)
                return
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
        self._json({"ok": True, "name": name, "servers": h.status,
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


def serve(bind=None, port=None, idle_exit=None):
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
    if _CFG_MIGRATED and os.path.exists(CONFIG_PATH):
        # an older config, brought up to date: the original stays beside it
        kept = _set_aside(CONFIG_PATH, "before-v%d" % CONFIG_VERSION)
        save_config()
        TRACE.log("config", action="migrated", steps=list(_CFG_MIGRATED), kept=os.path.basename(kept))
        del _CFG_MIGRATED[:]
    inst.publish(srv.server_address[1], version=app_version(), desktop=bool(os.environ.get("BYTEBUNKER_DESKTOP")))
    if os.environ.get("BYTEBUNKER_DESKTOP"):
        import shutil
        UPDATE_STATE.update(read_update_state())
        shutil.rmtree(os.path.join(update_home(), "staging"), ignore_errors=True)
    import atexit
    atexit.register(release_instance)
    # the built-in jobs MCP server calls the console back on this port, with the token
    os.environ["BB_CONSOLE_PORT"] = str(srv.server_address[1])
    import mcp as mcpmod
    mcpmod.APP_TOKEN = local_token()
    start_scheduler()
    threading.Thread(target=watch_models, name="models", daemon=True).start()
    if idle_exit:
        threading.Thread(target=watch_idle, args=(int(idle_exit),), name="idle", daemon=True).start()
    mcp_host()      # servers start in the background, ready before the first chat
    return srv


INSTANCE = None
STARTED = time.time()

# ---- the local token: the API answers this server's own clients only ---------
TOKEN_COOKIE = "bb_token"
# /api/hello finds the server (no data in it); agents on this computer report usage with no token
OPEN_API = ("/api/hello", "/api/usage-event")
_HANDOFFS = {}
_HANDOFF_LOCK = threading.Lock()


def local_token():
    """This server's token, or None: no instance (a bare test server), or the
    check switched off by whoever started it (BYTEBUNKER_LOCAL_TOKEN=off)."""
    if os.environ.get("BYTEBUNKER_LOCAL_TOKEN") == "off" or INSTANCE is None:
        return None
    return (INSTANCE.info or {}).get("token")


def handoff_code(ttl_s=120):
    """A one-time code for GET /handoff, good for ttl_s."""
    now = time.time()
    code = secrets.token_urlsafe(24)
    with _HANDOFF_LOCK:
        for k, exp in list(_HANDOFFS.items()):
            if exp < now:
                del _HANDOFFS[k]
        _HANDOFFS[code] = now + ttl_s
    return code


def take_handoff(code):
    with _HANDOFF_LOCK:
        exp = _HANDOFFS.pop(code, None) if code else None
    return exp is not None and exp >= time.time()


def handoff_url(port):
    return "http://127.0.0.1:%d/handoff?code=%s" % (int(port), handoff_code())
STOPPING = threading.Event()
IDLE = {"last": time.time()}


def watch_idle(limit_s):
    """A server bb started on demand: gone after limit_s with no request, no
    app window open (no event stream) and nothing running, unless Settings
    says to keep it running."""
    while not STOPPING.wait(max(1, min(30, limit_s // 4))):
        if CFG.get("keep_running") or _LISTENING[0] > 0 or RUNS.active():
            IDLE["last"] = time.time()
            continue
        if time.time() - IDLE["last"] >= limit_s:
            TRACE.log("server", action="idle_exit", idle_s=int(time.time() - IDLE["last"]))
            release_instance()
            try:
                if _MCP is not None:
                    _MCP.stop_all()
            except Exception:   # noqa: BLE001
                pass
            os._exit(0)
_LISTENING = [0]                 # open /api/events streams: someone to tell about changes
_LISTEN_LOCK = threading.Lock()


def _models_signature():
    return (tuple(sorted(m["id"] for m in GW.models)),
            tuple(sorted((n, bool(st.get("ok"))) for n, st in GW.status.items())))


def watch_models():
    """Models come and go as engines start and stop. While an app window is
    open, look every models_watch_s (30 s) and say when the list changed, so
    the picker and the Models screen follow without a reload."""
    last = None
    while not STOPPING.is_set():
        every = max(2, _int(CFG.get("models_watch_s"), 30))
        if _LISTENING[0] > 0:
            try:
                if MON.list():
                    MON.cluster(0, max_age=every)       # the serving blocks, for caps_for
                GW.refresh(force=True)
                sig = _models_signature()
                if last is not None and sig != last:
                    BUS.publish("models", "changed", {"models": len(sig[0])})
                last = sig
            except Exception:   # noqa: BLE001 - a bad probe must not end the watch
                pass
        STOPPING.wait(every)


def app_version():
    return os.environ.get("BYTEBUNKER_VERSION") or VERSION


# ---- updating the desktop app (updater.py does the work) --------------------
UPDATE_STATE = {}       # what the last update did, read once at start; said until seen


def update_home():
    """Downloads, the unpacked new version and the previous one: beside the user's data."""
    return os.path.join(os.path.dirname(CONFIG_PATH), "updates")


def read_update_state():
    """After an update this process is the new version, or, when the swap
    failed, still the old one: what to tell the user."""
    try:
        with open(os.path.join(update_home(), "pending.json"), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return {}
    said = ""
    try:
        with open(os.path.join(update_home(), "updater.log"), encoding="utf-8") as f:
            said = (f.read().strip().splitlines() or [""])[-1]
    except OSError:
        pass
    if m.get("to") == app_version():
        return {"updated": {"from": m.get("from"), "to": m["to"],
                            "notes": (changelog.section(m["to"]) or {}).get("body", "")}}
    return {"update_failed": {"to": m.get("to"), "page": m.get("page") or "",
                              "why": said if said and said != "updated" else "the new version did not start"}}


def clear_update_state():
    UPDATE_STATE.clear()
    for name in ("pending.json", "updater.log"):
        try:
            os.remove(os.path.join(update_home(), name))
        except OSError:
            pass


def _leave_for_update():
    """The swap helper waits for this process to end. A window closes itself
    when its page asks; this ends a server that has no window of its own."""
    release_instance()
    try:
        if _MCP is not None:
            _MCP.stop_all()
    except Exception:   # noqa: BLE001
        pass
    os._exit(0)


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
    ap.add_argument("--idle-exit", type=int, help="exit after this many idle seconds (a server bb started)")
    args = ap.parse_args()
    srv = serve(args.bind, args.port, idle_exit=args.idle_exit)
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
