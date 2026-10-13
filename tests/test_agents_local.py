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


if __name__ == "__main__":
    unittest.main()
