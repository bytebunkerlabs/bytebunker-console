"""/api/chat through upstream.py, against the fake engine: the stream is
relayed as it comes, a server that refuses vLLM extras gets one retry
without them, errors arrive in the server's own words, and a stream that
stops mid-reply says so instead of passing for a complete answer."""
import glob
import gzip
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "fakes"))
from test_api import Server  # noqa: E402
from fake_engine import FakeEngine  # noqa: E402


class ChatProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = FakeEngine(models=[{"id": "fake-model", "max_model_len": 32768}]).start()
        cls.data = tempfile.mkdtemp()
        cls.srv = Server(cls.data, {"gateways": [
            {"name": "fake", "url": cls.eng.url, "key": "", "enabled": True},
            {"name": "gone", "url": "http://127.0.0.1:9/v1", "key": "", "enabled": True}]})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.eng.stop()
        shutil.rmtree(cls.data, ignore_errors=True)

    def chat(self, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=30)
        h = {"Host": "127.0.0.1:%d" % self.srv.port, "Content-Type": "application/json"}
        h.update(headers or {})
        c.request("POST", "/api/chat", body=json.dumps(body), headers=h)
        r = c.getresponse()
        raw = r.read().decode()
        c.close()
        events = []
        for line in raw.splitlines():
            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                events.append(json.loads(line[5:].strip()))
        return r, raw, events

    def text(self, events):
        return "".join(((e.get("choices") or [{}])[0].get("delta") or {}).get("content") or "" for e in events)

    def traces(self):
        out = []
        for p in glob.glob(os.path.join(self.data, "traces", "*.jsonl*")):
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt") as f:
                out += [json.loads(x) for x in f if x.strip()]
        return [t for t in out if t.get("kind") == "chat"]

    def test_stream_is_relayed_and_traced(self):
        self.eng.script([{"content": "hello there, friend"}])
        r, raw, events = self.chat({"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
                                   {"X-BB-Session": "s-proxy", "X-BB-Turn": "t-1"})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("X-BB-Trace"))
        self.assertEqual(self.text(events), "hello there, friend")
        self.assertTrue(any(e.get("usage") for e in events))          # usage requested for the client
        sent = self.eng.requests[-1]["body"]
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        self.assertNotIn("error", raw)
        t = [x for x in self.traces() if x.get("session") == "s-proxy"][-1]
        self.assertEqual((t["turn"], t["gateway"], t["status"]), ("t-1", "fake", 200))
        self.assertEqual(t["response"]["content"], "hello there, friend")
        self.assertEqual(t["id"], r.getheader("X-BB-Trace"))

    def test_refused_extras_are_stripped_for_one_retry(self):
        self.eng.script([{"status": 400, "error": "Unrecognized request argument supplied: reasoning_effort"},
                         {"content": "fine without it"}])
        n = len(self.eng.requests)
        r, raw, events = self.chat({"model": "fake-model", "reasoning_effort": "high", "top_k": 20,
                                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.text(events), "fine without it")
        first, second = self.eng.requests[n]["body"], self.eng.requests[n + 1]["body"]
        self.assertIn("reasoning_effort", first)
        for k in ("reasoning_effort", "top_k", "stream_options"):
            self.assertNotIn(k, second)

    def test_errors_come_in_the_servers_own_words(self):
        self.eng.script([{"overflow": {"ctx": 32768, "requested": 40000}}])
        r, raw, _ = self.chat({"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status, 400)
        msg = json.loads(raw)["error"]
        self.assertTrue(msg.startswith("This model's maximum context length is 32768 tokens"), msg)

    def test_unreachable_gateway_is_a_502(self):
        r, raw, _ = self.chat({"model": "anything@gone", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status, 502)
        self.assertTrue(json.loads(raw)["error"])
        self.assertEqual(self.traces()[-1]["gateway"], "gone")       # the trace says which one

    def test_a_stream_cut_mid_reply_says_so(self):
        self.eng.script([{"content": "this reply will not finish", "cut": 3}])
        r, raw, events = self.chat({"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status, 200)
        self.assertTrue(events[-1].get("error", "").startswith("the model server closed the stream"), events[-1])


class ModelWatchTest(unittest.TestCase):
    def test_a_model_that_appears_is_announced(self):
        eng = FakeEngine(models=[{"id": "first-model", "max_model_len": 8192}]).start()
        data = tempfile.mkdtemp()
        srv = Server(data, {"gateways": [{"name": "fake", "url": eng.url, "key": "", "enabled": True}],
                            "models_watch_s": 2})
        try:
            seq = srv.request("GET", "/api/runs")[1]["seq"]
            # an app window listening; then an engine loads a second model
            got = []
            t = threading.Thread(target=lambda: got.extend(srv.sse(
                "/api/events?topics=models&after=%d" % seq, timeout=12,
                until=lambda fr: len(fr) >= 1)))
            t.start()
            time.sleep(2.5)                      # the watch has seen the first list
            eng.models.append({"id": "second-model", "max_model_len": 8192})
            t.join(15)
            self.assertTrue(got, "no models event")
            self.assertEqual(json.loads(got[0]["data"])["data"]["models"], 2)
            st, models = srv.request("GET", "/api/models")
            self.assertEqual(sorted(m["id"] for m in models["data"]), ["first-model", "second-model"])
        finally:
            srv.stop()
            eng.stop()
            shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
