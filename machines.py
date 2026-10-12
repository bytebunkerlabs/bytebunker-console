"""Machines: the computers this app can reach with your ssh, what they are,
and which dgx-serve recipes fit them.

Find machines (a button, never in the background) lists the hosts in your
~/.ssh/config and the machines of your tailnet, then asks each one, with your
own ssh and never a password, one read-only shell command (PROBE): OS, CPU,
memory, GPUs, disk, and whether docker, podman and dgx-serve are there.
Nothing is installed or changed by looking.

Suggestions come from recipes_catalog.json, a copy of dgx-serve's curated
recipes made at build time (scripts/recipes_catalog.py), with dgx-serve's own
fit arithmetic (py/rackfit.py), so they work before dgx-serve is anywhere.
"""
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(ROOT, "recipes_catalog.json")

PROBE = r'''
echo "os=$(uname -s 2>/dev/null)"
echo "arch=$(uname -m 2>/dev/null)"
echo "hostname=$(hostname 2>/dev/null)"
if [ "$(uname -s 2>/dev/null)" = Darwin ]; then
  echo "os_name=macOS"
  echo "os_version=$(sw_vers -productVersion 2>/dev/null)"
  echo "mem_b=$(sysctl -n hw.memsize 2>/dev/null)"
  echo "cpu=$(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
  echo "cores=$(sysctl -n hw.ncpu 2>/dev/null)"
  echo "wired_mb=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null)"
else
  if [ -r /etc/os-release ]; then
    echo "os_name=$(. /etc/os-release && echo "$NAME")"
    echo "os_version=$(. /etc/os-release && echo "$VERSION_ID")"
  fi
  [ -r /proc/meminfo ] && echo "mem_kb=$(awk '/^MemTotal/{print $2}' /proc/meminfo)"
  [ -r /proc/cpuinfo ] && echo "cpu=$(awk -F': *' '/^model name|^Model|^Hardware/{print $2; exit}' /proc/cpuinfo)"
  echo "cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null)"
  grep -qi microsoft /proc/version 2>/dev/null && echo "wsl=1"
  [ -e /etc/dgx-release ] && echo "dgx=1"
  case "$(cat /sys/class/dmi/id/product_name /sys/class/dmi/id/product_family 2>/dev/null)" in *DGX*|*Spark*) echo "dgx=1" ;; esac
fi
NS=nvidia-smi
[ -x /usr/lib/wsl/lib/nvidia-smi ] && NS=/usr/lib/wsl/lib/nvidia-smi
if command -v "$NS" >/dev/null 2>&1; then
  "$NS" --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>/dev/null | sed 's/^/gpu=/'
fi
echo "disk_kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print $4}')"
for t in docker podman bwrap git python3 curl; do command -v "$t" >/dev/null 2>&1 && echo "has_$t=1"; done
R=$(PATH="$HOME/.local/bin:$PATH" command -v rack 2>/dev/null)
[ -n "$R" ] && echo "rack=$("$R" version 2>/dev/null | head -1)"
echo "BB_PROBE_OK"
'''


GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "ssh.dev.azure.com", "codeberg.org", "ssh.github.com"}


def _ssh_config(path=None):
    """Host aliases from ~/.ssh/config (no patterns), with their HostName."""
    path = path or os.environ.get("BYTEBUNKER_SSH_CONFIG") or os.path.join(os.path.expanduser("~"), ".ssh", "config")
    out, current = [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    names = {}
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        k, _, v = s.partition(" ")
        if "=" in k:
            k, _, v = s.partition("=")
        k, v = k.strip().lower(), v.strip()
        if k == "host":
            current = [h for h in v.split() if not any(c in h for c in "*?!")]
            for h in current:
                if h not in names:
                    names[h] = None
                    out.append(h)
        elif k == "match":
            current = []
        elif k == "hostname" and current:
            for h in current:
                names[h] = names[h] or v
    rows, by_name = [], {}
    for h in out:
        hn = names[h] or h
        if hn.lower() in GIT_HOSTS or h.lower() in GIT_HOSTS:
            continue                          # a git server, not a computer
        if hn in by_name:                     # "Host burhan spark-1": one machine, two names
            by_name[hn]["aliases"].append(h)
            continue
        by_name[hn] = {"target": h, "hostname": hn, "source": "ssh config", "aliases": [h]}
        rows.append(by_name[hn])
    return rows


def _tailnet():
    """This tailnet's online machines: (peers [{name, ip}], this machine's own IPs)."""
    if os.environ.get("BYTEBUNKER_TAILNET") == "off":
        return [], set()
    for exe in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/bin/tailscale",
                r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            d = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=6).stdout)
        except Exception:   # noqa: BLE001  (not installed here, or not running)
            continue
        suffix = str(d.get("MagicDNSSuffix") or "").strip(".")
        own = set((d.get("Self") or {}).get("TailscaleIPs") or [])
        peers = []
        for p in (d.get("Peer") or {}).values():
            dns = str(p.get("DNSName") or "").rstrip(".")
            if p.get("Online") is False or any("mullvad" in str(t) for t in (p.get("Tags") or [])):
                continue
            if suffix and not dns.endswith("." + suffix):
                continue                     # exit nodes and shared-in services are not ours
            ip = next((x for x in p.get("TailscaleIPs") or [] if ":" not in x), "")
            if ip:
                peers.append({"name": dns.split(".")[0] or p.get("HostName") or ip, "ip": ip, "dns": dns,
                              "os": str(p.get("OS") or "")})
        return peers, own
    return [], set()


def candidates(ssh_config=None):
    """Hosts to ask: your ssh config's, then tailnet machines it does not already name."""
    hosts = _ssh_config(ssh_config)
    peers, own = _tailnet()
    seen = {h["hostname"] for h in hosts} | {h["target"] for h in hosts}
    for p in peers:
        if p["ip"] in own or p["ip"] in seen or p["dns"] in seen or p["name"] in seen:
            continue
        hosts.append({"target": p["name"] if p["dns"] else p["ip"], "hostname": p["ip"], "source": "tailnet",
                      "tailnet_os": p["os"], "aliases": [p["name"]]})
        seen |= {p["ip"], p["name"], p["dns"]}
    return [h for h in hosts if h["hostname"] not in own]


def parse(text):
    info = {"gpus": []}
    for ln in (text or "").splitlines():
        k, eq, v = ln.partition("=")
        if not eq:
            continue
        k, v = k.strip(), v.strip()
        if k == "gpu":
            parts = [x.strip() for x in v.split(",")]
            mem = parts[1] if len(parts) > 1 else ""
            info["gpus"].append({"name": parts[0], "mem_mb": int(mem) if mem.isdigit() else None,
                                 "driver": parts[2] if len(parts) > 2 else ""})
        elif re.match(r"^[a-z_]+$", k):
            info[k] = v
    return info


def describe(info):
    """platform (dgx, linux, windows, mac, or unsupported), budget for models
    (MB), a one-line description, and why not when it cannot serve."""
    mem_mb = 0
    if info.get("mem_kb", "").isdigit():
        mem_mb = int(info["mem_kb"]) // 1024
    elif info.get("mem_b", "").isdigit():
        mem_mb = int(info["mem_b"]) // (1024 * 1024)
    gpus = info.get("gpus") or []
    gpu_mb = sum(g["mem_mb"] or 0 for g in gpus)
    osname = info.get("os", "")
    why, platform, budget = "", "unsupported", 0
    if osname == "Darwin":
        if info.get("arch") == "arm64":
            platform = "mac"
            wired = int(info["wired_mb"]) if info.get("wired_mb", "").isdigit() else 0
            budget = wired or (mem_mb * 2 // 3 if mem_mb <= 36864 else mem_mb * 3 // 4)
        else:
            why = "an Intel Mac: dgx-serve serves on Apple Silicon"
    elif osname == "Linux":
        if info.get("dgx") == "1" or any("GB10" in g["name"] for g in gpus):
            platform, budget = "dgx", mem_mb
        elif gpus:
            platform, budget = ("windows" if info.get("wsl") == "1" else "linux"), gpu_mb
        else:
            why = "no NVIDIA GPU found (nvidia-smi)"
    elif osname:
        why = "%s: dgx-serve runs on Linux, macOS and Windows through WSL2" % osname
    else:
        why = "did not answer like a Linux or macOS shell (a Windows PC: reach its WSL2 instead)"
    kind = {"dgx": "DGX Spark", "mac": "Mac", "linux": "Linux", "windows": "Windows PC (WSL2)"}.get(platform)
    bits = [b for b in (
        ("%s %s" % (info.get("os_name", ""), info.get("os_version", ""))).strip(),
        info.get("cpu") or "",
        "%d GiB memory" % round(mem_mb / 1024) if mem_mb else "",
        ", ".join("%s%s" % (g["name"], " %d GiB" % round(g["mem_mb"] / 1024) if g["mem_mb"] else "") for g in gpus),
        "%d GB free" % (int(info["disk_kb"]) // 1000000) if info.get("disk_kb", "").isdigit() else "",
    ) if b]
    desc = (kind + ": " if kind else "") + " · ".join(bits)
    return {"platform": platform, "budget_mb": budget, "description": desc, "why_not": why, "mem_mb": mem_mb,
            "gpus": gpus, "disk_gb": int(info["disk_kb"]) // 1000000 if info.get("disk_kb", "").isdigit() else None,
            "has": sorted(k[4:] for k, v in info.items() if k.startswith("has_") and v == "1"),
            "rack": info.get("rack") or "", "hostname": info.get("hostname") or ""}


def load_catalog(path=CATALOG):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"recipes": [], "fit": {}}


def suggest(catalog, platform, budget_mb):
    """Every recipe with a variant for this platform, with dgx-serve's verdict
    for one machine of this size; those that fit first, the largest first:
    the first is the suggestion."""
    fit = catalog.get("fit") or {}
    reserve = (fit.get("reserve_gb") or {}).get(platform, 1.0)
    out = []
    for r in catalog.get("recipes") or []:
        v = (r.get("variants") or {}).get(platform)
        if not v:
            continue
        engine = v.get("engine") or "vllm"
        nodes = int(v.get("nodes") or 1)
        usable = max(0.0, budget_mb / 1024.0 - reserve)
        w = v.get("weights_gb")
        if nodes > 1:
            fits, need, note = False, None, "needs %d machines in one rack" % nodes
        elif w is None:
            fits, need, note = None, None, "size unknown"
        else:
            need = w * (fit.get("weight_factor") or {}).get(engine, 1.03) + \
                (fit.get("overhead_gb") or {}).get(engine, 1.5) + w * (fit.get("kv_guess") or 0.10)
            fits, note = need <= usable, ""
        out.append({"name": r["name"], "model": r.get("model") or "", "engine": engine, "weights_gb": w,
                    "needs_gb": round(need, 1) if need else None, "usable_gb": round(usable, 1), "fits": fits,
                    "note": note, "roles": r.get("roles") or []})
    rank = {True: 0, None: 1, False: 2}
    out.sort(key=lambda x: (rank[x["fits"]], -(x["weights_gb"] or 0) if x["fits"] else (x["weights_gb"] or 1e9)))
    return out


def probe(keys, target, timeout=20):
    """Ask target, with your own ssh, what it is. {ok, ...describe} or {ok: False, error, kind}."""
    argv = keys.argv(target, own_key=False, first=True, extra=("-o", "ConnectTimeout=6")) + ["sh -c " + shlex.quote(PROBE)]
    return _run_probe(argv, timeout)


def probe_here(timeout=20):
    if sys.platform == "win32":
        return _run_probe(["wsl.exe", "-e", "sh", "-c", PROBE], timeout)
    return _run_probe(["/bin/sh", "-c", PROBE], timeout)


def _run_probe(argv, timeout):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "timeout", "error": "did not answer in %d s" % timeout}
    except OSError as e:
        return {"ok": False, "kind": "error", "error": str(e)}
    err = (r.stderr or "").strip()
    if "BB_PROBE_OK" not in (r.stdout or ""):
        if "Permission denied" in err or "Too many authentication failures" in err:
            return {"ok": False, "kind": "password", "error": "your ssh wants a password or a key it cannot use here"}
        if "Host key verification failed" in err or "IDENTIFICATION HAS CHANGED" in err:
            return {"ok": False, "kind": "hostkey", "error": "its host key changed since you last connected"}
        if "is not recognized" in (r.stdout or "") + err:
            return {"ok": False, "kind": "windows", "error": "a Windows shell: reach its WSL2 Ubuntu with ssh instead"}
        last = [l for l in err.splitlines() if l.strip()]
        return {"ok": False, "kind": "unreachable", "error": (last[-1] if last else "exit %d" % r.returncode)[:200]}
    return dict(describe(parse(r.stdout)), ok=True)


def find(keys, catalog=None, ssh_config=None, workers=12):
    """Every candidate, asked in parallel, with suggestions for those that answer."""
    catalog = catalog if catalog is not None else load_catalog()
    hosts = candidates(ssh_config)

    def one(h):
        res = dict(h, **probe(keys, h["target"]))
        if res.get("ok") and res["platform"] != "unsupported":
            res["recipes"] = suggest(catalog, res["platform"], res["budget_mb"])
        return res
    with ThreadPoolExecutor(max_workers=workers) as ex:
        found = list(ex.map(one, hosts))
    found.sort(key=lambda m: (not m.get("ok"), m["platform"] == "unsupported" if m.get("ok") else True, m["target"]))
    return found


def here(catalog=None):
    catalog = catalog if catalog is not None else load_catalog()
    res = dict({"target": "local", "hostname": socket.gethostname(), "source": "this computer"}, **probe_here())
    if res.get("ok") and res["platform"] != "unsupported":
        res["recipes"] = suggest(catalog, res["platform"], res["budget_mb"])
    return res
