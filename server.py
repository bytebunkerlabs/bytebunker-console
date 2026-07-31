#!/usr/bin/env python3
"""ByteBunker Console — one-file server. Stdlib only, Python 3.9+.

Successor to chatserve: serves the console UI and provides the small API the
UI needs. Nothing leaves the rack — the only outbound calls are to the
upstream OpenAI-compatible endpoint (your gateway or vLLM) and, optionally,
your own Prometheus.

  python3 server.py                 # reads config.json next to this file
  python3 server.py --port 8765

API:
  GET  /                     the console
  GET  /api/config           UI-facing config (node names, identity, rates)
  GET  /api/models           proxied upstream /v1/models
  POST /api/chat             proxied streaming /v1/chat/completions (SSE)
  GET  /api/telemetry        Prometheus-backed node cards (or {"nodes": []})
  GET  /api/sessions         list sessions  |  POST save  |  DELETE ?id=
  POST /api/usage-event      client-reported completion stats -> usage.jsonl
  GET  /api/usage            14-day aggregates for the Usage screen
"""
import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)

DEFAULT_CONFIG = {
    "bind": "127.0.0.1",
    "port": 8765,
    "upstream_url": "http://127.0.0.1:8000/v1",
    "upstream_key": "bb-local",
    "prometheus_url": "",
    "nodes": [
        {"name": "spark-1", "instance": "spark-1"},
        {"name": "spark-2", "instance": "spark-2"},
    ],
    "identity": {"user": "mo@bunker", "host": "local"},
    "frontier_rates_per_mtok": {"input": 3.0, "output": 15.0},
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()
_LOCK = threading.Lock()


# ---------------------------------------------------------------- sessions --
def _sessions_path():
    return os.path.join(DATA, "sessions.json")


def read_sessions():
    try:
        with open(_sessions_path()) as f:
            return json.load(f)
    except Exception:
        return []


def write_sessions(sessions):
    tmp = _sessions_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sessions, f)
    os.replace(tmp, _sessions_path())


# ------------------------------------------------------------------- usage --
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
    tps = []
    tot_in = tot_out = 0
    try:
        with open(os.path.join(DATA, "usage.jsonl")) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("ts", 0) < horizon:
                    continue
                out = int(e.get("completion_tokens") or 0)
                inn = int(e.get("prompt_tokens") or 0)
                tot_out += out
                tot_in += inn
                day = time.strftime("%d", time.localtime(e["ts"]))
                days[day] = days.get(day, 0) + out
                m = e.get("model") or "unknown"
                by_model[m] = by_model.get(m, 0) + out
                if e.get("decode_tok_s"):
                    tps.append(float(e["decode_tok_s"]))
    except FileNotFoundError:
        pass
    tps.sort()
    rates = CFG.get("frontier_rates_per_mtok", {})
    saved = (tot_in / 1e6) * float(rates.get("input", 0)) + \
            (tot_out / 1e6) * float(rates.get("output", 0))
    return {
        "total_out": tot_out,
        "median_tok_s": tps[len(tps) // 2] if tps else None,
        "requests": sum(1 for _ in tps) or None,
        "frontier_saved_usd": round(saved, 2),
        "days": days,
        "by_model": by_model,
    }


# -------------------------------------------------------------- prometheus --
def prom_query(q):
    base = CFG.get("prometheus_url", "").rstrip("/")
    if not base:
        return None
    url = base + "/api/v1/query?" + urllib.parse.urlencode({"query": q})
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            d = json.load(r)
        if d.get("status") == "success":
            return d["data"]["result"]
    except Exception:
        return None
    return None


# Queries assume utkuozdemir/nvidia_gpu_exporter + node-exporter, one pair per
# node, labelled by instance. Adjust to your labels; every query failing just
# renders as an em-dash in the UI, never a fake number.
def telemetry():
    out = []
    for node in CFG.get("nodes", []):
        inst = node.get("instance", node["name"])
        def one(q):
            r = prom_query(q % {"i": inst})
            try:
                return float(r[0]["value"][1])
            except (TypeError, IndexError, KeyError, ValueError):
                return None
        mem_total = one('node_memory_MemTotal_bytes{instance=~"%(i)s.*"}')
        mem_avail = one('node_memory_MemAvailable_bytes{instance=~"%(i)s.*"}')
        used_gb = None
        if mem_total and mem_avail:
            used_gb = round((mem_total - mem_avail) / 2**30, 1)
        out.append({
            "name": node["name"],
            "util": one('nvidia_smi_utilization_gpu_ratio{instance=~"%(i)s.*"}'),
            "mem_used_gb": used_gb,
            "mem_total_gb": round(mem_total / 2**30) if mem_total else None,
            "temp": one('nvidia_smi_temperature_gpu{instance=~"%(i)s.*"}'),
            "power": one('nvidia_smi_power_draw_watts{instance=~"%(i)s.*"}'),
            "cpu": one('100 - avg(rate(node_cpu_seconds_total{mode="idle",instance=~"%(i)s.*"}[1m])) * 100'),
            "uptime_s": one('time() - node_boot_time_seconds{instance=~"%(i)s.*"}'),
        })
    return {"nodes": out}


# ---------------------------------------------------------------- upstream --
def upstream_request(path, payload=None, method="GET"):
    url = CFG["upstream_url"].rstrip("/") + path
    headers = {
        "Authorization": "Bearer " + CFG.get("upstream_key", "none"),
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    return urllib.request.Request(url, data=data, headers=headers, method=method)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- helpers ----
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        fs = os.path.realpath(os.path.join(PUBLIC, path.lstrip("/")))
        if not fs.startswith(os.path.realpath(PUBLIC)) or not os.path.isfile(fs):
            self._json({"error": "not found"}, 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css",
            ".js": "text/javascript",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(fs)[1], "application/octet-stream")
        with open(fs, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- routes ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            self._json({
                "nodes": [n["name"] for n in CFG.get("nodes", [])],
                "identity": CFG.get("identity", {}),
                "upstream": CFG.get("upstream_url", ""),
                "telemetry": bool(CFG.get("prometheus_url")),
            })
        elif path == "/api/models":
            try:
                with urllib.request.urlopen(upstream_request("/models"), timeout=8) as r:
                    self._json(json.load(r))
            except Exception as e:
                self._json({"error": str(e), "data": []}, 502)
        elif path == "/api/telemetry":
            self._json(telemetry())
        elif path == "/api/sessions":
            self._json(read_sessions())
        elif path == "/api/usage":
            self._json(usage_summary())
        else:
            self._static(path)

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/sessions":
            sid = urllib.parse.parse_qs(u.query).get("id", [None])[0]
            with _LOCK:
                write_sessions([s for s in read_sessions() if s.get("id") != sid])
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/chat":
            self._chat()
        elif path == "/api/sessions":
            s = self._body()
            with _LOCK:
                cur = [x for x in read_sessions() if x.get("id") != s.get("id")]
                cur.insert(0, s)
                write_sessions(cur[:200])
            self._json({"ok": True})
        elif path == "/api/usage-event":
            append_usage(self._body())
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    # ---- streaming chat proxy ----
    RETRY_STRIP = ("reasoning_effort", "top_k", "repetition_penalty")

    def _chat(self):
        payload = self._body()
        payload["stream"] = True
        payload.setdefault("stream_options", {"include_usage": True})

        def attempt(p):
            req = upstream_request("/chat/completions", p, "POST")
            return urllib.request.urlopen(req, timeout=600)

        try:
            try:
                resp = attempt(payload)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                # Pure-OpenAI upstreams reject vLLM extras; strip and retry once.
                if e.code == 400 and any(k in detail for k in self.RETRY_STRIP):
                    for k in self.RETRY_STRIP:
                        payload.pop(k, None)
                    resp = attempt(payload)
                else:
                    self._json({"error": detail[:500]}, e.code)
                    return
        except Exception as e:
            self._json({"error": str(e)}, 502)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                # accumulate a line at a time for prompt flushing
                line = chunk + resp.readline()
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=CFG.get("port", 8765))
    ap.add_argument("--bind", default=CFG.get("bind", "127.0.0.1"))
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("ByteBunker Console on http://%s:%d  (upstream %s)" %
          (args.bind, args.port, CFG.get("upstream_url")))
    srv.serve_forever()


if __name__ == "__main__":
    main()
