"""The rack card on the Recipes screen, driving `rack` over ssh: dgx-serve
1.0's `rack recipes --json` (recipes with their model and files, yours
outside the checkout) and the table a rack from before 1.0 prints."""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_api import Server  # noqa: E402


def script(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


STATUS_1_0 = """serving
  glm53 (org/GLM-Big) with vllm under docker on spark-1 spark-2, since 2026-10-06T15:11:04+00:00

api
  serving: glm-big-served
"""


class RackCard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.rack = os.path.join(self.tmp, "checkout")
        self.mine = os.path.join(self.tmp, "config", "recipes")
        for d in (self.bin, self.rack, self.mine, os.path.join(self.rack, "recipes", "small")):
            os.makedirs(d, exist_ok=True)
        # ssh runs the remote command here; the rack is a stand-in
        script(os.path.join(self.bin, "ssh"), 'for a; do last=$a; done; exec /bin/sh -c "$last"')
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        self.srv = Server(data, {"rack": {"ssh": "spark", "dir": self.rack}},
                          env={"PATH": self.bin + os.pathsep + os.environ.get("PATH", "")})

    def test_a_dgx_serve_rack(self):
        flat = os.path.join(self.mine, "glm53.env")
        with open(flat, "w") as f:
            f.write("MODEL=org/GLM-Big\n")
        for fn, text in (("model.env", "MODEL=org/Small\n"), ("dgx.env", ". \"$RECIPE_DIR/model.env\"\n")):
            with open(os.path.join(self.rack, "recipes", "small", fn), "w") as f:
                f.write(text)
        listing = {"schema": 1, "here": "dgx", "recipes": [
            {"name": "glm53", "source": "mine", "layout": "flat", "location": flat, "platforms": ["dgx", "linux"],
             "model": "org/GLM-Big", "variants": {"dgx": {"file": flat, "tensor_parallel": 2},
                                                  "linux": {"file": flat, "tensor_parallel": 2}}},
            {"name": "small", "source": "repo", "layout": "v2", "location": os.path.join(self.rack, "recipes", "small"),
             "platforms": ["dgx", "mac"], "model": "org/Small",
             "variants": {"dgx": {"file": os.path.join(self.rack, "recipes", "small", "dgx.env"), "tensor_parallel": 1},
                          "mac": {"file": os.path.join(self.rack, "recipes", "small", "mac.env")}}}]}
        with open(os.path.join(self.tmp, "recipes.json"), "w") as f:
            json.dump(listing, f)
        with open(os.path.join(self.tmp, "status.txt"), "w") as f:
            f.write(STATUS_1_0)
        script(os.path.join(self.rack, "rack"), 'case "$*" in "recipes --json") cat %s;; status) cat %s;; '
               '*) echo "usage" >&2; exit 2;; esac' % (os.path.join(self.tmp, "recipes.json"),
                                                         os.path.join(self.tmp, "status.txt")))
        self.start()
        st, d = self.srv.request("GET", "/api/rack")
        self.assertEqual(st, 200, d)
        got = {r["name"]: (r["mode"], r["model"], r["platforms"]) for r in d["recipes"]}
        self.assertEqual(got, {"glm53": ("TP=2", "org/GLM-Big", ["dgx", "linux"]),
                               "small": ("solo", "org/Small", ["dgx", "mac"])})
        self.assertEqual(d["serving"], "glm53")                 # the recipe, not the engine's served name
        st, x = self.srv.request("POST", "/api/rack", {"action": "show", "recipe": "glm53"})
        self.assertTrue(x["ok"], x)
        self.assertEqual(x["file"], flat)                        # yours, outside the checkout
        self.assertIn("MODEL=org/GLM-Big", x["text"])
        st, x = self.srv.request("POST", "/api/rack", {"action": "show", "recipe": "small"})
        self.assertIn("MODEL=org/Small", x["text"])              # model.env, then the variant
        self.assertIn('. "$RECIPE_DIR/model.env"', x["text"])

    def test_a_rack_from_before_1_0(self):
        with open(os.path.join(self.rack, "recipes", "dsv4.env"), "w") as f:
            f.write("MODEL=org/Deep\n")
        script(os.path.join(self.rack, "rack"),
               'case "$1" in recipes) [ "$2" = --json ] && { echo "unknown option --json" >&2; exit 2; }; '
               'printf "recipes\\n  dsv4   TP=2   org/Deep\\n";; status) echo "serving: dsv4";; esac')
        self.start()
        st, d = self.srv.request("GET", "/api/rack")
        self.assertEqual([(r["name"], r["mode"], r["model"]) for r in d["recipes"]], [("dsv4", "TP=2", "org/Deep")])
        self.assertEqual(d["serving"], "dsv4")
        st, x = self.srv.request("POST", "/api/rack", {"action": "show", "recipe": "dsv4"})
        self.assertEqual((x["file"], x["text"].strip()), ("recipes/dsv4.env", "MODEL=org/Deep"))


if __name__ == "__main__":
    unittest.main()
