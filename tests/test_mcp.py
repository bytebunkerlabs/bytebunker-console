"""mcp.py: concurrent calls, dead servers, selective restarts, pings, cancels."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mcp  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes", "fake_mcp.py")


def spec(extra_env=None, tag="a"):
    return {"command": sys.executable, "args": [FAKE, "--tag", tag], "env": dict(extra_env or {}), "enabled": True}


class Concurrency(unittest.TestCase):
    def setUp(self):
        self.host = mcp.MCPHost({"f": spec()})
        self.assertEqual(self.host.status["f"]["state"], "ready", self.host.status)

    def tearDown(self):
        self.host.stop_all()

    def test_hundred_overlapping_calls_each_get_their_own_reply(self):
        results, errors = {}, []

        def worker(k):
            for j in range(5):
                text, err = self.host.call("f__echo", {"text": "%d-%d" % (k, j)})
                if err:
                    errors.append(text)
                results["%d-%d" % (k, j)] = text
        threads = [threading.Thread(target=worker, args=(k,)) for k in range(20)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 100)
        for key, text in results.items():
            self.assertEqual(text, "echo:" + key)
        self.assertLess(time.time() - t0, 20)

    def test_a_dead_server_fails_pending_calls_at_once(self):
        out = {}
        t = threading.Thread(target=lambda: out.update(slow=self.host.call("f__slow", {"seconds": 30})))
        t.start()
        time.sleep(0.3)
        t0 = time.time()
        text, err = self.host.call("f__crash", {})
        t.join(10)
        self.assertFalse(t.is_alive(), "the pending slow call should fail as soon as the server dies")
        self.assertTrue(out["slow"][1])
        self.assertLess(time.time() - t0, 5)
        text, err = self.host.call("f__echo", {"text": "x"})
        self.assertTrue(err)
        self.assertIn("not running", text)

    def test_server_ping_is_answered(self):
        text, err = self.host.call("f__ping_client", {})
        self.assertEqual((text, err), ("pong-ok", False))

    def test_annotations(self):
        self.assertEqual(self.host.tool_annotations("f__echo"), {"readOnlyHint": True})
        self.assertEqual(self.host.tool_annotations("f__nope"), {})


class Timeouts(unittest.TestCase):
    def test_timeout_cancels_on_the_server(self):
        log = tempfile.NamedTemporaryFile(delete=False)
        log.close()
        host = mcp.MCPHost({"f": spec({"FAKE_MCP_LOG": log.name})})
        try:
            s = host.servers["f"]
            with self.assertRaises(TimeoutError):
                s.call("slow", {"seconds": 3}, timeout=0.5)
            time.sleep(0.3)
            with open(log.name) as f:
                self.assertIn('"reason": "timeout"', f.read())
            text, err = host.call("f__echo", {"text": "still alive"})
            self.assertEqual(text, "echo:still alive")
        finally:
            host.stop_all()
            os.unlink(log.name)


class Sync(unittest.TestCase):
    def test_only_what_changed_restarts(self):
        cfg = {"a": spec(tag="a"), "b": spec(tag="b"), "c": spec(tag="c")}
        host = mcp.MCPHost(cfg)
        try:
            pids = {n: s.proc.pid for n, s in host.servers.items()}
            cfg2 = {"a": spec(tag="a"),                       # unchanged: keeps running
                    "b": spec({"X": "1"}, tag="b"),           # edited: restarts
                    "c": dict(spec(tag="c"), enabled=False),  # disabled: stops
                    "d": spec(tag="d")}                       # added: starts
            st = host.sync(cfg2)
            self.assertEqual(host.servers["a"].proc.pid, pids["a"])
            self.assertNotEqual(host.servers["b"].proc.pid, pids["b"])
            self.assertNotIn("c", host.servers)
            self.assertEqual(st["c"]["state"], "disabled")
            self.assertEqual(st["d"]["state"], "ready")
            host.restart("a", cfg2)
            self.assertNotEqual(host.servers["a"].proc.pid, pids["a"])
            st = host.sync({"a": cfg2["a"]})                  # removed ones disappear
            self.assertEqual(sorted(st), ["a"])
            self.assertEqual(sorted(host.servers), ["a"])
        finally:
            host.stop_all()

    def test_a_server_that_fails_to_start_reports_why(self):
        host = mcp.MCPHost({"bad": {"command": sys.executable, "args": ["-c", "import sys; sys.exit(2)"], "env": {}}})
        try:
            self.assertEqual(host.status["bad"]["state"], "error")
            self.assertTrue(host.status["bad"]["error"])
        finally:
            host.stop_all()


class WhatItSaid(unittest.TestCase):
    """A server that cannot start says why, in its own words; one that writes
    a lot to stderr is not stalled by it; the folder it needs is made."""

    def test_a_server_that_fails_says_what_it_said(self):
        code = ("import sys; sys.stderr.write('Warning: Cannot access directory /nowhere, skipping\\n"
                "Error: None of the specified directories are accessible\\n'); sys.exit(1)")
        host = mcp.MCPHost({"fs": {"command": sys.executable, "args": ["-c", code], "env": {}}})
        try:
            st = host.status["fs"]
            self.assertEqual(st["state"], "error")
            self.assertIn("exited (1): Warning: Cannot access directory /nowhere, skipping", st["error"])
            self.assertIn("None of the specified directories are accessible", st["error"])
        finally:
            host.stop_all()

    def test_a_noisy_server_is_not_stalled_by_its_stderr(self):
        code = ("import runpy, sys; sys.stderr.write('x' * 300000 + '\\n'); sys.stderr.flush(); "
                "sys.argv = [%r, '--tag', 'n']; runpy.run_path(%r, run_name='__main__')" % (FAKE, FAKE))
        host = mcp.MCPHost({"n": {"command": sys.executable, "args": ["-c", code], "env": {}}})
        try:
            self.assertEqual(host.status["n"]["state"], "ready", host.status)
        finally:
            host.stop_all()

    def test_the_folder_a_catalog_server_needs_is_made(self):
        tmp = tempfile.mkdtemp()
        folder = os.path.join(tmp, "projects", "deep")
        # laid out like the catalog's filesystem entry: the root is the third argument
        host = mcp.MCPHost({"fs": {"command": sys.executable, "args": [FAKE, "--tag", folder], "env": {},
                                   "catalog": "filesystem"}})
        try:
            self.assertEqual(host.status["fs"]["state"], "ready", host.status)
            self.assertTrue(os.path.isdir(folder))
            self.assertEqual(host.status["fs"]["created"], [folder])
        finally:
            host.stop_all()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
