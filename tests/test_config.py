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
        self.assertEqual(cfg["config_version"], 4)
        self.assertEqual([(g["name"], g["url"]) for g in cfg["gateways"]], [("upstream", "http://192.0.2.20:4000/v1")])
        self.assertNotIn("sparkdash_url", cfg)
        self.assertNotIn("telemetry_source", cfg)
        self.assertEqual(cfg["racks"], [])
        kept = glob.glob(self.path + ".before-v4-*")
        self.assertEqual(len(kept), 1)
        with open(kept[0]) as f:
            self.assertEqual(json.load(f)["sparkdash_url"], "http://old:8080")

    def test_the_one_rack_becomes_the_first_of_racks(self):
        self.start(json.dumps({"config_version": 2, "gateways": [], "monitors": [],
                               "rack": {"ssh": "you@spark-1", "dir": "~/dgx/dgx-serve"}}))
        cfg = self.disk()
        self.assertEqual(cfg["config_version"], 4)
        self.assertNotIn("rack", cfg)
        self.assertEqual(cfg["racks"], [{"name": "spark-1", "ssh": "you@spark-1", "dir": "~/dgx/dgx-serve"}])
        self.assertEqual(len(glob.glob(self.path + ".before-v4-*")), 1)
        st, d = self.srv.request("GET", "/api/racks")
        self.assertEqual([(r["name"], r["paired"]) for r in d["racks"]], [("spark-1", False)])

    def test_the_courts_model_picks_become_the_apps_roles(self):
        # 0.9.1: Agents, Settings had a model picker for the master, the thinking roles and the minions; the court
        # runs on the app's roles now. A pick moves to a role that names no model; a role that already names one
        # keeps it. Two keys nothing reads any more go with them
        self.start(json.dumps({"config_version": 3, "gateways": [], "monitors": [], "racks": [],
                               "roles": {"fast": "my-fast"}, "prometheus_url": "http://127.0.0.1:19090",
                               "litellm": {"ssh": "you@spark-1", "config_path": "/x.yaml"},
                               "agents": {"enabled": False, "master_model": "big-one", "thinking_model": "",
                                          "slave_model": "other-fast", "master_name": "Sultan"}}))
        cfg = self.disk()
        self.assertEqual(cfg["config_version"], 4)
        self.assertEqual(cfg["roles"], {"fast": "my-fast", "big": "big-one"})
        self.assertEqual(cfg["agents"], {"enabled": False, "master_name": "Sultan"})
        self.assertNotIn("prometheus_url", cfg)
        self.assertNotIn("litellm", cfg)
        kept = glob.glob(self.path + ".before-v4-*")
        self.assertEqual(len(kept), 1)
        with open(kept[0]) as f:
            self.assertEqual(json.load(f)["agents"]["slave_model"], "other-fast")     # what was there is not lost
        st, d = self.srv.request("GET", "/api/agents")
        self.assertNotIn("master_model", d)
        # no worker is set up here: the line shows the roles a worker would be handed
        self.assertEqual((d["harness_state"], d["court_models"], d["roles_unused"]),
                         ("", {"master": "big-one", "thinking": "", "fast": "my-fast"}, []))

    def test_a_pick_that_a_role_already_answers_is_said_not_swallowed(self):
        # the master was picked as one model and the big role names another: the role stays (profiles, jobs and bb
        # use it too), the court follows it, and the app says so once with the name of the file it kept
        self.start(json.dumps({"config_version": 3, "gateways": [], "monitors": [], "racks": [],
                               "roles": {"big": "role-big", "fast": "  ", "thinking": "same"},
                               "agents": {"master_model": " picked-master ", "slave_model": " picked-fast ",
                                          "thinking_model": "same", "master_name": "Sultan"}}))
        cfg = self.disk()
        self.assertEqual(cfg["roles"], {"big": "role-big", "fast": "picked-fast", "thinking": "same"})   # spaces go; an empty role takes the pick
        self.assertEqual(cfg["agents"], {"master_name": "Sultan"})
        st, shown = self.srv.request("GET", "/api/config")
        said = [w for w in shown["warnings"] if "The court's model picks moved to your roles" in w]
        self.assertEqual(len(said), 1, shown["warnings"])
        self.assertIn("did not move, and the role stays as it is: the master ran on picked-master, and the big role names role-big.", said[0])
        # one pick lost to its role: the one that moved and the one that already equalled its role are not news
        self.assertEqual((said[0].count(" ran on "), "picked-fast" in said[0], "same" in said[0]), (1, False, False))
        kept = glob.glob(self.path + ".before-v4-*")
        self.assertEqual(len(kept), 1)
        self.assertIn(os.path.basename(kept[0]), said[0])

    def test_a_worker_too_old_for_the_roles_is_shown_as_keeping_its_own_models(self):
        # a harness that takes no run spec is never handed the roles: the screen says what it will really run on
        def shown(agents):
            self.srv.stop() if getattr(self, "srv", None) else None
            self.start(json.dumps({"config_version": 4, "gateways": [], "monitors": [], "racks": [],
                                   "roles": {"big": "glm", "fast": "qwen"}, "agents": agents}))
            st, d = self.srv.request("GET", "/api/agents")
            return d["harness_state"], d["court_models"], d["roles_unused"], d["unserved"]
        own, roles = {"master": "", "thinking": "", "fast": ""}, {"master": "glm", "thinking": "", "fast": "qwen"}
        gone = {"master": "glm", "fast": "qwen"}                       # no gateway here serves anything
        # asked, and too old
        self.assertEqual(shown({"enabled": True, "ssh": "you@worker", "dir": "~/h", "harness": {"checked": 1, "pool": False}}),
                         ("old", own, ["master", "fast"], {}))
        # on this computer, and its script cannot be read: a goal takes that as old, and so does the screen
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertEqual(shown({"enabled": True, "dir": empty}), ("unread", own, ["master", "fast"], {}))
        # over ssh, never asked: a goal asks it first, so the roles are shown, and the page says nobody has asked
        self.assertEqual(shown({"enabled": True, "ssh": "you@worker", "dir": "~/h"}), ("unasked", roles, [], gone))
        # asked, and it takes a run spec
        self.assertEqual(shown({"enabled": True, "ssh": "you@worker", "dir": "~/h", "harness": {"checked": 1, "spec": True}}),
                         ("spec", roles, [], gone))
        # a harness on this computer is read at every look, whatever was remembered of it
        os.makedirs(os.path.join(empty, "scripts"))
        with open(os.path.join(empty, "scripts", "run_master.py"), "w") as f:
            f.write("p.add_argument('--goal-id')\n")
        self.assertEqual(shown({"enabled": True, "dir": empty, "harness": {"checked": 1, "spec": True}})[0], "old")
        with open(os.path.join(empty, "scripts", "run_master.py"), "a") as f:
            f.write("p.add_argument('--spec')\n")
        self.assertEqual(shown({"enabled": True, "dir": empty})[0], "spec")

    def test_an_odd_old_file_still_starts(self):
        # hand-edited files: roles that is not an object, a pick that is not text, a version written as text
        self.start(json.dumps({"config_version": "3", "gateways": [], "monitors": [], "racks": [], "roles": "big",
                               "agents": {"master_model": ["glm"], "thinking_model": True, "slave_model": "qwen"}}))
        cfg = self.disk()
        self.assertEqual(cfg["config_version"], 4)
        self.assertEqual(cfg["roles"], {"fast": "qwen"})               # what was not a model's name is not made into one
        self.assertEqual(cfg["agents"], {})
        st, d = self.srv.request("GET", "/api/agents")
        self.assertEqual(st, 200, d)

    def test_what_is_not_a_models_name_gives_way(self):
        # a version nobody can read is an old file, not the newest; a role that is not text gives way to the pick
        self.start(json.dumps({"config_version": "three", "gateways": [], "monitors": [], "racks": [],
                               "roles": {"big": 7, "fast": None, "vision": "v"},
                               "agents": {"master_model": "glm", "slave_model": "qwen"}}))
        cfg = self.disk()
        self.assertEqual((cfg["config_version"], cfg["roles"], cfg["agents"]),
                         (4, {"big": "glm", "fast": "qwen", "vision": "v"}, {}))
        st, shown = self.srv.request("GET", "/api/config")
        self.assertFalse(any(" ran on " in w for w in shown["warnings"]), shown["warnings"])

    def test_agents_and_roles_that_are_not_objects_name_nothing(self):
        # hand-edited into text or a list, in an old file or a current one, at the start or while the app runs:
        # the screens that read them answer, and the app's own defaults stand
        for version in (3, 4):
            self.srv.stop() if getattr(self, "srv", None) else None
            self.start(json.dumps({"config_version": version, "gateways": [], "monitors": [], "racks": [], "roles": "big", "agents": ["x"]}))
            for path in ("/api/hello", "/api/agents", "/api/config"):
                st, d = self.srv.request("GET", path)
                self.assertEqual(st, 200, (version, path))
            self.assertEqual(d["roles"], {}, version)
        time.sleep(1.1)
        with open(self.path, "w") as f:
            json.dump({"config_version": 4, "gateways": [], "monitors": [], "racks": [], "roles": ["big"], "agents": "x"}, f)
        st, d = self.srv.request("POST", "/api/settings", {"roles": {"fast": "qwen"}})
        self.assertEqual(st, 200, d)
        st, d = self.srv.request("GET", "/api/agents")
        self.assertEqual((st, d["court_models"]), (200, {"master": "", "thinking": "", "fast": "qwen"}))
        self.assertEqual((self.disk()["roles"], self.disk()["agents"].get("enabled")), ({"fast": "qwen"}, False))

    def test_an_older_file_put_back_while_the_app_runs_is_kept_too(self):
        self.start(json.dumps({"config_version": 4, "gateways": [], "monitors": [], "racks": []}))
        self.assertEqual(glob.glob(self.path + ".before-v4-*"), [])
        time.sleep(0.05)
        with open(self.path, "w") as f:                                 # a 0.9.0 file restored by hand
            json.dump({"config_version": 3, "gateways": [], "monitors": [], "racks": [], "roles": {"big": "role-big"},
                       "prometheus_url": "http://127.0.0.1:19090", "agents": {"master_model": "picked", "slave_model": "qwen"}}, f)
        st, _ = self.srv.request("POST", "/api/settings", {"user": "ada"})
        self.assertEqual(st, 200)
        after = self.disk()
        self.assertEqual((after["config_version"], after["roles"], after["agents"]), (4, {"big": "role-big", "fast": "qwen"}, {}))
        kept = glob.glob(self.path + ".before-v4-*")
        self.assertEqual(len(kept), 1)
        with open(kept[0]) as f:
            self.assertEqual(json.load(f)["agents"]["master_model"], "picked")
        st, shown = self.srv.request("GET", "/api/config")
        self.assertEqual(sum("the master ran on picked" in w for w in shown["warnings"]), 1, shown["warnings"])
        # put back once more, with nothing that clashes: a second copy, and the first note is not said again
        time.sleep(1.1)                                                # the copy's name holds the second
        with open(self.path, "w") as f:
            json.dump({"config_version": 3, "gateways": [], "monitors": [], "racks": [], "agents": {"slave_model": "qwen"}}, f)
        self.srv.request("POST", "/api/settings", {"user": "bo"})
        st, shown = self.srv.request("GET", "/api/config")
        self.assertEqual(sum(" ran on " in w for w in shown["warnings"]), 1, shown["warnings"])
        self.assertEqual(len(glob.glob(self.path + ".before-v4-*")), 2)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs a folder the user cannot write")
    def test_a_config_that_cannot_be_rewritten_does_not_stop_the_app(self):
        # 0.9.1 brings every file up to date at its first start: where the file cannot be written the app runs on
        # what it worked out, and says so
        cfgdir = os.path.join(self.data, "ro")
        os.makedirs(cfgdir)
        path = os.path.join(cfgdir, "config.json")
        with open(path, "w") as f:
            json.dump({"config_version": 3, "gateways": [], "monitors": [], "racks": [], "roles": {"big": "other"},
                       "agents": {"master_model": "glm"}}, f)
        os.chmod(cfgdir, 0o555)
        try:
            self.srv = Server(self.data, keep_config=True, env={"BYTEBUNKER_CONFIG": path})
            st, shown = self.srv.request("GET", "/api/config")
            self.assertEqual(st, 200)
            self.assertTrue(any("could not be rewritten" in w for w in shown["warnings"]), shown["warnings"])
            # no copy could be made there, and the warning does not name one
            moved = [w for w in shown["warnings"] if " ran on " in w]
            self.assertEqual(len(moved), 1, shown["warnings"])
            self.assertIn("No copy of your settings from before could be made; the file is as it was.", moved[0])
            self.assertNotIn("before-v4", moved[0])
            self.assertEqual(os.listdir(cfgdir), ["config.json"])
            st, d = self.srv.request("GET", "/api/settings")
            with open(path) as f:
                self.assertEqual(json.load(f)["config_version"], 3)     # the file is as it was
        finally:
            os.chmod(cfgdir, 0o755)

    def test_an_edit_made_while_running_survives_a_change_in_the_app(self):
        self.start(json.dumps({"config_version": 2, "gateways": [], "monitors": []}))
        cfg = self.disk()
        cfg["model_capabilities"] = {"my-model": {"ctx": 8192}}          # typed by hand
        time.sleep(1.1)                                                 # a second copy would get its own name
        with open(self.path, "w") as f:
            json.dump(cfg, f)
        st, _ = self.srv.request("POST", "/api/settings", {"user": "ada", "host": "lab"})
        self.assertEqual(st, 200)
        after = self.disk()
        self.assertEqual(after["model_capabilities"], {"my-model": {"ctx": 8192}})
        self.assertEqual(after["identity"], {"user": "ada", "host": "lab"})
        self.assertEqual(glob.glob(self.path + ".replaced-*"), [])
        self.assertEqual(len(glob.glob(self.path + ".before-v4-*")), 1)     # the start's own: an edit of an up-to-date file keeps none

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
