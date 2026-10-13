"""A chat's terminal commands: sandboxed unless the chat has full access; running
one outside the sandbox asks (once, every time in this chat, or no); the model
cannot switch the sandbox off itself."""
import os
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cmdbox  # noqa: E402
import mcp_terminal as term  # noqa: E402

HAS_BOX = cmdbox.kind() in ("seatbelt", "bwrap")


@unittest.skipUnless(HAS_BOX, "no command sandbox on this machine")
class Sandboxed(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp())
        term.STATE["cwd"] = term.STATE["root"] = self.dir
        self.outside = os.path.join(os.path.expanduser("~"), ".bb-sandbox-probe-%d" % os.getpid())

    def tearDown(self):
        if os.path.exists(self.outside):
            os.remove(self.outside)

    def run_(self, cmd, mode="local", ok=False):
        return term.run({"command": cmd, "__bb_mode": mode, "__bb_outside_ok": ok})

    def test_the_folder_is_writable_and_the_rest_of_home_is_not(self):
        text, err = self.run_("echo hi > inside.txt && cat inside.txt")
        self.assertFalse(err, text)
        self.assertIn("sandboxed, no network", text)
        text, err = self.run_("echo x > %s" % self.outside)
        self.assertTrue(err, text)
        self.assertFalse(os.path.exists(self.outside))
        text, err = self.run_("ls %s" % os.path.expanduser("~/.ssh"))
        self.assertTrue(err or "No such file" in text, text)

    def test_no_network_in_local(self):
        probe = ("import socket\ns=socket.socket(); s.settimeout(3)\n"
                 "try:\n s.connect(('1.1.1.1', 53)); print('NET OPEN')\nexcept OSError: print('net blocked')")
        with open(os.path.join(self.dir, "p.py"), "w") as f:
            f.write(probe)
        text, err = self.run_("%s p.py" % sys.executable)
        self.assertIn("net blocked", text)

    def test_full_access_and_an_allowed_command_are_not_sandboxed(self):
        for mode, ok in (("full", False), ("local", True)):
            text, err = self.run_("echo x > %s" % self.outside, mode, ok)
            self.assertFalse(err, text)
            self.assertTrue(os.path.exists(self.outside))
            os.remove(self.outside)

    def test_keys_are_not_in_the_environment(self):
        os.environ["GH_TOKEN"] = "ghp_should_not_leak"
        try:
            text, err = self.run_("echo token=${GH_TOKEN:-none}")
        finally:
            os.environ.pop("GH_TOKEN")
        self.assertIn("token=none", text)


class OutsideAsks(unittest.TestCase):
    """server.terminal_call: the mode, the question, and what the model may not do."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        os.environ.setdefault("BYTEBUNKER_DATA", os.path.join(cls.home, "data"))
        os.environ.setdefault("BYTEBUNKER_CONFIG", os.path.join(cls.home, "config.json"))
        import server
        import sessions
        cls.srv = server
        # a session store of its own, whatever data folder the server module was imported with
        cls.saved = (server.SESSIONS, server.BUS.publish)
        server.SESSIONS = sessions.SessionStore(os.path.join(cls.home, "sessions"))
        server.BUS.publish = lambda *a, **k: None
        server.SESSIONS.put({"id": "s-box", "title": "t", "messages": []})

    @classmethod
    def tearDownClass(cls):
        cls.srv.SESSIONS, cls.srv.BUS.publish = cls.saved

    def call(self, args, access=None, answer="deny"):
        asked = []

        def ask(shown, kind):
            asked.append((shown, kind))
            return answer
        denied, out = self.srv.terminal_call(args, "s-box", access, ask)
        return denied, out, asked

    def test_a_plain_command_runs_in_the_chats_mode_without_asking(self):
        denied, out, asked = self.call({"command": "ls"}, "web")
        if cmdbox.kind():
            self.assertEqual((denied, asked, out["__bb_mode"], out["__bb_outside_ok"]), (None, [], "web", False))

    def test_the_model_cannot_switch_the_sandbox_off(self):
        denied, out, asked = self.call({"command": "ls", "__bb_mode": "full", "__bb_outside_ok": True}, "local")
        if cmdbox.kind():
            self.assertEqual((out["__bb_mode"], out["__bb_outside_ok"]), ("local", False))

    def test_outside_asks_once_always_or_no(self):
        denied, out, asked = self.call({"command": "git push", "outside_sandbox": True}, "local", "deny")
        self.assertTrue(denied and asked and asked[0][1] == "outside")
        denied, out, asked = self.call({"command": "git push", "outside_sandbox": True}, "local", "allow")
        self.assertIsNone(denied)
        self.assertTrue(out["__bb_outside_ok"])
        self.assertEqual(self.srv.SESSIONS.get("s-box").get("max_access"), "full")   # the dot goes red
        denied, out, asked = self.call({"command": "git push", "outside_sandbox": True}, "local", "always")
        denied, out, asked = self.call({"command": "brew install jq", "outside_sandbox": True}, "local", "deny")
        self.assertEqual((denied, asked), (None, []))         # every time in this chat: no more questions

    def test_full_access_never_asks(self):
        denied, out, asked = self.call({"command": "anything", "outside_sandbox": True}, "full")
        self.assertEqual((denied, asked, out["__bb_mode"]), (None, [], "full"))


if __name__ == "__main__":
    unittest.main()
