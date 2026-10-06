"""The HTTP API end to end: a real server process on a temporary data folder,
found the way bb finds it (instance.json, then /api/hello)."""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class HttpError(Exception):
    def __init__(self, status, raw):
        super().__init__("HTTP %d: %s" % (status, raw[:300]))
        self.status = status
        try:
            self.body = json.loads(raw or b"null")
        except ValueError:
            self.body = raw


class Server:
    """python -c 'import server; server.serve(port=0).serve_forever()' on a temp folder."""

    def __init__(self, data, config=None, keep_config=False):
        self.data = data
        self.cfg = os.path.join(data, "config.json")
        if not keep_config:
            with open(self.cfg, "w") as f:
                json.dump(dict({"gateways": [], "monitors": []}, **(config or {})), f)
        env = dict(os.environ, BYTEBUNKER_DATA=data, BYTEBUNKER_CONFIG=self.cfg, PYTHONDONTWRITEBYTECODE="1")
        code = ("import sys; sys.path.insert(0, %r); import server; "
                "srv = server.serve('127.0.0.1', 0); srv.serve_forever()" % ROOT)
        self.proc = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=data,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        self.port = None
        while time.time() < deadline:
            try:
                with open(os.path.join(data, "instance.json")) as f:
                    info = json.load(f)
                if info.get("pid") == self.proc.pid:      # ours, not a server already there
                    self.port = info["port"]
                    break
            except (OSError, ValueError, KeyError):
                pass
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode(errors="replace")
                self.proc.stdout.close()
                raise RuntimeError("server exited: " + out)
            time.sleep(0.05)
        if not self.port:
            raise RuntimeError("server did not publish instance.json")

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": "127.0.0.1:%d" % self.port}
        if body is not None:
            h["Content-Type"] = "application/json"
            body = json.dumps(body)
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw or b"null")
        except ValueError:
            return r.status, raw

    def sse(self, path, headers=None, until=None, timeout=10, method="GET", body=None):
        """Read SSE frames until until(frames) is true or timeout. Returns frames."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Host": "127.0.0.1:%d" % self.port}
        if body is not None:
            h["Content-Type"] = "application/json"
            body = json.dumps(body)
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        if r.status != 200:
            raw = r.read()
            c.close()
            raise HttpError(r.status, raw)
        frames, cur = [], {}
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                line = r.fp.readline()
                if not line:
                    break
                line = line.decode().rstrip("\n")
                if not line:
                    if "data" in cur:          # skip the retry: preamble
                        frames.append(cur)
                        if until and until(frames):
                            break
                    cur = {}
                    continue
                if line.startswith(":"):
                    continue
                k, _, v = line.partition(": ")
                cur[k] = v
        except socket.timeout:
            pass                               # a quiet stream: return what came
        finally:
            c.close()
        return frames

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        self.proc.stdout.close()


def session(sid, n=2, updated=None):
    msgs = []
    for i in range(n):
        msgs += [{"role": "user", "content": "question %d" % i},
                 {"role": "assistant", "content": "answer %d" % i}]
    return {"id": sid, "title": "about " + sid, "model": "m1", "turns": len(msgs), "chars": 40,
            "updated": updated or int(time.time() * 1000), "messages": msgs}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp()
        with open(os.path.join(cls.data, "sessions.json"), "w") as f:
            json.dump([session("s-legacy", 3, 1000)], f)
        cls.srv = Server(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.data, ignore_errors=True)

    def test_hello(self):
        st, hello = self.srv.request("GET", "/api/hello")
        self.assertEqual(st, 200)
        self.assertEqual(hello["service"], "bytebunker")
        self.assertEqual(hello["pid"], self.srv.proc.pid)

    def test_legacy_sessions_migrated_on_start(self):
        st, lst = self.srv.request("GET", "/api/sessions")
        self.assertIn("s-legacy", [s["id"] for s in lst])
        st, full = self.srv.request("GET", "/api/sessions?id=s-legacy")
        self.assertEqual(len(full["messages"]), 6)
        self.assertFalse(os.path.exists(os.path.join(self.data, "sessions.json")))

    def test_sessions_list_is_summaries_and_id_returns_one(self):
        st, _ = self.srv.request("POST", "/api/sessions", session("s-one", 2))
        self.assertEqual(st, 200)
        st, lst = self.srv.request("GET", "/api/sessions")
        row = [s for s in lst if s["id"] == "s-one"][0]
        self.assertNotIn("messages", row)
        self.assertEqual(row["title"], "about s-one")
        st, full = self.srv.request("GET", "/api/sessions?id=s-one")
        self.assertEqual([m["content"] for m in full["messages"]][-1], "answer 1")
        st, _ = self.srv.request("GET", "/api/sessions?id=nope")
        self.assertEqual(st, 404)
        st, _ = self.srv.request("GET", "/api/sessions?id=../config")
        self.assertEqual(st, 404)

    def test_bad_ids_refused(self):
        st, _ = self.srv.request("POST", "/api/sessions", dict(session("x"), id="../../etc"))
        self.assertEqual(st, 400)
        st, _ = self.srv.request("DELETE", "/api/sessions?id=../x", headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)

    def test_delete_session_publishes(self):
        self.srv.request("POST", "/api/sessions", session("s-del"))
        st, runs = self.srv.request("GET", "/api/runs")
        after = runs["seq"]
        st, _ = self.srv.request("DELETE", "/api/sessions?id=s-del", headers={"Content-Type": "application/json"})
        self.assertEqual(st, 200)
        frames = self.srv.sse("/api/events?topics=sessions&after=%d" % after,
                              until=lambda fr: any('"deleted"' in f.get("data", "") for f in fr))
        evts = [json.loads(f["data"]) for f in frames]
        self.assertIn(("deleted", "s-del"), [(e["type"], e["data"].get("id")) for e in evts])
        st, _ = self.srv.request("GET", "/api/sessions?id=s-del")
        self.assertEqual(st, 404)

    def test_event_stream_resumes_with_last_event_id(self):
        st, runs = self.srv.request("GET", "/api/runs")
        start = runs["seq"]
        for i in range(3):
            self.srv.request("POST", "/api/sessions", session("s-ev%d" % i))
        first = self.srv.sse("/api/events?topics=sessions&after=%d" % start, until=lambda fr: len(fr) >= 1)
        self.assertEqual(json.loads(first[0]["data"])["data"]["id"], "s-ev0")
        # a browser reconnecting sends the last id it saw; the URL's after= is stale
        rest = self.srv.sse("/api/events?topics=sessions&after=%d" % start,
                            headers={"Last-Event-ID": first[0]["id"]}, until=lambda fr: len(fr) >= 2)
        self.assertEqual([json.loads(f["data"])["data"]["id"] for f in rest], ["s-ev1", "s-ev2"])
        self.assertTrue(all(int(f["id"]) > int(first[0]["id"]) for f in rest))

    def test_unknown_run(self):
        st, _ = self.srv.request("GET", "/api/runs/run-nope/events")
        self.assertEqual(st, 404)
        st, _ = self.srv.request("GET", "/api/runs/..%2f..%2fx/events")
        self.assertIn(st, (400, 404))
        st, body = self.srv.request("POST", "/api/runs/run-nope/cancel", {})
        self.assertEqual(st, 409)
        self.assertFalse(body["ok"])

    def test_bad_numbers_do_not_break_requests(self):
        st, body = self.srv.request("GET", "/api/runs?limit=lots")
        self.assertEqual(st, 200)
        self.assertIn("runs", body)
        st, _ = self.srv.request("GET", "/api/runs/run-nope/events?after=x")
        self.assertEqual(st, 404)
        frames = self.srv.sse("/api/events?after=soon", timeout=2)
        self.assertEqual(frames, [])          # starts at now: nothing new yet, no error

    def test_the_page_loads_nothing_from_the_internet(self):
        st, html = self.srv.request("GET", "/")
        self.assertEqual(st, 200)
        st, css = self.srv.request("GET", "/console.css")
        for text in (html, css):
            text = text.decode() if isinstance(text, bytes) else str(text)
            self.assertNotRegex(text, r"(src|href)=[\"']https?://|url\(['\"]?https?://|@import")
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request("GET", "/fonts/Geist-Variable.woff2", headers={"Host": "127.0.0.1:%d" % self.srv.port})
        r = c.getresponse()
        body = r.read()
        c.close()
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "font/woff2"))
        self.assertEqual(body[:4], b"wOF2")
        # class display rules once overrode the hidden attribute: Video studio, the
        # first-run card and Stop all showed when the code had hidden them
        css = css.decode() if isinstance(css, bytes) else str(css)
        self.assertIn("[hidden]{display:none!important}", css.replace(" ", ""))

    def test_static_files_stay_inside_public(self):
        for path in ("/../server.py", "/%2e%2e/server.py", "/fonts/../../config.json"):
            c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
            c.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.srv.port})
            r = c.getresponse()
            r.read()
            c.close()
            self.assertEqual(r.status, 404, path)

    def test_post_needs_json(self):
        st, _ = self.srv.request("POST", "/api/sessions", None, headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)

    def test_foreign_host_refused(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request("GET", "/api/sessions", headers={"Host": "evil.example:80"})
        self.assertEqual(c.getresponse().status, 403)
        c.close()


class SecondServerTest(unittest.TestCase):
    def test_second_server_on_same_folder_refuses(self):
        data = tempfile.mkdtemp()
        a = Server(data)
        try:
            with self.assertRaises(RuntimeError) as cm:
                Server(data)
            self.assertIn("already using", str(cm.exception))
        finally:
            a.stop()
            shutil.rmtree(data, ignore_errors=True)


@unittest.skipIf(sys.platform == "win32", "SIGTERM is a POSIX service manager's stop")
class HeadlessStopTest(unittest.TestCase):
    def test_sigterm_with_an_open_event_stream_stops_promptly(self):
        """launchd and systemd stop the headless app with SIGTERM; an open
        browser tab's event stream must not hold the process up."""
        import signal
        home = tempfile.mkdtemp()
        env = dict(os.environ, BYTEBUNKER_HOME=home, PYTHONDONTWRITEBYTECODE="1")
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"gateways": [], "port": 0}, f)
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "desktop", "app.py"), "--headless"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            port, deadline = None, time.time() + 30
            while time.time() < deadline and port is None:
                try:
                    with open(os.path.join(home, "data", "instance.json")) as f:
                        info = json.load(f)
                    port = info["port"] if info.get("pid") == proc.pid else None
                except (OSError, ValueError, KeyError):
                    pass
                if proc.poll() is not None:
                    self.fail("app exited: " + proc.stdout.read().decode(errors="replace"))
                time.sleep(0.05)
            self.assertTrue(port, "the headless app never published instance.json")
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/api/events", headers={"Host": "127.0.0.1:%d" % port})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.fp.readline()                          # the stream is open
            t0 = time.time()
            proc.send_signal(signal.SIGTERM)
            proc.wait(10)
            self.assertLess(time.time() - t0, 5)
            self.assertFalse(os.path.exists(os.path.join(home, "data", "instance.json")))
            c.close()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            proc.stdout.close()
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
