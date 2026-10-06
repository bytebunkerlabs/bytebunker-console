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
    """Config → list of gateway dicts. A console configured the old way (no
    `gateways` key, one upstream_url) gets one gateway named 'upstream'. An
    explicit empty list stays empty: that is a fresh desktop install, and the
    UI offers discovery. The first enabled gateway is mirrored back into
    upstream_url/upstream_key for code that still reads them."""
    gws = cfg.get("gateways")
    if not isinstance(gws, list):
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
    cfg["upstream_url"] = first["url"] if first else ""
    cfg["upstream_key"] = (first.get("key") or "") if first else ""
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
        self.served = {}            # model id -> the window its engine says it serves (vLLM max_model_len)
        self.at = 0

    def live(self):
        """Live stats for every enabled gateway; tokens/s from the counter
        delta since the previous call."""
        gws = self.gateways()
        out = {}
        if not gws:
            return out

        def one(g):
            try:
                return g, live_stats(g), None
            except Exception as e:   # noqa: BLE001
                return g, {}, str(e)[:120]
        prev = getattr(self, "_live_prev", {})
        now = time.time()
        with ThreadPoolExecutor(max_workers=min(8, len(gws))) as pool:
            for g, st, err in pool.map(one, gws):
                if "gen_total" in st:
                    p = prev.get(g["name"])
                    if p and 0 < now - p[0] < 600 and st["gen_total"] >= p[1]:
                        st["gen_tps"] = round((st["gen_total"] - p[1]) / (now - p[0]), 1)
                        st["prompt_tps"] = round((st["prompt_total"] - p[2]) / (now - p[0]), 1)
                    prev[g["name"]] = (now, st["gen_total"], st["prompt_total"])
                if err:
                    st["error"] = err
                out[g["name"]] = st
        self._live_prev = prev
        return out

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
            # vLLM says the window it serves; the capability table only guesses
            ctx = {m.get("id"): m["max_model_len"] for m in (data.get("data") or [])
                   if isinstance(m.get("max_model_len"), int) and m["max_model_len"] > 0}
            return g, {"ok": True, "models": len(ids), "ms": int((time.time() - t0) * 1000), "error": None,
                       "kind": g.get("kind") or detect_kind(g["url"], owned), "url": g["url"]}, ids, owned, ctx
        except Exception as e:   # noqa: BLE001
            return g, {"ok": False, "models": 0, "ms": int((time.time() - t0) * 1000), "error": str(e)[:160],
                       "kind": g.get("kind") or "", "url": g["url"]}, [], {}, {}

    def refresh(self, force=False):
        with self._lock:
            if not force and time.time() - self.at < TTL and self.models:
                return
            gws = self.gateways()
            models, model_map, status, pinned = [], {}, {}, []
            if gws:
                with ThreadPoolExecutor(max_workers=min(8, len(gws))) as pool:
                    probed = list(pool.map(self._probe, gws))
                served = {}
                for _g, _st, _ids, _owned, ctx in probed:
                    for mid, n in ctx.items():
                        served.setdefault(mid, n)
                self.served = served             # before caps_for reads it below
                for g, st, ids, owned, _ctx in probed:
                        status[g["name"]] = st
                        if st["kind"] and not g.get("kind"):
                            g["kind"] = st["kind"]
                        for mid in ids:
                            if mid in model_map:
                                # served twice (litellm and the engine behind it,
                                # say): the first gateway wins the plain name and
                                # "id@gateway" pins the other one
                                for m in models:
                                    if m["id"] == mid:
                                        m.setdefault("also_on", []).append(g["name"])
                                pinned.append({"id": mid + "@" + g["name"], "base_id": mid, "gateway": g["name"],
                                               "owned_by": owned.get(mid), "caps": self.caps_for(mid),
                                               "object": "model", "pinned": True})
                                continue
                            model_map[mid] = g["name"]
                            models.append({"id": mid, "gateway": g["name"], "owned_by": owned.get(mid),
                                           "caps": self.caps_for(mid), "object": "model"})
            self.models = models + pinned
            self.model_map, self.status, self.at = model_map, status, time.time()

    def resolve(self, model):
        """'id' or 'id@gateway' → (model id, gateway dict). Unknown ids go to the
        first enabled gateway, which is what a single-upstream console did."""
        model = str(model or "")
        if "@" in model:
            # only a pin when the suffix names a gateway: some providers put
            # "@" in their own ids
            base, gw_name = model.rsplit("@", 1)
            g = self.gateway(gw_name)
            if g and g.get("enabled", True):
                return base, g
        if model not in self.model_map:
            self.refresh(force=time.time() - self.at > 5)
        name = self.model_map.get(model)
        g = self.gateway(name) if name else None
        if g is None:
            gws = self.gateways()
            g = gws[0] if gws else None
        return model, g


def _base(url):
    return url[:-3] if url.endswith("/v1") else url


def live_stats(g):
    """What an engine is doing right now, from its own endpoints: vLLM's
    Prometheus /metrics, Ollama's /api/ps. Routers (litellm) and engines
    without such endpoints return {} — their model list is all we know."""
    kind = g.get("kind") or ""
    base = _base(g["url"])
    if kind == "vllm":
        req = urllib.request.Request(base + "/metrics")
        if g.get("key"):
            req.add_header("Authorization", "Bearer " + g["key"])
        with urllib.request.urlopen(req, timeout=4) as r:
            txt = r.read(4_000_000).decode("utf-8", "replace")
        vals = {}
        for ln in txt.splitlines():
            if not ln.startswith("vllm:") or " " not in ln:
                continue
            name, v = ln.rsplit(" ", 1)
            try:
                vals[name.split("{", 1)[0]] = vals.get(name.split("{", 1)[0], 0.0) + float(v)
            except ValueError:
                continue
        kv = vals.get("vllm:kv_cache_usage_perc", vals.get("vllm:gpu_cache_usage_perc", 0.0))
        return {"running": int(vals.get("vllm:num_requests_running", 0)),
                "waiting": int(vals.get("vllm:num_requests_waiting", 0)),
                "kv_pct": round(100.0 * kv, 1),
                "gen_total": vals.get("vllm:generation_tokens_total", 0.0),
                "prompt_total": vals.get("vllm:prompt_tokens_total", 0.0)}
    if kind == "ollama":
        ps = get_json(base + "/api/ps", g.get("key"), timeout=4)
        return {"loaded": [{"name": m.get("name"), "vram_gb": round((m.get("size_vram") or 0) / 2 ** 30, 1)}
                           for m in ps.get("models") or []]}
    return {}


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


def tailscale_peers(with_names=False):
    """IPv4 of online tailnet peers, via the local tailscale CLI if present.
    with_names: also return their MagicDNS names (for https on 443, where a
    certificate names the host, not the address)."""
    for exe in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/bin/tailscale",
                r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=6).stdout
            d = json.loads(out)
            # only machines in this tailnet: a Mullvad add-on lists hundreds of
            # exit nodes as peers, and shared-in services (hello.ts.net) are not ours
            suffix = str(d.get("MagicDNSSuffix") or "").strip(".")
            ips, names = [], []
            for p in (d.get("Peer") or {}).values():
                if p.get("Online") is False:
                    continue
                if any("mullvad" in str(t) for t in (p.get("Tags") or [])):
                    continue
                if suffix and not str(p.get("DNSName") or "").rstrip(".").endswith("." + suffix):
                    continue
                for ip in p.get("TailscaleIPs") or []:
                    if ":" not in ip:
                        ips.append(ip)
                dns = str(p.get("DNSName") or "").rstrip(".")
                if dns:
                    names.append(dns)
            return (ips, names) if with_names else ips
        except Exception:   # noqa: BLE001
            continue
    return ([], []) if with_names else []


def _open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_endpoint(host, port, key=None, scheme="http"):
    url = ("%s://%s/v1" % (scheme, host)) if (scheme, port) == ("https", 443) else ("%s://%s:%d/v1" % (scheme, host, port))
    try:
        data = get_json(url + "/models", key, timeout=3)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # an engine behind a key answers in JSON; a router or NAS login
            # page answers in HTML and is not a gateway
            try:
                json.loads(e.read(4000).decode("utf-8", "replace"))
            except Exception:   # noqa: BLE001
                return None
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
    ts_names = []
    if include_tailnet:
        ts_ips, ts_names = tailscale_peers(with_names=True)
        for h in ts_ips:
            if h not in hosts:
                hosts.append(h)
    if scan_lan:
        for h in local_subnet_hosts():
            if h not in hosts:
                hosts.append(h)
    targets = [("http", h, p) for h in hosts for p in ports] + [("https", n, 443) for n in ts_names]
    found = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=96) as pool:
        opened = [t for t, ok in zip(targets, pool.map(lambda t: _open(t[1], t[2]), targets)) if ok]
    with ThreadPoolExecutor(max_workers=16) as pool:
        for r in pool.map(lambda t: _probe_endpoint(t[1], t[2], None, t[0]), opened):
            if r:
                found.append(r)
    known = {g["url"] for g in normalize(cfg)}
    for r in found:
        r["configured"] = r["url"] in known
    found.sort(key=lambda r: (r["configured"], r["host"] != "127.0.0.1", r["host"], r["port"]))
    return {"found": found, "hosts_probed": len(hosts), "ports": ports, "ms": int((time.time() - t0) * 1000)}
