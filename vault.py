"""vault.py — a 0600 store for credentials the app keeps but must never show,
log or export, and the git auth that uses them.

Today it holds one kind of secret: a token per git host (github.com, gitlab.com,
a self-hosted host...), used to clone and pull private plugin repos over https.
Tokens never leave the app: the API returns only which hosts are connected and
the user name when known, never the token. A token reaches git through
GIT_ASKPASS (an env-fed helper), never the remote URL — which would persist in
.git/config — and never the command line.

Kept out of config.json on purpose: config is shown, migrated and may be shared;
secrets must not ride along. Named vault.py, not secrets.py, so it does not
shadow the standard library's secrets module (instance.py uses it).
"""

from __future__ import annotations

import json
import os
import threading
from urllib.parse import urlsplit


def host_of(url: str) -> str:
    """The host of a git remote — https/ssh:// via the URL, and the scp-like
    git@github.com:owner/repo form — lower-cased, no port. '' when it is a local
    path or cannot be told."""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" in u:
        return (urlsplit(u).hostname or "").lower()
    if u.startswith((".", "/", "~")):
        return ""
    if ":" in u:                                   # [user@]host:path (scp-like)
        left = u.split(":", 1)[0]
        return left.rsplit("@", 1)[-1].lower()
    return ""


# ------------------------------------------------------------------- vault --
class Vault:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)            # a token lives here: never widen it
        except OSError:
            pass
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- git host tokens --
    def set_git(self, host: str, token: str, user: str = "", kind: str = "pat") -> None:
        host = (host or "").lower()
        if not host or not token:
            raise ValueError("host and token are required")
        with self._lock:
            d = self._read()
            d.setdefault("git_hosts", {})[host] = {"token": token, "user": user or "", "kind": kind}
            self._write(d)

    def git_token(self, host: str):
        """The full record (incl. the token) for internal git use, or None."""
        host = (host or "").lower()
        rec = self._read().get("git_hosts", {}).get(host)
        return rec if isinstance(rec, dict) and rec.get("token") else None

    def delete_git(self, host: str) -> bool:
        host = (host or "").lower()
        with self._lock:
            d = self._read()
            g = d.get("git_hosts", {})
            if host in g:
                g.pop(host)
                self._write(d)
                return True
            return False

    def git_hosts(self) -> list:
        """What may leave the app: host, user and kind — never the token."""
        g = self._read().get("git_hosts", {})
        return [{"host": h, "user": r.get("user", ""), "kind": r.get("kind", "pat")}
                for h, r in sorted(g.items()) if isinstance(r, dict) and r.get("token")]


# --------------------------------------------------------------- git auth ---
_ASKPASS_SH = (
    "#!/bin/sh\n"
    "# ByteBunker git askpass: the token comes from the environment, never a\n"
    "# command line or the remote URL. Written 0700; reads BB_GIT_* from env.\n"
    'case "$1" in\n'
    '  *[Uu]sername*) printf %s "${BB_GIT_USER:-x-access-token}" ;;\n'
    '  *) printf %s "$BB_GIT_TOKEN" ;;\n'
    "esac\n"
)

_ASKPASS_CMD = (
    "@echo off\r\n"
    'echo %~1 | findstr /I "sername" >nul\r\n'
    "if %errorlevel%==0 (<nul set /p=%BB_GIT_USER%) else (<nul set /p=%BB_GIT_TOKEN%)\r\n"
)


def _ensure_askpass(dirpath: str) -> str:
    """Write (once, or when it changed) the helper git runs to get credentials;
    return its path."""
    if os.name == "nt":
        path, body = os.path.join(dirpath, "git-askpass.cmd"), _ASKPASS_CMD
    else:
        path, body = os.path.join(dirpath, "git-askpass.sh"), _ASKPASS_SH
    try:
        cur = open(path, encoding="utf-8", newline="").read()
    except OSError:
        cur = None
    if cur != body:
        os.makedirs(dirpath, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(body)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def git_env(dirpath: str, token_rec, base=None) -> dict:
    """Environment for a git command. Always turns off interactive prompts so a
    private repo fails fast instead of hanging a background clone. When a token
    is given, wires GIT_ASKPASS to feed it from the environment — the token is
    never on the command line or in the stored remote URL."""
    env = dict(base if base is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token_rec and token_rec.get("token"):
        env["GIT_ASKPASS"] = _ensure_askpass(dirpath)
        env["BB_GIT_TOKEN"] = token_rec["token"]
        env["BB_GIT_USER"] = token_rec.get("user") or "x-access-token"
        env["GIT_CONFIG_NOSYSTEM"] = "1"       # ignore a machine-wide helper/askpass
        env.pop("SSH_ASKPASS", None)
    return env


_AUTH_HINTS = ("authentication failed", "could not read username", "terminal prompts disabled",
               "invalid username or password", "permission denied", "http 401", "http 403",
               "fatal: authentication", "remote: repository not found", "access denied",
               "could not read password")


def looks_like_auth(stderr: str) -> bool:
    """Whether a failed git exchange failed for want of credentials (so the UI
    should offer to connect), rather than a bad URL or a network fault."""
    s = (stderr or "").lower()
    return any(h in s for h in _AUTH_HINTS)
