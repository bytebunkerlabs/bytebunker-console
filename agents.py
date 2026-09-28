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
        remote = "cd %s && %s %s --goal %s" % (
            qdir, launcher, shlex.quote(script), shlex.quote(goal))
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", ssh, remote]

    # local: no shell, goal is a discrete argv element
    return shlex.split(launcher) + [script, "--goal", goal]


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
