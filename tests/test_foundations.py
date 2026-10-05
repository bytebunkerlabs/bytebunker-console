"""events.py, runs.py, sessions.py: the phase-1 foundations."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import events  # noqa: E402
import runs  # noqa: E402
import sessions  # noqa: E402


class Bus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bus = events.EventBus(runs_dir=os.path.join(self.dir, "runs"), ring=5)

    def test_order_filter_gap_and_run_replay(self):
        for i in range(3):
            self.bus.publish("sessions", "updated", {"i": i})
        self.bus.publish("runs", "line", {"text": "a"}, run="r1")
        out, gap = self.bus.since(0)
        self.assertEqual([e["seq"] for e in out], [1, 2, 3, 4])
        self.assertFalse(gap)
        out, _ = self.bus.since(0, topics={"runs"})
        self.assertEqual([e["data"] for e in out], [{"text": "a"}])
        for i in range(5):
            self.bus.publish("runs", "line", {"text": str(i)}, run="r1")
        out, gap = self.bus.since(1)
        self.assertTrue(gap, "events 2..4 left the ring: the client must reload")
        # the run's own file keeps everything, beyond the ring
        self.assertEqual(len(self.bus.run_events("r1")), 6)
        self.assertEqual(len(self.bus.run_events("r1", after=4)), 5)   # seqs 5..9
        frame = events.sse_frame(out[-1]).decode()
        self.assertTrue(frame.startswith("id: %d\nevent: runs\n" % out[-1]["seq"]))

    def test_wait_wakes_on_publish(self):
        seq = self.bus.seq
        threading.Timer(0.2, lambda: self.bus.publish("system", "tick")).start()
        t0 = time.time()
        self.assertTrue(self.bus.wait(seq, 5))
        self.assertLess(time.time() - t0, 3)
        self.assertFalse(self.bus.wait(self.bus.seq, 0.1))


class Runs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bus = events.EventBus(runs_dir=os.path.join(self.dir, "runs"))
        self.reg = runs.RunRegistry(self.bus, os.path.join(self.dir, "runs", "index.jsonl"))

    def test_done_error_cancel(self):
        def ok(run):
            self.reg.emit(run, "line", {"text": "working"})
            return {"answer": 42}
        r = self.reg.start("agents", "cli", ok, title="t")
        self.assertTrue(r.done_event.wait(5))
        self.assertEqual((r.state, r.result), ("done", {"answer": 42}))
        types = [e["type"] for e in self.bus.run_events(r.id)]
        self.assertEqual(types, ["started", "line", "finished"])

        def boom(run):
            raise RuntimeError("engine down")
        r2 = self.reg.start("job", "job", boom)
        r2.done_event.wait(5)
        self.assertEqual((r2.state, r2.error), ("error", "engine down"))

        def slow(run):
            while not run.cancel_event.wait(0.05):
                pass
            return None
        r3 = self.reg.start("deploy", "app", slow)
        time.sleep(0.1)
        self.assertEqual(self.reg.active(), [r3])
        self.assertTrue(self.reg.cancel(r3.id))
        r3.done_event.wait(5)
        self.assertEqual(r3.state, "cancelled")
        self.assertFalse(self.reg.cancel(r3.id))
        hist = self.reg.history()
        self.assertEqual({h["id"]: h["state"] for h in hist},
                         {r.id: "done", r2.id: "error", r3.id: "cancelled"})

    def test_a_restart_marks_unfinished_runs_interrupted(self):
        gate = threading.Event()
        r = self.reg.start("agents", "app", lambda run: gate.wait(5))
        time.sleep(0.1)
        # a new registry on the same files = the server restarted mid-run
        bus2 = events.EventBus(runs_dir=os.path.join(self.dir, "runs"))
        reg2 = runs.RunRegistry(bus2, os.path.join(self.dir, "runs", "index.jsonl"))
        self.assertEqual({h["id"]: h["state"] for h in reg2.history()}[r.id], "interrupted")
        gate.set()


class Sessions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def store(self, legacy=None):
        return sessions.SessionStore(os.path.join(self.dir, "sessions"), legacy)

    def test_put_get_list_append_patch_delete(self):
        st = self.store()
        st.put({"id": "s1", "title": "first", "model": "m", "messages": [{"role": "user", "content": "hi"}],
                "updated": 100})
        st.put({"id": "s2", "title": "second", "messages": [], "updated": 200})
        self.assertEqual([r["id"] for r in st.list()], ["s2", "s1"])
        self.assertEqual(st.list()[1]["turns"], 1)
        st.append("s1", "msg", message={"role": "assistant", "content": "hello"})
        st.append("s1", "patch", index=0, message={"role": "user", "content": "hi!"})
        st.append("s1", "meta", title="renamed", source="cli", cwd="/tmp/x")
        s1 = st.get("s1")
        self.assertEqual([m["content"] for m in s1["messages"]], ["hi!", "hello"])
        self.assertEqual((s1["title"], s1["source"], s1["cwd"]), ("renamed", "cli", "/tmp/x"))
        self.assertEqual(st.list()[0]["id"], "s1")           # most recently updated first
        gone = st.delete("s1")
        self.assertEqual(gone["title"], "renamed")
        self.assertIsNone(st.get("s1"))
        self.assertEqual([r["id"] for r in st.list()], ["s2"])
        with self.assertRaises(ValueError):
            st.put({"id": "../escape"})

    def test_a_snapshot_supersedes_and_a_torn_line_is_ignored(self):
        st = self.store()
        st.append("s", "msg", message={"role": "user", "content": "a"})
        st.put({"id": "s", "messages": [{"role": "user", "content": "b"}]})
        with open(os.path.join(self.dir, "sessions", "s.jsonl")) as f:
            self.assertEqual(len(f.readlines()), 1)           # the file was rewritten
        with open(os.path.join(self.dir, "sessions", "s.jsonl"), "a") as f:
            f.write('{"type": "msg", "message": {"role": "assi')  # a crash mid-write
        self.assertEqual([m["content"] for m in st.get("s")["messages"]], ["b"])

    def test_index_rebuilds_when_lost(self):
        st = self.store()
        st.put({"id": "a", "messages": []})
        os.remove(os.path.join(self.dir, "sessions", "index.json"))
        self.assertEqual([r["id"] for r in self.store().list()], ["a"])

    def test_migration_from_the_single_file(self):
        legacy = os.path.join(self.dir, "sessions.json")
        old = [{"id": "new", "title": "n", "updated": 300, "messages": [{"role": "user", "content": "x"}]},
               {"id": "old", "title": "o", "updated": 100, "messages": []}]
        with open(legacy, "w") as f:
            json.dump(old, f)
        st = self.store(legacy)
        self.assertEqual(st.migrated, 2)
        self.assertEqual([r["id"] for r in st.list()], ["new", "old"])
        self.assertEqual(st.get("new")["messages"], old[0]["messages"])
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(any(n.startswith("sessions.json.migrated-") for n in os.listdir(self.dir)))
        self.assertEqual(self.store(legacy).migrated, 0)        # once only

    def test_huge_tool_output_is_capped_in_storage(self):
        st = self.store()
        big = "x" * 1000000
        st.put({"id": "t", "messages": [{"role": "bot", "toolUse": [{"name": "fs__read", "args": "{}", "result": big}]}]})
        r = st.get("t")["messages"][0]["toolUse"][0]["result"]
        self.assertLess(len(r), 70000)
        self.assertIn("1000000 characters in total", r)
        self.assertLess(os.path.getsize(os.path.join(self.dir, "sessions", "t.jsonl")), 80000)

    def test_parallel_writes_to_different_sessions(self):
        st = self.store()

        def writer(k):
            for j in range(20):
                st.append("s%d" % k, "msg", message={"role": "user", "content": "%d" % j})
        ts = [threading.Thread(target=writer, args=(k,)) for k in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(st.list()), 8)
        for k in range(8):
            self.assertEqual(len(st.get("s%d" % k)["messages"]), 20)


if __name__ == "__main__":
    unittest.main()
