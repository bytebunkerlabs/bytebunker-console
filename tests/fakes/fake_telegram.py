"""A stand-in for Telegram's Bot API, for tests. Stdlib only.

    tg = FakeTelegram(token="123456:TEST...").start()
    ... point the app at tg.url (BYTEBUNKER_TELEGRAM_API) ...
    tg.message(42, "what served overnight?", sender="sam")     # someone writes to the bot
    tg.press(42, message_id, "ap:<id>:allow")                 # someone presses a button
    tg.sent        -> every message the bot sent: {"method", "chat_id", "text", ...}
    tg.wait_sent(n) -> True once n were sent

It answers getMe, getUpdates (long polling, at most a second here),
sendMessage, sendDocument, editMessageText, answerCallbackQuery, getFile and
the file download. A wrong token is a 401, as the real one's.

It is strict where the real service is forgiving, so that a client which
drifts from the documentation fails here and not on someone's phone: a
parameter the Bot API does not document for a method is refused (PARAMS, from
core.telegram.org/bots/api at Bot API 10.4), and so are a text over 4096
UTF-16 units, an empty text, button data over 64 bytes, a callback answer
over 200 characters, a chat id that is a number written as a string, and an
edit of a message that was never sent. A second getUpdates while one waits
ends the first with a 409, as the real one does to two programs that share a
bot. tg.flood(n, retry_after) answers the next n sends with a 429.

As a process (python fake_telegram.py --port N), for the browser tests:
POST /_fake/message {chat, text}, POST /_fake/press {chat, message_id, data},
GET /_fake/sent.
"""
import json
import re
import threading
import time
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PARAMS = {
    "getMe": set(),
    "getUpdates": {"offset", "limit", "timeout", "allowed_updates"},
    "sendMessage": {"business_connection_id", "chat_id", "message_thread_id", "direct_messages_topic_id",
                    "ephemeral_message_parameters", "text", "parse_mode", "entities", "link_preview_options",
                    "disable_notification", "protect_content", "allow_paid_broadcast", "message_effect_id",
                    "suggested_post_parameters", "reply_parameters", "reply_markup"},
    "editMessageText": {"business_connection_id", "chat_id", "message_id", "inline_message_id", "text", "parse_mode",
                        "entities", "link_preview_options", "rich_message", "reply_markup"},
    "answerCallbackQuery": {"callback_query_id", "text", "show_alert", "url", "cache_time"},
    "getFile": {"file_id"},
    "sendDocument": {"business_connection_id", "chat_id", "message_thread_id", "direct_messages_topic_id",
                     "ephemeral_message_parameters", "document", "thumbnail", "caption", "parse_mode", "caption_entities",
                     "disable_content_type_detection", "disable_notification", "protect_content", "allow_paid_broadcast",
                     "message_effect_id", "suggested_post_parameters", "reply_parameters", "reply_markup"},
}
TEXT_UNITS = 4096


class QuickHTTPServer(ThreadingHTTPServer):
    """Without the DNS question http.server asks when it binds (socket.getfqdn): half a minute where the resolver is slow."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def units(text):
    return len((text or "").encode("utf-16-le")) // 2


def refusal(method, body, sent_ids, callbacks):
    """Why the real Bot API (or its documentation) would not take this call, or None."""
    named = body["_fields"] if body.get("_multipart") else [k for k in body if not k.startswith("_")]
    extra = sorted(k for k in named if k not in PARAMS.get(method, set()))
    if extra:
        return "Bad Request: %s is not a documented parameter of %s" % (", ".join(extra), method)
    if method in ("sendMessage", "editMessageText", "sendDocument"):
        chat = body.get("chat_id")
        if isinstance(chat, bool) or not isinstance(chat, (int, str)) or (isinstance(chat, str) and not chat.startswith("@")):
            return "Bad Request: chat_id is an integer, or the @name of a public chat"
    if method == "sendDocument":
        if units(body.get("caption") or "") > 1024:
            return "Bad Request: message caption is too long"
        rp = body.get("reply_parameters")
        if rp is not None and not (isinstance(rp, dict) and isinstance(rp.get("message_id"), int)):
            return "Bad Request: can't parse reply parameters"
    if method in ("sendMessage", "editMessageText"):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return "Bad Request: message text is empty"
        if units(text) > TEXT_UNITS:
            return "Bad Request: message is too long"
        lp = body.get("link_preview_options")
        if lp is not None and not (isinstance(lp, dict) and set(lp) <= {"is_disabled", "url", "prefer_small_media",
                                                                        "prefer_large_media", "show_above_text"}):
            return "Bad Request: can't parse link preview options"
        rp = body.get("reply_parameters")
        if rp is not None and not (isinstance(rp, dict) and isinstance(rp.get("message_id"), int)):
            return "Bad Request: can't parse reply parameters"
        for row in ((body.get("reply_markup") or {}).get("inline_keyboard") or []):
            for b in row:
                data = b.get("callback_data")
                if not b.get("text") or not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64:
                    return "Bad Request: BUTTON_DATA_INVALID"
    if method == "editMessageText" and body.get("message_id") not in sent_ids:
        return "Bad Request: message to edit not found"
    if method == "answerCallbackQuery":
        if body.get("callback_query_id") not in callbacks:
            return "Bad Request: query is too old and response timeout expired or query ID is invalid"
        if len(body.get("text") or "") > 200:
            return "Bad Request: MESSAGE_TOO_LONG"
    return None


class FakeTelegram:
    def __init__(self, token="123456:TESTtokenTESTtokenTESTtokenTEST", host="127.0.0.1", port=0):
        self.token, self.host, self.port = token, host, port
        self.refused = []                # (method, why): calls this stand-in would not take
        self._flood = [0, 1]             # sends still to refuse with a 429, and the wait they name
        self._poll = 0                   # the getUpdates that waits now: a newer one ends it
        self.updates = []
        self.sent = []
        self.answers = []                # answerCallbackQuery texts
        self.files = {}                  # file_id -> (path, bytes)
        self.calls = []
        self._next_update = 1000
        self._next_message = 1
        self._lock = threading.Condition()
        self.srv = None

    @property
    def url(self):
        return "http://%s:%d" % (self.host, self.port)

    # ------------------------------------------------------------ scripting
    def message(self, chat, text="", sender="sam", title=None, document=None, photo=None, kind="private"):
        with self._lock:
            self._next_update += 1
            self._next_message += 1
            m = {"message_id": self._next_message, "date": int(time.time()), "text": text,
                 "chat": {"id": chat, "type": kind, "title": title, "first_name": sender if kind == "private" else None},
                 "from": {"id": abs(hash(sender)) % 100000, "username": sender}}
            if document:
                name, data = document
                fid = "file-%d" % len(self.files)
                self.files[fid] = ("documents/" + name, data)
                m["document"] = {"file_id": fid, "file_name": name, "file_size": len(data)}
                m["caption"] = m.pop("text")
            if photo:
                fid = "file-%d" % len(self.files)
                self.files[fid] = ("photos/p.jpg", photo)
                m["photo"] = [{"file_id": fid, "file_size": len(photo)}]
                m["caption"] = m.pop("text")
            self.updates.append({"update_id": self._next_update, "message": m})
            self._lock.notify_all()
            return self._next_message

    def press(self, chat, message_id, data, sender="sam"):
        with self._lock:
            self._next_update += 1
            self.updates.append({"update_id": self._next_update, "callback_query": {
                "id": "cb-%d" % self._next_update, "data": data, "from": {"id": 1, "username": sender},
                "message": {"message_id": message_id, "chat": {"id": chat}}}})
            self._lock.notify_all()

    def flood(self, n=1, retry_after=1):
        """The next n sends are told to slow down (429, parameters.retry_after)."""
        with self._lock:
            self._flood = [n, retry_after]

    def wait_sent(self, n, timeout=10, method=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                rows = [s for s in self.sent if method is None or s["method"] == method]
                if len(rows) >= n:
                    return True
            time.sleep(0.02)
        return False

    def texts(self, chat=None):
        with self._lock:
            return [s.get("text") for s in self.sent if s["method"] in ("sendMessage", "editMessageText")
                    and (chat is None or str(s.get("chat_id")) == str(chat))]

    # --------------------------------------------------------------- server
    def start(self):
        tg = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/_fake/sent":                       # a browser test reads what the bot sent
                    with tg._lock:
                        return self._json(200, {"sent": list(tg.sent), "answers": list(tg.answers)})
                m = re.match(r"^/file/bot([^/]+)/(.+)$", self.path)
                if m and m.group(1) == tg.token:
                    data = next((d for p, d in tg.files.values() if p == m.group(2)), None)
                    if data is None:
                        return self._json(404, {"ok": False, "description": "no such file"})
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._json(404, {"ok": False, "description": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if self.path in ("/_fake/message", "/_fake/press"):  # ... and plays the person in the chat
                    b = json.loads(raw.decode() or "{}")
                    if self.path.endswith("message"):
                        return self._json(200, {"message_id": tg.message(b["chat"], b.get("text") or "", sender=b.get("sender") or "sam")})
                    tg.press(b["chat"], b["message_id"], b["data"], sender=b.get("sender") or "sam")
                    return self._json(200, {"ok": True})
                m = re.match(r"^/bot([^/]+)/(\w+)$", self.path)
                if not m:
                    return self._json(404, {"ok": False, "description": "not found"})
                if m.group(1) != tg.token:
                    return self._json(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                method = m.group(2)
                ctype = self.headers.get("Content-Type") or ""
                if ctype.startswith("multipart/form-data"):
                    body = {"_multipart": True}
                    fn = re.search(rb'filename="([^"]*)"', raw)
                    cid = re.search(rb'name="chat_id"\r\n\r\n([^\r]*)', raw)
                    body["chat_id"] = cid.group(1).decode() if cid else None
                    if re.match(r"^-?\d+$", body["chat_id"] or ""):      # a form's values are all text
                        body["chat_id"] = int(body["chat_id"])
                    body["_fields"] = sorted({f.decode() for f in re.findall(rb'Content-Disposition: form-data; name="([^"]+)"', raw)})
                    cap = re.search(rb'name="caption"\r\n\r\n(.*?)\r\n--', raw, re.S)
                    if cap:
                        body["caption"] = cap.group(1).decode("utf-8", "replace")
                    rp = re.search(rb'name="reply_parameters"\r\n\r\n(.*?)\r\n--', raw, re.S)
                    if rp:
                        try:
                            body["reply_parameters"] = json.loads(rp.group(1).decode("utf-8", "replace"))
                        except ValueError:
                            body["reply_parameters"] = rp.group(1).decode("utf-8", "replace")
                    body["filename"] = fn.group(1).decode() if fn else None
                    body["bytes"] = len(raw)
                    body["content"] = raw.split(b"\r\n\r\n", 2)[-1][:4000].decode("utf-8", "replace") if fn else ""
                else:
                    try:
                        body = json.loads(raw.decode() or "{}")
                    except ValueError:
                        body = {}
                with tg._lock:
                    tg.calls.append((method, body))
                    why = refusal(method, body, {x.get("message_id") for x in tg.sent},
                                  {"cb-%d" % n for n in range(1000, tg._next_update + 1)})
                    if why:
                        tg.refused.append((method, why))
                    elif method in ("sendMessage", "sendDocument", "editMessageText") and tg._flood[0] > 0:
                        tg._flood[0] -= 1
                        return self._json(429, {"ok": False, "error_code": 429,
                                                "description": "Too Many Requests: retry after %s" % tg._flood[1],
                                                "parameters": {"retry_after": tg._flood[1]}})
                if why:
                    return self._json(400, {"ok": False, "error_code": 400, "description": why})
                if method == "getMe":
                    return self._json(200, {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "Rack Bot", "username": "rack_test_bot"}})
                if method == "getUpdates":
                    offset = int(body.get("offset") or 0)
                    deadline = time.time() + min(1.0, float(body.get("timeout") or 0))
                    with tg._lock:
                        tg._poll += 1
                        mine = tg._poll
                        tg._lock.notify_all()
                        while True:
                            if tg._poll != mine:      # two programs on one bot: the older wait ends
                                return self._json(409, {"ok": False, "error_code": 409, "description":
                                                        "Conflict: terminated by other getUpdates request; make sure "
                                                        "that only one bot instance is running"})
                            ready = [u for u in tg.updates if u["update_id"] >= offset]
                            if ready or time.time() >= deadline:
                                break
                            tg._lock.wait(0.05)
                        tg.updates = [u for u in tg.updates if u["update_id"] >= offset]
                    return self._json(200, {"ok": True, "result": ready})
                if method in ("sendMessage", "sendDocument", "editMessageText"):
                    with tg._lock:
                        if method != "editMessageText":
                            tg._next_message += 1
                            mid = tg._next_message
                        else:
                            mid = body.get("message_id")
                        tg.sent.append(dict(body, method=method, message_id=mid))
                    return self._json(200, {"ok": True, "result": {"message_id": mid, "chat": {"id": body.get("chat_id")}}})
                if method == "answerCallbackQuery":
                    with tg._lock:
                        tg.answers.append(body.get("text"))
                    return self._json(200, {"ok": True, "result": True})
                if method == "getFile":
                    f = tg.files.get(body.get("file_id"))
                    if not f:
                        return self._json(400, {"ok": False, "description": "Bad Request: file not found"})
                    return self._json(200, {"ok": True, "result": {"file_id": body["file_id"], "file_path": f[0], "file_size": len(f[1])}})
                self._json(404, {"ok": False, "description": "no such method"})

        self.srv = QuickHTTPServer((self.host, self.port), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18996)
    ap.add_argument("--token", default="123456:TESTtokenTESTtokenTESTtokenTEST")
    a = ap.parse_args()
    FakeTelegram(token=a.token, port=a.port).start()
    while True:
        time.sleep(3600)
