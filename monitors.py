"""Rack monitors: where the Cluster screen's numbers come from.

A monitor is `rack monitor up` on a rack's head node (monitor/rackmon.py in
github.com/bytebunkerlabs/dgx-spark-serve): one URL and one token that
answer for every node behind it. Any Linux box can run the same file bare
(`python3 rackmon.py serve`). config.json keeps a list:

    "monitors": [{"name": "rack", "url": "http://192.0.2.10:9177",
                  "token": "...", "enabled": true}]

The console fetches each monitor's /v1/cluster server-side, so the token
never reaches the browser, and merges the nodes into one Cluster view.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import gateways as gwmod

DEFAULT_PORT = 9177
SCHEMA = 1


def parse_url(raw):
    """'host', 'host:port', 'http://host:port/anything' or the paste-ready
    'http://rack:TOKEN@host:port' that `rack monitor token` prints ->
    (url, token). The port defaults to 9177; a path is dropped."""
    s = str(raw or "").strip()
    if not s:
        return "", ""
    if "://" not in s:
        s = "http://" + s
    try:
        u = urllib.parse.urlsplit(s)
        port = u.port
    except ValueError:
        return "", ""
    if u.scheme not in ("http", "https") or not u.hostname:
        return "", ""
    token = urllib.parse.unquote(u.password) if u.password else ""
    host = "[%s]" % u.hostname if ":" in u.hostname else u.hostname
    if port is None:
        port = DEFAULT_PORT if u.scheme == "http" else 443
    tail = "" if (u.scheme == "https" and port == 443) else ":%d" % port
    return "%s://%s%s" % (u.scheme, host, tail), token


def normalize(cfg):
    out = []
    for m in cfg.get("monitors") or []:
        if not isinstance(m, dict):
            continue
        url, tok = parse_url(m.get("url"))
        if not url:
            continue
        out.append({"name": str(m.get("name") or url.split("//")[-1])[:60], "url": url,
                    "token": str(m.get("token") or tok or "").strip(),
                    "enabled": m.get("enabled", True) is not False})
    return out


def _get(url, token=None, timeout=4.0):
    headers = {"Accept": "application/json", "User-Agent": "bytebunker-console"}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return json.loads(r.read(32 << 20).decode("utf-8", "replace"))


def explain(e):
    """An error a person can act on."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return "unauthorized: wrong or missing token (on the head: rack monitor token)"
        return "the monitor answered HTTP %d" % e.code
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if isinstance(r, (socket.timeout, TimeoutError)) or "timed out" in str(r):
            return "timed out (is the port open from here? tailnet works; the LAN may be firewalled)"
        if isinstance(r, ConnectionRefusedError) or "refused" in str(r):
            return "connection refused: nothing listening (on the head: rack monitor status)"
        return str(r)[:160]
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(e, ValueError):
        return str(e)[:160]
    return (type(e).__name__ + ": " + str(e))[:160]


def hello(url, timeout=2.5):
    d = _get(url.rstrip("/") + "/v1/hello", timeout=timeout)
    if not isinstance(d, dict) or d.get("service") != "rack-monitor":
        raise ValueError("that address answers, but it is not a rack monitor")
    return d


def check(url, token, timeout=5.0):
    """hello, then an authorized /v1/cluster: what Add runs before saving."""
    url = parse_url(url)[0] or url
    try:
        h = hello(url, timeout=min(timeout, 3.0))
    except urllib.error.HTTPError as e:
        # something answers HTTP there, but not /v1/hello: a gateway, a web app
        return {"ok": False, "stage": "reach",
                "error": "that address answers HTTP %d, but it is not a rack monitor (the monitor listens on :%d)" % (e.code, DEFAULT_PORT)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "reach", "error": explain(e)}
    try:
        d = _get(url.rstrip("/") + "/v1/cluster?history=0", token, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "auth", "hello": h, "error": explain(e)}
    if d.get("schema") != SCHEMA:
        return {"ok": False, "stage": "schema", "hello": h,
                "error": "monitor speaks schema %r; this console speaks %d" % (d.get("schema"), SCHEMA)}
    nodes = d.get("nodes") or []
    return {"ok": True, "hello": h, "cluster": d.get("cluster"), "head": d.get("head"),
            "nodes": [{"name": n.get("name"), "ok": n.get("ok", True)} for n in nodes]}


class Monitors:
    """Fetches and merges every enabled monitor; caches briefly so the
    sidebar, the Cluster screen and the playground share one fetch."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cache = {}                     # history -> (t, result)
        self.pool = ThreadPoolExecutor(max_workers=8)

    def list(self):
        return normalize(self.cfg)

    def invalidate(self):
        with self.lock:
            self.cache.clear()

    def _one(self, m, history):
        t0 = time.time()
        st = {"name": m["name"], "url": m["url"], "enabled": m["enabled"], "has_token": bool(m["token"])}
        try:
            d = _get("%s/v1/cluster?history=%d" % (m["url"], history), m["token"], timeout=5.0)
            if not isinstance(d, dict) or d.get("service") != "rack-monitor":
                raise ValueError("not a rack monitor")
            if d.get("schema") != SCHEMA:
                raise ValueError("monitor speaks schema %r; this console speaks %d" % (d.get("schema"), SCHEMA))
            nodes = [n for n in d.get("nodes") or [] if isinstance(n, dict)]
            for n in nodes:
                n["monitor"] = m["name"]
            st.update(ok=True, ms=int((time.time() - t0) * 1000), cluster=d.get("cluster"),
                      head=d.get("head"), version=d.get("version"), nodes=len(nodes),
                      down=sum(1 for n in nodes if n.get("ok") is False))
            return st, nodes
        except Exception as e:  # noqa: BLE001
            st.update(ok=False, ms=int((time.time() - t0) * 1000), error=explain(e))
            return st, []

    def cluster(self, history=0, max_age=2.0):
        history = max(0, min(450, int(history or 0)))
        with self.lock:
            hit = self.cache.get(history)
            if hit and time.time() - hit[0] < max_age:
                return hit[1]
        mons = self.list()
        live = [m for m in mons if m["enabled"]]
        results = list(self.pool.map(lambda m: self._one(m, history), live))
        nodes, seen, dupes = [], set(), 0
        for _, ns in results:
            for n in ns:
                # the same machine reachable through two monitors shows once
                key = ((n.get("system") or {}).get("hostname") or n.get("name"), n.get("name"))
                if key in seen:
                    dupes += 1
                    continue
                seen.add(key)
                nodes.append(n)
        out = {"configured": len(mons), "enabled": len(live), "monitors": [r[0] for r in results],
               "nodes": nodes, "duplicates": dupes, "at": round(time.time(), 3)}
        with self.lock:
            self.cache[history] = (time.time(), out)
        return out

    def latest(self, max_age):
        now = time.time()
        with self.lock:
            fresh = [(t, v) for t, v in self.cache.values() if now - t < max_age]
        return max(fresh, key=lambda tv: tv[0])[1] if fresh else None

    def serving(self, max_age=300):
        """What dgx-serve says each node serves (rack up's record, the
        monitor's serving block), by the name the engine serves it under.
        From the last fetch only: a model's facts never wait on the network."""
        d = self.latest(max_age)
        out = {}
        for n in (d or {}).get("nodes") or []:
            s = n.get("serving")
            if isinstance(s, dict) and (s.get("served_name") or s.get("model")):
                out.setdefault(s.get("served_name") or s.get("model"), dict(s, node=n.get("name")))
        return out

    def engine_stats(self):
        """What every monitored engine is doing right now, summed: the
        playground asks when a stream goes quiet."""
        d = self.latest(max_age=6.0) or self.cluster(0, max_age=3.0)
        engines = [e for n in d["nodes"] if n.get("ok") is not False
                   for e in (n.get("engines") or []) if e.get("ok", True) and e.get("kind") != "openai"]
        if not engines:
            return {"ok": False, "error": "no engine on any monitor" if d["monitors"] else "no monitor configured"}

        def total(k):
            return round(sum(e.get(k) or 0 for e in engines), 1)
        return {"ok": True, "rate": total("gen_tps"), "prompt_rate": total("prompt_tps"),
                "running": int(total("running")), "source": "monitor"}


def _try_hello(host, port):
    h = "[%s]" % host if ":" in host else host
    url = "http://%s:%d" % (h, port)
    try:
        d = hello(url, timeout=2.0)
    except Exception:  # noqa: BLE001
        return None
    return {"url": url, "host": host, "name": d.get("name"), "cluster": d.get("cluster"),
            "role": d.get("role"), "peers": d.get("peers") or 0, "version": d.get("version")}


def discover(cfg, extra_hosts=None, include_tailnet=True, port=DEFAULT_PORT):
    """Find rack monitors: this machine, the hosts of known gateways and
    monitors, tailnet peers. Only /v1/hello is asked; it needs no token."""
    hosts = ["127.0.0.1"]
    for g in gwmod.normalize(cfg) + normalize(cfg):
        h = urllib.parse.urlsplit(g["url"]).hostname
        if h and h not in hosts:
            hosts.append(h)
    for h in extra_hosts or []:
        h = str(h).strip()
        if h and h not in hosts:
            hosts.append(h)
    if include_tailnet:
        ips, _names = gwmod.tailscale_peers(with_names=True)
        for h in ips:
            if h not in hosts:
                hosts.append(h)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=48) as pool:
        opened = [h for h, ok in zip(hosts, pool.map(lambda h: gwmod._open(h, port), hosts)) if ok]
        found = [r for r in pool.map(lambda h: _try_hello(h, port), opened) if r]
    known = {m["url"] for m in normalize(cfg)}
    for r in found:
        r["configured"] = r["url"] in known
    # a head (it has peers) covers its rack: list it first
    found.sort(key=lambda r: (r["configured"], -r["peers"], r["host"]))
    return {"found": found, "hosts_probed": len(hosts), "port": port, "ms": int((time.time() - t0) * 1000)}
