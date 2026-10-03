"""Gateways: config migration, merged model list, routing with pins, and
discovery — with the network faked out, so this runs anywhere (stdlib
unittest, Python 3.9+): python3 -m unittest discover -s tests"""

import io
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gateways as gw  # noqa: E402


def caps(mid):
    return {"tools": True, "vision": "vision" in mid}


class FakeNet:
    """GET <url>/models → the ids listed for that base URL."""

    def __init__(self, table):
        self.table = table

    def __call__(self, url, key=None, timeout=6):
        base = url.rsplit("/models", 1)[0]
        if base not in self.table:
            raise OSError("connection refused")
        return {"data": [{"id": i, "owned_by": "test"} for i in self.table[base]]}


class NormalizeTest(unittest.TestCase):
    def test_legacy_upstream_becomes_a_gateway(self):
        cfg = {"upstream_url": "http://10.0.0.5:4000", "upstream_key": "k"}
        out = gw.normalize(cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "upstream")
        self.assertEqual(out[0]["url"], "http://10.0.0.5:4000/v1")
        self.assertEqual(out[0]["key"], "k")
        self.assertIn("gateways", cfg)

    def test_explicit_empty_list_stays_empty(self):
        cfg = {"gateways": [], "upstream_url": "http://127.0.0.1:8000/v1"}
        self.assertEqual(gw.normalize(cfg), [])
        self.assertEqual(cfg["upstream_url"], "")

    def test_url_forms(self):
        self.assertEqual(gw._norm_url("1.2.3.4:8001"), "http://1.2.3.4:8001/v1")
        self.assertEqual(gw._norm_url("https://a.example/v1/"), "https://a.example/v1")
        self.assertEqual(gw._norm_url("  "), "")

    def test_first_enabled_is_mirrored(self):
        cfg = {"gateways": [{"name": "a", "url": "h1:1", "enabled": False},
                            {"name": "b", "url": "h2:2", "key": "kb"}]}
        gw.normalize(cfg)
        self.assertEqual(cfg["upstream_url"], "http://h2:2/v1")
        self.assertEqual(cfg["upstream_key"], "kb")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"gateways": [
            {"name": "router", "url": "http://r:4000/v1", "kind": "litellm"},
            {"name": "gpu", "url": "http://g:8001/v1", "kind": "vllm"},
            {"name": "off", "url": "http://o:9/v1", "kind": "vllm", "enabled": False},
        ]}
        self.real = gw.get_json
        gw.get_json = FakeNet({"http://r:4000/v1": ["big", "small"],
                               "http://g:8001/v1": ["small"],
                               "http://o:9/v1": ["small"]})
        self.reg = gw.Registry(self.cfg, caps)
        self.reg.refresh(force=True)

    def tearDown(self):
        gw.get_json = self.real

    def test_merge_first_gateway_wins_and_pins_exist(self):
        ids = [m["id"] for m in self.reg.models]
        self.assertEqual(ids.count("small"), 1)
        self.assertIn("small@gpu", ids)
        self.assertNotIn("small@off", ids)              # disabled gateways are not probed
        small = next(m for m in self.reg.models if m["id"] == "small")
        self.assertEqual(small["gateway"], "router")
        self.assertEqual(small["also_on"], ["gpu"])

    def test_resolve(self):
        self.assertEqual(self.reg.resolve("small")[1]["name"], "router")
        mid, g = self.reg.resolve("small@gpu")
        self.assertEqual((mid, g["name"]), ("small", "gpu"))

    def test_at_sign_is_only_a_pin_for_a_real_enabled_gateway(self):
        mid, g = self.reg.resolve("vendor/model@2024")
        self.assertEqual(mid, "vendor/model@2024")
        self.assertEqual(g["name"], "router")            # unknown → first enabled gateway
        mid, g = self.reg.resolve("small@off")
        self.assertEqual(g["name"], "router")

    def test_unreachable_gateway_reports_error(self):
        gw.get_json = FakeNet({"http://r:4000/v1": ["big"]})
        self.reg.refresh(force=True)
        self.assertFalse(self.reg.status["gpu"]["ok"])
        self.assertTrue(self.reg.status["gpu"]["error"])
        self.assertEqual(self.reg.status["gpu"]["url"], "http://g:8001/v1")


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.real = gw.get_json

    def tearDown(self):
        gw.get_json = self.real

    def _raise(self, code, body):
        def f(url, key=None, timeout=6):
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(body))
        return f

    def test_401_json_means_an_engine_behind_a_key(self):
        gw.get_json = self._raise(401, b'{"error": {"message": "Authentication Error"}}')
        r = gw._probe_endpoint("10.0.0.1", 4000)
        self.assertTrue(r and r["needs_key"])

    def test_401_html_is_a_login_page_not_an_engine(self):
        gw.get_json = self._raise(401, b"<html><body>Router login</body></html>")
        self.assertIsNone(gw._probe_endpoint("10.0.0.1", 8080))

    def test_https_on_443_has_no_port_in_url(self):
        gw.get_json = lambda url, key=None, timeout=6: {"data": [{"id": "m"}]}
        real_kind = gw.detect_kind
        gw.detect_kind = lambda url, owned=None: "vllm"
        try:
            r = gw._probe_endpoint("box.tail.ts.net", 443, None, "https")
        finally:
            gw.detect_kind = real_kind
        self.assertEqual(r["url"], "https://box.tail.ts.net/v1")


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.saved = (gw._open, gw._probe_endpoint, gw.tailscale_peers)

    def tearDown(self):
        gw._open, gw._probe_endpoint, gw.tailscale_peers = self.saved

    def test_targets_flags_and_order(self):
        opened = {("127.0.0.1", 11434), ("100.64.0.2", 8888), ("box.tail.ts.net", 443), ("10.0.0.5", 4000)}
        gw.tailscale_peers = lambda with_names=False: (["100.64.0.2"], ["box.tail.ts.net"]) if with_names else ["100.64.0.2"]
        gw._open = lambda h, p, timeout=0.5: (h, p) in opened

        def probe(h, p, key=None, scheme="http"):
            url = ("https://%s/v1" % h) if scheme == "https" else ("http://%s:%d/v1" % (h, p))
            return {"url": url, "host": h, "port": p, "needs_key": False, "models": ["x"], "kind": "vllm"}
        gw._probe_endpoint = probe
        cfg = {"gateways": [{"name": "r", "url": "http://10.0.0.5:4000/v1"}]}
        res = gw.discover(cfg, include_tailnet=True)
        urls = [f["url"] for f in res["found"]]
        self.assertIn("https://box.tail.ts.net/v1", urls)
        self.assertIn("http://127.0.0.1:11434/v1", urls)
        self.assertEqual(urls[-1], "http://10.0.0.5:4000/v1")      # already configured sorts last
        self.assertTrue(res["found"][-1]["configured"])
        self.assertEqual(res["hosts_probed"], 3)                     # localhost, the known host, one peer


if __name__ == "__main__":
    unittest.main()
