"""A scriptable OpenAI-compatible engine for tests. Stdlib only.

    eng = FakeEngine(models=[{"id": "m1", "max_model_len": 32768}]).start()
    eng.script([{"content": "hello"},
                {"tool_calls": [{"name": "fs__read", "arguments": {"path": "a"}}]},
                {"status": 400, "error": "overflow", "ctx": 32768, "requested": 40000}])
    ... point a client at eng.url (".../v1") ...
    eng.requests  -> every request received: {"path", "headers", "body", "t"}
    eng.stop()

Each POST /v1/chat/completions takes the next scripted reply (or, when the
script is empty, echoes the last user message). A reply can carry:
  content, reasoning           text, streamed in small deltas
  tool_calls                   [{"name", "arguments" (dict or str), "id"?}]
  finish_reason                default "tool_calls" when there are tool calls, else "stop"
  status, error                an HTTP error instead of a completion
  overflow                     {"ctx": N, "requested": M, "prompt"?: P}: vLLM's 400 for a prompt too long
  delay                        seconds before the first byte (time to first token)
  stall                        seconds of silence after the first delta (a buffered tool call)
  chunk                        characters per delta (default 4)
  cut                          close the stream after this many deltas, mid-reply
  usage                        override {"prompt_tokens", "completion_tokens"}
Streaming honours stream_options.include_usage like vLLM does.

Also: GET /v1/models, GET /metrics (vLLM-style), GET /health.
Run standalone for browser tests:  python3 tests/fakes/fake_engine.py --port 18999
"""
import argparse
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _tok(text):
    """A crude, deterministic token count: about four characters a token."""
    return max(1, (len(text or "") + 3) // 4)


class FakeEngine:
    def __init__(self, models=None, host="127.0.0.1", port=0, api_key=None):
        self.models = models or [{"id": "fake-model", "max_model_len": 32768}]
        self.host, self.port, self.api_key = host, port, api_key
        self.requests = []
        self._script = []
        self._lock = threading.Lock()
        self.gen_total = 0
        self.prompt_total = 0
        self.running = 0
        self.srv = None

    # ------------------------------------------------------------ control
    def script(self, replies):
        with self._lock:
            self._script.extend(replies)
        return self

    def clear(self):
        with self._lock:
            self._script = []
            self.requests = []

    @property
    def url(self):
        return "http://%s:%d/v1" % (self.host, self.port)

    def start(self):
        engine = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _auth_ok(self):
                if not engine.api_key:
                    return True
                return self.headers.get("Authorization") == "Bearer " + engine.api_key

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/health"):
                    return self._json(200, {"ok": True})
                if self.path.startswith("/metrics"):
                    body = engine.metrics().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not self._auth_ok():
                    return self._json(401, {"error": {"message": "unauthorized"}})
                if self.path.rstrip("/").endswith("/v1/models"):
                    data = [dict({"object": "model", "owned_by": "vllm"}, **m) for m in engine.models]
                    return self._json(200, {"object": "list", "data": data})
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw.decode() or "{}")
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                with engine._lock:
                    engine.requests.append({"path": self.path, "headers": dict(self.headers.items()),
                                            "body": body, "t": time.time()})
                if not self._auth_ok():
                    return self._json(401, {"error": {"message": "unauthorized"}})
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._json(404, {"error": {"message": "not found"}})
                with engine._lock:
                    reply = engine._script.pop(0) if engine._script else None
                if reply is None:
                    last = ""
                    for m in reversed(body.get("messages") or []):
                        if m.get("role") == "user":
                            c = m.get("content")
                            last = c if isinstance(c, str) else json.dumps(c)
                            break
                    reply = {"content": "echo: " + last}
                if reply.get("delay"):
                    time.sleep(float(reply["delay"]))
                if reply.get("overflow"):
                    o = reply["overflow"]
                    prompt = o.get("prompt", o["requested"] - 1)
                    msg = ("This model's maximum context length is %d tokens. However, you requested %d tokens "
                           "(%d in the messages, %d in the completion). Please reduce the length of the messages "
                           "or completion." % (o["ctx"], o["requested"], prompt, o["requested"] - prompt))
                    return self._json(400, {"object": "error", "message": msg, "type": "BadRequestError",
                                            "code": 400})
                if reply.get("status"):
                    return self._json(int(reply["status"]), {"object": "error",
                                                            "message": reply.get("error") or "error",
                                                            "code": int(reply["status"])})
                if body.get("stream"):
                    return engine._stream(self, body, reply)
                return self._json(200, engine._whole(body, reply))

        self.srv = ThreadingHTTPServer((self.host, self.port), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    # ------------------------------------------------------------ replies
    def _calls(self, reply):
        out = []
        for i, tc in enumerate(reply.get("tool_calls") or []):
            args = tc.get("arguments", {})
            out.append({"id": tc.get("id") or "call_%s" % uuid.uuid4().hex[:12], "type": "function",
                        "index": i, "function": {"name": tc["name"],
                                                 "arguments": args if isinstance(args, str) else json.dumps(args)}})
        return out

    def _usage(self, body, reply):
        prompt = _tok(json.dumps(body.get("messages") or []))
        completion = _tok((reply.get("content") or "") + (reply.get("reasoning") or "")) + 8 * len(reply.get("tool_calls") or [])
        u = {"prompt_tokens": prompt, "completion_tokens": completion}
        u.update(reply.get("usage") or {})
        u["total_tokens"] = u["prompt_tokens"] + u["completion_tokens"]
        with self._lock:
            self.prompt_total += u["prompt_tokens"]
            self.gen_total += u["completion_tokens"]
        return u

    def _whole(self, body, reply):
        calls = self._calls(reply)
        msg = {"role": "assistant", "content": reply.get("content") or ("" if calls else "")}
        if reply.get("reasoning"):
            msg["reasoning_content"] = reply["reasoning"]
        if calls:
            msg["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in calls]
        fin = reply.get("finish_reason") or ("tool_calls" if calls else "stop")
        return {"id": "chatcmpl-" + uuid.uuid4().hex[:16], "object": "chat.completion", "created": int(time.time()),
                "model": body.get("model"), "choices": [{"index": 0, "message": msg, "finish_reason": fin}],
                "usage": self._usage(body, reply)}

    def _stream(self, handler, body, reply):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        cid = "chatcmpl-" + uuid.uuid4().hex[:16]
        model = body.get("model")
        size = int(reply.get("chunk") or 4)
        stalled = [False]
        sent = [0]

        class Cut(Exception):
            pass

        def send(delta=None, finish=None, usage=None, choices=True):
            if reply.get("cut") is not None and sent[0] >= int(reply["cut"]):
                raise Cut()
            sent[0] += 1
            obj = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                   "choices": [] if not choices else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
            if usage is not None:
                obj["usage"] = usage
            handler.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            handler.wfile.flush()
            if reply.get("stall") and not stalled[0]:
                stalled[0] = True
                time.sleep(float(reply["stall"]))
            elif reply.get("pace"):
                time.sleep(float(reply["pace"]))

        with self._lock:
            self.running += 1
        try:
            send({"role": "assistant", "content": ""})
            r = reply.get("reasoning") or ""
            for i in range(0, len(r), size):
                send({"reasoning_content": r[i:i + size]})
            c = reply.get("content") or ""
            for i in range(0, len(c), size):
                send({"content": c[i:i + size]})
            calls = self._calls(reply)
            for c in calls:
                args = c["function"]["arguments"]
                send({"tool_calls": [{"index": c["index"], "id": c["id"], "type": "function",
                                      "function": {"name": c["function"]["name"], "arguments": ""}}]})
                for i in range(0, len(args), size * 3):
                    send({"tool_calls": [{"index": c["index"], "function": {"arguments": args[i:i + size * 3]}}]})
            fin = reply.get("finish_reason") or ("tool_calls" if calls else "stop")
            send({}, finish=fin)
            if (body.get("stream_options") or {}).get("include_usage"):
                send(usage=self._usage(body, reply), choices=False)
            else:
                self._usage(body, reply)
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, Cut):
            pass
        finally:
            with self._lock:
                self.running -= 1

    def metrics(self):
        names = [m["id"] for m in self.models]
        lines = []
        for name in names[:1]:
            lab = '{engine="0",model_name="%s"}' % name
            lines += ["vllm:num_requests_running%s %d" % (lab, self.running),
                      "vllm:num_requests_waiting%s 0" % lab,
                      "vllm:kv_cache_usage_perc%s 0.1" % lab,
                      "vllm:prompt_tokens_total%s %d" % (lab, self.prompt_total),
                      "vllm:generation_tokens_total%s %d" % (lab, self.gen_total)]
        return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=18999)
    ap.add_argument("--model", action="append", default=[])
    ap.add_argument("--ctx", type=int, default=32768)
    ap.add_argument("--script", help="JSON file with a list of replies")
    ap.add_argument("--api-key")
    a = ap.parse_args(argv)
    models = [{"id": m, "max_model_len": a.ctx} for m in (a.model or ["fake-model"])]
    eng = FakeEngine(models=models, port=a.port, api_key=a.api_key).start()
    if a.script:
        with open(a.script) as f:
            eng.script(json.load(f))
    print("fake engine on %s" % eng.url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        eng.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
