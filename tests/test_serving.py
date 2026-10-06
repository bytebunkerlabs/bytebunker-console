"""dgx-serve's serving block, read through a rack monitor: the model's
window, dialect and thinking switch come from what rack up recorded, and a
router's other routes are marked as not serving."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "fakes"))
from test_api import Server  # noqa: E402
from fake_engine import FakeEngine  # noqa: E402

SERVING = {"schema": 1, "recipe": "qwen3-8b", "platform": "dgx", "engine": "vllm", "served_name": "qwen3-8b",
           "model": "Qwen/Qwen3-8B", "port": 8888, "roles": ["chat", "tools", "reasoning"], "context": 40960,
           "tools": True, "vision": False, "gateway_name": "qwen-route",
           "dialect": {"thinking": "chat_template_kwargs.enable_thinking", "strip_reasoning": "1",
                       "effort": "low medium high", "min_max_tokens": "2048"}}


class FakeMonitor:
    def __init__(self):
        mon = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/v1/hello"):
                    body = {"service": "rack-monitor", "schema": 1, "name": "spark", "role": "head", "cluster": "t"}
                else:
                    body = {"service": "rack-monitor", "schema": 1, "cluster": "t", "head": "spark",
                            "nodes": [{"schema": 1, "name": "spark", "ok": True, "gpus": [], "engines": [],
                                       "serving": SERVING}]}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


class ServingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # a router that lists three routes: the served name, the name rack
        # gateway gave its route, and a route to nothing
        cls.eng = FakeEngine(models=[{"id": "qwen3-8b"}, {"id": "qwen-route"}, {"id": "old-route"}]).start()
        cls.mon = FakeMonitor()
        cls.data = tempfile.mkdtemp()
        cls.srv = Server(cls.data, {"gateways": [{"name": "router", "url": cls.eng.url, "key": "", "enabled": True,
                                                  "kind": "litellm"}],
                                    "monitors": [{"name": "rack", "url": cls.mon.url, "token": "", "enabled": True}]})
        cls.srv.request("GET", "/api/cluster")          # the app's Cluster screen fetches; facts come from that

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.eng.stop()
        cls.mon.srv.shutdown()
        shutil.rmtree(cls.data, ignore_errors=True)

    def turn(self, **kw):
        st, r = self.srv.request("POST", "/api/sessions/new/turns", dict({"text": "hi", "model": "qwen3-8b"}, **kw))
        self.assertEqual(st, 202, r)
        self.srv.sse("/api/runs/%s/events" % r["run"], timeout=20,
                     until=lambda fr: json.loads(fr[-1]["data"])["type"] == "finished")
        return self.eng.requests[-1]["body"]

    def test_the_serving_block_says_what_the_model_can_do(self):
        models = {m["id"]: m for m in self.srv.request("GET", "/api/models")[1]["data"]}
        caps = models["qwen3-8b"]["caps"]
        self.assertEqual((caps["ctx"], caps["strip_reasoning"], caps["effort"], caps["thinking_switch"]),
                         (40960, True, ["low", "medium", "high"], "chat_template_kwargs.enable_thinking"))
        self.assertEqual((models["qwen3-8b"]["served"], models["old-route"]["served"]), (True, False))

    def test_a_route_named_by_rack_gateway_is_the_served_model(self):
        # LiteLLM lists GATEWAY_NAME (glm-5.3-flash), the engine serves another name (glm5.3-flash)
        models = {m["id"]: m for m in self.srv.request("GET", "/api/models")[1]["data"]}
        self.assertIs(models["qwen-route"]["served"], True)
        self.assertEqual((models["qwen-route"]["caps"]["ctx"], models["qwen-route"]["caps"]["min_max_tokens"]),
                         (40960, 2048))

    def test_effort_off_turns_thinking_off_and_a_level_turns_it_on(self):
        self.eng.script([{"content": "a"}, {"content": "b"}, {"content": "c"}])
        off = self.turn(effort="off")
        self.assertIs(off["chat_template_kwargs"]["enable_thinking"], False)
        self.assertNotIn("reasoning_effort", off)
        high = self.turn(effort="high")
        self.assertIs(high["chat_template_kwargs"]["enable_thinking"], True)
        self.assertEqual(high["reasoning_effort"], "high")
        plain = self.turn()
        self.assertNotIn("chat_template_kwargs", plain)          # Default: the model's own default

    def test_a_model_that_thinks_first_gets_room(self):
        self.eng.script([{"content": "x"}])
        body = self.turn(params={"max_tokens": 256})
        self.assertEqual(body["max_tokens"], 2048)


if __name__ == "__main__":
    unittest.main()
