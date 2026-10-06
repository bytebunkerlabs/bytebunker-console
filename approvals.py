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
import threading
import time
import uuid

POLICIES = ("allow", "ask", "deny")
WAIT_S = 600


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


class Approvals:
    def __init__(self, bus=None):
        self.bus = bus
        self.pending = {}
        self.lock = threading.Lock()

    def ask(self, run, tool, args, session=None, wait_s=WAIT_S):
        """Block until someone answers (allow, always, deny), the run is
        cancelled, or wait_s passes. Returns the decision."""
        aid = "ap-" + uuid.uuid4().hex[:10]
        rec = {"id": aid, "run": run.id, "session": session, "tool": tool, "args": args,
               "created": round(time.time(), 3), "decision": None, "by": None, "event": threading.Event()}
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
