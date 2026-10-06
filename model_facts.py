"""What the engines have told us about their models, kept across restarts.

The capability table says what a model can do; the engine may serve less
(measured: DeepSeek-V4 vision at 262k under a 1M manifest). Its overflow
error names the window it really serves, and that figure is believed from
then on, for every client, after a reload too (the Playground used to keep
it in the tab and forget it).

    facts = ModelFacts("data/model_facts.json")
    facts.learn_ctx("deepseek-v4", 262144)
    facts.ctx("deepseek-v4")            # 262144, or None when never learned
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

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.facts, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass
