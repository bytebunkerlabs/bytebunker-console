"""Mail: an IMAP poll of one folder, every few minutes, with a sender allowlist.

An agent's wake list names who may wake it by mail:

    {"kind": "mail", "from": ["you@example.com", "*@yourcompany.com"],
     "prompt": "Summarize it and say what needs my attention.",
     "account": "default", "folder": "INBOX", "reply": "channel"}

A mail from a listed sender becomes one wake within a poll. The wake's ask is
your prompt; the mail itself rides along as data from that sender, never as
an instruction (anyone can write "From: you"). A mail from anyone else is
ignored and logged. The mailbox is opened read-only and nothing in it is
marked or moved: what was seen is remembered by its UID (and the mailbox's
UIDVALIDITY: a mailbox that was rebuilt starts again from what is there).
Only what arrived since the last look is asked for, and of a very large mail
only the first 2 MB are read.

Any mailbox that takes a password over IMAP works: Gmail and iCloud with an
app password, Fastmail, Proton through its Bridge, your own server. Mailboxes
that only take OAuth sign-in (Outlook.com, Microsoft 365) do not, yet.

Replies go to the agent's own channel by default. "reply": "thread" answers
the mail instead, over SMTP, and that is an outward action: it asks first
unless the agent's rules allow it, and always when the rule about untrusted
wakes is on.

Accounts live in config.json (channels.mail.accounts); their passwords in
the vault (secret "mail:<account>"), never in config.
"""
import base64
import email
import email.header
import email.message
import email.utils
import imaplib
import re
import smtplib
import ssl
import time

import rules as rulesmod

BODY_MAX = 20000
FETCH_MAX = 2 * 1024 * 1024     # of one mail, the bytes that are read
DELIVER_TRIES = 3       # looks at which a mail that was read is offered to its agent before it is skipped
POLL_S = 180
_TAGS = re.compile(r"<(script|style)[\s\S]*?</\1>|<[^>]+>", re.I)


def imap_utf7(name):
    """A mailbox name as IMAP writes it (RFC 3501, 5.1.3): printable ASCII as it is, & as &-,
    anything else as UTF-16 in a base64 of its own between & and -."""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            del buf[:]
    for ch in name or "":
        if 0x20 <= ord(ch) <= 0x7e:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def mailbox(name):
    """A folder's name as SELECT wants it: quoted (imaplib does not), so a space or a
    bracket in it is part of the name and not the end of it."""
    return '"%s"' % imap_utf7(name).replace("\\", "\\\\").replace('"', '\\"')


def login(c, user, password):
    """Log in to an IMAP connection. imaplib writes LOGIN in ASCII and leaves the name as it
    is: a name with a space is quoted here, and a name or a password outside ASCII goes
    through AUTHENTICATE PLAIN instead, as UTF-8."""
    try:
        user.encode("ascii")
        password.encode("ascii")
    except UnicodeEncodeError:
        c.authenticate("PLAIN", lambda _: b"\0" + user.encode("utf-8") + b"\0" + password.encode("utf-8"))
        return
    if not re.match(r"^[A-Za-z0-9@._+%=/-]+$", user):
        user = '"%s"' % user.replace("\\", "\\\\").replace('"', '\\"')
    c.login(user, password)


def _code(c, name):
    """A number the server gave in a response code of the last SELECT (UIDVALIDITY, UIDNEXT), or None."""
    try:
        typ, data = c.response(name)
        v = data[0] if data else None
        return int(v.decode() if isinstance(v, bytes) else v) if v not in (None, b"", "") else None
    except Exception:   # noqa: BLE001 - a server that does not say is not an error
        return None


def sender_allowed(addr, allow):
    """Whether an address may wake the agent: listed, or under a listed *@domain (or bare domain)."""
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return False
    dom = addr.rsplit("@", 1)[1]
    for a in allow or []:
        a = str(a).strip().lower()
        if a == addr or a == "*@" + dom or a == dom:
            return True
    return False


def decode_header(v):
    out = []
    for part, enc in email.header.decode_header(v or ""):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", "replace"))
            except LookupError:
                out.append(part.decode("utf-8", "replace"))
        else:
            out.append(part)
    return " ".join("".join(out).split())


def body_text(msg):
    """The mail as plain text: its text part, else its HTML with the tags taken out."""
    plain, html = None, None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for p in parts:
        if p.get_content_maintype() == "multipart" or (p.get("Content-Disposition") or "").lower().startswith("attachment"):
            continue
        try:
            raw = p.get_payload(decode=True) or b""
            text = raw.decode(p.get_content_charset() or "utf-8", "replace")
        except (LookupError, AttributeError):
            continue
        if p.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif p.get_content_type() == "text/html" and html is None:
            html = text
    text = plain if plain is not None else re.sub(r"[ \t]+", " ", _TAGS.sub(" ", html or ""))
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    return text[:BODY_MAX] + ("\n[the mail goes on: %d more characters]" % (len(text) - BODY_MAX) if len(text) > BODY_MAX else "")


def parse(raw):
    msg = email.message_from_bytes(raw)
    name, addr = email.utils.parseaddr(msg.get("From") or "")
    files = [decode_header(p.get_filename()) for p in (msg.walk() if msg.is_multipart() else []) if p.get_filename()]
    return {"from": addr.lower(), "name": decode_header(name), "subject": decode_header(msg.get("Subject")),
            "id": (msg.get("Message-ID") or "").strip(), "date": msg.get("Date") or "",
            "references": " ".join(x for x in ((msg.get("References") or "").strip(), (msg.get("Message-ID") or "").strip()) if x),
            "text": body_text(msg), "files": files[:20]}


class Mail:
    """deps: store, state, accounts() -> {name: {host, port, user, folder, ssl, smtp_host, smtp_port, from}},
    password(account) -> str or None, wake(agent_id, source, text, **kw), audit, log,
    imap(account, password) and smtp(account, password): connection factories (tests replace them)."""

    kind = "mail"

    def __init__(self, deps):
        self.d = deps
        self.errors = {}                # account -> the last error, for the app
        self._undelivered = {}          # (mailbox, uid) -> looks at which a mail was read and could not be handed on

    # ------------------------------------------------------------- inbound
    def _imap(self, acct, password):
        fac = getattr(self.d, "imap", None)
        if fac:
            return fac(acct, password)
        port = int(acct.get("port") or (993 if acct.get("ssl", True) else 143))
        if acct.get("ssl", True):
            c = imaplib.IMAP4_SSL(acct["host"], port, ssl_context=ssl.create_default_context(), timeout=30)
        else:
            c = imaplib.IMAP4(acct["host"], port, timeout=30)
        try:
            if not acct.get("ssl", True):
                c.starttls(ssl.create_default_context())     # never a password in the clear
            login(c, acct["user"], password)
        except Exception:
            try:
                c.shutdown()                                  # a refused login leaves no connection behind
            except Exception:   # noqa: BLE001
                pass
            raise
        return c

    def watched(self):
        """[(agent, wake entry, account name, folder)] for every mail wake."""
        out = []
        for a in self.d.store.list():
            if not a.get("enabled", True):
                continue
            for w in a.get("wake") or []:
                if w.get("kind") == "mail" and w.get("enabled") is not False:
                    out.append((a, w, w.get("account") or "default", w.get("folder") or ""))
        return out

    def poll(self, now=None):
        watched = self.watched()
        accounts = self.d.accounts() or {}
        boxes = {}
        for a, w, acct, folder in watched:
            conf = accounts.get(acct)
            if not conf:
                self.errors[acct] = "no mail account named %r: add it under Agents, Channels" % acct
                continue
            boxes.setdefault((acct, folder or conf.get("folder") or "INBOX"), []).append((a, w))
        for (acct, folder), users in boxes.items():
            try:
                self._check(acct, accounts[acct], folder, users)
                self.errors.pop(acct, None)
            except Exception as e:   # noqa: BLE001 - a mailbox that is away is tried again next time
                self.errors[acct] = "%s: %s" % (type(e).__name__, str(e)[:200])
                self.d.log("mail: %s: %s" % (acct, self.errors[acct]))
        return max(30, int((accounts.get("default") or {}).get("poll_s") or POLL_S)) if watched else 60

    def _check(self, acct, conf, folder, users):
        password = self.d.password(acct)
        if not password:
            raise RuntimeError("no password kept for this account")
        c = self._imap(conf, password)
        try:
            typ, data = c.select(mailbox(folder), readonly=True)
            if typ != "OK":
                raise RuntimeError("no folder %r in this mailbox" % folder)
            key = "%s/%s" % (acct, folder)
            cur = (self.d.state.get("boxes", {}) or {}).get(key) or {}
            validity = _code(c, "UIDVALIDITY")
            rebuilt = bool(cur) and validity is not None and cur.get("validity") not in (None, validity)
            if not cur or rebuilt:
                # the first look (or a mailbox whose UIDs were dealt again): what is already
                # there is history, not a wake
                nxt = _code(c, "UIDNEXT")
                if nxt is not None:
                    top = max(0, nxt - 1)
                else:
                    typ, data = c.uid("search", None, "ALL")
                    uids = sorted(int(x) for x in (data[0] or b"").split()) if typ == "OK" else []
                    top = uids[-1] if uids else 0
                if rebuilt:
                    self.d.log("mail: %s: the mailbox was rebuilt (its UIDs are new): starting from what is there now" % key)
                self._cursor(key, top, validity)
                return
            last = int(cur.get("last") or 0)
            # only what came since: "n:*" also names the newest mail when nothing is newer, hence the filter
            typ, data = c.uid("search", None, "UID", "%d:*" % (last + 1))
            uids = sorted(int(x) for x in (data[0] or b"").split()) if typ == "OK" and data else []
            for uid in [u for u in uids if u > last][:50]:
                typ, got = c.uid("fetch", str(uid), "(BODY.PEEK[]<0.%d>)" % FETCH_MAX)
                raw = next((x[1] for x in got or [] if isinstance(x, tuple) and len(x) > 1), None)
                if typ == "OK" and raw:
                    try:
                        m = parse(raw)
                    except Exception as e:   # noqa: BLE001 - a mail anyone can send must not stop the mailbox
                        m = None
                        self.d.log("mail: %s: message %s could not be read and was skipped: %s: %s"
                                   % (key, uid, type(e).__name__, str(e)[:160]))
                    if m is not None:
                        try:
                            self._deliver(m, acct, users)
                            self._undelivered.pop((key, uid), None)
                        except Exception as e:   # noqa: BLE001
                            # it was read and could not be handed on (a full disk, a write that failed): it
                            # stays new and is tried at the next looks, then skipped so the mailbox moves on
                            tries = self._undelivered[(key, uid)] = self._undelivered.get((key, uid), 0) + 1
                            if tries < DELIVER_TRIES:
                                raise
                            self._undelivered.pop((key, uid), None)
                            self.d.log("mail: %s: message %s could not be handed to its agent in %d looks and was skipped: %s: %s"
                                       % (key, uid, tries, type(e).__name__, str(e)[:160]))
                self._cursor(key, uid, validity if validity is not None else cur.get("validity"))
        finally:
            try:
                c.logout()
            except Exception:   # noqa: BLE001
                pass

    def _cursor(self, key, uid, validity=None):
        self.d.state.update(lambda d: d.setdefault("boxes", {}).__setitem__(
            key, {"last": int(uid), "validity": validity, "at": round(time.time())}))

    def _deliver(self, m, acct, users):
        woke = False
        for agent, w in users:
            if not sender_allowed(m["from"], w.get("from")):
                continue
            woke = True
            ask = w.get("prompt") or "A mail arrived for you. Summarize it and say what, if anything, needs your owner's attention."
            data = "From: %s%s\nSubject: %s\nDate: %s\n%s\n%s" % (
                m["from"], " (%s)" % m["name"] if m["name"] else "", m["subject"], m["date"],
                "Attached (not read): %s\n" % ", ".join(m["files"]) if m["files"] else "", m["text"])
            reply = {"kind": "mail", "to": m["from"], "subject": m["subject"], "message_id": m["id"],
                     "references": m["references"], "account": acct} if w.get("reply") == "thread" else None
            self.d.wake(agent["id"], "mail", ask, trust="untrusted", data=data, data_source="a mail from " + (rulesmod.sender(m["from"]) or "an unnamed sender"),
                        reply=reply, dedupe="mail:%s:%s" % (acct, m["id"] or "%s|%s" % (m["from"], m["date"])),
                        title=m["subject"][:120], **{"from": m["from"]})
        if not woke:
            try:
                self.d.audit.record("access", "channels.mail.ignored", m["from"] or "?", False, who="mail", account=acct,
                                    subject=m["subject"][:120], why="the sender is on no agent's allowlist")
            except Exception:   # noqa: BLE001
                pass

    # ------------------------------------------------------------ outbound
    def _smtp(self, conf, password):
        fac = getattr(self.d, "smtp", None)
        if fac:
            return fac(conf, password)
        host = conf.get("smtp_host")
        if not host:
            raise RuntimeError("this account has no smtp_host: replies by mail need one")
        port = int(conf.get("smtp_port") or 465)
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.starttls(context=ssl.create_default_context())
        s.login(conf.get("smtp_user") or conf["user"], password)
        return s

    def send(self, reply, text, agent=None, wake=None, **kw):
        """A reply in the mail's thread (or a new mail to an address the agent's channels name)."""
        acct = reply.get("account") or "default"
        conf = (self.d.accounts() or {}).get(acct)
        if not conf:
            raise RuntimeError("no mail account named %r" % acct)
        password = self.d.password(acct)
        if not password:
            raise RuntimeError("no password kept for the mail account %r" % acct)
        msg = email.message.EmailMessage()
        subject = reply.get("subject") or "From %s" % ((agent or {}).get("name") or "your agent")
        msg["Subject"] = subject if subject.lower().startswith("re:") or not reply.get("message_id") else "Re: " + subject
        msg["From"] = conf.get("from") or conf["user"]
        msg["To"] = reply["to"]
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=(conf.get("from") or conf["user"]).rsplit("@", 1)[-1])
        if reply.get("message_id"):
            msg["In-Reply-To"] = reply["message_id"]
            msg["References"] = reply.get("references") or reply["message_id"]
        msg.set_content(text)
        s = self._smtp(conf, password)
        try:
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:   # noqa: BLE001
                pass
        return True

    def test(self, acct, conf, password):
        """Log in and count what is in the folder: what Add runs before saving."""
        c = self._imap(conf, password)
        try:
            typ, data = c.select(mailbox(conf.get("folder") or "INBOX"), readonly=True)
            if typ != "OK":
                raise RuntimeError("no folder %r in this mailbox" % (conf.get("folder") or "INBOX"))
            return {"ok": True, "messages": int((data[0] or b"0").decode() or 0)}
        finally:
            try:
                c.logout()
            except Exception:   # noqa: BLE001
                pass
