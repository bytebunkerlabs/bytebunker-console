"""A stand-in IMAP server, for tests. Stdlib only. It speaks the protocol on a
socket, so the app's own client (imaplib) is what gets tested, quoting and all.

    srv = FakeImapServer(user="ops@example.com", password="app-password").start()
    srv.add("INBOX", raw_bytes)            -> the new mail's UID
    srv.boxes["Agents In"] = Box()         # a folder; its name may have spaces or accents
    srv.rebuild("INBOX")                   # the server deals its UIDs again (a new UIDVALIDITY)
    srv.commands                           # every command line received, without its tag
    srv.seen                               # UIDs a client marked as read (a PEEK never does)
    imaplib.IMAP4("127.0.0.1", srv.port)   # plain, for the tests only

What it knows of RFC 3501: CAPABILITY, NOOP, LOGIN, AUTHENTICATE PLAIN, SELECT and EXAMINE (the
name as an atom or a quoted string, in modified UTF-7), UID SEARCH ALL and
UID SEARCH UID a:b, UID FETCH n (BODY[] or BODY.PEEK[], whole or <0.N>),
LOGOUT. Like a real server it answers BAD to a mailbox name that is two words
because it was not quoted, and NO to a folder that does not exist.
"""
import base64
import re
import socketserver
import threading


def utf7_decode(name):
    """A mailbox name from IMAP's modified UTF-7."""
    out, i = [], 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        j = name.index("-", i)
        chunk = name[i + 1:j]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be"))
        i = j + 1
    return "".join(out)


def words(line):
    """A command's arguments: atoms and quoted strings (with \\\\ and \\" inside)."""
    out, i = [], 0
    while i < len(line):
        if line[i] == " ":
            i += 1
        elif line[i] == '"':
            j, buf = i + 1, []
            while j < len(line) and line[j] != '"':
                if line[j] == "\\" and j + 1 < len(line):
                    j += 1
                buf.append(line[j])
                j += 1
            if j >= len(line):
                raise ValueError("a quoted string that never ends")
            out.append("".join(buf))
            i = j + 1
        else:
            j = line.find(" ", i)
            j = len(line) if j < 0 else j
            out.append(line[i:j])
            i = j
    return out


class Box:
    def __init__(self, validity=1700000000):
        self.validity = validity
        self.msgs = {}                    # uid -> raw bytes
        self.next = 1


class FakeImapServer:
    def __init__(self, user="ops@example.com", password="app-password", host="127.0.0.1", port=0):
        self.user, self.password, self.host, self.port = user, password, host, port
        self.boxes = {"INBOX": Box()}
        self.commands = []
        self.seen = set()                 # (folder, uid) a client fetched without PEEK
        self.logins = 0
        self._lock = threading.Lock()
        self.srv = None

    # ------------------------------------------------------------ scripting
    def add(self, folder, raw):
        with self._lock:
            box = self.boxes.setdefault(folder, Box())
            uid = box.next
            box.msgs[uid] = raw
            box.next += 1
            return uid

    def rebuild(self, folder):
        """The server lost its index: the same mails get new UIDs under a new UIDVALIDITY."""
        with self._lock:
            box = self.boxes[folder]
            raws = [box.msgs[u] for u in sorted(box.msgs)]
            box.validity += 1
            box.msgs = {i + 1: r for i, r in enumerate(raws)}
            box.next = len(raws) + 1

    # --------------------------------------------------------------- server
    def start(self):
        outer = self

        class H(socketserver.StreamRequestHandler):
            def send(self, text):
                self.wfile.write(text.encode("utf-8") + b"\r\n")

            def handle(self):
                self.send("* OK IMAP4rev1 a stand-in is ready")
                box = name = None
                authed = False
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    tag, _, rest = line.partition(" ")
                    with outer._lock:
                        outer.commands.append(re.sub(r'^(LOGIN \S+) .*', r"\1 ***", rest))
                    cmd, _, args = rest.partition(" ")
                    cmd = cmd.upper()
                    try:
                        if cmd == "CAPABILITY":
                            self.send("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
                            self.send("%s OK CAPABILITY completed" % tag)
                        elif cmd == "NOOP":
                            self.send("%s OK NOOP completed" % tag)
                        elif cmd == "LOGOUT":
                            self.send("* BYE logging out")
                            self.send("%s OK LOGOUT completed" % tag)
                            return
                        elif cmd == "LOGIN":
                            w = words(args)
                            if len(w) == 2 and w[0] == outer.user and w[1] == outer.password:
                                authed = True
                                with outer._lock:
                                    outer.logins += 1
                                self.send("%s OK LOGIN completed" % tag)
                            else:
                                self.send("%s NO [AUTHENTICATIONFAILED] Authentication failed." % tag)
                        elif cmd == "AUTHENTICATE" and args.strip().upper() == "PLAIN":
                            self.send("+ ")
                            blob = base64.b64decode(self.rfile.readline().strip() or b"")
                            parts = blob.decode("utf-8", "replace").split("\0")
                            if len(parts) == 3 and parts[1] == outer.user and parts[2] == outer.password:
                                authed = True
                                with outer._lock:
                                    outer.logins += 1
                                self.send("%s OK AUTHENTICATE completed" % tag)
                            else:
                                self.send("%s NO [AUTHENTICATIONFAILED] Authentication failed." % tag)
                        elif not authed:
                            self.send("%s BAD log in first" % tag)
                        elif cmd in ("SELECT", "EXAMINE"):
                            w = words(args)
                            if len(w) != 1:
                                self.send("%s BAD Error in IMAP command %s: Extra arguments" % (tag, cmd))
                                continue
                            name = utf7_decode(w[0])
                            with outer._lock:
                                box = outer.boxes.get(name)
                                if box is not None:
                                    info = (len(box.msgs), box.validity, box.next)
                            if box is None:
                                name = None
                                self.send("%s NO Mailbox doesn't exist: %s" % (tag, w[0]))
                                continue
                            self.send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
                            self.send("* %d EXISTS" % info[0])
                            self.send("* 0 RECENT")
                            self.send("* OK [UIDVALIDITY %d] UIDs valid" % info[1])
                            self.send("* OK [UIDNEXT %d] Predicted next UID" % info[2])
                            self.send("%s OK [%s] %s completed" % (tag, "READ-ONLY" if cmd == "EXAMINE" else "READ-WRITE", cmd))
                        elif cmd == "UID" and box is not None:
                            sub, _, a = args.partition(" ")
                            sub = sub.upper()
                            with outer._lock:
                                uids = sorted(box.msgs)
                                if sub == "SEARCH":
                                    m = re.match(r"^UID (\d+):(\*|\d+)$", a.strip(), re.I)
                                    if a.strip().upper() == "ALL":
                                        hit = uids
                                    elif m:
                                        top = uids[-1] if uids else 0
                                        lo = int(m.group(1))
                                        hi = top if m.group(2) == "*" else int(m.group(2))
                                        lo, hi = min(lo, hi), max(lo, hi)     # n:* names the newest mail even past it
                                        hit = [u for u in uids if lo <= u <= hi]
                                    else:
                                        self.send("%s BAD a search this stand-in does not know: %s" % (tag, a))
                                        continue
                                    self.send("* SEARCH" + "".join(" %d" % u for u in hit))
                                    self.send("%s OK SEARCH completed" % tag)
                                elif sub == "FETCH":
                                    m = re.match(r"^(\d+) \(BODY(\.PEEK)?\[\](?:<(\d+)\.(\d+)>)?\)$", a.strip(), re.I)
                                    if not m:
                                        self.send("%s BAD a fetch this stand-in does not know: %s" % (tag, a))
                                        continue
                                    uid = int(m.group(1))
                                    data = box.msgs.get(uid)
                                    if data is not None:
                                        if not m.group(2):
                                            outer.seen.add((name, uid))
                                        part = ""
                                        if m.group(3) is not None:
                                            data = data[int(m.group(3)):int(m.group(3)) + int(m.group(4))]
                                            part = "<%s>" % m.group(3)
                                        self.wfile.write(("* %d FETCH (UID %d BODY[]%s {%d}\r\n" % (
                                            uids.index(uid) + 1, uid, part, len(data))).encode() + data + b")\r\n")
                                    self.send("%s OK FETCH completed" % tag)
                                else:
                                    self.send("%s BAD unknown UID command" % tag)
                        else:
                            self.send("%s BAD unknown command or no mailbox selected: %s" % (tag, cmd))
                    except (ValueError, IndexError) as e:
                        self.send("%s BAD %s" % (tag, e))

        class S(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.srv = S((self.host, self.port), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None
