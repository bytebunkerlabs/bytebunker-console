"""Requests to the model servers: the one place that knows how to reach a
gateway, survive its quirks and say what went wrong.

The chat proxy (/api/chat), jobs and the server runner all go through here:

    resp, gw = upstream.open_chat(registry, payload)      # UpstreamError(status, message)
    cap = StreamCapture()
    err = upstream.relay(resp, write, cap)                 # bytes to the client as they come

open_chat resolves the gateway that serves the payload's model ("id@gateway"
pins one), turns streaming on with usage, and when a pure-OpenAI server
rejects a vLLM extra (reasoning_effort, top_k, ...) strips them and tries
once more. relay forwards the server-sent events unchanged and notices a
stream that ends without finishing, which a client would otherwise render
as a complete reply.
"""
import json
import urllib.error
import urllib.request

# vLLM extras a pure-OpenAI server refuses; stripped for one retry
RETRY_STRIP = ("reasoning_effort", "top_k", "repetition_penalty", "stream_options")


class UpstreamError(Exception):
    def __init__(self, status, message, gateway=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.gateway = gateway


def message(detail):
    """The sentence inside an upstream error body.

    A proxy chain wraps errors in envelopes: litellm puts vLLM's message
    inside {"error": {"message": ...}} and the console used to wrap that
    again, so the browser ended up rendering 400 characters of escaped JSON.
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


def trace_safe(payload):
    """The request as logged: image data URLs replaced by their size, so a
    screenshot does not become 300 KB of base64 in every trace line."""
    try:
        msgs = payload.get("messages")
        if not isinstance(msgs, list):
            return payload
        out = dict(payload)
        new_msgs = []
        for m in msgs:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, list):
                parts = []
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = str((part.get("image_url") or {}).get("url") or "")
                        if url.startswith("data:"):
                            head = url.split(",", 1)[0]
                            part = {"type": "image_url", "image_url": {"url": "%s,<%d bytes>" % (head, len(url))}}
                    parts.append(part)
                m = dict(m, content=parts)
            new_msgs.append(m)
        out["messages"] = new_msgs
        return out
    except Exception:   # noqa: BLE001 - logging must never break the chat
        return payload


def request(registry, path, payload=None, method="GET", model=None, gateway=None):
    """A request to the gateway that serves `model` (or the payload's model;
    'id@gateway' pins one explicitly). No model: the first enabled gateway.
    Callers that may retry resolve once and pass `gateway` so a pin holds."""
    gw = gateway
    if gw is None:
        want = model or (payload.get("model") if isinstance(payload, dict) else None)
        if want:
            mid, gw = registry.resolve(want)
            if isinstance(payload, dict) and payload.get("model") != mid:
                payload["model"] = mid
        else:
            gws = registry.gateways()
            gw = gws[0] if gws else None
    if gw is None:
        raise RuntimeError("no gateway configured: add one on the Gateways screen")
    url = gw["url"].rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if gw.get("key"):
        headers["Authorization"] = "Bearer " + gw["key"]
    data = json.dumps(payload).encode() if payload is not None else None
    return urllib.request.Request(url, data=data, headers=headers, method=method)


def open_chat(registry, payload, timeout=600):
    """Start a streaming chat completion. The payload is changed in place:
    stream on, usage requested, the model id as its gateway knows it, and
    any extras stripped for the retry. Returns (response, gateway); raises
    UpstreamError with the server's status and its own words."""
    payload["stream"] = True
    payload.setdefault("stream_options", {"include_usage": True})
    mid, gw = registry.resolve(payload.get("model") or "")
    payload["model"] = mid

    def attempt(p):
        return urllib.request.urlopen(request(registry, "/chat/completions", p, "POST", gateway=gw), timeout=timeout)

    try:
        try:
            return attempt(payload), gw
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code == 400 and any(k in detail for k in RETRY_STRIP):
                for k in RETRY_STRIP:
                    payload.pop(k, None)
                return attempt(payload), gw
            raise UpstreamError(e.code, message(detail)[:1000], gw)
    except UpstreamError:
        raise
    except Exception as e:   # noqa: BLE001 - unreachable, refused, the retry's own error
        raise UpstreamError(502, str(e), gw)


def relay(resp, write, cap):
    """Forward the server-sent events as they arrive, feeding cap (a
    traces.StreamCapture) on the way. Returns the error the stream ended
    with, or None. write(bytes) raising BrokenPipeError or
    ConnectionResetError means the client left (Stop, or a closed tab).

    Bytes go out as the socket has them (read1): a readline-per-event loop
    holds each event's terminating blank line hostage until the next event
    arrives, so the stream would render one token late, permanently."""
    try:
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                break
            cap.feed(chunk)
            write(chunk)
        if not cap.error and cap.finish is None and not cap.done:
            # the server closed the stream mid-reply: say so, or the client
            # renders a truncated reply as if it were complete
            cap.error = "the model server closed the stream before the reply finished (%d chunks received)" % cap.chunks
            _write_error(write, cap.error)
    except (BrokenPipeError, ConnectionResetError):
        cap.error = cap.error or "client disconnected"      # Stop, or the tab went away
    except Exception as e:   # noqa: BLE001
        cap.error = "upstream stream failed: " + str(e)[:200]
        _write_error(write, cap.error)
    return cap.error


def _write_error(write, text):
    try:
        write(("data: " + json.dumps({"error": text}) + "\n\n").encode())
    except OSError:
        pass
