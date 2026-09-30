# ByteBunker operations handbook

Everything that runs, where it runs, how it was deployed, how to change it, and how to
debug it. The command-by-command reproduction of every host is `RUNBOOK.md`. Written for the operator, from the live deployment as of 30 September 2026.

---

## 1. Inventory

| Host | What it is | Reach it | Runs |
|---|---|---|---|
| **hermes** (`mos-mac-mini`) | Control plane, Mac mini, user `mo` | `ssh hermes` from the laptop; LAN + tailnet 100.112.146.107 | console (launchd), ssh tunnel service (launchd) |
| **spark-1** (`burhan`) | Model plane head, DGX Spark, user `trickyfalcon` | `ssh spark-1` (laptop and hermes → `trickyfalcon@172.16.25.186`); tailnet 100.90.164.11; direct link 192.168.100.1 | vLLM head (`serve_node`, docker), litellm gateway (`dgx-inference-litellm-1`, :4000), Prometheus (:9090), Grafana, sparkDash (:5555, docker) |
| **spark-2** (`aleem`) | Model plane worker, DGX Spark | `ssh spark-2` from spark-1; LAN 172.16.25.185; direct link 192.168.100.2 | vLLM tensor-parallel worker (`serve_node`), node + GPU exporters |
| **leagueofash** | Windows Server box with an RTX 2070 (8 GB); LAN 172.16.25.83; tailnet 100.95.150.96 | RDP / local | Ollama (native, unused by the stack), Windows port forwards, the WSL2 VM below |
| **agents-worker** | Ubuntu WSL2 inside leagueofash, user `trickyfalcon`; its own tailnet node 100.100.129.98; NAT address 172.22.36.141 (changes on reboot) | `ssh agents-worker` from hermes and the laptop | agent harness, rootless podman, vLLM for the small model (`vllm-qwen3-4b-fast.service`, :8001) |

Addresses the console cares about: litellm `http://172.16.25.186:4000/v1` (LAN), Prometheus through the hermes tunnel `127.0.0.1:19090`, sparkDash through the hermes tunnel `127.0.0.1:15555` and on the tailnet `https://burhan.tailed338.ts.net`.

The rule behind the layout: **agents never run on a model host or on the control host.** The Sparks only serve; hermes only launches and watches; the worker runs agents in containers with no network.

---

## 2. Where the code is

| Repo | Visibility | Laptop checkout | Deployed copy |
|---|---|---|---|
| `bytebunkerlabs/bytebunker-console` | public | `~/Documents/AI/bytebunker-console` | hermes `~/bytebunker-console` — **a file copy, not a git checkout** |
| `bytebunkerlabs/bytebunker-harness` | private, no LICENSE | `~/Documents/AI/bytebunker-harness` | agents-worker `~/bytebunker-harness` — a git clone whose deploy key needs a passphrase, so code is pushed with tar (see §6) |
| `bytebunkerlabs/dgx-spark-serve` | | | spark-1 `~/dgx/dgx-spark-serve` (`rack` CLI, recipes for the Sparks) |
| `bytebunkerlabs/dgx-spark-setup` | | | spark-1 `~/dgx/dgx-spark-setup` (host prep; litellm and Prometheus configs live here) |
| `MiaAI-Lab/sparkDash` | MIT | | spark-1 `~/dgx/sparkDash`, with one local commit (ByteBunker themes) |

Console layout: `server.py` (HTTP server, all API routes), `agents.py` (harness launch, live views, worker stats, agent usage), `recipes.py` (model deployment recipes), `skills.py` (skills + plugins), `traces.py` (trace log + training export), `mcp.py` / `mcp_terminal.py` (MCP host and the terminal tool), `public/` (one HTML, one JS, one CSS), `skills/` (built-in skills), `plugins/`, `docs/`, `install.sh`.

Harness layout (`src/bytebunker_agents/`): `master/` (the Sultan: `agent.py` loop and tools, `prompts.py` doctrine), `slaves/` (`runner.py` the in-container loop, `archetypes.py` roles, `factory.py` spec building, `prompts.py`), `tools/restricted.py` (the tool registry: files, shell, `fetch_url`, `cve_record`, `kev_lookup`), `harness/` (`spawn.py` podman/docker/local runner + the unix-socket LLM proxy, `cost.py`), `graph/verifier.py` (skeptic panels), `trajectories/` (tracer + spawn ledger), `llm.py` (HTTP client with keepalive and deadline), `config.py`; plus `scripts/run_master.py`, `docker/Dockerfile.slave`, `docker/entrypoint.sh`, `skills/`, `config/config.yaml`.

---

## 3. Services, and how to drive them

### hermes
- **Console**: launchd `ai.bytebunker.console` → `/usr/bin/python3 ~/bytebunker-console/server.py`, port 8765, log `~/bytebunker-console/data/console.log`.
  `launchctl kickstart -k gui/501/ai.bytebunker.console` restarts it. **Never restart while an agent run is streaming**: the run's ssh session dies with the server.
  `public/*.js|html|css` are read from disk on every request: copy and reload the browser, no restart. `server.py`, `agents.py`, `recipes.py`, `skills.py`, `config.json` need a restart.
- **Tunnels**: launchd `ai.bytebunker.tunnel` → `ssh -N … -L 127.0.0.1:19090:127.0.0.1:9090 -L 127.0.0.1:18091:127.0.0.1:8091 -L 127.0.0.1:15555:127.0.0.1:5555 trickyfalcon@172.16.25.186`. Edit the plist, then `launchctl bootout gui/501/ai.bytebunker.tunnel && launchctl bootstrap gui/501 ~/Library/LaunchAgents/ai.bytebunker.tunnel.plist` (kickstart alone does not re-read ProgramArguments).
- Data: `data/config.json` is **not** the config; the live config is `~/bytebunker-console/config.json`. `data/traces/<day>.jsonl` (every chat, tool call, rating, agent run, recipe action), `data/usage.jsonl` (chat token ledger), `data/sessions/`.

### spark-1 / spark-2 (model plane)
- **Engine**: `rack up <recipe>` / `rack down` / `rack status` / `rack logs` from `~/dgx/dgx-spark-serve` (also on PATH as `rack`). The recipe `recipes/dsv4-vision-ab.env` is what serves today: TP=2, `--max-model-len 262144`, `--kv-cache-dtype fp8`, tool + reasoning parsers, and `--default-chat-template-kwargs '{"thinking":true,"reasoning_effort":"max"}'` — **thinking is on by default at the engine**, clients must send `chat_template_kwargs.thinking=false` to turn it off. `rack up` also registers the recipe's `GATEWAY_NAME` with litellm.
- **litellm**: container `dgx-inference-litellm-1`, config bind-mounted from `~/dgx/dgx-spark-setup/config/litellm.config.yaml`. Add a `model_list` entry, then `docker restart dgx-inference-litellm-1`. Every engine on the rack is a name here: `deepseek-v4-vision-uncensored` (the Sparks), `qwen3-4b-fast` (the 2070).
- **Prometheus/Grafana**: `~/dgx/dgx-spark-setup` compose. Spark-2's GPU exporter loses NVML after a while: `docker restart monitoring-nvidia-gpu-exporter-1` on spark-2.
- **sparkDash**: `~/dgx/sparkDash`, `docker compose up --build -d` (container `sparkDash`, host network, loopback :5555). Units live in `config/sparks.json`; secrets in `config/`. Exposed on the tailnet with `tailscale serve --bg --https=443 5555` (operator mode was set with `sudo tailscale set --operator=$USER`; MagicDNS and HTTPS certs are enabled on the tailnet).

### agents-worker (agent plane)
- **Harness**: `~/bytebunker-harness`, run by the console as `uv run scripts/run_master.py --goal …` (uv at `/home/trickyfalcon/.local/bin/uv`). `config/config.yaml` holds the models and limits (§4).
- **Slave image**: `podman build -f docker/Dockerfile.slave -t bytebunker-slave:latest .` — required after any change under `src/` or `docker/`, because slaves run from the image, not from the checkout.
- **Small model**: systemd user unit `vllm-qwen3-4b-fast.service` (written by the console's recipe), script `~/vllm-qwen3-4b-fast.sh`, log `~/vllm-qwen3-4b-fast.log`, venv `~/vllm-env` (Python 3.12, vLLM 0.30). `systemctl --user status|restart vllm-qwen3-4b-fast`. Listens on 0.0.0.0:8001 inside WSL2.
- Requirements already met: `loginctl enable-linger trickyfalcon` (rootless podman over ssh needs it), `build-essential` (vLLM's Triton kernels), line 1 of `~/.bashrc` exporting `/usr/lib/wsl/lib` and `~/.local/bin` so ssh command sessions find `nvidia-smi` and `uv`.

### leagueofash (Windows side)
Everything the console needs from Windows is one-time and already done; redo after a Windows reinstall:
1. **NVIDIA driver with WSL support** (610.62 today). It makes the GPU visible inside WSL2 as `/dev/dxg` with libraries in `/usr/lib/wsl/lib`.
2. **WSL2 Ubuntu** with `systemd=true` in `/etc/wsl.conf`, sshd, Tailscale installed inside the VM (that is why `agents-worker` is its own tailnet node).
3. **Port forward + firewall** for the small model, in an admin PowerShell (the WSL address changes on reboot; re-run with the new one from `ip -4 addr show eth0` inside WSL):
   ```
   netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8001 connectaddress=<wsl-ip> connectport=8001
   netsh advfirewall firewall add rule name="vllm-8001" dir=in action=allow protocol=TCP localport=8001
   ```
   litellm reaches the model at `http://172.16.25.83:8001/v1` through this forward. There is no forward for ssh: the console and sparkDash reach the VM over its tailnet address.
4. Ollama on Windows is installed but not part of the stack; it can be registered in litellm with the Ollama recipe if wanted.

---

## 4. Configuration, all of it

**Console `config.json` (hermes)**
- `upstream_url` / `upstream_key`: litellm. `model_capabilities`: per-model context and thinking switches (`deepseek-v4-vision` 262144 with `ctk thinking:true`; `qwen3-4b-fast` 24576 with `enable_thinking:false`).
- `skills_dirs: ["~/bytebunker-console/skills-harness"]` — a copy of the harness `skills/`. `plugins` state, `plugins_dirs`.
- `agents`: `enabled`, `ssh agents-worker`, `dir ~/bytebunker-harness`, `python "/home/trickyfalcon/.local/bin/uv run"`, `script scripts/run_master.py`, `master_name Sultan`, `master_instructions` (the court doctrine; editable on the Agents screen), `run_timeout_s 10800`, `worker_model_metrics http://127.0.0.1:8001/metrics`, `worker_model_label`.
- `litellm`: `ssh spark-1`, `config_path`, `container` — for the Recipes screen's Register button.
- `rack`: `ssh spark-1`, `dir ~/dgx/dgx-spark-serve` — for the Recipes screen's Spark card.
- `prometheus_url http://127.0.0.1:19090`, `nodes` (spec strings), `sparkdash_url http://127.0.0.1:15555`, `telemetry_source sparkdash`, `sparkdash_open_url https://burhan.tailed338.ts.net`.

**Harness `config/config.yaml` (worker)**
- `llm`: `base_url http://172.16.25.186:4000/v1` (LAN, never the tailnet), `api_key` (the litellm key), `master_model deepseek-v4-vision-uncensored`, `default_slave_model qwen3-4b-fast`, `thinking_model deepseek-v4-vision-uncensored`, `timeout_s 900`, `master_max_tokens 8000`, `slave_max_tokens 3000`, `slave_transcript_chars 36000`, `fetch_max_chars 8000`; defaults in code: `thinking_extra` (on, max effort) and `thinking_off_extra`.
- `concurrency`: `max_concurrent_slaves 3`, `max_slaves_per_goal 16`, `default_slave_timeout_s 1800`.
- `sandbox.runner podman`, `llm_bridge uds`, `allowed_roles` for network (internet_agent, researcher, minion, wazir, malikah).

**litellm `model_list`**: one entry per engine; `rack up` writes the Spark one, the console's Register writes others.

---

## 5. How a goal runs (the thing to picture when debugging)

1. Browser → `POST /api/agents {goal}` → `server.py` builds the ssh command from `agents.py:build_command`: `ssh agents-worker 'cd ~/bytebunker-harness && exec 3<&0; setsid env PYTHONUNBUFFERED=1 BYTEBUNKER_MASTER_NAME=… uv run scripts/run_master.py --goal … & … sidecar …'`. The sidecar `cat <&3` waits on the console's stdin: when the console closes it (Stop, client gone, timeout) it kills the process group and every container labelled `bytebunker.pgid=<pgid>`. Stdout streams back as SSE with a 5 s keepalive.
2. `run_master.py` loads `config.yaml`, the skill catalog (`skills/*/SKILL.md`), the archetype roster, and the master's standing orders (env `BYTEBUNKER_MASTER_INSTRUCTIONS`), then runs the Sultan's loop: LLM round → tool calls (`spawn_slave`, `wait_for_slaves`, `get_result`, `verify_result`, `steer_slave`, `kill_slave`, `final_answer`). Every round is traced to `trajectories/<goal>/master.jsonl` (`llm`, `tool`, `spawn`, `spawn_result`, `panel`, `reply_cut`, `final_answer`, `bail`).
3. `spawn_slave` → `harness/spawn.py`: writes `/tmp/bb-<id>-*/input.json` (spec, skills, llm settings incl. model, max_tokens, extra thinking switch, context budgets), starts the unix-socket proxy, runs `podman run --network none|bridge --userns=keep-id --label bytebunker.pgid=… -v <task>:/task -v <sock>:/task/llm.sock bytebunker-slave:latest`. The container runs `docker/entrypoint.sh` → `slaves/runner.py`, which loops gather→act→verify, checkpoints `result.json` each step, and appends `events.jsonl`.
4. `_collect` reads `result.json`, records the spawn in `trajectories/spawns.jsonl` (role, depth, tokens with prompt/completion split, model, success, error, final_result), and the Sultan sees `ok` / `ANSWERED but UNVERIFIED` / `failed`.
5. The console reads all of this over ssh: `/api/agents/slaves` (spawn ledger tail + master.jsonl tail + live `events.jsonl` of running containers), `/api/agents/slave?id=` (one agent's record and timeline), `/api/agents/stats` (containers, masters, 24 h outcomes, the worker's own model metrics), `/api/usage` (agent tokens by day, role, model).

---

## 6. Deploy procedures

**Console** (from the laptop checkout):
```
scp server.py agents.py recipes.py skills.py hermes:bytebunker-console/
scp public/index.html public/console.js public/console.css hermes:bytebunker-console/public/
ssh hermes 'ssh agents-worker "pgrep -fc \"python scripts/run_maste[r].py\""'   # must be 0 before a restart
ssh hermes 'launchctl kickstart -k gui/501/ai.bytebunker.console'
```
Skills shipped with the harness must also land in hermes `~/bytebunker-console/skills-harness/` (the Skills screen reads that copy). Skills created in the UI are written there and mirrored to the worker automatically.

**Harness** (from the laptop checkout; `git pull` on the worker fails non-interactively):
```
COPYFILE_DISABLE=1 tar c --no-xattrs src/... skills/... | ssh hermes 'ssh agents-worker "cd ~/bytebunker-harness && tar x; find . -name \"._*\" -delete"'
ssh hermes 'ssh agents-worker "cd ~/bytebunker-harness && podman build -q -f docker/Dockerfile.slave -t bytebunker-slave:latest ."'   # after src/ or docker/ changes
```
Host-side files (`master/*`, `harness/spawn.py`, `config.py`, `trajectories/*`, `graph/verifier.py`) take effect on the next run without a rebuild; anything the runner imports needs the rebuild.

**Small model**: Recipes screen → vLLM on a CUDA GPU → Deploy (idempotent: it exits early if the served name already answers on the port). To redeploy, first `systemctl --user disable --now vllm-qwen3-4b-fast` on the worker.

**Sparks**: Recipes screen → rack card, or `rack up <recipe>` on spark-1.

**sparkDash**: edit under `~/dgx/sparkDash`, `docker compose up --build -d`. Keep the local commit `theme: bytebunker light/dark palettes…` when pulling upstream (rebase onto it), or the embedded frame loses the palette.

**Test suite** for the harness: `uv run pytest -q` in the harness checkout (the tests use a scripted LLM transport; no engine needed).

---

## 7. Debugging playbook

| Symptom | Where to look | Usual cause / fix |
|---|---|---|
| Agent run "starts" then nothing | Agents screen Sultan card; `trajectories/<goal>/master.jsonl` on the worker (`llm` events show each round's seconds and tokens) | The Sultan is writing prose; the engine is busy (check sparkDash TTFT p95). Not a hang unless a round exceeds `llm.timeout_s`. |
| Run ends `killed: timeout` | `agents.run_timeout_s` in the console config (minutes on the Agents form) | Raise it; multi-step goals are 60–90 min on this engine. |
| Agent "failed: out of time before verification" | Slave detail: `verify_skipped_out_of_time` | Not a failure: the answer is there, unverified. The Sultan should verify with a panel; if it re-spawns the same brief, check the doctrine text. |
| Wrong facts from a minion | Slave timeline: which tools ran | For CVEs the `cve-lookup` skill must be attached (`cve_record`, `kev_lookup`). Bot-gated pages fetched via `fetch_url` mislead small models. |
| Stop button leaves containers | `podman ps` on the worker; labels `bytebunker.pgid` | `pkill -KILL -f "run_maste[r].py"; podman ps -q \| xargs -r podman kill`. Note the `[r]` trick: `pkill -f run_master.py` self-matches the ssh shell. |
| Small model down | Cluster screen worker card; `systemctl --user status vllm-qwen3-4b-fast`; `~/vllm-qwen3-4b-fast.log` | "Failed to find C compiler" → `build-essential`; port in use → another unit still active; after a Windows reboot the port forward points at a stale WSL IP → re-run the netsh line with the new address. |
| litellm 404 for a model | `docker logs dgx-inference-litellm-1`; `curl 172.16.25.186:4000/v1/models` | Entry missing or api_base unreachable from the Spark (test `curl http://172.16.25.83:8001/v1/models` from spark-1). |
| Cluster cards empty | `/api/telemetry` on hermes; `curl 127.0.0.1:15555/api/sparks` | Tunnel down (`launchctl print gui/501/ai.bytebunker.tunnel`), or sparkDash stopped (`docker ps` on spark-1). |
| sparkDash shows the worker without a GPU | ssh from spark-1 to `trickyfalcon@100.100.129.98 nvidia-smi` | `nvidia-smi` not on PATH for ssh sessions (the `.bashrc` line), or spark-1's key missing from the worker's `authorized_keys`. |
| Spark-2 GPU metrics flat | Prometheus target `spark-2-gpu` | `docker restart monitoring-nvidia-gpu-exporter-1` on spark-2 (NVML loss in a long-lived container). |
| Everything is slow | sparkDash LLM panel for the head: tokens/s, KV %, queue, TTFT p95, prefix hit | One engine shared by all agents (~27 tok/s total). Hidden reasoning must be off for doers (it is, via `thinking_off_extra`); move doers to the small model (`default_slave_model`). |

Useful one-liners on the worker: `ls -td trajectories/goal-* | head -1` (latest goal), `tail -f /tmp/bb-<agent>-*/events.jsonl` (a live agent), `podman ps --format '{{.Names}} {{.Status}}'`.

---

## 8. Extending it

- **A new skill**: Skills screen → New skill (or a `skills/<name>/SKILL.md` in the harness repo, then copy to both places). The Sultan sees the catalog's `description`/`whenToUse`; the body is injected into the agent's system prompt.
- **A new tool for agents**: `tools/restricted.py` → define a function returning `ToolResult`, `reg.register(ToolDef(name, description, json-schema, fn))`, add the name to the right list in `slaves/archetypes.py` (`READ_ONLY_TOOLS`, `READ_WRITE_TOOLS`, `NETWORK_TOOLS`), rebuild the image. `cve_record` / `kev_lookup` are the template for "deterministic facts a small model must not guess".
- **A new archetype**: `slaves/archetypes.py` → `Archetype(name, tools, max_attempts, depth, thinking, max_tokens, timeout_s, description)`; mention it in the doctrine if the Sultan should use it; add to `allowed_roles` if it needs the network.
- **A new engine**: Recipes screen (any CUDA host or Ollama on Windows) or a `rack` recipe for the Sparks; then it is a name in litellm and can be set as `default_slave_model`, `thinking_model` or `master_model`.
- **A new telemetry source**: `server.py` `telemetry()` returns a list of node dicts (`name, util, mem_used_gb, mem_total_gb, temp, power, cpu, uptime_s`, optional `llm`, `kind`, `role`); add a function like `sparkdash_telemetry()` and a `telemetry_source` value.
- **A new console screen**: a `<section class="screen" id="screen-x">` + a nav button `data-nav="x"` in `index.html`, the name in the `screens` array and a `render…()` call in `go()` in `console.js`.
- **Training data**: `/api/export` (chat traces, optionally `?rated=up`) from the console; `trajectories/spawns.jsonl` and each goal's `master.jsonl` from the worker.

---

## 9. Known sharp edges

- Paths with `~` must not be shell-quoted when sent over ssh (`"$HOME"/…` instead); a quoted tilde creates a literal `~` directory.
- `pkill -f "vllm serve"` and `pkill -f run_master.py` match the ssh shell that runs them; use `serv[e]` / `maste[r]`.
- The engine reasons by default (recipe flag); every non-thinking role must send `thinking:false`, and the console's caps table does the same for chat.
- The WSL2 NAT address changes on reboot; the Windows port forward for 8001 must follow it.
- `launchctl kickstart` does not reload a changed plist; bootout + bootstrap does.
- The console's Skills copy and the harness `skills/` are two directories; the UI keeps them in sync, manual edits must be copied to both.
- The harness is private and unlicensed; the merge into one installable package waits on that decision.
