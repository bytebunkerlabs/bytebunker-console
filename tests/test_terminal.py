"""mcp_terminal.py on this OS: commands run in the platform's shell (zsh or
bash, PowerShell on Windows), exit codes come back, `cd` persists, and the
tool says which OS it is on."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mcp_terminal as term  # noqa: E402


class TerminalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "sub"))
        term.STATE["cwd"] = self.dir

    def test_output_and_exit_codes(self):
        text, err = term.run({"command": "echo hello-bb"})
        self.assertFalse(err, text)
        self.assertTrue(text.startswith("exit 0"), text)
        self.assertIn("hello-bb", text)
        text, err = term.run({"command": "exit 3"})
        self.assertTrue(err)
        self.assertTrue(text.startswith("exit 3"), text)

    def test_cd_persists_between_calls(self):
        text, err = term.run({"command": "cd sub"})
        self.assertFalse(err, text)
        self.assertTrue(os.path.samefile(term.STATE["cwd"], os.path.join(self.dir, "sub")), term.STATE["cwd"])
        text, _ = term.run({"command": "cd .."})
        self.assertTrue(os.path.samefile(term.STATE["cwd"], self.dir))

    def test_quotes_survive(self):
        if term.WINDOWS:
            cmd = """Write-Output 'it''s a "quoted" string'"""
        else:
            cmd = """echo "it's a 'quoted' \\"string\\"" """
        text, err = term.run({"command": cmd})
        self.assertFalse(err, text)
        self.assertIn("quoted", text)
        self.assertIn("it's", text)

    def test_timeout(self):
        text, err = term.run({"command": "sleep 5" if not term.WINDOWS else "Start-Sleep -Seconds 5", "timeout_s": 1})
        self.assertTrue(err)
        self.assertIn("killed after 1s", text)

    def test_the_tool_names_this_os_and_shell(self):
        desc = term.TOOLS[0]["description"]
        self.assertIn({"darwin": "macOS", "linux": "Linux", "win32": "Windows"}[sys.platform], desc)
        if sys.platform == "win32":
            self.assertIn("PowerShell", desc)


if __name__ == "__main__":
    unittest.main()
