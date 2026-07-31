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

# MCP servers start lazily on first use: a console that never opens a tool
# should not spawn subprocesses, and a broken server config must not stop the
# console from booting.
_MCP = None
_MCP_LOCK = threading.Lock()


def mcp_host():
    global _MCP
    with _MCP_LOCK:
        if _MCP is None:
            from mcp import MCPHost
            _MCP = MCPHost(CFG.get("mcp_servers") or {})
        return _MCP


def mcp_reload():
    """Tear down every server and start from the current config. Called after
    any edit so changes take effect without restarting the console."""
    global _MCP
    with _MCP_LOCK:
        if _MCP is not None:
            _MCP.stop_all()
        from mcp import MCPHost
        _MCP = MCPHost(CFG.get("mcp_servers") or {})
        return _MCP


def save_config():
    """Persist CFG back to config.json, preserving formatting sanity. Written
    atomically so a crash mid-write cannot leave the console unbootable."""
    path = os.path.join(ROOT, "config.json")
    tmp = path + ".tmp"
    with _LOCK:
        with open(tmp, "w") as f:
            json.dump(CFG, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)


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
def sanitize_usage(body):
    """The ledger is permanent; one garbage line must never poison every read.
    Coerce to known-good types here, and drop the event if nothing survives."""
    if not isinstance(body, dict):
        return None
    def as_int(v):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return None
    def as_float(v):
        try:
            return round(float(v), 3)
        except (TypeError, ValueError):
            return None
    evt = {
        "model": str(body.get("model") or "unknown")[:200],
        "prompt_tokens": as_int(body.get("prompt_tokens")),
        "completion_tokens": as_int(body.get("completion_tokens")),
        "ttft_s": as_float(body.get("ttft_s")),
        "decode_tok_s": as_float(body.get("decode_tok_s")),
        "estimated": bool(body.get("estimated")),
    }
    return evt if evt["completion_tokens"] else None


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
                if not isinstance(e, dict) or e.get("ts", 0) < horizon:
                    continue
                try:  # old or hand-edited lines must not poison the ledger
                    out = int(e.get("completion_tokens") or 0)
                    inn = int(e.get("prompt_tokens") or 0)
                    dec = float(e["decode_tok_s"]) if e.get("decode_tok_s") else None
                except (TypeError, ValueError):
                    continue
                tot_out += out
                tot_in += inn
                day = time.strftime("%d", time.localtime(e["ts"]))
                days[day] = days.get(day, 0) + out
                m = str(e.get("model") or "unknown")
                by_model[m] = by_model.get(m, 0) + out
                if dec and not e.get("estimated"):
                    tps.append(dec)
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
        util = one('nvidia_smi_utilization_gpu_ratio{instance=~"%(i)s.*"}')
        out.append({
            "name": node["name"],
            # exporter reports a 0-1 ratio; the UI speaks percent
            "util": round(util * 100, 1) if util is not None else None,
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

    # Loopback binding stops remote packets, not the operator's own browser.
    # A hostile page can reach 127.0.0.1 via DNS rebinding (its origin becomes
    # this host) or fire preflight-free "simple" POSTs cross-origin. So: only
    # accept our own Host, reject foreign Origins, and require JSON POSTs.
    def _guard(self):
        hostname = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if hostname not in ("127.0.0.1", "localhost", "::1", CFG.get("bind", "")):
            self._json({"error": "bad host"}, 403, close=True)
            return False
        origin = self.headers.get("Origin")
        if origin:
            ohost = urllib.parse.urlparse(origin).hostname
            if ohost not in ("127.0.0.1", "localhost", "::1", CFG.get("bind", "")):
                self._json({"error": "cross-origin denied"}, 403, close=True)
                return False
        if self.command in ("POST", "DELETE"):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if self.command == "POST" and ctype != "application/json":
                self._drain()
                self._json({"error": "expected application/json"}, 415, close=True)
                return False
        return True

    def _drain(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            while n > 0:
                n -= len(self.rfile.read(min(n, 65536)) or b"x")
        except (ValueError, OSError):
            self.close_connection = True

    def do_OPTIONS(self):
        self._json({"error": "forbidden"}, 403, close=True)

    # ---- helpers ----
    def _json(self, obj, code=200, close=False):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
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
        """Read and parse the JSON body; None (plus a 400 already sent) on garbage."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "invalid JSON body"}, 400, close=True)
            return None

    # ---- routes ----
    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            self._json({
                # spec is optional operator-declared hardware copy; the UI
                # renders it verbatim or omits the line — it never invents one
                "nodes": [{"name": n["name"], "spec": n.get("spec", "")}
                          for n in CFG.get("nodes", [])],
                "identity": CFG.get("identity", {}),
                "upstream": CFG.get("upstream_url", ""),
                "telemetry": bool(CFG.get("prometheus_url")),
                "mcp": bool(CFG.get("mcp_servers")),
            })
        elif path == "/api/models":
            try:
                with urllib.request.urlopen(upstream_request("/models"), timeout=8) as r:
                    self._json(json.load(r))
            except Exception as e:
                self._json({"error": str(e), "data": []}, 502)
        elif path == "/api/telemetry":
            self._json(telemetry())
        elif path == "/api/tools":
            try:
                h = mcp_host()
                defs = h.openai_tools()
                body = {"servers": h.status, "tools": [
                    {"name": t["function"]["name"],
                     "description": t["function"]["description"][:200]}
                    for t in defs]}
                if urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("full"):
                    body["defs"] = defs   # real JSON Schemas for the model
                body["config"] = CFG.get("mcp_servers", {})
                self._json(body)
            except Exception as e:
                self._json({"servers": {}, "tools": [], "error": str(e)[:300]})
        elif path == "/api/sessions":
            self._json(read_sessions())
        elif path == "/api/usage":
            self._json(usage_summary())
        else:
            self._static(path)

    def do_DELETE(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        self._drain()
        if u.path == "/api/sessions":
            sid = urllib.parse.parse_qs(u.query).get("id", [None])[0]
            with _LOCK:
                write_sessions([s for s in read_sessions() if s.get("id") != sid])
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/chat", "/api/sessions", "/api/usage-event",
                        "/api/tool-call", "/api/mcp"):
            self._drain()  # unread bodies desync HTTP/1.1 keep-alive
            self._json({"error": "not found"}, 404)
            return
        body = self._body()
        if body is None:
            return
        if path == "/api/chat":
            self._chat(body)
        elif path == "/api/tool-call":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            text, is_err = mcp_host().call(body.get("name") or "", body.get("arguments") or {})
            self._json({"content": text, "isError": is_err})
        elif path == "/api/mcp":
            self._mcp_admin(body)
        elif path == "/api/sessions":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            with _LOCK:
                cur = [x for x in read_sessions() if x.get("id") != body.get("id")]
                cur.insert(0, body)
                write_sessions(cur[:200])
            self._json({"ok": True})
        elif path == "/api/usage-event":
            evt = sanitize_usage(body)
            if evt:
                append_usage(evt)
            self._json({"ok": bool(evt)})

    # ---- MCP server management ----
    # add / remove / toggle / restart, persisted to config.json and applied
    # live. Command strings are never shell-parsed — they go straight to
    # Popen as argv, so there is no shell-injection surface here.
    def _mcp_admin(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action")
        servers = CFG.setdefault("mcp_servers", {})
        name = (body.get("name") or "").strip()

        if action == "add":
            if not name or not re.match(r"^[A-Za-z0-9_-]{1,32}$", name):
                self._json({"error": "name must be 1-32 chars: letters, digits, - or _"}, 400)
                return
            cmd = (body.get("command") or "").strip()
            if not cmd:
                self._json({"error": "command is required"}, 400)
                return
            args = body.get("args")
            if isinstance(args, str):
                args = [a for a in args.split() if a]
            servers[name] = {
                "command": cmd,
                "args": list(args or []),
                "env": body.get("env") or {},
                "enabled": True,
            }
        elif action == "remove":
            if name not in servers:
                self._json({"error": "no such server"}, 404)
                return
            servers.pop(name)
        elif action == "toggle":
            if name not in servers:
                self._json({"error": "no such server"}, 404)
                return
            servers[name]["enabled"] = not servers[name].get("enabled", True)
        elif action == "restart":
            pass  # config unchanged; the reload below does the work
        else:
            self._json({"error": "unknown action"}, 400)
            return

        save_config()
        h = mcp_reload()
        self._json({"ok": True, "servers": h.status,
                    "config": CFG.get("mcp_servers", {})})

    # ---- streaming chat proxy ----
    RETRY_STRIP = ("reasoning_effort", "top_k", "repetition_penalty", "stream_options")

    def _chat(self, payload):
        if not isinstance(payload, dict):
            self._json({"error": "expected object"}, 400)
            return
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
        self.close_connection = True
        self.end_headers()
        # Forward raw bytes as they arrive — the client parses SSE framing.
        # (A readline-per-event loop holds each event's terminating blank line
        # hostage until the NEXT event arrives: the stream renders one token
        # late, permanently.) read1 returns whatever the socket has.
        try:
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away — nothing to tell it
            except Exception as e:
                # upstream died mid-stream: without this, the client sees a
                # clean EOF and silently renders a truncated reply as complete
                try:
                    msg = json.dumps({"error": "upstream stream failed: " + str(e)[:200]})
                    self.wfile.write(("data: " + msg + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    pass
        finally:
            resp.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=CFG.get("port", 8765))
    ap.add_argument("--bind", default=CFG.get("bind", "127.0.0.1"))
    args = ap.parse_args()
    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        # There is no auth on any route. Loopback-only is the security model;
        # a wider bind turns the rack into an open inference gateway for the
        # whole LAN. Refuse rather than warn — reach it over SSH or the overlay.
        raise SystemExit(
            "refusing to bind %s: the console has no auth and is loopback-only "
            "by design. Reach it via SSH tunnel or overlay network." % args.bind)
    CFG["bind"] = args.bind
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("ByteBunker Console on http://%s:%d  (upstream %s)" %
          (args.bind, args.port, CFG.get("upstream_url")))
    srv.serve_forever()


if __name__ == "__main__":
    main()
