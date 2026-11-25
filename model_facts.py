"""What the engines have told us about their models, kept across restarts.

The capability table says what a model can do; the engine may serve less
(measured: DeepSeek-V4 vision at 262k under a 1M manifest). Its overflow
error names the window it really serves, and that figure is believed from
then on, for every client, after a reload too (the Playground used to keep
it in the tab and forget it).

    facts = ModelFacts("data/model_facts.json")
    facts.learn_ctx("deepseek-v4", 262144)
    facts.ctx("deepseek-v4")            # 262144, or None when never learned

It also keeps each model's capability card for the router (router.py): the
speed measured on real turns, how often its requests fail, when it was last
live, and whatever a person set by hand (tier, tools, vision, thinking).
"""
import json
import os
import threading
import time


class ModelFacts:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.facts = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.facts = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def ctx(self, model):
        v = (self.facts.get(model) or {}).get("ctx")
        return v if isinstance(v, int) and v > 0 else None

    def learn_ctx(self, model, window):
        if not model or not isinstance(window, int) or window <= 0:
            return
        with self._lock:
            if self.ctx(model) == window:
                return
            self.facts.setdefault(model, {}).update(ctx=window, learned=round(time.time(), 3))
            self._save()

    def caps(self, model, caps):
        """caps with what the engine said it serves laid over the table's."""
        learned = self.ctx(model)
        return dict(caps, ctx=learned) if learned else caps

    # ---- capability cards (router.py): what was measured, and what a person set ----
    CARD_KEYS = ("tier", "tools", "vision", "thinking", "speed", "uncensored")

    def card(self, model):
        """What is known about this model beyond the capability table: measured
        speed and failures, when it was last live, and anything set by hand."""
        base = (model or "").rsplit("@", 1)[0]
        c = (self.facts.get(model) or self.facts.get(base) or {}).get("card")
        return dict(c) if isinstance(c, dict) else {}

    def set_card(self, model, fields):
        """A person's word on a model (the Agents screen): it wins over every guess. A
        field set to None goes back to being guessed."""
        if not model or not isinstance(fields, dict):
            return {}
        with self._lock:
            c = self.facts.setdefault(model, {}).setdefault("card", {})
            for k in self.CARD_KEYS:
                if k not in fields:
                    continue
                v = fields[k]
                if v in (None, ""):
                    c.pop(k, None)
                elif k in ("tier", "speed"):
                    c[k] = str(v)
                else:
                    c[k] = bool(v)
            self._save()
            return dict(c)

    def learn_speed(self, model, tok_s):
        """A measured decode speed, folded into a running average."""
        if not model or not isinstance(tok_s, (int, float)) or tok_s <= 0 or tok_s > 5000:
            return
        with self._lock:
            c = self.facts.setdefault(model, {}).setdefault("card", {})
            old = c.get("tok_s")
            c["tok_s"] = round(float(tok_s) if not isinstance(old, (int, float)) else old * 0.8 + float(tok_s) * 0.2, 2)
            c["samples"] = int(c.get("samples") or 0) + 1
            if c["samples"] <= 5 or c["samples"] % 10 == 0:       # the first few at once, then now and then
                self._save()

    def learn_outcome(self, model, ok):
        """Whether a request to this model worked: a failure rate over its recent requests."""
        if not model:
            return
        with self._lock:
            c = (self.facts.get(model) or {}).get("card")
            if not c:
                return          # a name nothing here ever served (a typo, another gateway's model): no card is made of it
            old = c.get("fail_rate")
            x = 0.0 if ok else 1.0
            new = round(x if not isinstance(old, (int, float)) else old * 0.9 + x * 0.1, 4)
            changed = (new >= 0.01) != ((old or 0) >= 0.01) or abs(new - (old or 0)) >= 0.05
            c["fail_rate"] = new
            if changed:
                self._save()

    def seen_live(self, models, now=None):
        """These models are live now. Kept so that, when one is gone, the router can say which was down."""
        now = now if now is not None else time.time()
        with self._lock:
            dirty = False
            for m in models or []:
                if not m:
                    continue
                c = self.facts.setdefault(m, {}).setdefault("card", {})
                if now - float(c.get("last_live") or 0) >= 600:
                    c["last_live"] = round(now, 1)
                    dirty = True
            if dirty:
                self._save()

    def note_serving(self, recipe, names, rack=None, now=None):
        """What a rack serves now: remembered, so that when it stops the router knows which recipe brings it back."""
        if not recipe:
            return
        now = now if now is not None else time.time()
        with self._lock:
            cur = self.facts.get("@serving") or {}
            rec = {"recipe": str(recipe), "names": sorted(set(n for n in names or [] if n)), "rack": rack}
            if {k: cur.get(k) for k in rec} == rec and now - float(cur.get("at") or 0) < 600:
                return
            self.facts["@serving"] = dict(rec, at=round(now, 1))
            self._save()

    def last_serving(self):
        d = self.facts.get("@serving")
        return dict(d) if isinstance(d, dict) and d.get("recipe") else {}

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.facts, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass
