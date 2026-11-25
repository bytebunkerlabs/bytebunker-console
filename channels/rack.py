"""The rack as a wake source: what the monitors say changed.

    {"kind": "rack", "events": ["engine_down", "model_switched", "disk_high"],
     "disk_pct": 90, "recipe": "glm53-flash-keys", "prompt": "..."}

Every half minute the monitors' picture of each node is compared with the one
before. A change becomes an event, and an event wakes every agent that
listed it:

    node_down / node_up       a node stopped or resumed answering its monitor
    engine_down / engine_up   a node that serves a recipe has no engine that
                              answers (seen on two looks in a row: a monitor
                              that blinks is not an outage), or has one again
    model_starting            rack up began loading a model
    model_switched            a node serves a different recipe than before
    disk_high                 a disk went over the threshold (90% unless the wake says)

An engine_down also carries what was serving, so the agent (or its rule) knows
which recipe brings it back. What happened is kept in the agent's memory by
this module itself, with the monitor as its source: the morning report can
say when the engine went down, what was done and how long it took, whether or
not a model was up to hear about it at the time.
"""
import re
import time

POLL_S = 30
DISK_PCT = 90


def picture(cluster):
    """What matters about each node, from a monitor's /v1/cluster answer."""
    out = {}
    for n in (cluster or {}).get("nodes") or []:
        name = n.get("name")
        if not name:
            continue
        sv = n.get("serving") if isinstance(n.get("serving"), dict) else {}
        st = n.get("starting") if isinstance(n.get("starting"), dict) else {}
        engines = [e for e in n.get("engines") or [] if e.get("kind") != "openai"]
        disks = {}
        for d in n.get("disks") or []:
            if d.get("total"):
                disks[d.get("mount") or "/"] = round(100.0 * d["used"] / d["total"], 1)
        out[name] = {"ok": n.get("ok") is not False, "monitor": str(n.get("monitor") or ""), "recipe": sv.get("recipe") or "",
                     "model": sv.get("gateway_name") or sv.get("served_name") or sv.get("model") or "",
                     "engine": any(e.get("ok", True) for e in engines),
                     "loading": st.get("state") == "loading", "loading_recipe": st.get("recipe") or "",
                     "started_at": st.get("started_at") or 0, "disks": disks}
    return out


def diff(old, new, misses=None, disk_pct=DISK_PCT):
    """The events between two pictures. misses: {node: looks in a row with a recipe
    and no engine}, kept by the caller, so one blink is not an outage."""
    misses = misses if misses is not None else {}
    events = []
    for name, n in new.items():
        o = old.get(name)
        if o is None:
            misses[name] = 0
            continue
        if o["ok"] and not n["ok"]:
            events.append({"kind": "node_down", "node": name, "recipe": o["recipe"], "model": o["model"]})
        elif not o["ok"] and n["ok"]:
            events.append({"kind": "node_up", "node": name})
        if not n["ok"]:
            continue
        expects = bool(n["recipe"]) and not n["loading"]
        if n["engine"]:
            if misses.get(name, 0) >= 2:
                events.append({"kind": "engine_up", "node": name, "recipe": n["recipe"], "model": n["model"]})
            misses[name] = 0
        elif expects:
            misses[name] = misses.get(name, 0) + 1
            if misses[name] == 2:
                events.append({"kind": "engine_down", "node": name, "recipe": n["recipe"], "model": n["model"]})
        elif not n["recipe"] and not n["loading"]:
            misses[name] = 0                    # nothing is meant to serve here any more: not an outage
        # (loading, with no engine yet: an outage that is being mended keeps its count)
        if n["loading"] and (not o["loading"] or n["started_at"] != o["started_at"]):
            events.append({"kind": "model_starting", "node": name, "recipe": n["loading_recipe"]})
        if n["recipe"] and o["recipe"] and n["recipe"] != o["recipe"]:
            events.append({"kind": "model_switched", "node": name, "recipe": n["recipe"], "was": o["recipe"], "model": n["model"]})
        for mount, pct in n["disks"].items():
            was = o["disks"].get(mount)
            if was is not None and was < disk_pct <= pct:
                events.append({"kind": "disk_high", "node": name, "mount": mount, "pct": pct})
    for e in events:
        for k in ("node", "recipe", "model", "was", "mount", "rack"):
            if e.get(k):
                e[k] = name_of(e[k], k)         # what a monitor sends is a name, never a sentence
    return events


_NOT_IN = {"node": r"[^A-Za-z0-9._-]+", "recipe": r"[^A-Za-z0-9._-]+", "was": r"[^A-Za-z0-9._-]+",
           "rack": r"[^A-Za-z0-9._-]+", "mount": r"[^A-Za-z0-9._/-]+", "model": r"[^A-Za-z0-9._:/@+-]+"}
_WHOLE = ("node", "rack", "mount")          # names an owner gives: kept whole, since two nodes must stay two
_RECIPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GONE_FORGET = 7 * 24 * 3600 // POLL_S       # looks after which a node no monitor lists is forgotten (a week)


def name_of(text, kind="model"):
    """What a monitor sends where a name belongs, as a name, in the letters that kind of name
    is written in. A model's or a recipe's name has no space, so it ends at its first word (a
    sentence does not ride in on it). A node's name is its owner's choice: it is kept whole,
    its spaces as hyphens, so that "dgx spark 1" and "dgx spark 2" stay two nodes."""
    words = str(text).split()
    first = "-".join(words) if kind in _WHOLE else (words or [""])[0]
    return re.sub(_NOT_IN.get(kind, _NOT_IN["model"]), "", first)[:64]


def carry(old, new, monitors=None):
    """Nodes the last picture had and this one lacks (their monitor is away, or every monitor
    is): kept in the picture, and down from the second look in a row that misses them. One
    that stays missing for a week (renamed, taken out of the rack) is forgotten, and one whose
    monitor is no longer in this app (monitors: the names that are) is forgotten at once:
    taking a monitor out is not an outage."""
    for name, o in (old or {}).items():
        if name not in new:
            if monitors is not None and o.get("monitor") and o["monitor"] not in monitors:
                continue
            gone = int(o.get("gone") or 0) + 1
            if gone >= GONE_FORGET:
                continue
            new[name] = dict(o, gone=gone, ok=bool(o.get("ok")) and gone < 2, engine=False if gone >= 2 else o.get("engine"))
    return new


def words(e):
    k, node = e["kind"], e.get("node")
    what = e.get("model") or e.get("recipe") or "the model"
    return {
        "node_down": "%s stopped answering its monitor%s." % (node, " (it was serving %s)" % what if e.get("recipe") else ""),
        "node_up": "%s answers its monitor again." % node,
        "engine_down": "The engine on %s is down: it should serve %s (recipe %s) and nothing answers." % (node, what, e.get("recipe")),
        "engine_up": "The engine on %s answers again: %s (recipe %s)." % (node, what, e.get("recipe")),
        "model_starting": "%s began loading recipe %s." % (node, e.get("recipe")),
        "model_switched": "%s now serves recipe %s (it served %s)." % (node, e.get("recipe"), e.get("was")),
        "disk_high": "The disk %s on %s is %s%% full." % (e.get("mount"), node, e.get("pct")),
    }.get(k, k)


class RackWatch:
    """deps: store, state, cluster() -> the monitors' merged answer (or None when there
    is no monitor), wake(agent_id, source, text, **kw), remember(agent_id, fact, name), log."""

    def __init__(self, deps):
        self.d = deps
        self.misses = {}
        self.error = None

    def listeners(self):
        out = []
        for a in self.d.store.list():
            if not a.get("enabled", True):
                continue
            for w in a.get("wake") or []:
                if w.get("kind") == "rack" and w.get("enabled") is not False:
                    out.append((a, w))
        return out

    def poll(self, now=None):
        now = now if now is not None else time.time()
        listeners = self.listeners()
        if not listeners:
            return 60
        try:
            cluster = self.d.cluster()
            self.error = None
        except Exception as e:   # noqa: BLE001
            self.error = str(e)[:200]
            cluster = {}                                # a monitor that did not answer
        old = self.d.state.get("picture")
        if cluster is None:
            # this app has no monitor (any more): nothing is watched, so nothing is missing. What was
            # known goes; a monitor added again starts from a first picture
            if old:
                self.misses = {}
                self.d.state.update(lambda d: d.update(picture=None, misses={}, down_since={}))
            return 60
        if not (cluster and cluster.get("nodes")) and not isinstance(old, dict):
            return POLL_S                               # nothing known yet, and nothing to compare with
        # a monitor that is away lists nothing: the nodes it listed before are then missing, which after
        # two looks in a row is an outage worth a wake (the head going dark takes its monitor with it)
        mons = cluster.get("monitors") if isinstance(cluster, dict) else None
        new = carry(old if isinstance(old, dict) else {}, picture(cluster) if cluster and cluster.get("nodes") else {},
                    {m.get("name") for m in mons if isinstance(m, dict)} if isinstance(mons, list) else None)
        self.misses = dict(self.d.state.get("misses") or self.misses)
        down_since = dict(self.d.state.get("down_since") or {})
        if isinstance(old, dict):
            by_pct = {}
            for a, w in listeners:
                by_pct.setdefault(int(w.get("disk_pct") or DISK_PCT), []).append((a, w))
            fired = set()
            for pct, group in by_pct.items():
                misses = dict(self.misses)
                for e in diff(old, new, misses, pct):
                    key = (e["kind"], e.get("node"), e.get("mount"))
                    for a, w in group:
                        if e["kind"] in (w.get("events") or []) and (key, a["id"]) not in fired:
                            fired.add((key, a["id"]))
                            self._wake(a, w, e, now, down_since)
                last = misses
            self.misses = last
        else:
            diff({}, new, self.misses)                  # the first picture: nothing to compare with
        # (what was kept about a node that is forgotten goes with it)
        self.misses = {k: v for k, v in self.misses.items() if k in new}
        alive = {name_of(n, "node") for n in new} | {"rack"}         # (events name a node by its cleaned name)
        down_since = {k: v for k, v in down_since.items() if k.split(":", 1)[-1] in alive}
        self.d.state.update(lambda d: d.update(picture=new, misses=self.misses, down_since=down_since))
        return POLL_S

    def _wake(self, agent, w, e, now, down_since):
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        text = words(e)
        node = e.get("node") or "rack"
        key = "%s:%s" % (agent["id"], node)
        if e["kind"] in ("engine_down", "node_down"):
            down_since[key] = now
            self.d.remember(agent["id"], "%s %s" % (stamp, text), "rack-%s-%s" % (e["kind"].replace("_", "-"), time.strftime("%Y%m%d-%H%M", time.localtime(now))))
        elif e["kind"] in ("engine_up", "node_up") and down_since.get(key):
            mins = max(1, round((now - down_since.pop(key)) / 60.0))
            text += " It was down for %d min." % mins
            self.d.remember(agent["id"], "%s %s" % (stamp, text), "rack-%s-%s" % (e["kind"].replace("_", "-"), time.strftime("%Y%m%d-%H%M", time.localtime(now))))
        ask = "The rack monitor reports: %s" % text
        if w.get("prompt"):
            ask += "\n\n" + w["prompt"]
        elif e["kind"] == "engine_down":
            ask += "\n\nLook at the rack (rack_status), say what is down, and bring it back if your rules allow it."
        # the recipe to bring back: the one the owner pinned on this wake, else the one that node was
        # serving (two engines serve two recipes), else the last one this app saw served anywhere
        known = getattr(self.d, "last_recipe", lambda: "")() or ""
        theirs = e.get("recipe") if _RECIPE.match(e.get("recipe") or "") else ""
        event = dict(e, at=round(now), recipe=w.get("recipe") or theirs or known or "")
        self.d.wake(agent["id"], "rack", ask, trust="system", event=event,
                    dedupe="rack:%s:%s:%s:%d" % (e["kind"], node, e.get("mount") or "", int(now // 300)), dedupe_s=600,
                    title=text[:120])
