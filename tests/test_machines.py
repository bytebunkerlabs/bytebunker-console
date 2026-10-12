"""Machines: finding what your ssh reaches, what each one is, which recipes
fit it, and one click to serve one there (install, pair, serve, gateway)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import machines  # noqa: E402

CATALOG = {"fit": {"reserve_gb": {"dgx": 9.0, "linux": 0.5, "windows": 0.8, "mac": 0.5},
                   "overhead_gb": {"vllm": 1.5, "llamacpp": 0.5}, "weight_factor": {"vllm": 1.03, "llamacpp": 1.0},
                   "kv_guess": 0.1},
           "recipes": [{"name": "small", "variants": {"mac": {"engine": "llamacpp", "weights_gb": 5.0, "nodes": 1},
                                                      "linux": {"engine": "vllm", "weights_gb": 16.4, "nodes": 1}}},
                       {"name": "mid", "variants": {"mac": {"engine": "llamacpp", "weights_gb": 19.6, "nodes": 1}}},
                       {"name": "pair", "variants": {"dgx": {"engine": "vllm", "weights_gb": None, "nodes": 2}}},
                       {"name": "unknown", "variants": {"mac": {"engine": "llamacpp", "weights_gb": None, "nodes": 1}}}]}


class Describe(unittest.TestCase):
    def test_platforms(self):
        spark = machines.describe(machines.parse("os=Linux\narch=aarch64\nmem_kb=127600000\ngpu=NVIDIA GB10, [N/A], 580\n"
                                                 "os_name=Ubuntu\nos_version=24.04\ndisk_kb=1400000000\nhas_docker=1\n"))
        self.assertEqual((spark["platform"], spark["budget_mb"]), ("dgx", 127600000 // 1024))
        self.assertIn("DGX Spark", spark["description"])
        self.assertEqual(spark["has"], ["docker"])
        box = machines.describe(machines.parse("os=Linux\nmem_kb=32000000\ngpu=NVIDIA GeForce RTX 2070, 8192, 610\nwsl=1\n"))
        self.assertEqual((box["platform"], box["budget_mb"]), ("windows", 8192))
        mac = machines.describe(machines.parse("os=Darwin\narch=arm64\nmem_b=17179869184\nwired_mb=\n"))
        self.assertEqual((mac["platform"], mac["budget_mb"]), ("mac", 16384 * 2 // 3))
        self.assertEqual(machines.describe(machines.parse("os=Darwin\narch=arm64\nmem_b=17179869184\nwired_mb=14000\n"))["budget_mb"], 14000)
        self.assertIn("Intel", machines.describe(machines.parse("os=Darwin\narch=x86_64\n"))["why_not"])
        self.assertIn("no NVIDIA GPU", machines.describe(machines.parse("os=Linux\nmem_kb=1000\n"))["why_not"])
        self.assertIn("WSL2", machines.describe(machines.parse(""))["why_not"])


class Suggest(unittest.TestCase):
    def test_what_fits_first_the_largest_first(self):
        got = machines.suggest(CATALOG, "mac", 16000)
        self.assertEqual([(r["name"], r["fits"]) for r in got], [("small", True), ("unknown", None), ("mid", False)])
        got = machines.suggest(CATALOG, "mac", 96000)
        self.assertEqual([r["name"] for r in got if r["fits"]], ["mid", "small"])
        (p,) = machines.suggest(CATALOG, "dgx", 124000)
        self.assertEqual((p["fits"], p["note"]), (False, "needs 2 machines in one rack"))
        self.assertEqual(machines.suggest(CATALOG, "windows", 8192), [])

    def test_the_shipped_catalog_is_dgx_serves(self):
        cat = machines.load_catalog()
        self.assertTrue(cat["recipes"] and cat["fit"]["reserve_gb"]["dgx"], cat.get("dgx_serve"))
        self.assertIn("qwen3-8b", [r["name"] for r in cat["recipes"]])


class SshConfig(unittest.TestCase):
    def test_aliases_patterns_and_git_hosts(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "config")
            with open(p, "w") as f:
                f.write("Host burhan spark-1\n  HostName 100.1.1.1\n  User me\n\nHost *\n  ServerAliveInterval 30\n"
                        "Host github.com\n  User git\nHost box\nHost burhan spark-1 aleem\n  ControlMaster auto\n"
                        "Match host x\n  User y\n")
            got = machines._ssh_config(p)
            self.assertEqual([(h["target"], h["hostname"], h["aliases"]) for h in got],
                             [("burhan", "100.1.1.1", ["burhan", "spark-1"]), ("box", "box", ["box"]),
                              ("aleem", "aleem", ["aleem"])])
        finally:
            shutil.rmtree(d)


@unittest.skipIf(sys.platform == "win32", "the probe is POSIX shell")
class ProbeHere(unittest.TestCase):
    def test_this_computer_answers(self):
        m = machines.here(CATALOG)
        self.assertTrue(m["ok"], m)
        self.assertTrue(m["description"], m)
        self.assertIn(m["platform"], ("mac", "linux", "dgx", "windows", "unsupported"))
