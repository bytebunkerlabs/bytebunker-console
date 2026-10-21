"""A private bundle repo (skills + MCP servers) added from git, synced with a
later git pull, and its skills mirrored to the agent worker. The 'remote' is a
local git repo reached over file://, so no network is touched."""
import os
import subprocess
import tempfile
import unittest

from test_api import Server


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


SKILL = ("---\nname: %s\ndescription: a demo skill\n---\nbody of %s\n")


def write_skill(repo, name):
    d = os.path.join(repo, "skills", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(SKILL % (name, name))


class PluginGit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        # the upstream bundle repo
        self.origin = os.path.join(self.tmp, "bundle")
        os.makedirs(self.origin)
        git(self.origin, "init", "-q", "-b", "main")
        with open(os.path.join(self.origin, "plugin.json"), "w") as f:
            f.write('{"name":"bundle","description":"private kit","version":"1.0",'
                    '"mcp_servers":{"burp":{"command":"npx","args":["-y","mcp-remote","http://127.0.0.1:9876/"]}}}')
        write_skill(self.origin, "recon")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", "v1")
        # a local "worker" harness the console mirrors skills into
        self.worker = os.path.join(self.tmp, "harness")
        os.makedirs(self.worker)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.srv = Server(self.data, config={
            "agents": {"enabled": True, "dir": self.worker, "ssh": ""},
        })
        self.addCleanup(self.srv.stop)

    def plugins(self):
        _, d = self.srv.request("GET", "/api/plugins")
        return {p["name"]: p for p in d.get("plugins") or []}

    def test_add_enable_sync_mirror(self):
        # add from file:// (full clone)
        st, d = self.srv.request("POST", "/api/plugins",
                                 {"action": "add", "source": "file://" + self.origin})
        self.assertEqual(st, 200, d)
        p = self.plugins()["bundle"]
        self.assertTrue(p["git"] and p["git"]["remote"].endswith("bundle"), p["git"])
        self.assertIn("burp", p["mcp_servers"])
        self.assertIn("recon", p["skills"])

        # enabling an agent-ready bundle mirrors its skills to the worker
        st, d = self.srv.request("POST", "/api/plugins", {"action": "enable", "name": "bundle"})
        self.assertEqual(st, 200, d)
        self.assertTrue(d.get("mirror", {}).get("pushed"), d.get("mirror"))
        self.assertTrue(os.path.isfile(os.path.join(self.worker, "skills", "recon", "SKILL.md")))

        # upstream gains a skill; sync pulls it and mirrors it
        write_skill(self.origin, "exploit")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", "v2")
        st, d = self.srv.request("POST", "/api/plugins", {"action": "sync", "name": "bundle"})
        self.assertEqual(st, 200, d)
        self.assertIn("exploit", d["mirror"]["skills"])
        self.assertTrue(os.path.isfile(os.path.join(self.worker, "skills", "exploit", "SKILL.md")))
        self.assertIn("exploit", self.plugins()["bundle"]["skills"])

    def test_sync_rejects_a_non_git_plugin(self):
        # a plugin created in place (no .git) cannot be synced
        st, _ = self.srv.request("POST", "/api/plugins",
                                 {"action": "create", "name": "loose", "description": "x"})
        self.assertEqual(st, 200)
        st, d = self.srv.request("POST", "/api/plugins", {"action": "sync", "name": "loose"})
        self.assertEqual(st, 400)
        self.assertIn("not a git checkout", d["error"])


if __name__ == "__main__":
    unittest.main()
