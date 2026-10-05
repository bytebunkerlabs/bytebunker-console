"""agents.agent_usage never makes the Usage screen wait for the worker."""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agents  # noqa: E402

CFG = {"agents": {"enabled": True, "ssh": "worker", "dir": "~/h"}}


class Done:
    def __init__(self, stdout):
        self.stdout = stdout


class AgentUsageTest(unittest.TestCase):
    def setUp(self):
        agents._usage_cache.update(at=0, val=None, busy=False)

    def test_answers_at_once_and_updates_later(self):
        calls, updated = [], threading.Event()

        def slow_worker(cmd, **kw):
            calls.append(cmd)
            time.sleep(1.0)                       # an ssh to a worker that is slow or away
            return Done('{"total": 42, "goals": 1}\n')
        with mock.patch.object(agents.subprocess, "run", slow_worker):
            t0 = time.time()
            first = agents.agent_usage(CFG, on_update=updated.set)
            self.assertLess(time.time() - t0, 0.2)
            self.assertEqual(first, {"pending": True})
            self.assertEqual(agents.agent_usage(CFG), {"pending": True})   # one ssh in flight, not two
            self.assertTrue(updated.wait(5))
            self.assertEqual(len(calls), 1)
            fresh = agents.agent_usage(CFG)
            self.assertEqual(fresh["total"], 42)
            self.assertNotIn("stale", fresh)
            # past max_age: the old numbers at once, marked, while a refresh runs
            old = agents.agent_usage(CFG, max_age=0)
            self.assertEqual((old["total"], old["stale"]), (42, True))
            deadline = time.time() + 5
            while agents._usage_cache["busy"] and time.time() < deadline:
                time.sleep(0.05)

    def test_a_worker_error_is_reported_not_raised(self):
        done = threading.Event()

        def broken(cmd, **kw):
            raise OSError("ssh: connect to host worker: No route to host")
        with mock.patch.object(agents.subprocess, "run", broken):
            agents.agent_usage(CFG, on_update=done.set)
            self.assertTrue(done.wait(5))
            self.assertIn("No route to host", agents.agent_usage(CFG)["error"])

    def test_disabled_agents_ask_nothing(self):
        self.assertIsNone(agents.agent_usage({"agents": {"enabled": False}}))


if __name__ == "__main__":
    unittest.main()
