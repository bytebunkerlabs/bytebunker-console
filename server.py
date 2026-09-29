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
  POST /api/archive          file away compressed turns  |  GET /api/archive/<name>
  POST /api/rate             thumbs up/down on a turn, into the trace log
  GET  /api/traces           trace log stats  |  GET /api/export?...  training JSONL
  GET  /api/skills           skill catalog  |  GET /api/skills/<name>  full body
  GET  /api/plugins          installed plugins  |  POST enable/disable/rescan
  GET  /api/agents           harness status + recent runs  |  POST run a goal (SSE)
  POST /api/usage-event      client-reported completion stats -> usage.jsonl
  GET  /api/usage            14-day aggregates for the Usage screen
  POST /api/video            multipart passthrough -> H3 /v1/videos (job id)
  GET  /api/video[/id[/content]]   job list / status / the finished MP4
  DELETE /api/video/<id>     drop a job and its stored output
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

from traces import TraceLog, StreamCapture, export_lines
import skills as skillmod
import agents as agentmod

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)
# Every model request, tool run, rating and archive, append-only, forever:
# data/traces/<day>.jsonl (gzipped after the day ends). See traces.py.
TRACE = TraceLog(DATA)

DEFAULT_CONFIG = {
    "bind": "127.0.0.1",
    "port": 8765,
    "upstream_url": "http://127.0.0.1:8000/v1",
    "upstream_key": "bb-local",
    "prometheus_url": "",
    "h3_url": "",
    "netcheck_ssh": "",
    "nodes": [
        {"name": "spark-1", "instance": "spark-1"},
        {"name": "spark-2", "instance": "spark-2"},
    ],
    "identity": {"user": "mo@bunker", "host": "local"},
    "frontier_rates_per_mtok": {"input": 3.0, "output": 15.0},
    # Safe by default: the harness launcher is inert until you enable it and
    # point dir/ssh at a dedicated worker host (see agents.py).
    "agents": {"enabled": False, "ssh": "", "dir": "", "python": "uv run",
               "script": "scripts/run_master.py"},
}


# What each model can actually do. An OpenAI-compatible gateway normalises the
# envelope, not the behaviour: two models behind the same LiteLLM will disagree
# about whether tools are accepted, whether thinking is on by default, which
# effort values are legal, and whether prior reasoning must be resent or
# stripped. Guessing produces a 400 that reads like a crash, so the client asks
# instead. Keys are matched as substrings of the model id, longest match
# wins (so "deepseek-v4-vision" beats "deepseek-v4"); override or extend via
# "model_capabilities" in config.json.
#
#   tools           send the tools array at all
#   effort          legal reasoning-effort values; [] hides the dial entirely
#   ctk             merged into chat_template_kwargs on every request
#   strip_reasoning drop prior <think> from resent history
#   ctx             context window the engine is SERVING (--max-model-len),
#                   not what the weights could do: it drives the meter and the
#                   max_tokens clamp. If the engine's window turns out smaller,
#                   the client learns the real figure from its overflow error
#                   and uses that for the rest of the session.
CAPS_FALLBACK = {"tools": True, "effort": [], "ctk": {}, "strip_reasoning": True,
                 "ctx": 131072}
DEFAULT_CAPS = {
    # vLLM defaults DeepSeek-V4 thinking OFF (DeepSeek's own API defaults it
    # ON at high) — so it must be asked for explicitly. The tokenizer wrapper
    # coerces any unrecognized effort string ('low', 'medium', ...) to 'high',
    # and only 'max' changes the prompt — so 'max' is the only value worth
    # offering. (The encoder one layer down does assert on 'low', but the
    # wrapper is its only caller, so the assert is unreachable via requests.)
    # strip_reasoning is False because DeepSeek 400s if reasoning_content is
    # missing from a tool exchange.
    "deepseek-v4": {"tools": True, "effort": ["max"], "strip_reasoning": False,
                    "ctk": {"thinking": True, "reasoning_effort": "max"},
                    "ctx": 1048576},   # native YaRN 1M; recipe serves full window
    # The vision recipe is served at 262k on both Sparks (--max-model-len,
    # measured 2026-09-24). Same caps otherwise. Re-serve it wider and this
    # line (or a model_capabilities override) must follow.
    "deepseek-v4-vision": {"tools": True, "effort": ["max"], "strip_reasoning": False,
                           "ctk": {"thinking": True, "reasoning_effort": "max"},
                           "ctx": 262144},
    # Inkling's renderer accepts none/minimal/low/medium/high/xhigh/max —
    # 'minimal', not 'min': an unknown name resolves to None and the template
    # falls back to its 0.9 default, i.e. the dial silently stops working.
    "inkling": {"tools": True,
                "effort": ["none", "minimal", "low", "medium", "high", "xhigh"],
                "strip_reasoning": True, "ctk": {}, "ctx": 262144},
}


def caps_for(model_id):
    table = dict(DEFAULT_CAPS)
    table.update(CFG.get("model_capabilities") or {})
    mid = (model_id or "").lower()
    hits = [k for k in table if k.lower() in mid]
    if hits:
        merged = dict(CAPS_FALLBACK)
        merged.update(table[max(hits, key=len)])
        return merged
    return dict(CAPS_FALLBACK)


def upstream_message(detail):
    """The sentence inside an upstream error body.

    A proxy chain wraps errors in envelopes — litellm puts vLLM's message
    inside {"error": {"message": ...}} and the console used to wrap that
    again — so the browser ended up rendering 400 characters of escaped JSON.
    Peel the envelopes; hand back the original text if they don't parse."""
    obj = detail
    for _ in range(3):
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except ValueError:
                break
        if isinstance(obj, dict):
            inner = obj.get("error", obj.get("message", obj.get("detail")))
            if isinstance(inner, dict):
                inner = inner.get("message")
            if not isinstance(inner, str):
                break
            obj = inner
            continue
        break
    return obj if isinstance(obj, str) else detail


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()
_LOCK = threading.Lock()

# Skills live in the repo's skills/, in any dir listed in config's skills_dirs
# (point one at the harness's skills/ to share them), and inside enabled
# plugins. Plugins live in the repo's plugins/ and in config's plugins_dirs.
BUILTIN_SKILLS = os.path.join(ROOT, "skills")
BUILTIN_PLUGINS = os.path.join(ROOT, "plugins")


def _plugin_state():
    return CFG.setdefault("plugins", {})


def discover_plugins():
    dirs = [BUILTIN_PLUGINS] + list(CFG.get("plugins_dirs") or [])
    found, warnings = skillmod.discover_plugins(dirs)
    return found, warnings


def enabled_plugins():
    found, _ = discover_plugins()
    st = _plugin_state()
    return {n: p for n, p in found.items() if st.get(n, {}).get("enabled")}


def skill_roots():
    """Ranked (dir, source) list; earlier wins duplicate names. Enabled
    plugins first, then configured dirs, then the repo's own skills/."""
    roots = []
    for name, p in sorted(enabled_plugins().items()):
        if p.skills_dir:
            roots.append((p.skills_dir, "plugin:" + name))
    for d in (CFG.get("skills_dirs") or []):
        roots.append((d, d))
    roots.append((BUILTIN_SKILLS, "built-in"))
    return roots


def skill_catalog():
    return skillmod.load_catalog(skill_roots())


def effective_mcp_servers():
    """config.json servers plus every enabled plugin's servers. A plugin
    server whose name collides with a configured one does not override it."""
    servers = dict(CFG.get("mcp_servers") or {})
    for name, p in sorted(enabled_plugins().items()):
        for sname, spec in (p.mcp_servers or {}).items():
            servers.setdefault(sname, dict(spec, _plugin=name))
    return servers

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
            _MCP = MCPHost(effective_mcp_servers())
        return _MCP


def mcp_reload():
    """Tear down every server and start from the current config. Called after
    any edit so changes take effect without restarting the console."""
    global _MCP
    with _MCP_LOCK:
        if _MCP is not None:
            _MCP.stop_all()
        from mcp import MCPHost
        _MCP = MCPHost(effective_mcp_servers())
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


# ------------------------------------------------------------------ archive --
# Compression moves the older part of a conversation out of the model's
# window and replaces it with a summary. The originals are not thrown away:
# each compression writes one file here, full fidelity (reasoning, tool
# calls, tool results) as JSON, with a readable Markdown transcript beside
# it. The transcript marker keeps the file name so it can link back.
ARCHIVE = os.path.join(DATA, "archive")
_ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9_-]+\.(json|md)$")


def write_archive(body):
    session = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("session") or ""))[:40] or "nosession"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(ARCHIVE, exist_ok=True)
    name, n = "%s_%s" % (session, stamp), 0
    while os.path.exists(os.path.join(ARCHIVE, name + ".json")):
        n += 1   # two compressions in one second must not overwrite each other
        name = "%s_%s-%d" % (session, stamp, n)
    msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
    rec = {"session": session, "model": str(body.get("model") or ""),
           "created": time.time(), "summary": str(body.get("summary") or ""),
           "messages": msgs}
    with open(os.path.join(ARCHIVE, name + ".json"), "w") as f:
        json.dump(rec, f, indent=1)
    with open(os.path.join(ARCHIVE, name + ".md"), "w") as f:
        f.write(archive_markdown(rec))
    TRACE.log("archive", session=session, file=name, count=len(msgs), model=rec["model"])
    return name


def read_archives():
    out = []
    if not os.path.isdir(ARCHIVE):
        return out
    for name in sorted(os.listdir(ARCHIVE)):
        if name.endswith(".json"):
            try:
                with open(os.path.join(ARCHIVE, name)) as f:
                    rec = json.load(f)
                rec["id"] = name[:-5]
                out.append(rec)
            except (OSError, ValueError):
                continue
    return out


def archive_markdown(rec):
    out = ["# Archived conversation \u2014 %s" % rec["session"], "",
           "model: %s  " % rec["model"],
           "archived: %s  " % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["created"])),
           "messages: %d" % len(rec["messages"]), "",
           "## Summary that replaced these messages", "", rec["summary"] or "(none)", "",
           "## Transcript", ""]
    for m in rec["messages"]:
        if not isinstance(m, dict):
            continue
        role = "User" if m.get("role") == "user" else "Assistant"
        if m.get("kind") == "summary":
            role = "Context summary (an earlier compression)"
        elif m.get("kind") == "summary-ack":
            continue
        out.append("### " + role)
        if m.get("reasoning"):
            out += ["", "<details><summary>reasoning</summary>", "", str(m["reasoning"]), "", "</details>"]
        out += ["", str(m.get("content") or ""), ""]
        for t in (m.get("toolUse") or []):
            if not isinstance(t, dict):
                continue
            out += ["**tool: %s**" % t.get("name", ""), "", "```json", str(t.get("args") or "{}"),
                    "```", "", "```", str(t.get("result", ""))[:20000], "```", ""]
    return "\n".join(out)


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
def _prom_query(q):
    base = CFG.get("prometheus_url", "").rstrip("/")
    url = base + "/api/v1/query?query=" + urllib.parse.quote(q)
    with urllib.request.urlopen(url, timeout=3) as r:
        d = json.load(r)
    vals = [float(s["value"][1]) for s in d.get("data", {}).get("result", [])]
    return sum(vals) if vals else None


def engine_stats():
    """The engine's own word on whether it is working. Some tool parsers
    (measured: deepseek_v4 — 64s of silent wire, then a whole file in 15
    bursts) buffer a tool call server-side while the GPU streams into a
    buffer. A dead stream and a busy-but-buffered stream are identical from
    the client, so the console asks Prometheus, which scrapes vLLM directly.
    Colons are legal in Prometheus metric names but not in PromQL bare
    selectors — hence the __name__ form."""
    if not CFG.get("prometheus_url"):
        return {"ok": False}
    try:
        rate = _prom_query('sum(rate({__name__="vllm:generation_tokens_total"}[20s]))')
        prompt = _prom_query('sum(rate({__name__="vllm:prompt_tokens_total"}[20s]))')
        running = _prom_query('sum({__name__="vllm:num_requests_running"})')
        if rate is None and prompt is None and running is None:
            return {"ok": False, "error": "prometheus has no vllm metrics"}
        return {"ok": True, "rate": rate or 0.0, "prompt_rate": prompt or 0.0,
                "running": -1 if running is None else running}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


# ---------------------------------------------------------------- netcheck --
# "Is my model truly local?" deserves a measurement, not an assurance. The
# head node's rack net audits every serving container from inside its own
# pid namespace (host-side ss -p silently loses root-owned sockets without
# sudo) and tags each established connection LOCAL or INTERNET. This runs it
# over SSH and caches the answer — a verdict is stable for minutes, and a
# page refresh should not fork ssh.
_NET = {"at": 0, "result": None}
_NET_LOCK = threading.Lock()


def netcheck(fresh=False):
    target = CFG.get("netcheck_ssh", "")
    if not target:
        return {"ok": False, "error": "netcheck_ssh not configured"}
    with _NET_LOCK:
        if not fresh and _NET["result"] and time.time() - _NET["at"] < 300:
            return _NET["result"]
        import subprocess
        try:
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
                 "cd dgx/dgx-spark-serve && ./rack net"],
                capture_output=True, text=True, timeout=60)
            out = re.sub(r"\x1b\[[0-9;]*m", "", (p.stdout + p.stderr)).strip()
            res = {"ok": p.returncode == 0 or "VERDICT" in out,
                   "at": int(time.time()),
                   "lines": out.splitlines()[:40]}
        except subprocess.TimeoutExpired:
            res = {"ok": False, "at": int(time.time()),
                   "error": "audit timed out after 60s"}
        except Exception as e:
            res = {"ok": False, "at": int(time.time()), "error": str(e)[:200]}
        _NET.update(at=time.time(), result=res)
        return res


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
            # /api/video carries file uploads; everything else stays JSON-only.
            # Multipart is a "simple request" a hostile form could fire without
            # preflight — but form POSTs carry Origin, and the check above
            # already rejected foreign ones.
            path = urllib.parse.urlparse(self.path).path
            want = "multipart/form-data" if path == "/api/video" else "application/json"
            if self.command == "POST" and ctype != want:
                self._drain()
                self._json({"error": "expected " + want}, 415, close=True)
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
        # revalidate every load: a fix to console.js must not wait on a
        # heuristic browser cache to expire
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _export(self, q):
        """Training data as JSONL: ?from=YYYY-MM-DD&to=...&model=substr
        &rated=up|any&errors=1&redact=0&source=all|traces|sessions
        &purpose=chat|compress. Streams; there is no telling the size first."""
        one = lambda k, d=None: (q.get(k) or [d])[0]
        with _LOCK:
            sessions = read_sessions()
        gen = export_lines(
            TRACE, sessions, read_archives(),
            day_from=one("from"), day_to=one("to"), model=one("model"),
            rated=one("rated"), include_errors=one("errors") == "1",
            do_redact=one("redact") != "0", source=one("source", "all"), purpose=one("purpose"))
        self.send_response(200)
        self.send_header("Content-Type", "application/jsonl; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="bytebunker-train-%s.jsonl"' % time.strftime("%Y%m%d-%H%M"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        n = 0
        try:
            for line in gen:
                self.wfile.write(line.encode("utf-8"))
                n += 1
        except (BrokenPipeError, ConnectionResetError):
            return
        TRACE.log("export", examples=n, query={k: v[0] for k, v in q.items()})

    def _archive_get(self, name):
        if not _ARCHIVE_NAME.match(name):
            self._json({"error": "not found"}, 404)
            return
        fs = os.path.join(ARCHIVE, name)
        if not os.path.isfile(fs):
            self._json({"error": "not found"}, 404)
            return
        with open(fs, "rb") as f:
            body = f.read()
        self.send_response(200)
        # text/plain for the .md: the browser shows it instead of downloading
        self.send_header("Content-Type", "application/json" if name.endswith(".json")
                         else "text/plain; charset=utf-8")
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

    # ---- video (MiniMax-H3 via vLLM-Omni) ----
    # /v1/videos is multipart form in, MP4 out, with async job polling. The
    # engine binds to loopback on spark-1 and is reached through the same SSH
    # tunnel that carries Prometheus — this proxy adds only the hop, plus the
    # host/origin guard every other route gets. Nothing new opens on the LAN.
    def _video_post(self):
        base = (CFG.get("h3_url") or "").rstrip("/")
        if not base:
            self._drain()
            self._json({"error": "no h3_url configured"}, 503)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= 80 * 1024 * 1024:  # engine rejects >64 MB; fail fast here
            self._drain()
            self._json({"error": "body missing or over 80 MB"}, 413)
            return
        req = urllib.request.Request(
            base + "/v1/videos", data=self.rfile.read(n), method="POST",
            headers={"Content-Type": self.headers.get("Content-Type") or ""})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                self._json(json.load(r))
        except urllib.error.HTTPError as e:
            self._json({"error": e.read().decode("utf-8", "replace")[:500]}, e.code)
        except Exception as e:
            self._json({"error": str(e)[:300]}, 502)

    def _video_get(self, path):
        base = (CFG.get("h3_url") or "").rstrip("/")
        if not base:
            self._json({"error": "no h3_url configured", "data": []}, 503)
            return
        parts = [p for p in path[len("/api/video"):].split("/") if p]
        bad = (len(parts) > 2 or (len(parts) == 2 and parts[1] != "content")
               or (parts and not re.fullmatch(r"[\w.-]+", parts[0])))
        if bad:
            self._json({"error": "not found"}, 404)
            return
        url = base + "/v1/videos" + "".join("/" + p for p in parts)
        try:
            if len(parts) == 2:  # the MP4 itself — stream it through
                with urllib.request.urlopen(url, timeout=600) as r:
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     r.headers.get("Content-Type") or "video/mp4")
                    clen = r.headers.get("Content-Length")
                    if clen:
                        self.send_header("Content-Length", clen)
                    else:  # unknown length can't keep-alive on HTTP/1.1
                        self.send_header("Connection", "close")
                        self.close_connection = True
                    self.end_headers()
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            else:  # job status, or the job list
                with urllib.request.urlopen(url, timeout=30) as r:
                    self._json(json.load(r))
        except urllib.error.HTTPError as e:
            self._json({"error": e.read().decode("utf-8", "replace")[:500]}, e.code)
        except Exception as e:
            self._json({"error": str(e)[:300]}, 502)

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
                "video": bool(CFG.get("h3_url")),
                "netcheck": bool(CFG.get("netcheck_ssh")),
            })
        elif path == "/api/models":
            try:
                with urllib.request.urlopen(upstream_request("/models"), timeout=8) as r:
                    body = json.load(r)
                for m in body.get("data") or []:
                    m["caps"] = caps_for(m.get("id"))
                self._json(body)
            except Exception as e:
                self._json({"error": str(e), "data": []}, 502)
        elif path == "/api/telemetry":
            self._json(telemetry())
        elif path == "/api/engine":
            self._json(engine_stats())
        elif path == "/api/netcheck":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(netcheck(fresh=bool(q.get("fresh"))))
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
        elif path.startswith("/api/archive/"):
            self._archive_get(path[len("/api/archive/"):])
        elif path == "/api/agents":
            st = agentmod.status(CFG)
            a = CFG.get("agents") or {}
            st["master_name"] = a.get("master_name") or ""
            st["master_instructions"] = a.get("master_instructions") or ""
            runs = []
            for e in TRACE.events(time.strftime("%Y-%m-%d",
                                  time.localtime(time.time() - 14 * 86400))):
                if e.get("kind") == "agent_run":
                    runs.append({"id": e.get("id"), "ts": e.get("ts"), "goal": e.get("goal"),
                                 "exit": e.get("exit"), "killed": e.get("killed"),
                                 "lines": e.get("lines"), "host": e.get("host")})
            st["recent"] = runs[-40:][::-1]
            self._json(st)
        elif path == "/api/agents/log":
            rid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
            found = None
            for e in TRACE.events(time.strftime("%Y-%m-%d",
                                  time.localtime(time.time() - 30 * 86400))):
                if e.get("kind") == "agent_run" and e.get("id") == rid:
                    found = e
            if not found:
                self._json({"error": "run not found"}, 404)
            else:
                self._json({"id": rid, "goal": found.get("goal"), "output": found.get("output") or [],
                            "exit": found.get("exit"), "killed": found.get("killed"),
                            "host": found.get("host"), "ts": found.get("ts")})
        elif path == "/api/agents/slaves":
            self._json(agentmod.recent_slaves(CFG))
        elif path == "/api/agents/stats":
            self._json(agentmod.stats(CFG))
        elif path == "/api/agents/slave":
            sid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
            self._json(agentmod.slave_detail(CFG, sid))
        elif path == "/api/skills":
            cat = skill_catalog()
            self._json({"skills": cat.summaries(), "warnings": cat.warnings})
        elif path.startswith("/api/skills/"):
            name = path[len("/api/skills/"):]
            sk = skill_catalog().get(name)
            if not sk:
                self._json({"error": "no such skill"}, 404)
            else:
                self._json({"name": sk.name, "body": sk.body(), **sk.summary()})
        elif path == "/api/plugins":
            found, warnings = discover_plugins()
            st = _plugin_state()
            self._json({"plugins": [found[n].info(bool(st.get(n, {}).get("enabled")))
                                    for n in sorted(found)], "warnings": warnings})
        elif path == "/api/traces":
            self._json(TRACE.stats())
        elif path == "/api/export":
            self._export(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
        elif path == "/api/usage":
            self._json(usage_summary())
        elif path == "/api/video" or path.startswith("/api/video/"):
            self._video_get(path)
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
                cur = read_sessions()
                for gone in cur:
                    if gone.get("id") == sid:
                        TRACE.log("session_deleted", session=sid, record=gone)
                write_sessions([s for s in cur if s.get("id") != sid])
            self._json({"ok": True})
        elif u.path.startswith("/api/video/"):
            base = (CFG.get("h3_url") or "").rstrip("/")
            vid = u.path.rsplit("/", 1)[1]
            if not base or not re.fullmatch(r"[\w.-]+", vid):
                self._json({"error": "not found"}, 404)
                return
            try:
                req = urllib.request.Request(base + "/v1/videos/" + vid,
                                             method="DELETE")
                with urllib.request.urlopen(req, timeout=30):
                    self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)[:300]}, 502)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/video":  # multipart, not JSON — handled whole
            self._video_post()
            return
        if path not in ("/api/chat", "/api/sessions", "/api/usage-event",
                        "/api/tool-call", "/api/mcp", "/api/archive", "/api/rate",
                        "/api/plugins", "/api/agents", "/api/agents/slave"):
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
            t0 = time.time()
            text, is_err = mcp_host().call(body.get("name") or "", body.get("arguments") or {})
            TRACE.log("tool", session=self.headers.get("X-BB-Session"),
                      turn=self.headers.get("X-BB-Turn"), name=body.get("name") or "",
                      arguments=body.get("arguments") or {}, result=str(text)[:200000],
                      is_error=bool(is_err), ms=int((time.time() - t0) * 1000))
            self._json({"content": text, "isError": is_err})
        elif path == "/api/mcp":
            self._mcp_admin(body)
        elif path == "/api/plugins":
            self._plugin_admin(body)
        elif path == "/api/agents":
            self._agents_run(body)
        elif path == "/api/agents/slave":
            if not isinstance(body, dict) or body.get("action") != "kill":
                self._json({"error": "expected {action: kill, id}"}, 400)
                return
            res = agentmod.kill_slave(CFG, str(body.get("id") or ""))
            TRACE.log("slave_kill", id=body.get("id"), result=res.get("result"), ok=res.get("ok"))
            self._json(res)
        elif path == "/api/rate":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            try:
                rating = max(-1, min(1, int(body.get("rating") or 0)))
            except (TypeError, ValueError):
                rating = 0
            TRACE.log("rating", session=body.get("session"), turn=body.get("turn"),
                      traces=[str(t)[:32] for t in (body.get("traces") or [])][:64],
                      model=body.get("model"), rating=rating)
            self._json({"ok": True})
        elif path == "/api/archive":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            try:
                self._json({"ok": True, "file": write_archive(body)})
            except OSError as e:
                self._json({"error": "could not write archive: " + str(e)[:200]}, 500)
        elif path == "/api/sessions":
            if not isinstance(body, dict):
                self._json({"error": "expected object"}, 400)
                return
            with _LOCK:
                cur = [x for x in read_sessions() if x.get("id") != body.get("id")]
                cur.insert(0, body)
                for old in cur[200:]:   # aged out of the UI, not out of the record
                    TRACE.log("session_evicted", session=old.get("id"), record=old)
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
    def _agents_run(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        if body.get("action") == "config":
            # Save the master's name + instructions into the agents block.
            a = CFG.setdefault("agents", {})
            a["master_name"] = str(body.get("master_name") or "")[:120]
            a["master_instructions"] = str(body.get("master_instructions") or "")[:8000]
            save_config()
            self._json({"ok": True, "master_name": a["master_name"],
                        "master_instructions": a["master_instructions"]})
            return
        goal = body.get("goal") or ""
        try:
            cmd = agentmod.build_command(CFG, goal)
        except agentmod.AgentConfigError as e:
            self._json({"error": str(e)}, 400)
            return
        st = agentmod.status(CFG)
        cwd = None if st["mode"] == "ssh" else os.path.expanduser((CFG.get("agents") or {}).get("dir") or ".")
        started = time.time()
        rid = TRACE.new_id()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        stop = threading.Event()
        lines = [0]
        captured = []   # full master output, stored so a past run can be reopened
        wlock = threading.Lock()   # emitter and heartbeat share one socket

        def emit(obj):
            try:
                with wlock:
                    self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                    self.wfile.flush()
            except OSError:
                stop.set()   # client went away — stop the run

        emit({"phase": "start", "id": rid, "host": st["host"], "mode": st["mode"], "isolated": st["isolated"]})

        # A run that prints nothing for minutes would never notice its client
        # left (a dropped connection only shows up on the next write). An SSE
        # comment every 10 s is invisible to the client's parser and makes the
        # write fail promptly, which sets `stop` and kills the remote run.
        def heartbeat():
            while not stop.wait(5):
                try:
                    with wlock:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except OSError:
                    stop.set()
        threading.Thread(target=heartbeat, daemon=True).start()

        def on_line(text):
            lines[0] += 1
            if len(captured) < 4000:
                captured.append(text)
            emit({"line": text})

        try:
            code, killed = agentmod.run_streaming(cmd, cwd, on_line, stop)
        except FileNotFoundError as e:
            emit({"error": "could not launch the harness: %s" % e})
            code, killed = -1, "error"
        except Exception as e:   # noqa: BLE001 - report anything to the client
            emit({"error": str(e)[:300]})
            code, killed = -1, "error"
        emit({"phase": "done", "exit": code, "killed": killed})
        TRACE.log("agent_run", id=rid, goal=goal[:8000], host=st["host"], mode=st["mode"],
                  isolated=st["isolated"], exit=code, killed=killed or False,
                  lines=lines[0], output=captured, ms=int((time.time() - started) * 1000))

    def _plugin_admin(self, body):
        if not isinstance(body, dict):
            self._json({"error": "expected object"}, 400)
            return
        action = body.get("action")
        name = (body.get("name") or "").strip()
        found, warnings = discover_plugins()
        if action in ("enable", "disable"):
            if name not in found:
                self._json({"error": "no such plugin"}, 404)
                return
            _plugin_state().setdefault(name, {})["enabled"] = (action == "enable")
            save_config()
            TRACE.log("plugin", action=action, name=name,
                      contributes=found[name].info(action == "enable"))
            mcp_reload()   # a plugin's MCP servers come or go with it
        elif action != "rescan":
            self._json({"error": "unknown action"}, 400)
            return
        st = _plugin_state()
        self._json({"ok": True, "plugins": [found[n].info(bool(st.get(n, {}).get("enabled")))
                                            for n in sorted(found)], "warnings": warnings})

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
        # who this request belongs to, for the trace — headers, so nothing
        # extra travels upstream
        tid = TRACE.new_id()
        who = {"id": tid, "session": self.headers.get("X-BB-Session"),
               "turn": self.headers.get("X-BB-Turn"),
               "purpose": self.headers.get("X-BB-Purpose") or "chat"}
        started = time.time()

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
                    msg = upstream_message(detail)[:1000]
                    TRACE.log("chat", status=e.code, request=payload, response=None, error=msg,
                              ms=int((time.time() - started) * 1000), **who)
                    self._json({"error": msg}, e.code)
                    return
        except Exception as e:
            TRACE.log("chat", status=502, request=payload, response=None, error=str(e)[:500],
                      ms=int((time.time() - started) * 1000), **who)
            self._json({"error": str(e)}, 502)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-BB-Trace", tid)
        self.close_connection = True
        self.end_headers()
        cap = StreamCapture()
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
                    cap.feed(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                cap.error = cap.error or "client disconnected"  # user hit Stop, or left
            except Exception as e:
                # upstream died mid-stream: without this, the client sees a
                # clean EOF and silently renders a truncated reply as complete
                cap.error = "upstream stream failed: " + str(e)[:200]
                try:
                    msg = json.dumps({"error": cap.error})
                    self.wfile.write(("data: " + msg + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    pass
        finally:
            resp.close()
            TRACE.log("chat", status=200, request=payload, response=cap.result(started),
                      ms=int((time.time() - started) * 1000), **who)


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
