"""bb, the CLI, as a user runs it: a separate process that finds (or
starts) the server for its data folder, streams answers, and whose work
shows up in the app's sessions and runs."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "fakes"))
sys.path.insert(0, ROOT)
from test_api import Server  # noqa: E402
from fake_engine import FakeEngine  # noqa: E402
import commands  # noqa: E402

MODEL = "fake-model"


def bb_env(data):
    env = dict(os.environ, BYTEBUNKER_DATA=data, NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", BB_INTERACTIVE="0")
    env.pop("BYTEBUNKER_HOME", None)
    return env


class BBTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = FakeEngine(models=[{"id": MODEL, "max_model_len": 32768}]).start()
        cls.data = tempfile.mkdtemp()
        cls.srv = Server(cls.data, {"gateways": [{"name": "fake", "url": cls.eng.url, "key": "", "enabled": True}]})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.eng.stop()
        shutil.rmtree(cls.data, ignore_errors=True)

    def bb(self, *args, stdin=None, timeout=60, cwd=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "bb.py")] + list(args), input=stdin,
                              capture_output=True, text=True, timeout=timeout, env=bb_env(self.data), cwd=cwd)

    def test_ask_streams_the_answer_to_stdout(self):
        self.eng.script([{"content": "four", "reasoning": "2+2"}])
        r = self.bb("ask", "-m", MODEL, "what is 2+2?", stdin="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "four\n")
        self.assertIn(MODEL, r.stderr)                         # the meta line
        sess = self.srv.request("GET", "/api/sessions")[1]
        self.assertEqual(sess[0]["source"], "cli")
        self.assertEqual(sess[0]["title"], "what is 2+2?")

    def test_bare_words_are_a_question_and_the_default_model_is_used(self):
        self.eng.script([{"content": "yes"}])
        r = self.bb("is", "this", "on?", stdin="")
        self.assertEqual((r.returncode, r.stdout), (0, "yes\n"), r.stderr)

    def test_json_output(self):
        self.eng.script([{"content": "structured"}])
        r = self.bb("ask", "--json", "-m", MODEL, "hi", stdin="")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual((d["message"]["content"], d["state"]), ("structured", "done"))

    def test_piped_input_is_attached(self):
        self.eng.script([{"content": "a log"}])
        n = len(self.eng.requests)
        r = self.bb("ask", "-m", MODEL, "what is this?", stdin="ERROR disk full on /dev/sda1\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        content = self.eng.requests[n]["body"]["messages"][-1]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        self.assertIn("what is this?", text)
        self.assertIn("ERROR disk full on /dev/sda1", text)

    def test_a_failed_turn_exits_1(self):
        self.eng.script([{"status": 500, "error": "engine fell over"}])
        r = self.bb("ask", "-m", MODEL, "hi", stdin="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("engine fell over", r.stderr)

    def test_unknown_profile_is_a_usage_error(self):
        r = self.bb("ask", "-p", "Nope", "hi", stdin="")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no profile named", r.stderr)

    def test_lists(self):
        for args, want in ((["models"], MODEL), (["gateways"], "fake"), (["runs", "ls"], "run"),
                           (["sessions", "ls"], "session"), (["help"], "bb ask")):
            r = self.bb(*args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertIn(want, r.stdout, args)

    def test_the_chat_keeps_one_session_and_reads_slash_commands(self):
        self.eng.script([{"content": "hello there"}, {"content": "still here"}])
        n = len(self.eng.requests)
        r = self.bb("chat", "-m", MODEL, stdin="/models\nhi\n/effort high\nand again\n/quit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello there", r.stdout)
        self.assertIn("still here", r.stdout)
        self.assertIn(MODEL, r.stderr)                          # /models lists it
        second = self.eng.requests[n + 1]["body"]["messages"]
        self.assertEqual([m["content"] for m in second], ["hi", "hello there", "and again"])   # one session

    def test_the_model_works_in_bbs_folder(self):
        folder = tempfile.mkdtemp()
        try:
            with open(os.path.join(folder, "todo.txt"), "w") as f:
                f.write("buy milk\n")
            self.eng.script([{"tool_calls": [{"name": "ws__read", "arguments": {"path": "todo.txt"}}]},
                             {"tool_calls": [{"name": "ws__write", "arguments": {"path": "done.txt", "content": "milk bought\n"}}]},
                             {"content": "done"}])
            r = self.bb("ask", "-m", MODEL, "--yes", "do the todo", stdin="", cwd=folder)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(folder, "done.txt")) as f:
                self.assertEqual(f.read(), "milk bought\n")
            n = len(self.eng.requests)
            self.assertIn("buy milk", json.dumps(self.eng.requests[n - 2]["body"]["messages"][-1]))   # what ws__read returned
            # nobody to ask, no --yes: the write is not made and bb says why (exit 4)
            self.eng.script([{"tool_calls": [{"name": "ws__write", "arguments": {"path": "nope.txt", "content": "x"}}]},
                             {"content": "never"}])
            r = self.bb("ask", "-m", MODEL, "write a file", stdin="", cwd=folder)
            self.assertEqual(r.returncode, 4, r.stderr)
            self.assertIn("--yes", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(folder, "nope.txt")))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_run_a_workflow(self):
        st, _ = self.srv.request("POST", "/api/workflows", {"action": "save", "name": "greet",
                                                            "workflow": {"prompt": "Say hello to {{who}}"}})
        self.assertEqual(st, 200)
        self.eng.script([{"content": "hello, Ada"}])
        n = len(self.eng.requests)
        r = self.bb("run", "greet", "-p", "who=Ada", stdin="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "hello, Ada\n")
        self.assertEqual(self.eng.requests[n]["body"]["messages"][-1]["content"], "Say hello to Ada")
        r = self.bb("run", "greet", stdin="")
        self.assertEqual(r.returncode, 2)
        self.assertIn("-p who=", r.stderr)
        r = self.bb("workflows")
        self.assertIn("greet", r.stdout)

    def test_doctor(self):
        r = self.bb("doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gateways: 1 of 1 answer", r.stdout)

    @unittest.skipIf(sys.platform == "win32", "SIGINT to a child is a POSIX thing")
    def test_ctrl_c_stops_the_turn_on_the_server(self):
        self.eng.script([{"content": "word " * 300, "chunk": 5, "pace": 0.05}])
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bb.py"), "ask", "-m", MODEL, "tell me a long story"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=bb_env(self.data))
        time.sleep(2.0)
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 3, err)
        runs = self.srv.request("GET", "/api/runs?kind=chat&limit=5")[1]["runs"]
        self.assertEqual(runs[0]["state"], "cancelled")
        self.assertTrue(out.startswith("word word"))
        self.assertLess(len(out), 1400)


class StartsItsOwnServerTest(unittest.TestCase):
    def test_bb_starts_a_server_when_none_runs(self):
        data = tempfile.mkdtemp()
        with open(os.path.join(data, "config.json"), "w") as f:
            json.dump({"gateways": [], "monitors": []}, f)
        env = bb_env(data)
        env["BYTEBUNKER_CONFIG"] = os.path.join(data, "config.json")
        try:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "bb.py"), "models"], capture_output=True,
                               text=True, timeout=60, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no models", r.stdout)
            with open(os.path.join(data, "instance.json")) as f:
                pid = json.load(f)["pid"]
        finally:
            try:
                with open(os.path.join(data, "instance.json")) as f:
                    os.kill(json.load(f)["pid"], signal.SIGTERM)
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
            shutil.rmtree(data, ignore_errors=True)
        self.assertTrue(pid)


class InstallCliTest(unittest.TestCase):
    def test_settings_installs_a_working_command(self):
        data, home = tempfile.mkdtemp(), tempfile.mkdtemp()
        env = {"HOME": home, "USERPROFILE": home, "LOCALAPPDATA": os.path.join(home, "AppData", "Local")}
        srv = Server(data, {}, env=env)
        try:
            st, res = srv.request("POST", "/api/settings", {"action": "install_cli"})
            self.assertEqual(st, 200, res)
            names = [os.path.basename(p) for p in res["written"]]
            self.assertIn("bytebunker" + (".cmd" if os.name == "nt" else ""), names)
            self.assertFalse(res["on_path"])
            self.assertTrue(res["hint"])
            cmd = [p for p in res["written"] if os.path.basename(p).startswith("bytebunker")][0]
            r = subprocess.run([cmd, "--version"] if os.name != "nt" else ["cmd", "/c", cmd, "--version"],
                               capture_output=True, text=True, timeout=30)
            self.assertIn("ByteBunker", r.stdout + r.stderr)
        finally:
            srv.stop()
            shutil.rmtree(data, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)


class CommandTableTest(unittest.TestCase):
    def test_every_command_has_its_handler_and_route(self):
        import bb
        with open(os.path.join(ROOT, "server.py"), encoding="utf-8") as f:
            src = f.read()
        for c in commands.cli_commands():
            self.assertIn(c["cli"].split()[0], bb.HANDLERS, c)
        with open(os.path.join(ROOT, "bb.py"), encoding="utf-8") as f:
            bb_src = f.read()
        for c in commands.slash_commands():
            self.assertIn('word == "%s"' % c["slash"], bb_src, c)
        for route in commands.routes():
            method, path = route.split(" ", 1)
            stem = path.split("<id>")[0].rstrip("/")
            self.assertIn(stem, src, route)


if __name__ == "__main__":
    unittest.main()
