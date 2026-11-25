"""The model router: a pure function over cards, the live list and a role's needs."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import router  # noqa: E402

GLM = "glm-5.3-flash"
QWEN = "qwen3-4b-fast"
A3 = "deepseek-v4-flash"
VIS = "deepseek-v4-vision"
MAC = "qwen3-8b-mac"


def cards():
    """This rack as it is known: what each model can do, measured where it was."""
    return {
        GLM: router.card(GLM, {"tools": True, "ctx": 131072, "effort": ["low", "high"]}, {"tok_s": 34.0, "last_live": 1000.0, "uncensored": True},
                         roles=("big", "thinking")),
        QWEN: router.card(QWEN, {"tools": True, "ctx": 24576}, {"tok_s": 81.0, "last_live": 900.0}, roles=("fast",)),
        A3: router.card(A3, {"tools": True, "ctx": 262144, "effort": ["max"]}, {"tok_s": 8.0}, roles=("big", "thinking")),
        VIS: router.card(VIS, {"tools": True, "ctx": 262144, "vision": True, "effort": ["max"]}, {"tok_s": 20.0},
                         roles=("big", "vision", "thinking")),
        MAC: router.card(MAC, {"tools": True, "ctx": 32768}, {"tok_s": 60.0}, roles=("fast",)),
    }


def live(*ids, starting=None, load=None, recipes=None, last=None):
    return {"models": [{"id": i, "gateway": "g"} for i in ids], "starting": starting or [], "load": load or {},
            "recipes": recipes or [], "last_recipe": last or {}}


RECIPES = [{"name": "glm53-flash-keys", "rack": "spark-1", "roles": ["big", "thinking"], "fits": True},
           {"name": "dsv4-flash", "rack": "spark-1", "roles": ["big", "thinking"], "fits": True},
           {"name": "too-big", "rack": "spark-1", "roles": ["big", "vision"], "fits": False}]
LAST = {"rack": "spark-1", "recipe": "glm53-flash-keys", "names": [GLM]}
LOADING = [{"recipe": "glm53-flash-keys", "gateway_name": GLM, "state": "loading", "minutes": 6.2, "node": "spark-1"}]
OPS = {"master": {"tools": True, "ctx_min": 32000, "tier": "big"}, "fast": {"speed": "fast"}}


class Cards(unittest.TestCase):
    def test_a_card_is_seeded_from_the_recipes_roles_and_the_caps_table(self):
        c = cards()
        self.assertEqual((c[GLM]["tier"], c[GLM]["tools"], c[GLM]["thinking"], c[GLM]["ctx"], c[GLM]["speed"], c[GLM]["uncensored"]),
                         ("big", True, True, 131072, "medium", True))
        self.assertEqual((c[QWEN]["tier"], c[QWEN]["speed"], c[QWEN]["thinking"]), ("small", "fast", False))
        self.assertEqual((c[A3]["speed"], c[VIS]["vision"]), ("slow", True))
        # without a learnt fact, the name says it
        self.assertEqual([router.card(m, {})["uncensored"] for m in ("some-model", "some-model-abliterated", "Some-Uncensored-8b")],
                         [False, True, True])

    def test_a_tier_is_guessed_from_a_name_when_nothing_else_is_known(self):
        for name, tier in (("qwen3-4b-fast", "small"), ("gemma-3-27b", "medium"), ("llama-3.3-70b", "big"),
                           ("glm-5.3-flash", "big"), ("phi-4", "medium"), ("Qwen3.8-0.6B", "small"),
                           ("deepseek-v4-flash", "big")):
            self.assertEqual(router.guess_tier(name), tier, name)
        self.assertEqual(router.guess_tier("mystery", roles=("fast",)), "small")
        self.assertEqual(router.guess_tier("mystery-4b", roles=("big",)), "big")        # the recipe knows better

    def test_a_model_served_with_a_reasoning_parser_thinks_whatever_its_size(self):
        self.assertTrue(router.card("glm-5.3-flash", {"reasoning": True})["thinking"])
        small = router.card("qwen3-4b-fast", {"reasoning": True})
        self.assertEqual((small["thinking"], small["tier"]), (True, "small"))      # thinking, and still a small model
        self.assertFalse(router.card("glm-5.3-flash", {})["thinking"])

    def test_what_a_person_set_or_was_measured_wins(self):
        c = router.card("phi-4", {"tools": True, "ctx": 16384}, {"tier": "small", "tools": False, "tok_s": 120})
        self.assertEqual((c["tier"], c["tools"], c["speed"]), ("small", False, "fast"))

    def test_speed_is_measured_when_it_can_be(self):
        self.assertEqual([router.speed_class(x) for x in (81, 34, 8)], ["fast", "medium", "slow"])
        self.assertEqual(router.speed_class(None, "small"), "fast")


class ThirtyRequests(unittest.TestCase):
    """Scripted role requests against this rack: each names what must be picked, or the move."""

    CASES = [
        # (what is live, role, needs, shape, expected model or move, extra)
        ("the rack as usual: the master gets GLM", (GLM, QWEN), "master", None, "chat", GLM, {}),
        ("thinking gets GLM", (GLM, QWEN), "thinking", None, "goal", GLM, {}),
        ("fast gets the small engine", (GLM, QWEN), "fast", None, "goal", QWEN, {}),
        ("the ops agent's own needs", (GLM, QWEN), "master", OPS["master"], "chat", GLM, {}),
        ("two big models: the less loaded one", (GLM, A3), "master", None, "chat", A3, {"load": {GLM: {"running": 4}}}),
        ("two big models, both idle: the wider window", (GLM, A3), "master", None, "chat", A3, {}),
        ("a fast role with two fast engines: the quicker", (QWEN, MAC, GLM), "fast", None, "goal", QWEN, {}),
        ("a fast role with the quick one busy still prefers speed", (QWEN, MAC), "fast", None, "goal", QWEN,
         {"load": {QWEN: {"running": 3}}}),
        ("vision asked: only the vision model fits", (GLM, VIS, QWEN), "master", {"vision": True}, "chat", VIS, {}),
        ("vision asked and none is live: never traded away", (GLM, QWEN), "master", {"vision": True}, "chat", "park", {}),
        ("GLM down, a chat ask: degrade to the small engine", (QWEN,), "master", OPS["master"], "chat", QWEN, {"flag": GLM}),
        ("GLM down, a scheduled goal: park (no degrade for goals)", (QWEN,), "master", OPS["master"], "goal", "park", {}),
        ("GLM loading, a scheduled goal: wait", (QWEN,), "master", OPS["master"], "goal", "wait", {"starting": LOADING}),
        ("GLM loading, a chat ask: answer now on the small engine", (QWEN,), "master", OPS["master"], "chat", QWEN,
         {"starting": LOADING, "flag": GLM}),
        ("GLM loading, a chat ask, nothing else live: wait", (), "master", OPS["master"], "chat", "wait", {"starting": LOADING}),
        ("GLM down, a recipe could serve it, a goal: ask for the switch", (QWEN,), "master", OPS["master"], "goal", "ask",
         {"recipes": RECIPES, "last": LAST, "recipe": "glm53-flash-keys"}),
        ("nothing live at all, a chat ask, a recipe known: ask", (), "master", None, "chat", "ask",
         {"recipes": RECIPES, "last": LAST, "recipe": "glm53-flash-keys"}),
        ("nothing live, nothing loading, no recipe: park", (), "master", None, "chat", "park", {}),
        ("a model set by hand and live", (GLM, QWEN), "master", None, "chat", QWEN, {"pin": QWEN}),
        ("a model set by hand and down, chat: degrade and say which", (QWEN,), "master", None, "chat", QWEN,
         {"pin": A3, "flag": A3}),
        ("a model set by hand and loading, goal: wait for that one", (QWEN,), "master", None, "goal", "wait",
         {"pin": GLM, "starting": LOADING}),
        ("a pinned gateway copy counts as live", (GLM,), "master", None, "chat", GLM + "@spark-1", {"pin": GLM + "@spark-1"}),
        ("thinking with GLM down, chat: a model without thinking, flagged", (QWEN,), "thinking", None, "chat", QWEN,
         {"flag": GLM}),
        ("fast with only GLM live: speed is a preference, the one model does it", (GLM,), "fast", None, "chat", GLM, {}),
        ("fast with only GLM live, a goal: the same, nothing parks", (GLM,), "fast", None, "goal", GLM, {}),
        ("thinking on a rack whose only model has no thinking switch", (QWEN,), "thinking", {"tier": "small"}, "goal", QWEN, {}),
        ("thinking prefers the model that can think", (GLM, MAC), "thinking", {"tier": "small"}, "goal", GLM, {}),
        ("a small window is given up last: the big model wins over a fast small one", (GLM, QWEN), "master",
         {"tools": True, "ctx_min": 100000}, "chat", GLM, {}),
        ("uncensored is a must-have", (A3, QWEN), "master", {"uncensored": True, "tier": "big"}, "chat", "park", {}),
        ("uncensored and live", (A3, GLM), "master", {"uncensored": True}, "chat", GLM, {}),
        ("a recipe that does not fit the machine is never proposed", (), "master", {"vision": True, "tier": "big"}, "goal",
         "park", {"recipes": RECIPES}),
        ("an agent that would rather wait than degrade, in chat", (QWEN,), "master", OPS["master"], "chat", "wait",
         {"starting": LOADING, "order": ["wait", "degrade", "park"]}),
        ("an agent allowed to degrade a goal", (QWEN,), "master", OPS["master"], "goal", QWEN,
         {"order": ["wait", "degrade", "park"], "flag": GLM}),
        ("a failed load is not a model that is loading", (QWEN,), "master", OPS["master"], "goal", "park",
         {"starting": [dict(LOADING[0], state="failed")]}),
    ]

    def test_each_request_picks_the_expected_model_or_move(self):
        self.assertGreaterEqual(len(self.CASES), 30)
        for title, ids, role, need, shape, want, x in self.CASES:
            lv = live(*ids, starting=x.get("starting"), load=x.get("load"), recipes=x.get("recipes"), last=x.get("last"))
            plan = router.route((role,), {role: need} if need else {}, cards(), lv, shape=shape, order=x.get("order"),
                                pins={role: x["pin"]} if x.get("pin") else None)
            with self.subTest(title):
                if want in router.MOVES:
                    self.assertFalse(plan["ok"], plan)
                    self.assertEqual(plan["move"], want, plan)
                    self.assertEqual(plan["role"], role)
                    self.assertTrue(plan["why"])
                    if x.get("recipe"):
                        self.assertEqual(plan["propose"]["recipe"], x["recipe"])
                else:
                    self.assertTrue(plan["ok"], plan)
                    self.assertEqual(plan["roles"][role]["model"], want, plan)
                    if x.get("flag"):
                        self.assertEqual(plan["degraded"], "on %s; %s was down" % (want, x["flag"]))
                    else:
                        self.assertIsNone(plan["degraded"], plan)


class Plans(unittest.TestCase):
    def test_a_goal_fills_three_roles_and_says_why(self):
        plan = router.route(router.ROLES, OPS, cards(), live(GLM, QWEN), shape="goal")
        self.assertTrue(plan["ok"])
        self.assertEqual({r: plan["roles"][r]["model"] for r in router.ROLES}, {"master": GLM, "thinking": GLM, "fast": QWEN})
        self.assertIn("the only live model that fits", plan["roles"]["master"]["why"])
        self.assertIn("131k window", plan["roles"]["master"]["why"])
        self.assertEqual(len(router.explain(plan)), 3)

    def test_the_fast_role_gets_a_pool_of_every_engine_as_quick_as_the_pick(self):
        plan = router.route(("fast",), {}, cards(), live(GLM, QWEN, MAC), shape="goal", slots={MAC: 8})
        self.assertEqual(plan["roles"]["fast"]["pool"], [{"model": QWEN, "slots": 4}, {"model": MAC, "slots": 8}])
        one = router.route(("fast",), {}, cards(), live(GLM, QWEN), shape="goal")
        self.assertNotIn("pool", one["roles"]["fast"])                  # the big engine is not a minion's

    def test_a_rack_with_one_model_runs_every_role_on_it_and_says_what_it_lacks(self):
        plan = router.route(router.ROLES, {}, cards(), live(QWEN), shape="goal", order=["degrade", "park"])
        self.assertEqual({r: plan["roles"][r]["model"] for r in router.ROLES}, {r: QWEN for r in router.ROLES})
        plan = router.route(("fast",), {}, cards(), live(GLM), shape="goal")
        self.assertTrue(plan["ok"])
        self.assertIn("it is medium, not fast", plan["roles"]["fast"]["why"])

    def test_the_first_role_without_a_model_decides_the_move(self):
        plan = router.route(router.ROLES, OPS, cards(), live(QWEN), shape="goal", )
        self.assertEqual((plan["ok"], plan["move"], plan["role"]), (False, "park", "master"))
        self.assertIn("glm-5.3-flash is not served", plan["why"])

    def test_waiting_says_how_long_the_model_has_been_loading(self):
        plan = router.route(("master",), OPS, cards(), live(QWEN, starting=LOADING), shape="goal")
        self.assertEqual(plan["notice"], "glm-5.3-flash is loading, 6 min so far")
        self.assertEqual(plan["retry_s"], 45)

    def test_a_degraded_answer_says_what_it_gave_up(self):
        plan = router.route(("master",), OPS, cards(), live(QWEN), shape="chat")
        why = plan["roles"]["master"]["why"]
        self.assertIn("a small model, not a big one", why)
        self.assertIn("a 25k window, not 32k", why)

    def test_a_model_never_seen_before_that_is_loading_is_judged_by_its_recipes_roles(self):
        starting = [{"recipe": "new-big", "gateway_name": "brand-new-model", "state": "loading", "minutes": 1, "roles": ["big"]}]
        plan = router.route(("master",), {}, cards(), live(QWEN, starting=starting), shape="goal")
        self.assertEqual(plan["move"], "wait")

    def test_a_models_failures_break_a_tie(self):
        c = cards()
        c[A3]["ctx"] = c[GLM]["ctx"]
        c[A3]["fail_rate"] = 0.4
        plan = router.route(("master",), {}, c, live(GLM, A3))
        self.assertEqual(plan["roles"]["master"]["model"], GLM)

    def test_a_role_that_prefers_speed_takes_the_quickest_model_that_can_do_the_job(self):
        need = {"master": {"tools": True, "prefer": "speed"}}                  # the concierge's
        plan = router.route(("master",), need, cards(), live(GLM, QWEN))
        self.assertEqual((plan["roles"]["master"]["model"], plan["degraded"]), (QWEN, None))
        self.assertEqual(router.route(("master",), need, cards(), live(GLM))["roles"]["master"]["model"], GLM)

    def test_a_rack_whose_biggest_model_is_a_medium_one_is_not_degraded(self):
        only = {"gemma-3-27b": router.card("gemma-3-27b", {"tools": True, "ctx": 32768})}
        for shape in ("chat", "goal"):
            plan = router.route(router.ROLES if shape == "goal" else ("master",), {}, only, live("gemma-3-27b"), shape=shape)
            self.assertTrue(plan["ok"], plan)
            self.assertIsNone(plan["degraded"])
            self.assertEqual(plan["roles"]["master"]["model"], "gemma-3-27b")
        # a tier asked for by name stays as asked
        plan = router.route(("master",), {"master": {"tier": "big"}}, only, live("gemma-3-27b"), shape="goal")
        self.assertEqual(plan["move"], "park")
        # and once a big model has been seen here, its absence is a degraded answer again
        both = dict(only, **{GLM: cards()[GLM]})
        plan = router.route(("master",), {}, both, live("gemma-3-27b"), shape="chat")
        self.assertEqual(plan["degraded"], "on gemma-3-27b; %s was down" % GLM)

    def test_a_turn_nobody_waits_for_waits_for_a_model_that_is_loading_then_settles(self):
        # a schedule, a mail, a feed: GLM loading and the small model live. An ask from a person runs on the small
        # one at once; a turn nobody waits for waits for GLM, and settles for the small one only when GLM is not coming
        need = {"master": OPS["master"]}
        asked = router.route(("master",), need, cards(), live(QWEN, starting=LOADING), shape="chat")
        self.assertEqual((asked["ok"], asked["roles"]["master"]["model"]), (True, QWEN))
        held = router.route(("master",), need, cards(), live(QWEN, starting=LOADING), shape="chat", order=router.ORDER["background"])
        self.assertEqual((held["ok"], held["move"]), (False, "wait"))
        later = router.route(("master",), need, cards(), live(QWEN), shape="chat", order=router.ORDER["background"])
        self.assertEqual((later["ok"], later["roles"]["master"]["model"]), (True, QWEN))
        self.assertIn(GLM, later["degraded"])

    def test_with_no_cards_at_all_it_parks_and_says_so(self):
        plan = router.route(("master",), {}, {}, live())
        self.assertEqual((plan["move"], plan["why"]), ("park", "master: no model is live"))


class Learned(unittest.TestCase):
    def test_a_failure_counts_for_a_model_this_app_has_seen_and_makes_no_card_of_a_name_it_never_served(self):
        import shutil
        import tempfile
        import model_facts
        d = tempfile.mkdtemp()
        try:
            f = model_facts.ModelFacts(os.path.join(d, "facts.json"))
            f.learn_outcome("deepseek-v4-flash-typo", False)        # a request to a name no gateway serves
            self.assertNotIn("deepseek-v4-flash-typo", f.facts)
            f.seen_live([GLM])
            f.learn_outcome(GLM, False)
            self.assertEqual(f.facts[GLM]["card"]["fail_rate"], 1.0)
            f.learn_outcome(GLM, True)
            self.assertEqual(f.facts[GLM]["card"]["fail_rate"], 0.9)
            again = model_facts.ModelFacts(os.path.join(d, "facts.json"))
            self.assertEqual(sorted(again.facts), [GLM])            # and nothing of the typo is on disk
            # so a rack whose biggest model is a medium one stays what it is: no unserved "big" card appears
            cs = {QWEN: cards()[QWEN]}
            need = router.needs_for("master")
            self.assertEqual(router.relax(need, None, cs).get("tier"), cs[QWEN].get("tier"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
