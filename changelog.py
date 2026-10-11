"""The changelog, read from CHANGELOG.md: one section per version, newest
first. The app shows a version's section after updating to it (and all of
them under Settings, What's new); CI puts the same text in the version's
GitHub release, so what an update brings can be read before taking it.

    sections()           [{"version", "date", "body"}], newest first
    section("0.4.0")     one of them, or None
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
HEAD = re.compile(r"^## (\d+\.\d+\.\d+)(?:\s+\S+\s+(\d{4}-\d{2}-\d{2}))?\s*$")


def path():
    return os.path.join(HERE, "CHANGELOG.md")


def sections(text=None):
    if text is None:
        try:
            with open(path(), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return []
    out, cur = [], None
    for line in text.splitlines():
        m = HEAD.match(line)
        if m:
            cur = {"version": m.group(1), "date": m.group(2) or "", "lines": []}
            out.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
    for s in out:
        s["body"] = "\n".join(s.pop("lines")).strip()
    return out


def section(version, text=None):
    return next((s for s in sections(text) if s["version"] == str(version).lstrip("v")), None)
