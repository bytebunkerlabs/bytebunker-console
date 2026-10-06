"""The Help center: every guide parses, links resolve, screens have guides,
and nothing points away from ByteBunker's own products."""
import os
import re
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELP = os.path.join(ROOT, "docs", "help")


def guides():
    out = {}
    for name in sorted(os.listdir(HELP)):
        if name.endswith(".md"):
            with open(os.path.join(HELP, name), encoding="utf-8") as f:
                text = f.read()
            meta = dict(l.split(":", 1) for l in text.split("---")[1].strip().splitlines())
            out[meta["id"].strip()] = {"meta": {k.strip(): v.strip() for k, v in meta.items()}, "text": text, "file": name}
    return out


class HelpTest(unittest.TestCase):
    def test_front_matter_and_links(self):
        g = guides()
        self.assertGreater(len(g), 10)
        with open(os.path.join(ROOT, "public", "console.js"), encoding="utf-8") as f:
            screens = re.search(r"const screens = \[([^\]]+)\]", f.read()).group(1)
        screens = set(re.findall(r'"([a-z]+)"', screens))
        for gid, info in g.items():
            self.assertTrue(info["meta"].get("title"), gid)
            for target in re.findall(r"\]\(help:([a-z0-9-]+)\)", info["text"]):
                self.assertIn(target, g, "%s links to a missing guide %s" % (info["file"], target))
            for target in re.findall(r"\]\(#([a-z]+)\)", info["text"]):
                self.assertIn(target, screens, "%s links to a missing screen %s" % (info["file"], target))
            if info["meta"].get("screen"):
                self.assertIn(info["meta"]["screen"], screens, info["file"])

    def test_every_screen_has_a_guide(self):
        g = guides()
        with_guides = {i["meta"].get("screen") for i in g.values()}
        for screen in ("playground", "sessions", "gateways", "models", "workflows", "jobs", "agents", "mcp",
                       "skills", "plugins", "cluster", "usage", "settings", "recipes"):
            self.assertIn(screen, with_guides, "no guide for the %s screen" % screen)

    def test_only_our_products_and_no_private_addresses(self):
        from urllib.parse import urlparse
        words = re.compile(r"litellm|ollama|lm studio|mo@bunker|leagueofash|hermes|spark-1|172\.16\.|100\.90\.", re.I)
        for gid, info in guides().items():
            body = info["text"]
            self.assertEqual(words.findall(body), [], info["file"])
            for url in re.findall(r"https?://[^\s)`\"']+", body):
                host = urlparse(url).hostname or ""
                ok = (host.startswith("192.0.2.") or host in ("127.0.0.1", "localhost")
                      or host == "bytebunkerlabs.ai" or host.endswith(".bytebunkerlabs.ai"))
                self.assertTrue(ok, "%s points away from ByteBunker: %s" % (info["file"], url))


if __name__ == "__main__":
    unittest.main()
