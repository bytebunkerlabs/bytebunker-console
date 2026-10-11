"""Print one version's changelog section, for its GitHub release.

    python desktop/release_notes.py 0.4.0 > notes.md
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import changelog  # noqa: E402


def main(argv):
    if len(argv) != 1:
        sys.stderr.write("usage: release_notes.py <version>\n")
        return 2
    s = changelog.section(argv[0])
    if not s:
        sys.stderr.write("CHANGELOG.md has no section for %s\n" % argv[0])
        return 1
    print(s["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
