"""The model router: which live model fits each role right now, and what to
do when none does.

A pure function over three inputs, so it is tested without a GPU:

  cards   one capability card per model name (model_facts.py keeps what was
          learned; card() below fills in the rest): tier, tools, vision,
          thinking, ctx, speed, uncensored
  live    what answers now: the gateways' models, what the monitors say is
          loading (and for how long), how busy each model is, and the
          recipes a rack could serve instead
  needs   what each role must have and would like: the agent's own, over
          these defaults: the master needs tools and a big tier; thinking
          roles need thinking and a big tier; fast roles need speed
          (prefer: speed makes any role take the quickest model that fits)

    plan = route(("master",), needs, cards, live, shape="chat")
    plan["ok"], plan["roles"]["master"]["model"], plan["degraded"]

What a role asks for comes in three strengths:

  must have   tools, vision, uncensored, a model set by hand: never traded
              away, whatever is down
  wants       tier and context window: given up only by "degrade", and the
              answer then says so
  prefers     thinking and speed: they rank the models that fit and never
              keep a run from starting (a rack with one model runs every
              role on it)

When a role has no model that fits, there are four moves, tried in this
order for a chat ask: degrade, wait, ask, park; and for a scheduled goal:
wait, ask, park (an agent's rules can say otherwise):

  wait     the model is loading and the monitor says for how long
  degrade  a lower tier is live: run on it and flag the answer
  ask      nothing serves the role but a recipe could: propose rack switch
  park     nothing fits: keep the wake and try again later

A run pins its models at the start; a switch on the rack mid-run does not
move a running master.
"""
import re

ROLES = ("master", "thinking", "fast")
TIER = {"big": 3, "medium": 2, "small": 1}
SPEED = {"fast": 3, "medium": 2, "slow": 1}
HARD = ("tools", "vision", "uncensored")
DEFAULT_NEEDS = {
    "master": {"tools": True, "tier": "big"},
    "thinking": {"thinking": True, "tier": "big"},
    "fast": {"speed": "fast"},
}
MOVES = ("wait", "degrade", "ask", "park")
ORDER = {"chat": ["degrade", "wait", "ask", "park"], "goal": ["wait", "ask", "park"],
         # a turn nobody is waiting for (a schedule, a mail, a feed, a file): it waits for a model that is
         # loading, and only then settles for a smaller one
         "background": ["wait", "degrade", "ask", "park"]}
RETRY_S = {"wait": 45, "ask": 120, "park": 120}
# what a recipe's ROLES line (dgx-serve) says a model is good for, as a need would ask
BIG_FAMILIES = ("glm-5", "glm5", "deepseek", "dsv4", "inkling", "nemotron", "bolt", "kimi", "minimax", "qwen3-max",
                "llama-4", "mistral-large", "command-r-plus", "gpt-oss-120")
_PARAMS = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*b(?![a-z0-9])", re.I)


def guess_tier(model_id, roles=()):
    """big, medium or small, from what is known without asking the model: the
    recipe's roles, a parameter count in the name, a known family."""
    tags = set(roles or ())
    if tags & {"big", "thinking"}:
        return "big"
    mid = (model_id or "").lower()
    m = _PARAMS.search(mid.replace("_", "-"))
    if m:
        try:
            n = float(m.group(1))
            return "small" if n < 10 else ("medium" if n < 40 else "big")
        except ValueError:
            pass
    if tags == {"fast"}:
        return "small"
    if any(f in mid for f in BIG_FAMILIES):
        return "big"
    return "medium"


def speed_class(tok_s, tier="medium"):
    """fast, medium or slow: measured tokens per second when there are any, else by size."""
    if isinstance(tok_s, (int, float)) and tok_s > 0:
        return "fast" if tok_s >= 55 else ("medium" if tok_s >= 18 else "slow")
    return {"small": "fast", "medium": "medium", "big": "medium"}.get(tier, "medium")


def card(model_id, caps=None, learned=None, roles=(), served_by=None):
    """One model's capability card: what the capability table and the engine
    say (caps), over what was measured and what a person set (learned, from
    model_facts), with guesses for the rest."""
    caps, learned = caps or {}, learned or {}
    mid = (model_id or "").lower()
    tier = learned.get("tier") if learned.get("tier") in TIER else guess_tier(model_id, roles)
    tok_s = learned.get("tok_s")
    thinking = learned.get("thinking")
    if thinking is None:
        thinking = bool(caps.get("effort") or caps.get("thinking_switch") or (caps.get("ctk") or {}).get("thinking")
                        or caps.get("reasoning") or "thinking" in set(roles or ()))
    c = {"model": model_id, "tier": tier,
         "tools": bool(learned["tools"]) if "tools" in learned else bool(caps.get("tools", True)),
         "vision": bool(learned["vision"]) if "vision" in learned else bool(caps.get("vision") or "vision" in set(roles or ())),
         "thinking": bool(thinking),
         "ctx": int(caps.get("ctx") or learned.get("ctx") or 0),
         "speed": learned.get("speed") if learned.get("speed") in SPEED else speed_class(tok_s, tier),
         "tok_s": round(float(tok_s), 1) if isinstance(tok_s, (int, float)) else None,
         "uncensored": bool(learned["uncensored"]) if "uncensored" in learned
         else any(w in mid for w in ("uncensored", "ablit", "abliterated")),
         "fail_rate": learned.get("fail_rate"),
         "last_live": learned.get("last_live"),          # when a gateway last listed it
         "served_by": list(served_by or [])}
    return c


def needs_for(role, own=None):
    """A role's needs: the agent's own over the role's defaults."""
    n = dict(DEFAULT_NEEDS.get(role) or {})
    own = own or {}
    if own.get("prefer") == "speed" and "tier" not in own:
        n.pop("tier", None)             # the quickest model that can do the job, whatever its size
    for k, v in own.items():
        if v in (None, ""):
            continue
        n[k] = v
    return n


def relax(need, own, cards):
    """A role's default tier means "the most capable model you have". With no
    model of that tier known at all (live now, or seen before), it is the best
    tier there is: a rack whose biggest model is a medium one is not degraded,
    it is what it is. A tier the agent asked for by name stays as asked."""
    if not need.get("tier") or (own or {}).get("tier"):
        return need
    top = max([TIER.get(c.get("tier"), 0) for c in cards.values() if not hard_misses(c, need)], default=0)
    if top and top < TIER[need["tier"]]:
        return dict(need, tier=next(k for k, v in TIER.items() if v == top))
    return need


def hard_misses(c, need):
    """The must-haves this model lacks (an empty list: it can do the job at all)."""
    out = []
    for k in HARD:
        if need.get(k) and not c.get(k):
            out.append(k)
    return out


def soft_misses(c, need):
    """What this model lacks of what the role wants (tier, window): what degrading to it gives up."""
    out = []
    if need.get("tier") and TIER.get(c.get("tier"), 0) < TIER.get(need["tier"], 0):
        out.append("tier")
    if need.get("ctx_min") and c.get("ctx") and c["ctx"] < int(need["ctx_min"]):
        out.append("ctx_min")
    return out


def wish_misses(c, need):
    """What this model lacks of what the role only prefers (thinking, speed): it still fits."""
    out = []
    if need.get("thinking") and not c.get("thinking"):
        out.append("thinking")
    if need.get("speed") and SPEED.get(c.get("speed"), 0) < SPEED.get(need["speed"], 0):
        out.append("speed")
    return out


def _load(live, model):
    ld = (live.get("load") or {}).get(model) or {}
    return int(ld.get("running") or 0) + int(ld.get("waiting") or 0)


def _rank(role, c, need, live):
    """Higher is better. The master and the thinking roles want the most
    capable model; fast roles the quickest; then whichever has room."""
    misses = len(soft_misses(c, need))
    wishes = len(wish_misses(c, need))
    load = _load(live, c["model"])
    fail = c.get("fail_rate") or 0
    if role == "fast" or need.get("prefer") == "speed":
        return (-misses, SPEED.get(c.get("speed"), 0), c.get("tok_s") or 0, -load, -fail, c["model"])
    return (-misses, -wishes, TIER.get(c.get("tier"), 0), -load, c.get("ctx") or 0, -fail, c["model"])


def _describe(c, live):
    bits = [c["tier"]]
    if c.get("tools"):
        bits.append("tools")
    if c.get("thinking"):
        bits.append("thinking")
    if c.get("vision"):
        bits.append("vision")
    if c.get("ctx"):
        bits.append("%dk window" % round(c["ctx"] / 1000))
    if c.get("tok_s"):
        bits.append("%g tok/s" % c["tok_s"])
    else:
        bits.append(c.get("speed") or "")
    bits.append("%d running" % _load(live, c["model"]))
    return ", ".join(b for b in bits if b)


def _need_words(need):
    out = []
    for k in ("tools", "vision", "uncensored", "thinking"):
        if need.get(k):
            out.append(k)
    if need.get("tier"):
        out.append("a %s tier" % need["tier"])
    if need.get("ctx_min"):
        out.append("a %dk window" % round(int(need["ctx_min"]) / 1000))
    if need.get("speed"):
        out.append("%s speed" % need["speed"])
    return ", ".join(out) or "nothing in particular"


def _gave_up(misses, c, need):
    w = []
    for k in misses:
        if k == "tier":
            w.append("a %s model, not a %s one" % (c["tier"], need["tier"]))
        elif k == "ctx_min":
            w.append("a %dk window, not %dk" % (round((c.get("ctx") or 0) / 1000), round(int(need["ctx_min"]) / 1000)))
    return "; ".join(w)


def _wish_words(c, need):
    w = []
    for k in wish_misses(c, need):
        if k == "thinking":
            w.append("it has no thinking switch")
        elif k == "speed":
            w.append("it is %s, not %s" % (c.get("speed"), need["speed"]))
    return "; ".join(w)


def _starting_for(need, cards, live, pin=None):
    """A model that is loading now and would fit, with what the monitor says about it."""
    for s in live.get("starting") or []:
        if s.get("state") not in (None, "loading"):
            continue                    # failed, interrupted, stopped: not on its way
        names = [n for n in (s.get("gateway_name"), s.get("served_name"), s.get("model")) if n]
        if pin and pin not in names and pin.rsplit("@", 1)[0] not in names:
            continue
        c = next((cards[n] for n in names if n in cards), None)
        if c is None:
            # never served here before: its recipe's roles are all there is to go by
            c = card(names[0] if names else (s.get("recipe") or "?"), roles=s.get("roles") or ())
        if not hard_misses(c, need) and (pin or not soft_misses(c, need)):
            return s, c
    return None, None


def _recipe_for(need, live, wanted=None):
    """A recipe a rack could serve that would fit: the one that served the
    wanted model last, else one whose roles say it does this kind of work."""
    recipes = [r for r in live.get("recipes") or [] if r.get("name") and r.get("fits") is not False]
    last = live.get("last_recipe") or {}
    if last.get("recipe") and (not wanted or wanted in (last.get("names") or [])):
        hit = next((r for r in recipes if r["name"] == last["recipe"] and (not last.get("rack") or r.get("rack") in (None, last.get("rack")))), None)
        if hit or not recipes:
            return {"recipe": last["recipe"], "rack": last.get("rack"), "why": "it served %s last" % (wanted or "here")}
    tags = set()
    if need.get("tier") == "big":
        tags.add("big")
    if need.get("thinking"):
        tags.add("thinking")
    if need.get("vision"):
        tags.add("vision")
    if need.get("speed") == "fast" and need.get("tier") != "big":
        tags.add("fast")
    for r in recipes:
        roles = set(r.get("roles") or [])
        if tags and tags <= roles:
            return {"recipe": r["name"], "rack": r.get("rack"), "why": "its roles are " + ", ".join(sorted(roles))}
    return None


def _loading_words(s):
    name = s.get("gateway_name") or s.get("served_name") or s.get("recipe") or "a model"
    mins = s.get("minutes")
    if mins is None and s.get("started_at"):
        try:
            import time
            mins = max(0.0, (time.time() - float(s["started_at"])) / 60.0)
        except (TypeError, ValueError):
            mins = None
    return "%s is loading%s" % (name, ", %d min so far" % round(mins) if isinstance(mins, (int, float)) else "")


def choose(role, need, cards, live, pin=None, order=None):
    """One role's choice. Returns {"model", "why", ...} when a model fits (or
    one was degraded to), else {"move": wait|ask|park, "why", ...}."""
    order = [m for m in (order or ORDER["chat"]) if m in MOVES]
    if "park" not in order:
        order.append("park")
    live_ids = [m["id"] for m in live.get("models") or [] if m.get("served", True) is not False]
    pool = [cards[i] for i in live_ids if i in cards]
    usable = [c for c in pool if not hard_misses(c, need)]
    wanted = None
    if pin:
        base = pin.rsplit("@", 1)[0]
        if pin in live_ids or base in live_ids:
            c = cards.get(pin) or cards.get(base) or card(pin)
            return {"model": pin, "why": "set by hand", "card": c, "misses": []}
        wanted = pin
    else:
        full = [c for c in usable if not soft_misses(c, need)]
        if full:
            best = max(full, key=lambda c: _rank(role, c, need, live))
            others = len(full) - 1
            lacks = _wish_words(best, need)
            return {"model": best["model"], "card": best, "misses": [], "wishes": wish_misses(best, need),
                    "why": "%s: %s; it needs %s%s" % (
                        "the only live model that fits" if not others else "the best of %d live models that fit" % len(full),
                        _describe(best, live), _need_words(need), " (%s)" % lacks if lacks else "")}
        # what would have fitted, had it been up: for the flag and the proposal
        known = [c for c in cards.values() if c["model"] not in live_ids and not hard_misses(c, need) and not soft_misses(c, need)]
        if known:
            # the one that was up most recently is the one that "was down"
            wanted = max(known, key=lambda c: (c.get("last_live") or 0,) + _rank(role, c, need, {})).get("model")
    starting, _sc = _starting_for(need, cards, live, pin)
    for move in order:
        if move == "wait" and starting is not None:
            return {"move": "wait", "why": _loading_words(starting), "notice": _loading_words(starting),
                    "wanted": wanted or starting.get("gateway_name") or starting.get("served_name"),
                    "retry_s": RETRY_S["wait"], "starting": starting}
        if move == "degrade" and usable:
            best = max(usable, key=lambda c: _rank(role, c, need, live))
            misses = soft_misses(best, need)
            flag = "on %s; %s" % (best["model"], ("%s was down" % wanted) if wanted else "nothing better was live")
            return {"model": best["model"], "card": best, "misses": misses, "wishes": wish_misses(best, need),
                    "degraded": flag, "wanted": wanted,
                    "why": "degraded: %s (%s)" % (flag, _gave_up(misses, best, need) or "the nearest fit")}
        if move == "ask":
            prop = _recipe_for(need, live, wanted)
            if prop:
                return {"move": "ask", "propose": prop, "wanted": wanted, "retry_s": RETRY_S["ask"],
                        "why": "nothing live has %s; %s could serve it (%s)" % (_need_words(need), prop["recipe"], prop["why"])}
        if move == "park":
            break
    lacking = "no model is live" if not pool else "no live model has %s" % _need_words(need)
    return {"move": "park", "wanted": wanted, "retry_s": RETRY_S["park"],
            "why": lacking + (" (%s is not served)" % wanted if wanted else "")}


def route(roles, needs=None, cards=None, live=None, shape="chat", order=None, pins=None, slots=None):
    """Fill the roles of one run. Returns the plan:

      ok        every role has a model
      roles     {role: {"model", "why", "degraded"?, "pool"?}}
      degraded  the flag for the answer when a role settled for less, else None
      move      wait | ask | park when not ok, with role, why, notice, retry_s, propose
    """
    cards, live, needs, pins = cards or {}, live or {}, needs or {}, pins or {}
    order = order or ORDER.get(shape) or ORDER["chat"]
    plan = {"ok": True, "roles": {}, "degraded": None, "move": None, "shape": shape}
    flags = []
    for role in [r for r in ROLES if r in roles]:
        need = relax(needs_for(role, needs.get(role)), needs.get(role), cards)
        pin = pins.get(role)
        pin = pin if pin and pin != "auto" else None
        got = choose(role, need, cards, live, pin, order)
        if got.get("move"):
            plan.update(ok=False, move=got["move"], role=role, why="%s: %s" % (role, got["why"]),
                        notice=got.get("notice"), retry_s=got.get("retry_s") or 120,
                        propose=got.get("propose"), wanted=got.get("wanted"))
            plan["roles"][role] = {"model": None, "why": got["why"]}
            return plan
        entry = {"model": got["model"], "why": got["why"]}
        if got.get("degraded"):
            entry["degraded"] = got["degraded"]
            if got["degraded"] not in flags:
                flags.append(got["degraded"])
        if role == "fast" and not pin:
            # every live engine that can do a fast role's work shares it: the harness's
            # engine pool gives each minion the least loaded one, and fails over
            live_ids = [m["id"] for m in live.get("models") or [] if m.get("served", True) is not False]
            fits = [cards[i] for i in live_ids if i in cards and not hard_misses(cards[i], need)
                    and soft_misses(cards[i], need) == got.get("misses", [])
                    and wish_misses(cards[i], need) == got.get("wishes", [])]      # its equals: as quick as the pick
            fits.sort(key=lambda c: _rank(role, c, need, live), reverse=True)
            pool = [{"model": c["model"], "slots": int((slots or {}).get(c["model"]) or 4)} for c in fits]
            if len(pool) > 1:
                entry["pool"] = pool
        plan["roles"][role] = entry
    plan["degraded"] = "; ".join(flags) if flags else None
    return plan


def explain(plan):
    """The plan in a few lines, for Agents > Settings and bb."""
    lines = []
    for role in ROLES:
        r = (plan.get("roles") or {}).get(role)
        if r:
            lines.append("%s: %s (%s)" % (role, r.get("model") or "none", r.get("why") or ""))
    if not plan.get("ok"):
        lines.append("%s: %s" % (plan.get("move"), plan.get("why") or ""))
    return lines
