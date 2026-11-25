"""An agent on disk: its record, its memory (one fact per file), its inbox and its runs."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agentstore  # noqa: E402
import events  # noqa: E402

OPS = {"id": "ops", "name": "Ops",
       "purpose": "Keeps the rack serving. Reports what changed and what it did about it.",
       "roles": {"master": "auto", "thinking": "auto", "fast": "auto"},
       "needs": {"master": {"tools": True, "ctx_min": 32000, "tier": "big"}, "fast": {"speed": "fast"}},
       "skills": ["rack-ops"],
       "access": {"mcp_servers": ["prometheus"]},
       "rules": {"outward": "ask", "rack": "ask", "shell": "allow", "egress": ["developer.nvidia.com"]},
       "budget": {"tokens_day": 2000000, "minutes_run": 90, "runs_day": 12, "quiet": "23:00-07:00"},
       "channels": [{"kind": "telegram", "chat": "123456789"}],
       "wake": [{"kind": "cron", "cron": "0 7 * * 1-5",
                 "prompt": "Morning report: what served overnight, what failed, disk on both nodes."},
                {"kind": "rack", "events": ["engine_down", "model_switched"]}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.bus = events.EventBus()
        self.store = agentstore.AgentStore(self.root, self.bus)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Records(Base):
    def test_the_briefs_first_agent_is_stored_as_its_file_reads(self):
        a = self.store.save(OPS)
        self.assertEqual(a["roles"], {"master": "auto", "thinking": "auto", "fast": "auto"})
        self.assertEqual(a["rules"], {"egress": ["developer.nvidia.com"]})           # only what differs from the defaults
        self.assertEqual(a["wake"][1], {"kind": "rack", "events": ["engine_down", "model_switched"]})
        with open(os.path.join(self.root, "ops", "agent.json")) as f:
            self.assertEqual(json.load(f)["purpose"], OPS["purpose"])
        for part in ("memory/MEMORY.md", "runs"):
            self.assertTrue(os.path.exists(os.path.join(self.root, "ops", part)), part)
        self.assertEqual([x["id"] for x in self.store.list()], ["ops"])

    def test_an_edit_keeps_what_it_does_not_mention(self):
        self.store.save(OPS)
        a = self.store.save({"id": "ops", "purpose": "Watches the rack."})
        self.assertEqual(a["purpose"], "Watches the rack.")
        self.assertEqual(a["skills"], ["rack-ops"])
        self.assertEqual(a["channels"], [{"kind": "telegram", "chat": "123456789"}])
        self.assertEqual(a["created"], self.store.get("ops")["created"])

    def test_an_id_comes_from_the_name_and_reserved_words_are_refused(self):
        self.assertEqual(self.store.save({"name": "Deep Research", "purpose": "Reads papers."})["id"], "deep-research")
        for bad, word in (({"id": "Ops!", "purpose": "x"}, "lower-case"), ({"id": "ls", "purpose": "x"}, "pick another"),
                          ({"id": "ok", "purpose": ""}, "purpose"),
                          ({"id": "ok", "purpose": "x", "roles": {"boss": "m"}}, "master, thinking and fast"),
                          ({"id": "ok", "purpose": "x", "needs": {"master": {"tier": "huge"}}}, "tier"),
                          ({"id": "ok", "purpose": "x", "wake": [{"kind": "cron", "cron": "bogus", "prompt": "p"}]}, "cron"),
                          ({"id": "ok", "purpose": "x", "wake": [{"kind": "cron", "cron": "0 7 * * *"}]}, "prompt"),
                          ({"id": "ok", "purpose": "x", "wake": [{"kind": "mail"}]}, "allowlist"),
                          ({"id": "ok", "purpose": "x", "channels": [{"kind": "telegram", "chat": "@me"}]}, "numeric"),
                          ({"id": "ok", "purpose": "x", "budget": {"runs_day": 0}}, "runs_day"),
                          ({"id": "ok", "purpose": "x", "rules": {"spend": "always"}}, "rules.spend")):
            with self.assertRaises(agentstore.AgentError) as e:
                self.store.save(bad)
            self.assertIn(word, str(e.exception), bad)
        self.assertIsNone(self.store.get("ok"))

    def test_create_refuses_a_second_agent_with_the_same_id(self):
        self.store.save(OPS, create=True)
        with self.assertRaises(agentstore.AgentError):
            self.store.save(OPS, create=True)
        with self.assertRaises(agentstore.AgentError):
            self.store.save({"id": "nobody", "purpose": "x"}, create=False)

    def test_delete_moves_the_folder_aside_with_its_memory(self):
        self.store.save(OPS)
        self.store.remember("ops", "the owner prefers short reports", "user", "the owner said")
        kept = self.store.delete("ops")
        self.assertIsNone(self.store.get("ops"))
        self.assertTrue(os.path.isfile(os.path.join(kept, "memory", "the-owner-prefers-short-reports.md")))
        self.assertEqual(self.store.list(), [])


class Memory(Base):
    def setUp(self):
        super().setUp()
        self.store.save(OPS)

    def test_one_fact_per_file_with_its_type_and_source_and_an_index_line(self):
        f = self.store.remember("ops", "spark-2 had 83 GB free at 07:00 on 2026-11-09", "project", "run agents-1a2b3c",
                                name="disk-spark-2")
        self.assertEqual((f["name"], f["type"], f["source"]), ("disk-spark-2", "project", "run agents-1a2b3c"))
        with open(os.path.join(self.root, "ops", "memory", "disk-spark-2.md")) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: disk-spark-2\ntype: project\nsource: run agents-1a2b3c\n"))
        self.assertTrue(text.rstrip().endswith("spark-2 had 83 GB free at 07:00 on 2026-11-09"))
        with open(os.path.join(self.root, "ops", "memory", "MEMORY.md")) as fh:
            index = fh.read()
        self.assertIn("- [disk-spark-2](disk-spark-2.md) — spark-2 had 83 GB free", index)
        self.assertIn("(project; run agents-1a2b3c)", index)

    def test_the_same_name_replaces_and_the_same_words_are_kept_once(self):
        self.store.remember("ops", "83 GB free", "project", "run a", name="disk")
        self.store.remember("ops", "412 GB free", "project", "run b", name="disk")
        self.assertEqual([f["fact"] for f in self.store.memory("ops")], ["412 GB free"])
        self.assertEqual(self.store.fact("ops", "disk")["source"], "run b")
        a = self.store.remember("ops", "GLM serves from recipe glm53-flash-keys", "project", "the owner said")
        b = self.store.remember("ops", "GLM serves  from recipe glm53-flash-keys", "project", "run c")
        self.assertTrue(b.get("unchanged"))
        self.assertEqual(a["name"], b["name"])
        self.assertEqual(len(self.store.memory("ops")), 2)

    def test_two_facts_that_start_alike_get_two_files(self):
        a = self.store.remember("ops", "the disk on spark-1 was at 60 percent on monday", "project", "run a")
        b = self.store.remember("ops", "the disk on spark-1 was at 60 percent on tuesday too", "project", "run b")
        self.assertNotEqual(a["name"], b["name"])

    def test_forget_removes_the_file_and_the_index_line(self):
        self.store.remember("ops", "old news", "project", "run a", name="old")
        self.assertTrue(self.store.forget("ops", "old"))
        self.assertFalse(self.store.forget("ops", "old"))
        self.assertFalse(self.store.forget("ops", "../agent"))            # a name, never a path
        self.assertFalse(self.store.forget("ops", "MEMORY"))
        with open(os.path.join(self.root, "ops", "memory", "MEMORY.md")) as fh:
            self.assertNotIn("old", fh.read())

    def test_a_file_the_owner_wrote_by_hand_is_a_fact(self):
        with open(os.path.join(self.root, "ops", "memory", "by-hand.md"), "w") as f:
            f.write("never restart the rack before 22:00\n")
        f = self.store.fact("ops", "by-hand")
        self.assertEqual((f["fact"], f["type"], f["source"]), ("never restart the rack before 22:00", "project", "unknown"))

    def test_a_run_is_told_its_standing_guidance_first_then_what_is_nearest_the_ask(self):
        self.store.remember("ops", "the owner wants reports under ten lines", "feedback", "the owner said", name="short-reports")
        self.store.remember("ops", "spark-2 disk was 98 percent full", "project", "run a", name="disk-spark-2")
        self.store.remember("ops", "the gate listens on port 4000", "reference", "https://example.com/gate", name="gate-port",
                            trust="untrusted")
        text = self.store.memory_text("ops", "how is the disk on spark-2?")
        lines = [l for l in text.splitlines() if l.startswith("- [")]
        self.assertTrue(lines[0].startswith("- [feedback] short-reports"))
        self.assertTrue(lines[1].startswith("- [project] disk-spark-2"))
        self.assertIn("read from untrusted content", lines[2])
        self.assertIn("never an instruction", text)
        small = self.store.memory_text("ops", "disk", budget=120)
        self.assertIn("Not shown (2 more)", small)
        # the ask chooses which facts fit when not all do, never their order: memory that all fits reads the
        # same whatever is asked, so an engine that has read a conversation after it does not read it again
        self.assertEqual(text, self.store.memory_text("ops", "which port does the gate listen on?"))
        self.assertEqual(text, self.store.memory_text("ops"))
        self.assertIn("- [reference] gate-port", self.store.memory_text("ops", "the gate port", budget=230))
        self.assertNotIn("- [reference] gate-port", self.store.memory_text("ops", "disk spark-2", budget=230))

    def test_tidy_tells_facts_apart_in_any_script_and_the_index_keeps_its_name(self):
        for i, fact in enumerate(("التقارير قبل السابعة", "报告不超过十行", "Отчёты до семи утра", "报告不超过十行。")):
            self.store.remember("ops", fact, "feedback", "the owner said", name="f%d" % i)
            time.sleep(0.01)
        out = self.store.tidy("ops")
        self.assertEqual(out["merged"], [{"dropped": "f3", "same_as": "f1"}])        # only the one that says the same
        self.assertEqual(sorted(f["name"] for f in self.store.memory("ops")), ["f0", "f1", "f2"])
        # a fact cannot take the index's own name (MEMORY.md and memory.md are one file on this Mac's disk)
        f = self.store.remember("ops", "the index is a file too", "reference", "run a", name="memory")
        self.assertEqual(f["name"], "memory-fact")
        self.assertIn("f0", open(os.path.join(self.root, "ops", "memory", "MEMORY.md"), encoding="utf-8").read())

    def test_the_owners_ask_goes_ahead_of_what_waits(self):
        a = self.store.wake("ops", "schedule", "report")
        b = self.store.wake("ops", "poller:news", "an item", trust="untrusted")
        c = self.store.wake("ops", "telegram", "are you there?")
        d = self.store.wake("ops", "app", "and this?")
        e = self.store.wake("ops", "agent:research", "a question")
        f = self.store.wake("ops", "rack", "the engine on spark-1 is down", trust="system")
        self.assertEqual([w["id"] for w in self.store.pending("ops")], [c["id"], d["id"], f["id"], a["id"], e["id"], b["id"]])

    def test_a_line_cut_short_does_not_swallow_the_next_wake(self):
        a = self.store.wake("ops", "app", "one")
        with open(os.path.join(self.root, "ops", "inbox.jsonl"), "a") as f:
            f.write('{"id": "w-cut", "source": "app", "text": "a line the disk cut sho')     # no newline: a full disk
        b = self.store.wake("ops", "app", "two")
        self.assertEqual([w["text"] for w in self.store.pending("ops")], ["one", "two"])
        self.assertIsNotNone(self.store.get_wake("ops", b["id"]))

    def test_an_agents_list_of_runs_keeps_the_newest_and_their_answers(self):
        saved = agentstore.RUNS_COMPACT_AT, agentstore.RUNS_KEEP
        agentstore.RUNS_COMPACT_AT, agentstore.RUNS_KEEP = 40, 10
        try:
            for i in range(60):
                self.store.record_run("ops", {"run": "agent-%03d" % i, "state": "running"})
                self.store.write_answer("ops", "agent-%03d" % i, "answer %d" % i)
                self.store.record_run("ops", {"run": "agent-%03d" % i, "state": "done", "ok": True})
            d = os.path.join(self.root, "ops", "runs")
            with open(os.path.join(d, "index.jsonl")) as f:
                self.assertLessEqual(len(f.readlines()), 40)
            runs = self.store.runs("ops", 500)
            self.assertEqual((runs[0]["run"], runs[0]["state"]), ("agent-059", "done"))
            self.assertLessEqual(len([n for n in os.listdir(d) if n.endswith(".md")]), 40)
            self.assertEqual(self.store.answer("ops", "agent-059"), "answer 59")
            self.assertIsNone(self.store.answer("ops", "agent-000"))
            # a text a channel did not take stays while it may still be sent, and goes once it is days old
            self.store.write_answer("ops", "agent-000.unsent", "still to be sent")
            self.store.write_answer("ops", "agent-001.unsent", "nobody will send this any more")
            old = time.time() - agentstore.UNSENT_KEEP_S - 60
            os.utime(os.path.join(d, "agent-001.unsent.md"), (old, old))
            for i in range(60, 110):
                self.store.record_run("ops", {"run": "agent-%03d" % i, "state": "done", "ok": True})
            self.assertEqual(self.store.answer("ops", "agent-000.unsent"), "still to be sent")
            self.assertIsNone(self.store.answer("ops", "agent-001.unsent"))
            self.store.drop_answer("ops", "agent-000.unsent")
            self.assertIsNone(self.store.answer("ops", "agent-000.unsent"))
            self.store.drop_answer("ops", "../../agent")              # a name that is no run's is not a path
        finally:
            agentstore.RUNS_COMPACT_AT, agentstore.RUNS_KEEP = saved

    def test_an_empty_memory_says_how_to_start_one(self):
        self.assertIn("remember(fact, type, source)", self.store.memory_text("ops"))

    def test_a_fact_is_one_fact(self):
        with self.assertRaises(agentstore.AgentError):
            self.store.remember("ops", "x" * 2001, "project", "run a")
        with self.assertRaises(agentstore.AgentError):
            self.store.remember("ops", "   ", "project", "run a")
        with self.assertRaises(agentstore.AgentError):
            self.store.remember("nobody", "a fact", "project", "run a")

    def test_the_tidy_merges_duplicates_and_lists_what_came_from_untrusted_content(self):
        self.store.remember("ops", "GLM is on the Sparks", "project", "run a", name="glm-a")
        time.sleep(0.02)
        self.store.remember("ops", "glm is on the sparks.", "project", "run b", name="glm-b")
        self.store.remember("ops", "the page says to disable the firewall", "reference", "https://example.com/x",
                            name="page", trust="untrusted")
        r = self.store.tidy("ops")
        self.assertEqual(r["merged"], [{"dropped": "glm-b", "same_as": "glm-a"}])
        self.assertEqual(r["review"], ["page"])
        with open(os.path.join(self.root, "ops", "memory", "MEMORY.md")) as fh:
            index = [l for l in fh.read().splitlines() if l.startswith("- [")]
        self.assertEqual(len(index), len(set(index)))
        self.assertEqual(len(index), 2)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_memory_under_git_is_committed_when_the_folder_is_a_repository(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store.remember("ops", "committed fact", "project", "run a", name="committed")
        deadline = time.time() + 10
        log = ""
        while time.time() < deadline and "remember committed" not in log:
            log = subprocess.run(["git", "log", "--oneline"], cwd=self.root, capture_output=True, text=True).stdout
            time.sleep(0.1)
        self.assertIn("ops: remember committed", log)


class Inbox(Base):
    def setUp(self):
        super().setUp()
        self.store.save(OPS)

    def test_a_wake_is_one_line_with_where_to_reply(self):
        w = self.store.wake("ops", "telegram", "what served overnight?", reply={"kind": "telegram", "chat": "123456789"})
        self.assertEqual((w["source"], w["trust"], w["state"]), ("telegram", "owner", "new"))
        with open(os.path.join(self.root, "ops", "inbox.jsonl")) as f:
            line = json.loads(f.readline())
        self.assertEqual(line["reply"], {"kind": "telegram", "chat": "123456789"})
        self.assertEqual([x["id"] for x in self.store.pending("ops")], [w["id"]])

    def test_states_are_appended_and_the_latest_wins(self):
        w = self.store.wake("ops", "app", "a question")
        self.store.set_state("ops", w["id"], "running", run="chat-1")
        self.assertEqual(self.store.pending("ops"), [])
        self.store.set_state("ops", w["id"], "done", run="chat-1", ok=True)
        got = self.store.inbox("ops")[0]
        self.assertEqual((got["state"], got["run"], got["text"]), ("done", "chat-1", "a question"))
        with open(os.path.join(self.root, "ops", "inbox.jsonl")) as f:
            self.assertEqual(len(f.readlines()), 3)                     # append-only

    def test_a_source_that_repeats_itself_wakes_it_once(self):
        a = self.store.wake("ops", "poller:rss", "new release 1.2", dedupe="rss:https://x/feed:1.2", trust="untrusted")
        b = self.store.wake("ops", "poller:rss", "new release 1.2", dedupe="rss:https://x/feed:1.2", trust="untrusted")
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        self.store.set_state("ops", a["id"], "done")
        self.assertIsNone(self.store.wake("ops", "poller:rss", "new release 1.2", dedupe="rss:https://x/feed:1.2"))
        self.assertIsNotNone(self.store.wake("ops", "poller:rss", "new release 1.3", dedupe="rss:https://x/feed:1.3"))
        # two long keys that differ only at their ends are two: a folder with a long path, a feed with long addresses
        long = "folder:/Users/someone/Library/Mobile Documents/com~apple~CloudDocs/" + "a long folder name/" * 12
        first = self.store.wake("ops", "folder", "say what it is", dedupe=long + "invoice.txt:1795100000", trust="untrusted")
        second = self.store.wake("ops", "folder", "say what it is", dedupe=long + "second.txt:1795100007", trust="untrusted")
        self.assertTrue(first and second and first["id"] != second["id"])
        self.assertIsNone(self.store.wake("ops", "folder", "say what it is", dedupe=long + "second.txt:1795100007", trust="untrusted"))
        self.assertLessEqual(len(second["dedupe"]), 200)
        self.assertEqual(agentstore.repeat_key("mail:default:<m1@example.com>"), "mail:default:<m1@example.com>")

    def test_a_held_wake_comes_back_when_its_time_has_come(self):
        w = self.store.wake("ops", "schedule", "morning report")
        now = time.time()
        self.store.set_state("ops", w["id"], "deferred", not_before=now + 3600, why="quiet hours")
        self.assertEqual(self.store.pending("ops", now), [])
        self.assertEqual([x["id"] for x in self.store.pending("ops", now + 3601)], [w["id"]])

    def test_pending_is_oldest_first(self):
        ids = [self.store.wake("ops", "app", "q%d" % i)["id"] for i in range(3)]
        self.assertEqual([w["id"] for w in self.store.pending("ops")], ids)
        self.assertEqual([w["id"] for w in self.store.inbox("ops")], ids[::-1])

    def test_a_wake_left_running_by_a_stopped_server_is_tried_again_then_closed(self):
        w = self.store.wake("ops", "app", "q")
        self.store.set_state("ops", w["id"], "running", run="chat-9")
        self.assertEqual(self.store.recover("ops", live_runs=("chat-9",)), [])          # still going: left alone
        self.assertEqual(self.store.recover("ops"), [w["id"]])
        self.assertEqual(self.store.get_wake("ops", w["id"])["state"], "new")
        self.store.set_state("ops", w["id"], "running", run="chat-10")
        self.store.recover("ops")
        self.store.set_state("ops", w["id"], "running", run="chat-11")
        self.assertEqual(self.store.recover("ops"), [])
        self.assertEqual(self.store.get_wake("ops", w["id"])["state"], "error")

    def test_the_file_is_compacted_and_keeps_every_wakes_last_state(self):
        saved = agentstore.INBOX_COMPACT_AT, agentstore.INBOX_KEEP
        agentstore.INBOX_COMPACT_AT, agentstore.INBOX_KEEP = 40, 10
        try:
            last = None
            for i in range(30):
                last = self.store.wake("ops", "app", "q%d" % i)
                self.store.set_state("ops", last["id"], "done", run="chat-%d" % i)
            with open(os.path.join(self.root, "ops", "inbox.jsonl")) as f:
                n = len(f.readlines())
            self.assertLess(n, 41)
            got = self.store.get_wake("ops", last["id"])
            self.assertEqual((got["state"], got["run"], got["text"]), ("done", "chat-29", "q29"))
        finally:
            agentstore.INBOX_COMPACT_AT, agentstore.INBOX_KEEP = saved

    def test_events_say_what_woke_whom(self):
        self.store.wake("ops", "app", "hello there")
        evts, _ = self.bus.since(0, {"inbox"})
        self.assertEqual((evts[-1]["type"], evts[-1]["data"]["agent"], evts[-1]["data"]["text"]), ("wake", "ops", "hello there"))


class RunsAndUsage(Base):
    def setUp(self):
        super().setUp()
        self.store.save(OPS)

    def test_runs_are_indexed_newest_first_and_answers_kept(self):
        self.store.record_run("ops", {"run": "chat-1", "state": "running", "started": 1.0})
        self.store.record_run("ops", {"run": "chat-1", "state": "done", "ok": True, "tokens": 120})
        self.store.record_run("ops", {"run": "agents-2", "state": "done", "ok": True})
        self.store.write_answer("ops", "chat-1", "All served. Disk 60%.")
        rows = self.store.runs("ops")
        self.assertEqual([r["run"] for r in rows], ["agents-2", "chat-1"])
        self.assertEqual((rows[1]["state"], rows[1]["tokens"], rows[1]["started"]), ("done", 120, 1.0))
        self.assertEqual(self.store.answer("ops", "chat-1"), "All served. Disk 60%.")
        self.assertIsNone(self.store.answer("ops", "../agent"))

    def test_todays_counters_start_again_at_midnight(self):
        self.store.add_usage("ops", tokens=1500, runs=1)
        self.store.add_usage("ops", tokens=500, runs=1, told=True)
        self.assertEqual({k: v for k, v in self.store.usage("ops").items() if k != "day"},
                         {"tokens": 2000, "runs": 2, "told": True})
        tomorrow = time.time() + 86400
        self.assertEqual(self.store.usage("ops", tomorrow)["tokens"], 0)
        self.assertFalse(self.store.usage("ops", tomorrow)["told"])

    def test_the_summary_is_what_the_list_shows(self):
        self.store.remember("ops", "a fact", "project", "run a")
        w = self.store.wake("ops", "schedule", "report")
        self.store.set_state("ops", w["id"], "parked", not_before=time.time() + 60, why="budget")
        s = self.store.summary("ops")
        self.assertEqual((s["facts"], s["waiting"], s["parked"], s["running"]), (1, 1, 1, None))
        self.assertEqual(self.store.home_volume("ops"), "bb-agent-ops")


class Tidying(unittest.TestCase):
    """What is kept is bounded, and nothing that still has work to do is lost to the bound."""

    def test_a_wake_that_has_not_ended_survives_the_inbox_being_tidied(self):
        root = tempfile.mkdtemp()
        keep, at = agentstore.INBOX_KEEP, agentstore.INBOX_COMPACT_AT
        agentstore.INBOX_KEEP, agentstore.INBOX_COMPACT_AT = 5, 30
        try:
            store = agentstore.AgentStore(root)
            store.save({"id": "ops", "name": "Ops", "purpose": "x"})
            waiting = store.wake("ops", "telegram", "the oldest ask, still waiting for its model")
            store.set_state("ops", waiting["id"], "parked", why="no model is live", not_before=1)
            running = store.wake("ops", "app", "an ask that is running")
            store.set_state("ops", running["id"], "running", run="r1")
            owed = store.wake("ops", "telegram", "answered, and its chat did not take the answer")
            store.set_state("ops", owed["id"], "done", run="r0", deliver_at=5.0, deliver_tries=1, deliver_text="r0.unsent")
            for n in range(40):                                  # forty more come and go
                w = store.wake("ops", "app", "ask %d" % n)
                store.set_state("ops", w["id"], "done", run="r%d" % n)
            got = {w["id"]: w["state"] for w in store.inbox("ops", 1000)}
            self.assertLess(len(got), 20)                        # the file was tidied
            self.assertEqual((got.get(waiting["id"]), got.get(running["id"])), ("parked", "running"))
            self.assertEqual([w["id"] for w in store.pending("ops", now=2)], [waiting["id"]])
            self.assertEqual(sorted(got.values()).count("done"), len(got) - 2)
            # one whose answer is still to be sent again is kept too, with what the resend needs
            self.assertEqual([(w["id"], w["deliver_text"]) for w in store.undelivered("ops", now=10)], [(owed["id"], "r0.unsent")])
        finally:
            agentstore.INBOX_KEEP, agentstore.INBOX_COMPACT_AT = keep, at
            shutil.rmtree(root, ignore_errors=True)

    def test_a_source_that_floods_an_agent_is_turned_away_past_a_bound_and_its_owner_never_is(self):
        root = tempfile.mkdtemp()
        bound = agentstore.OPEN_MAX
        agentstore.OPEN_MAX = 5
        try:
            bus = events.EventBus()
            store = agentstore.AgentStore(root, bus)
            store.save({"id": "ops", "name": "Ops", "purpose": "x"})
            taken = [store.wake("ops", "poller", "item %d" % n) for n in range(9)]
            self.assertEqual([w is not None for w in taken], [True] * 5 + [False] * 4)       # five wait, the rest are not kept
            mine = store.wake("ops", "telegram", "my own ask, in the middle of the flood")
            self.assertIsNotNone(mine)                                                          # the owner is always heard
            rows = store.inbox("ops", 100)
            told = [w for w in rows if w["state"] == "dropped"]
            self.assertEqual(len(told), 1)                                                      # said once, not per wake
            self.assertIn("its inbox is full: 5 wakes are waiting", told[0]["why"])
            self.assertEqual(len([w for w in rows if w["state"] == "new"]), 6)
            full = [e for e in bus.scan(0, {"inbox"})[0] if e["type"] == "full"]
            self.assertEqual((len(full), full[0]["data"]["source"]), (4, "poller"))
            # once it has worked some off, the source is taken again, and a later flood is said again
            for w in taken[:3]:
                store.set_state("ops", w["id"], "done", run="r")
            self.assertIsNotNone(store.wake("ops", "poller", "after it cleared"))
        finally:
            agentstore.OPEN_MAX = bound
            shutil.rmtree(root, ignore_errors=True)

    def test_a_session_nobody_is_using_leaves_no_lock_behind(self):
        # every wake of an agent that is not a conversation has an id of its own: looking one up must not keep anything
        import gc
        import sessions as sessmod
        root = tempfile.mkdtemp()
        try:
            store = sessmod.SessionStore(root) if hasattr(sessmod, "SessionStore") else sessmod.Sessions(root)
            for n in range(500):
                self.assertIsNone(store.get("ag-ops-w%04d" % n))
            gc.collect()
            self.assertLess(len(store._locks), 5)
            a, b = store._slock("one"), store._slock("one")
            self.assertIs(a, b)                                  # while one is in use, it is the same one for everybody
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_finished_runs_are_let_go_while_an_older_one_is_still_going(self):
        import runs as runsmod
        bus = events.EventBus()
        reg = runsmod.RunRegistry(bus, keep=5)
        gate = threading.Event()
        long = reg.start("agents", "test", lambda run: gate.wait(20) and {"ok": True}, title="a goal that takes its time")
        quick = [reg.start("chat", "test", lambda run: {"ok": True}, title="turn %d" % n) for n in range(30)]
        for r in quick:
            r.done_event.wait(10)
        reg.start("chat", "test", lambda run: {"ok": True}, title="one more").done_event.wait(10)
        with reg.lock:
            kept = dict(reg.runs)
        self.assertIn(long.id, kept)                             # the one still going is not dropped
        self.assertLessEqual(len(kept), 6)                       # and it did not make the registry keep every later one
        gate.set()
        long.done_event.wait(10)

class Portable(unittest.TestCase):
    """A data folder moves between a Mac, a Linux box and Windows: no name in it is one Windows cannot make."""

    def test_no_agent_and_no_fact_takes_a_windows_device_name(self):
        root = tempfile.mkdtemp()
        try:
            store = agentstore.AgentStore(root)
            for name in ("con", "nul", "aux", "com1", "lpt9"):
                with self.assertRaises(agentstore.AgentError) as e:
                    store.save({"id": name, "name": name, "purpose": "x"})
                self.assertIn("Windows keeps for a device", str(e.exception))
            store.save({"id": "ops", "name": "Ops", "purpose": "x"})
            f = store.remember("ops", "the console port is 8800", name="con")
            self.assertEqual(f["name"], "con-fact")
            self.assertTrue(os.path.exists(os.path.join(root, "ops", "memory", "con-fact.md")))
            self.assertEqual(store.remember("ops", "nul", source="the owner said")["name"], "nul-fact")
            for dp, dirs, files in os.walk(root):
                for n in dirs + files:
                    self.assertNotIn(n.split(".")[0].lower(), agentstore.DEVICES, n)
                    self.assertFalse(set(n) & set('<>:"|?*'), n)                 # nor a character Windows refuses
        finally:
            shutil.rmtree(root, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
