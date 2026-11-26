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
        cfg = {"roles": {"big": "role-model@lan"},
               "agents": {"enabled": True, "dir": d, "python": sys.executable, "script": "probe.py"}}
        env = dict(os.environ, BYTEBUNKER_CONFIG="/somewhere/config.json")
        # a run's own choice (a persistent agent's router made it) arrives, the router's id@gateway by its id
        cmd = agents.build_command(cfg, "a goal", caps={}, models={"master": "m1@spark-1"})
        out = subprocess.run(cmd, cwd=d, env=env, capture_output=True, text=True, timeout=30).stdout
        self.assertIn("config=unset", out)
        self.assertEqual(out.split("master=")[1].split()[0:1], ["m1"])
        # the app's roles never travel in the environment: a harness that reads its models there keeps its own
        # config (one that takes a run spec reads them in the spec)
        cmd = agents.build_command(cfg, "a goal", caps={})
        out = subprocess.run(cmd, cwd=d, env=env, capture_output=True, text=True, timeout=30).stdout
        self.assertEqual(out.split("master=")[1].strip(), "")

    def test_only_a_runs_own_models_are_put_in_the_environment(self):
        cfg = {"roles": {"big": "glm", "thinking": "glm", "fast": "qwen"},
               "agents": {"enabled": True, "dir": tempfile.mkdtemp(), "python": sys.executable, "script": "x.py"}}

        def models_in(cmd):
            return sorted(a for a in cmd if a.startswith("BYTEBUNKER_") and "_MODEL=" in a)
        self.assertEqual(models_in(agents.build_command(cfg, "a goal", caps={})), [])
        self.assertEqual(models_in(agents.build_command(cfg, "a goal", caps={"spec": True})), [])
        got = models_in(agents.build_command(cfg, "a goal", caps={}, models={"master": "a@lan", "thinking": " b ", "fast": "c"}))
        self.assertEqual(got, ["BYTEBUNKER_MASTER_MODEL=a", "BYTEBUNKER_SLAVE_MODEL=c", "BYTEBUNKER_THINKING_MODEL=b"])


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

    def test_a_harness_that_does_not_sign_its_lines_is_one_from_before_agents(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "scripts"))
        os.makedirs(os.path.join(d, "src", "bytebunker_agents"))
        with open(os.path.join(d, "scripts", "run_master.py"), "w") as f:
            f.write("--goal-id --spec --proto --timeout-s --court")
        cfg = {"agents": {"enabled": True, "dir": d}}
        spec = os.path.join(d, "src", "bytebunker_agents", "runspec.py")
        with open(spec, "w") as f:
            f.write("ALLOWED = {'agent_context', 'plan_effort'}\n")
        caps = agents.harness_caps(cfg)
        self.assertEqual((caps["spec"], caps["plan"], caps["agent"]), (True, True, False))
        with open(spec, "w") as f:
            f.write("ALLOWED = {'agent_context', 'plan_effort', 'proto_key'}\n")
        self.assertTrue(agents.harness_caps(cfg)["agent"])            # the run's key would be refused by the one before


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
        server.CFG, server.GW = {"roles": {"fast": "qwen"}, "agents": {"ssh": ssh}}, GW()
        try:
            return server.agents_spec()["roles"]["fast"]
        finally:
            server.CFG, server.GW = saved

    def test_this_computer_gets_the_address_and_a_worker_does_not(self):
        self.assertEqual(self.spec(""), {"model": "qwen", "base_url": "http://127.0.0.1:8888/v1", "api_key": "k"})
        self.assertEqual(self.spec("me@worker"), {"model": "qwen"})


class MinionPool(unittest.TestCase):
    """The minions' engines: the fast role's model and the ones added, each with its gateway."""

    def spec(self, fast, agents, gateways):
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
        server.CFG, server.GW = {"roles": {"fast": fast}, "agents": agents}, GW()
        try:
            return (server.agents_spec() or {}).get("roles", {}).get("fast")
        finally:
            server.CFG, server.GW = saved

    GWS = {"qwen-mac": {"name": "this computer", "url": "http://127.0.0.1:8888/v1", "key": "k1"},
           "qwen-box": {"name": "box", "url": "http://10.0.0.7:8888/v1", "key": "k2"},
           "spark-1": {"name": "spark-1", "url": "http://10.0.0.11:8888/v1"}}

    def test_two_engines_make_a_pool_with_their_keys(self):
        fast = self.spec("qwen-mac", {"minion_slots": 4, "harness": {"pool": True},
                                      "minion_pool": [{"model": "qwen-box", "slots": 8}]}, self.GWS)
        self.assertEqual(fast["pool"], [
            {"name": "this computer", "model": "qwen-mac", "base_url": "http://127.0.0.1:8888/v1", "slots": 4, "api_key": "k1"},
            {"name": "box", "model": "qwen-box", "base_url": "http://10.0.0.7:8888/v1", "slots": 8, "api_key": "k2"}])

    def test_no_pool_for_an_old_harness_or_one_engine(self):
        for agents in ({"harness": {}, "minion_pool": [{"model": "qwen-box"}]},
                       {"harness": {"pool": True}, "minion_pool": []}):
            fast = self.spec("qwen-mac", agents, self.GWS)
            self.assertNotIn("pool", fast)
            self.assertEqual(fast["model"], "qwen-mac")

    def test_a_model_served_nowhere_is_never_sent_to_the_first_gateway(self):
        fast = self.spec("qwen3.8-27b", {"harness": {"pool": True}, "minion_pool": [{"model": "qwen-box"}]}, self.GWS)
        self.assertEqual(fast, {"model": "qwen3.8-27b"})          # no base_url: no guessed engine

    def test_a_goal_does_not_start_with_an_unserved_model(self):
        import server
        saved = server.CFG, server.GW

        class GW:
            models, model_map = [{"id": "qwen-mac"}], {"qwen-mac": "this computer"}

            def refresh(self, force=False):
                pass
        server.CFG = {"roles": {"big": "glm", "fast": "qwen3.8-27b", "thinking": "qwen-mac"},
                      "agents": {"enabled": True, "dir": tempfile.mkdtemp(), "harness": {"spec": True}}}
        server.GW = GW()
        try:
            self.assertEqual(server.unserved_roles(), [("the master", "glm"), ("the minions", "qwen3.8-27b")])
            with self.assertRaises(agents.AgentConfigError) as e:
                server.agents_ready("a goal")
            self.assertIn("not started: the master's model glm is not served anywhere right now; "
                          "the minions' model qwen3.8-27b is not served anywhere right now.", str(e.exception))
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
            server.CFG = {"roles": {"big": "glm"}, "agents": {"enabled": True, "dir": d, "harness": {}}}
            with self.assertRaises(agents.AgentConfigError) as e:
                server.agents_ready("a goal")
            self.assertIn("the minions' model qwen3-4b-fast (the worker's config.yaml) is not served anywhere right now", str(e.exception))
            self.assertIn("Work here updates it", str(e.exception))
            # a harness that takes the spec: only the blank roles fall back
            server.CFG = {"roles": {"big": "glm", "thinking": "glm"},
                          "agents": {"enabled": True, "dir": d, "harness": {"spec": True}}}
            self.assertEqual(server.unserved_worker_defaults(),
                             [("the minions (the worker's config.yaml)", "qwen3-4b-fast")])
            server.CFG["roles"]["fast"] = "glm"
            self.assertEqual(server.unserved_worker_defaults(), [])
            server.agents_ready("a goal")
        finally:
            server.CFG, server.GW = saved

    def test_a_remote_worker_skips_this_machines_loopback(self):
        fast = self.spec("qwen-mac", {"ssh": "me@worker", "harness": {"pool": True},
                                      "minion_pool": [{"model": "qwen-box"}, {"model": "spark-1", "slots": 2}]}, self.GWS)
        self.assertEqual([e["name"] for e in fast["pool"]], ["box", "spark-1"])


class CourtModels(unittest.TestCase):
    """The court's master, thinking roles and minions run on the app's roles (Settings, Roles), unless a run
    brings its own models. What is checked before a goal starts follows what the worker is really handed."""

    def setUp(self):
        home = tempfile.mkdtemp()
        os.environ.setdefault("BYTEBUNKER_DATA", os.path.join(home, "data"))
        os.environ.setdefault("BYTEBUNKER_CONFIG", os.path.join(home, "config.json"))
        import server
        self.server = server
        case = self

        class GW:
            models, model_map = [{"id": "glm"}, {"id": "qwen"}], {"qwen@box": "box"}
            forced = 0

            def refresh(self, force=False):
                case.forced += bool(force)

            def resolve(self, model):
                mid, _, gw = model.partition("@")
                return mid, {"name": gw or "lan", "url": "http://10.0.0.%d:8888/v1" % (7 if gw else 5)}
        self.forced = 0
        self.saved = server.CFG, server.GW
        server.GW = GW()

    def tearDown(self):
        self.server.CFG, self.server.GW = self.saved

    def test_a_runs_own_choice_then_the_role_then_the_workers_default(self):
        server = self.server
        server.CFG = {"roles": {"big": " glm ", "fast": "qwen", "vision": "v"}}
        self.assertEqual(server.court_models(), {"master": "glm", "thinking": "", "fast": "qwen"})
        self.assertEqual(server.court_models({"master": "own", "fast": ""}), {"master": "own", "thinking": "", "fast": "qwen"})
        self.assertEqual(server.court_models({"thinking": "t"}, roles=False), {"master": "", "thinking": "t", "fast": ""})
        for none in ({}, {"roles": None}, {"roles": {}}):
            server.CFG = none
            self.assertEqual(server.court_models(), {"master": "", "thinking": "", "fast": ""})

    def test_a_role_is_checked_where_the_worker_is_handed_it(self):
        server = self.server
        spec, old = {"harness": {"spec": True}}, {"harness": {}}
        server.CFG = {"roles": {"big": " gone ", "thinking": "glm", "fast": "gone-too"}, "agents": spec}
        self.assertEqual(server.unserved_roles(), [("the master", "gone"), ("the minions", "gone-too")])
        # a harness that takes no run spec is not handed the roles: they are not what its goal would fail on
        server.CFG["agents"] = old
        self.assertEqual(server.unserved_roles(), [])
        # a run's own choice is handed to both kinds, so it always counts
        self.assertEqual(server.unserved_roles({"master": "other-gone", "fast": "qwen"}), [("the master", "other-gone")])
        server.CFG["agents"] = spec
        self.assertEqual(server.unserved_roles({"master": "glm", "fast": "qwen"}), [])

    def test_the_gateways_are_asked_afresh_once_however_many_models_are_missing(self):
        server = self.server
        server.CFG = {"roles": {"big": "gone", "thinking": "gone", "fast": "gone-too"},
                      "agents": {"harness": {"spec": True, "pool": True}, "minion_pool": [{"model": "gone-too"}, {"model": "third"}]}}
        self.assertEqual(server.unserved_roles(), [("the master", "gone"), ("the thinking roles", "gone"), ("the minions", "gone-too"),
                                                   ("a minion engine", "gone-too"), ("a minion engine", "third")])
        self.assertEqual(self.forced, 1)
        self.forced = 0
        self.assertEqual(server.not_served(["glm", " qwen ", "", None, "qwen@box"]), [])
        self.assertEqual(self.forced, 0)                              # nothing missing: nothing asked again

    def test_the_agents_screen_takes_one_look_however_much_is_missing(self):
        server = self.server
        server.CFG = {"roles": {"big": "gone", "thinking": "gone-2", "fast": "gone-3"}}
        a = {"ssh": "you@worker", "minion_pool": [{"model": "gone-4"}, {"model": "gone-5"}, {"model": " qwen@box "}]}
        st = server.court_status(a, {"spec": True, "pool": True})
        self.assertEqual(self.forced, 1)                              # five missing models, one fresh look
        self.assertEqual((st["harness_state"], st["court_models"], st["roles_unused"]),
                         ("spec", {"master": "gone", "thinking": "gone-2", "fast": "gone-3"}, []))
        self.assertEqual(st["unserved"], st["court_models"])
        self.assertEqual(st["pool_engines"], [{"name": "box", "model": "qwen", "slots": 4}])
        # a worker too old for the roles: none shown, none marked, each one that names a model listed
        self.forced = 0
        st = server.court_status(a, {"spec": False})
        self.assertEqual((st["harness_state"], st["court_models"], st["roles_unused"], st["unserved"], self.forced),
                         ("old", {"master": "", "thinking": "", "fast": ""}, ["master", "thinking", "fast"], {}, 1))
        # what is known of a harness, as a goal started now would find it
        for agents, caps, state in (({"ssh": "you@worker"}, {}, "unasked"), ({"dir": "~/h"}, {}, "unread"), ({}, {}, ""),
                                    ({"dir": "~/h"}, {"spec": True}, "spec"), ({"dir": "~/h"}, {"court": True}, "old"),
                                    ({"ssh": " ", "dir": " "}, {}, "")):
            self.assertEqual(server.harness_state(agents, caps), state, (agents, caps))

    def test_the_pool_is_built_from_one_look_on_the_names_the_check_reads(self):
        server = self.server
        a = {"minion_slots": 4, "minion_pool": [{"model": " qwen@box ", "slots": 8}, {"model": "gone"}, {"model": "gone-too"}]}
        pool = server.minion_pool(a, "glm", False)
        self.assertEqual([(e["name"], e["model"], e["slots"]) for e in pool], [("lan", "glm", 4), ("box", "qwen", 8)])
        self.assertEqual(self.forced, 1)                              # two missing engines, one fresh look
        self.forced = 0
        # a caller that has already looked hands over what it found: no look at all here
        self.assertEqual([e["model"] for e in server.minion_pool(a, "glm", False, gone={"gone", "gone-too", "glm"})], ["qwen"])
        self.assertEqual(self.forced, 0)

    def test_a_runs_own_pool_is_the_one_that_is_checked(self):
        server = self.server
        server.CFG = {"roles": {"big": "glm", "thinking": "glm", "fast": "qwen"},
                      "agents": {"enabled": True, "dir": tempfile.mkdtemp(), "harness": {"spec": True},
                                 "minion_pool": [{"model": "ticked-and-gone"}]}}
        saved_caps = server.agents_caps
        server.agents_caps = lambda: server.CFG["agents"]["harness"]     # as this test names them
        self.addCleanup(lambda: setattr(server, "agents_caps", saved_caps))
        # a harness that takes no pool is never handed the ticked engine: it is not what a goal there fails on
        server.agents_ready("a goal")
        server.CFG["agents"]["harness"] = {"spec": True, "pool": True}
        with self.assertRaises(agents.AgentConfigError) as e:
            server.agents_ready("a goal")
        said = str(e.exception)
        self.assertIn("a minion engine's model ticked-and-gone is not served anywhere right now", said)
        self.assertIn("a role's model in Settings, Roles (big, thinking, fast), a minion engine on Agents, Settings", said)
        # a persistent agent's run names its own engines: the ticked one is not its business
        server.agents_ready("a goal", {"master": "glm", "fast": "qwen", "pool": ["qwen@box"]})
        with self.assertRaises(agents.AgentConfigError) as e:
            server.agents_ready("a goal", {"master": "glm", "fast": "qwen", "pool": ["its-own-and-gone"]})
        self.assertIn("a minion engine's model its-own-and-gone is not served", str(e.exception))


class NamesThatWereMissing(unittest.TestCase):
    """Two names server.py used and never had: each was a NameError waiting for its moment."""

    def setUp(self):
        home = tempfile.mkdtemp()
        os.environ.setdefault("BYTEBUNKER_DATA", os.path.join(home, "data"))
        os.environ.setdefault("BYTEBUNKER_CONFIG", os.path.join(home, "config.json"))
        import server
        self.server = server

    def test_a_playground_tool_call_that_asks_first_has_a_run_to_wait_on(self):
        self.assertRegex(self.server._CallRun().id, r"^call-[0-9a-f]{10}$")

    def test_an_agents_goal_on_a_harness_from_before_agents_is_refused_in_words(self):
        server = self.server
        saved = server.agents_ready, server.agents_caps, server.CFG
        server.agents_ready = lambda *a, **k: None
        server.agents_caps = lambda: {"spec": True}                      # takes a spec, knows nothing of agents
        server.CFG = {"agents": {"enabled": True, "dir": tempfile.mkdtemp()}}
        try:
            with self.assertRaises(agents.AgentConfigError) as e:
                server.agent_goal(None, lambda *a, **k: None, "a goal", agent={"ctx": {"id": "ops"}})
            self.assertIn("predates agents that persist", str(e.exception))
        finally:
            server.agents_ready, server.agents_caps, server.CFG = saved


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
        server.CFG = {"roles": {"big": "glm", "fast": "qwen"},
                      "agents": {"master_effort": "high", "master_plan_effort": "off", "fast_effort": "low",
                                 "harness": {"plan": True}}}
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
