"""runner.py through the API, against the fake engine and a fake MCP
server: a turn runs on the server, streams to any client that follows its
run, and is saved, traced and counted once, whoever started it."""
import glob
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "fakes"))
from test_api import Server, HttpError  # noqa: E402
from fake_engine import FakeEngine  # noqa: E402

MODEL = "fake-model"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = FakeEngine(models=[{"id": MODEL, "max_model_len": 32768}, {"id": "tiny-model"}, {"id": "dial-model"}, {"id": "floor-model"}, {"id": "mid-model"}]).start()
        cls.data = tempfile.mkdtemp()
        cls.srv = Server(cls.data, {
            "gateways": [{"name": "fake", "url": cls.eng.url, "key": "", "enabled": True}],
            "mcp_servers": {"fake": {"command": sys.executable, "args": [os.path.join(HERE, "fakes", "fake_mcp.py")]}},
            "model_capabilities": {"tiny-model": {"ctx": 1200}, "dial-model": {"effort": ["none", "low", "high"]},
                                   "floor-model": {"min_max_tokens": 12000}, "mid-model": {"ctx": 24576}}})
        deadline = time.time() + 20
        while time.time() < deadline:
            tools = cls.srv.request("GET", "/api/tools")[1]
            if any(t["name"] == "fake__echo" for t in tools.get("tools") or []):
                break
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.eng.stop()
        shutil.rmtree(cls.data, ignore_errors=True)

    def setUp(self):
        self.eng.clear()
        self.n0 = len(self.eng.requests)

    def turn(self, text, sid="new", wait=True, **kw):
        body = dict({"text": text, "model": MODEL}, **kw)
        st, r = self.srv.request("POST", "/api/sessions/%s/turns" % sid, body, headers={"X-BB-Client": "cli"})
        self.assertEqual(st, 202, r)
        if not wait:
            return r, None
        return r, self.follow(r["run"])

    def follow(self, rid, timeout=30):
        frames = self.srv.sse("/api/runs/%s/events" % rid, timeout=timeout,
                              until=lambda fr: json.loads(fr[-1]["data"])["type"] == "finished")
        return [json.loads(f["data"]) for f in frames]

    @staticmethod
    def of(evts, type_):
        return [e["data"] for e in evts if e["type"] == type_]

    def sent(self, i=0):
        return self.eng.requests[self.n0 + i]["body"]


class RunnerTest(_Base):

    # ------------------------------------------------------------ the turn
    def test_max_tokens_auto_a_number_and_more_than_the_window(self):
        self.eng.script([{"content": "a"}, {"content": "b"}, {"content": "c"}])
        self.turn("auto", params={"max_tokens": "auto"})
        self.assertNotIn("max_tokens", self.sent(0))
        self.turn("pinned", params={"max_tokens": 4096})
        self.assertEqual(self.sent(1)["max_tokens"], 4096)
        # a 1M dial on a 32k window: cut down to the room left, with a note, not an error
        r, evts = self.turn("huge", params={"max_tokens": 1048576})
        self.assertLess(self.sent(2)["max_tokens"], 32768)
        self.assertEqual(self.of(evts, "finished")[0]["state"], "done")

    def test_a_turn_streams_saves_traces_and_counts(self):
        self.eng.script([{"content": "Paris is the capital.", "reasoning": "geography",
                          "usage": {"prompt_tokens": 40, "completion_tokens": 9}}])
        r, evts = self.turn("capital of France?", system="be brief")
        deltas = self.of(evts, "delta")
        self.assertEqual("".join(d.get("content", "") for d in deltas), "Paris is the capital.")
        self.assertEqual("".join(d.get("reasoning", "") for d in deltas), "geography")
        done = self.of(evts, "done")[0]["message"]
        self.assertEqual((done["role"], done["content"], done["reasoning"]), ("bot", "Paris is the capital.", "geography"))
        self.assertTrue(done["meta"].startswith(MODEL + "  ·  9 tok"), done["meta"])
        self.assertEqual(self.of(evts, "finished")[0]["state"], "done")
        sent = self.sent()
        self.assertEqual(sent["messages"], [{"role": "system", "content": "be brief"},
                                            {"role": "user", "content": "capital of France?"}])
        self.assertEqual((sent["temperature"], sent["top_p"], sent["top_k"]), (0.7, 0.95, 40))
        self.assertNotIn("max_tokens", sent)     # Auto: the engine may use what the window has left
        self.assertTrue(sent["stream"])
        sess = self.srv.request("GET", "/api/sessions?id=" + r["session"])[1]
        self.assertEqual([m["role"] for m in sess["messages"]], ["user", "bot"])
        self.assertEqual(sess["source"], "cli")
        with open(os.path.join(self.data, "usage.jsonl")) as f:
            usage = [json.loads(x) for x in f]
        self.assertIn(("cli", r["session"], 9), [(u.get("source"), u.get("session"), u.get("completion_tokens")) for u in usage])

    def test_the_next_turn_carries_the_history(self):
        self.eng.script([{"content": "first answer"}, {"content": "second answer"}])
        r, _ = self.turn("one")
        self.turn("two", sid=r["session"])
        self.assertEqual([m["content"] for m in self.sent(1)["messages"]], ["one", "first answer", "two"])

    def test_when_it_was_said_rides_on_the_message_and_is_kept_with_it(self):
        # an agent's ask carries its date and time on the message, not in the system prompt: the next
        # ask sends the first one exactly as it was sent, so the engine does not read it again
        self.eng.script([{"content": "first answer"}, {"content": "second answer"}])
        r, _ = self.turn("one", stamp="Monday, November 16, 2026, 10:02 local time", system="You are Ops.")
        self.turn("two", sid=r["session"], stamp="Monday, November 16, 2026, 10:09 local time", system="You are Ops.")
        first, second = self.sent(0)["messages"], self.sent(1)["messages"]
        self.assertEqual(first[-1]["content"], "[Monday, November 16, 2026, 10:02 local time]\none")
        self.assertEqual(second[:len(first)], first)                              # the same text, byte for byte
        self.assertEqual(second[-1]["content"], "[Monday, November 16, 2026, 10:09 local time]\ntwo")
        sess = self.srv.request("GET", "/api/sessions?id=" + r["session"])[1]
        self.assertEqual([(m["content"], m.get("stamp")) for m in sess["messages"] if m["role"] == "user"],
                         [("one", "Monday, November 16, 2026, 10:02 local time"),
                          ("two", "Monday, November 16, 2026, 10:09 local time")])   # what was typed stays what was typed

    def test_tool_hops_run_on_the_server(self):
        self.eng.script([{"tool_calls": [{"name": "fake__echo", "arguments": {"text": "pong"}}]},
                         {"content": "the tool said pong"}])
        r, evts = self.turn("call the tool")
        self.assertEqual(self.of(evts, "tool_call")[0]["name"], "fake__echo")
        res = self.of(evts, "tool_result")[0]
        self.assertFalse(res["is_error"])
        self.assertIn("pong", res["content"])
        second = self.sent(1)["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant", "tool"])
        self.assertIn("pong", second[2]["content"])
        done = self.of(evts, "done")[0]["message"]
        self.assertEqual(done["content"], "the tool said pong")
        self.assertEqual(len(done["hops"]), 1)
        self.assertEqual(done["toolUse"][0]["name"], "fake__echo")
        self.assertIn("1 tool call", done["meta"])

    def test_overflow_is_retried_smaller_and_the_window_remembered(self):
        self.eng.script([{"overflow": {"ctx": 16384, "requested": 20000, "prompt": 8000}}, {"content": "fits now"}])
        r, evts = self.turn("hello", params={"max_tokens": 12000})
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "fits now")
        self.assertLess(self.sent(1)["max_tokens"], 12000)
        self.assertTrue(any("Max tokens clamped" in n["text"] for n in self.of(evts, "notice")))
        with open(os.path.join(self.data, "model_facts.json")) as f:
            self.assertEqual(json.load(f)[MODEL]["ctx"], 16384)

    def test_llama_cpp_saying_the_prompt_is_too_long_is_understood_and_its_window_remembered(self):
        said = "request (188014 tokens) exceeds the available context size (20000 tokens), try increasing it"
        self.eng.script([{"status": 400, "error": said}])
        r, evts = self.turn("far too much", model="llama-model")
        msg = self.srv.request("GET", "/api/sessions?id=" + r["session"])[1]["messages"][-1]
        self.assertIn("context is full: the prompt is 188,014 tokens of a 20,000-token window", msg["error"])
        with open(os.path.join(self.data, "model_facts.json")) as f:
            self.assertEqual(json.load(f)["llama-model"]["ctx"], 20000)

    def test_a_server_without_tool_support_answers_without_tools(self):
        self.eng.script([{"status": 400, "error": "'auto' tool choice requires --enable-auto-tool-choice"},
                         {"content": "no tools then"}])
        r, evts = self.turn("hi")
        self.assertIn("tools", self.sent(0))
        self.assertNotIn("tools", self.sent(1))
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "no tools then")

    def test_a_truncated_tool_call_is_dropped_not_run(self):
        self.eng.script([{"tool_calls": [{"name": "fake__echo", "arguments": '{"text": "unfinished'}],
                          "finish_reason": "length"}])
        r, evts = self.turn("go")
        self.assertEqual(self.of(evts, "tool_call"), [])
        self.assertTrue(any("Dropped a truncated tool call" in n["text"] for n in self.of(evts, "notice")))

    def test_the_same_call_three_times_stops(self):
        call = {"tool_calls": [{"name": "fake__echo", "arguments": {"text": "again"}}]}
        self.eng.script([call, call, call, {"content": "never reached"}])
        r, evts = self.turn("loop")
        self.assertEqual(len(self.of(evts, "tool_call")), 3)
        self.assertTrue(any("identical tool call three times" in n["text"] for n in self.of(evts, "notice")))

    def test_stop_ends_the_turn_and_keeps_what_came(self):
        self.eng.script([{"content": "word " * 200, "chunk": 5, "pace": 0.05}])
        r, _ = self.turn("long story", wait=False)
        time.sleep(1.0)
        t0 = time.time()
        st, _ = self.srv.request("POST", "/api/runs/%s/cancel" % r["run"], {})
        self.assertEqual(st, 200)
        evts = self.follow(r["run"])
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(self.of(evts, "finished")[0]["state"], "cancelled")
        msg = self.srv.request("GET", "/api/sessions?id=" + r["session"])[1]["messages"][-1]
        self.assertTrue(msg["content"].startswith("word word"))
        self.assertLess(len(msg["content"]), 1000)
        self.assertEqual(msg["error"], "")

    def test_one_turn_per_session(self):
        self.eng.script([{"content": "slow " * 50, "pace": 0.05}])
        r, _ = self.turn("first", wait=False)
        st, body = self.srv.request("POST", "/api/sessions/%s/turns" % r["session"], {"text": "second", "model": MODEL})
        self.assertEqual((st, body.get("run")), (409, r["run"]))
        self.follow(r["run"])

    def test_compress_folds_older_turns_into_a_summary(self):
        self.eng.script([{"content": "a1"}, {"content": "a2"}, {"content": "a3"}, {"content": "SUMMARY: we talked"}])
        r, _ = self.turn("q1")
        for q in ("q2", "q3"):
            self.turn(q, sid=r["session"])
        st, c = self.srv.request("POST", "/api/sessions/%s/compress" % r["session"], {"model": MODEL})
        self.assertEqual(st, 202, c)
        evts = self.follow(c["run"])
        self.assertEqual(self.of(evts, "compressed")[0]["count"], 2)
        msgs = self.srv.request("GET", "/api/sessions?id=" + r["session"])[1]["messages"]
        self.assertEqual(msgs[0]["kind"], "summary")
        self.assertIn("SUMMARY: we talked", msgs[0]["content"])
        self.assertEqual([m["content"] for m in msgs[2:]], ["q2", "a2", "q3", "a3"])
        self.assertTrue(glob.glob(os.path.join(self.data, "archive", r["session"] + "_*.json")))
        self.assertEqual(self.sent(3)["max_tokens"], 8192)
        self.assertEqual(self.sent(3)["messages"][0]["content"][:40], "You are compressing a conversation so th")

    def test_a_full_context_says_so(self):
        r, evts = self.turn("x" * 6000, model="tiny-model", auto_compress=False)
        err = self.of(evts, "done")[0]["message"]["error"]
        self.assertIn("context is full", err)
        self.assertEqual(self.of(evts, "finished")[0]["state"], "error")


class ApprovalTest(_Base):
    """Tools that ask before they run, and tools the client runs itself."""
    ASKS = "fake__ping_client"          # no annotations: may change things, so it asks

    def call_then_answer(self, **kw):
        self.eng.script([{"tool_calls": [{"name": self.ASKS, "arguments": {}}]}, {"content": "after the tool"}])
        return self.turn("use the tool", wait=False, **kw)

    def wait_for(self, rid, type_, timeout=15):
        frames = self.srv.sse("/api/runs/%s/events" % rid, timeout=timeout,
                              until=lambda fr: any(json.loads(f["data"])["type"] in (type_, "finished") for f in fr))
        return [json.loads(f["data"]) for f in frames]

    def test_nobody_to_ask_ends_the_turn(self):
        r, _ = self.call_then_answer(interactive=False)
        evts = self.follow(r["run"])
        fin = self.of(evts, "finished")[0]
        self.assertEqual((fin["state"], fin["result"]["approval_needed"]), ("error", True))
        self.assertIn("approval needed", self.of(evts, "done")[0]["message"]["error"])

    def test_allow_runs_it_and_any_client_may_answer(self):
        r, _ = self.call_then_answer()
        ask = self.of(self.wait_for(r["run"], "approval"), "approval")[0]
        self.assertEqual(ask["tool"], self.ASKS)
        self.assertEqual([p["id"] for p in self.srv.request("GET", "/api/approvals")[1]["pending"]], [ask["id"]])
        st, _ = self.srv.request("POST", "/api/approvals", {"id": ask["id"], "decision": "allow"})
        self.assertEqual(st, 200)
        st, _ = self.srv.request("POST", "/api/approvals", {"id": ask["id"], "decision": "deny"})
        self.assertEqual(st, 409)                                   # the first answer won
        evts = self.follow(r["run"])
        self.assertFalse(self.of(evts, "tool_result")[0]["is_error"])
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "after the tool")

    def test_deny_tells_the_model(self):
        r, _ = self.call_then_answer()
        ask = self.of(self.wait_for(r["run"], "approval"), "approval")[0]
        self.srv.request("POST", "/api/approvals", {"id": ask["id"], "decision": "deny"})
        evts = self.follow(r["run"])
        res = self.of(evts, "tool_result")[0]
        self.assertTrue(res["is_error"])
        self.assertIn("did not allow", res["content"])
        self.assertIn("did not allow", self.sent(1)["messages"][-1]["content"])

    def test_always_stops_asking(self):
        r, _ = self.call_then_answer()
        ask = self.of(self.wait_for(r["run"], "approval"), "approval")[0]
        self.srv.request("POST", "/api/approvals", {"id": ask["id"], "decision": "always"})
        self.follow(r["run"])
        self.eng.script([{"tool_calls": [{"name": self.ASKS, "arguments": {}}]}, {"content": "no question"}])
        r2, evts = self.turn("again", interactive=False)
        self.assertEqual(self.of(evts, "approval"), [])
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "no question")
        with open(os.path.join(self.data, "config.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["tool_policy"][self.ASKS], "allow")
        cfg["tool_policy"].pop(self.ASKS)                          # the other tests expect it to ask
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump(cfg, f)

    def test_yes_and_a_profiles_deny(self):
        self.eng.script([{"tool_calls": [{"name": self.ASKS, "arguments": {}}]}, {"content": "allowed by yes"}])
        r, evts = self.turn("go", yes=True, interactive=False)
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "allowed by yes")
        self.srv.request("POST", "/api/profiles", {"action": "save", "name": "Locked",
                                                   "profile": {"tool_policy": {"fake__*": "deny"}}})
        self.eng.script([{"tool_calls": [{"name": "fake__echo", "arguments": {"text": "x"}}]}, {"content": "ok"}])
        r, evts = self.turn("go", profile="Locked")
        self.assertIn("turned off", self.of(evts, "tool_result")[0]["content"])

    def test_client_tools_run_on_the_client(self):
        defs = [{"type": "function", "function": {"name": "ws__read", "description": "read",
                                                  "parameters": {"type": "object", "properties": {}}}}]
        self.eng.script([{"tool_calls": [{"name": "ws__read", "arguments": {"path": "a.txt"}}]}, {"content": "read it"}])
        r, _ = self.turn("read a.txt", wait=False, client_tools=defs)
        call = self.of(self.wait_for(r["run"], "client_call"), "client_call")[0]
        self.assertEqual((call["name"], call["args"]), ("ws__read", {"path": "a.txt"}))
        st, _ = self.srv.request("POST", "/api/runs/%s/tool-results" % r["run"],
                                 {"call_id": call["call_id"], "content": "file body", "is_error": False})
        self.assertEqual(st, 200)
        evts = self.follow(r["run"])
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "read it")
        self.assertEqual(self.sent(1)["messages"][-1]["content"], "file body")
        self.assertEqual(self.sent(0)["tools"][-1]["function"]["name"], "ws__read")


class WorkflowTest(_Base):
    def save(self, name, **wf):
        st, r = self.srv.request("POST", "/api/workflows", {"action": "save", "name": name, "workflow": wf})
        self.assertEqual(st, 200, r)
        return r

    def test_a_workflow_runs_as_a_turn_with_its_prompt_filled(self):
        r = self.save("review", prompt="Review this for bugs: {{code}} (focus: {{focus}})", profile="Deep",
                      params=[{"name": "focus", "default": "correctness"}])
        self.assertEqual([p["name"] for p in r["workflows"]["review"]["params"]], ["focus", "code"])
        st, err = self.srv.request("POST", "/api/workflows/review/run", {"params": {}})
        self.assertEqual(st, 400)
        self.assertIn("needs code", err["error"])
        self.assertIn("-p code=", err["error"])
        self.eng.script([{"content": "looks fine"}])
        st, run = self.srv.request("POST", "/api/workflows/review/run", {"params": {"code": "x = 1"}})
        self.assertEqual(st, 202, run)
        evts = self.follow(run["run"])
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "looks fine")
        sent = self.sent(0)
        self.assertEqual(sent["messages"][-1]["content"], "Review this for bugs: x = 1 (focus: correctness)")
        self.assertGreater(sent["max_tokens"], 30000)                   # Deep's 32768, clamped to the served window
        sess = self.srv.request("GET", "/api/sessions?id=" + run["session"])[1]
        self.assertEqual(sess["title"], "Review this for bugs: x = 1 (focus: correctness)")

    def test_a_job_can_run_a_workflow_and_never_asks(self):
        self.save("ping", prompt="ping the client and report: {{why}}")
        st, saved = self.srv.request("POST", "/api/jobs", {"action": "save", "job": {
            "name": "pinger", "workflow": "ping", "params": {"why": "nightly"},
            "schedule": {"kind": "cron", "cron": "0 3 * * *"}}})
        self.assertEqual(st, 200, saved)
        self.eng.script([{"tool_calls": [{"name": "fake__ping_client", "arguments": {}}]}, {"content": "could not"}])
        st, r = self.srv.request("POST", "/api/jobs", {"action": "run_now", "id": saved["job"]["id"]})
        evts = self.follow(r["run"])
        self.assertEqual(self.of(evts, "finished")[0]["state"], "done")
        self.assertEqual(self.sent(0)["messages"][-1]["content"], "ping the client and report: nightly")
        self.assertIn("scheduled job never asks", self.sent(1)["messages"][-1]["content"])
        self.assertEqual(self.of(evts, "approval"), [])
        hist = self.srv.request("GET", "/api/jobs/runs?id=" + saved["job"]["id"])[1]["runs"]
        self.assertEqual((hist[0]["ok"], hist[0]["output"]), (True, "could not"))
        names = [s["id"] for s in self.srv.request("GET", "/api/sessions")[1]]
        self.assertNotIn("job-" + saved["job"]["id"], names)             # a job leaves no session behind

    def job(self, name, model=None, **kw):
        job = dict({"name": name, "prompt": "the morning report", "schedule": {"kind": "cron", "cron": "0 3 * * *"}}, **kw)
        if model:
            job["model"] = model
        st, saved = self.srv.request("POST", "/api/jobs", {"action": "save", "job": job})
        self.assertEqual(st, 200, saved)
        return saved["job"]["id"]

    def ran(self, jid, script):
        self.eng.clear()
        self.eng.script(script)
        st, r = self.srv.request("POST", "/api/jobs", {"action": "run_now", "id": jid})
        self.follow(r["run"])
        return self.srv.request("GET", "/api/jobs/runs?id=" + jid)[1]["runs"][0]

    def test_a_job_that_wrote_nothing_at_its_limit_or_after_a_second_ask_has_failed_and_says_why(self):
        spent = {"reasoning": "Thinking that never gets to the point. " * 40, "finish_reason": "length"}
        plain, dial = self.job("reporter", model=MODEL), self.job("reporter-dial", model="dial-model")   # MODEL has no dial
        h = self.ran(plain, [spent])                                   # nothing to turn down: its limit ends it
        self.assertEqual((h["ok"], h["error"]), (False, "the model gave no answer (it reached the limit of one reply without answering)"))
        h = self.ran(dial, [spent, spent, {"content": "never asked for"}])
        self.assertEqual((h["ok"], h["error"]), (False, "the model gave no answer (The model used the whole of one reply thinking)"))
        # stopped for circles, asked again, and the second reply ends by itself with nothing: no silent done either
        h = self.ran(dial, [{"reasoning": circles(2000), "chunk": 120, "pace": 0.002}, {"reasoning": "Still nothing to say."}])
        self.assertEqual((h["ok"], h["error"]),
                         (False, "the model gave no answer (The model was going round in circles while thinking and was stopped)"))
        # thinking sent inside the answer, in any case, with nothing after it, is nothing written
        for only_thinking in ("<THINK>" + "thinking " * 200, "<think>" + "thinking " * 200 + "</think>", "thinking " * 200 + "</think>"):
            h = self.ran(plain, [{"content": only_thinking, "finish_reason": "length"}])
            self.assertEqual(h["ok"], False, only_thinking[:20])
        # what a job did write is kept: an answer cut at the limit, and an answer after a second ask
        h = self.ran(plain, [{"content": "The first half of the report", "finish_reason": "length"}])
        self.assertEqual((h["ok"], h["output"]), (True, "The first half of the report"))
        h = self.ran(dial, [spent, {"content": "All quiet."}])
        self.assertEqual((h["ok"], h["output"]), (True, "All quiet."))

    def test_a_job_whose_tools_did_the_work_and_whose_last_reply_is_empty_stays_done(self):
        call = {"tool_calls": [{"name": "fake__echo", "arguments": {"text": "hi"}}]}
        jid = self.job("pinger-plain", model=MODEL, prompt="echo hi and stop", tools=True)
        h = self.ran(jid, [call, {"content": ""}])
        self.assertEqual((h["ok"], h["output"], h.get("error")), (True, "", None))
        # also when an earlier reply of its turn had to be asked for twice: what counts is how the turn ended
        jid = self.job("pinger-dial", model="dial-model", prompt="echo hi and stop", tools=True)
        spent = {"reasoning": "Thinking that never gets to the point. " * 40, "finish_reason": "length"}
        h = self.ran(jid, [spent, call, {"content": ""}])
        self.assertEqual((h["ok"], h["output"], h.get("error")), (True, "", None))
        h = self.ran(jid, [{"reasoning": circles(2000), "chunk": 120, "pace": 0.002}, call, {"content": ""}])
        self.assertEqual((h["ok"], h["output"], h.get("error")), (True, "", None))
        # and when the turn ends in that very hop: one hop allowed, the second ask calls the tool, the tool runs
        jid = self.job("pinger-one-hop", model="dial-model", prompt="echo hi and stop", tools=True, max_hops=1)
        h = self.ran(jid, [spent, call, {"content": "never asked for"}])
        self.assertEqual((h["ok"], h.get("error")), (True, None))

    def test_names_and_kinds_are_checked(self):
        for body, want in (({"name": "has space", "workflow": {"prompt": "x"}}, "no spaces"),
                           ({"name": "k", "workflow": {"prompt": "x", "kind": "robot"}}, "chat or agents"),
                           ({"name": "p", "workflow": {"prompt": "x", "profile": "Nope"}}, "no profile"),
                           ({"name": "e", "workflow": {"prompt": "  "}}, "needs a prompt")):
            st, r = self.srv.request("POST", "/api/workflows", dict(body, action="save"))
            self.assertEqual(st, 400, body)
            self.assertIn(want, r["error"])


class RoleTest(_Base):
    def test_role_names_resolve_or_say_what_to_do(self):
        st, r = self.srv.request("POST", "/api/sessions/new/turns", {"text": "hi", "model": "role:fast"})
        self.assertEqual(st, 400)
        self.assertIn("Settings, Roles", r["error"])
        self.srv.request("POST", "/api/settings", {"roles": {"fast": "tiny-model", "big": "nowhere-model"}})
        self.eng.script([{"content": "fast answer"}])
        r, evts = self.turn("hi", model="role:fast")
        self.assertEqual(self.sent(0)["model"], "tiny-model")
        st, r = self.srv.request("POST", "/api/sessions/new/turns", {"text": "hi", "model": "role:big"})
        self.assertEqual(st, 400)
        self.assertIn("no gateway serves it", r["error"])
        st, r = self.srv.request("POST", "/api/sessions/new/turns", {"text": "hi", "model": "role:huge"})
        self.assertIn("there is no 'huge' role", r["error"])
        self.srv.request("POST", "/api/settings", {"roles": {"fast": "", "big": ""}})

    def test_a_job_on_an_unset_role_fails_before_it_starts(self):
        st, saved = self.srv.request("POST", "/api/jobs", {"action": "save", "job": {
            "name": "role job", "prompt": "hello", "model": "role:thinking", "schedule": {"kind": "cron", "cron": "0 3 * * *"}}})
        st, r = self.srv.request("POST", "/api/jobs", {"action": "run_now", "id": saved["job"]["id"]})
        evts = self.follow(r["run"])
        self.assertEqual(self.of(evts, "finished")[0]["state"], "error")
        hist = self.srv.request("GET", "/api/jobs/runs?id=" + saved["job"]["id"])[1]["runs"]
        self.assertIn("thinking role names no model", hist[0]["error"])
        self.assertEqual(len(self.eng.requests), self.n0)                 # nothing was sent


if __name__ == "__main__":
    unittest.main()


ROUND = ("Let me decide once more whether the owner really asked for this and what the rules say about it.\n"
         "Hmm, but actually, let me reconsider that from the start before I do anything at all here.\n")


def circles(n, lead="First I read the file that was dropped, and it is an invoice for hosting.\n"):
    """Words that begin honestly and then say the same two lines n times over."""
    return lead + ROUND * n


def report(n=16):
    """An honest report: every service has its own lines, and one line that is the same for all of them."""
    return "".join("## Service %d, %s\n- Status: healthy, no restarts in the last 24 hours and no errors in its log\n"
                   "- Requests since midnight: %d, slowest %d ms\n\n" % (i, "abcdefghijklmnop"[i % 16] * 6, 1000 + 37 * i, 90 + i)
                   for i in range(n)) + "Nothing needs you today."


MATRIX = "Here is the 50 by 50 zero matrix:\n\nM = [\n" + ("    [" + ", ".join(["0"] * 50) + "],\n") * 50 + "]\n"


class RepliesThatCircle(_Base):
    """Where nobody is watching, thinking that goes round in circles is stopped while it streams, not at the
    end of the window. Unstopped, each circling reply here streams for more than six seconds (3,000 deltas,
    2 ms apart). An answer is never judged, and a person's turn is never stopped."""

    def last_message(self, r):
        return self.srv.request("GET", "/api/sessions?id=" + r["session"])[1]["messages"][-1]

    def traces(self):
        out = []
        for p in glob.glob(os.path.join(self.data, "traces", "*.jsonl")):
            with open(p) as f:
                out += [json.loads(line) for line in f]
        return out

    def test_thinking_in_circles_is_stopped_early_and_the_engine_let_go(self):
        self.eng.script([{"reasoning": circles(2000), "content": "never reached", "chunk": 120, "pace": 0.002},
                         {"content": "It is an invoice.", "reasoning": "Short this time."}])
        t0 = time.time()
        r, evts = self.turn("think it over", model="dial-model", unattended=True)
        self.assertLess(time.time() - t0, 5)
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), ("It is an invoice.", ""))
        deadline = time.time() + 5                                     # the engine was let go of, not left writing
        while time.time() < deadline and self.eng.running:
            time.sleep(0.05)
        self.assertEqual(self.eng.running, 0)
        mine = [t for t in self.traces() if t.get("kind") == "chat" and t.get("turn") == msg["turn"]]
        self.assertEqual([(t["response"] or {}).get("error") or "" for t in mine],
                         ["stopped by the app: the reply's thinking was going round in circles", ""])
        kept = (mine[0]["response"] or {}).get("reasoning") or ""
        self.assertLess(len(kept), 12000)                              # stopped after a few rounds, not after pages of them
        # the reply the engine never counted is counted by the app, and the turn's count is marked as an estimate
        done = self.of(evts, "done")[0]
        self.assertIn("~", done["meta"])
        fin = self.of(evts, "finished")[0]["result"]
        self.assertGreater(fin["tokens"], 300)                         # about a quarter of the characters it wrote
        self.assertLess(fin["tokens"], 3000)
        self.eng.clear()                                               # the prompt was read twice: about double one reading
        self.eng.script([{"content": "It is an invoice."}])
        r1, evts1 = self.turn("think it over", model="dial-model", unattended=True)
        once = self.of(evts1, "finished")[0]["result"]["prompt_tokens"]
        self.assertGreater(fin["prompt_tokens"], 1.6 * once)           # (the app's own count of a prompt runs higher than this engine's)

    def test_a_model_with_nothing_to_turn_down_is_not_stopped_its_reply_ends_by_itself_or_at_its_bound(self):
        # nothing could be asked for differently, so nothing is stopped: thinking that only looks like circles costs nothing
        sheet = "".join("Row %d: every cell reads the same.\n" % 7 for _ in range(70))
        self.eng.script([{"reasoning": "I go down the sheet.\n" + sheet, "content": "All 70 rows read the same.", "chunk": 400}])
        r, evts = self.turn("check the sheet", unattended=True)        # fake-model has no dial
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), ("All 70 rows read the same.", ""))
        self.assertEqual(msg["reasoning"], "I go down the sheet.\n" + sheet)
        self.assertEqual(len(self.eng.requests) - self.n0, 1)

    def test_what_was_thought_in_earlier_hops_stays_when_a_later_reply_is_asked_for_again(self):
        call = {"reasoning": "Thinking of the first hop, kept as it was. ", "tool_calls": [{"name": "fake__echo", "arguments": {"text": "hi"}}]}
        self.eng.script([call, {"reasoning": circles(2000), "chunk": 120, "pace": 0.002},
                         {"content": "Echoed, and all is quiet.", "reasoning": "Short this time."}])
        r, evts = self.turn("echo, then report", model="dial-model", unattended=True, yes=True)
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), ("Echoed, and all is quiet.", ""))
        self.assertEqual(msg["reasoning"], "Thinking of the first hop, kept as it was. Short this time.")
        self.assertIn("~", self.of(evts, "done")[0]["meta"])           # a stopped reply and whole ones: still an estimate

    def test_honest_words_that_come_back_are_left_alone(self):
        steps = "".join("Step %d: note %d says spark-%d had %d GB free, which I compare with the note before it.\n"
                        % (i, i, 1 + i % 2, 100 + i * 7 % 300) for i in range(400))
        sorting = "".join("Ticket %d is about %s.\nThis is about getting into the account, so it goes to the access queue "
                          "with normal priority.\n" % (i, "a login that fails on %s" % ("Mondays", "the phone", "the VPN")[i % 3])
                          for i in range(40))
        self.eng.script([{"reasoning": steps + sorting, "content": report(), "chunk": 400}])
        r, evts = self.turn("the morning report", model="dial-model", unattended=True)
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), (report(), ""))
        self.assertEqual(msg["reasoning"], steps + sorting)
        self.assertEqual(len(self.eng.requests) - self.n0, 1)

    def test_an_answer_is_never_judged_it_may_be_a_thousand_equal_lines(self):
        seats = "```sql\n" + "INSERT INTO seats (status) VALUES ('free');\n" * 150 + "```\n"
        for answer in (MATRIX, seats, "0x00, " * 1024):
            self.eng.clear()
            self.eng.script([{"content": answer, "chunk": 400}])
            n = len(self.eng.requests)
            r, evts = self.turn("what I asked for", model="dial-model", unattended=True)
            msg = self.last_message(r)
            self.assertEqual((msg["content"], msg["error"]), (answer, ""), answer[:30])
            self.assertEqual(len(self.eng.requests) - n, 1)

    def test_a_person_is_watching_so_nothing_is_stopped_for_them(self):
        # a person sees the thinking run and presses Stop when they have seen enough: the app does not decide for them
        self.eng.script([{"reasoning": circles(60), "content": "And here is the answer.", "chunk": 400}])
        r, evts = self.turn("think it over", model="dial-model")
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), ("And here is the answer.", ""))
        self.assertEqual(msg["reasoning"], circles(60))
        self.assertEqual(len(self.eng.requests) - self.n0, 1)

    def test_a_turn_nobody_watches_is_asked_again_at_once_with_less_thinking(self):
        # max_hops 1: the second ask is not a tool hop, so it is made even when no hop is left
        self.eng.script([{"reasoning": circles(2000), "content": "never reached", "chunk": 120, "pace": 0.002},
                         {"content": "The morning report: all quiet.", "reasoning": "Short this time."}])
        r, evts = self.turn("the morning report", model="dial-model", unattended=True, max_hops=1)
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), ("The morning report: all quiet.", ""))
        self.assertEqual(msg["reasoning"], "Short this time.")         # the circles are not kept
        self.assertTrue(any(n["text"] == "The model was going round in circles while thinking and was stopped. "
                                         "Asked again with less thinking." for n in self.of(evts, "notice")))
        first, second = self.sent(0), self.sent(1)
        self.assertNotIn("reasoning_effort", first)
        self.assertEqual(second["reasoning_effort"], "low")
        self.assertEqual(first["messages"], second["messages"])
        fin = self.of(evts, "finished")[0]["result"]
        self.assertGreater(fin["tokens"], 300)                         # the reply that was stopped is paid for too
        self.assertGreater(fin["prompt_tokens"], 2 * 5)                # and the prompt was read twice

    def test_thinking_that_arrives_inside_the_answer_is_judged_as_thinking(self):
        # an engine with no reasoning parser: the model's thinking comes in the answer, and the reply begins with <think>
        for tag in ("<think>", "\n<THINK>"):
            self.eng.clear()
            self.eng.script([{"content": tag + circles(2000), "chunk": 120, "pace": 0.002},
                             {"content": "The morning report: all quiet."}])
            n = len(self.eng.requests)
            r, evts = self.turn("the morning report", model="dial-model", unattended=True)
            msg = self.last_message(r)
            self.assertEqual((msg["content"], msg["error"]), ("The morning report: all quiet.", ""), tag)
            self.assertEqual(self.eng.requests[n + 1]["body"]["reasoning_effort"], "low")
        # an answer that speaks of the tag further on is an answer: whole, whatever follows
        about = "These models put their reasoning after a <think> tag. Set this in each of the 70 files:\n" + "enable_thinking: false\n" * 70
        self.eng.clear()
        self.eng.script([{"content": about, "chunk": 400}])
        n = len(self.eng.requests)
        r, evts = self.turn("how do I turn it off?", model="dial-model", unattended=True)
        self.assertEqual((self.last_message(r)["content"], len(self.eng.requests) - n), (about, 1))

    def test_a_reply_that_was_all_thinking_at_its_limit_is_asked_again_when_nobody_watches(self):
        spent = {"reasoning": "Thinking that never gets to the point. " * 40, "finish_reason": "length"}
        self.eng.script([dict(spent), {"content": "The morning report: all quiet."}])
        r, evts = self.turn("the morning report", model="dial-model", unattended=True, reply_cap=8192)
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), ("The morning report: all quiet.", ""))
        self.assertTrue(any(n["text"] == "The model used the whole of one reply thinking. Asked again with less thinking."
                            for n in self.of(evts, "notice")))
        self.assertEqual((self.sent(0).get("reasoning_effort"), self.sent(1)["reasoning_effort"]), (None, "low"))
        fin = self.of(evts, "finished")[0]["result"]
        self.assertGreater(fin["prompt_tokens"], 2 * 5)                # both replies were counted by the engine
        # once only; and what the turn's own record says names the limit as what it is
        self.eng.clear()
        self.eng.script([dict(spent), dict(spent), {"content": "never asked for"}])
        n = len(self.eng.requests)
        r, evts = self.turn("again", model="dial-model", unattended=True, reply_cap=8192)
        self.assertEqual(len(self.eng.requests) - n, 2)
        self.assertEqual(self.last_message(r)["content"], "")
        self.assertIn("stopped at the limit of one reply", self.of(evts, "done")[0]["meta"])
        # a person's own Max tokens: their reply, their choice, and their words for it
        self.eng.clear()
        self.eng.script([dict(spent), {"content": "never asked for"}])
        n = len(self.eng.requests)
        r, evts = self.turn("and for a person", model="dial-model", params={"max_tokens": 500})
        self.assertEqual(len(self.eng.requests) - n, 1)
        self.assertIn("stopped at Max tokens", self.of(evts, "done")[0]["meta"])
        # a long answer cut at its limit is an answer, whatever thinking came before it: kept, not asked for again
        self.eng.clear()
        self.eng.script([{"reasoning": "A short think first.", "content": "The first half of a long report", "finish_reason": "length"},
                         {"content": "never asked for"}])
        n = len(self.eng.requests)
        r, evts = self.turn("the long report", model="dial-model", unattended=True, reply_cap=8192)
        self.assertEqual(len(self.eng.requests) - n, 1)
        self.assertEqual(self.last_message(r)["content"], "The first half of a long report")
        # a whole tool call written just as the limit was reached is a call: it is run, and the turn goes on
        self.eng.clear()
        self.eng.script([{"reasoning": "A short think first.", "tool_calls": [{"name": "fake__echo", "arguments": {"text": "hi"}}],
                          "finish_reason": "length"}, {"content": "after the tool"}, {"content": "never asked for"}])
        n = len(self.eng.requests)
        r, evts = self.turn("echo hi", model="dial-model", unattended=True, reply_cap=8192, yes=True)
        self.assertEqual((len(self.eng.requests) - n, len(self.of(evts, "tool_call"))), (2, 1))
        self.assertEqual(self.last_message(r)["content"], "after the tool")
        # a reply that is nothing but line breaks at its limit (JSON mode gone wrong) was not thinking either
        self.eng.clear()
        self.eng.script([{"content": "\n" * 2000, "finish_reason": "length"}, {"content": "never asked for"}])
        n = len(self.eng.requests)
        r, evts = self.turn("as JSON", model="dial-model", unattended=True, reply_cap=8192)
        self.assertEqual(len(self.eng.requests) - n, 1)
        self.assertEqual(self.of(evts, "notice"), [])

    def test_the_second_reply_is_not_watched_whatever_it_thinks_it_is_let_finish(self):
        self.eng.script([{"reasoning": circles(2000), "chunk": 120, "pace": 0.002},
                         {"reasoning": circles(40), "content": "Done.", "chunk": 400}])
        r, evts = self.turn("the morning report", model="dial-model", unattended=True)
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), ("Done.", ""))
        self.assertEqual(msg["reasoning"], circles(40))                # whole: it was neither stopped nor trimmed

    def test_a_reply_at_its_limit_that_began_with_the_tag_in_capitals_was_thinking_too(self):
        self.eng.script([{"content": "<THINK>" + "Thinking that never gets to the point. " * 40, "finish_reason": "length"},
                         {"content": "The morning report: all quiet."}])
        r, evts = self.turn("the morning report", model="dial-model", unattended=True, reply_cap=8192)
        self.assertEqual(len(self.eng.requests) - self.n0, 2)
        self.assertEqual(self.last_message(r)["content"], "The morning report: all quiet.")

    def test_one_second_ask_in_a_turn_and_after_it_nothing_is_stopped(self):
        # the first reply circles and is asked again; that reply calls a tool; the reply after the tool circles too:
        # it is not stopped (nothing is left to change) and it is not asked for again
        call = {"tool_calls": [{"name": "fake__echo", "arguments": {"text": "hi"}}]}
        self.eng.script([{"reasoning": circles(2000), "chunk": 120, "pace": 0.002}, call,
                         {"reasoning": circles(40), "content": "Echoed.", "chunk": 400}, {"content": "never asked for"}])
        n = len(self.eng.requests)
        r, evts = self.turn("echo, then report", model="dial-model", unattended=True, yes=True)
        self.assertEqual(len(self.eng.requests) - n, 3)
        self.assertEqual(len(self.of(evts, "tool_call")), 1)
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"]), ("Echoed.", ""))
        self.assertTrue(msg["reasoning"].endswith(ROUND * 40))        # the later thinking is whole: nobody stopped it

    def test_thinking_that_left_no_room_for_its_tool_call_is_asked_again_but_a_long_call_alone_is_not(self):
        cut = {"tool_calls": [{"name": "fake__echo", "arguments": '{"text": "unfin'}], "finish_reason": "length"}
        self.eng.script([dict(cut, reasoning="A long think before the call. " * 40), {"content": "Done without it."}])
        n = len(self.eng.requests)
        r, evts = self.turn("echo something", model="dial-model", unattended=True, reply_cap=8192)
        self.assertEqual(len(self.eng.requests) - n, 2)
        self.assertEqual(self.last_message(r)["content"], "Done without it.")
        self.eng.clear()
        self.eng.script([dict(cut), {"content": "never asked for"}])     # no thinking: the call itself was too long
        n = len(self.eng.requests)
        r, evts = self.turn("echo a very long thing", model="dial-model", unattended=True, reply_cap=8192)
        self.assertEqual(len(self.eng.requests) - n, 1)
        self.assertTrue(any("Dropped a truncated tool call" in x["text"] and "It reached the limit of one reply." in x["text"]
                            for x in self.of(evts, "notice")))

    def test_a_reply_stopped_for_circles_is_put_aside_whole(self):
        # a tool call written before the thinking went round: it is not run (it may be half a call), and the
        # second ask starts from the same place
        self.eng.script([{"tool_calls": [{"name": "fake__echo", "arguments": {"text": "must not run"}}], "calls_first": True,
                          "reasoning": circles(2000), "chunk": 120, "pace": 0.002}, {"content": "Nothing echoed."}])
        t0 = time.time()
        r, evts = self.turn("echo and go on", model="dial-model", unattended=True, yes=True)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(self.of(evts, "tool_call"), [])
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"], bool(msg.get("toolUse"))), ("Nothing echoed.", "", False))
        self.assertEqual([q["body"].get("reasoning_effort") for q in self.eng.requests[self.n0:]], [None, "low"])
        # words of an answer written before it (an engine that sends them first) do not stand either: they may be half of one
        self.eng.clear()
        self.eng.script([{"content": "The answer, as far as it", "content_first": True, "reasoning": circles(2000), "chunk": 120,
                          "pace": 0.002}, {"content": "The whole answer."}])
        n = len(self.eng.requests)
        r, evts = self.turn("answer, then think", model="dial-model", unattended=True)
        msg = self.last_message(r)
        self.assertEqual((msg["content"], msg["error"], len(self.eng.requests) - n), ("The whole answer.", "", 2))

    def test_the_turn_ends_as_its_second_reply_ends_also_when_that_one_is_cut_short(self):
        spent = {"reasoning": "Thinking that never gets to the point. " * 40, "finish_reason": "length"}
        self.eng.script([spent, {"content": "word " * 400, "chunk": 5, "pace": 0.05}])
        r, _ = self.turn("the morning report", model="dial-model", unattended=True, reply_cap=8192, wait=False)
        deadline = time.time() + 10
        while time.time() < deadline and len(self.eng.requests) - self.n0 < 2:
            time.sleep(0.05)
        time.sleep(0.5)
        self.srv.request("POST", "/api/runs/%s/cancel" % r["run"], {})
        evts = self.follow(r["run"])
        fin = self.of(evts, "finished")[0]
        self.assertEqual(fin["state"], "cancelled")
        self.assertIsNone(fin["result"]["finish"])                     # not the first reply's "length"
        self.assertNotIn("limit", self.of(evts, "done")[0]["meta"])

    def test_a_summary_is_an_answer_and_is_never_judged(self):
        r, _ = self.turn("first", sid="new")
        sid = r["session"]
        for t in ("second", "third"):
            self.turn(t, sid=sid)
        # a summary that quotes, as it is told to, the 150 equal rows the conversation was about
        quoting = "The owner asked for seed rows and got these:\n" + "INSERT INTO seats (status) VALUES ('free');\n" * 150 + "Then two questions."
        self.eng.script([{"content": quoting, "chunk": 400}])
        r, evts = self.turn("/compress", sid=sid)
        first = self.srv.request("GET", "/api/sessions?id=" + sid)[1]["messages"][0]
        self.assertEqual(first["kind"], "summary")
        self.assertTrue(first["content"].endswith(quoting))
        # nor is the summariser's thinking looked at: whatever it thinks, its summary is what counts
        for t in ("fourth", "fifth"):
            self.turn(t, sid=sid)
        self.eng.clear()
        self.eng.script([{"reasoning": circles(60), "content": "The summary of it all.", "chunk": 400}])
        r, evts = self.turn("/compress", sid=sid)
        first = self.srv.request("GET", "/api/sessions?id=" + sid)[1]["messages"][0]
        self.assertTrue(first["content"].endswith("The summary of it all."), first["content"][-80:])


class ABoundOnOneReply(_Base):
    """reply_cap: the caller's bound on a reply nobody watches. It is fitted to the room without a word,
    it does not make a conversation fold sooner, and an engine that refuses it answers without it."""

    def test_it_is_sent_fitted_to_the_room_and_says_nothing(self):
        self.eng.script([{"content": "short"}])
        r, evts = self.turn("hello", reply_cap=8192)
        self.assertEqual(self.sent()["max_tokens"], 8192)
        self.eng.clear()
        self.eng.script([{"content": "short"}])
        n = len(self.eng.requests)                                     # a 1,200-token window, about 300 tokens asked
        r, evts = self.turn("y" * 1000, model="tiny-model", reply_cap=8192, auto_compress=False, tools=False)
        self.assertEqual(len(self.eng.requests) - n, 1)
        self.assertLess(self.eng.requests[-1]["body"]["max_tokens"], 1200)
        self.assertGreater(self.eng.requests[-1]["body"]["max_tokens"], 256)
        self.assertEqual(self.of(evts, "notice"), [])
        self.eng.clear()
        self.eng.script([{"content": "short"}])                        # the person's own Max tokens wins when it is smaller
        r, evts = self.turn("hello", reply_cap=8192, params={"max_tokens": 500})
        self.assertEqual(self.eng.requests[-1]["body"]["max_tokens"], 500)

    def test_it_does_not_fold_a_conversation_sooner(self):
        # A fold needs three turns behind it, so the fourth ask is the first that can show where the line is. On a
        # 24,576-token window the line is 19,660 tokens; a reply of 8,192 kept free would put it at 12,288, and
        # the fourth ask here stands at about 14,000 to 17,600.
        import runner
        self.assertEqual((runner.compress_limit(24576, None), runner.compress_limit(24576, 8192)), (19660, 12288))
        sid = "new"
        for n in range(4):
            self.eng.clear()
            self.eng.script([{"content": "ok %d" % n}])
            before = len(self.eng.requests)
            r, evts = self.turn("part %d " % n + "w" * 14000, sid=sid, model="mid-model", reply_cap=8192, tools=False)
            sid = r["session"]
            self.assertEqual(self.of(evts, "compress"), [], n)
            self.assertEqual(len(self.eng.requests) - before, 1, n)
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "ok 3")

    def test_a_model_that_needs_room_to_think_keeps_it_and_a_persons_limit_is_called_theirs(self):
        self.eng.script([{"content": "short"}])
        r, evts = self.turn("hello", model="floor-model", reply_cap=8192)
        self.assertEqual(self.eng.requests[-1]["body"]["max_tokens"], 12000)      # its recipe's floor, over the bound
        spent = {"reasoning": "Thinking. " * 40, "finish_reason": "length"}
        self.eng.clear()
        self.eng.script([dict(spent)])
        r, evts = self.turn("hello", model="dial-model", reply_cap=8192, params={"max_tokens": 500})
        self.assertIn("stopped at Max tokens", self.of(evts, "done")[0]["meta"])  # a person set it: it is theirs to raise

    def test_an_engine_that_refuses_it_answers_without_it(self):
        refusals = (
            (400, "max_tokens is too large: 8192. This model supports at most 4096 completion tokens, whereas you provided 8192."),
            (400, "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."),
            (422, "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4097. Given: 150 `inputs` tokens and "
                  "8192 `max_new_tokens`"),
            (400, "Bad Request"))                                      # an engine that does not say why
        for n, (status, why) in enumerate(refusals):
            model = "picky-model-%d" % n
            self.eng.clear()
            self.eng.script([{"status": status, "error": why}, {"content": "It is an invoice for 84.00."}])
            before = len(self.eng.requests)
            r, evts = self.turn("what is this?", model=model, reply_cap=8192)
            self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "It is an invoice for 84.00.", why)
            first, second = [q["body"] for q in self.eng.requests[before:]]
            self.assertEqual(first["max_tokens"], 8192)
            self.assertNotIn("max_tokens", second)
            self.assertEqual(self.of(evts, "notice"), [])
        # nothing is remembered about it: the next reply is offered the bound again (one refusal is not a rule)
        self.eng.clear()
        self.eng.script([{"content": "And this is a receipt."}])
        before = len(self.eng.requests)
        r, evts = self.turn("and this?", model="picky-model-0", reply_cap=8192)
        self.assertEqual(len(self.eng.requests) - before, 1)
        self.assertEqual(self.eng.requests[-1]["body"]["max_tokens"], 8192)
        # an engine that first refuses the extras it does not know, and then the bound: both are met, in its own words
        extras = "Unrecognized request arguments supplied: repetition_penalty, top_k"
        self.eng.clear()
        self.eng.script([{"status": 400, "error": extras},
                         {"status": 400, "error": "Unsupported parameter: 'max_tokens' is not supported with this model."},
                         {"status": 400, "error": extras}, {"content": "It is an invoice for 84.00."}])
        before = len(self.eng.requests)
        r, evts = self.turn("what is this?", model="picky-model-extras", reply_cap=8192)
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "It is an invoice for 84.00.")
        bodies = [q["body"] for q in self.eng.requests[before:]]
        self.assertEqual(["max_tokens" in b for b in bodies], [True, True, False, False])
        self.assertEqual(["top_k" in b for b in bodies], [True, False, True, False])
        # one more ask, not more: an engine that refuses the request without the bound too fails as it did before
        self.eng.clear()
        self.eng.script([{"status": 400, "error": "the messages are malformed"}, {"status": 400, "error": "the messages are malformed"},
                         {"content": "A later answer."}])
        before = len(self.eng.requests)
        r, evts = self.turn("what is this?", model="picky-model-other", reply_cap=8192)
        self.assertEqual(len(self.eng.requests) - before, 2)
        self.assertIn("the messages are malformed", self.of(evts, "done")[0]["message"]["error"])
        r, evts = self.turn("and now?", model="picky-model-other", reply_cap=8192)
        self.assertEqual(self.eng.requests[-1]["body"]["max_tokens"], 8192)
        # an engine in trouble is not an engine that refuses the bound: nothing is asked a second time
        for status in (500, 503, 401):
            self.eng.clear()
            self.eng.script([{"status": status, "error": "the engine is in trouble"}, {"content": "never asked for"}])
            before = len(self.eng.requests)
            r, evts = self.turn("what is this?", model="picky-model-down", reply_cap=8192)
            self.assertEqual(len(self.eng.requests) - before, 1, status)
        # the person's own Max tokens is theirs: an engine that refuses it says so to them
        self.eng.clear()
        self.eng.script([{"status": 400, "error": "max_tokens is too large: 9000."}])
        r, evts = self.turn("what is this?", params={"max_tokens": 9000})
        self.assertIn("max_tokens is too large", self.of(evts, "done")[0]["message"]["error"])

    def test_a_second_refusal_is_told_in_the_servers_own_words(self):
        # refused for extras it does not know, asked again without them, refused again for another reason
        self.eng.script([{"status": 400, "error": "Unrecognized request arguments supplied: repetition_penalty, top_k"},
                         {"status": 400, "error": "the messages are malformed: a tool result with no call before it"}])
        r, evts = self.turn("what is this?")
        err = self.of(evts, "done")[0]["message"]["error"]
        self.assertIn("the messages are malformed: a tool result with no call before it", err)
        self.assertNotIn("HTTP Error 400", err)

    def test_vllms_rule_is_met_without_a_notice(self):
        # prompt + max_tokens must fit the window; the app counted fewer tokens than the engine did
        self.eng.script([{"overflow": {"ctx": 16384, "requested": 20000, "prompt": 13403}}, {"content": "fits now"}])
        r, evts = self.turn("hello", reply_cap=8192)
        self.assertEqual(self.of(evts, "done")[0]["message"]["content"], "fits now")
        self.assertLess(self.sent(1)["max_tokens"], 16384 - 13403)
        self.assertEqual(self.of(evts, "notice"), [])


class Circles(unittest.TestCase):
    """runner.circling, runner.in_think and runner.think_less by themselves."""

    def test_what_counts_as_going_round_in_circles(self):
        import runner
        lead = "It begins with something that is said once and never again in the reply. "
        self.assertEqual(runner.circling(lead + ROUND * 2), 0)                     # twice is said again, not circles
        self.assertEqual(runner.circling(lead + ROUND * 4), 0)                     # three in a row, but a short stretch
        block = "".join("Sentence %d of a block of thinking that is eight hundred characters long. " % i for i in range(11))[:800]
        self.assertEqual(runner.circling(lead + block * 3), 1600)                  # three long ones in a row are enough
        many = lead + ROUND * 12
        self.assertEqual(runner.circling(many), 11 * len(ROUND))                   # all of it but the first writing
        self.assertEqual(many[:len(many) - runner.circling(many)], lead + ROUND)
        self.assertGreater(runner.circling("An opening. " + "and then the same words with no line break at all, " * 200), 0)
        for junk in ("G" * 9000, "." * 9000, "| --- | --- |\n" * 700, "</div>\n" * 900, "\n" * 9000, "0, " * 3000):
            self.assertGreater(runner.circling(junk), 0, junk[:20])                # an engine's garbage is circles too
        # a short repeat inside the long one does not hide the long one: twelve equal lines and one other, forty times
        nested = lead + (("word " * 5 + "\n") * 12 + "and one other line\n") * 40
        self.assertEqual(nested[:len(nested) - runner.circling(nested)], lead + ("word " * 5 + "\n") * 12 + "and one other line\n")
        # blocks as long as the ones a real model went round in (2,600 and 4,400 characters), and far longer
        for size, rounds in ((2600, 4), (4400, 4), (19000, 3.2)):
            block = "".join("Sentence %d of a block that is %d characters long in all. " % (i, size) for i in range(size // 40))[:size]
            text = lead + block * int(rounds) + block[:int(size * (rounds - int(rounds)))]
            self.assertGreater(runner.circling(text), size, size)
        # the end of a long reply is what is looked at: circles after a long honest start are seen
        long = "".join("Honest line %d of a very long reply.\n" % i for i in range(5000)) + ROUND * 12
        again = runner.circling(long)                # (a character or two of the honest end may read like the round's)
        self.assertTrue(11 * len(ROUND) <= again < 11 * len(ROUND) + 8, again)

    def test_what_does_not(self):
        import runner
        honest = [
            report(),
            "".join("Line %d of honest thinking that differs from every other line there is.\n" % i for i in range(300)),
            "".join("Ticket %d is about a login.\nThis is about getting into the account, so it goes to the access queue "
                    "with normal priority.\n" % i for i in range(60)),
            "".join("CREATE TABLE t%d (\n  id BIGINT PRIMARY KEY,\n  name_%d TEXT NOT NULL,\n"
                    "  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
                    "  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\n\n" % (i, i) for i in range(40)),
            # a function drafted five times, one line changed each time
            "".join("def merge(intervals):\n    intervals = sorted(intervals, key=lambda pair: (pair[0], pair[1]))\n"
                    "    out = []\n    for a, b in intervals:\n        if out and a <= out[-1][1] + %d:\n"
                    "            out[-1][1] = max(out[-1][1], b)\n        else:\n            out.append([a, b])\n    return out\n\n" % i
                    for i in range(5)),
            # a file written out twice, the second time with one line changed: said again, not circles
            "".join("line %d of the file\n" % i for i in range(400)) + "Here it is again with the fix:\n"
            + "".join("line %d of the file\n" % i for i in range(399)) + "the last line, fixed\n",
            # a long passage quoted twice in a row, to the letter: twice is not circles, three times is
            "Once: " + "".join("sentence %d of the passage that is quoted. " % i for i in range(120)) * 2,
            # a short draft of something periodic, in passing
            "Let me picture the grid first.\n" + ". . . . . . . . . .\n" * 30 + "Good: thirty rows of ten.\n",
            # honest words that END in a short run of something: closing tags, a rule, blank lines
            "".join("<div class=\"row r%d\">cell %d</div>\n" % (i, i) for i in range(120)) + "</div>\n" * 8,
            "".join("Paragraph %d of the page, with words of its own in it.\n" % i for i in range(80)) + "-" * 60 + "\n",
            "".join("Paragraph %d of the page, with words of its own in it.\n" % i for i in range(80)) + "\n" * 40,
            # the same idea with a number that counts up is not seen (its bound on tokens ends it)
            "".join("Attempt %d: the same idea once more, with nothing new in it at all.\n" % i for i in range(900)),
            "",
        ]
        for text in honest:
            self.assertEqual(runner.circling(text), 0, text[:60])

    def test_the_look_is_cheap(self):
        import runner
        text = "".join("Honest line %d of a long reply that never repeats itself.\n" % i for i in range(3000))
        t0 = time.time()
        for _ in range(20):
            runner.circling(text)
        self.assertLess(time.time() - t0, 2.0)                                     # 20 looks at the most it ever looks at

    def test_think_less_turns_down_what_the_model_has(self):
        import runner
        cases = [   # the model's levels, what was asked, what it is turned down to (None: nothing to turn down)
            (["none", "minimal", "low", "medium", "high", "max"], None, "low"),
            (["none", "minimal", "low", "medium", "high", "max"], "high", "low"),
            (["none", "minimal", "low", "medium", "high", "max"], "max", "low"),
            (["none", "minimal", "low", "medium", "high", "max"], "low", "none"),
            (["none", "minimal", "low", "medium", "high", "max"], "minimal", "none"),
            (["none", "minimal", "low", "medium", "high", "max"], "none", None),
            (["high", "medium", "low"], "high", "low"),         # a recipe may list its levels in any order
            (["low", "minimal"], "minimal", None),              # never turned UP to low
            (["medium", "high"], None, "medium"),               # no low: the least it has, up to medium
            (["medium", "high"], "high", "medium"),
            (["medium", "high"], "medium", None),
            (["high", "max"], "max", "high"),
            (["high", "max"], None, None),                      # its own level may be no higher than these
            (["off", "on"], None, "off"),
            (["off", "on"], "on", "off"),
            (["max"], None, None),                              # one level only, and it is the top
            ([], None, None),
        ]
        for levels, asked, then in cases:
            body = {"reasoning_effort": asked} if asked else {}
            self.assertEqual(runner.think_less(body, {"effort": levels}), then is not None, (levels, asked))
            self.assertEqual(body.get("reasoning_effort"), then or asked, (levels, asked))
        switch = {"effort": [], "thinking_switch": "chat_template_kwargs.enable_thinking"}
        body = {"chat_template_kwargs": {"other": 1}}
        kept = dict(body)
        self.assertTrue(runner.think_less(dict(body), switch))                     # asked of a copy: the request is as it was
        self.assertEqual(body, kept)
        self.assertTrue(runner.think_less(body, switch))
        self.assertEqual(body, {"chat_template_kwargs": {"other": 1, "enable_thinking": False}})
        self.assertFalse(runner.think_less(body, switch))                          # already off: nothing left to change

    def test_thinking_sent_inside_the_answer_is_known_as_thinking(self):
        import runner
        for still in ("<think>still at it", "\n  <think>still at it", "<THINK>Still At It", "<Think>"):
            self.assertTrue(runner.in_think(still), still)
            self.assertEqual(runner.said(still), "", still)
        self.assertEqual(runner.said("<THINK>done</THINK> The answer. "), "The answer.")
        self.assertEqual(runner.said(None), "")
        # the chat template opened the block itself: thinking, a bare closing tag, and then the answer or nothing
        self.assertEqual(runner.said("Step one, step two.</think>"), "")
        self.assertEqual(runner.said("Step one, step two.</THINK>\nThe answer."), "The answer.")
        # the same reading as the one an agent's answer gets
        import dispatch
        self.assertEqual(runner.said("x <THINK>a</THINK> b"), "x  b")
        for text in ("plain words", "<think>a</think> b", "<THINK>a</THINK> b", "x <THINK>a</THINK> b", "a</think> b", "a</think>",
                     "<think>open", "", None,
                     "said <think>and then thought", "<think>one</think> said <think>and again"):
            self.assertEqual(runner.said(text), dispatch.strip_think(text), text)
        for not_or_over in ("", "plain words", "<think>done</think> the answer", "<THINK>done</THINK>", "the answer </think>",
                            "These models put their reasoning after a <think> tag.", "x<think>"):
            self.assertFalse(runner.in_think(not_or_over), not_or_over)


class MaxTokensRule(unittest.TestCase):
    """Max tokens: Auto or a number up to 1M; auto-compress follows the real window."""

    def test_auto_and_numbers(self):
        import runner
        for v in (None, "", 0, "auto", "Auto"):
            self.assertIsNone(runner.max_tokens_of(v))
        self.assertEqual(runner.max_tokens_of("4096"), 4096)

    def test_an_engines_refusal_of_a_long_prompt_in_each_engines_words(self):
        import runner
        import upstream
        # llama.cpp b12626, word for word: its body, and the sentence in it that the runner is given
        llama = ('{"error":{"code":400,"message":"request (188014 tokens) exceeds the available context size (131072 tokens), '
                 'try increasing it","type":"exceed_context_size_error","n_prompt_tokens":188014,"n_ctx":131072}}')
        said = upstream.message(llama)
        self.assertEqual(said, "request (188014 tokens) exceeds the available context size (131072 tokens), try increasing it")
        self.assertEqual(runner.parse_ctx_error(said), {"win": 131072, "prompt": 188014, "bound": False})
        # only where it opens the message: behind other words it could be a line somebody quoted
        for before in ("litellm.BadRequestError: OpenAIException - ", "my log said: ", "a pasted line - "):
            self.assertIsNone(runner.parse_ctx_error(before + said), before)
        old = ("This model's maximum context length is 16384 tokens. However, you requested 20000 tokens (8000 in the "
               "messages, 12000 in the completion).")
        self.assertEqual(runner.parse_ctx_error(old), {"win": 16384, "prompt": 8000, "bound": False})
        new = ("maximum context length is 131072 tokens ... your prompt contains at least 130001 input tokens")
        self.assertEqual(runner.parse_ctx_error(new), {"win": 131072, "prompt": 130001, "bound": True})
        for other in ("", None, "the model is loading", "request (12 tokens) is fine",
                      # a refusal of something else that quotes those words; a window nothing could fit; a prompt that fits it
                      "tool arguments are not valid JSON: 'request (9000 tokens) exceeds the available context size (2000 tokens)'",
                      "request (20 tokens) exceeds the available context size (9 tokens), try increasing it",
                      "request (500 tokens) exceeds the available context size (4096 tokens), try increasing it"):
            self.assertIsNone(runner.parse_ctx_error(other), other)

    def test_compress_limit(self):
        import runner
        win = 262144
        self.assertEqual(runner.compress_limit(win, None), int(win * 0.8))            # Auto: 80% of the window
        self.assertEqual(runner.compress_limit(win, 8192), int(win * 0.8))            # a small answer: still 80%
        self.assertEqual(runner.compress_limit(win, 1048576), win - win // 2 - 4096)  # 1M reserves half, not all
        self.assertEqual(runner.compress_limit(32768, None), int(32768 * 0.8))        # a small window: 80% of it, sooner

    def test_prompt_counts_are_kept_for_the_newest_sessions_only(self):
        import runner
        r = runner.Runner(None)
        for n in range(runner.CALIB_KEEP + 40):               # an agent's wake has a session of its own: one each
            r._calibrate("ag-ops-w%d" % n, {"model": "m", "tokens": 100 + n, "chars": 320})
        r._calibrate("ag-ops-w45", {"model": "m", "tokens": 7, "chars": 22})     # seen again: it is the newest now
        self.assertEqual(len(r.calib), runner.CALIB_KEEP)
        self.assertNotIn("ag-ops-w0", r.calib)
        self.assertEqual(r.calib["ag-ops-w45"]["tokens"], 7)
        self.assertEqual(list(r.calib)[-1], "ag-ops-w45")

    def test_bb_flag(self):
        import argparse
        import bb
        self.assertEqual(bb.max_tokens_arg("auto"), "auto")
        self.assertEqual(bb.max_tokens_arg("1048576"), "1048576")
        for bad in ("0", "-5", "lots"):
            with self.assertRaises(argparse.ArgumentTypeError):
                bb.max_tokens_arg(bad)
