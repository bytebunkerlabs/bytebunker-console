"""Telegram: one bot, long polling from this computer, no inbound port.

Each chat (yours, or a group you made) is bound to one agent: a message there
is an ask to that agent, and its answer, its questions and its approval
requests come back in the same chat. A chat that is not bound is ignored and
logged; the app lists it so you can bind it (send the bot /start from the
chat you want to use).

    setup   BotFather gives a token; the app keeps it in its vault
            (vault secret "telegram"), never in config.json
    in      text, and files (documents, photos) up to 20 MB
    out     the answer as messages (a long one as a .md file), notes,
            approval requests with Yes and No buttons
    commands /start (who answers here), /new (a fresh conversation; its
            memory stays), /stop (stop what it is doing), /goal and /chat
            in front of an ask force the court or one turn

Only the conversation leaves this computer: an agent's answer (a long one as
a text file), never its files.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import approvals as approvalsmod

API = "https://api.telegram.org"
POLL_S = 25
TEXT_MAX = 3900              # under Telegram's 4096 (UTF-16 units), with room for a part mark
CAPTION_MAX = 1000           # under the 1024 units a file's caption may have
RETRY_MAX_S = 30             # the longest "retry after" a send waits out itself
FILE_MAX = 20 * 1024 * 1024   # the Bot API's download limit


class TelegramError(Exception):
    """code: the HTTP status Telegram answered with, when it answered. retry_after: the seconds it
    asked the bot to wait, when it said the bot is sending too fast."""

    def __init__(self, text, code=None, retry_after=None):
        super().__init__(text)
        self.code = code
        self.retry_after = retry_after


def units(text):
    """A text's length as Telegram counts it: UTF-16 code units (an emoji is two)."""
    return len((text or "").encode("utf-16-le")) // 2


def chat_ref(chat):
    """A chat id as the Bot API types it: an integer, or the @name of a public chat."""
    try:
        return int(chat)
    except (TypeError, ValueError):
        return str(chat)


class Bot:
    """The Bot API, one method per call. base: for tests, a fake server."""

    def __init__(self, token, base=None):
        self.token = token
        self.base = (base or os.environ.get("BYTEBUNKER_TELEGRAM_API") or API).rstrip("/")

    def call(self, method, params=None, timeout=35, files=None):
        url = "%s/bot%s/%s" % (self.base, self.token, method)
        if files:
            boundary = "bb" + uuid.uuid4().hex
            body = b""
            for k, v in (params or {}).items():
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (
                    boundary, k, v if isinstance(v, str) else json.dumps(v))).encode("utf-8")
            for k, (name, data) in files.items():
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                         "Content-Type: application/octet-stream\r\n\r\n" % (boundary, k, name.replace('"', ""))).encode("utf-8")
                body += data + b"\r\n"
            body += ("--%s--\r\n" % boundary).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        else:
            req = urllib.request.Request(url, data=json.dumps(params or {}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read().decode("utf-8", "replace"))
            except ValueError:
                d = {}
            finally:
                e.close()
            d = d if isinstance(d, dict) else {}
            wait = (d.get("parameters") or {}).get("retry_after") if isinstance(d.get("parameters"), dict) else None
            # the token is in the URL: an error names the method, never the URL
            raise TelegramError("%s: %s" % (method, d.get("description") or "HTTP %d" % e.code), code=e.code,
                                retry_after=wait if isinstance(wait, (int, float)) else None)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TelegramError("%s: %s" % (method, getattr(e, "reason", e)))
        if not isinstance(d, dict) or not d.get("ok"):
            raise TelegramError("%s: %s" % (method, (d.get("description") if isinstance(d, dict) else None) or "refused"))
        return d.get("result")

    def send(self, method, params=None, timeout=35, files=None, sleep=time.sleep):
        """A call that sends something. When Telegram says the bot is sending too fast it names
        the seconds to wait: that wait is kept (up to half a minute, twice) before giving up."""
        for attempt in range(3):
            try:
                return self.call(method, params, timeout=timeout, files=files)
            except TelegramError as e:
                if e.code != 429 or attempt == 2 or not e.retry_after or e.retry_after > RETRY_MAX_S:
                    raise
                sleep(float(e.retry_after) + 0.5)

    def download(self, file_id):
        """(name, bytes) of a file someone sent the bot."""
        info = self.call("getFile", {"file_id": file_id})
        if int(info.get("file_size") or 0) > FILE_MAX:
            raise TelegramError("the file is larger than 20 MB")
        path = info.get("file_path") or ""
        url = "%s/file/bot%s/%s" % (self.base, self.token, urllib.parse.quote(path))
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return os.path.basename(path) or "file", r.read(FILE_MAX + 1)
        except (urllib.error.URLError, OSError) as e:
            raise TelegramError("download: %s" % getattr(e, "reason", e))


def split_text(text, limit=TEXT_MAX):
    """A long message in parts, cut at paragraph or line ends where it can be."""
    text = text or ""
    out = []
    while units(text) > limit:
        n = min(len(text), limit)
        while units(text[:n]) > limit:          # characters outside the basic plane count twice
            n -= max(1, (units(text[:n]) - limit + 1) // 2)
        cut = text.rfind("\n\n", 0, n)
        if cut < n // 2:
            cut = text.rfind("\n", 0, n)
        if cut < n // 2:
            cut = text.rfind(" ", 0, n)
        if cut < n // 2:
            cut = n
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text.strip():
        out.append(text)
    return out


class Telegram:
    """deps: store (AgentStore), state (channels.State), bus, approvals, audit,
    token() -> the bot token or None, ask(agent_id, text, source, reply, trust, **extra),
    save_file(agent_id, name, data) -> attachment dict, cancel(agent_id) -> bool,
    new_thread(agent_id, chat), log(text)."""

    kind = "telegram"

    def __init__(self, deps):
        self.d = deps
        self._stop_evt = threading.Event()
        self._kick = threading.Event()  # a token was just connected: the loop need not finish its wait
        self._threads = []
        self.me = None                  # the bot, as getMe names it
        self.error = None
        self._sent = {}                 # approval id -> (chat, message id): its buttons, to settle later
        self._lock = threading.Lock()
        self._bot = None
        self._token = None

    # ------------------------------------------------------------ plumbing
    def bot(self):
        token = self.d.token()
        if not token:
            return None
        if self._bot is None or self._token != token:
            self._bot, self._token, self.me = Bot(token), token, None
            self._same_bot(token)
        return self._bot

    def _same_bot(self, token):
        """Update ids belong to one bot. With another bot's token the kept offset means nothing,
        and would hide that bot's first messages: it starts from what Telegram still holds."""
        bid = str(token).split(":")[0]
        if self.d.state.get("bot") != bid:
            self.d.state.update(lambda d: d.update(bot=bid, offset=0))

    def connected(self, token, me):
        """A token was just accepted (getMe answered): the bot is known from this moment, not
        from the loop's next look, and the loop starts listening at once."""
        with self._lock:
            self._bot, self._token = Bot(token), token
            self.me = {"username": me.get("username"), "name": me.get("first_name")}
            self.error = None
        self._same_bot(token)
        self._kick.set()

    def start(self):
        for name, fn in (("telegram", self._poll_loop), ("telegram-approvals", self._approvals_loop)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop_evt.set()
        self._kick.set()

    def chats(self):
        """{chat id: agent id} from every agent's channels: a chat belongs to one agent."""
        out = {}
        for a in self.d.store.list():
            for c in a.get("channels") or []:
                if c.get("kind") == "telegram" and c.get("chat"):
                    out.setdefault(str(c["chat"]), a["id"])
        return out

    def status(self):
        bound = self.chats()
        seen = self.d.state.get("seen", {})
        return {"connected": bool(self.d.token()), "bot": self.me, "error": self.error,
                "chats": [{"chat": c, "agent": a, "title": (seen.get(c) or {}).get("title") or ""} for c, a in sorted(bound.items())],
                "pending": sorted(({"chat": c, "title": v.get("title") or "", "from": v.get("from") or "", "last": v.get("last"),
                                    "count": v.get("count") or 0, "text": v.get("text") or ""}
                                   for c, v in seen.items() if c not in bound),
                                  key=lambda x: -(x["last"] or 0))[:20]}

    def check(self, token):
        """Whether a token is a bot's: its name, or TelegramError."""
        return Bot(token).call("getMe", timeout=15)

    # ------------------------------------------------------------- inbound
    def _poll_loop(self):
        backoff = 2
        while not self._stop_evt.is_set():
            bot = self.bot()
            if bot is None:
                with self._lock:
                    if not self.d.token():                # still none: there is no bot to name
                        self._bot = self._token = self.me = None
                self.error = None
                self._kick.wait(5)
                self._kick.clear()
                continue
            try:
                if self.me is None:
                    me = bot.call("getMe", timeout=15)
                    self.me = {"username": me.get("username"), "name": me.get("first_name")}
                    self.d.bus.publish("channels", "telegram.connected", {"bot": self.me.get("username")})
                offset = int(self.d.state.get("offset", 0) or 0)
                updates = bot.call("getUpdates", {"offset": offset, "timeout": POLL_S,
                                                  "allowed_updates": ["message", "callback_query"]}, timeout=POLL_S + 15)
                self.error = None
                backoff = 2
                for u in updates or []:
                    try:
                        self.handle(u)
                    except Exception as e:   # noqa: BLE001 - one update's trouble must not stop the bot
                        self.d.log("telegram: update %s: %s" % (u.get("update_id"), e))
                    self.d.state.set("offset", int(u.get("update_id") or 0) + 1)
            except TelegramError as e:
                self.error = str(e)
                self._stop_evt.wait(backoff)
                backoff = min(60, backoff * 2)
            except Exception as e:   # noqa: BLE001
                self.error = "%s: %s" % (type(e).__name__, str(e)[:160])
                self._stop_evt.wait(backoff)
                backoff = min(60, backoff * 2)

    def handle(self, update):
        if update.get("callback_query"):
            return self._callback(update["callback_query"])
        m = update.get("message")
        if not m:
            return
        chat = m.get("chat") or {}
        cid = str(chat.get("id"))
        who = (m.get("from") or {})
        name = who.get("username") or " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or str(who.get("id") or "")
        text = (m.get("text") or m.get("caption") or "").strip()
        agent_id = self.chats().get(cid)
        if agent_id is None:
            self._unknown(cid, chat, name, text)
            return
        agent = self.d.store.get(agent_id)
        if agent is None:
            return
        word = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if word == "/start":
            self._say(cid, "%s answers here. %s\n\nWrite what you want; /new starts a fresh conversation, /stop stops "
                           "what it is doing." % (agent["name"], agent["purpose"]))
            return
        if word == "/new":
            self.d.new_thread(agent_id, cid)
            self._say(cid, "A fresh conversation with %s. Its memory stays." % agent["name"])
            return
        if word == "/stop":
            self._say(cid, "Stopping." if self.d.cancel(agent_id) else "%s is not doing anything right now." % agent["name"])
            return
        atts, notes = [], []
        doc = m.get("document")
        photo = (m.get("photo") or [None])[-1]
        for item, label in ((doc, (doc or {}).get("file_name")), (photo, "photo.jpg")):
            if not item:
                continue
            try:
                fname, data = self.bot().download(item["file_id"])
                atts.append(self.d.save_file(agent_id, label or fname, data))
            except Exception as e:   # noqa: BLE001
                notes.append("(a file came with this and could not be read: %s)" % str(e)[:120])
        if not text and not atts and not notes:
            return                              # a sticker, a join, a pin: nothing to ask
        text = "\n".join([text] + notes).strip() or "(see the attached file)"
        self.d.ask(agent_id, text, source="telegram", trust="owner",
                   reply={"kind": "telegram", "chat": cid, "message_id": m.get("message_id")},
                   attachments=atts or None, dedupe="telegram:%s:%s" % (cid, m.get("message_id")), dedupe_s=86400,
                   **{"from": name})

    def _unknown(self, cid, chat, name, text):
        """A chat nobody bound: ignored, counted, and listed in the app so its owner can bind it."""
        now = time.time()
        first = [False]

        def note(d):
            seen = d.setdefault("seen", {})
            rec = seen.get(cid)
            if rec is None:
                first[0] = True
                rec = seen[cid] = {"count": 0}
                if len(seen) > 50:              # strangers do not grow the file without bound
                    for k in sorted(seen, key=lambda k: seen[k].get("last") or 0)[:len(seen) - 50]:
                        seen.pop(k, None)
            rec.update(title=chat.get("title") or chat.get("username") or chat.get("first_name") or "", last=round(now),
                       count=int(rec.get("count") or 0) + 1, text=text[:80])
            rec["from"] = name
        self.d.state.update(note)
        if first[0]:
            try:
                self.d.audit.record("access", "channels.telegram.ignored", cid, False, who="telegram", sender=name,
                                    why="a chat that is not bound to an agent")
            except Exception:   # noqa: BLE001
                pass
            if text.split()[0:1] == ["/start"]:
                self._say(cid, "This chat is not connected to an agent yet. In the ByteBunker app, open Agents, "
                               "Channels, and bind chat %s to one." % cid)
            try:
                self.d.bus.publish("channels", "telegram.pending", {"chat": cid, "from": name})
            except Exception:   # noqa: BLE001
                pass

    # ------------------------------------------------------------ outbound
    def _say(self, chat, text, **extra):
        bot = self.bot()
        if bot is None:
            raise TelegramError("no bot token: connect Telegram in the app")
        return bot.send("sendMessage", dict({"chat_id": chat_ref(chat), "text": text,
                                             "link_preview_options": {"is_disabled": True}}, **extra))

    def send(self, reply, text, agent=None, wake=None, run=None, note=False, files=None, **kw):
        """An answer (or a note) to a chat. A very long answer goes as a file,
        with its first lines as the message."""
        chat = str(reply.get("chat") or "")
        if not chat:
            raise TelegramError("no chat to answer in")
        if chat not in self.chats():
            raise TelegramError("chat %s is not bound to an agent any more" % chat)
        parts = split_text(text)
        first = {}
        if reply.get("message_id") and not note:
            first = {"reply_parameters": {"message_id": reply["message_id"], "allow_sending_without_reply": True}}
        if len(parts) > 4:
            # one call: the file, with the answer's first lines as its caption. Sent as a message and then a
            # file, a refused file left the message behind, and every later try repeated it
            name = "%s-%s.md" % ((agent or {}).get("id") or "answer", time.strftime("%Y%m%d-%H%M"))
            note, head = "\n\n(the whole answer is in the file)", parts[0]
            while units(head) > CAPTION_MAX - len(note):
                head = head[:max(1, len(head) * 3 // 4)]
            bot = self.bot()
            if bot is None:
                raise TelegramError("no bot token: connect Telegram in the app")
            bot.send("sendDocument", dict({"chat_id": chat_ref(chat), "caption": head.rstrip() + note}, **first),
                     files={"document": (name, text.encode("utf-8"))}, timeout=60)
        else:
            for i, p in enumerate(parts or ["(nothing to say)"]):
                try:
                    self._say(chat, p, **(first if i == 0 else {}))
                except Exception as e:   # noqa: BLE001
                    if i:
                        e.rest = "\n\n".join(parts[i:])     # the parts before this one arrived: a later try sends the rest
                    raise
        for path in files or []:
            with open(path, "rb") as f:
                self.bot().send("sendDocument", {"chat_id": chat_ref(chat)},
                                files={"document": (os.path.basename(path), f.read())}, timeout=120)
        return True

    # ----------------------------------------------------------- approvals
    def _approvals_loop(self):
        """An agent's request that came from a chat is asked in that chat, with two buttons."""
        bus = self.d.bus
        pos = bus.seq
        while not self._stop_evt.is_set():
            if not bus.wait(pos, 5):
                continue
            evts, gap, pos = bus.scan(pos, {"approvals"})
            for e in evts:
                try:
                    if e["type"] == "asked":
                        self._ask(e["data"])
                    elif e["type"] == "answered":
                        self._settle(e["data"])
                except Exception as ex:   # noqa: BLE001
                    self.d.log("telegram: approval %s: %s" % ((e.get("data") or {}).get("id"), ex))

    def _ask(self, ap):
        reply = ap.get("reply") or {}
        if reply.get("kind") != "telegram" or not reply.get("chat") or self.bot() is None:
            return
        chat = str(reply["chat"])
        if chat not in self.chats():
            return
        words = approvalsmod.words(ap)
        if len(words) > 1500:
            # never a yes to something only partly shown
            words = words[:1500] + "\n\n[%d more characters are not shown here: read it all in the app before you answer]" % (len(words) - 1500)
        text = "%s\n\n%s\n\nrun %s. No answer in ten minutes is a no." % (
            ap.get("title") or "%s asks before it runs" % ap.get("tool"), words, ap.get("run"))
        sent = self._say(chat, text, reply_markup={"inline_keyboard": [[
            {"text": "Yes", "callback_data": "ap:%s:allow" % ap["id"]},
            {"text": "No", "callback_data": "ap:%s:deny" % ap["id"]}]]})
        with self._lock:
            self._sent[ap["id"]] = (chat, (sent or {}).get("message_id"), text)

    def _settle(self, ap):
        """The request was answered (here, in the app, in bb, or by silence): the buttons go."""
        with self._lock:
            rec = self._sent.pop(ap.get("id"), None)
        if not rec or self.bot() is None:
            return
        chat, mid, text = rec
        how = {"allow": "Allowed", "always": "Allowed, and from now on", "deny": "Not allowed"}.get(ap.get("decision"), "Not allowed")
        by = ap.get("by") or ""
        who = "no answer in time" if by == "timeout" else "the run was stopped" if by == "cancelled" else \
            "you, here" if by == "telegram:" + chat else "answered in " + (by or "the app")
        try:
            self.bot().send("editMessageText", {"chat_id": chat_ref(chat), "message_id": mid,
                                                "text": "%s\n\n%s (%s)." % (text, how, who),
                                                "link_preview_options": {"is_disabled": True}})
        except TelegramError:
            pass

    def _callback(self, q):
        data = str(q.get("data") or "")
        chat = str(((q.get("message") or {}).get("chat") or {}).get("id"))
        parts = data.split(":")
        answer = "This is not something to answer here."
        if len(parts) == 3 and parts[0] == "ap" and parts[2] in ("allow", "deny"):
            with self._lock:
                rec = self._sent.get(parts[1])
            if rec is None or rec[0] != chat or chat not in self.chats():
                answer = "Already answered, or it was not asked here."
            else:
                done = self.d.approvals.answer(parts[1], parts[2], by="telegram:" + chat)
                answer = ("Allowed." if parts[2] == "allow" else "Not allowed.") if done else "Already answered."
                if done:
                    try:
                        self.d.audit.record("access", "approval." + parts[2], parts[1], True, who="telegram:" + chat,
                                            tool=done.get("tool"), agent=done.get("agent"), run=done.get("run"))
                    except Exception:   # noqa: BLE001
                        pass
        try:
            self.bot().call("answerCallbackQuery", {"callback_query_id": q.get("id"), "text": answer})
        except TelegramError:
            pass
