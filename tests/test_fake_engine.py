"""The fake engine itself: the other tests trust it, so pin its behaviour."""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tests.fakes.fake_engine import FakeEngine  # noqa: E402


def post(url, body, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    return urllib.request.urlopen(req, timeout=10)


def sse(resp):
    out = []
    for raw in resp:
        line = raw.decode().strip()
        if line.startswith("data:"):
            p = line[5:].strip()
            if p == "[DONE]":
                break
            out.append(json.loads(p))
    return out


class FakeEngineTest(unittest.TestCase):
    def setUp(self):
        self.eng = FakeEngine(models=[{"id": "m1", "max_model_len": 4096}]).start()

    def tearDown(self):
        self.eng.stop()

    def test_models_and_echo(self):
        with urllib.request.urlopen(self.eng.url + "/models", timeout=5) as r:
            d = json.load(r)
        self.assertEqual(d["data"][0]["id"], "m1")
        self.assertEqual(d["data"][0]["max_model_len"], 4096)
        with post(self.eng.url + "/chat/completions", {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}) as r:
            d = json.load(r)
        self.assertEqual(d["choices"][0]["message"]["content"], "echo: hi")
        self.assertEqual(self.eng.requests[-1]["body"]["model"], "m1")

    def test_stream_reasoning_tools_usage(self):
        self.eng.script([{"reasoning": "think", "content": "ok",
                          "tool_calls": [{"name": "fs__read", "arguments": {"path": "a.txt"}}]}])
        with post(self.eng.url + "/chat/completions", {"model": "m1", "stream": True,
                                                       "stream_options": {"include_usage": True},
                                                       "messages": [{"role": "user", "content": "x"}]}) as r:
            chunks = sse(r)
        text = "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c["choices"])
        reasoning = "".join((c["choices"][0]["delta"].get("reasoning_content") or "") for c in chunks if c["choices"])
        args = "".join(((c["choices"][0]["delta"].get("tool_calls") or [{}])[0].get("function") or {}).get("arguments") or ""
                       for c in chunks if c["choices"])
        self.assertEqual((text, reasoning, json.loads(args)), ("ok", "think", {"path": "a.txt"}))
        self.assertEqual([c["choices"][0]["finish_reason"] for c in chunks if c["choices"]][-1], "tool_calls")
        self.assertIn("usage", chunks[-1])

    def test_overflow_and_errors_and_key(self):
        self.eng.script([{"overflow": {"ctx": 4096, "requested": 5000}}, {"status": 503, "error": "busy"}])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            post(self.eng.url + "/chat/completions", {"model": "m1", "messages": []})
        self.assertEqual(cm.exception.code, 400)
        self.assertIn("maximum context length is 4096", cm.exception.read().decode())
        cm.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            post(self.eng.url + "/chat/completions", {"model": "m1", "messages": []})
        self.assertEqual(cm.exception.code, 503)
        cm.exception.close()
        locked = FakeEngine(api_key="k").start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post(locked.url + "/chat/completions", {"model": "fake-model", "messages": []})
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
            with post(locked.url + "/chat/completions", {"model": "fake-model", "messages": []}, key="k") as r:
                self.assertEqual(r.status, 200)
        finally:
            locked.stop()


if __name__ == "__main__":
    unittest.main()
