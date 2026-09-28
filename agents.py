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
    envs = {}
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
        env_prefix = "".join("%s=%s " % (k, shlex.quote(v)) for k, v in envs.items())
        remote = "cd %s && %s%s %s --goal %s" % (
            qdir, env_prefix, launcher, shlex.quote(script), shlex.quote(goal))
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
    if ssh:
        d = directory
        if d == "~":
            qd = "~"
        elif d.startswith("~/"):
            qd = "~/" + shlex.quote(d[2:])
        else:
            qd = shlex.quote(d)
        remote = "tail -n %d %s/%s 2>/dev/null" % (int(limit), qd, rel)
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
    slaves = []
    for line in out.splitlines():
        try:
            r = _json.loads(line)
        except ValueError:
            continue
        fr = r.get("final_result") or {}
        ans = fr.get("answer") if isinstance(fr, dict) else ""
        slaves.append({
            "role": r.get("role"), "depth": r.get("depth"),
            "brief": (r.get("brief") or "")[:200], "success": r.get("success"),
            "tokens": r.get("tokens"), "network": r.get("network_granted"),
            "answer": (ans or "")[:400], "error": (r.get("error") or "")[:200],
            "ts": r.get("timestamp"),
        })
    return {"ok": True, "slaves": slaves[::-1]}


def run_streaming(cmd, cwd, on_line, stop_event, timeout_s=3600):
    """Run cmd, calling on_line(text) for each stdout line. Returns
    (exit_code, killed). Merges stderr into stdout so a crash is visible.
    A watchdog thread enforces the timeout and honours stop_event."""
    proc = subprocess.Popen(
        cmd, cwd=cwd or None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, stdin=subprocess.DEVNULL,
    )
    killed = {"v": False}

    def watchdog():
        if stop_event.wait(timeout_s):
            killed["v"] = "stopped"
        else:
            killed["v"] = "timeout"
        try:
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
        code = proc.wait()
        stop_event.set()      # release the watchdog if the process ended on its own
    return code, killed["v"]
