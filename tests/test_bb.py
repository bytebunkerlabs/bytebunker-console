"""bb, the CLI, as a user runs it: a separate process that finds (or
starts) the server for its data folder, streams answers, and whose work
shows up in the app's sessions and runs."""
import json
import os
import re
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

    def test_a_title_with_newlines_stays_on_one_row(self):
        self.srv.request("POST", "/api/sessions", {"id": "s-multiline", "title": "#!/bin/sh\nset -e\necho hi",
                                                   "updated": int(time.time() * 1000), "messages": []})
        r = self.bb("sessions", "ls")
        row = [l for l in r.stdout.splitlines() if l.startswith("s-multiline")]
        self.assertEqual(len(row), 1)
        self.assertIn("#!/bin/sh set -e echo hi", row[0])

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
        # a suite started in the background of a script inherits "ignore Ctrl-C", and so would bb: here it must not
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bb.py"), "ask", "-m", MODEL, "tell me a long story"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=bb_env(self.data), preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
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


class IdleExitTest(unittest.TestCase):
    def start(self, data):
        with open(os.path.join(data, "config.json"), "w") as f:
            json.dump({"gateways": [], "monitors": []}, f)
        env = dict(os.environ, BYTEBUNKER_DATA=data, BYTEBUNKER_CONFIG=os.path.join(data, "config.json"))
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--port", "0", "--idle-exit", "4"],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(os.path.join(data, "instance.json")):
            time.sleep(0.1)
        with open(os.path.join(data, "instance.json")) as f:
            return p, json.load(f)["port"]

    def test_a_server_bb_started_leaves_when_idle(self):
        data = tempfile.mkdtemp()
        p, _ = self.start(data)
        try:
            p.wait(30)
            self.assertFalse(os.path.exists(os.path.join(data, "instance.json")))
        finally:
            if p.poll() is None:
                p.kill()
            shutil.rmtree(data, ignore_errors=True)

    def test_an_open_app_window_keeps_it(self):
        import http.client
        data = tempfile.mkdtemp()
        p, port = self.start(data)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            with open(os.path.join(data, "instance.json")) as f:
                token = json.load(f)["token"]
            c.request("GET", "/api/events", headers={"Host": "127.0.0.1:%d" % port, "X-BB-Token": token})
            c.getresponse()
            time.sleep(9)
            self.assertIsNone(p.poll())                      # a window is listening
        finally:
            c.close()
            p.kill()
            p.wait(10)
            shutil.rmtree(data, ignore_errors=True)


class AgentsCliTest(unittest.TestCase):
    """bb agents: make one, ask it, read what it remembers, change its rules."""

    @classmethod
    def setUpClass(cls):
        cls.eng = FakeEngine(models=[{"id": "glm-5.3-flash", "max_model_len": 131072}]).start()
        cls.data = tempfile.mkdtemp()
        cls.srv = Server(cls.data, {"gateways": [{"name": "fake", "url": cls.eng.url, "key": "", "enabled": True}]})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.eng.stop()
        shutil.rmtree(cls.data, ignore_errors=True)

    def bb(self, *args, stdin="", timeout=60):
        return subprocess.run([sys.executable, os.path.join(ROOT, "bb.py")] + list(args), input=stdin,
                              capture_output=True, text=True, timeout=timeout, env=bb_env(self.data))

    def test_1_new_ls_and_show(self):
        r = self.bb("agents", "ls")
        self.assertIn("no agents yet", r.stdout)
        r = self.bb("agents", "new", "ops", "--purpose", "Keeps the rack serving.", "--name", "Ops",
                    "--cron", "0 7 * * 1-5", "--prompt", "Morning report.")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("made ops (Ops)", r.stderr)
        r = self.bb("agents", "new", "ops", "--purpose", "again")
        self.assertEqual(r.returncode, 2)
        self.assertIn("there is already an agent 'ops'", r.stderr)
        r = self.bb("agents", "ls")
        self.assertIn("ops", r.stdout.splitlines()[1])
        self.assertIn("Keeps the rack serving.", r.stdout)
        r = self.bb("agents", "show", "ops")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("master: glm-5.3-flash (the only live model that fits", r.stdout)
        self.assertIn("outward=ask", r.stdout)
        self.assertIn("wakes on cron 0 7 * * 1-5: Morning report.", r.stdout)
        self.assertEqual(json.loads(self.bb("agents", "show", "ops", "--json").stdout)["agent"]["id"], "ops")
        self.assertEqual(self.bb("agents", "show", "nobody").returncode, 2)

    def test_2_ask_prints_the_answer_and_memory_is_kept(self):
        self.eng.script([{"tool_calls": [{"name": "remember", "arguments": {
            "fact": "the owner asked about disks on monday", "type": "project", "source": "the owner said", "name": "asked-disks"}}]},
            {"content": "Both disks are fine."}])
        r = self.bb("agents", "ask", "ops", "how are the disks?")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "Both disks are fine.\n")
        self.assertIn("remember", r.stderr)                         # the tool call, as it happened
        r = self.bb("agents", "memory", "ops")
        self.assertIn("asked-disks", r.stdout)
        self.assertIn("the owner asked about disks on monday", r.stdout)
        r = self.bb("agents", "memory", "ops", "add", "reports go out before 07:30", "--type", "feedback", "--name", "report-time")
        self.assertIn("remembered as report-time", r.stderr)
        self.assertIn("report-time", self.bb("agents", "memory", "ops").stdout)
        self.assertIn("forgot asked-disks", self.bb("agents", "memory", "ops", "forget", "asked-disks").stderr)
        self.assertNotIn("asked-disks", self.bb("agents", "memory", "ops").stdout)
        self.assertEqual(self.bb("agents", "memory", "ops", "forget", "asked-disks").returncode, 2)
        r = self.bb("agents", "inbox", "ops")
        self.assertIn("how are the disks?", r.stdout)
        self.assertIn("done", r.stdout)

    def test_3_piped_input_is_the_ask_and_an_ask_to_nobody_goes_to_the_concierge(self):
        self.eng.script([{"content": "Summarized."}])
        r = self.bb("agents", "ask", "ops", "summarize this", stdin="line one\nline two\n")
        self.assertEqual(r.stdout, "Summarized.\n")
        sent = [q["body"] for q in self.eng.requests if q["path"].endswith("/chat/completions")][-1]
        self.assertIn("summarize this\n\nline one\nline two", sent["messages"][-1]["content"])
        self.eng.script([{"content": "It is Monday."}])
        r = self.bb("agents", "ask", "what day is it?")
        self.assertEqual((r.returncode, r.stdout), (0, "It is Monday.\n"))
        self.assertIn("concierge", self.bb("agents", "ls").stdout)
        # an option between the words: bb agents ask ops --chat "…"
        self.eng.script([{"content": "In one turn."}])
        r = self.bb("agents", "ask", "ops", "--chat", "answer in one turn")
        self.assertEqual((r.returncode, r.stdout), (0, "In one turn.\n"), r.stderr)

    @unittest.skipIf(os.name == "nt", "a socket as standard input is a POSIX thing")
    def test_3a_standard_input_that_never_ends_does_not_hold_an_ask(self):
        """A scheduler or another program can start bb with a socket as its standard input that stays
        open and says nothing. The ask goes ahead with its words; what did arrive rides along."""
        import socket
        for sent, want in ((b"", "is anyone there?"), (b"a line that arrived\n", "is anyone there?\n\na line that arrived")):
            ours, theirs = socket.socketpair()
            try:
                if sent:
                    ours.sendall(sent)
                self.eng.script([{"content": "Here."}])
                t0 = time.time()
                r = subprocess.run([sys.executable, os.path.join(ROOT, "bb.py"), "agents", "ask", "ops", "--chat", "is anyone there?"],
                                   stdin=theirs.fileno(), capture_output=True, text=True, timeout=60, env=bb_env(self.data))
                self.assertEqual((r.returncode, r.stdout), (0, "Here.\n"), r.stderr)
                self.assertLess(time.time() - t0, 30)
                body = [q["body"] for q in self.eng.requests if q["path"].endswith("/chat/completions")][-1]
                # (an agent's ask carries its date and time on its first line)
                self.assertEqual(re.sub(r"^\[[^\]\n]* local time\]\n", "", body["messages"][-1]["content"]), want)
            finally:
                ours.close()
                theirs.close()
        # bb ask, the same
        ours, theirs = socket.socketpair()
        try:
            self.eng.script([{"content": "Yes."}])
            r = subprocess.run([sys.executable, os.path.join(ROOT, "bb.py"), "ask", "--no-tools", "still there?"],
                               stdin=theirs.fileno(), capture_output=True, text=True, timeout=60, env=bb_env(self.data))
            self.assertEqual((r.returncode, r.stdout), (0, "Yes.\n"), r.stderr)
        finally:
            ours.close()
            theirs.close()

    def test_3c_an_ask_that_fails_says_why_once(self):
        self.eng.script([{"status": 500, "error": "the engine fell over"}] * 6)
        r = self.bb("agents", "ask", "ops", "--chat", "will this work?")
        self.assertEqual((r.returncode, r.stdout), (1, ""), r.stderr)
        self.assertEqual(r.stderr.count("the engine fell over"), 1, r.stderr)       # said, and not twice
        self.eng.clear()

    def test_3b_options_go_anywhere_among_an_agents_commands_words(self):
        sys.path.insert(0, os.path.join(HERE, ".."))
        import bb
        p = bb.parser()
        first = lambda *argv: bb.options_first(p, list(argv))
        self.assertEqual(first("agents", "ask", "ops", "--chat", "hello there"), ["agents", "--chat", "ask", "ops", "hello there"])
        self.assertEqual(first("agents", "inbox", "ops", "-n", "5"), ["agents", "-n", "5", "inbox", "ops"])
        self.assertEqual(first("agents", "memory", "ops", "add", "--type=feedback", "be brief", "--name", "brief"),
                         ["agents", "--type=feedback", "--name", "brief", "memory", "ops", "add", "be brief"])
        self.assertEqual(first("agents", "new", "ops", "--pur", "Keeps the rack serving."),
                         ["agents", "--pur", "Keeps the rack serving.", "new", "ops"])
        # what argparse reads as a word stays where it is: a dash with a space, a number, anything after --
        self.assertEqual(first("agents", "ask", "ops", "-v means verbose?"), ["agents", "ask", "ops", "-v means verbose?"])
        self.assertEqual(first("agents", "ask", "ops", "-5"), ["agents", "ask", "ops", "-5"])
        self.assertEqual(first("agents", "ask", "ops", "--", "--chat"), ["agents", "ask", "ops", "--", "--chat"])
        self.assertEqual(first("agents", "ask", "ops", "--nope", "x"), ["agents", "ask", "ops", "--nope", "x"])   # argparse says so
        a = p.parse_args(first("agents", "ask", "ops", "--goal", "research this"))
        self.assertEqual((a.goal, a.shape), (["ask", "ops", "research this"], "goal"))

    def test_4_rules_and_budget_are_shown_and_set(self):
        r = self.bb("agents", "rules", "ops")
        self.assertRegex(r.stdout, r"outward\s+ask\s+a message to any channel")
        self.assertRegex(r.stdout, r"spend\s+deny")
        r = self.bb("agents", "rules", "ops", "rack=deny", "egress=+developer.nvidia.com,docs.vllm.ai", "runs_day=20", "quiet=22:00-06:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("developer.nvidia.com, docs.vllm.ai", r.stdout)
        self.assertIn("20 runs a day", r.stdout)
        self.assertIn("quiet 22:00-06:00", r.stdout)
        agent = json.loads(self.bb("agents", "show", "ops", "--json").stdout)["agent"]
        self.assertEqual((agent["rules"]["rack"], agent["budget"]["runs_day"]), ("deny", 20))
        r = self.bb("agents", "rules", "ops", "egress=-docs.vllm.ai", "rack=sometimes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("rules.rack is allow, ask or deny", r.stderr)
        # the list is all its court reaches; any is a * the shell does not expand; none empties it
        self.assertIn("egress  developer.nvidia.com, docs.vllm.ai (and nothing else)", self.bb("agents", "rules", "ops").stdout)
        r = self.bb("agents", "rules", "ops", "egress=any")
        self.assertIn("egress  any public site (your own network is refused)", r.stdout)
        self.assertEqual(json.loads(self.bb("agents", "show", "ops", "--json").stdout)["agent"]["rules"]["egress"],
                         ["developer.nvidia.com", "docs.vllm.ai", "*"])
        self.assertIn("egress  none (nothing outside is reachable)", self.bb("agents", "rules", "ops", "egress=none").stdout)
        self.assertIn("egress  docs.vllm.ai (and nothing else)", self.bb("agents", "rules", "ops", "egress=+docs.vllm.ai,-any").stdout)

    def test_5_off_on_router_and_rm(self):
        self.assertIn("ops is off", self.bb("agents", "off", "ops").stderr)
        self.assertIn("off", self.bb("agents", "ls").stdout)
        self.bb("agents", "on", "ops")
        r = self.bb("agents", "router")
        self.assertIn("glm-5.3-flash", r.stdout)
        self.assertIn("auto, a goal:", r.stdout)
        r = self.bb("agents", "rm", "ops", "--yes")
        self.assertIn("its folder is kept at", r.stderr)
        self.assertNotIn("ops  ", self.bb("agents", "ls").stdout)


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
