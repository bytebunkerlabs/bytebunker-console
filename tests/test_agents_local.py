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
            models, model_map = [{"id": "qwen"}], {"qwen": "this computer"}

            def refresh(self, force=False):
                pass

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
            models = [{"id": m} for m in gateways]
            model_map = {m: g["name"] for m, g in gateways.items()}

            def refresh(self, force=False):
                pass

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

    def test_a_model_served_nowhere_is_never_sent_to_the_first_gateway(self):
        fast = self.spec({"slave_model": "qwen3.8-27b", "harness": {"pool": True},
                          "minion_pool": [{"model": "qwen-box"}]}, self.GWS)
        self.assertEqual(fast, {"model": "qwen3.8-27b"})          # no base_url: no guessed engine

    def test_a_goal_does_not_start_with_an_unserved_model(self):
        import server
        saved = server.CFG, server.GW

        class GW:
            models, model_map = [{"id": "qwen-mac"}], {"qwen-mac": "this computer"}

            def refresh(self, force=False):
                pass
        server.CFG = {"agents": {"enabled": True, "dir": tempfile.mkdtemp(), "master_model": "glm",
                                 "slave_model": "qwen3.8-27b", "thinking_model": "qwen-mac"}}
        server.GW = GW()
        try:
            self.assertEqual(server.unserved_roles(), [("the master", "glm"), ("the minions", "qwen3.8-27b")])
            with self.assertRaises(agents.AgentConfigError) as e:
                server.agents_ready("a goal")
            self.assertIn("the minions' model qwen3.8-27b is not served anywhere right now", str(e.exception))
        finally:
            server.CFG, server.GW = saved

    def test_the_workers_own_defaults_are_checked_when_a_run_leaves_a_role_to_them(self):
        import server
        saved = server.CFG, server.GW
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "config"))
        with open(os.path.join(d, "config", "config.yaml"), "w") as f:
            f.write('llm:\n  base_url: "http://127.0.0.1:4000/v1"\n  master_model: "deepseek"  # old\n'
                    '  default_slave_model: qwen3-4b-fast\n  # thinking_model: "x"\nconsole_url: ""\n')
        with open(os.path.join(d, "config", "config.yaml")) as f:
            self.assertEqual(agents.parse_worker_models(f.read()),
                             {"master": "deepseek", "thinking": "deepseek", "fast": "qwen3-4b-fast"})

        class GW:
            models, model_map = [{"id": "glm"}], {"glm": "spark-1"}

            def refresh(self, force=False):
                pass
        server.GW = GW()
        try:
            # an old harness (no run spec): every role falls to config.yaml, even the ones chosen here
            server.CFG = {"agents": {"enabled": True, "dir": d, "master_model": "glm", "harness": {}}}
            with self.assertRaises(agents.AgentConfigError) as e:
                server.agents_ready("a goal")
            self.assertIn("the minions (the worker's config.yaml)' model qwen3-4b-fast", str(e.exception))
            self.assertIn("Work here updates it", str(e.exception))
            # a harness that takes the spec: only the blank roles fall back
            server.CFG = {"agents": {"enabled": True, "dir": d, "master_model": "glm", "thinking_model": "glm",
                                     "harness": {"spec": True}}}
            self.assertEqual(server.unserved_worker_defaults(),
                             [("the minions (the worker's config.yaml)", "qwen3-4b-fast")])
            server.CFG["agents"]["slave_model"] = "glm"
            self.assertEqual(server.unserved_worker_defaults(), [])
            server.agents_ready("a goal")
        finally:
            server.CFG, server.GW = saved

    def test_a_remote_worker_skips_this_machines_loopback(self):
        fast = self.spec({"ssh": "me@worker", "slave_model": "qwen-mac", "harness": {"pool": True},
                          "minion_pool": [{"model": "qwen-box"}, {"model": "spark-1", "slots": 2}]}, self.GWS)
        self.assertEqual([e["name"] for e in fast["pool"]], ["box", "spark-1"])


class GoalAnswer(unittest.TestCase):
    """The Agents screen's Answer: what was asked, the master's answer, how it ended."""

    def test_the_latest_goals_answer(self):
        import json
        d = tempfile.mkdtemp()
        g = os.path.join(d, "trajectories", "agents-abc123")
        os.makedirs(g)
        open(os.path.join(d, "trajectories", "spawns.jsonl"), "w").close()
        with open(os.path.join(g, "events.jsonl"), "w") as f:
            for e in ({"type": "goal_accepted", "goal_id": "agents-abc123", "ts": 1.0, "text": "versions?"},
                      {"type": "slave_spawned", "goal_id": "agents-abc123", "ts": 2.0},
                      {"type": "final_answer", "goal_id": "agents-abc123", "ts": 9.0, "text": "| a | b |\n|---|---|"},
                      {"type": "run_end", "goal_id": "agents-abc123", "ts": 9.5, "outcome": "answered",
                       "reason": "", "elapsed_s": 8.5}):
                f.write(json.dumps(e) + "\n")
        out = agents.recent_slaves({"agents": {"dir": d}})
        a = out["answer"]
        self.assertEqual((a["goal"], a["text"], a["outcome"], a["elapsed_s"]), ("versions?", "| a | b |\n|---|---|", "answered", 8.5))


class Thinking(unittest.TestCase):
    def test_efforts_and_the_masters_planning_reach_the_spec(self):
        home = tempfile.mkdtemp()
        os.environ.setdefault("BYTEBUNKER_DATA", os.path.join(home, "data"))
        os.environ.setdefault("BYTEBUNKER_CONFIG", os.path.join(home, "config.json"))
        import server

        class GW:
            models, model_map = [{"id": "glm"}, {"id": "qwen"}], {"glm": "spark-1", "qwen": "mac"}

            def refresh(self, force=False):
                pass

            def resolve(self, model):
                return model, {"name": "x", "url": "http://10.0.0.1:8888/v1"}
        saved = server.CFG, server.GW
        server.CFG = {"agents": {"master_model": "glm", "slave_model": "qwen", "master_effort": "high",
                                 "master_plan_effort": "off", "fast_effort": "low", "harness": {"plan": True}}}
        server.GW = GW()
        try:
            roles = server.agents_spec()["roles"]
            self.assertEqual((roles["master"]["effort"], roles["master"]["plan_effort"], roles["fast"]["effort"]),
                             ("high", "off", "low"))
            server.CFG["agents"]["harness"] = {}
            self.assertNotIn("plan_effort", server.agents_spec()["roles"]["master"])   # an older harness
        finally:
            server.CFG, server.GW = saved


if __name__ == "__main__":
    unittest.main()
