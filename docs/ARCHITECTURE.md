# ByteBunker — how the pieces fit, and the plan to make them one thing

## Today: four parts on four hosts

| Plane | What runs | Where (reference deployment) | Talks to |
|---|---|---|---|
| **Model plane** | vLLM (one tensor-parallel engine across two DGX Sparks) + litellm gateway + the rack monitor (read-only telemetry, :9177) | spark-1 / spark-2 | nothing outbound |
| **Control plane** | the console (`server.py`, stdlib Python + one JS file) | hermes (Mac mini), launchd | litellm, the worker over ssh, the rack monitor |
| **Agent plane** | the harness (`run_master.py`, Sultan + court), rootless podman, one container per agent | agents-worker (WSL2 Ubuntu inside a Windows box) | litellm through a unix-socket proxy; the internet only for roles that get `network=true` |
| **Fast-model plane** | a small model on the worker's own GPU (vLLM, RTX 2070) | same Windows box, WSL2 | registered in litellm, so it is just another model name |

The safety rule that shaped this: **agents never run where a model is served, and the console never runs agent code.** The console launches a goal over ssh and watches; the worker runs it in `--network none` containers; the model hosts only serve tokens.

### One request, end to end

1. You type a goal on the **Agents** screen. The console sshes to the worker and starts `run_master.py` with the goal, streaming stdout back as server-sent events.
2. The **Sultan** (master, big model) reads its standing orders, the skill catalog and the archetype roster, then spawns **minions** (quick doers, small model), a **wazir** (deep thinker, big model) when a question is contested, and finally **Malikah** (the queen) to judge its synthesis before the human sees it.
3. Every agent runs in its own container from the `bytebunker-slave` image, with a brief, optional material, its skills' instructions and a tool allowlist. It reaches the model only through the proxy socket mounted into the container.
4. Results flow back through the Sultan: unverified answers are labelled as such, skeptic panels refute what they can, and the final answer is delivered — as prose saved as `@draft` if it was ever too long for a tool call.
5. Everything is written down: `trajectories/spawns.jsonl`, per-goal `master.jsonl`, per-agent event streams, and the console's own trace log, which the **Usage** and **Agents** screens read and `/api/export` turns into training data.

### Where things are configured

- Console: `config.json` — upstream gateway + key, model capabilities, skills dirs, plugins, `agents` (ssh host, harness dir, master name and standing instructions, run timeout, worker model metrics), `litellm` (ssh host, config path, container) for recipe registration, `prometheus_url` and `nodes` for the Cluster screen.
- Harness: `config/config.yaml` on the worker — `llm` (gateway URL and key, `master_model`, `default_slave_model`, `thinking_model`, output caps, context budgets, thinking switches), `concurrency`, `sandbox.runner` (podman/docker/local), `allowed_roles` for network.
- Gateway: litellm's `model_list` — every engine is a name here, and the console's Recipes screen appends to it.

## The plan: one repo, one installer, one CLI

The pieces are disjoint because they were built in the order the problems appeared. They share one vocabulary already (skills in the same SKILL.md format, the same model names through litellm, the same trace shapes). Merging is mostly moving files and one configuration.

### Target layout

```
bytebunker/
  bytebunker/            one Python package
    console/             server.py, traces.py, skills.py, mcp.py, recipes.py, public/
    harness/             src/bytebunker_agents/* (master, slaves, tools, graph, harness)
    cli.py               `bytebunker console | worker | run | deploy | export`
  skills/                the ONE skills dir (console and harness read the same files)
  plugins/
  recipes/               deployment recipes as data (today: recipes.py)
  docker/                Dockerfile.slave, entrypoint
  install.sh             one-command install (console, optional worker, service)
  config.example.yaml    one config file with sections console:, harness:, gateway:
  docs/
```

### Phases

1. **Installer first (done in this repo):** `install.sh` installs the console as a service on macOS or Linux, writes `config.json`, and optionally clones the harness next to it and wires `agents` in local mode. This is what "anyone can install" needs before any code moves.
2. **Import the harness:** move `bytebunker-harness/src/bytebunker_agents` under `bytebunker/harness`, keep its tests, keep `scripts/run_master.py` as a thin entry. The worker install becomes `bytebunker worker --init` (creates the venv with uv, builds the slave image, enables linger).
3. **One skills dir:** delete the console's copy-of-harness-skills; both read `skills/`. The console's "New skill" form writes there and mirrors to remote workers as it does today.
4. **One config:** `config.yaml` with `console:`, `harness:`, `gateway:` sections; `server.py` and `config.py` read their section. Keep `config.json` readable for one release.
5. **CLI:** `bytebunker console` (serve), `bytebunker worker` (run the agent plane on this host), `bytebunker run "<goal>"` (headless goal from a terminal), `bytebunker deploy vllm-cuda --host ...` (recipes without the UI), `bytebunker export` (training data).
6. **Recipes as data:** the three recipes become YAML files under `recipes/` with the same fields (`params`, `script`, `litellm_entry`), so users add their own without touching Python.

### Decisions the merge needs from the owner

- **Licence and visibility.** The console is public; the harness is private and has no LICENSE file. A merged repo is one or the other. "Anyone can install" means public with a licence (MIT or Apache-2.0 fit the stdlib-only, no-warranty spirit).
- **Name of the package** on PyPI, if any: `bytebunker` is the obvious one.
