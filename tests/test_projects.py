"""Projects: related work kept together. A project's folder is made when
missing; every chat in a project follows its instructions and default
profile; bb joins the project whose folder it runs in; chats, workflows and
runs carry its id; and a chat opens in a terminal in the project's folder."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "fakes"))
from test_api import Server  # noqa: E402
from test_bb import MODEL, bb_env  # noqa: E402
from fake_engine import FakeEngine  # noqa: E402


class Projects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = FakeEngine(models=[{"id": MODEL, "max_model_len": 32768}]).start()
        cls.tmp = tempfile.mkdtemp()
        cls.data = os.path.join(cls.tmp, "data")
        os.makedirs(cls.data)
        cls.srv = Server(cls.data, {"gateways": [{"name": "fake", "url": cls.eng.url, "key": "", "enabled": True}]})
        cls.folder = os.path.join(cls.tmp, "code", "billing")          # not there yet
        st, r = cls.srv.request("POST", "/api/projects", {"action": "save", "project": {
            "name": "Billing", "folder": cls.folder, "instructions": "Money is integer cents.", "profile": "Fast"}})
        assert st == 200, r
        cls.pid = r["id"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.eng.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def turn(self, body, sid="new"):
        st, r = self.srv.request("POST", "/api/sessions/%s/turns" % sid, dict({"model": MODEL}, **body))
        self.assertEqual(st, 202, r)
        self.srv.sse("/api/runs/%s/events" % r["run"], timeout=20,
                     until=lambda fr: json.loads(fr[-1]["data"])["type"] == "finished")
        return r["session"]

    def bb(self, *args, cwd=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "bb.py")] + list(args), input="",
                              capture_output=True, text=True, timeout=60, env=bb_env(self.data), cwd=cwd)

    def project(self):
        return next(p for p in self.srv.request("GET", "/api/projects")[1]["projects"] if p["id"] == self.pid)

    def test_saving_makes_the_folder_and_refuses_what_cannot_work(self):
        self.assertTrue(os.path.isdir(self.folder))
        for proj, why in (({"name": "billing"}, "already a project named"),
                          ({"name": "X", "folder": "relative/path"}, "full path"),
                          ({"name": "Y", "profile": "Nope"}, "no profile named"),
                          ({"name": ""}, "needs a name")):
            st, r = self.srv.request("POST", "/api/projects", {"action": "save", "project": proj})
            self.assertEqual(st, 400, proj)
            self.assertIn(why, r["error"])

    def test_a_chat_in_a_project_follows_its_instructions_and_profile(self):
        self.eng.script([{"content": "ok"}])
        sid = self.turn({"text": "hi", "project": "Billing"})             # by name
        body = self.eng.requests[-1]["body"]
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("# Project: Billing\n\nMoney is integer cents.", body["messages"][0]["content"])
        self.assertEqual(body["max_tokens"], 4096)                        # the project's profile: Fast
        self.assertEqual(self.srv.request("GET", "/api/sessions?id=" + sid)[1]["project"], self.pid)

    def test_bb_in_the_project_folder_joins_it(self):
        sub = os.path.join(self.folder, "src")
        os.makedirs(sub, exist_ok=True)
        self.eng.script([{"content": "fine"}])
        r = self.bb("ask", "-m", MODEL, "--no-workspace", "check the totals", cwd=sub)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Money is integer cents.", self.eng.requests[-1]["body"]["messages"][0]["content"])
        mine = self.srv.request("GET", "/api/sessions?project=" + self.pid)[1]
        self.assertIn("check the totals", [s["title"] for s in mine])
        r = self.bb("projects", cwd=sub)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("* Billing", r.stdout)
        r = self.bb("ask", "-P", "Nope", "x", cwd=sub)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no project named 'Nope'", r.stderr)

    def test_bb_with_a_project_and_no_command(self):
        r = self.bb("-P", "Billing", cwd=self.tmp)                         # a chat in the project
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("project Billing", r.stderr)
        self.eng.script([{"content": "noted"}])
        r = self.bb("-P", "Billing", "-m", MODEL, "--no-workspace", "what is owed?", cwd=self.tmp)   # a question
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("noted", r.stdout)
        self.assertIn("Money is integer cents.", self.eng.requests[-1]["body"]["messages"][0]["content"])

    def test_sessions_by_project_and_moving_one(self):
        self.eng.script([{"content": "a"}])
        loose = self.turn({"text": "no project here"})
        self.assertIn(loose, [s["id"] for s in self.srv.request("GET", "/api/sessions?project=none")[1]])
        st, r = self.srv.request("POST", "/api/sessions", {"action": "set_project", "id": loose, "project": self.pid})
        self.assertEqual(st, 200, r)
        self.assertIn(loose, [s["id"] for s in self.srv.request("GET", "/api/sessions?project=" + self.pid)[1]])
        self.assertGreaterEqual(self.project()["chats"], 1)

    def test_a_workflow_in_a_project(self):
        st, r = self.srv.request("POST", "/api/workflows", {"action": "save", "name": "summarize", "workflow": {
            "kind": "chat", "prompt": "Summarize.", "project": self.pid}})
        self.assertEqual(st, 200, r)
        self.assertIn("summarize", self.project()["workflows"])
        self.eng.script([{"content": "done"}])
        st, r = self.srv.request("POST", "/api/workflows/summarize/run", {})
        self.assertEqual(st, 202, r)
        self.srv.sse("/api/runs/%s/events" % r["run"], timeout=20,
                     until=lambda fr: json.loads(fr[-1]["data"])["type"] == "finished")
        self.assertEqual(self.srv.request("GET", "/api/sessions?id=" + r["session"])[1]["project"], self.pid)

    def test_the_terminal_command_where_no_window_can_open(self):
        self.eng.script([{"content": "a"}])
        sid = self.turn({"text": "for the terminal", "project": self.pid})
        where = shlex.quote(os.path.realpath(self.folder))
        then = "; " if sys.platform == "win32" else " && "                # Windows PowerShell 5 has no &&
        st, r = self.srv.request("POST", "/api/terminal", {"session": sid})
        self.assertEqual((st, r["opened"]), (200, False))                 # a server, not the desktop app
        self.assertEqual(r["command"], "cd %s%sbb chat %s" % (where, then, sid))
        st, r = self.srv.request("POST", "/api/terminal", {"project": "Billing"})
        self.assertEqual(r["command"], "cd %s%sbb chat -P %s" % (where, then, self.pid))
        # a chat bb had in a subfolder reopens there
        sub = os.path.join(self.folder, "api")
        os.makedirs(sub, exist_ok=True)
        self.eng.script([{"content": "b"}])
        r = self.bb("ask", "-m", MODEL, "--no-workspace", "in the api folder", cwd=sub)
        self.assertEqual(r.returncode, 0, r.stderr)
        there = next(s for s in self.srv.request("GET", "/api/sessions?project=" + self.pid)[1]
                     if s["title"] == "in the api folder")
        st, r = self.srv.request("POST", "/api/terminal", {"session": there["id"]})
        self.assertEqual(r["folder"], os.path.realpath(sub))


# What the desktop app starts, with Popen replaced: nothing opens.
LAUNCH = r"""
import json, os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import server
calls = []
class Popen:
    def __init__(self, args, **kw):
        calls.append({"args": args, "flags": kw.get("creationflags")})
subprocess.Popen = Popen
sys.platform = sys.argv[2]
os.environ["BYTEBUNKER_DESKTOP"] = "1"
res = server.open_terminal(sys.argv[3], sys.argv[4] or None, sys.argv[5] or None)
script = None
if calls and calls[0]["args"][0] == "open":
    with open(calls[0]["args"][1], encoding="utf-8") as f:
        script = f.read()
print(json.dumps({"res": res, "calls": calls, "script": script}))
"""


class OpenTerminal(unittest.TestCase):
    def launch(self, platform, session="", project=""):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        folder = os.path.join(tmp, "work", "new")                         # not there yet
        env = dict(os.environ, BYTEBUNKER_DATA=os.path.join(tmp, "data"),
                   BYTEBUNKER_CONFIG=os.path.join(tmp, "config.json"))
        env.pop("BYTEBUNKER_DESKTOP", None)
        r = subprocess.run([sys.executable, "-c", LAUNCH, ROOT, platform, folder, session, project],
                           capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(os.path.isdir(folder))                            # made for it
        self.assertTrue(out["res"]["opened"])
        return out, os.path.realpath(folder), tmp

    def test_mac_opens_a_command_file_in_the_terminal_app(self):
        out, folder, tmp = self.launch("darwin", session="s-abc123")
        (call,) = out["calls"]
        self.assertEqual(call["args"][0], "open")
        self.assertEqual(os.path.basename(call["args"][1]), "bb-s-abc123.command")
        script = out["script"]
        self.assertIn("cd %s || exit 1" % shlex.quote(folder), script)
        self.assertIn("BYTEBUNKER_CONFIG=%s" % shlex.quote(os.path.join(tmp, "config.json")), script)
        self.assertIn(" chat s-abc123\n", script)

    def test_windows_opens_powershell_in_its_own_window(self):
        out, folder, tmp = self.launch("win32", project="p-1234")
        (call,) = out["calls"]
        self.assertEqual(call["args"][:3], ["powershell", "-NoExit", "-Command"])
        self.assertEqual(call["flags"], 0x10)                             # CREATE_NEW_CONSOLE
        ps = call["args"][3]
        self.assertIn("Set-Location -LiteralPath '%s'" % folder, ps)
        self.assertTrue(ps.endswith("'chat' '-P' 'p-1234'"), ps)


if __name__ == "__main__":
    unittest.main()
