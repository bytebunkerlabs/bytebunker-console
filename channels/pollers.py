"""Pulled events: small pollers that turn something new on the outside into a wake.

    {"kind": "poller", "what": "rss",  "url": "https://example.com/feed.xml", "every_min": 30, "prompt": "..."}
    {"kind": "poller", "what": "json", "url": "https://example.com/items.json", "path": "data.items"}
    {"kind": "poller", "what": "github"}                      your notifications
    {"kind": "poller", "what": "ci", "repo": "owner/name"}    workflow runs that did not pass

Each keeps a cursor (what it has already shown), so an item wakes its agent
once. The first look only takes note of what is there: an agent is not woken
for a feed's history. What a feed says reaches the agent as data from that
feed, never as an instruction; your prompt is the ask.

GitHub uses the token you connected for github.com (Tools, Plugins, Connect a
git host). Nothing here listens: every poller asks, on its own interval.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

MAX_BYTES = 2 * 1024 * 1024
SEEN_KEEP = 600
UA = "ByteBunker (agents poller)"
_TAGS = re.compile(r"<[^>]+>")


_fetching = {}      # url -> the thread still reading it


def fetch(url, headers=None, timeout=20, total=45):
    """The page, or TimeoutError when it is not all there within `total` seconds, however the
    server spends them: saying nothing, sending its headers a byte at a time, or its body. The
    reading is done beside the caller, which goes on at the deadline; a server that still drips
    bytes then is not read a second time until that reading has ended."""
    last = _fetching.get(url)
    if last is not None and last.is_alive():
        raise TimeoutError("the last download from there has not ended yet")
    box = {}

    def work():
        try:
            box["out"] = _read(url, headers, timeout, total)
        except BaseException as e:   # noqa: BLE001 - handed to the caller as it is
            box["err"] = e
    t = threading.Thread(target=work, name="fetch", daemon=True)
    _fetching[url] = t
    t.start()
    t.join(total + 0.5)
    if t.is_alive():
        raise TimeoutError("the download took more than %d s" % total)
    _fetching.pop(url, None)
    if "err" in box:
        raise box["err"]
    return box["out"]


def _read(url, headers, timeout, total):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA, "Accept": "*/*"}, **(headers or {})))
    end = time.time() + total
    out = b""
    with urllib.request.urlopen(req, timeout=min(timeout, total)) as r:
        while len(out) <= MAX_BYTES:
            if time.time() > end:
                raise TimeoutError("the download took more than %d s" % total)
            chunk = r.read1(65536)
            if not chunk:
                break
            out += chunk
    return out[:MAX_BYTES]


def _text(el):
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def rss_items(raw):
    """Items of an RSS or Atom feed: [{"id", "title", "link", "summary"}], newest as the feed lists them."""
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for c in el:
            name = _local(c.tag)
            if name == "link":
                f.setdefault("link", (c.get("href") or _text(c)).strip())
            elif name in ("title", "guid", "id", "description", "summary", "content", "pubdate", "updated", "published"):
                f.setdefault(name, _text(c))
        summary = _TAGS.sub(" ", f.get("description") or f.get("summary") or f.get("content") or "")
        ident = f.get("guid") or f.get("id") or f.get("link") or hashlib.sha1((f.get("title") or "").encode()).hexdigest()
        out.append({"id": ident[:300], "title": (f.get("title") or "")[:300], "link": (f.get("link") or "")[:500],
                    "summary": " ".join(summary.split())[:1500],
                    "when": f.get("pubdate") or f.get("updated") or f.get("published") or ""})
    return out


def json_items(obj, path=""):
    """A list inside a JSON document (path "a.b.c", or the document itself)."""
    cur = obj
    for key in [k for k in str(path or "").split(".") if k]:
        cur = cur.get(key) if isinstance(cur, dict) else None
    if not isinstance(cur, list):
        raise ValueError("no list at %r in that JSON" % (path or "the top"))
    out = []
    for it in cur[:200]:
        if isinstance(it, dict):
            ident = str(it.get("id") or it.get("guid") or it.get("url") or it.get("link") or
                        hashlib.sha1(json.dumps(it, sort_keys=True).encode()).hexdigest())
            out.append({"id": ident[:300], "title": str(it.get("title") or it.get("name") or "")[:300],
                        "link": str(it.get("url") or it.get("link") or it.get("html_url") or "")[:500],
                        "summary": json.dumps(it, ensure_ascii=False)[:1500], "when": str(it.get("updated_at") or it.get("date") or "")})
        else:
            s = str(it)
            out.append({"id": hashlib.sha1(s.encode()).hexdigest(), "title": s[:300], "link": "", "summary": "", "when": ""})
    return out


class Pollers:
    """deps: store, state, wake(agent_id, source, text, **kw), git_token(host) -> token or None, log."""

    def __init__(self, deps):
        self.d = deps
        self.api = (os.environ.get("BYTEBUNKER_GITHUB_API") or "https://api.github.com").rstrip("/")
        self.errors = {}

    def entries(self):
        out = []
        for a in self.d.store.list():
            if not a.get("enabled", True):
                continue
            for i, w in enumerate(a.get("wake") or []):
                if w.get("kind") == "poller" and w.get("enabled") is not False:
                    spec = json.dumps({k: w.get(k) for k in ("what", "url", "repo", "path")}, sort_keys=True)
                    out.append((a, w, "%s:%s" % (a["id"], hashlib.sha1(spec.encode()).hexdigest()[:10])))
        return out

    def poll(self, now=None):
        now = now if now is not None else time.time()
        entries = self.entries()
        st = self.d.state.get("pollers", {}) or {}
        for agent, w, key in entries:
            rec = st.get(key) or {}
            if float(rec.get("next") or 0) > now:
                continue
            every = max(1, int(w.get("every_min") or 10)) * 60
            try:
                items = self.items(w)
                self.errors.pop(key, None)
            except Exception as e:   # noqa: BLE001 - a feed that is away is asked again next time
                self.errors[key] = "%s: %s" % (type(e).__name__, str(e)[:200])
                self.d.log("poller %s (%s): %s" % (key, w.get("what"), self.errors[key]))
                items = None
                getattr(e, "close", lambda: None)()          # an HTTP error holds its response open
            seen = list(rec.get("seen") or [])
            fresh = []
            if items is not None:
                known = set(seen)
                fresh = [it for it in items if it["id"] not in known]
                if rec.get("primed"):
                    # ten a look, oldest first; the newer ones stay new and come at the next looks
                    taken = fresh[-10:]
                    for it in taken[::-1]:
                        self._wake(agent, w, it)
                else:
                    taken = fresh                             # the first look: what is there is history
                # everything a feed lists stays known, however long the feed is
                seen = (seen + [it["id"] for it in taken])[-max(SEEN_KEEP, 2 * len(items)):]
            st[key] = {"seen": seen, "next": now + every, "primed": bool(rec.get("primed") or items is not None),
                       "last": round(now), "new": len(fresh) if rec.get("primed") else 0}
        live = set(k for _, _, k in entries)
        for k in [k for k in st if k not in live]:
            st.pop(k, None)                                    # an entry that is gone takes its cursor with it
        self.d.state.set("pollers", st)
        return 20 if entries else 60

    def items(self, w):
        what = w.get("what")
        if what == "rss":
            return rss_items(fetch(w["url"], {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}))
        if what == "json":
            return json_items(json.loads(fetch(w["url"], {"Accept": "application/json"}).decode("utf-8", "replace")), w.get("path"))
        token = self.d.git_token("github.com")
        if not token:
            raise RuntimeError("no GitHub token: connect github.com under Tools, Plugins")
        auth = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if what == "github":
            rows = json.loads(fetch(self.api + "/notifications?per_page=30", auth).decode("utf-8", "replace"))
            return [{"id": "%s:%s" % (n.get("id"), n.get("updated_at")),
                     "title": ((n.get("subject") or {}).get("title") or "")[:300],
                     "link": (n.get("repository") or {}).get("html_url") or "",
                     "summary": "%s in %s (%s)" % ((n.get("subject") or {}).get("type") or "notification",
                                                   (n.get("repository") or {}).get("full_name") or "?", n.get("reason") or ""),
                     "when": n.get("updated_at") or ""} for n in rows if isinstance(n, dict)]
        if what == "ci":
            d = json.loads(fetch("%s/repos/%s/actions/runs?per_page=20" % (self.api, w["repo"]), auth).decode("utf-8", "replace"))
            out = []
            for r in d.get("workflow_runs") or []:
                if r.get("status") != "completed" or r.get("conclusion") in ("success", "skipped", "neutral"):
                    continue                                   # what passed is not news
                out.append({"id": "run:%s" % r.get("id"), "title": "%s: %s on %s" % (r.get("name"), r.get("conclusion"), r.get("head_branch")),
                            "link": r.get("html_url") or "", "when": r.get("updated_at") or "",
                            "summary": "workflow %s, run %s, commit %s by %s: %s" % (
                                r.get("name"), r.get("run_number"), str(r.get("head_sha") or "")[:8],
                                (r.get("actor") or {}).get("login") or "?", ((r.get("head_commit") or {}).get("message") or "")[:200])})
            return out
        raise ValueError("unknown poller %r" % what)

    def _wake(self, agent, w, it):
        what = w.get("what")
        where = w.get("url") or w.get("repo") or "GitHub"
        ask = w.get("prompt") or {
            "ci": "A CI run did not pass. Say what failed and what to look at first.",
            "github": "A GitHub notification arrived. Say in a line or two whether it needs your owner.",
        }.get(what, "Something new appeared in a feed you watch. Say in a line or two whether it matters to your purpose.")
        data = "%s\n%s\n%s\n%s" % (it.get("title") or "", it.get("link") or "", it.get("when") or "", it.get("summary") or "")
        self.d.wake(agent["id"], "poller:" + what, ask, trust="untrusted", data=data.strip(),
                    data_source="%s (%s)" % (where, what), dedupe="poller:%s:%s:%s" % (what, where, it["id"]),
                    title=(it.get("title") or "")[:120])
