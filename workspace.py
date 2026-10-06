"""The tools bb gives the model: they run in bb's own process, in the folder
bb was started in, with the user's own environment (venv, PATH, ssh-agent).
Nothing in that environment reaches the server; the server only relays the
calls and records them. File tools stay inside the folder; the shell is a
shell, so it asks first.

    ws = Workspace(os.getcwd())
    ws.defs()                         # tool definitions to send with a turn
    text, is_error = ws.call("ws__read", {"path": "README.md"})
"""
import fnmatch
import glob as globmod
import os
import re

import mcp_terminal

MAX_READ_LINES = 2000
MAX_CHARS = 100000
MAX_ENTRIES = 500
MAX_MATCHES = 200
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}


def _fn(name, desc, props, required=(), annotations=None):
    return {"type": "function",
            "function": {"name": name, "description": desc,
                         "parameters": {"type": "object", "properties": props, "required": list(required)}},
            "annotations": annotations or {}}


TOOLS = [
    _fn("ws__run", "Run a command in a shell in the user's working folder, as the user, and return its exit code, "
                   "stdout and stderr. cd persists between calls. Non-interactive: pass -y style flags.",
        {"command": {"type": "string"}, "timeout_s": {"type": "integer", "minimum": 1, "maximum": 600}},
        ("command",), {"destructiveHint": True}),
    _fn("ws__read", "Read a text file in the working folder, with line numbers. offset is the first line (1-based).",
        {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
         "limit": {"type": "integer", "minimum": 1}}, ("path",), {"readOnlyHint": True}),
    _fn("ws__write", "Write a file in the working folder (created, or replaced whole). Parent folders are created.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ("path", "content"), {"destructiveHint": True}),
    _fn("ws__edit", "Replace an exact string in a file in the working folder. old_string must occur exactly once "
                    "unless replace_all is true.",
        {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
         "replace_all": {"type": "boolean"}}, ("path", "old_string", "new_string"), {"destructiveHint": True}),
    _fn("ws__list", "List a folder inside the working folder (default: the working folder itself).",
        {"path": {"type": "string"}}, (), {"readOnlyHint": True}),
    _fn("ws__glob", "Find files in the working folder by pattern, such as **/*.py.",
        {"pattern": {"type": "string"}}, ("pattern",), {"readOnlyHint": True}),
    _fn("ws__grep", "Search file contents in the working folder with a regular expression. Returns path:line: text.",
        {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
         "ignore_case": {"type": "boolean"}}, ("pattern",), {"readOnlyHint": True}),
]
NAMES = {t["function"]["name"] for t in TOOLS}


class Workspace:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        mcp_terminal.STATE["cwd"] = self.root

    def defs(self):
        """Definitions for the model (without the annotations, which stay here)."""
        return [{"type": t["type"], "function": t["function"]} for t in TOOLS]

    @staticmethod
    def annotations(name):
        for t in TOOLS:
            if t["function"]["name"] == name:
                return t["annotations"]
        return {}

    def _path(self, p):
        full = os.path.realpath(os.path.join(self.root, os.path.expanduser(str(p or "."))))
        if os.path.commonpath([full, self.root]) != self.root:
            raise ValueError("%s is outside the working folder %s" % (p, self.root))
        return full

    def _rel(self, p):
        r = os.path.relpath(p, self.root)
        return "." if r == "." else r.replace(os.sep, "/")

    def call(self, name, args):
        try:
            fn = getattr(self, "_" + name.split("__", 1)[1])
        except (AttributeError, IndexError):
            return "unknown workspace tool %s" % name, True
        try:
            return fn(args or {})
        except (OSError, ValueError, re.error) as e:
            return "%s: %s" % (name, e), True

    # ------------------------------------------------------------ the tools
    def _run(self, a):
        return mcp_terminal.run(a)

    def _read(self, a):
        p = self._path(a.get("path"))
        start = max(1, int(a.get("offset") or 1))
        limit = max(1, min(MAX_READ_LINES, int(a.get("limit") or MAX_READ_LINES)))
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        chunk = lines[start - 1:start - 1 + limit]
        out = "".join("%6d\t%s" % (start + i, l if l.endswith("\n") else l + "\n") for i, l in enumerate(chunk))
        more = len(lines) - (start - 1 + len(chunk))
        if len(out) > MAX_CHARS:
            out = out[:MAX_CHARS] + "\n[…cut at %d characters]" % MAX_CHARS
        if more > 0:
            out += "[%d more lines: read on with offset=%d]\n" % (more, start + len(chunk))
        return out or "(empty file)", False

    def _write(self, a):
        p = self._path(a.get("path"))
        content = str(a.get("content") if a.get("content") is not None else "")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        existed = os.path.exists(p)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return "%s %s (%d characters)" % ("replaced" if existed else "created", self._rel(p), len(content)), False

    def _edit(self, a):
        p = self._path(a.get("path"))
        old, new = str(a.get("old_string") or ""), str(a.get("new_string") or "")
        if not old:
            return "old_string is empty: use ws__write to create a file", True
        with open(p, encoding="utf-8", newline="") as f:
            text = f.read()
        n = text.count(old)
        if n == 0:
            return "old_string does not occur in %s" % self._rel(p), True
        if n > 1 and not a.get("replace_all"):
            return ("old_string occurs %d times in %s: give more context to make it unique, or set replace_all"
                    % (n, self._rel(p))), True
        text = text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return "edited %s (%d replacement%s)" % (self._rel(p), n if a.get("replace_all") else 1,
                                                "s" if a.get("replace_all") and n > 1 else ""), False

    def _list(self, a):
        p = self._path(a.get("path") or ".")
        rows = []
        for name in sorted(os.listdir(p))[:MAX_ENTRIES]:
            full = os.path.join(p, name)
            if os.path.isdir(full):
                rows.append(name + "/")
            else:
                try:
                    rows.append("%s  (%d bytes)" % (name, os.path.getsize(full)))
                except OSError:
                    rows.append(name)
        return ("\n".join(rows) or "(empty folder)"), False

    def _glob(self, a):
        pattern = str(a.get("pattern") or "")
        if not pattern or os.path.isabs(pattern) or ".." in pattern.split("/"):
            return "a pattern relative to the working folder, such as **/*.py", True
        hits = []
        for p in globmod.iglob(os.path.join(self.root, pattern), recursive=True):
            rel = self._rel(p)
            if any(part in SKIP_DIRS for part in rel.split("/")[:-1]):
                continue
            hits.append(rel + ("/" if os.path.isdir(p) else ""))
            if len(hits) >= MAX_ENTRIES:
                break
        return ("\n".join(sorted(hits)) or "no files match %s" % pattern), False

    def _grep(self, a):
        rx = re.compile(str(a.get("pattern") or ""), re.I if a.get("ignore_case") else 0)
        base = self._path(a.get("path") or ".")
        only = a.get("glob") or "*"
        out = []
        files = [base] if os.path.isfile(base) else None
        if files is None:
            files = []
            for d, dirs, names in os.walk(base):
                dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
                files.extend(os.path.join(d, n) for n in sorted(names) if fnmatch.fnmatch(n, only))
        for f in files:
            try:
                if os.path.getsize(f) > 2_000_000:
                    continue
                with open(f, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            out.append("%s:%d: %s" % (self._rel(f), i, line.rstrip("\n")[:300]))
                            if len(out) >= MAX_MATCHES:
                                return "\n".join(out) + "\n[first %d matches]" % MAX_MATCHES, False
            except (UnicodeDecodeError, OSError):
                continue                 # binary or unreadable
        return ("\n".join(out) or "no matches for %s" % a.get("pattern")), False
