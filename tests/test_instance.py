"""instance.py: one server per data folder; stale files never fool clients."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import instance  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def try_from_another_process(data):
    code = "import sys; sys.path.insert(0, %r); import instance; print(instance.Instance(%r).acquire())" % (ROOT, data)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout.strip()


class InstanceTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()

    def test_second_owner_refused_until_released(self):
        a = instance.Instance(self.data)
        self.assertTrue(a.acquire())
        self.assertTrue(instance.held(self.data))
        self.assertEqual(try_from_another_process(self.data), "False")
        a.release()
        self.assertFalse(instance.held(self.data))
        self.assertEqual(try_from_another_process(self.data), "True")

    def test_publish_is_private_and_removed_on_release(self):
        a = instance.Instance(self.data)
        self.assertTrue(a.acquire())
        info = a.publish(12345, version="9.9")
        self.assertEqual(instance.read(self.data)["port"], 12345)
        self.assertTrue(info["token"])
        if sys.platform != "win32":
            self.assertEqual(os.stat(os.path.join(self.data, "instance.json")).st_mode & 0o777, 0o600)
        a.release()
        self.assertIsNone(instance.read(self.data))

    def test_find_needs_the_lock_and_a_matching_hello(self):
        pid = os.getpid()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"service": "bytebunker", "pid": pid}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            a = instance.Instance(self.data)
            # a stale file and no lock: not running
            with open(os.path.join(self.data, "instance.json"), "w") as f:
                json.dump({"pid": pid, "port": srv.server_address[1]}, f)
            self.assertIsNone(instance.find(self.data))
            self.assertTrue(a.acquire())
            a.publish(srv.server_address[1])
            found = instance.find(self.data)
            self.assertEqual(found["url"], "http://127.0.0.1:%d/" % srv.server_address[1])
            a.release()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
