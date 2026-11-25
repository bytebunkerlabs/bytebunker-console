"""Asking before a tool runs.

Each tool has a policy: allow, ask or deny. It starts from what the tool's
MCP server says about it (read-only tools run; anything that may change
something asks, which is the MCP default for a tool that says nothing), and
config and profiles override it by name or pattern:

    "tool_policy": {"fs__read_*": "allow", "terminal__run": "ask", "web__*": "deny"}

An ask waits for an answer from any client that can give one (the app, or
bb in a terminal); the first answer wins, and no answer in 10 minutes is a
no. A turn whose client cannot answer (bb in a script) fails instead, so a
script never hangs on a question nobody sees.
"""
import fnmatch
import json
import threading
import time
import uuid

POLICIES = ("allow", "ask", "deny")
WAIT_S = 600
META_KEYS = ("agent", "cls", "title", "reply", "wake")


def policy_for(name, annotations, rules=()):
    """The policy for a flat tool name. rules: dicts {pattern: policy},
    most specific last (config, then the profile, then the turn)."""
    decided = None
    for table in rules:
        for pattern, pol in (table or {}).items():
            if pol in POLICIES and fnmatch.fnmatchcase(name, pattern):
                decided = pol            # later tables win; within one, the last match
    if decided:
        return decided
    a = annotations or {}
    if a.get("readOnlyHint"):
        return "allow"
    if a.get("destructiveHint") is False:
        return "allow"                   # says it changes things, but never destroys
    return "ask"


def words(ap):
    """What a request is about, as the person reads it on a phone: an agent's own action in
    words, a command as it is, any other tool by its name and arguments."""
    args = ap.get("args") if isinstance(ap.get("args"), dict) else {}
    tool = str(ap.get("tool") or "")

    def one(key):
        return " ".join(str(args.get(key) or "").split())

    def body(key):
        return str(args.get(key) or "").strip()
    if args.get("command"):
        # the command as it is, and whatever else the call names (the host it runs on, a working folder)
        rest = {k: v for k, v in args.items() if k != "command" and v not in (None, "", [], {})
                and not str(k).startswith("__bb_")}             # (the app's own marks on the call are not the model's)
        return "%s\n%s%s" % (tool, args["command"],
                             "\n" + json.dumps(rest, ensure_ascii=False, indent=1) if rest else "")
    own = {
        # (the name is shown: a fact kept under a name that exists replaces that fact)
        "remember": lambda: "%s\n(%s%s; source: %s)" % (
            body("fact"), one("type") or "project", ", named %s" % one("name") if one("name") else "",
            one("source") or "not named"),
        "forget": lambda: "the fact named %s" % one("name"),
        "send_message": lambda: "%s\n(on %s)" % (body("text"), one("channel") or "its first channel"),
        "mail_reply": lambda: "To: %s\nSubject: %s\n\n%s" % (one("to"), one("subject"), body("text")),
        "rack_switch": lambda: "recipe %s%s" % (one("recipe"), "\n" + one("why") if args.get("why") else ""),
        "ask_agent": lambda: "%s: %s" % (one("agent"), body("text")),
        "hand_to": lambda: "%s: %s" % (one("agent"), body("text")),
        "start_goal": lambda: body("goal"),
        "schedule_job": lambda: "%s, %s\n%s" % (one("name"), one("schedule"), body("prompt")),
    }
    if tool in own:
        return own[tool]()
    return "%s\n%s" % (tool, json.dumps(ap.get("args"), ensure_ascii=False, indent=1))


class Approvals:
    def __init__(self, bus=None):
        self.bus = bus
        self.pending = {}
        self.lock = threading.Lock()

    def ask(self, run, tool, args, session=None, wait_s=WAIT_S, kind="tool", meta=None):
        """Block until someone answers (allow, always, deny), the run is
        cancelled, or wait_s passes. Returns the decision. kind "outside": a
        command asks to run outside the chat's sandbox ("always" then means
        every time in this chat). meta: for an agent's action, who asks
        (agent), the action's class (cls), what it is in a line (title) and
        where the ask came from (reply), so the request shows in that channel."""
        aid = "ap-" + uuid.uuid4().hex[:10]
        rec = {"id": aid, "run": run.id, "session": session, "tool": tool, "args": args, "kind": kind,
               "created": round(time.time(), 3), "decision": None, "by": None, "event": threading.Event()}
        for k in META_KEYS:
            if (meta or {}).get(k) not in (None, ""):
                rec[k] = meta[k]
        with self.lock:
            self.pending[aid] = rec
        view = self.view(rec)
        if self.bus:
            self.bus.publish("approvals", "asked", view)
            self.bus.publish("output", "approval", view, run=run.id)
        deadline = time.time() + wait_s
        try:
            while not rec["event"].wait(0.25):
                if run.cancelled:
                    rec["decision"], rec["by"] = "deny", "cancelled"
                    break
                if time.time() >= deadline:
                    rec["decision"], rec["by"] = "deny", "timeout"
                    break
        finally:
            with self.lock:
                self.pending.pop(aid, None)
            if self.bus:
                done = dict(view, decision=rec["decision"], by=rec["by"])
                self.bus.publish("approvals", "answered", done)
                self.bus.publish("output", "approved" if rec["decision"] in ("allow", "always") else "denied",
                                 done, run=run.id)
        return rec["decision"]

    def answer(self, aid, decision, by="app"):
        """First answer wins. Returns the record's view, or None if it was
        already answered or never asked."""
        if decision not in ("allow", "always", "deny"):
            raise ValueError("decision is allow, always or deny")
        with self.lock:
            rec = self.pending.get(aid)
            if rec is None or rec["decision"] is not None:
                return None
            rec["decision"], rec["by"] = decision, by
        rec["event"].set()
        return self.view(rec)

    def list(self):
        with self.lock:
            return [self.view(r) for r in self.pending.values()]

    @staticmethod
    def view(rec):
        return {k: v for k, v in rec.items() if k != "event"}
