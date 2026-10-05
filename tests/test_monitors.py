"""monitors.py: URL parsing, merging, auth errors, discovery, engine totals.
Fake rack monitors run on 127.0.0.1; no network beyond loopback.

    python3 -m unittest discover -s tests
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import monitors as monmod  # noqa: E402


def node(name, host=None, gen=None, running=0, ok=True):
    n = {"schema": 1, "name": name, "role": "node", "ok": ok, "system": {"hostname": host or name},
         "gpus": [{"name": "NVIDIA GB10", "util": 10}], "engines": []}
    if gen is not None:
        n["engines"].append({"kind": "vllm", "port": 8888, "ok": True, "gen_tps": gen, "prompt_tps": 100.0, "running": running})
    return n


class FakeMonitor:
    """Answers like rackmon.py: /v1/hello open, /v1/cluster behind a token."""

    def __init__(self, token, nodes, cluster="rack", schema=1):
        self.hits = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/hello":
                    return self._send(200, {"service": "rack-monitor", "version": "1.0.0", "schema": schema,
                                            "name": nodes[0]["name"], "role": "head", "cluster": cluster,
                                            "peers": len(nodes) - 1})
                if self.path.startswith("/v1/cluster"):
                    if self.headers.get("Authorization") != "Bearer " + token:
                        return self._send(401, {"error": "token required"})
                    fake.hits += 1
                    return self._send(200, {"service": "rack-monitor", "schema": schema, "cluster": cluster,
                                            "head": nodes[0]["name"], "version": "1.0.0",
                                            "nodes": json.loads(json.dumps(nodes))})
                self._send(404, {"error": "not found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ParseUrl(unittest.TestCase):
    def test_forms(self):
        p = monmod.parse_url
        self.assertEqual(p("100.90.164.11"), ("http://100.90.164.11:9177", ""))
        self.assertEqual(p("burhan.tailed338.ts.net:9177"), ("http://burhan.tailed338.ts.net:9177", ""))
        self.assertEqual(p("http://10.0.0.5:9000/v1/cluster?x=1"), ("http://10.0.0.5:9000", ""))
        self.assertEqual(p("https://mon.example"), ("https://mon.example", ""))
        self.assertEqual(p("http://[fd7a::1]:9177"), ("http://[fd7a::1]:9177", ""))
        # what `rack monitor token` prints as the paste-ready string
        self.assertEqual(p("http://rack:s3cr%2Ft-_x@100.90.164.11:9177"), ("http://100.90.164.11:9177", "s3cr/t-_x"))
        for bad in ("", None, "ftp://x", "http://", "http://host:99999"):
            self.assertEqual(p(bad), ("", ""), bad)

    def test_normalize(self):
        cfg = {"monitors": [{"name": "a", "url": "h1", "token": "t"},
                            {"url": "http://rack:tok@h2:9177", "enabled": False},
                            {"name": "broken", "url": ""}, "junk"]}
        out = monmod.normalize(cfg)
        self.assertEqual([m["url"] for m in out], ["http://h1:9177", "http://h2:9177"])
        self.assertEqual(out[1]["token"], "tok")            # token carried in the URL
        self.assertFalse(out[1]["enabled"])
        self.assertEqual(out[1]["name"], "h2:9177")


class Merge(unittest.TestCase):
    def setUp(self):
        self.a = FakeMonitor("ta", [node("spark-1", "burhan", gen=27.5, running=2), node("spark-2", "aleem")])
        # the second monitor sees spark-2 again (someone added the worker too)
        self.b = FakeMonitor("tb", [node("box", gen=3.0, running=1), node("spark-2", "aleem")], cluster="lab")
        self.dead = FakeMonitor("x", [node("gone")])
        self.dead.close()

    def tearDown(self):
        self.a.close()
        self.b.close()

    def test_cluster_merges_dedupes_and_reports(self):
        cfg = {"monitors": [{"name": "rack", "url": self.a.url, "token": "ta"},
                            {"name": "lab", "url": self.b.url, "token": "tb"},
                            {"name": "old", "url": self.dead.url, "token": "x"},
                            {"name": "off", "url": "127.0.0.1:1", "enabled": False}]}
        m = monmod.Monitors(cfg)
        d = m.cluster(history=10)
        self.assertEqual(d["configured"], 4)
        self.assertEqual(d["enabled"], 3)
        self.assertEqual([n["name"] for n in d["nodes"]], ["spark-1", "spark-2", "box"])
        self.assertEqual(d["duplicates"], 1)
        self.assertEqual({n["name"]: n["monitor"] for n in d["nodes"]}, {"spark-1": "rack", "spark-2": "rack", "box": "lab"})
        st = {s["name"]: s for s in d["monitors"]}
        self.assertTrue(st["rack"]["ok"] and st["lab"]["ok"])
        self.assertFalse(st["old"]["ok"])
        self.assertIn("refused", st["old"]["error"])
        # cached: a second call inside max_age does not refetch
        hits = self.a.hits
        m.cluster(history=10)
        self.assertEqual(self.a.hits, hits)
        # engine totals across monitors, for the playground's busy hint
        e = m.engine_stats()
        self.assertEqual((e["ok"], e["rate"], e["running"]), (True, 30.5, 3))

    def test_check_explains(self):
        ok = monmod.check(self.a.url, "ta")
        self.assertTrue(ok["ok"])
        self.assertEqual([n["name"] for n in ok["nodes"]], ["spark-1", "spark-2"])
        bad = monmod.check(self.a.url, "wrong")
        self.assertEqual((bad["ok"], bad["stage"]), (False, "auth"))
        self.assertIn("token", bad["error"])
        gone = monmod.check(self.dead.url, "x")
        self.assertEqual(gone["stage"], "reach")
        # something else answering HTTP there: say so, do not call it a monitor
        notmon = monmod.check(self.a.url + "/elsewhere", "ta")       # path is dropped: still the monitor
        self.assertTrue(notmon["ok"])

    def test_not_a_monitor(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(404)
                self.end_headers()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = monmod.check("http://127.0.0.1:%d" % srv.server_address[1], "t")
            self.assertEqual(r["stage"], "reach")
            self.assertIn("not a rack monitor", r["error"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_no_engines(self):
        m = monmod.Monitors({"monitors": []})
        self.assertEqual(m.engine_stats(), {"ok": False, "error": "no monitor configured"})

    def test_discover(self):
        cfg = {"monitors": [{"name": "rack", "url": self.a.url, "token": "ta"}]}
        r = monmod.discover(cfg, include_tailnet=False, port=self.b.port)
        found = {f["url"]: f for f in r["found"]}
        self.assertIn(self.b.url, found)                      # 127.0.0.1 is always asked
        self.assertEqual(found[self.b.url]["cluster"], "lab")
        self.assertFalse(found[self.b.url]["configured"])
        r = monmod.discover(cfg, include_tailnet=False, port=self.a.port)
        self.assertTrue(r["found"][0]["configured"])


if __name__ == "__main__":
    unittest.main()
