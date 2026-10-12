"""Make recipes_catalog.json from a dgx-serve checkout: its curated recipes
(the repo's, not a site's own) with each platform's engine, size and nodes,
and dgx-serve's fit arithmetic, so the app can suggest recipes for a machine
before dgx-serve is on it.

    python3 scripts/recipes_catalog.py ~/bb/dgx-serve
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(src):
    src = os.path.abspath(os.path.expanduser(src))
    sys.path.insert(0, os.path.join(src, "py"))
    import rackfit  # noqa: E402
    env = dict(os.environ, DGX_SERVE_CONFIG=os.path.join(src, ".no-site-config"), NO_COLOR="1")
    out = subprocess.run([os.path.join(src, "rack"), "recipes", "--json"], capture_output=True, text=True, env=env,
                         check=True).stdout
    d = json.loads(out)
    commit = subprocess.run(["git", "-C", src, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    recipes = []
    for r in d["recipes"]:
        if r.get("source") != "repo":
            continue
        variants = {}
        for p, v in (r.get("variants") or {}).items():
            variants[p] = {k: v.get(k) for k in ("engine", "weights_gb", "nodes", "tensor_parallel", "context")}
        recipes.append({"name": r["name"], "model": r.get("model") or "", "roles": r.get("roles") or [],
                        "platforms": r.get("platforms") or [], "variants": variants})
    cat = {"schema": 1, "dgx_serve": commit,
           "fit": {"reserve_gb": rackfit.RESERVE_GB, "overhead_gb": rackfit.OVERHEAD_GB,
                   "weight_factor": rackfit.WEIGHT_FACTOR, "kv_guess": rackfit.KV_GUESS},
           "recipes": sorted(recipes, key=lambda x: x["name"])}
    with open(os.path.join(ROOT, "recipes_catalog.json"), "w") as f:
        json.dump(cat, f, indent=1)
        f.write("\n")
    print("%d recipes from dgx-serve %s" % (len(recipes), commit))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "~/bb/dgx-serve")
