"""Scheduled jobs — the console wakes up on a schedule and has the model work.

A job is a prompt (a chat turn, with the console's tools and skills if asked)
or a goal for the agent harness, run on a cron expression or every N minutes.
Jobs live in data/jobs.json; every run appends one line to data/jobs/<id>.jsonl
with its output, so the Jobs screen shows what the model did and when.

Stdlib only: a five-field cron matcher (minute hour day-of-month month
day-of-week, with * , - / and names), a scheduler thread that ticks every 20 s,
and a runner that reuses the server's upstream client, MCP host and agent
launcher. Nothing here calls out anywhere the console does not already reach.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_DOWS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _field(spec, lo, hi, names=None):
    """Parse one cron field into a set of allowed integers."""
    spec = spec.strip().lower()
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            step = max(1, int(st))
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            a = _val(a, names); b = _val(b, names)
        else:
            a = b = _val(part, names)
            if step > 1:
                b = hi
        if a < lo or b > hi or a > b:
            raise ValueError("cron field out of range: %r" % spec)
        out.update(range(a, b + 1, step))
    if not out:
        raise ValueError("empty cron field: %r" % spec)
    return out


def _val(tok, names):
    tok = tok.strip().lower()
    if names and tok[:3] in names:
        return names[tok[:3]]
    return int(tok)


def parse_cron(expr):
    """'m h dom mon dow' → a dict of sets. Raises ValueError on bad input."""
    f = str(expr or "").split()
    if len(f) != 5:
        raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week")
    dow = _field(f[4], 0, 7, _DOWS)
    if 7 in dow:
        dow.discard(7); dow.add(0)
    return {"min": _field(f[0], 0, 59), "hour": _field(f[1], 0, 23), "dom": _field(f[2], 1, 31),
            "mon": _field(f[3], 1, 12, _MONTHS), "dow": dow}


def cron_matches(c, t):
    """Does the parsed cron match this local time (struct_time)?"""
    dow = (t.tm_wday + 1) % 7          # python: Mon=0 … cron: Sun=0
    return (t.tm_min in c["min"] and t.tm_hour in c["hour"] and t.tm_mday in c["dom"]
            and t.tm_mon in c["mon"] and dow in c["dow"])


def next_cron(expr, after=None, horizon_days=400):
    """Next matching minute strictly after `after` (epoch), or None."""
    c = parse_cron(expr)
    t = int(after or time.time()) // 60 * 60 + 60
    end = t + horizon_days * 86400
    while t < end:
        if cron_matches(c, time.localtime(t)):
            return t
        t += 60
    return None


def describe_schedule(job):
    s = job.get("schedule") or {}
    if s.get("kind") == "interval":
        m = int(s.get("every_min") or 60)
        return "every %d min" % m if m < 120 else "every %.1f h" % (m / 60)
    return "cron " + str(s.get("cron") or "")


def next_run(job, after=None):
    s = job.get("schedule") or {}
    after = after or time.time()
    if s.get("kind") == "interval":
        every = max(1, int(s.get("every_min") or 60)) * 60
        base = job.get("last_run") or job.get("created") or after
        n = base + every
        while n <= after:
            n += every
        return n
    try:
        return next_cron(s.get("cron") or "", after)
    except ValueError:
        return None


class JobStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "jobs.json")
        self.runs_dir = os.path.join(data_dir, "jobs")
        os.makedirs(self.runs_dir, exist_ok=True)
        self._lock = threading.Lock()
        self.jobs = []
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.jobs = json.load(f).get("jobs", [])
        except (OSError, ValueError):
            self.jobs = []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"jobs": self.jobs}, f, indent=2)
        os.replace(tmp, self.path)

    def list(self):
        with self._lock:
            out = []
            for j in self.jobs:
                d = dict(j)
                d["next_run"] = next_run(j) if j.get("enabled", True) else None
                d["schedule_text"] = describe_schedule(j)
                d["last"] = self.last_run(j["id"])
                out.append(d)
            return out

    def get(self, jid):
        with self._lock:
            return next((j for j in self.jobs if j["id"] == jid), None)

    def upsert(self, spec):
        """Validate and create or update. Returns the stored job."""
        sched = spec.get("schedule") or {}
        if sched.get("kind") == "interval":
            every = int(sched.get("every_min") or 0)
            if every < 1 or every > 10080:
                raise ValueError("interval must be 1 … 10080 minutes")
            sched = {"kind": "interval", "every_min": every}
        else:
            parse_cron(sched.get("cron") or "")
            sched = {"kind": "cron", "cron": " ".join(str(sched.get("cron")).split())}
        kind = spec.get("kind") if spec.get("kind") in ("chat", "agent") else "chat"
        prompt = str(spec.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("the job needs a prompt (chat) or a goal (agent)")
        name = str(spec.get("name") or prompt[:40]).strip()[:80]
        job = {
            "id": spec.get("id") or ("job-" + uuid.uuid4().hex[:8]),
            "name": name, "kind": kind, "prompt": prompt, "schedule": sched,
            "enabled": bool(spec.get("enabled", True)),
            "model": str(spec.get("model") or "")[:120],
            "tools": bool(spec.get("tools", True)),
            "skills": [str(x) for x in (spec.get("skills") or [])][:12],
            "system": str(spec.get("system") or "")[:8000],
            "max_hops": max(1, min(30, int(spec.get("max_hops") or 12))),
            "created_by": str(spec.get("created_by") or "you")[:40],
            "reason": str(spec.get("reason") or "")[:500],
            "goal": str(spec.get("goal") or "")[:40],
            "created": None, "last_run": None, "runs": 0,
        }
        with self._lock:
            old = next((j for j in self.jobs if j["id"] == job["id"]), None)
            if old is None and not spec.get("id"):
                # a master that retries a filing must not create twins
                old = next((j for j in self.jobs if j["name"] == job["name"] and j["prompt"] == job["prompt"]
                            and j["schedule"] == job["schedule"]), None)
                if old:
                    return dict(old)
            if old:
                job["created"] = old.get("created"); job["last_run"] = old.get("last_run"); job["runs"] = old.get("runs", 0)
                self.jobs[self.jobs.index(old)] = job
            else:
                job["created"] = time.time()
                self.jobs.append(job)
            self._save()
        return job

    def delete(self, jid):
        with self._lock:
            self.jobs = [j for j in self.jobs if j["id"] != jid]
            self._save()

    def set_enabled(self, jid, on):
        with self._lock:
            for j in self.jobs:
                if j["id"] == jid:
                    j["enabled"] = bool(on)
            self._save()

    def record_run(self, jid, rec):
        with self._lock:
            for j in self.jobs:
                if j["id"] == jid:
                    j["last_run"] = rec["ts"]; j["runs"] = j.get("runs", 0) + 1
            self._save()
        with open(os.path.join(self.runs_dir, jid + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def runs(self, jid, limit=30):
        try:
            with open(os.path.join(self.runs_dir, jid + ".jsonl"), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out[::-1]

    def last_run(self, jid):
        r = self.runs(jid, 1)
        if not r:
            return None
        x = dict(r[0]); x["output"] = (x.get("output") or "")[:200]
        return x


class Scheduler(threading.Thread):
    """Ticks every 20 s; runs each due job once per matching minute. One job
    at a time: the engine is shared with everything else on the rack."""

    def __init__(self, store, runner, log=lambda *a: None):
        super().__init__(daemon=True, name="jobs")
        self.store, self.runner, self.log = store, runner, log
        self._fired = {}          # job id → minute it last fired
        self.running = None

    def due(self, job, now):
        if not job.get("enabled", True):
            return False
        s = job.get("schedule") or {}
        if s.get("kind") == "interval":
            every = max(1, int(s.get("every_min") or 60)) * 60
            base = job.get("last_run") or job.get("created") or now
            return now - base >= every
        try:
            c = parse_cron(s.get("cron") or "")
        except ValueError:
            return False
        minute = int(now) // 60
        if self._fired.get(job["id"]) == minute:
            return False
        return cron_matches(c, time.localtime(now))

    def run(self):
        while True:
            try:
                now = time.time()
                for job in list(self.store.jobs):
                    if self.due(job, now):
                        self._fired[job["id"]] = int(now) // 60
                        self.run_job(job, "schedule")
            except Exception as e:   # noqa: BLE001 - the scheduler must outlive any job
                self.log("jobs: tick failed: %s" % e)
            time.sleep(20)

    def run_job(self, job, trigger):
        self.running = job["id"]
        t0 = time.time()
        rec = {"ts": t0, "trigger": trigger, "kind": job["kind"], "ok": False, "ms": 0, "output": "", "error": None}
        try:
            out = self.runner(job)
            rec.update(ok=True, output=str(out.get("output") or "")[:40000], hops=out.get("hops"), tokens=out.get("tokens"))
        except Exception as e:   # noqa: BLE001
            rec["error"] = str(e)[:1000]
        rec["ms"] = int((time.time() - t0) * 1000)
        self.store.record_run(job["id"], rec)
        self.running = None
        return rec
