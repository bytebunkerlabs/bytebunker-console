"""Channels: how a wake reaches an agent from outside the app, and how its
reply leaves.

Every source pulls. Nothing here listens on a port: the console's API stays
loopback-only behind its token, and nothing on the internet can reach this
computer through a channel.

    telegram.py   one bot, long polling; a chat is bound to one agent
    mail.py       an IMAP poll of one folder; a sender allowlist
    pollers.py    GitHub notifications, CI runs, RSS and JSON feeds
    folder.py     a watched drop folder on this computer
    rack.py       what the rack monitors say changed

A source writes a line in an agent's inbox (deps.wake) and the dispatcher
does the rest. A channel that can also carry replies has
send(reply, text, **kw), and is registered under its kind in the server's
CHANNELS, where the dispatcher finds it.

State that must survive a restart (a bot's update offset, a mailbox's last
message, what a feed already showed) lives in data/channels/<name>.json.
"""
import json
import os
import threading
import time


class State:
    """One channel's small state file: read whole, written whole, never a secret in it."""

    def __init__(self, root, name):
        self.path = os.path.join(root, name + ".json")
        self._lock = threading.Lock()
        os.makedirs(root, exist_ok=True)
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.data = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, key, default=None):
        with self._lock:
            return self.data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self.data[key] = value
            self._save()

    def update(self, fn):
        """fn(data) changes it in place; saved after."""
        with self._lock:
            fn(self.data)
            self._save()

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


class Ticker(threading.Thread):
    """Runs each registered poller when it is due. A poller is fn(now) ->
    seconds until it wants to run again; one that raises is tried again in a
    minute and its error kept for the app to show."""

    def __init__(self, log=None, step=2.0, cap=None):
        super().__init__(daemon=True, name="channels")
        self.jobs = {}                  # name -> {"fn", "next", "error", "last", "runs"}
        self.log = log or (lambda *a: None)
        self.step = step
        self.cap = cap                  # the longest any poller waits (tests look more often)
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()

    def add(self, name, fn, first_in=1.0):
        with self._lock:
            self.jobs[name] = {"fn": fn, "next": time.time() + first_in, "error": None, "last": None, "runs": 0}

    def remove(self, name):
        with self._lock:
            self.jobs.pop(name, None)

    def names(self):
        with self._lock:
            return sorted(self.jobs)

    def status(self):
        with self._lock:
            return {n: {"error": j["error"], "last": j["last"], "next": round(j["next"], 1), "runs": j["runs"]}
                    for n, j in self.jobs.items()}

    def tick(self, now=None, wait=True):
        """Run what is due, each source on a thread of its own: a feed that answers a byte at a
        time, or a mail server that is slow, holds up nothing but itself. A source still at its
        last look is not started again. wait: come back when the looks begun here have ended."""
        now = now if now is not None else time.time()
        with self._lock:
            due = [(n, j) for n, j in self.jobs.items() if j["next"] <= now and not j.get("busy")]
            for _, j in due:
                j["busy"] = True
        threads = []
        for n, j in due:
            t = threading.Thread(target=self._look, args=(n, j, now), name="channel-" + n, daemon=True)
            try:
                t.start()
                threads.append(t)
            except Exception as e:   # noqa: BLE001 - no thread to be had just now: this source is due again at the next tick
                j["busy"] = False
                self.log("channels: %s: could not start its look: %s" % (n, e))
        if wait:
            for t in threads:
                t.join()

    def _look(self, name, job, now):
        try:
            try:
                wait = job["fn"](now)
                job["error"] = None
            except Exception as e:   # noqa: BLE001 - one source's trouble is its own
                wait = 60
                job["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
                self.log("channels: %s: %s" % (name, job["error"]))
            job["last"] = round(now, 1)
            job["runs"] += 1
            wait = max(1.0, float(wait or 60))
            job["next"] = time.time() + (min(wait, self.cap) if self.cap else wait)
        finally:
            job["busy"] = False

    def run(self):
        while not self._stop_evt.wait(self.step):
            try:
                self.tick(wait=False)
            except Exception as e:   # noqa: BLE001
                self.log("channels: tick failed: %s" % e)

    def stop(self):
        self._stop_evt.set()
