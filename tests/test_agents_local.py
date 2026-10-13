"""Agents on this computer: the harness starts as the app's child, so it must not
inherit the app's own BYTEBUNKER_CONFIG (its config.json), which the harness
would read in place of its config/config.yaml."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agents  # noqa: E402


@unittest.skipIf(sys.platform == "win32", "a run on this computer starts through env; on Windows agents work in WSL2")
class LocalRun(unittest.TestCase):
    def test_the_harness_does_not_see_the_apps_config(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "probe.py"), "w") as f:
            f.write("import os\nprint('config=' + os.environ.get('BYTEBUNKER_CONFIG', 'unset'))\n"
                    "print('master=' + os.environ.get('BYTEBUNKER_MASTER_MODEL', ''))\n")
        cfg = {"agents": {"enabled": True, "dir": d, "python": sys.executable, "script": "probe.py",
                          "master_model": "m1"}}
        cmd = agents.build_command(cfg, "a goal", caps={})
        env = dict(os.environ, BYTEBUNKER_CONFIG="/somewhere/config.json")
        out = subprocess.run(cmd, cwd=d, env=env, capture_output=True, text=True, timeout=30).stdout
        self.assertIn("config=unset", out)
        self.assertIn("master=m1", out)               # this run's own settings still arrive


class Caps(unittest.TestCase):
    def test_a_harness_on_this_computer_is_read_fresh(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "scripts"))
        os.makedirs(os.path.join(d, "src", "bytebunker_agents", "harness"))
        with open(os.path.join(d, "scripts", "run_master.py"), "w") as f:
            f.write("--goal-id --spec --proto --timeout-s --court")
        cfg = {"agents": {"enabled": True, "dir": d, "harness": {"spec": True, "pool": False}}}
        self.assertFalse(agents.harness_caps(cfg).get("pool"))
        open(os.path.join(d, "src", "bytebunker_agents", "harness", "engine_pool.py"), "w").close()
        self.assertTrue(agents.harness_caps(cfg).get("pool"))        # updated: seen without a Test


class LoopbackModels(unittest.TestCase):
    """A model on this machine's 127.0.0.1: agents here can reach it, a worker over ssh cannot."""

    def spec(self, ssh):
        home = tempfile.mkdtemp()
        os.environ.setdefault("BYTEBUNKER_DATA", os.path.join(home, "data"))
        os.environ.setdefault("BYTEBUNKER_CONFIG", os.path.join(home, "config.json"))
        import server

        class GW:
            def resolve(self, model):
                return model, {"url": "http://127.0.0.1:8888/v1", "key": "k"}
        saved = server.CFG, server.GW
        server.CFG, server.GW = {"agents": {"ssh": ssh, "slave_model": "qwen"}}, GW()
        try:
            return server.agents_spec()["roles"]["fast"]
        finally:
            server.CFG, server.GW = saved

    def test_this_computer_gets_the_address_and_a_worker_does_not(self):
        self.assertEqual(self.spec(""), {"model": "qwen", "base_url": "http://127.0.0.1:8888/v1", "api_key": "k"})
        self.assertEqual(self.spec("me@worker"), {"model": "qwen"})


class MinionPool(unittest.TestCase):
    """The minions' engines: the doers' model and the ones added, each with its gateway."""

    def spec(self, agents, gateways):
        home = tempfile.mkdtemp()
        os.environ.setdefault("BYTEBUNKER_DATA", os.path.join(home, "data"))
        os.environ.setdefault("BYTEBUNKER_CONFIG", os.path.join(home, "config.json"))
        import server

        class GW:
            def resolve(self, model):
                mid, _, gw = model.partition("@")
                g = gateways.get(gw or mid)
                return mid, g
        saved = server.CFG, server.GW
        server.CFG, server.GW = {"agents": agents}, GW()
        try:
            return (server.agents_spec() or {}).get("roles", {}).get("fast")
        finally:
            server.CFG, server.GW = saved

    GWS = {"qwen-mac": {"name": "this computer", "url": "http://127.0.0.1:8888/v1", "key": "k1"},
           "qwen-box": {"name": "box", "url": "http://10.0.0.7:8888/v1", "key": "k2"},
           "spark-1": {"name": "spark-1", "url": "http://10.0.0.11:8888/v1"}}

    def test_two_engines_make_a_pool_with_their_keys(self):
        fast = self.spec({"slave_model": "qwen-mac", "minion_slots": 4, "harness": {"pool": True},
                          "minion_pool": [{"model": "qwen-box", "slots": 8}]}, self.GWS)
        self.assertEqual(fast["pool"], [
            {"name": "this computer", "model": "qwen-mac", "base_url": "http://127.0.0.1:8888/v1", "slots": 4, "api_key": "k1"},
            {"name": "box", "model": "qwen-box", "base_url": "http://10.0.0.7:8888/v1", "slots": 8, "api_key": "k2"}])

    def test_no_pool_for_an_old_harness_or_one_engine(self):
        for agents in ({"slave_model": "qwen-mac", "harness": {}, "minion_pool": [{"model": "qwen-box"}]},
                       {"slave_model": "qwen-mac", "harness": {"pool": True}, "minion_pool": []}):
            fast = self.spec(agents, self.GWS)
            self.assertNotIn("pool", fast)
            self.assertEqual(fast["model"], "qwen-mac")

    def test_a_remote_worker_skips_this_machines_loopback(self):
        fast = self.spec({"ssh": "me@worker", "slave_model": "qwen-mac", "harness": {"pool": True},
                          "minion_pool": [{"model": "qwen-box"}, {"model": "spark-1", "slots": 2}]}, self.GWS)
        self.assertEqual([e["name"] for e in fast["pool"]], ["box", "spark-1"])


if __name__ == "__main__":
    unittest.main()
