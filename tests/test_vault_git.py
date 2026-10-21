"""The credential vault and git auth: tokens are stored 0600, never handed
back by the API, fed to git only through an env-backed askpass (never the URL
or argv), and a real private clone over HTTP Basic auth succeeds with the
stored token and fails without it."""
import base64
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import vault as vaultmod  # noqa: E402
from test_api import Server  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


class Unit(unittest.TestCase):
    def test_host_of(self):
        self.assertEqual(vaultmod.host_of("https://github.com/o/r.git"), "github.com")
        self.assertEqual(vaultmod.host_of("git@github.com:o/r.git"), "github.com")
        self.assertEqual(vaultmod.host_of("ssh://git@gitlab.example.com:22/o/r"), "gitlab.example.com")
        self.assertEqual(vaultmod.host_of("http://127.0.0.1:8080/x"), "127.0.0.1")
        self.assertEqual(vaultmod.host_of("~/local/path"), "")
        self.assertEqual(vaultmod.host_of("/abs/path"), "")

    def test_vault_round_trip_masks_and_is_0600(self):
        d = tempfile.mkdtemp()
        v = vaultmod.Vault(os.path.join(d, "secrets.json"))
        v.set_git("github.com", "ghp_SECRETVALUE123", user="trickyfalcon")
        self.assertEqual(v.git_token("github.com")["token"], "ghp_SECRETVALUE123")
        hosts = v.git_hosts()
        self.assertEqual(hosts, [{"host": "github.com", "user": "trickyfalcon", "kind": "pat"}])
        self.assertNotIn("token", json.dumps(hosts))                 # never exposed
        self.assertNotIn("SECRETVALUE", json.dumps(hosts))
        mode = stat.S_IMODE(os.stat(v.path).st_mode)
        if os.name != "nt":
            self.assertEqual(mode, 0o600, oct(mode))
        self.assertTrue(v.delete_git("github.com"))
        self.assertIsNone(v.git_token("github.com"))
        self.assertFalse(v.delete_git("github.com"))

    def test_askpass_feeds_the_token_from_env_only(self):
        if os.name == "nt":
            self.skipTest("POSIX askpass")
        d = tempfile.mkdtemp()
        env = vaultmod.git_env(d, {"token": "T0P_SECRET", "user": "x-access-token"})
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        helper = env["GIT_ASKPASS"]
        self.assertEqual(stat.S_IMODE(os.stat(helper).st_mode), 0o700)
        # git calls the helper with the prompt as argv[1]; the token is in env, not argv
        run = lambda prompt: subprocess.run([helper, prompt], capture_output=True, text=True,
                                            env=dict(BB_GIT_TOKEN=env["BB_GIT_TOKEN"],
                                                     BB_GIT_USER=env["BB_GIT_USER"])).stdout
        self.assertEqual(run("Password for 'https://github.com': "), "T0P_SECRET")
        self.assertEqual(run("Username for 'https://github.com': "), "x-access-token")
        # no token wired when none is held
        self.assertNotIn("GIT_ASKPASS", vaultmod.git_env(d, None))

    def test_looks_like_auth(self):
        self.assertTrue(vaultmod.looks_like_auth("fatal: Authentication failed for 'https://..'"))
        self.assertTrue(vaultmod.looks_like_auth("could not read Username for 'https://github.com'"))
        self.assertFalse(vaultmod.looks_like_auth("fatal: repository 'https://x/y' does not resolve"))


class AuthedRepo:
    """A bare repo served over HTTP behind Basic auth (dumb protocol: git falls
    back to it because info/refs is served as a plain file, not the smart type).
    The password must equal the token; the username is ignored, as GitHub does."""

    def __init__(self, token):
        src = tempfile.mkdtemp()
        git(src, "init", "-q")
        with open(os.path.join(src, "plugin.json"), "w") as f:
            f.write('{"name":"priv","description":"private bundle","skills":"skills"}')
        os.makedirs(os.path.join(src, "skills", "secret-skill"))
        with open(os.path.join(src, "skills", "secret-skill", "SKILL.md"), "w") as f:
            f.write("---\nname: secret-skill\ndescription: from a private repo\n---\nbody\n")
        git(src, "add", "-A")
        git(src, "commit", "-q", "-m", "v1")
        self.bare = tempfile.mkdtemp(suffix=".git")
        subprocess.run(["git", "clone", "-q", "--bare", src, self.bare], check=True, capture_output=True)
        git(self.bare, "update-server-info")
        bare = self.bare

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                auth = self.headers.get("Authorization", "")
                ok = False
                if auth.startswith("Basic "):
                    try:
                        _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                        ok = pw == token
                    except Exception:
                        ok = False
                if not ok:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                rel = self.path.split("?", 1)[0].lstrip("/")
                full = os.path.join(bare, rel)
                if not os.path.isfile(full):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = open(full, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Api(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.srv = Server(self.data, config={})
        self.addCleanup(self.srv.stop)

    def test_connect_probe_add_private_over_http(self):
        repo = AuthedRepo("ghp_the_right_token")
        self.addCleanup(repo.close)

        # probe without a token: not reachable, needs auth
        st, d = self.srv.request("POST", "/api/git", {"action": "probe", "url": repo.url})
        self.assertEqual(st, 200, d)
        self.assertFalse(d["reachable"])
        self.assertTrue(d["needs_auth"], d)
        self.assertEqual(d["host"], "127.0.0.1")

        # connecting with the wrong token is refused (validated against the repo)
        st, d = self.srv.request("POST", "/api/git",
                                 {"action": "connect", "url": repo.url, "token": "ghp_wrong"})
        self.assertEqual(st, 400, d)

        # the right token connects, and the API never echoes it back
        st, d = self.srv.request("POST", "/api/git",
                                 {"action": "connect", "url": repo.url, "token": "ghp_the_right_token"})
        self.assertEqual(st, 200, d)
        self.assertNotIn("ghp_the_right_token", json.dumps(d))
        self.assertEqual(d["hosts"], [{"host": "127.0.0.1", "user": "", "kind": "pat"}])

        # GET never leaks the token; it is on disk 0600, not in config.json
        _, g = self.srv.request("GET", "/api/git")
        self.assertNotIn("ghp_the_right_token", json.dumps(g))
        secrets = os.path.join(self.data, "secrets.json")
        self.assertIn("ghp_the_right_token", open(secrets).read())    # it IS stored...
        self.assertNotIn("ghp_the_right_token", open(os.path.join(self.data, "config.json")).read())  # ...but not here
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(secrets).st_mode), 0o600)

        # now the private repo adds as a plugin (clone authenticates with the stored token)
        st, d = self.srv.request("POST", "/api/plugins", {"action": "add", "source": repo.url})
        self.assertEqual(st, 200, d)
        p = {x["name"]: x for x in d["plugins"]}.get("priv")
        self.assertTrue(p, d)
        self.assertIn("secret-skill", p["skills"])

        # the audit log recorded the connect with the token masked
        _, a = self.srv.request("GET", "/api/audit")
        line = next((e for e in a.get("entries") or [] if e.get("action") == "git.connect"), None)
        self.assertTrue(line, a)
        self.assertNotIn("ghp_the_right_token", json.dumps(line))

    def test_disconnect(self):
        v = vaultmod.Vault(os.path.join(self.data, "secrets.json"))
        v.set_git("example.com", "tok", user="me")
        st, d = self.srv.request("POST", "/api/git", {"action": "disconnect", "host": "example.com"})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["hosts"], [])
        st, _ = self.srv.request("POST", "/api/git", {"action": "disconnect", "host": "example.com"})
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
