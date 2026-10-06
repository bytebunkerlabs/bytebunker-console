"""The remote wrapper agents.build_command sends over ssh, run for real in a
local shell (a fake ssh runs the remote command): the spec arrives through
stdin into a 0600 file the harness reads, and is gone when the run ends;
closing stdin still stops the run."""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import agents  # noqa: E402
from test_runs_api import FAKE_HARNESS_V2, FAKE_HARNESS  # noqa: E402

FAKE_SSH = """#!/bin/sh
# ssh [-o opt]... host command: run the command here
while [ "$1" = "-o" ]; do shift 2; done
shift
exec sh -c "$1"
"""


@unittest.skipIf(sys.platform == "win32", "the worker side is POSIX shell")
class RemoteWrapperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bin = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "v2.py"), "w") as f:
            # remotely a harness's stdin is /dev/null: no stdin watcher here
            f.write(FAKE_HARNESS_V2.replace("threading.Thread(target=watch, daemon=True).start()", ""))
        with open(os.path.join(self.dir, "v1.py"), "w") as f:
            f.write(FAKE_HARNESS)
        with open(os.path.join(self.dir, "slow.py"), "w") as f:     # a harness that only works, slowly
            f.write("import time\nfor i in range(600):\n    print('line %d' % i, flush=True)\n    time.sleep(0.05)\n")
        p = os.path.join(self.bin, "ssh")
        with open(p, "w") as f:
            f.write(FAKE_SSH)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.bin, ignore_errors=True)

    def cfg(self, script):
        return {"agents": {"enabled": True, "ssh": "worker", "dir": self.dir, "python": sys.executable,
                           "script": script}}

    def run_goal(self, cfg, goal, spec="", stop_after=None):
        lines, stop = [], threading.Event()
        with mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"]}):
            caps = agents.harness_caps(cfg, refresh=True)
            cmd = agents.build_command(cfg, goal, run_id="agents-t1", caps=caps, spec_len=len(spec.encode()))
            if stop_after:
                threading.Timer(stop_after, stop.set).start()
            code, killed = agents.run_streaming(cmd, None, lines.append, stop, timeout_s=30, stdin_first=spec)
        return code, killed, lines

    def test_the_spec_travels_on_stdin_and_is_removed(self):
        cfg = self.cfg("v2.py")
        spec = json.dumps({"version": 1, "roles": {"master": {"model": "m-big", "api_key": "secret-key"}}})
        code, killed, lines = self.run_goal(cfg, "quick", spec)
        self.assertEqual(code, 0, lines)
        self.assertIn("spec private: master=m-big", lines)
        path = next(l for l in lines if l.startswith("spec file "))[10:]
        self.assertFalse(os.path.exists(path))
        self.assertTrue(any(l.startswith("BB_EVENT ") and '"run_end"' in l for l in lines))
        self.assertTrue(any('"goal_id": "agents-t1"' in l for l in lines))
        self.assertFalse(any("Terminated" in l for l in lines), lines[-3:])     # the shell's notices stay out

    def test_closing_stdin_still_stops_the_run(self):
        t0 = time.time()
        code, killed, lines = self.run_goal(self.cfg("slow.py"), "anything", spec="{}", stop_after=1.0)
        self.assertEqual(killed, "stopped")
        self.assertLess(len(lines), 200)                      # it did not run its 600 lines
        self.assertLess(time.time() - t0, 12)

    def test_an_older_harness_gets_only_the_goal(self):
        with mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"]}):
            caps = agents.harness_caps(self.cfg("v1.py"), refresh=True)
        self.assertFalse(caps["proto2"] or caps["spec"] or caps["goal_id"])


if __name__ == "__main__":
    unittest.main()
