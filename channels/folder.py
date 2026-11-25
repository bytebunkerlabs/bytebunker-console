"""A drop folder: a file put in a folder on this computer wakes an agent, with the file attached.

    {"kind": "folder", "path": "~/Drop/ops", "prompt": "Read it and file what matters."}

A file counts once it has stopped growing (the same size on two looks). What
was there when the watch began is history, not a wake. The file stays where
you put it; the agent gets a copy among the app's uploads. Its content is
data from a file, never an instruction: your prompt is the ask.
"""
import os
import re
import stat
import time
import uuid

import rules as rulesmod

TEXT_MAX = 100000
FILE_MAX = 25 * 1024 * 1024
TEXT_EXT = (".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".xml", ".html", ".py", ".js", ".sh", ".toml",
            ".ini", ".cfg", ".tsv", ".rst", ".env", ".sql")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
POLL_S = 10


def regular(path):
    """A regular file that is itself in the folder: not a link, a folder, a pipe or a device."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


class Folders:
    """deps: store, state, wake(agent_id, source, text, **kw), save_file(agent_id, name, data) -> attachment, log."""

    def __init__(self, deps):
        self.d = deps
        self._sizes = {}            # path -> size at the last look: a file still being written is left alone
        self.errors = {}

    def entries(self):
        out = []
        for a in self.d.store.list():
            if not a.get("enabled", True):
                continue
            for w in a.get("wake") or []:
                if w.get("kind") == "folder" and w.get("path") and w.get("enabled") is not False:
                    out.append((a, w, os.path.realpath(os.path.expanduser(w["path"]))))
        return out

    def poll(self, now=None):
        entries = self.entries()
        st = self.d.state.get("folders", {}) or {}
        for agent, w, path in entries:
            key = "%s:%s" % (agent["id"], path)
            try:
                os.makedirs(path, exist_ok=True)        # a drop folder that is not there yet is made
                # a file put there, never a link to one: a link would hand the agent whatever it points at
                names = sorted(n for n in os.listdir(path) if not n.startswith(".") and regular(os.path.join(path, n)))
                self.errors.pop(key, None)
            except OSError as e:
                self.errors[key] = str(e)[:200]
                continue
            rec = st.get(key)
            stamps = {}
            for n in names:
                try:
                    s = os.lstat(os.path.join(path, n))
                    stamps[n] = "%d:%d" % (s.st_mtime_ns, s.st_size)
                except OSError:
                    continue
            if rec is None:
                st[key] = {"seen": stamps}               # the first look: what is there is history
                continue
            seen = dict(rec.get("seen") or {})
            for n, stamp in stamps.items():
                if seen.get(n) == stamp:
                    continue
                full = os.path.join(path, n)
                size = int(stamp.split(":")[1])
                if self._sizes.get(full) != size:
                    self._sizes[full] = size            # still arriving, perhaps: look again
                    continue
                self._sizes.pop(full, None)
                seen[n] = stamp
                try:
                    self._wake(agent, w, full, n, size)
                except Exception as e:   # noqa: BLE001
                    self.d.log("folder %s: %s: %s" % (path, n, e))
            st[key] = {"seen": {n: s for n, s in seen.items() if n in stamps}}
        live = set("%s:%s" % (a["id"], p) for a, _, p in entries)
        for k in [k for k in st if k not in live]:
            st.pop(k, None)
        self.d.state.set("folders", st)
        return POLL_S if entries else 60

    def _wake(self, agent, w, full, name, size):
        ext = os.path.splitext(name)[1].lower()
        # whoever dropped the file chose its name: it is told to the model inside the data, as text
        shown = rulesmod.plain(name, 120) or "a file with no name"
        att, data = None, "a file named %s, %d bytes" % (shown, size)
        if size > FILE_MAX:
            data += " (too large to attach: it stays in the folder)"
        else:
            fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise OSError("not a regular file")
                raw = f.read(FILE_MAX + 1)
            is_text = ext not in IMAGE_EXT and (ext in TEXT_EXT or (b"\x00" not in raw[:4096] and size < TEXT_MAX * 4))
            # a file that is attached is named to the model beside the data, not inside it (its name, and where
            # its copy was saved). Whoever dropped it chose that name, so the copy goes by a plain one; the
            # file's own name is told inside the data, above
            kept = name if is_text else "dropped-%s-%s%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6],
                                                           ext if re.match(r"^\.[a-z0-9]{1,8}$", ext) else "")
            att = self.d.save_file(agent["id"], kept, raw)
            if ext in IMAGE_EXT:
                att["kind"] = "image"
            elif is_text:
                text = raw.decode("utf-8", "replace")
                att.update(kind="text", text=text[:TEXT_MAX])
                data = "File name: %s\n\n%s%s" % (shown, text[:TEXT_MAX], "\n[the file goes on: %d more characters]"
                                                    % (len(text) - TEXT_MAX) if len(text) > TEXT_MAX else "")
            else:
                att["kind"] = "file"
        ask = w.get("prompt") or "A file was dropped in your folder. Read it and say what it is and what, if anything, to do with it."
        # (the name is inside the data, with the file's text: beside it stands only what kind of thing this is)
        self.d.wake(agent["id"], "folder", ask, trust="untrusted", data=data, data_source="a file dropped in your folder",
                    attachments=[att] if att and att.get("kind") != "text" else None,
                    dedupe="folder:%s:%s" % (full, int(os.path.getmtime(full))), title=name[:120])
