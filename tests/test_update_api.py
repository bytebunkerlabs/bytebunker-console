"""The update routes on a desktop server: the check, why this one cannot take
an update, the changelog, the start-up check setting, and what the app says
after an update (or after one that did not finish)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import changelog  # noqa: E402
import updater  # noqa: E402
from test_api import Server  # noqa: E402
from test_update import FakeReleases  # noqa: E402
from version import VERSION  # noqa: E402


class UpdateRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.rel = FakeReleases()
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.stop()
        self.rel.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **env):
        self.srv = Server(self.data, {}, env=dict({"BYTEBUNKER_DESKTOP": "1",
                                                   "BYTEBUNKER_UPDATE_URL": self.rel.base + "/latest"}, **env))

    def test_a_server_from_a_checkout_says_why_it_cannot_update_itself(self):
        name = updater.asset_name("9.9.9") or "ByteBunker-9.9.9-mac-arm64.zip"
        self.rel.publish("9.9.9", {name: b"zip"})
        self.start()
        st, r = self.srv.request("GET", "/api/update/check")
        self.assertEqual(st, 200, r)
        self.assertEqual((r["current"], r["latest"], r["newer"], r["can_update"]), (VERSION, "9.9.9", True, False))
        self.assertIn("source checkout", r["why_not"])
        st, r = self.srv.request("POST", "/api/update/apply", {})
        self.assertEqual(st, 409)
        self.assertIn("source checkout", r["error"])

    def test_no_release_published_yet(self):
        self.start()
        st, r = self.srv.request("GET", "/api/update/check")
        self.assertEqual((st, r["none_published"], r["newer"]), (200, True, False))

    def test_the_changelog_and_the_start_up_check(self):
        self.start()
        st, r = self.srv.request("GET", "/api/changelog")
        self.assertEqual((r["current"], r["sections"][0]["version"]), (VERSION, VERSION))
        self.assertFalse(self.srv.request("GET", "/api/settings")[1]["update_check_at_start"])
        self.srv.request("POST", "/api/settings", {"update_check_at_start": True})
        self.assertTrue(self.srv.request("GET", "/api/settings")[1]["update_check_at_start"])

    def marker(self, to, log=None):
        home = os.path.join(self.data, "updates")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "pending.json"), "w") as f:
            json.dump({"from": "0.3.2", "to": to, "page": "https://example.invalid/r"}, f)
        if log:
            with open(os.path.join(home, "updater.log"), "w") as f:
                f.write(log + "\n")
        return home

    def test_after_an_update_it_says_so_once(self):
        home = self.marker(VERSION, "updated")
        self.start()
        s = self.srv.request("GET", "/api/settings")[1]
        self.assertEqual((s["updated"]["from"], s["updated"]["to"]), ("0.3.2", VERSION))
        self.assertEqual(s["updated"]["notes"], changelog.section(VERSION)["body"])
        self.assertIsNone(s["update_failed"])
        self.srv.request("POST", "/api/settings", {"action": "update_seen"})
        self.assertIsNone(self.srv.request("GET", "/api/settings")[1]["updated"])
        self.assertFalse(os.path.exists(os.path.join(home, "pending.json")))

    def test_an_update_that_did_not_finish_says_why(self):
        self.marker("9.9.9", "could not move the old app aside: nothing was changed")
        self.start()
        s = self.srv.request("GET", "/api/settings")[1]
        self.assertIsNone(s["updated"])
        self.assertEqual((s["update_failed"]["to"], s["update_failed"]["why"]),
                         ("9.9.9", "could not move the old app aside: nothing was changed"))


if __name__ == "__main__":
    unittest.main()
