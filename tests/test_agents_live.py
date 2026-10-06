"""The Agents screen's live view on the worker: a slave whose role has a
dash still shows while it runs, docker works as well as podman, the goal on
screen is the running one when the harness named it, and Stop reaches the
slave's container under either runtime."""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agents  # noqa: E402

SLAVE = "deep-researcher-1a2b3c4d"


@unittest.skipIf(sys.platform == "win32", "the worker side is POSIX shell")
class LiveViewTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        traj = os.path.join(self.home, "trajectories")
        for goal, text in (("goal-20261001-old", "older goal"), ("g-7f3a", "named goal")):
            os.makedirs(os.path.join(traj, goal))
            with open(os.path.join(traj, goal, "master.jsonl"), "w") as f:
                f.write(json.dumps({"event": "round", "note": text}) + "\n")
        os.utime(os.path.join(traj, "goal-20261001-old"), (2e9, 2e9))      # the newest directory, not the named one
        open(os.path.join(traj, "spawns.jsonl"), "w").close()
        self.task = tempfile.mkdtemp(prefix="bb-%s-" % SLAVE, dir="/tmp")
        with open(os.path.join(self.task, "events.jsonl"), "w") as f:
            f.write(json.dumps({"type": "tool", "name": "search"}) + "\n")
        self.bin = tempfile.mkdtemp()
        self.cfg = {"agents": {"enabled": True, "ssh": "", "dir": self.home}}

    def tearDown(self):
        for d in (self.home, self.task, self.bin):
            shutil.rmtree(d, ignore_errors=True)

    def fake(self, name, out="", code=0):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s/%s.calls\n%sexit %d\n" % (
                self.bin, name, ("printf '%s\\n'\n" % out) if out else "", code))
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)

    def env(self):
        return mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ.get("PATH", "")})

    def live(self, **kw):
        with self.env():
            res = agents.recent_slaves(self.cfg, **kw)
        self.assertTrue(res["ok"], res)
        return res

    def test_a_dashed_role_shows_while_it_runs_under_podman(self):
        self.fake("podman", SLAVE)
        self.fake("docker", code=1)
        self.assertEqual([x["name"] for x in self.live()["live"]], [SLAVE])

    def test_docker_workers_show_too(self):
        self.fake("podman", code=1)
        self.fake("docker", SLAVE)
        self.assertEqual([x["name"] for x in self.live()["live"]], [SLAVE])

    def test_a_finished_slave_does_not_show(self):
        self.fake("podman", "someone-else-99")
        self.fake("docker", code=1)
        self.assertEqual(self.live()["live"], [])

    def test_the_named_goal_wins_over_the_newest_directory(self):
        self.fake("podman")
        self.fake("docker")
        self.assertEqual(self.live()["master"][0]["note"], "older goal")              # newest directory
        self.assertEqual(self.live(goal_id="g-7f3a")["master"][0]["note"], "named goal")

    def test_stop_reaches_the_container_under_either_runtime(self):
        self.fake("podman", code=1)                     # not podman's
        self.fake("docker")
        with self.env():
            res = agents.kill_slave(self.cfg, SLAVE)
        self.assertTrue(res["ok"], res)
        with open(os.path.join(self.bin, "docker.calls")) as f:
            self.assertIn("kill " + SLAVE, f.read())


if __name__ == "__main__":
    unittest.main()
