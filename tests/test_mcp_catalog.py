"""The MCP catalog: defaults that fit this machine, folders made rather than
refused, a repository that must exist, and one terminal."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import mcp_catalog as cat  # noqa: E402
from test_api import Server  # noqa: E402


class Catalog(unittest.TestCase):
    def test_defaults_fit_this_machine(self):
        view = {c["id"]: c for c in cat.catalog_view({})}
        tz = view["time"]["params"][0]["default"]
        self.assertEqual(tz, cat.local_timezone())
        self.assertTrue(tz == "UTC" or "/" in tz, tz)
        for c in view.values():                        # nobody else's machine in a default
            for p in c["params"]:
                self.assertNotIn("bytebunker-console", p["default"])

    def test_a_repository_must_exist_and_a_folder_need_not(self):
        tmp = tempfile.mkdtemp()
        try:
            git, fs = cat.get("git"), cat.get("filesystem")
            self.assertEqual(cat.problem(git, {}), "repository path is required")
            self.assertEqual(cat.problem(git, {"repo": tmp}), "%s is not a git repository" % tmp)
            os.mkdir(os.path.join(tmp, ".git"))
            self.assertIsNone(cat.problem(git, {"repo": tmp}))
            self.assertIsNone(cat.problem(fs, {"root": os.path.join(tmp, "not-yet")}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_folders_a_server_needs(self):
        self.assertEqual(cat.folders_for({"catalog": "filesystem", "args": ["-y", "pkg", "~/projects"]}),
                         [os.path.expanduser("~/projects")])
        self.assertEqual(cat.folders_for({"catalog": "sqlite", "args": ["mcp-server-sqlite", "--db-path", "/x/y/notes.db"]}),
                         ["/x/y"])
        self.assertEqual(cat.folders_for({"command": "npx", "args": ["-y", "pkg", "/z"]}), [])   # not from the catalog


class OneTerminal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        ws = os.path.join(self.tmp, "workspace")
        self.srv = Server(self.data, {"mcp_servers": {
            "terminal": {"command": "python3", "args": ["mcp_terminal.py", ws], "env": {}, "enabled": False}}})

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_adding_the_terminal_turns_the_built_in_on_in_a_folder_it_makes(self):
        folder = os.path.join(self.tmp, "somewhere", "new")
        st, r = self.srv.request("POST", "/api/mcp", {"action": "catalog_add", "id": "terminal", "name": "terminal-2",
                                                      "params": {"root": folder}})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["name"], "terminal")
        self.assertEqual(sorted(r["config"]), ["terminal"])                      # no second copy
        self.assertEqual(r["config"]["terminal"]["enabled"], True)
        self.assertEqual(r["config"]["terminal"]["args"], ["mcp_terminal.py", folder])
        self.assertEqual(r["servers"]["terminal"]["state"], "ready", r["servers"])
        st, out = self.srv.request("POST", "/api/tool-call", {"name": "terminal__run", "arguments": {"command": "pwd"}})
        self.assertFalse(out["isError"], out)
        self.assertIn(os.path.realpath(folder), os.path.realpath(out["content"].split("cwd: ", 1)[1].split(")")[0]))

    def test_a_repository_that_is_not_one_is_refused_before_anything_starts(self):
        st, r = self.srv.request("POST", "/api/mcp", {"action": "catalog_add", "id": "git",
                                                      "params": {"repo": self.tmp}})
        self.assertEqual(st, 400)
        self.assertEqual(r["error"], "%s is not a git repository" % self.tmp)


if __name__ == "__main__":
    unittest.main()
