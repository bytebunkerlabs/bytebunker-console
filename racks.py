"""Racks: machines that serve models with dgx-serve, driven over ssh.

config.json keeps a list:

    "racks": [{"name": "spark-1", "ssh": "you@spark-1", "paired": true,
               "app": "laptop-3fa2", "gateway": "spark-1", "monitor": "spark-1"}]

Pairing (Add a rack, bb pair) asks the machine once, with your own ssh, to
run `rack pair --json --key <this app's key>`: dgx-serve puts the key in its
authorized_keys restricted to `rack remote --app <name>` (rack's own commands
and nothing else) and answers with what the app needs: the engine's port and
key, the monitor's port and token, the machine's addresses. The app then
finds which address answers from here and adds the engine as a gateway and
the monitor, if you want them. From then on it reaches the rack with its own
key only. A rack from before pairing (`dir` set, `paired` not) is reached as
before: your own ssh, ./rack in its checkout.
"""
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

# user@host or a host / ssh alias: never a leading dash (ssh would read an option)
TARGET = re.compile(r"^(?!-)[A-Za-z0-9._%+-]{1,64}(@(?!-)[A-Za-z0-9._:%\[\]-]{1,253})?$")
# what `rack remote` accepts in a command, word by word
WORD = re.compile(r"^[A-Za-z0-9._:/=@+,-]+$")
NAME = re.compile(r"[^A-Za-z0-9._-]+")


class PairError(Exception):
    """Pairing failed; the message says why and what to do."""


def entries(cfg):
    out = []
    for r in cfg.get("racks") or []:
        if not isinstance(r, dict) or not TARGET.match(str(r.get("ssh") or "")):
            continue
        out.append({"name": str(r.get("name") or r["ssh"])[:60], "ssh": r["ssh"], "dir": str(r.get("dir") or ""),
                    "paired": bool(r.get("paired")), "app": str(r.get("app") or ""),
                    "platform": str(r.get("platform") or ""), "gateway": str(r.get("gateway") or ""),
                    "monitor": str(r.get("monitor") or ""), "paired_at": r.get("paired_at") or 0})
    return out


def migrate(cfg):
    """0.4.2: the one `rack` {ssh, dir} becomes the first of `racks`."""
    old = cfg.pop("rack", None)
    racks = [r for r in (cfg.get("racks") or []) if isinstance(r, dict)]
    if isinstance(old, dict) and str(old.get("ssh") or "").strip() and not any(r.get("ssh") == old["ssh"] for r in racks):
        ssh = str(old["ssh"]).strip()
        racks.insert(0, {"name": ssh.split("@")[-1], "ssh": ssh, "dir": old.get("dir") or "~/dgx/dgx-spark-serve"})
    cfg["racks"] = racks


def host_of(target):
    return target.split("@", 1)[-1]


def _fwd(p):
    # OpenSSH on Windows takes forward slashes, and a backslash in an -o value can read as an escape
    return p.replace("\\", "/") if sys.platform == "win32" else p


def _q(p):
    return '"%s"' % _fwd(p) if (" " in p or "\\" in p) else p


class Keys:
    """This app's own ssh identity and the host keys it has pinned, in its data folder."""

    def __init__(self, home):
        self.dir = os.path.join(home, "ssh")
        self.key = os.path.join(self.dir, "bytebunker_ed25519")
        self.known_hosts = os.path.join(self.dir, "known_hosts")
        self._lock = threading.Lock()

    def public(self, comment="bytebunker"):
        """The public key, made the first time it is asked for."""
        with self._lock:
            if not os.path.exists(self.key):
                os.makedirs(self.dir, exist_ok=True)
                try:
                    os.chmod(self.dir, 0o700)
                except OSError:
                    pass
                try:
                    r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", self.key],
                                       capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
                except FileNotFoundError:
                    raise PairError("ssh-keygen is not installed on this computer: it comes with OpenSSH, "
                                    "which pairing needs (ssh and ssh-keygen)")
                if r.returncode != 0:
                    raise PairError("ssh-keygen could not make this app's key: " + (r.stderr or r.stdout).strip()[:200])
            with open(self.key + ".pub", encoding="utf-8") as f:
                return f.read().strip()

    def app_name(self):
        """How the rack's authorized_keys names this app: the computer, and
        a few characters of the key, so two apps on one computer differ."""
        body = (self.public().split() + ["", ""])[1]
        host = NAME.sub("-", socket.gethostname().split(".")[0]).strip("-.")[:30] or "app"
        return "%s-%s" % (host, hashlib.sha256(body.encode()).hexdigest()[:4])

    def fingerprint(self):
        r = subprocess.run(["ssh-keygen", "-l", "-f", self.key + ".pub"], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
        return (r.stdout.split() + ["", ""])[1] if r.returncode == 0 else ""

    def _known(self):
        user = os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")
        return "UserKnownHostsFile=%s %s" % (_q(self.known_hosts), _q(user))

    def argv(self, target, own_key=True, first=False, extra=()):
        """ssh to target. own_key: as this app, with nothing else offered
        (no agent, no other keys); first: a host never seen is pinned now."""
        a = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
             "-o", "ServerAliveCountMax=4", "-o", self._known(),
             "-o", "StrictHostKeyChecking=" + ("accept-new" if first else "yes")]
        if own_key:
            a += ["-i", _fwd(self.key), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none"]
        return a + list(extra) + [target]


def rack_argv(keys, rack, args):
    """The command line that runs `rack <args>` on a rack."""
    import shlex
    for w in args:
        if not WORD.match(str(w)):
            raise ValueError("not something rack takes: %r" % w)
    if rack.get("paired"):
        return keys.argv(rack["ssh"]) + ["rack " + " ".join(args)]
    inner = "export PATH=$HOME/.local/bin:$PATH; cd %s && ./rack %s" % (
        _rq(rack.get("dir") or "~/dgx/dgx-spark-serve"), " ".join(shlex.quote(a) for a in args))
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", rack["ssh"], inner]


def _rq(path):
    """A remote path quoted for sh, a leading ~ still expanded (as agents.py does)."""
    import shlex
    p = str(path)
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


PAIR_CMD = ("export PATH=\"$HOME/.local/bin:$PATH\"; "
            "command -v rack >/dev/null 2>&1 || { echo BB_NO_RACK; exit 3; }; "
            "rack pair --json --key '%s' --name %s")


def _last(text, n=3):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:])[:400]


def ssh_hostname(target):
    """The address ssh would connect to for target (an alias resolves through ~/.ssh/config)."""
    try:
        r = subprocess.run(["ssh", "-G", target], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        for line in r.stdout.splitlines():
            if line.startswith("hostname "):
                return line.split(None, 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return host_of(target)


def ask(keys, target, emit, timeout=90):
    """Run rack pair on target. ("paired", info with secrets) or
    ("needs_key", the command to run there). PairError says why not."""
    if not TARGET.match(target or ""):
        raise PairError("that is not an ssh destination: user@host, or a host from ~/.ssh/config")
    pub = keys.public("bytebunker@" + socket.gethostname().split(".")[0])
    name = keys.app_name()
    emit("this app's key: %s (%s)" % (keys.fingerprint() or "ed25519", name))
    emit("asking %s with your own ssh: rack pair" % target)
    try:
        r = subprocess.run(keys.argv(target, own_key=False, first=True) + [PAIR_CMD % (pub, name)],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise PairError("ssh is not installed on this computer (OpenSSH's client)")
    except subprocess.TimeoutExpired:
        raise PairError("%s did not answer in %d s" % (target, timeout))
    out, err = r.stdout or "", r.stderr or ""
    if r.returncode == 0:
        return "paired", parse(out)
    if "BB_NO_RACK" in out:
        raise PairError("dgx-serve is not on %s: there is no rack command there. Install it on that machine, "
                        "then pair again." % target)
    if "Host key verification failed" in err or "IDENTIFICATION HAS CHANGED" in err:
        raise PairError("%s's host key is not the one recorded before: the machine was reinstalled, or something is "
                        "in between. If you know which, remove its line from ~/.ssh/known_hosts (and %s), then pair "
                        "again." % (target, keys.known_hosts))
    if r.returncode == 255 and ("Permission denied" in err or "Too many authentication failures" in err):
        emit("your ssh to %s wants a password or a key this app cannot use; trying this app's own key" % target)
        try:
            r2 = subprocess.run(keys.argv(target, first=True) + ["rack pair --json"], capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            r2 = None
        if r2 is not None and r2.returncode == 0:
            return "paired", parse(r2.stdout)
        return "needs_key", {"command": "rack pair --key '%s' --name %s" % (pub, name), "target": target}
    if r.returncode == 255:
        raise PairError("could not reach %s: %s" % (target, _last(err)))
    said = _last(err) or _last(out)
    if "unknown command: pair" in said:
        raise PairError("the dgx-serve on %s is older than this app (it has no rack pair): update it there "
                        "(git pull in its checkout), then pair again" % target)
    raise PairError("rack pair on %s failed: %s" % (target, said or "exit %d" % r.returncode))


def parse(text):
    try:
        d = json.loads((text or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise PairError("rack pair did not answer with JSON: " + _last(text))
    if not isinstance(d, dict) or not isinstance(d.get("engine"), dict) or not isinstance(d.get("monitor"), dict):
        raise PairError("rack pair answered in a shape this app does not know (schema %r)" % (d.get("schema") if isinstance(d, dict) else None))
    return d


def _urlhost(h):
    return "[%s]" % h if ":" in h and not h.startswith("[") else h


def _get(url, token=None, timeout=3.0):
    req = urllib.request.Request(url, headers=dict({"Accept": "application/json", "User-Agent": "bytebunker"},
                                                   **({"Authorization": "Bearer " + token} if token else {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(4 << 20).decode("utf-8", "replace"))


def find(target, info, timeout=3.0):
    """The engine's and the monitor's URLs as this computer reaches them:
    the first address, in order, that answers (the one ssh used, the
    tailnet's, the LAN's). (engine_url or None, monitor_url or None, tried)."""
    hosts = []
    for h in [ssh_hostname(target)] + [a.get("ip") for a in info.get("addresses") or []] \
            + [a.get("name") for a in info.get("addresses") or []] + [info.get("hostname")]:
        if h and isinstance(h, str) and h not in hosts:     # loopback too: the app may run on the rack itself
            hosts.append(h)
    eng, mon = info["engine"], info["monitor"]

    def engine(h):
        url = "http://%s:%d" % (_urlhost(h), int(eng.get("port") or 8888))
        try:
            d = _get(url + "/v1/models", eng.get("key") or None, timeout)
            return url if isinstance(d.get("data"), list) else None
        except Exception:   # noqa: BLE001  (refused, timed out, not an engine)
            return None

    def monitor(h):
        url = "http://%s:%d" % (_urlhost(h), int(mon.get("port") or 9177))
        try:
            hello = _get(url + "/v1/hello", None, timeout)
            if not isinstance(hello, dict):
                return None
            _get(url + "/v1/cluster", mon.get("token") or None, timeout)      # the token works too
            return url
        except Exception:   # noqa: BLE001
            return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        engines = list(ex.map(engine, hosts))
        monitors = list(ex.map(monitor, hosts)) if mon.get("token") else [None] * len(hosts)
    return next((u for u in engines if u), None), next((u for u in monitors if u), None), hosts


def summary(target, info, engine_url, monitor_url):
    """What pairing found, without the secrets: for the screen and bb."""
    s = (info["engine"].get("serving") or {})
    return {"target": target, "name": info.get("name") or host_of(target), "platform": info.get("platform") or "",
            "rack_version": info.get("rack_version") or "", "hostname": info.get("hostname") or "",
            "addresses": info.get("addresses") or [], "apps": info.get("apps") or [],
            "engine": {"url": engine_url, "port": info["engine"].get("port"), "has_key": bool(info["engine"].get("key")),
                       "answering": bool(info["engine"].get("answering")),
                       "serving": {k: s.get(k) for k in ("recipe", "model", "served_name", "engine") if s.get(k)} or None},
            "monitor": {"url": monitor_url, "port": info["monitor"].get("port"), "running": bool(info["monitor"].get("running")),
                        "has_token": bool(info["monitor"].get("token"))}}


class Pending:
    """Pairings found and not yet saved, kept here (they hold the engine key
    and the monitor token) and gone after 15 minutes."""
    TTL = 900

    def __init__(self):
        self._d, self._lock = {}, threading.Lock()

    def put(self, item):
        pid = "pair-" + uuid.uuid4().hex[:12]
        with self._lock:
            now = time.time()
            for k in [k for k, v in self._d.items() if now - v["at"] > self.TTL]:
                self._d.pop(k, None)
            self._d[pid] = dict(item, at=now)
        return pid

    def take(self, pid):
        with self._lock:
            item = self._d.pop(pid, None)
        if not item or time.time() - item["at"] > self.TTL:
            return None
        return item


def unique(name, taken):
    base, n = name or "rack", 2
    while name in taken:
        name = "%s-%d" % (base, n)
        n += 1
    return name


def apply(cfg, item, gateway=True, monitor=True, gw_normalize=None, now_ms=None):
    """Save a pairing into cfg: the rack, and the engine as a gateway and the
    monitor when found and wanted. Pairing a rack again updates what it
    added before. Returns the rack entry."""
    info, target = item["info"], item["target"]
    racks = [r for r in (cfg.get("racks") or []) if isinstance(r, dict)]
    old = next((r for r in racks if r.get("ssh") == target), None)
    name = (old or {}).get("name") or unique(NAME.sub("-", info.get("name") or host_of(target))[:60],
                                              {r.get("name") for r in racks})
    rack = {"name": name, "ssh": target, "paired": True, "app": item.get("app") or "",
            "platform": info.get("platform") or "", "paired_at": now_ms or int(time.time() * 1000)}
    if item.get("engine_url") and gateway:
        gws = [g for g in (cfg.get("gateways") or []) if isinstance(g, dict)]
        url = item["engine_url"] + "/v1"
        g = next((g for g in gws if g.get("name") == (old or {}).get("gateway")), None) \
            or next((g for g in gws if g.get("url") == url), None)
        if g is None:
            g = {"name": unique(name, {x.get("name") for x in gws}), "enabled": True}
            gws.append(g)
        g.update(url=url, key=info["engine"].get("key") or "", kind="dgx-serve")
        cfg["gateways"] = gws
        rack["gateway"] = g["name"]
        if gw_normalize:
            gw_normalize(cfg)
    elif old and old.get("gateway"):
        rack["gateway"] = old["gateway"]
    if item.get("monitor_url") and monitor:
        mons = [m for m in (cfg.get("monitors") or []) if isinstance(m, dict)]
        m = next((m for m in mons if m.get("name") == (old or {}).get("monitor")), None) \
            or next((m for m in mons if m.get("url") == item["monitor_url"]), None)
        if m is None:
            m = {"name": unique(name, {x.get("name") for x in mons}), "enabled": True}
            mons.append(m)
        m.update(url=item["monitor_url"], token=info["monitor"].get("token") or "")
        cfg["monitors"] = mons
        rack["monitor"] = m["name"]
    elif old and old.get("monitor"):
        rack["monitor"] = old["monitor"]
    if old:
        racks[racks.index(old)] = rack
    else:
        racks.append(rack)
    cfg["racks"] = racks
    return rack
