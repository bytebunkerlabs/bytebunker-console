"""Updating the desktop app: what a release offers, why an app may not take
it, the download checked against its checksum, the unpacked app asked its
version, and the helper that swaps it in once the app has quit."""
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import updater  # noqa: E402
from version import VERSION  # noqa: E402


class FakeReleases:
    """GitHub's releases/latest and the files it links to, from a dict."""

    def __init__(self):
        self.release, self.files = {}, {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/latest":
                    if not outer.release:
                        return self._send(404, b'{"message": "Not Found"}')
                    return self._send(200, json.dumps(outer.release).encode())
                name = self.path.rsplit("/", 1)[-1]
                if name in outer.files:
                    return self._send(200, outer.files[name])
                self._send(404, b"")

            def _send(self, code, raw):
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def publish(self, version, files, checksums=True, notes="### New\n- something"):
        self.files = dict(files)
        if checksums:
            for name, raw in files.items():
                self.files[name + ".sha256"] = ("%s  %s\n" % (hashlib.sha256(raw).hexdigest(), name)).encode()
        self.release = {"tag_name": "v" + version, "body": notes, "html_url": self.base + "/page",
                        "published_at": "2026-10-10T12:00:00Z",
                        "assets": [{"name": n, "size": len(r), "browser_download_url": self.base + "/files/" + n}
                                   for n, r in self.files.items()]}

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class Check(unittest.TestCase):
    def setUp(self):
        self.rel = FakeReleases()
        self.env = mock.patch.dict(os.environ, {"BYTEBUNKER_UPDATE_URL": self.rel.base + "/latest"})
        self.env.start()
        self.tmp = tempfile.mkdtemp()
        self.app = os.path.join(self.tmp, "ByteBunker.app")          # somewhere this app could write
        os.makedirs(self.app)
        self.name = updater.asset_name("9.9.9") or "ByteBunker-9.9.9-mac-arm64.zip"

    def tearDown(self):
        self.env.stop()
        self.rel.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_versions_compare_as_numbers(self):
        self.assertGreater(updater.vtuple("v0.10.0"), updater.vtuple("0.9.9"))
        self.assertEqual(updater.vtuple("1.2"), (1, 2, 0))

    def test_a_newer_release_that_this_app_can_take(self):
        self.rel.publish("9.9.9", {self.name: b"zip"})
        r = updater.check("0.4.0", True, self.app)
        self.assertEqual((r["latest"], r["newer"], r["notes"]), ("9.9.9", True, "### New\n- something"))
        if updater.asset_name("9.9.9"):                         # macOS and Windows have a build
            self.assertEqual((r["can_update"], r["why_not"], r["asset"]["name"]), (True, None, self.name))
            self.assertTrue(r["checksum_url"].endswith(self.name + ".sha256"))

    def test_why_an_app_cannot_take_it(self):
        self.rel.publish("9.9.9", {self.name: b"zip"})
        self.assertIn("for the desktop app", updater.check("0.4.0", False, self.app)["why_not"])
        self.assertIn("source checkout", updater.check("0.4.0", True, None)["why_not"])
        if updater.asset_name("9.9.9"):
            self.rel.publish("9.9.9", {self.name: b"zip"}, checksums=False)
            self.assertIn("publishes no checksum", updater.check("0.4.0", True, self.app)["why_not"])
            self.rel.publish("9.9.9", {"ByteBunker-9.9.9-somewhere-else.zip": b"zip"})
            self.assertIn("no build for this computer", updater.check("0.4.0", True, self.app)["why_not"])

    def test_nothing_newer(self):
        self.rel.publish("0.4.0", {self.name: b"zip"})
        self.assertFalse(updater.check("0.4.0", True, self.app)["newer"])
        self.assertFalse(updater.check("0.5.0", True, self.app)["newer"])     # an older release is not an update

    def test_download_and_its_checksum(self):
        raw = os.urandom(300000)
        self.rel.publish("9.9.9", {self.name: raw})
        seen = []
        got = updater.download(self.rel.base + "/files/" + self.name, os.path.join(self.tmp, "dl.zip"),
                               lambda n, total: seen.append((n, total)))
        self.assertEqual(got, hashlib.sha256(raw).hexdigest())
        self.assertEqual(seen[-1], (len(raw), len(raw)))
        self.assertEqual(updater.expected_sha256(self.rel.base + "/files/" + self.name + ".sha256", self.name), got)
        with self.assertRaises(ValueError):
            updater.expected_sha256(self.rel.base + "/files/" + self.name + ".sha256", "another.zip")


@unittest.skipIf(sys.platform == "win32", "the fake app is a shell script")
class Unpack(unittest.TestCase):
    def test_the_app_inside_and_what_it_says_it_is(self):
        tmp = tempfile.mkdtemp()
        try:
            name = "ByteBunker.app" if sys.platform == "darwin" else "ByteBunker"
            exe = os.path.join(tmp, "src", name, "Contents", "MacOS", "ByteBunker") if sys.platform == "darwin" \
                else os.path.join(tmp, "src", name, "ByteBunker-cli.exe")
            os.makedirs(os.path.dirname(exe))
            with open(exe, "w") as f:
                f.write("#!/bin/sh\necho 'ByteBunker 9.9.9'\n")
            os.chmod(exe, 0o755)
            zpath = os.path.join(tmp, "app.zip")
            if sys.platform == "darwin":
                subprocess.run(["ditto", "-c", "-k", "--keepParent", os.path.join(tmp, "src", name), zpath], check=True)
            else:
                with zipfile.ZipFile(zpath, "w") as z:
                    z.write(exe, os.path.join(name, os.path.basename(exe)))
            app = updater.unpack(zpath, os.path.join(tmp, "staging"))
            self.assertEqual(os.path.basename(app), name)
            if sys.platform == "darwin":
                self.assertEqual(updater.unpacked_version(app), "9.9.9")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Swap(unittest.TestCase):
    """The helper waits for the app to end, swaps, and opens whichever is in place."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.current, self.new = os.path.join(self.tmp, "Applications", "ByteBunker.app"), os.path.join(self.tmp, "staging", "ByteBunker.app")
        self.backup, self.log = os.path.join(self.tmp, "previous", "ByteBunker.app"), os.path.join(self.tmp, "updater.log")
        for d, mark in ((self.current, "old"), (self.new, "new")):
            os.makedirs(d)
            with open(os.path.join(d, "which"), "w") as f:
                f.write(mark)
        self.app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def tearDown(self):
        if self.app.poll() is None:
            self.app.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def relaunch(self):
        marker = os.path.join(self.tmp, "relaunched")
        if sys.platform == "win32":
            return [sys.executable], None          # the helper starts it; there is no file to see
        return [sys.executable, "-c", "open(%r, 'w').write('yes')" % marker], marker

    def wait_for_log(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.log) and open(self.log).read().strip():
                return open(self.log).read().strip()
            time.sleep(0.2)
        self.fail("the helper wrote nothing")

    def which(self, d):
        with open(os.path.join(d, "which")) as f:
            return f.read()

    def test_waits_for_the_app_then_swaps_and_reopens(self):
        cmd, marker = self.relaunch()
        updater.start_swap(self.app.pid, self.current, self.new, self.backup, self.log, cmd, os.path.join(self.tmp, "work"))
        time.sleep(1.5)
        self.assertEqual(self.which(self.current), "old")              # nothing moves while the app runs
        self.app.kill()
        self.app.wait()
        self.assertEqual(self.wait_for_log(), "updated")
        self.assertEqual((self.which(self.current), self.which(self.backup)), ("new", "old"))
        if marker:
            deadline = time.time() + 10
            while not os.path.exists(marker) and time.time() < deadline:
                time.sleep(0.1)
            self.assertTrue(os.path.exists(marker), "the app was not reopened")

    def test_a_new_app_that_cannot_move_in_puts_the_old_one_back(self):
        shutil.rmtree(self.new)                                        # nothing to move in
        updater.start_swap(self.app.pid, self.current, self.new, self.backup, self.log, [], os.path.join(self.tmp, "work"))
        self.app.kill()
        self.app.wait()
        self.assertIn("the old one is back", self.wait_for_log())
        self.assertEqual(self.which(self.current), "old")


if __name__ == "__main__":
    unittest.main()
