"""workspace.py (bb's own tools) and approvals.policy_for."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import approvals  # noqa: E402
import workspace  # noqa: E402


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.outside = tempfile.mkdtemp()
        self.ws = workspace.Workspace(self.root)
        with open(os.path.join(self.root, "notes.txt"), "w") as f:
            f.write("alpha\nbeta\ngamma\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_read_numbers_lines_and_pages(self):
        text, err = self.ws.call("ws__read", {"path": "notes.txt", "offset": 2, "limit": 1})
        self.assertFalse(err)
        self.assertIn("2\tbeta", text)
        self.assertIn("1 more lines", text)

    def test_write_edit_and_their_guards(self):
        text, err = self.ws.call("ws__write", {"path": "src/app.py", "content": "x = 1\nx = 1\n"})
        self.assertFalse(err, text)
        text, err = self.ws.call("ws__edit", {"path": "src/app.py", "old_string": "x = 1", "new_string": "x = 2"})
        self.assertTrue(err)                                   # twice: not unique
        self.assertIn("occurs 2 times", text)
        text, err = self.ws.call("ws__edit", {"path": "src/app.py", "old_string": "x = 1", "new_string": "x = 2",
                                              "replace_all": True})
        self.assertFalse(err, text)
        with open(os.path.join(self.root, "src", "app.py")) as f:
            self.assertEqual(f.read(), "x = 2\nx = 2\n")

    def test_nothing_outside_the_folder(self):
        target = os.path.join(self.outside, "secret.txt")
        with open(target, "w") as f:
            f.write("keep out")
        for name, args in (("ws__read", {"path": target}), ("ws__read", {"path": "../" + os.path.basename(self.outside) + "/secret.txt"}),
                           ("ws__write", {"path": target, "content": "pwned"}), ("ws__list", {"path": self.outside}),
                           ("ws__glob", {"pattern": "../*"})):
            text, err = self.ws.call(name, args)
            self.assertTrue(err, (name, args, text))
        with open(target) as f:
            self.assertEqual(f.read(), "keep out")

    def test_list_glob_grep(self):
        os.makedirs(os.path.join(self.root, "pkg"))
        with open(os.path.join(self.root, "pkg", "mod.py"), "w") as f:
            f.write("def hello():\n    return 'beta'\n")
        self.assertIn("pkg/", self.ws.call("ws__list", {})[0])
        self.assertEqual(self.ws.call("ws__glob", {"pattern": "**/*.py"})[0], "pkg/mod.py")
        hits = self.ws.call("ws__grep", {"pattern": "beta"})[0].splitlines()
        self.assertEqual(sorted(h.split(":")[0] for h in hits), ["notes.txt", "pkg/mod.py"])

    def test_run_is_a_shell_in_the_folder(self):
        text, err = self.ws.call("ws__run", {"command": "echo hi-from-ws"})
        self.assertFalse(err, text)
        self.assertIn("hi-from-ws", text)


class PolicyTest(unittest.TestCase):
    def test_annotations_then_rules(self):
        p = approvals.policy_for
        self.assertEqual(p("fs__read", {"readOnlyHint": True}), "allow")
        self.assertEqual(p("x__y", {}), "ask")                          # says nothing: may change things
        self.assertEqual(p("x__y", {"destructiveHint": False}), "allow")
        self.assertEqual(p("term__run", {"readOnlyHint": True}, [{"term__*": "deny"}]), "deny")
        self.assertEqual(p("term__run", {}, [{"term__*": "allow"}, {"term__run": "ask"}]), "ask")   # later wins
        self.assertEqual(p("term__run", {}, [{"term__*": "nonsense"}]), "ask")


if __name__ == "__main__":
    unittest.main()
