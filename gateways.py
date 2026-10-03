"""Gateways — every OpenAI-compatible endpoint the console can talk to.

A gateway is a base URL (…/v1) and an optional key: a litellm router in front
of the Sparks, a vLLM on a workstation GPU, Ollama on a laptop, LM Studio,
llama.cpp. The console merges their model lists, routes each request to the
gateway that serves the model, remembers which gateway served what for the
usage ledger, and can discover endpoints on localhost, on known hosts, on
tailnet peers, and across the local /24.

Stdlib only; probes are short and concurrent so a scan of a /24 takes seconds.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PORTS = [11434, 1234, 4000, 8000, 8001, 8080, 8888, 5000, 3000]
PORT_HINT = {11434: "ollama", 1234: "lmstudio", 4000: "litellm", 8888: "vllm", 8000: "vllm", 8001: "vllm", 8080: "llama.cpp", 5000: "openai-compatible", 3000: "openai-compatible"}
TTL = 60


def _norm_url(u):
    u = str(u or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    if not u.endswith("/v1"):
        u = u + "/v1"
    return u


def normalize(cfg):
    """Config → list of gateway dicts. A console configured the old way (one
    upstream_url) gets one gateway named 'upstream'; the first enabled gateway
    is mirrored back into upstream_url/upstream_key for code that still reads them."""
    gws = cfg.get("gateways")
    if not isinstance(gws, list) or not gws:
        gws = []
        if cfg.get("upstream_url"):
            gws.append({"name": "upstream", "url": _norm_url(cfg["upstream_url"]),
                        "key": cfg.get("upstream_key", ""), "enabled": True, "kind": "litellm"})
        cfg["gateways"] = gws
    out = []
    for g in gws:
        if not isinstance(g, dict) or not g.get("url"):
            continue
        g["url"] = _norm_url(g["url"])
        g.setdefault("name", g["url"].split("//")[-1].split("/")[0])
        g.setdefault("key", "")
        g.setdefault("enabled", True)
        g.setdefault("kind", "")
        out.append(g)
    first = next((g for g in out if g.get("enabled", True)), None)
    if first:
        cfg["upstream_url"] = first["url"]
        cfg["upstream_key"] = first.get("key") or ""
    return out


class Registry:
    """Merged model list across gateways, refreshed at most every TTL seconds."""

    def __init__(self, cfg, caps_for):
        self.cfg = cfg
        self.caps_for = caps_for
        self._lock = threading.Lock()
        self.models = []            # [{id, gateway, caps, owned_by, also_on}]
        self.model_map = {}         # id -> gateway name
        self.status = {}            # gateway name -> {ok, models, ms, error, kind}
        self.at = 0

    def gateways(self, enabled_only=True):
        gws = normalize(self.cfg)
        return [g for g in gws if g.get("enabled", True)] if enabled_only else gws

    def gateway(self, name):
        return next((g for g in normalize(self.cfg) if g["name"] == name), None)

    def _probe(self, g):
        t0 = time.time()
        try:
            data = get_json(g["url"] + "/models", g.get("key"), timeout=6)
            ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            owned = {m.get("id"): m.get("owned_by") for m in (data.get("data") or [])}
            return g, {"ok": True, "models": len(ids), "ms": int((time.time() - t0) * 1000), "error": None,
                       "kind": g.get("kind") or detect_kind(g["url"], owned)}, ids, owned
        except Exception as e:   # noqa: BLE001
            return g, {"ok": False, "models": 0, "ms": int((time.time() - t0) * 1000), "error": str(e)[:160],
                       "kind": g.get("kind") or ""}, [], {}

    def refresh(self, force=False):
        with self._lock:
            if not force and time.time() - self.at < TTL and self.models:
                return
            gws = self.gateways()
            models, model_map, status = [], {}, {}
            if gws:
                with ThreadPoolExecutor(max_workers=min(8, len(gws))) as pool:
                    for g, st, ids, owned in pool.map(self._probe, gws):
                        status[g["name"]] = st
                        if st["kind"] and not g.get("kind"):
                            g["kind"] = st["kind"]
                        for mid in ids:
                            if mid in model_map:
                                for m in models:
                                    if m["id"] == mid:
                                        m.setdefault("also_on", []).append(g["name"])
                                continue
                            model_map[mid] = g["name"]
                            models.append({"id": mid, "gateway": g["name"], "owned_by": owned.get(mid),
                                           "caps": self.caps_for(mid), "object": "model"})
            self.models, self.model_map, self.status, self.at = models, model_map, status, time.time()

    def resolve(self, model):
        """'id' or 'id@gateway' → (model id, gateway dict). Unknown ids go to the
        first enabled gateway, which is what a single-upstream console did."""
        model = str(model or "")
        gw_name = None
        if "@" in model:
            model, gw_name = model.rsplit("@", 1)
        if gw_name:
            g = self.gateway(gw_name)
            if g:
                return model, g
        if model not in self.model_map:
            self.refresh(force=time.time() - self.at > 5)
        name = self.model_map.get(model)
        g = self.gateway(name) if name else None
        if g is None:
            gws = self.gateways()
            g = gws[0] if gws else None
        return model, g


def get_json(url, key=None, timeout=6):
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def detect_kind(url, owned_by=None):
    """Best effort: what is behind this /v1?"""
    base = url[:-3] if url.endswith("/v1") else url
    owned = " ".join(str(v) for v in (owned_by or {}).values()).lower()
    try:
        get_json(base + "/api/tags", timeout=3)
        return "ollama"
    except Exception:   # noqa: BLE001
        pass
    if "vllm" in owned:
        return "vllm"
    if "organization_owner" in owned or "lmstudio" in owned:
        return "lmstudio"
    if "llama" in owned and "cpp" in owned:
        return "llama.cpp"
    try:
        req = urllib.request.Request(base + "/metrics")
        with urllib.request.urlopen(req, timeout=3) as r:
            head = r.read(4000).decode("utf-8", "replace")
        if "vllm:" in head:
            return "vllm"
        if "litellm" in head:
            return "litellm"
    except Exception:   # noqa: BLE001
        pass
    try:
        req = urllib.request.Request(base + "/health/liveliness")
        with urllib.request.urlopen(req, timeout=3) as r:
            if r.status == 200:
                return "litellm"
    except Exception:   # noqa: BLE001
        pass
    p = int(url.split(":")[-1].split("/")[0]) if ":" in url.split("//")[-1] else 0
    return PORT_HINT.get(p, "openai-compatible")


# -------------------------------------------------------------- discovery --
def local_subnet_hosts():
    """Addresses of the local /24 (the interface that routes outward)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        return []
    base = ip.rsplit(".", 1)[0]
    return [base + "." + str(i) for i in range(1, 255) if base + "." + str(i) != ip]


def tailscale_peers():
    """IPv4 of online tailnet peers, via the local tailscale CLI if present."""
    for exe in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/bin/tailscale",
                r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=6).stdout
            d = json.loads(out)
            ips = []
            for p in (d.get("Peer") or {}).values():
                if p.get("Online") is False:
                    continue
                for ip in p.get("TailscaleIPs") or []:
                    if ":" not in ip:
                        ips.append(ip)
            return ips
        except Exception:   # noqa: BLE001
            continue
    return []


def _open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_endpoint(host, port, key=None):
    url = "http://%s:%d/v1" % (host, port)
    try:
        data = get_json(url + "/models", key, timeout=3)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"url": url, "host": host, "port": port, "needs_key": True, "models": [], "kind": PORT_HINT.get(port, "")}
        return None
    except Exception:   # noqa: BLE001
        return None
    ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    owned = {m.get("id"): m.get("owned_by") for m in (data.get("data") or [])}
    return {"url": url, "host": host, "port": port, "needs_key": False, "models": ids[:40],
            "kind": detect_kind(url, owned)}


def discover(cfg, extra_hosts=None, scan_lan=False, include_tailnet=True, ports=None):
    ports = ports or PORTS
    hosts = ["127.0.0.1"]
    for g in normalize(cfg):
        h = g["url"].split("//")[-1].split("/")[0].split(":")[0]
        if h not in hosts:
            hosts.append(h)
    for h in (extra_hosts or []):
        h = str(h).strip()
        if h and h not in hosts:
            hosts.append(h)
    if include_tailnet:
        for h in tailscale_peers():
            if h not in hosts:
                hosts.append(h)
    if scan_lan:
        for h in local_subnet_hosts():
            if h not in hosts:
                hosts.append(h)
    pairs = [(h, p) for h in hosts for p in ports]
    found = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=96) as pool:
        opened = [pair for pair, ok in zip(pairs, pool.map(lambda hp: _open(*hp), pairs)) if ok]
    with ThreadPoolExecutor(max_workers=16) as pool:
        for r in pool.map(lambda hp: _probe_endpoint(*hp), opened):
            if r:
                found.append(r)
    known = {g["url"] for g in normalize(cfg)}
    for r in found:
        r["configured"] = r["url"] in known
    found.sort(key=lambda r: (r["configured"], r["host"] != "127.0.0.1", r["host"], r["port"]))
    return {"found": found, "hosts_probed": len(hosts), "ports": ports, "ms": int((time.time() - t0) * 1000)}
