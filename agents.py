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

import shlex
import subprocess
import threading


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
        "host": ssh or "localhost (hermes)",
        "dir": a.get("dir") or "",
        "isolated": bool(ssh),           # local == same host as the console: not isolated
        "configured": bool(a.get("dir")),
    }


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
_stats_cache = {"at": 0, "val": None}


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
    if ssh:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh, "python3 -c " + shlex.quote(py)]
    else:
        cmd = ["python3", "-c", py]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        val = _json.loads(out[-1]) if out else {}
        val.update(ok=True, enabled=True, host=st["host"], isolated=st["isolated"])
    except Exception as e:   # noqa: BLE001
        val = {"ok": False, "enabled": True, "host": st["host"], "error": str(e)[:120]}
    _stats_cache.update(at=now, val=val)
    return val


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
    A watchdog thread enforces the timeout and honours stop_event."""
    # stdin stays open for the life of the run: over ssh the remote sidecar
    # reads it, and closing it is how a stop reaches the remote process group
    proc = subprocess.Popen(
        cmd, cwd=cwd or None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, stdin=subprocess.PIPE,
    )
    killed = {"v": False}

    def watchdog():
        if stop_event.wait(timeout_s):
            killed["v"] = "stopped"
        else:
            killed["v"] = "timeout"
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
        stop_event.set()      # release the watchdog if the process ended on its own
    return code, killed["v"]
