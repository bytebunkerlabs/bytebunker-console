"""jobs.py: the scheduler hands jobs off and never blocks; misses are kept;
a deleted job's history goes with it."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import jobs  # noqa: E402


class Busy(Exception):
    def __init__(self, started):
        super().__init__("busy")
        self.run = type("Run", (), {"started": started})()


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.store = jobs.JobStore(tempfile.mkdtemp())

    def test_tick_hands_off_each_due_job_once_a_minute(self):
        cron = self.store.upsert({"name": "c", "prompt": "p", "schedule": {"kind": "cron", "cron": "* * * * *"}})
        launched = []
        sched = jobs.Scheduler(self.store, lambda job, trigger: launched.append((job["id"], trigger)))
        now = (int(time.time()) // 60) * 60 + 5
        sched.tick(now)
        sched.tick(now + 20)                       # same minute
        self.assertEqual(launched, [(cron["id"], "schedule")])
        sched.tick(now + 60)
        self.assertEqual(len(launched), 2)

    def test_a_cron_minute_passing_while_busy_is_recorded_as_missed(self):
        cron = self.store.upsert({"name": "c", "prompt": "p", "schedule": {"kind": "cron", "cron": "* * * * *"}})
        every = self.store.upsert({"name": "i", "prompt": "p", "schedule": {"kind": "interval", "every_min": 1}})
        for j in self.store.jobs:
            j["created"] = time.time() - 600          # the interval job is due

        def launch(job, trigger):
            raise Busy(time.time() - 30)
        sched = jobs.Scheduler(self.store, launch)
        sched.tick(time.time())
        runs = self.store.runs(cron["id"])
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["missed"])
        self.assertIn("still going", runs[0]["error"])
        self.assertIsNone(self.store.last_run(cron["id"]))     # a miss is not a run
        self.assertEqual(self.store.get(cron["id"])["runs"], 0)
        self.assertEqual(self.store.runs(every["id"]), [])     # an interval job just waits

    def test_launch_errors_never_stop_the_scheduler(self):
        self.store.upsert({"name": "c", "prompt": "p", "schedule": {"kind": "cron", "cron": "* * * * *"}})
        logged = []

        def launch(job, trigger):
            raise RuntimeError("boom")
        jobs.Scheduler(self.store, launch, log=logged.append).tick(time.time())
        self.assertTrue(logged and "boom" in logged[0])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = jobs.JobStore(tempfile.mkdtemp())

    def test_delete_takes_the_history_with_it(self):
        j = self.store.upsert({"name": "c", "prompt": "p", "schedule": {"kind": "cron", "cron": "0 9 * * *"}})
        self.store.record_run(j["id"], {"ts": time.time(), "ok": True, "output": "x"})
        path = os.path.join(self.store.runs_dir, j["id"] + ".jsonl")
        self.assertTrue(os.path.exists(path))
        self.store.delete(j["id"])
        self.assertFalse(os.path.exists(path))
        self.store.record_run(j["id"], {"ts": time.time(), "ok": True, "output": "late"})   # a run that ended after
        self.assertFalse(os.path.exists(path))

    def test_job_ids_are_safe_file_names(self):
        with self.assertRaises(ValueError):
            self.store.upsert({"id": "../../x", "name": "c", "prompt": "p",
                               "schedule": {"kind": "cron", "cron": "0 9 * * *"}})
        self.assertEqual(self.store.runs("../x"), [])


if __name__ == "__main__":
    unittest.main()
