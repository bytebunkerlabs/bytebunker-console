"""Server-owned runs end to end: jobs against the fake engine, agent goals
against a fake harness. A run outlives the request that started it, any
client can attach to it, and cancel stops it."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "fakes"))
from test_api import Server, HttpError  # noqa: E402
from fake_engine import FakeEngine  # noqa: E402

# Stands in for the harness's run_master.py: the console runs it with
# --goal; closing its stdin is how the console stops it (as the remote
# sidecar does over ssh).
FAKE_HARNESS = r'''
import json, os, sys, threading, time
goal = sys.argv[sys.argv.index("--goal") + 1]
print("BB_RUN " + json.dumps({"goal_id": "g-test", "proto": 1}), flush=True)
def watch():
    sys.stdin.read()
    print("stopping on request", flush=True)
    os._exit(3)
threading.Thread(target=watch, daemon=True).start()
if goal == "file a job":
    print("BB_JOB " + json.dumps({"name": "nightly check", "kind": "chat", "prompt": "check the logs",
                                  "schedule": {"kind": "interval", "every_min": 120}}), flush=True)
n, pause = (400, 0.05) if goal == "slow" else (3, 0.01)
for i in range(n):
    print("line %d" % i, flush=True)
    time.sleep(pause)
'''


# A harness with protocol 2: --goal-id names the goal, --spec carries the
# run's models (a 0600 file), --proto 2 adds BB_PROTO and BB_EVENT lines,
# and the exit code says how the goal ended.
FAKE_HARNESS_V2 = r'''
import json, os, stat, sys, threading, time
args = sys.argv[1:]
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
goal, gid, proto = opt("--goal"), opt("--goal-id", "g-made-up"), opt("--proto", "1")
def ev(type_, **kw):
    if proto == "2":
        print("BB_EVENT " + json.dumps(dict(kw, type=type_, goal_id=gid)), flush=True)
if proto == "2":
    print("BB_PROTO 2", flush=True)
print("BB_RUN " + json.dumps({"goal_id": gid, "proto": int(proto)}), flush=True)
spec = opt("--spec")
if spec:
    mode = stat.S_IMODE(os.stat(spec).st_mode)
    roles = json.load(open(spec))["roles"]
    print("spec %s: %s" % ("private" if mode == 0o600 else "OPEN", ", ".join("%s=%s" % (r, v["model"]) for r, v in sorted(roles.items()))), flush=True)
    print("spec file " + spec, flush=True)
def watch():
    sys.stdin.read()
    os._exit(3)
threading.Thread(target=watch, daemon=True).start()
ev("goal_accepted", text=goal)
if goal == "file a job":
    ev("job_filed", job={"name": "weekly digest", "kind": "chat", "prompt": "digest", "schedule": {"kind": "interval", "every_min": 10080}})
print("working on it", flush=True)
if goal == "fail please":
    ev("run_end", outcome="failed", exit_code=1, reason="no final answer")
    sys.exit(1)
ev("final_answer", text="done")
ev("run_end", outcome="answered", exit_code=0, reason="")
'''


def data(frames):
    return [json.loads(f["data"]) for f in frames]


class _ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = FakeEngine(models=[{"id": "fake-model", "max_model_len": 32768}]).start()
        cls.data = tempfile.mkdtemp()
        with open(os.path.join(cls.data, "fake_harness.py"), "w") as f:
            f.write(FAKE_HARNESS)
        cls.srv = Server(cls.data, {
            "gateways": [{"name": "fake", "url": cls.eng.url, "key": "", "enabled": True}],
            "agents": {"enabled": True, "ssh": "", "dir": cls.data, "python": sys.executable,
                       "script": "fake_harness.py"}})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.eng.stop()
        shutil.rmtree(cls.data, ignore_errors=True)

    def wait_state(self, rid, states, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            runs = self.srv.request("GET", "/api/runs?limit=50")[1]["runs"]
            row = next((r for r in runs if r["id"] == rid), None)
            if row and row["state"] in states:
                return row
            time.sleep(0.1)
        self.fail("run %s never reached %s" % (rid, states))



class JobRunsTest(_ServerCase):
    def save_job(self, name, **kw):
        job = dict({"name": name, "kind": "chat", "prompt": "summarize the day",
                    "schedule": {"kind": "cron", "cron": "0 9 * * *"}, "model": "fake-model", "tools": False}, **kw)
        st, saved = self.srv.request("POST", "/api/jobs", {"action": "save", "job": job})
        self.assertEqual(st, 200, saved)
        return saved["job"]["id"]

    def test_job_run_is_a_run_with_history_usage_and_events(self):
        jid = self.save_job("daily")
        seq = self.srv.request("GET", "/api/runs")[1]["seq"]
        self.eng.script([{"content": "all quiet", "usage": {"prompt_tokens": 50, "completion_tokens": 7}}])
        st, r = self.srv.request("POST", "/api/jobs", {"action": "run_now", "id": jid})
        self.assertEqual(st, 200, r)
        row = self.wait_state(r["run"], ("done", "error"))
        self.assertEqual(row["state"], "done", row)
        hist = self.srv.request("GET", "/api/jobs/runs?id=" + jid)[1]["runs"]
        self.assertEqual((hist[0]["ok"], hist[0]["output"], hist[0]["run"]), (True, "all quiet", r["run"]))
        with open(os.path.join(self.data, "usage.jsonl")) as f:
            usage = [json.loads(x) for x in f]
        self.assertIn(("job", jid, 7), [(u.get("source"), u.get("job"), u.get("completion_tokens")) for u in usage])
        evts = data(self.srv.sse("/api/events?topics=jobs&after=%d" % seq,
                                 until=lambda fr: any('"finished"' in f["data"] for f in fr)))
        self.assertEqual([e["type"] for e in evts if e["data"].get("id") == jid][-2:], ["started", "finished"])
        replay = data(self.srv.sse("/api/runs/%s/events" % r["run"],
                                   until=lambda fr: any('"finished"' in f["data"] for f in fr)))
        self.assertEqual((replay[0]["type"], replay[-1]["type"]), ("started", "finished"))

    def test_a_failing_job_is_an_error_run(self):
        jid = self.save_job("broken")
        self.eng.script([{"status": 500, "error": "engine fell over"}])
        st, r = self.srv.request("POST", "/api/jobs", {"action": "run_now", "id": jid})
        row = self.wait_state(r["run"], ("done", "error"))
        self.assertEqual(row["state"], "error")
        hist = self.srv.request("GET", "/api/jobs/runs?id=" + jid)[1]["runs"]
        self.assertFalse(hist[0]["ok"])

    def test_deleting_a_job_removes_its_history(self):
        jid = self.save_job("short-lived")
        self.eng.script([{"content": "ok"}])
        st, r = self.srv.request("POST", "/api/jobs", {"action": "run_now", "id": jid})
        self.wait_state(r["run"], ("done", "error"))
        self.srv.request("POST", "/api/jobs", {"action": "delete", "id": jid})
        self.assertEqual(self.srv.request("GET", "/api/jobs/runs?id=" + jid)[1]["runs"], [])



@unittest.skipIf(sys.platform == "win32", "a local agents launch goes through env(1); agents never run on the app's host")
class AgentRunsV2Test(unittest.TestCase):
    """A harness that speaks protocol 2 and takes a spec."""

    @classmethod
    def setUpClass(cls):
        cls.eng = FakeEngine(models=[{"id": "fake-model", "max_model_len": 32768}]).start()
        cls.data = tempfile.mkdtemp()
        with open(os.path.join(cls.data, "fake_harness_v2.py"), "w") as f:
            f.write(FAKE_HARNESS_V2)
        cls.srv = Server(cls.data, {
            "gateways": [{"name": "lan", "url": cls.eng.url.replace("127.0.0.1", "localhost"), "key": "k-1",
                          "enabled": True, "agents_url": "http://192.0.2.10:8000/v1"}],
            "roles": {"fast": "fake-model"},
            "agents": {"enabled": True, "ssh": "", "dir": cls.data, "python": sys.executable,
                       "script": "fake_harness_v2.py", "master_model": "fake-model"}})

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.eng.stop()
        shutil.rmtree(cls.data, ignore_errors=True)

    def goal(self, text):
        frames = data(self.srv.sse("/api/agents", method="POST", body={"goal": text},
                                   until=lambda fr: '"done"' in fr[-1]["data"]))
        rid = frames[0]["run"]
        full = [json.loads(f["data"]) for f in self.srv.sse("/api/runs/%s/events" % rid, timeout=10,
                until=lambda fr: json.loads(fr[-1]["data"])["type"] == "finished")]
        return rid, frames, full

    def test_the_run_names_the_goal_and_the_spec_stays_private(self):
        rid, frames, full = self.goal("quick")
        lines = [f["line"] for f in frames if "line" in f]
        self.assertIn("spec private: fast=fake-model, master=fake-model", lines)
        spec_file = next(l for l in lines if l.startswith("spec file "))[10:]
        self.assertFalse(os.path.exists(spec_file))                     # deleted when the run ended
        self.assertFalse(any(l.startswith("BB_") for l in lines))        # protocol lines are data, not text
        types = [e["data"]["type"] for e in full if e["type"] == "agent_event"]
        self.assertEqual(types, ["goal_accepted", "final_answer", "run_end"])
        self.assertTrue(all(e["data"]["goal_id"] == rid for e in full if e["type"] == "agent_event"))
        fin = [e for e in full if e["type"] == "finished"][0]["data"]
        self.assertEqual((fin["state"], fin["result"]["outcome"], fin["meta"]["goal_id"]), ("done", "answered", rid))
        with open(os.path.join(self.data, "config.json")) as f:
            self.assertTrue(json.load(f)["agents"]["harness"]["proto2"])   # asked once, remembered

    def test_a_job_filed_as_an_event_is_filed_once(self):
        self.goal("file a job")
        names = [j["name"] for j in self.srv.request("GET", "/api/jobs")[1]["jobs"]]
        self.assertEqual(names.count("weekly digest"), 1)

    def test_how_the_goal_ended_decides_the_run(self):
        rid, frames, full = self.goal("fail please")
        fin = [e for e in full if e["type"] == "finished"][0]["data"]
        self.assertEqual(fin["state"], "error")
        self.assertIn("no final answer", fin["error"])


@unittest.skipIf(sys.platform == "win32", "a local agents launch goes through env(1); agents never run on the app's host")
class AgentRunsTest(_ServerCase):
    def test_agent_goal_streams_and_hides_protocol_lines(self):
        frames = data(self.srv.sse("/api/agents", method="POST", body={"goal": "quick"},
                                   until=lambda fr: '"done"' in fr[-1]["data"]))
        self.assertEqual(frames[0].get("kind"), "agents")
        rid = frames[0]["run"]
        self.assertEqual(frames[1]["phase"], "start")
        self.assertEqual([f["line"] for f in frames if "line" in f], ["line 0", "line 1", "line 2"])
        self.assertEqual(frames[-1], {"phase": "done", "exit": 0, "killed": False})
        row = self.wait_state(rid, ("done",))
        self.assertEqual(row["meta"].get("goal_id"), "g-test")

    def test_agent_goal_outlives_its_client_and_cancel_stops_it(self):
        frames = data(self.srv.sse("/api/agents", method="POST", body={"goal": "slow"},
                                   until=lambda fr: any(f.get("line") == "line 2" for f in data(fr))))
        rid = frames[0]["run"]
        time.sleep(0.6)                                    # the client is gone; the goal is not
        self.assertEqual(self.wait_state(rid, ("running",))["state"], "running")
        with self.assertRaises(HttpError) as cm:           # one goal at a time; the 409 names the one going
            self.srv.sse("/api/agents", method="POST", body={"goal": "quick"}, timeout=5)
        self.assertEqual((cm.exception.status, cm.exception.body.get("run")), (409, rid))
        # attach from anywhere: the whole run so far, then live
        seen = data(self.srv.sse("/api/runs/%s/events?format=data" % rid,
                                 until=lambda fr: any(f.get("line") == "line 12" for f in data(fr))))
        self.assertEqual([f.get("line") for f in seen if "line" in f][:3], ["line 0", "line 1", "line 2"])
        t0 = time.time()
        st, res = self.srv.request("POST", "/api/runs/%s/cancel" % rid, {})
        self.assertEqual(st, 200, res)
        row = self.wait_state(rid, ("cancelled", "done", "error"))
        self.assertEqual(row["state"], "cancelled")
        self.assertLess(time.time() - t0, 8)
        tail = data(self.srv.sse("/api/runs/%s/events?format=data" % rid, until=lambda fr: '"done"' in fr[-1]["data"]))
        self.assertEqual(tail[-1]["killed"], "stopped")

    def test_a_goal_can_file_a_job(self):
        seq = self.srv.request("GET", "/api/runs")[1]["seq"]
        frames = data(self.srv.sse("/api/agents", method="POST", body={"goal": "file a job"},
                                   until=lambda fr: '"done"' in fr[-1]["data"]))
        filed = [f["job"] for f in frames if "job" in f]
        self.assertEqual(filed[0]["name"], "nightly check")
        jobs_now = self.srv.request("GET", "/api/jobs")[1]["jobs"]
        self.assertIn("agents:master", [j["created_by"] for j in jobs_now if j["name"] == "nightly check"])
        evts = data(self.srv.sse("/api/events?topics=jobs&after=%d" % seq, until=lambda fr: len(fr) >= 1))
        self.assertEqual(evts[0]["type"], "saved")


if __name__ == "__main__":
    unittest.main()
