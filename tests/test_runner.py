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
        cls.eng = FakeEngine(models=[{"id": MODEL, "max_model_len": 32768}, {"id": "tiny-model"}]).start()
        cls.data = tempfile.mkdtemp()
        cls.srv = Server(cls.data, {
            "gateways": [{"name": "fake", "url": cls.eng.url, "key": "", "enabled": True}],
            "mcp_servers": {"fake": {"command": sys.executable, "args": [os.path.join(HERE, "fakes", "fake_mcp.py")]}},
            "model_capabilities": {"tiny-model": {"ctx": 1200}}})
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
        self.assertEqual((sent["temperature"], sent["top_p"], sent["max_tokens"], sent["top_k"]), (0.7, 0.95, 8192, 40))
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


if __name__ == "__main__":
    unittest.main()
