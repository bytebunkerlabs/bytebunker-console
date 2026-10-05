"""One server per data folder, and how clients find it.

The server holds an OS lock on <data>/instance.lock for its whole life and
publishes <data>/instance.json (pid, port, version, token, started; mode
0600). A second server pointed at the same folder finds the lock taken and
stops instead of running a second job scheduler on the same files. A client
(the app's window, the CLI) reads instance.json to find the running server.
A file left behind by a crash is harmless: the lock dies with the process,
so a stale instance.json is recognised and replaced.
"""
import json
import os
import secrets
import sys
import time
import urllib.request

LOCK = "instance.lock"
INFO = "instance.json"


def _lock(fh):
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh):
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class Instance:
    def __init__(self, data_dir):
        self.dir = os.path.abspath(os.path.expanduser(data_dir))
        self.lock_path = os.path.join(self.dir, LOCK)
        self.info_path = os.path.join(self.dir, INFO)
        self._fh = None
        self.info = None

    def acquire(self):
        """True if this process now owns the folder; False if another does."""
        os.makedirs(self.dir, exist_ok=True)
        fh = open(self.lock_path, "a+")
        try:
            _lock(fh)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def publish(self, port, version="", **extra):
        """Write instance.json for clients. Call after the port is known."""
        info = {"pid": os.getpid(), "port": int(port), "version": version,
                "token": secrets.token_urlsafe(24), "started": round(time.time(), 3),
                "data": self.dir}
        info.update(extra)
        tmp = self.info_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp, self.info_path)
        try:
            os.chmod(self.info_path, 0o600)
        except OSError:
            pass
        self.info = info
        return info

    def release(self):
        if self._fh is None:
            return
        try:
            cur = read(self.dir)
            if cur and cur.get("pid") == os.getpid():
                os.remove(self.info_path)
        except OSError:
            pass
        _unlock(self._fh)
        self._fh.close()
        self._fh = None


def read(data_dir):
    """instance.json as a dict, or None."""
    try:
        with open(os.path.join(os.path.expanduser(data_dir), INFO), encoding="utf-8") as f:
            info = json.load(f)
        return info if isinstance(info, dict) and info.get("port") else None
    except (OSError, ValueError):
        return None


def held(data_dir):
    """Is some process holding this folder's lock right now?"""
    path = os.path.join(os.path.expanduser(data_dir), LOCK)
    if not os.path.exists(path):
        return False
    try:
        fh = open(path, "a+")
    except OSError:
        return False
    try:
        _lock(fh)
    except OSError:
        fh.close()
        return True
    _unlock(fh)
    fh.close()
    return False


def find(data_dir, timeout=1.5):
    """The running server for this folder, as instance.json plus "url", or
    None. It must hold the lock AND answer /api/hello, so a stale file or a
    reused port is never mistaken for it."""
    info = read(data_dir)
    if not info or not held(data_dir):
        return None
    url = "http://127.0.0.1:%d/" % int(info["port"])
    try:
        with urllib.request.urlopen(url + "api/hello", timeout=timeout) as r:
            hello = json.loads(r.read().decode("utf-8"))
    except Exception:   # noqa: BLE001
        return None
    if hello.get("service") != "bytebunker" or hello.get("pid") != info.get("pid"):
        return None
    return dict(info, url=url)
