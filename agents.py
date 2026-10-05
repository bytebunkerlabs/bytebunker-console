"""Launch and observe the agent harness from the console — safely.

The console is the CONTROL plane. It never runs agent code itself. When you
launch a goal, the harness (bytebunker-agents) runs on a separate AGENT host,
and the models it calls run on the Sparks (the MODEL plane). Three planes,
which is what NVIDIA's agentic-safety guidance means by separating how agents
execute from how they reach data and tools, and by keeping monitoring
independent of the host that runs the agents.

This module only builds the launch command and streams the process output. It
refuses to run unless `agents.enabled` is true AND a harness directory is set,
so the feature is inert until you deliberately point it at a worker host.

Config (config.json):

    "agents": {
      "enabled": false,               // master switch; false = inert
      "ssh": "leagueofash",           // agent host over SSH; "" = run locally (NOT isolated)
      "dir": "~/bytebunker-harness",  // the harness checkout on that host
      "python": "uv run",             // launcher; "uv run" or e.g. "python3"
      "script": "scripts/run_master.py"
    }
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time


class AgentConfigError(Exception):
    pass


def agent_cfg(CFG):
    return CFG.get("agents") or {}


def status(CFG):
    a = agent_cfg(CFG)
    ssh = (a.get("ssh") or "").strip()
    return {
        "enabled": bool(a.get("enabled")),
        "mode": "ssh" if ssh else "local",
        "host": ssh or "this machine",
        "dir": a.get("dir") or "",
        "isolated": bool(ssh),           # local == same host as the console: not isolated
        "configured": bool(a.get("dir")),
    }


def ssh_available():
    """The OpenSSH client: built into macOS and Linux; an optional feature
    on Windows 10/11 (Settings > Apps > Optional features > OpenSSH Client)."""
    import shutil
    import sys as _sys
    if shutil.which("ssh"):
        return True
    return _sys.platform == "win32" and os.path.exists(r"C:\Windows\System32\OpenSSH\ssh.exe")


def test_worker(ssh, directory, python="uv run", script="scripts/run_master.py"):
    """Can this console start the harness on that host? One ssh round trip
    that reports what is there; nothing is installed or changed."""
    if not ssh_available():
        return {"ok": False, "error": "no ssh client on this machine" + (
            ": install it from Settings > Apps > Optional features > OpenSSH Client" if os.name == "nt" else "")}
    d = directory or "~/bytebunker-harness"
    probe = ("cd %s 2>/dev/null || { echo NO_DIR; exit 0; }; "
             "test -f %s && echo HARNESS_OK || echo NO_HARNESS; "
             "(command -v podman || command -v docker) >/dev/null 2>&1 && echo RUNTIME_OK || echo NO_RUNTIME; "
             "%s --version >/dev/null 2>&1 && echo LAUNCHER_OK || echo NO_LAUNCHER; "
             "uname -sm") % (_rq(d), shlex.quote(script), python.split()[0])
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh, probe],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:   # noqa: BLE001
        return {"ok": False, "error": "ssh failed: %s" % str(e)[:160]}
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        err = (r.stderr or "no answer").strip()[-200:]
        if "Host key verification failed" in err:
            err += (" This machine has not verified %s's host key yet. Connect once from a terminal "
                    "(ssh %s), check the fingerprint and accept it, then test again." % (ssh, ssh))
        elif "Permission denied" in err:
            err += " Add this machine's public key to the worker's ~/.ssh/authorized_keys (ssh-copy-id %s)." % ssh
        return {"ok": False, "error": "ssh %s: %s" % (ssh, err)}
    checks = {"reachable": True, "harness": "HARNESS_OK" in out, "container runtime": "RUNTIME_OK" in out,
              "launcher (%s)" % python.split()[0]: "LAUNCHER_OK" in out}
    if "NO_DIR" in out:
        checks["harness"] = False
    system = out.splitlines()[-1] if out and "_OK" not in out.splitlines()[-1] and "NO_" not in out.splitlines()[-1] else ""
    return {"ok": all(checks.values()), "checks": checks, "system": system}


def build_command(CFG, goal):
    """Return an argv list to spawn. Raises AgentConfigError when the feature
    is off or unconfigured. The goal is never interpolated into a shell
    unquoted: locally it is its own argv element; over SSH it is shlex-quoted
    inside the single remote command string."""
    a = agent_cfg(CFG)
    if not a.get("enabled"):
        raise AgentConfigError(
            "agents are disabled. Set agents.enabled and agents.dir in config.json, "
            "pointing at a dedicated worker host — not a Spark and not this console.")
    goal = (goal or "").strip()
    if not goal:
        raise AgentConfigError("a goal is required")
    if len(goal) > 8000:
        raise AgentConfigError("goal too long")
    directory = (a.get("dir") or "").strip()
    if not directory:
        raise AgentConfigError("agents.dir is not set")
    launcher = (a.get("python") or "uv run").strip()
    script = (a.get("script") or "scripts/run_master.py").strip()
    ssh = (a.get("ssh") or "").strip()

    # The master's persona travels as env so the harness picks it up without
    # editing the worker's yaml. Only set when non-empty.
    # Python block-buffers stdout when it is a pipe, so the master's narration
    # arrived in one lump at exit (or never, if the run was stopped). Unbuffered
    # makes it stream line by line, which is the point of watching a run.
    envs = {"PYTHONUNBUFFERED": "1"}
    if (a.get("master_name") or "").strip():
        envs["BYTEBUNKER_MASTER_NAME"] = a["master_name"].strip()
    if (a.get("master_instructions") or "").strip():
        envs["BYTEBUNKER_MASTER_INSTRUCTIONS"] = a["master_instructions"].strip()
    # which engine each role uses — chosen on the Agents screen, overriding the
    # worker's config.yaml for this run only
    for key, env in (("master_model", "BYTEBUNKER_MASTER_MODEL"), ("thinking_model", "BYTEBUNKER_THINKING_MODEL"),
                     ("slave_model", "BYTEBUNKER_SLAVE_MODEL")):
        if (a.get(key) or "").strip():
            envs[env] = a[key].strip()

    if ssh:
        # one remote command string; every interpolated value is quoted. A
        # leading ~/ is kept outside the quotes so the remote shell still
        # expands it to the worker's home — shlex.quote would otherwise quote
        # the ~ and cd would look for a literal "~" directory.
        if directory == "~":
            qdir = "~"
        elif directory.startswith("~/"):
            qdir = "~/" + shlex.quote(directory[2:])
        else:
            qdir = shlex.quote(directory)
        # setsid execs a COMMAND, so env assignments must go through `env`
        # rather than as a shell prefix (setsid would try to run "VAR=x").
        env_prefix = ("env " + "".join("%s=%s " % (k, shlex.quote(v)) for k, v in envs.items())) if envs else ""
        launch = "%s%s %s --goal %s" % (env_prefix, launcher, shlex.quote(script), shlex.quote(goal))
        # Killing the local ssh does NOT stop the remote harness — without a
        # tty the remote command just keeps running, and its slaves with it
        # (measured: two masters and their researchers still alive an hour
        # after their streams were closed). So the harness runs in its own
        # process group under setsid, and a sidecar watches our stdin: when
        # the console closes it (Stop, or a client that went away) the whole
        # group gets TERM, then KILL. podman forwards TERM into the container.
        # bash (the worker's login shell) gives a backgrounded command stdin
        # from /dev/null when job control is off, so a backgrounded `cat`
        # would see EOF at once and kill the run a second in (measured). The
        # real stdin is saved on fd 3 first and handed to the sidecar
        # explicitly; the harness itself correctly gets /dev/null.
        remote = ("cd %s && exec 3<&0; if command -v setsid >/dev/null 2>&1; then setsid %s & "
                  "else %s & fi; CPID=$!; "
                  "( cat <&3 >/dev/null; kill -TERM -- -$CPID 2>/dev/null; kill -TERM $CPID 2>/dev/null; "
                  "sleep 8; kill -KILL -- -$CPID 2>/dev/null; kill -KILL $CPID 2>/dev/null; "
                  # belt and braces: any slave container the dead harness left behind
                  "for rt in podman docker; do command -v $rt >/dev/null 2>&1 && "
                  "$rt ps -q --filter label=bytebunker.pgid=$CPID 2>/dev/null | xargs -r $rt kill >/dev/null 2>&1; done"
                  " ) >/dev/null 2>&1 & "
                  "SIDE=$!; wait $CPID; RC=$?; pkill -P $SIDE 2>/dev/null; kill $SIDE 2>/dev/null; exit $RC"
                  ) % (qdir, launch, launch)
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", ssh, remote]

    # local: no shell, goal is a discrete argv element; env via a leading `env`
    env_argv = ["env"] + ["%s=%s" % (k, v) for k, v in envs.items()] if envs else []
    return env_argv + shlex.split(launcher) + [script, "--goal", goal]


def recent_slaves(CFG, limit=40):
    """Read the harness's per-spawn trajectory log so the console can show what
    each agent (slave) actually did. Returns {ok, slaves:[...]} or {ok:False}.
    Read-only tail; never blocks a launch."""
    import json as _json
    a = agent_cfg(CFG)
    directory = (a.get("dir") or "").strip()
    if not directory:
        return {"ok": False, "error": "agents.dir not set"}
    ssh = (a.get("ssh") or "").strip()
    rel = "trajectories/spawns.jsonl"
    # Running slaves stream events to their host-mounted task dir
    # (/tmp/bb-<role>-<id>-*/events.jsonl); files touched in the last 30 min
    # are read too, so a slave shows up WHILE it works, not only after.
    live_cmd = ("echo ===MASTER===; G=$(ls -td %s/trajectories/goal-*/ 2>/dev/null | head -1); "
                "[ -n \"$G\" ] && tail -n 14 \"$G/master.jsonl\" 2>/dev/null; "
                "echo ===LIVE===; RUNNING=\" $(podman ps --format '{{.Names}}' 2>/dev/null | tr '\\n' ' ') \"; "
                "for f in $(find /tmp/ -maxdepth 2 -name events.jsonl -mmin -60 -path '*/bb-*' 2>/dev/null | head -12); do "
                "n=$(basename $(dirname $f) | cut -d- -f2,3); case \"$RUNNING\" in *\" $n \"*) echo \"### $f\"; tail -n 10 \"$f\";; esac; done")
    if ssh:
        d = directory
        if d == "~":
            qd = "~"
        elif d.startswith("~/"):
            qd = "~/" + shlex.quote(d[2:])
        else:
            qd = shlex.quote(d)
        remote = "tail -n %d %s/%s 2>/dev/null; %s" % (int(limit), qd, rel, live_cmd % qd)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", ssh, remote]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
        except (subprocess.TimeoutExpired, OSError) as e:
            return {"ok": False, "error": str(e)[:150]}
    else:
        import os
        path = os.path.join(os.path.expanduser(directory), rel)
        try:
            with open(path, encoding="utf-8") as f:
                out = "".join(f.readlines()[-limit:])
        except OSError as e:
            return {"ok": False, "error": str(e)[:150]}
        try:
            out += subprocess.run(["sh", "-c", live_cmd % shlex.quote(os.path.expanduser(directory))], capture_output=True, text=True, timeout=10).stdout
        except (subprocess.TimeoutExpired, OSError):
            pass
    spawn_part, _, rest = out.partition("===MASTER===")
    master_part, _, live_part = rest.partition("===LIVE===")
    master = []
    for line in master_part.splitlines():
        try:
            e = _json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            master.append({k: (v[:200] if isinstance(v, str) else v) for k, v in e.items() if k != "brief"})
    live = []
    cur = None
    for line in live_part.splitlines():
        if line.startswith("### "):
            path = line[4:].strip()
            base = path.split("/")[-2] if "/" in path else path      # bb-<role>-<id>-<rand>
            parts = base.split("-")
            cur = {"name": "-".join(parts[1:3]) if len(parts) >= 3 else base, "path": path, "events": []}
            live.append(cur)
            continue
        if cur is None:
            continue
        try:
            e = _json.loads(line)
        except ValueError:
            continue
        cur["events"].append({k: (v if not isinstance(v, str) else v[:160]) for k, v in e.items()})
    slaves = []
    for line in spawn_part.splitlines():
        try:
            r = _json.loads(line)
        except ValueError:
            continue
        fr = r.get("final_result") or {}
        ans = fr.get("answer") if isinstance(fr, dict) else ""
        slaves.append({
            "id": r.get("slave_id"), "goal": r.get("goal_id"),
            "role": r.get("role"), "depth": r.get("depth"),
            "brief": (r.get("brief") or "")[:200], "success": r.get("success"),
            "tokens": r.get("tokens"), "network": r.get("network_granted"),
            "answer": (ans or "")[:400], "error": (r.get("error") or "")[:200],
            "ts": r.get("timestamp"),
        })
    return {"ok": True, "slaves": slaves[::-1], "live": live, "master": master}


_SLAVE_ID = __import__("re").compile(r"^[A-Za-z0-9_-]{1,64}$")


def slave_detail(CFG, slave_id):
    """Everything the harness has about one slave: its spawn record (brief,
    answer, evidence, unknowns, confidence, tokens, error) and its full
    event timeline — the tracer file for a finished slave, the live
    events.jsonl in its task dir for a running one. The console shows this
    when you click an agent."""
    import json as _json, os
    if not _SLAVE_ID.match(slave_id or ""):
        return {"ok": False, "error": "bad slave id"}
    a = agent_cfg(CFG)
    directory = (a.get("dir") or "").strip()
    if not directory:
        return {"ok": False, "error": "agents.dir not set"}
    sid = slave_id
    script = (
        "cd %s 2>/dev/null; grep -F '\"slave_id\": \"%s\"' trajectories/spawns.jsonl 2>/dev/null | tail -n 1; "
        "echo ===TRACE===; cat trajectories/*/%s.jsonl 2>/dev/null | tail -n 300; "
        "echo ===LIVE===; tail -n 300 /tmp/bb-%s-*/events.jsonl 2>/dev/null; "
        "echo ===INPUT===; head -c 30000 /tmp/bb-%s-*/input.json 2>/dev/null"
    )
    ssh = (a.get("ssh") or "").strip()
    if ssh:
        d = directory
        qd = "~" if d == "~" else ("~/" + shlex.quote(d[2:]) if d.startswith("~/") else shlex.quote(d))
        remote = script % (qd, sid, sid, sid, sid)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", ssh, remote]
    else:
        cmd = ["sh", "-c", script % (shlex.quote(os.path.expanduser(directory)), sid, sid, sid, sid)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=25).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": str(e)[:150]}
    rec_part, _, rest = out.partition("===TRACE===")
    trace_part, _, rest = rest.partition("===LIVE===")
    live_part, _, input_part = rest.partition("===INPUT===")
    record = None
    for line in rec_part.strip().splitlines():
        try:
            record = _json.loads(line)
        except ValueError:
            pass
    events = []
    for part in (trace_part, live_part):
        for line in part.splitlines():
            try:
                e = _json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                events.append(e)
    events.sort(key=lambda e: e.get("ts") or 0)
    brief = None
    try:
        inp = _json.loads(input_part.strip()) if input_part.strip() else None
        if inp:
            brief = (inp.get("spec") or {}).get("brief")
    except ValueError:
        pass
    fr = (record or {}).get("final_result") or {}
    return {
        "ok": True, "id": sid, "found": bool(record) or bool(events) or bool(brief),
        "record": record and {
            "role": record.get("role"), "depth": record.get("depth"), "goal": record.get("goal_id"),
            "skills": record.get("skills"), "network": record.get("network_granted"),
            "success": record.get("success"), "tokens": record.get("tokens"),
            "error": record.get("error"), "ts": record.get("timestamp"),
            "brief": record.get("brief"),
            "answer": fr.get("answer") if isinstance(fr, dict) else None,
            "evidence": fr.get("evidence") if isinstance(fr, dict) else None,
            "unknowns": fr.get("unknowns") if isinstance(fr, dict) else None,
            "confidence": fr.get("confidence") if isinstance(fr, dict) else None,
        },
        "brief": (record or {}).get("brief") or brief,
        "events": events[-400:],
        "running": bool(live_part.strip()) and not record,
    }


_STATS_PY = r"""
import json, os, glob, time, subprocess
out = {"ts": time.time()}
try:
    names = subprocess.run(["podman", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=8).stdout.split()
except Exception:
    names = []
out["containers"] = names
try:
    ps = subprocess.run(["pgrep", "-fc", r"python scripts/run_maste[r].py"], capture_output=True, text=True, timeout=5).stdout.strip()
    out["masters"] = int(ps or 0)
except Exception:
    out["masters"] = None
# a slave is live iff its container is running (containers are named after
# the slave); a recently touched events file is NOT enough — a killed run
# leaves fresh files behind
now = time.time()
out["live"] = sorted(set(names))
agg = {"n": 0, "ok": 0, "unverified": 0, "failed": 0, "tokens": 0, "roles": {}, "last_ts": 0}
try:
    since = now - 86400
    with open(os.path.expanduser("~/bytebunker-harness/trajectories/spawns.jsonl")) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("timestamp") or 0) < since:
                continue
            agg["n"] += 1
            fr = r.get("final_result")
            answered = isinstance(fr, dict) and bool(fr.get("answer"))
            agg["ok" if r.get("success") else ("unverified" if answered else "failed")] += 1
            agg["tokens"] += int(r.get("tokens") or 0)
            role = r.get("role") or "?"
            agg["roles"][role] = agg["roles"].get(role, 0) + 1
            agg["last_ts"] = max(agg["last_ts"], r.get("timestamp") or 0)
except OSError:
    pass
out["day"] = agg
# A second engine on this worker — a small model on the local GPU — shown
# live on the Cluster screen: vLLM's own /metrics plus nvidia-smi.
fm = {"metrics_url": __FAST_METRICS__}
try:
    import urllib.request, re as _re
    txt = urllib.request.urlopen(fm["metrics_url"], timeout=4).read().decode("utf-8", "replace")
    vals = {}
    for ln in txt.splitlines():
        if not ln.startswith("vllm:") or " " not in ln:
            continue
        name, v = ln.rsplit(" ", 1)
        base = name.split("{", 1)[0]
        try:
            vals[base] = vals.get(base, 0.0) + float(v)
        except ValueError:
            continue
        if "model" not in fm:
            m = _re.search(r'model_name="([^"]+)"', name)
            if m:
                fm["model"] = m.group(1)
    kv = vals.get("vllm:kv_cache_usage_perc", vals.get("vllm:gpu_cache_usage_perc", 0.0))
    fm.update(up=True,
              running=int(vals.get("vllm:num_requests_running", 0)),
              waiting=int(vals.get("vllm:num_requests_waiting", 0)),
              kv_pct=round(100.0 * kv, 1),
              gen_total=vals.get("vllm:generation_tokens_total", 0.0),
              prompt_total=vals.get("vllm:prompt_tokens_total", 0.0),
              requests_total=int(vals.get("vllm:request_success_total", 0)),
              prefix_hits=vals.get("vllm:prefix_cache_hits_total", 0.0),
              prefix_queries=vals.get("vllm:prefix_cache_queries_total", 0.0))
except Exception as e:
    fm.update(up=False, error=str(e)[:80])
for smi in ("/usr/lib/wsl/lib/nvidia-smi", "nvidia-smi"):
    try:
        q = subprocess.run([smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6).stdout.strip().splitlines()
        if q:
            name, util, mu, mt, temp = [x.strip() for x in q[0].split(",")]
            fm["gpu"] = {"name": name, "util": int(float(util)), "mem_used_mb": int(float(mu)),
                         "mem_total_mb": int(float(mt)), "temp": int(float(temp))}
            break
    except Exception:
        continue
out["fast_model"] = fm
try:
    la = os.getloadavg(); out["load1"] = round(la[0], 2)
    mem = {}
    for ln in open("/proc/meminfo"):
        k, v = ln.split(":", 1); mem[k] = int(v.split()[0])
    out["mem_used_gb"] = round((mem["MemTotal"] - mem["MemAvailable"]) / 2**20, 1)
    out["mem_total_gb"] = round(mem["MemTotal"] / 2**20, 1)
except Exception:
    pass
print(json.dumps(out))
"""
_USAGE_PY = r"""
import json, os, glob, time, re
now = time.time(); horizon = now - 14 * 86400
root = os.path.expanduser("~/bytebunker-harness")
out = {"days": {}, "by_role": {}, "by_model": {}, "slaves": 0, "goals": set(), "slave_tokens": 0,
       "slave_prompt": 0, "slave_completion": 0, "master_prompt": 0, "master_completion": 0,
       "master_rounds": 0, "panel_tokens": 0, "panels": 0}
# role -> model, for records written before the ledger carried a model
models = {"default": "", "thinking": "", "master": ""}
try:
    cfg = open(os.path.join(root, "config", "config.yaml")).read()
    m = re.search(r'(?m)^\s*default_slave_model:\s*"?([^"\n]+)"?', cfg); models["default"] = (m.group(1).strip() if m else "")
    m = re.search(r'(?m)^\s*thinking_model:\s*"?([^"\n]+)"?', cfg); models["thinking"] = (m.group(1).strip() if m else "")
    m = re.search(r'(?m)^\s*master_model:\s*"?([^"\n]+)"?', cfg); models["master"] = (m.group(1).strip() if m else "")
except OSError:
    pass
THINKING_ROLES = ("wazir", "malikah")
def day(ts): return time.strftime("%d", time.localtime(ts))
def bump(d, k, n): d[k] = d.get(k, 0) + n
try:
    with open(os.path.join(root, "trajectories", "spawns.jsonl")) as fh:
        for line in fh:
            try: r = json.loads(line)
            except ValueError: continue
            ts = r.get("timestamp") or 0
            if ts < horizon: continue
            tok = int(r.get("tokens") or 0); pt = int(r.get("prompt_tokens") or 0); ct = int(r.get("completion_tokens") or 0)
            role = r.get("role") or "?"
            model = r.get("model") or (models["thinking"] or models["master"] if role in THINKING_ROLES else models["default"]) or "?"
            out["slaves"] += 1; out["goals"].add(r.get("goal_id"))
            out["slave_tokens"] += tok; out["slave_prompt"] += pt; out["slave_completion"] += ct
            bump(out["by_role"], role, tok); bump(out["by_model"], model, tok)
            d = out["days"].setdefault(day(ts), {"slaves": 0, "master": 0, "panels": 0})
            d["slaves"] += tok
except OSError:
    pass
for path in glob.glob(os.path.join(root, "trajectories", "goal-*", "master.jsonl")):
    try:
        if os.path.getmtime(path) < horizon: continue
        with open(path) as fh:
            for line in fh:
                try: e = json.loads(line)
                except ValueError: continue
                ts = e.get("ts") or 0
                if ts < horizon: continue
                k = e.get("kind")
                if k == "llm":
                    pt = int(e.get("prompt_tokens") or 0); ct = int(e.get("completion_tokens") or 0)
                    out["master_prompt"] += pt; out["master_completion"] += ct; out["master_rounds"] += 1
                    out["days"].setdefault(day(ts), {"slaves": 0, "master": 0, "panels": 0})["master"] += pt + ct
                    bump(out["by_model"], models["master"] or "master", pt + ct)
                elif k == "panel":
                    t = int(e.get("tokens") or 0); out["panel_tokens"] += t; out["panels"] += 1
                    out["days"].setdefault(day(ts), {"slaves": 0, "master": 0, "panels": 0})["panels"] += t
                    bump(out["by_model"], models["thinking"] or models["master"] or "?", t)
    except OSError:
        continue
out["goals"] = len([g for g in out["goals"] if g])
out["models"] = models
print(json.dumps(out))
"""

_usage_cache = {"at": 0, "val": None}


def agent_usage(CFG, max_age=60):
    """Agent-plane token usage for the Usage screen: the worker's spawn
    ledger (per slave) plus every goal's master trace (master rounds and
    skeptic panels), last 14 days, keyed by day like the console's own
    ledger. One ssh per minute at most."""
    import json as _json, time as _time
    now = _time.time()
    a = agent_cfg(CFG)
    if not a.get("enabled"):
        return None
    if _usage_cache["val"] is not None and now - _usage_cache["at"] < max_age:
        return _usage_cache["val"]
    ssh = (a.get("ssh") or "").strip()
    py = _USAGE_PY.replace("~/bytebunker-harness", (a.get("dir") or "~/bytebunker-harness"))
    cmd = (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh, "python3 -c " + shlex.quote(py)]
           if ssh else ["python3", "-c", py])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
        val = _json.loads(out[-1]) if out else None
    except Exception as e:   # noqa: BLE001
        val = {"error": str(e)[:120]}
    _usage_cache.update(at=now, val=val)
    return val


_stats_cache = {"at": 0, "val": None}
_fm_last = {}          # previous token counters of the worker's own model, for tok/s


def stats(CFG, max_age=8):
    """Live view of the agent plane for the Cluster screen: containers,
    masters, live slaves, last-24h spawn outcomes, worker load. One ssh per
    call, cached briefly so several browsers do not multiply it."""
    import json as _json, time as _time
    now = _time.time()
    st = status(CFG)
    if not st["enabled"]:                      # cheap, and never served from cache
        return {"ok": False, "enabled": False, "host": st["host"]}
    if _stats_cache["val"] is not None and now - _stats_cache["at"] < max_age:
        return _stats_cache["val"]
    a = agent_cfg(CFG)
    ssh = (a.get("ssh") or "").strip()
    py = _STATS_PY.replace("~/bytebunker-harness", (a.get("dir") or "~/bytebunker-harness"))
    py = py.replace("__FAST_METRICS__", _json.dumps(a.get("worker_model_metrics") or "http://127.0.0.1:8001/metrics"))
    if ssh:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh, "python3 -c " + shlex.quote(py)]
    else:
        cmd = ["python3", "-c", py]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        out = p.stdout.strip().splitlines()
        if not out:
            # nothing came back: an unreachable worker, not an idle one
            err = (p.stderr or "").strip().splitlines()
            raise RuntimeError(err[-1] if err else "no answer (exit %d)" % p.returncode)
        val = _json.loads(out[-1])
        val.update(ok=True, enabled=True, host=st["host"], isolated=st["isolated"])
        fm = val.get("fast_model") or {}
        fm["label"] = a.get("worker_model_label") or "worker model"
        if fm.get("up"):
            # tokens/s from the counter delta between two polls
            last = _fm_last
            dt = now - last.get("ts", 0)
            if last and 0 < dt < 600 and fm.get("gen_total", 0) >= last.get("gen", 0):
                fm["gen_tps"] = round((fm["gen_total"] - last["gen"]) / dt, 1)
                fm["prompt_tps"] = round((fm["prompt_total"] - last["prompt"]) / dt, 1)
            _fm_last.update(ts=now, gen=fm.get("gen_total", 0), prompt=fm.get("prompt_total", 0))
        val["fast_model"] = fm
    except Exception as e:   # noqa: BLE001
        val = {"ok": False, "enabled": True, "host": st["host"], "error": str(e)[:120]}
    _stats_cache.update(at=now, val=val)
    return val


def _remote_dir(CFG):
    a = agent_cfg(CFG)
    ssh = (a.get("ssh") or "").strip()
    return ssh, (a.get("dir") or "~/bytebunker-harness")


def _rq(path):
    """Quote a remote path for sh, keeping a leading ~ expandable: a quoted
    tilde is a literal directory named '~' (measured)."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def push_skill(CFG, name, text):
    """Mirror a skill written in the console to the agent worker's harness
    skills/ so the Sultan's roster and the console's Skills screen agree.
    Local mode: the harness dir is on this host, write it directly."""
    ssh, d = _remote_dir(CFG)
    if not agent_cfg(CFG).get("enabled"):
        return {"pushed": False, "reason": "agents disabled"}
    rel = "skills/%s/SKILL.md" % name
    try:
        if ssh:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh,
                   "mkdir -p %s && cat > %s" % (_rq(d + "/skills/" + name), _rq(d + "/" + rel))]
            r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=25)
            if r.returncode != 0:
                return {"pushed": False, "reason": (r.stderr or "ssh failed")[:160]}
            return {"pushed": True, "where": ssh + ":" + d + "/" + rel}
        path = os.path.join(os.path.expanduser(d), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return {"pushed": True, "where": path}
    except Exception as e:   # noqa: BLE001
        return {"pushed": False, "reason": str(e)[:160]}


def remove_skill_remote(CFG, name):
    ssh, d = _remote_dir(CFG)
    if not agent_cfg(CFG).get("enabled") or not _SLAVE_ID.match(name):
        return {"removed": False}
    try:
        target = d + "/skills/" + name
        if ssh:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh,
                                "rm -rf %s %s" % (_rq(target), _rq(target + ".md"))],
                               capture_output=True, text=True, timeout=20)
            return {"removed": r.returncode == 0}
        import shutil
        shutil.rmtree(os.path.expanduser(target), ignore_errors=True)
        return {"removed": True}
    except Exception as e:   # noqa: BLE001
        return {"removed": False, "reason": str(e)[:120]}


def kill_slave(CFG, slave_id):
    """Stop one running slave: its container is named after it. The master
    sees the slave end with an error and decides what to do next."""
    if not _SLAVE_ID.match(slave_id or ""):
        return {"ok": False, "error": "bad slave id"}
    a = agent_cfg(CFG)
    ssh = (a.get("ssh") or "").strip()
    inner = "podman kill %s >/dev/null 2>&1 && echo killed || echo not-running" % shlex.quote(slave_id)
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh, inner] if ssh else ["sh", "-c", inner]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": out == "killed", "id": slave_id, "result": out}


def run_streaming(cmd, cwd, on_line, stop_event, timeout_s=3600):
    """Run cmd, calling on_line(text) for each stdout line. Returns
    (exit_code, killed). Merges stderr into stdout so a crash is visible.
    A watchdog thread enforces the timeout and honours stop_event, which is
    only read: a run's cancel event can be passed as it is."""
    # stdin stays open for the life of the run: over ssh the remote sidecar
    # reads it, and closing it is how a stop reaches the remote process group
    proc = subprocess.Popen(
        cmd, cwd=cwd or None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, stdin=subprocess.PIPE,
    )
    killed = {"v": False}
    ended = threading.Event()

    def watchdog():
        deadline = time.time() + timeout_s
        while True:
            if ended.is_set():
                return
            if stop_event.wait(max(0.0, min(1.0, deadline - time.time()))):
                killed["v"] = "stopped"
                break
            if time.time() >= deadline:
                killed["v"] = "timeout"
                break
        try:
            try:
                proc.stdin.close()          # remote: sidecar kills the group
            except Exception:
                pass
            try:
                proc.wait(6)                # the remote sidecar TERMs the group on EOF within ~1 s
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    wd = threading.Thread(target=watchdog, daemon=True)
    wd.start()
    try:
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
    finally:
        # Output has ended: close stdin FIRST so a remote sidecar still
        # reading it gets EOF and cannot hold the ssh session open, then wait.
        try:
            proc.stdin.close()
        except Exception:
            pass
        code = proc.wait()
        ended.set()           # release the watchdog if the process ended on its own
    return code, killed["v"]
