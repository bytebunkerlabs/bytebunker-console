"""config.json: one writer, a versioned shape, and nothing a person typed is
ever silently lost: an older file is migrated with the original kept, an
edit made while the server runs is picked up before the next change, and a
file that does not parse is kept aside rather than overwritten."""
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
from test_api import Server  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.path = os.path.join(self.data, "config.json")

    def tearDown(self):
        if getattr(self, "srv", None):
            self.srv.stop()
        shutil.rmtree(self.data, ignore_errors=True)

    def start(self, raw):
        """A server whose config.json starts as exactly `raw`."""
        with open(self.path, "w") as f:
            f.write(raw)
        self.srv = Server(self.data, keep_config=True)

    def disk(self):
        with open(self.path) as f:
            return json.load(f)

    def test_an_older_config_is_migrated_and_the_original_kept(self):
        self.start(json.dumps({"upstream_url": "http://192.0.2.20:4000/v1", "upstream_key": "k",
                               "sparkdash_url": "http://old:8080", "telemetry_source": "prometheus",
                               "monitors": []}))
        cfg = self.disk()
        self.assertEqual(cfg["config_version"], 2)
        self.assertEqual([(g["name"], g["url"]) for g in cfg["gateways"]], [("upstream", "http://192.0.2.20:4000/v1")])
        self.assertNotIn("sparkdash_url", cfg)
        self.assertNotIn("telemetry_source", cfg)
        kept = glob.glob(self.path + ".before-v2-*")
        self.assertEqual(len(kept), 1)
        with open(kept[0]) as f:
            self.assertEqual(json.load(f)["sparkdash_url"], "http://old:8080")

    def test_an_edit_made_while_running_survives_a_change_in_the_app(self):
        self.start(json.dumps({"config_version": 2, "gateways": [], "monitors": []}))
        cfg = self.disk()
        cfg["model_capabilities"] = {"my-model": {"ctx": 8192}}          # typed by hand
        time.sleep(0.05)
        with open(self.path, "w") as f:
            json.dump(cfg, f)
        st, _ = self.srv.request("POST", "/api/settings", {"user": "ada", "host": "lab"})
        self.assertEqual(st, 200)
        after = self.disk()
        self.assertEqual(after["model_capabilities"], {"my-model": {"ctx": 8192}})
        self.assertEqual(after["identity"], {"user": "ada", "host": "lab"})
        self.assertEqual(glob.glob(self.path + ".replaced-*"), [])

    def test_a_half_written_edit_is_kept_aside_not_overwritten(self):
        self.start(json.dumps({"config_version": 2, "gateways": [], "monitors": []}))
        time.sleep(0.05)
        with open(self.path, "w") as f:
            f.write('{"gateways": [ {"name": "half')                     # an editor mid-save, or a typo
        st, _ = self.srv.request("POST", "/api/settings", {"user": "ada"})
        self.assertEqual(st, 200)
        self.assertEqual(self.disk()["identity"]["user"], "ada")            # the app's file is valid again
        kept = glob.glob(self.path + ".replaced-*")
        self.assertEqual(len(kept), 1)
        with open(kept[0]) as f:
            self.assertEqual(f.read(), '{"gateways": [ {"name": "half')
        st, cfg = self.srv.request("GET", "/api/config")
        self.assertTrue(any("could not read" in w for w in cfg["warnings"]), cfg["warnings"])

    def test_an_unreadable_file_at_start_is_kept_and_the_app_still_starts(self):
        self.start("{ this is not json")
        st, cfg = self.srv.request("GET", "/api/config")
        self.assertEqual(st, 200)
        self.assertTrue(any("could not be read" in w for w in cfg["warnings"]), cfg["warnings"])
        kept = glob.glob(self.path + ".unreadable-*")
        self.assertEqual(len(kept), 1)
        with open(kept[0]) as f:
            self.assertEqual(f.read(), "{ this is not json")
        st, _ = self.srv.request("POST", "/api/settings", {"user": "ada"})     # the first save
        self.assertEqual(self.disk()["identity"]["user"], "ada")
        self.assertEqual(glob.glob(self.path + ".replaced-*"), [])            # one copy of the broken file, not two


if __name__ == "__main__":
    unittest.main()
