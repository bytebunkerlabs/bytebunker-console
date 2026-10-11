"""CHANGELOG.md: every version that ships has its notes, read the same way by
the app (What's new) and by CI (the release's text)."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import changelog  # noqa: E402
from version import VERSION  # noqa: E402

SAMPLE = """# Changelog

## 2.0.0 — 2027-01-02

### New
- **Big thing.** It does
  what it says.

## 1.9.1

### Fixed
- a bug
"""


class Changelog(unittest.TestCase):
    def test_this_version_has_its_notes(self):
        s = changelog.section(VERSION)
        self.assertIsNotNone(s, "CHANGELOG.md needs a section for %s" % VERSION)
        self.assertTrue(s["body"].strip())
        self.assertRegex(s["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(changelog.sections()[0]["version"], VERSION)       # newest first

    def test_sections(self):
        got = changelog.sections(SAMPLE)
        self.assertEqual([(s["version"], s["date"]) for s in got], [("2.0.0", "2027-01-02"), ("1.9.1", "")])
        self.assertEqual(got[0]["body"], "### New\n- **Big thing.** It does\n  what it says.")
        self.assertEqual(changelog.section("v1.9.1", SAMPLE)["body"], "### Fixed\n- a bug")
        self.assertIsNone(changelog.section("3.0.0", SAMPLE))

    def test_release_notes_for_ci(self):
        script = os.path.join(ROOT, "desktop", "release_notes.py")
        r = subprocess.run([sys.executable, script, VERSION], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), changelog.section(VERSION)["body"])
        r = subprocess.run([sys.executable, script, "99.0.0"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no section for 99.0.0", r.stderr)


if __name__ == "__main__":
    unittest.main()
