"""The audit log: changes, who made them, whether they worked; a hash chain that
shows a line was edited, removed or moved; no secrets in it."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import audit  # noqa: E402
from test_api import Server  # noqa: E402


class Chain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = audit.AuditLog(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def lines(self):
        with open(self.log.path) as f:
            return f.readlines()

    def test_entries_chain_and_survive_a_restart(self):
        self.log.record("settings", "settings", ok=True, user="me")
        again = audit.AuditLog(self.dir)                      # a restart picks the chain up where it was
        e = again.record("tools", "mcp.add", "pg", True)
        self.assertEqual(e["seq"], 2)
        self.assertEqual(again.verify(), {"ok": True, "entries": 2, "broken_at": None})
        self.assertEqual([x["action"] for x in again.entries()], ["mcp.add", "settings"])   # newest first

    def test_an_edited_removed_or_moved_line_is_found(self):
        for i in range(4):
            self.log.record("data", "session.delete", "s%d" % i, True)
        lines = self.lines()
        edited = lines[:]
        edited[1] = edited[1].replace('"s1"', '"s9"')
        for bad, at in ((edited, 2), (lines[:1] + lines[2:], 3), ([lines[0], lines[2], lines[1], lines[3]], 3)):
            with open(self.log.path, "w") as f:
                f.writelines(bad)
            v = self.log.verify()
            self.assertFalse(v["ok"])
            self.assertEqual(v["broken_at"], at)

    def test_secrets_are_never_written(self):
        self.log.record("tools", "mcp.add", "pg", True, api_key="sk-live-1", env={"PGURL": "postgres://u:pw@h/db"},
                        token="t", needs_key=True, name="pg")
        raw = "".join(self.lines())
        for secret in ("sk-live-1", "pw@h", '"t"'):
            self.assertNotIn(secret, raw)
        d = self.log.entries()[0]["detail"]
        self.assertEqual((d["api_key"], d["env"], d["needs_key"], d["name"]), ("***", {"PGURL": "***"}, True, "pg"))

    def test_filters(self):
        self.log.record("access", "command.outside_sandbox", "s1", True, who="cli", command="brew install jq")
        self.log.record("settings", "settings", ok=True)
        self.assertEqual(len(self.log.entries(category="access")), 1)
        self.assertEqual(len(self.log.entries(q="BREW")), 1)
        self.assertEqual(len(self.log.entries(who="cli")), 1)
        self.assertEqual(len(self.log.entries(before=2)), 1)


class FromTheApi(unittest.TestCase):
    """Requests become audit lines with who made them; chat traffic does not."""

    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp()
        cls.srv = Server(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.data, ignore_errors=True)

    def entries(self, **q):
        qs = "&".join("%s=%s" % kv for kv in q.items())
        code, d = self.srv.request("GET", "/api/audit" + ("?" + qs if qs else ""))
        self.assertEqual(code, 200)
        return d["entries"]

    def test_changes_are_recorded_with_who_and_outcome(self):
        self.srv.request("POST", "/api/settings", {"user": "ops"}, headers={"X-BB-Client": "cli"})
        self.srv.request("POST", "/api/gateways", {"name": "lab", "url": "http://127.0.0.1:9/v1", "key": "sk-secret"})
        self.srv.request("POST", "/api/sessions", {"id": "s-nope", "action": "access", "access": "nonsense"})
        self.srv.request("POST", "/api/usage-event", {"model": "m", "prompt_tokens": 1})          # not a change
        es = {e["action"]: e for e in self.entries()}
        self.assertEqual((es["settings"]["actor"], es["settings"]["ok"], es["settings"]["detail"]), ("cli", True, {"user": "ops"}))
        self.assertEqual(es["gateways.add"]["detail"]["key"], "***")
        self.assertEqual(es["sessions.access"]["category"], "access")
        self.assertNotIn("usage_event", json.dumps(list(es)))
        with open(os.path.join(self.data, "audit", "audit.jsonl")) as f:
            self.assertNotIn("sk-secret", f.read())
        code, v = self.srv.request("GET", "/api/audit/verify")
        self.assertTrue(v["ok"], v)


class WhatCounts(unittest.TestCase):
    def test_reads_and_chat_are_not_changes(self):
        home = tempfile.mkdtemp()
        os.environ.setdefault("BYTEBUNKER_DATA", os.path.join(home, "data"))
        os.environ.setdefault("BYTEBUNKER_CONFIG", os.path.join(home, "config.json"))
        import server
        a = server.audit_action
        self.assertIsNone(a("POST", "/api/rack", {"action": "status", "rack": "spark-1"}, {}))
        self.assertIsNone(a("POST", "/api/sessions", {"id": "s", "messages": []}, {}))
        self.assertIsNone(a("POST", "/api/sessions/s-1/turns", {"content": "hi"}, {}))
        self.assertEqual(a("POST", "/api/rack", {"action": "down", "recipe": "", "rack": "local"}, {})[:3],
                         ("serving", "rack.down", "local"))
        self.assertEqual(a("DELETE", "/api/sessions", None, {"id": ["s-9"]}), ("data", "session.delete", "s-9", {}))
        self.assertEqual(a("POST", "/api/runs/r-1/cancel", {}, {})[:3], ("runs", "run.cancel", "r-1"))


class Commands(unittest.TestCase):
    """Every terminal command outside the sandbox, run or refused, is a line."""

    def test_outside_the_sandbox(self):
        home = tempfile.mkdtemp()
        os.environ.setdefault("BYTEBUNKER_DATA", os.path.join(home, "data"))
        os.environ.setdefault("BYTEBUNKER_CONFIG", os.path.join(home, "config.json"))
        import server
        import sessions
        saved = server.SESSIONS, server.AUDIT, server.BUS.publish
        server.SESSIONS = sessions.SessionStore(os.path.join(home, "sessions"))
        server.AUDIT = audit.AuditLog(home)
        server.BUS.publish = lambda *a, **k: None
        try:
            server.SESSIONS.put({"id": "s-a", "title": "t", "messages": []})
            server.terminal_call({"command": "rm -rf build"}, "s-a", "full", lambda *a: "deny")
            server.terminal_call({"command": "git push", "outside_sandbox": True}, "s-a", "local", lambda *a: "deny")
            server.terminal_call({"command": "ls"}, "s-a", "local", lambda *a: "deny")       # sandboxed: no line
            got = [(e["detail"]["command"], e["ok"], e["detail"]["how"]) for e in server.AUDIT.entries()]
        finally:
            server.SESSIONS, server.AUDIT, server.BUS.publish = saved
        expect = [("git push", False, "not allowed"), ("rm -rf build", True, "full access")]
        import cmdbox
        if cmdbox.kind() is None:      # no sandbox here: even ls asks, and was refused
            expect.insert(0, ("ls", False, "not allowed"))
        self.assertEqual(got, expect)


if __name__ == "__main__":
    unittest.main()
