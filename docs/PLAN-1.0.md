# ByteBunker 1.0: the first product, end to end

## Status (updated as work lands)

Started 2026-10-05 on the owner's go. Work happens in phase order; dgx-serve (phase 3) and the harness fixes run alongside the console's phases 0 to 2.

| Phase | State |
|---|---|
| Do first | transcript removed from dgx-spark-serve's tree (history purge awaits the owner); work in progress saved on spark-1 branch `wip/2026-10-05` (local only); dgx-spark-setup no longer resets a live firewall; the engine key waits for the detached launcher (it needs an engine restart) |
| 0 | done: a scriptable fake engine and fake MCP server; CI on Linux (Python 3.9 and 3.12), macOS and Windows |
| 1 | done (console 697eabc..bf54222): event bus and `GET /api/events`; server-owned runs (agent goals, rack deploys, recipe deploys, MCP installs, jobs) that outlive their tab, re-attach and cancel; one file per session (hermes's 48 MB `sessions.json` migrated losslessly to 2.45 MB); `upstream.py`; one server per data folder; MCP replies routed by id; a versioned config that keeps outside edits. Glitches fixed with tests: every row of section 4.8 that the runner does not replace, except the agents polling (phase 5, worker monitor). Found and fixed on the way: the `hidden` attribute never hid anything with a display rule (Video studio, the first-run card, Stop); a stream cut mid-reply rendered as complete. Browser journeys (Playwright, not yet in CI) cover Jobs, Agents, Sessions and the Playground |
| 2 | next: the runner, profiles, workflows, the command table, `bb` |
| 3 | dgx-serve `dev/1.0` (local commits, not pushed): platform detection, inventory (`rack init`, `rack nodes`), platform flags with `--plan` and `--on`; recipes v2 in progress |
| Harness | pushed to the harness's main (d33a12e..1696a0e, 179 tests): goals never parsed as commands, `--goal-id`, an exit code per outcome, protocol 2 with `events.jsonl`, `--spec` per role, skill bodies to slaves, platform-aware preflight, the iMessage handle removed. Not deployed to the worker |
| 4 to 6 | not started |

Decisions taken as recommended (the owner said "build it"): 5 (llama.cpp on Macs), 8 (no LiteLLM by default), 9 (private recipes to an overlay), 10 (tools catalog), 11 (no Linux desktop app), 12 (Video out of 1.0), 13 (support matrix), 14 (an engine on the agent worker allowed, labelled), 15 (a folder per recipe).

Waiting for the owner: 1 (purge the transcript from history: a force-push), 2 (renaming the repo), 3 (licenses), 4 (the harness EULA), 6 (signing accounts), 7 (docs and get domains), and a window to restart the live model with a key.


## Context

ByteBunker Console 0.2.0 works on the author's rack, but it grew in the order problems appeared. To ship as ByteBunker Labs' first product it must work for a stranger on their own hardware, with no glitches. The owner asked for:

1. Every deployment shape thought through (one machine, several machines) and how monitoring is added to each.
2. Help pages and step-by-step instructions inside the app.
3. Every pointer leading only to ByteBunker products. Deployment and recipes go through dgx-serve only, and dgx-serve learns to deploy on any NVIDIA GPU machine and on Macs.
4. Everything else that must be streamlined.
5. A CLI with full parity with the UI (chat, models, thinking effort, tools, agents, workflows, jobs), configured from the UI, with everything it does captured and visible in the UI.

This plan covers all five. It comes from a read-only survey of the four repos, measurements on the rack, and an independent design review. Nothing is built until the owner approves; the owner said work starts later.

## Summary

1.0 is two products and an add-on: **the app** (desktop, headless server, and the `bytebunker` CLI, which is a second face of the same server) and **dgx-serve** (deploys and monitors models on a DGX Spark, an NVIDIA Linux box, a Windows PC via WSL2, or an Apple Silicon Mac), with **ByteBunker Agents** installed from the app onto a separate worker. Six moves get there:

1. **Make serving durable.** Today a model lives only as long as the shell that started it, and nothing comes back after a reboot.
2. **Make runs outlive their client.** Every chat, agent run and deploy runs inside one HTTP connection today. Runs become server-owned, with an event stream any client can attach to.
3. **One engine for UI and CLI.** Chat moves into the server, so both share one engine, one log, one history.
4. **dgx-serve on any machine**: `rack pull` and `rack up` with `--dgx`, `--linux`, `--windows` or `--mac`, paired with the app in one step.
5. **An app that explains itself**: Add a rack, Deploy, Roles, a Help center and a setup checklist, pointing only to ByteBunker.
6. **Fix every glitch the survey found** (35 rows below), each with a test.

About 73 to 96 working days for one engineer, phases 0 to 6, with the dgx-serve work running alongside the server work. The 1.0 gate is shapes A, B, C and E plus the CLI. Fifteen decisions are the owner's (section 10); five small items should happen this week.

## Do first (small, urgent, independent of the rest)

1. **A 12,186-line Claude Code session transcript is public** in dgx-spark-serve (`bytebunkerk3.txt`, 721 KB, committed 2026-08-11 in f3d01a5). It names a person, hosts and paths. Remove it now; purging history needs a force-push (decision 1).
2. **The engine answers the whole tailnet without a key.** vLLM listens on `0.0.0.0:8888` with no API key; a plain request from the laptop over Tailscale returned the model list (measured). Give it a key file or bind it to loopback and the gateway bridge.
3. **The live model is held up by one shell.** The cluster launcher's `trap cleanup INT TERM EXIT` stops both nodes when it exits, `serve_node` has no restart policy, and the running GLM engine hangs off a `rack up` shell started 1 h 49 min earlier (measured). Until phase 3 lands, start models from a `tmux` session on spark-1, not from the app.
4. **dgx-spark-setup's security stage runs `ufw --force reset`** (`scripts/02-security.sh:17`), wiping the fabric and bridge rules. Guard it.
5. **Commit the dgx-spark-serve work in progress to a branch** (about 30 modified or untracked files, including the `rack gateway` feature and the recipe serving today, `glm53-flash-keys`), so the refactor starts from a known state; production pins a tag.

## Facts this plan rests on (measured 2026-10-05)

- The gateway advertises 15 models; exactly one is served. Every other choice errors.
- Serving does not survive: solo is `exec docker run --rm` in the foreground (`launch-solo.sh:68`); cluster is held by its launcher (`launch-cluster.sh:181, 223`); the app's `/api/rack up` stream has a 3600 s cap.
- spark-1's ufw drops host-network ports on the LAN; Docker-published ports (LiteLLM's :4000) bypass it; the tailnet works.
- hermes (Apple M4, 16 GB, macOS 27) reads per-core CPU, memory, GPU use, thermal pressure and every listening port without root. Macs ship bash 3.2, BSD tools and a 3.9 `python3`, so node code must stay bash 3.2 and stdlib Python 3.9.
- The WSL2 agents worker is gone while its Windows host is up: WSL2 stops after a reboot or idle. The agents' doer model (`qwen3-4b-fast`) lives there, so every agent run started now fails at its first minion.
- hermes's `sessions.json` is 48 MB and is rewritten after every chat turn. The Usage screen takes 8 s while the worker is offline.
- GLM-5.3-Flash decodes ~2.5 tokens per step (MTP), so per-step and per-token latency differ.
- No public repo has a LICENSE; dgx-spark-serve has no tags, no CI, tests only for the monitor; several production images are built outside the repo.
- Web: bytebunkerlabs.ai, blog., fits. live on Cloudflare (`bytebunkerlabswebsite` monorepo with a shared brand system). docs. and get. do not exist.

## 1. The product in one picture

| Piece | What it is | Runs on | Owns |
|---|---|---|---|
| **ByteBunker** (the app) | desktop app for macOS and Windows; the same server runs headless on Linux or macOS; ships the `bytebunker` CLI (`bb` for short) | the user's computer, or an always-on box like hermes | engines and gateways, monitors, roles, chat and sessions, tools (MCP), skills, profiles, workflows and jobs, agent runs, usage, help |
| **dgx-serve** (`rack`) | deploys and runs models; owns the engine and the monitor on each machine | DGX Spark, NVIDIA Linux, Windows via WSL2, Apple Silicon Mac | node inventory, recipes, engines, monitor, pairing |
| **ByteBunker Agents** | the Sultan's court in throwaway containers | a separate worker | agent runs and their sandboxes |

The default path has no LiteLLM: each machine's engine carries a key and the app routes by model, which it already does across any number of endpoints (`gateways.py`). The author's existing LiteLLM keeps working through `rack gateway`.

Principles each phase is checked against:
- **One path per task.** The UI, the CLI and the help pages show the same one.
- **Every instruction is a command or a button**, copyable, with the expected output.
- **Measured, never guessed.** What cannot be measured shows as unknown.
- **The UI and the CLI are two faces of one server**; both land in the same sessions, traces and usage.
- **Only our products are recommended.** Engines (vLLM, llama.cpp) run underneath dgx-serve and appear only as facts ("engine: vLLM"), never as instructions, links or install steps.
- **Nothing leaves the user's network** unless they ask: no telemetry, no web fonts, update checks on demand.

## 2. Deployment shapes, as a user lives them

Each ends the same way: a working model in the Playground and every machine on the Cluster screen. The Help center has one page per shape with exactly these steps.

**Supported for 1.0** (`rack init` refuses anything else and says why; dgx-serve never installs GPU drivers): DGX OS; Ubuntu 22.04 or 24.04 on x86_64 or arm64 with an NVIDIA driver installed; Ubuntu 24.04 under WSL2 on Windows 11; macOS 14+ on Apple Silicon.

**A. One machine does everything** (a Mac; a Linux PC with an NVIDIA GPU; a Windows PC with its GPU via WSL2)
1. Install the app (Linux: `install.sh` headless server plus a browser). First run detects the local GPU ("Apple M4, 16 GB unified, about 10 GB usable for models").
2. **Serve a model on this computer**: the app installs dgx-serve locally (on Windows into WSL2), lists recipes that fit, runs `rack up` with a live log.
3. The local engine and monitor are added automatically. Nothing to copy.

**B. One model machine, the app elsewhere** (one Spark, one NVIDIA Linux box, a Mac mini, a Windows PC via WSL2)
```
curl -fsSL https://get.bytebunkerlabs.ai/serve | sh   # installs dgx-serve (signed tarball, versioned)
rack init        # detects platform, GPU, memory, disk; checks driver vs image CUDA; lists anything missing with the fix
rack setup       # additive, with --dry-run: container toolkit, firewall rules for engine and monitor, linger
rack up <recipe> # detached engine with a key; returns when healthy; survives logout and reboot
rack monitor up
```
Every `rack pull` and `rack up` also takes a platform flag (`--dgx`, `--linux`, `--windows`, `--mac`; section 3.2) to choose explicitly.
In the app: **Add a rack** → `user@host`. The app pairs over SSH (`rack pair --json`): it reads engine and monitor addresses and keys, installs its own SSH key restricted to running `rack`, and pins the host key. Nothing is printed or pasted. Fallback: `rack pair` prints a one-time string to paste.
On Windows, one admin PowerShell line installs WSL2 and dgx-serve and does the Windows-side setup (section 3.2); after that the same `rack` commands work from PowerShell.

**C. One model across several machines** (two Sparks with TP=2 today)
```
rack nodes add spark-2 --fabric 192.168.100.2   # on the head
rack preflight          # fabric, RoCE/NCCL, same image digest and same weights on every node
rack pull <hf-id>       # download once (HF token if gated), replicate, verify shards
rack up <recipe>        # tensor parallel across the inventory; a boot unit re-forms the group after a reboot
rack monitor up         # one monitor per node; the head answers for all
```
Then Add a rack with the head. More than two nodes is planned and tested as dry runs (`rack up --plan --json`); real hardware later.

**D. Several machines, each serving its own model** (Sparks + a 2070 box + a Mac): for 1.0, add each machine in the app (Add a rack per machine); the app already merges any number of engines and monitors. A single head that fronts the whole fleet comes after 1.0.

**E. Add an agent worker** (any shape): App → Agents → **Add a worker** → `user@host`. The app installs ByteBunker Agents over SSH from its own bundle, builds the sandbox image, installs a monitor, and runs a test goal. No clone, no GitHub access.

## 3. dgx-serve deploys on any machine

**3.1 Platforms**

| Platform | Engine | Runtime | Kept alive by | Monitor |
|---|---|---|---|---|
| DGX Spark (GB10) | vLLM, sm_121 images | Docker | `--restart unless-stopped`; cluster: a boot unit on the head | container |
| NVIDIA Linux, 1..N GPUs | vLLM image per GPU generation (Turing: Triton attention, fp16, as learned on the 2070); llama.cpp CUDA for small GPUs | Docker | restart policy | container |
| Windows via WSL2 | vLLM, or llama.cpp CUDA for GGUF models | Docker if present, else a uv venv | systemd user unit + Windows startup task | bare process |
| Apple Silicon Mac | llama.cpp with Metal, a pinned release binary (decision 5) | native | launchd agent | bare process, macOS sampler |

Tensor parallelism across machines stays a Spark-and-fabric feature. Macs and WSL2 boxes serve one model per machine. AMD GPUs and Windows without WSL2 are out of scope.

**3.2 One command on every platform.** `rack pull` and `rack up` take a platform flag: `--dgx`, `--linux` (NVIDIA Linux), `--windows`, `--mac`.
```
rack pull --mac qwen3-8b                # the Mac variant's GGUF file; resumable; disk checked first
rack up   --mac qwen3-8b                # llama.cpp with Metal, kept alive by launchd
rack pull --dgx glm53-flash             # safetensors to the head, replicated over the fabric, shards verified
rack up   --dgx glm53-flash             # vLLM sm_121 image, tensor parallel across the inventory
rack up   --windows qwen3-4b-fast       # typed in PowerShell or inside WSL2; vLLM with Turing settings, or llama.cpp CUDA
rack up   --linux qwen3-8b              # vLLM image for the GPU's generation
rack up   --mac qwen3-8b --on hermes    # from another machine: run it on the Mac in the inventory, over SSH
rack up   --windows qwen3-4b-fast --plan  # print exactly what would run, on any machine
```
- With no flag, `rack` uses the platform `rack init` detected. A flag that does not match the machine is refused with the reason and the fix ("this is a DGX Spark; `--mac` needs a Mac: run it there, or add the Mac with `rack nodes add` and use `--on`").
- The flag picks the recipe's variant for that platform, so one recipe name works everywhere it has a variant. `rack recipes` shows which platforms each recipe supports; `rack recipes --mac` lists only those that fit this Mac.
- `rack pull <recipe>` fetches exactly what that variant needs (safetensors for vLLM, one GGUF file for llama.cpp); `rack pull <hf-id>` still takes a raw repo. One stdlib downloader serves every platform: resumable, checksummed against the hub's metadata, HF token for gated models, and on Windows into the WSL2 filesystem rather than the slow `/mnt/c`. `rack up` pulls first when weights are missing, after showing the size and checking disk.
- Engines are pinned per platform: vLLM images by digest; llama.cpp from its numbered release builds, which ship macOS arm64, Linux CUDA (x86_64 and arm64, so WSL2 too) and Windows CUDA binaries (checked: build b11430, 2026-10-05).
- `rack fit --mac|--windows|--linux|--dgx` uses that platform's real budget: the Mac's Metal working-set limit, the GPU's VRAM on Windows and Linux, unified memory on a Spark.
- **Windows gets its own installer.** One admin PowerShell line (`irm https://get.bytebunkerlabs.ai/serve.ps1 | iex`) installs WSL2 Ubuntu if missing, dgx-serve inside it, a `rack.cmd` shim on the Windows PATH that forwards to WSL, the startup task, mirrored networking where Windows supports it (otherwise a task that re-points the port forward after each reboot), and the firewall rule. After that `rack` works the same from PowerShell and from inside WSL.

**3.3 Layout.** `lib/platform.sh` (OS, architecture, WSL, Spark, GPUs and compute capability, memory, Metal limit, container runtime, init system); `lib/runtime/{docker,native}.sh` (start, stop, logs, status); `lib/engines/{vllm,llamacpp}.sh` (argument builders, health, multi-node ranks); `py/` helpers for recipes, fit and pairing. Node code is bash 3.2 and stdlib Python 3.9, checked by shellcheck and bats on Linux and macOS. Config moves out of the checkout to `~/.config/dgx-serve/` (node identity, inventory, engine key, HF token, recipe overlay); state to `~/.local/state/dgx-serve/serving.json`.

**3.4 Single-node bugs fixed first:** an empty worker turns back into spark-2 (`${WORKER_SSH:-spark-2}` in `rack:27`, `build.sh:11`, `stop-cluster.sh:6`, `sync-model.sh:14`); "is this the head" requires the fabric IP, so cloud and WSL boxes break (`rack:44-47`); `rack build` and `build.sh` disagree on the default; `rack logs` tails a file nothing writes; `verify` prints a literal spark-1; the hardcoded `cd dgx/dgx-spark-serve` on the worker.

**3.5 Inventory.** `rack nodes add|ls|rm|test`, one file per node (SSH target, platform, fabric IP and interface, HCA names, GPU count, role); `rack init` imports today's `.env`. Everything that assumes two nodes reads it: `run_on`, image sync, `sync-model.sh`, `stop-cluster`, preflight, `fit`, the monitor's peers, the literal `--nnodes 2` (`launch-cluster.sh:168`). Node count = TP × PP ÷ GPUs per node.

**3.6 Serving is always detached.** `rack up` streams logs until the engine is healthy, then returns; Ctrl-C detaches instead of stopping. The launcher owns host, port, the engine key (`--api-key-file`), and a memory cap derived from total RAM. `rack up --plan --json` prints every per-node command, so 1, 2 and 4-node launches are tested without hardware.

**3.7 Recipes v2: one folder per model, one file per platform.** `recipes/qwen3-8b/model.env` holds what every platform shares: `MODEL`, `ROLES`, the dialect (`DIALECT_THINKING`, `DIALECT_EFFORT`, `DIALECT_STRIP_REASONING`) and client rules that today live only in comments (GLM needs a generous `max_tokens`; DeepSeek needs `reasoning_content` echoed back). `dgx.env`, `linux.env`, `windows.env` and `mac.env` each source it and set `ENGINE`, the image or `ARTIFACT` (GGUF files), and `SERVE_ARGS`. Recipes stay sourced bash, and today's flat files keep working as DGX recipes. Context, tools, parsers, vision and speculative decoding are read from `SERVE_ARGS`. `rack recipes --json` sources each file in a subshell; `rack recipes check` rejects command substitution; `rack new <name> <hf-id> --mac` scaffolds one variant. Curated recipes ship in the repo; the author's own (uncensored, abliterated, film) move to a private overlay.

**3.8 Model facts reach the app.** `rack up` writes `serving.json` (model, port, roles, dialect); the monitor publishes it as a `serving` block in `/v1/node` and `/v1/cluster`; the app prefers it over its hand-kept table.

**3.9 LiteLLM, for those who have it.** No gateway by default. `rack gateway` (the author's work in progress) gets an external mode: it edits only between `# >>> dgx-serve managed` / `# <<< dgx-serve managed` markers, finds conflicts by scanning lines (no PyYAML), takes `api_base` from the inventory instead of `172.19.0.1`, removes the route on `rack down` (fixing the 15-model list at its source), and `rack gateway adopt <name>` moves an existing entry inside the markers only on request. The gateway image is pinned to the digest running today (`sha256:60f548df…`).

**3.10 The monitor everywhere:** a macOS sampler and a bare mode (launchd or systemd) for Macs and WSL2; peers from the inventory (rackmon.py already handles N).

**3.11 Images.** Definitions in `images/<name>/` with a pinned base digest and vLLM commit; mods baked in, so one mod mechanism remains; built on the Spark as a self-hosted CI runner (arm64, sm_121) and GitHub-hosted runners (x86_64); pushed to the ByteBunker registry; recipes pin digests. Bases are upstream vLLM or `nvidia/cuda`, not NGC, whose license limits redistribution.

**3.12 Install, version, update, uninstall.** Release tarballs with sha256 and an `ssh-keygen -Y` signature, installed to `versions/<v>` with a `current` symlink; `rack self-update` runs migrations and can roll back; `rack version --json` reports CLI, recipe, monitor and JSON schema versions for the app's compatibility check; `rack uninstall` removes services, containers and firewall rules and keeps weights unless asked. Before `pull` and `build`: disk-space check and visible 10 to 20 GB image downloads.

**3.13 Repo hygiene.** The transcript, the iMessage handle in the harness's `config.example.yaml`, `nohup.out`, the lab notebook and H3 film tooling leave the product repos; README and docs match the code.

## 4. The app

**4.1 Foundations: events and runs** (phase 1, everything else builds on them). `events.py`: one bus with a global sequence number, a ring of recent events, per-run JSONL in `data/runs/`, served as `GET /api/events?topics=…&after=` (SSE with `id:`, so a dropped stream resumes by itself; one stream per tab). `runs.py`: a registry of runs (chat, compress, job, workflow, agents, deploy) with source and state (queued, running, waiting for approval, done, error, cancelled, interrupted on restart). A run no longer dies with the HTTP connection that started it (today it does: `server.py:1549-1571`).

**4.2 First run.** Three doors: Add a rack (pairing over SSH); find racks on the network; serve on this computer. The card stays until an engine actually answers. Starter prompts stop assuming the author's rack.

**4.3 Deploy replaces Recipes.** Paired nodes with platform, GPU, memory and what serves; recipes per node, the variant for that node's platform, with a fit verdict; up / down / logs streamed as runs; models on disk; monitor state. Every action is `rack … --json`, run directly on this computer and over SSH elsewhere (a Mac node needs Remote Login; the guide says so). `recipes.py`, its scripts and the text parsing of `rack` output (`server.py:289-323`) are retired.

**4.4 Roles live in the app.** Settings → Roles maps big, fast, vision and thinking to what is serving, seeded from recipes' `ROLES`. Jobs, workflows, profiles and agent runs refer to roles. A run that needs a role with nothing live behind it stops before it starts, with the reason.

**4.5 Model facts.** Context window, thinking switch, effort levels, vision and decoding style come from the monitor's `serving` block; vLLM's `max_model_len` (ignored at `gateways.py:122`) covers other engines; learned windows persist (`model_facts.py`). `DEFAULT_CAPS` becomes a fallback. The model list shows only models that answer.

**4.6 Help center.** Guides in `docs/help/*.md` with front matter (id, title, screen, platforms), copied into the app at build time with a search index. A renderer of about 300 lines, no raw HTML: headings, lists, tables, callouts, code blocks with copy buttons, `help:id` links, and platform blocks that show the user's OS first. Every screen's "?" opens its guide beside it; every API error carries a help id that links to the fix (a lint enforces it). `GET /api/setup` drives a checklist of measured items, each with one action. Code blocks tagged `{check}` run in CI against the fakes, so guides cannot rot. docs.bytebunkerlabs.ai is an `apps/docs` app in the website monorepo, built from the same files at each release tag. `agents-flow.html` is rewritten for any setup.

**4.7 Only our products.** Bundle the Geist fonts (Google Fonts loads on every launch today); the MCP catalog's 16 links to other people's repos become in-app descriptions; stale advice ("point config.json upstream_url…", "enable agents in config.json") goes; examples use neutral addresses, not `172.16.25.83`, `100.90.164.11`, `mo@bunker`, `~/rack`, `leagueofash`. The README leads with dgx-serve, keeps "works with any OpenAI-compatible server" as one line of fact, and drops the "right-click Open / Run anyway" steps once builds are signed. A CI lint fails on new third-party links or the author's addresses.

**4.8 Glitches** (fixed with regression tests; rows marked R are fixed once in the server runner, not in the browser loop it replaces)

| Glitch | Where | Fix |
|---|---|---|
| Gateway lists 15 models, one served | LiteLLM routes | live-only list; `rack down` removes routes |
| Every agent-kind job fails (`cmd, cwd = build_command(...)` on one list) and never parses `BB_JOB` | `server.py:429` | jobs run as workflows through the runner |
| `--headless` skips the single-instance check: two servers, two schedulers, one data folder | `desktop/app.py:305` | `instance.lock` in every mode |
| Concurrent tool calls to one MCP server steal replies, then hang 120 s | `mcp.py:60-95` | one reader per server, replies routed by id |
| Any MCP edit restarts every server and kills calls in flight | `server.py:547-565` | restart only the edited server, in the background |
| R: Enter with no model selected clears the box and drops the message | `console.js:900, 1211` | keep the text; say "pick a model" |
| R: effort never sends "High" and hides "max" | `console.js:947-953` | abstract levels mapped per model |
| R: the learned context window is lost on reload | `console.js:209-216` | `model_facts.py` |
| The Jobs screen's 15 s refresh wipes a half-typed form | `console.js:2139-2170` | event stream, list-only refresh |
| The scheduler runs one job at a time and skips cron minutes while busy | `jobs.py:245-297` | runs on the run registry; misses recorded |
| Deleting a job keeps its history though the UI says otherwise | `jobs.py:202` | delete it |
| Job tokens never reach Usage | `run_job`, `/api/usage-event` | one ledger line per turn, written by the server |
| Sessions: one 48 MB file rewritten after every turn by whichever client saves last | `server.py:658-674` | append-only `data/sessions/<id>.jsonl`, an index, one active turn per session; migrated once with a backup |
| The Usage screen takes 8 s while the worker is offline | `agents.py:513` | local ledger first; worker numbers arrive later, marked stale |
| `config.json` read once, never migrated; outside edits lost | `server.py:184-193, 568-581` | versioned config with migrations; the server is the only writer |
| Models screen and picker never refresh | `console.js:4034, 2449` | event stream |
| `#video` deep link opens a hidden screen with the author's instructions | `console.js:3955` | hidden screens are not routable |
| Terminal tool says "macOS" everywhere; none on Windows | `mcp_terminal.py:42` | per-OS shell, PowerShell on Windows |
| A new skill falls back to the read-only app bundle | `server.py:243-261` | always the data folder |
| Skills never reach agents in production | harness `graph/master.py:48`, `spawn.py:309` | pass the bodies |
| A goal named `status`, `cancel`, `skills`… runs a command | harness `graph/master.py:60-98` | `--goal` never parses commands |
| The app links a run to "the newest goal directory"; two runs mix | `agents.py:195` | the goal id is the app's run id |
| Runs exit 0 when the goal failed | harness `run_master.py:83` | exit codes per outcome |
| The Sultan's own requests carry no thinking or effort | harness `agent.py:73-77` | profiles apply to every role |
| Agents polling sshes every 10 s; `/api/agents` rereads weeks of traces | `agents.py:542`, `server.py:1185` | worker monitor; events tailed by offset |
| Live agent view breaks on dashed role names; app assumes podman | `agents.py:197-243, 653` | parse by run id; runtime-agnostic stop |
| A Linux worker reports "not ready" (iMessage checks) | harness `preflight.py:85-95` | platform-aware preflight; iMessage left out of the product |
| README says Python 3.9+, install.sh needs 3.10; hermes runs 3.9 | README:91, install.sh:38 | 3.9+ everywhere, tested on Linux in CI |
| `install.sh --with-harness` puts agents on the app's host | install.sh:59-71 | removed |
| Serving stops with its launching shell; nothing returns after reboot | `launch-solo.sh:68`, `launch-cluster.sh:181` | detached serving (3.6) |
| The engine answers the tailnet without a key | vLLM `0.0.0.0:8888` | key file (3.6) |
| `rack gateway` hardcodes `api_base` `172.19.0.1:8888` and needs PyYAML | `rack:319, 336` | inventory address; line scanning (3.9) |
| An empty worker becomes spark-2; head detection needs the fabric IP | `rack:27, 44-47` | inventory (3.4, 3.5) |
| dgx-spark-setup's `ufw --force reset` | `scripts/02-security.sh:17` | new additive `rack setup` |
| ROADMAP.md and parts of ARCHITECTURE.md describe removed features | docs | rewritten with the Help work |

## 5. The CLI: `bytebunker`, or `bb` for short

**One engine, two faces.** The CLI never calls a model or writes a log itself. It is a client of the same local server the UI uses, so every chat, tool call, agent run and job it starts lands in the same sessions, trace log and usage ledger, written by one process, and the app sees it live through the event stream. That is "the same log files", done safely.

**The server runner.** `upstream.py` (pulled out of `_chat`, same retry and trace behavior) and `runner.py`, a port of the browser's `send`, `streamTurn`, `buildMsgs` and compress (`console.js:528-1205`): context fitting and overflow retries, message rebuild, effort mapping, skills in the system prompt, tools, compression, attachments, usage, session events. Endpoints: `POST /api/sessions/<id>/turns` (202 + run id), `POST /api/sessions/<id>/compress`, `POST /api/runs/<id>/cancel`, `POST /api/runs/<id>/tool-results`, `GET|POST /api/approvals`, `GET /api/hello`. Events: status, text and reasoning deltas, tool calls with argument progress, approvals, tool results, usage, done.

**Moving the Playground onto it, safely:** (1) extract `upstream.py` with no behavior change; (2) the CLI and jobs use the runner first (deleting `run_job` also fixes its broken agent branch); (3) a parity suite runs 30 scripted conversations through the browser loop and the runner against a fake engine that records every request, and requests, session records and ledger lines must match; (4) the Playground switches behind `features.server_runner`; (5) dogfood on hermes and the desktop app; (6) on by default in 1.0, the browser loop deleted in 1.1.

**Approvals.** Each tool's policy starts from its MCP annotations (read-only runs, destructive asks); profiles override. The first answer from any client wins, no answer in 10 minutes denies, jobs never ask (an "ask" tool in a job is rejected when the job is saved).

**Workspace tools run in the CLI's own process.** The CLI sends `client_tools` (run, read, write, edit, list, glob, grep) with each turn; the runner sends those calls back to the CLI, which runs them in its own folder and environment (venv, PATH, ssh-agent) and posts results. Secrets never reach the server, file tools are confined to the workspace, PowerShell on Windows. This replaces the one shared working directory of `mcp_terminal.py` for CLI sessions.

**Profiles, workflows, commands.** Profiles in config (role or model, abstract effort mapped per model, sampling, max tokens and hops, auto-compress, tools, MCP servers, skills, system prompt, approvals); defaults Default, Fast, Deep. Workflows (name, chat or agents, profile, a prompt with `{{param}}` placeholders, optional folder) run from the UI, `bb run`, or a job; jobs gain a `workflow_id`. `commands.py`, a table of about 25 commands (CLI path, slash command, arguments, HTTP route), feeds the CLI, the REPL and the Playground's slash commands, with a parity test; the Cmd-K palette waits for 1.1.

**Commands**
```
bb                         # chat in this folder: /model /effort /profile /tools /mcp /skills /attach /compress /new /open
bb ask "…" [-p profile] [--effort off|low|medium|high|max] [--model m] [--json] [--yes]   # one shot; stdin becomes an attachment
bb run <workflow> -p key=value       bb agents "goal" [--detach] [--timeout 90m]    bb agents ls|watch|stop
bb jobs ls|add|run|logs|on|off       bb sessions ls|show|export|open
bb models | engines | monitors | cluster | usage | skills | mcp       # list, add, remove, find
bb deploy ls | up <node> <recipe> | down <node> | logs <node>      # the node's platform picks the variant
bb pair <user@host>      bb doctor      bb trace tail      bb serve
```
Exit codes: 0 ok, 1 engine error, 2 usage, 3 cancelled, 4 approval needed but not interactive, 5 no server. Without a terminal it never prompts.

**Finding and starting the server.** `instance.json` (pid, port, version, a local token; mode 0600) plus `instance.lock` in every mode. The server is its own process; the window and `bb` both call one `ensure_server()`. macOS starts it through LaunchServices as a background helper inside the signed app (a bare interpreter loses Local Network permission, as hermes's uv Python did); Windows runs `ByteBunker.exe --headless` detached; Linux uses `systemd-run --user`. It exits after 30 idle minutes unless "Keep running in the background" is on. The local token keeps other OS users out; the browser gets it through a one-time URL handoff when the app or `bb open` launches it.

**Install.** macOS: Settings → **Install command-line tool** writes a `bb` shim to `~/.local/bin` that runs the app binary with `--cli`. Windows: a real installer replaces the zip, puts `bb.cmd` on the user PATH, and provides uninstall. Servers: `install.sh` adds the shim. Stdlib only, Python 3.9+, colors through ctypes on Windows.

**In the app:** Sessions shows live CLI runs with a terminal badge; opening one follows it live; approvals can be answered there; you can type into a CLI session from the app.

## 6. ByteBunker Agents inside the product

- **Bundle**: release CI checks out the harness at a pinned tag and packs the wheel, sandbox Dockerfile, skills and PyYAML wheel into `agents-<v>.tgz` with a sha256 inside the app.
- **Installer** (`agents_install.py`, over SSH, idempotent, prints `BB_STEP` lines): platform check; podman and uidmap; linger (the one sudo line); uv; bundle upload and checksum; venv; sandbox image build; `rack monitor up --bare`; self-test. Re-running upgrades in place.
- **Detached runs**: each goal runs as `systemd-run --user --unit bb-run-<id> -p RuntimeMaxSec=<timeout>`, surviving SSH drops and a sleeping laptop, with a hard deadline; stopping the unit kills the whole group. The run spec arrives over stdin into a 0600 `spec.json`, deleted when the run ends. The app tails `events.jsonl` from a byte offset and re-attaches after a restart; usage feeds the ledger directly. Without systemd, today's setsid wrapper stays.
- **Roles per run**: the spec carries master, thinking and fast, each with its address for agents (LAN), model, key and effort; the harness's LLM proxy routes by model. Keys never appear on a command line, in an environment, or inside a container.
- **Harness fixes that ride along**: goals never parsed as commands; the goal id is the app's run id; real exit codes; skill bodies passed; platform-aware preflight; iMessage left out of the product build; protocol v2 shipped in step with the app, v1 accepted for one release.
- **WSL2**: a Windows startup task that runs whether or not anyone logs on (`wsl.exe … sleep infinity`, the one admin step), `vmIdleTimeout=-1` where supported, and a "worker asleep" state instead of time-outs.

## 7. The quality bar: "no glitches", made checkable

- **Fakes**: a fake engine that scripts streaming, tool calls, reasoning, overflow 400s and stalls and records every request; the fake monitor; a fake worker. No test needs a GPU.
- **CI on macOS, Windows and Linux** (server on Python 3.9 and 3.12; node code under bash 3.2 too): unit and API tests for every route (today none cover the chat proxy, tool loops, sessions, jobs, agents or MCP concurrency); the runner parity suite; Playwright journeys for each shape and screen; CLI tests; contract tests pinning the monitor schema, `rack … --json` and the harness protocol on both sides; `{check}` blocks in the guides; link and naming lints.
- **Version contract**: the app reads `rack version --json` and the harness's protocol version and says exactly what to update.
- **Lifecycle**: migrations for sessions, config, `.env` → inventory, and the worker's harness; uninstall for all three products; a rotated server log; a trace retention setting.
- **Error-handling audit**: no silent `catch (e) {}` left; every failure says what failed and the one thing to do next.
- **24-hour soak** with the app open (memory, threads, sockets, log growth) and a **diagnostics export** (versions, redacted config, logs, connectivity) next to **Remove all data**.
- **Signed builds**: Developer ID and notarization on macOS (including the server helper), Trusted Signing on Windows.

## 8. Security and networking

- Secrets live in a 0600 `secrets.json`, never exported, logged or included in diagnostics. The OS keychain comes after 1.0 (it is locked for a headless server at boot, and Linux has no stdlib keychain).
- The app API stays loopback-only behind its existing guard (loopback Host, no foreign Origin, JSON-only POSTs, `server.py` `_guard`), plus the local token for other OS users.
- Pairing runs over SSH with the app's own key, installed with `command="rack remote",restrict` so it can only run `rack`, and the node's host key pinned.
- Engines carry a key file (vLLM `--api-key-file` guards `/v1` and leaves `/metrics` to the monitor); `rack setup` opens only the engine and monitor ports, only on the interfaces the user picks, and prints the rules.
- The monitor stays read-only; control stays on SSH.
- Agents keep the three-plane rule (decision 14 covers the worker's own small model).

## 9. Phases (working days, one engineer)

| # | Scope | Days | Needs | Done when |
|---|---|---|---|---|
| 0 | Do-first items; decisions; repo cleanup and licenses; apply for signing (D-U-N-S), DNS; fakes; Linux CI (3.9 and 3.12); bats on macOS | 4–6 | – | CI green on three OSes; the fake engine scripts tool calls, overflow and stalls |
| 1 | Event bus, run registry, session store and migration, `upstream.py`, instance lock, the glitches the runner does not replace | 8–10 | 0 | A copy of hermes's real `sessions.json` migrates losslessly; 100 overlapping MCP calls pass; a dropped stream resumes |
| 2 | Runner, profiles, workflows, command table, `bb` and server lifecycle, Playground behind the flag | 14–18 | 1 | Parity suite green; a CLI turn shows in the app within 1 s; cancel within 2 s; one ledger line per turn |
| 3 | dgx-serve 1.0 (all of section 3, including platform flags and the Windows installer) | 20–27 | 0, alongside 1–2 | Shapes A, B, C pass on hardware; `rack pull` and `rack up` work with `--dgx`, `--windows` and `--mac`; serving survives SSH drops and reboots; node code runs on bash 3.2 |
| 4 | App: Add a rack, Deploy, roles, model facts, live-only models, Help and checklist, lints, event stream replaces polling, runner on by default | 12–15 | 2, 3 | Playwright journeys green; Help works offline; lints green |
| 5 | Agents (all of section 6) | 9–12 | 2, 4 | Install from a clean distro; a laptop sleeps mid-run and re-attaches; a reboot without login brings the worker back; no keys in `/proc/*/cmdline` |
| 6 | Signing, Windows installer, docs and get sites, diagnostics, soak, hardware bug bash, draft releases | 6–8 + certificate waits | all | The owner signs off the release checklist |

About 73 to 96 days in total. After 1.0: the command palette, a fleet head and replicas (shape D in one pairing), managed LiteLLM, keychain, MLX, an Intel Mac build (GitHub is retiring Intel macOS runners and Intel Macs cannot serve models anyway), engines on Windows without WSL2 (llama.cpp already ships Windows CUDA builds), workflows the Sultan can call, Strax and Spectis hooks, remote access to the app, the Video studio, and more than two nodes on real hardware.

## 10. Decisions only the owner can make (recommendation first)

1. **The public transcript**: remove it now; purge it from history with a force-push.
2. **Names**: rename `dgx-spark-serve` → `dgx-serve` (GitHub redirects), keep `rack`; CLI `bytebunker` with `bb` as the short name (`bb` is also Babashka's command, so the installer skips it if one exists).
3. **Licenses**: Apache-2.0 for the app and dgx-serve.
4. **The harness**: ship it inside the app under an EULA. Keeping the repo private does not protect shipped code; a license does. Decide before CI bundles it.
5. **Mac engine**: llama.cpp with Metal, a pinned release binary; MLX after 1.0.
6. **Signing**: Apple Developer Program (needs a D-U-N-S number for an organization; start now) and Microsoft Trusted Signing.
7. **Web**: docs.bytebunkerlabs.ai and get.bytebunkerlabs.ai as apps in the website monorepo; images on a ByteBunker registry (GHCR).
8. **LiteLLM**: not in the default product; your setup keeps it through `rack gateway`'s managed markers.
9. **Your private recipes** (uncensored, abliterated, film): move to a private overlay repo; the public repo ships curated ones.
10. **Tools catalog**: third-party MCP tools stay as tools, described in-app; ByteBunker's MCP servers join when public.
11. **Linux desktop app**: not for 1.0 (headless server and a browser).
12. **Video studio**: out of 1.0; it returns as a dgx-serve recipe.
13. **Support matrix**: as listed in section 2; Windows 10 out of scope.
14. **An engine on the agent worker** (today's 2070 box runs both): allow it as an explicit, labelled exception, since sandboxes have no network and reach models only through the proxy; or move the small model to another machine.
15. **Recipe layout**: one folder per model with one file per platform (recommended), rather than one file with a block per platform. Folders keep each platform's file short and match today's sourcing of a parent recipe.

## Risks

| Risk | Mitigation |
|---|---|
| The runner changes model behavior (DeepSeek reasoning in tool exchanges, effort coercion, overflow retries) | parity suite, feature flag, dogfooding, one release to roll back |
| The dgx-serve refactor breaks the live rack | work-in-progress branch, a separate clone, versioned installs with rollback, hardware acceptance before switching |
| The platform matrix grows | the published matrix; `rack init` refuses with a reason; bash 3.2 CI; hardware smoke each release |
| macOS permission traps (Local Network, notarizing the helper) | the server only runs as the signed bundle or helper; tested on a fresh macOS user |
| WSL2 nodes and workers are not there when needed | startup task, keep-alive, an "asleep" state |
| Secrets leak (pairing data, command lines, traces, public history) | pairing over SSH, key files instead of arguments (tested), redaction, scrubbed repos, rotated keys |
| Legal (no LICENSE files, NGC terms, private recipes, shipping the harness) | phase 0 decisions, redistributable bases, curated recipes, an EULA |
| Schedule (one engineer, about four months) | the after-1.0 list; dgx-serve runs alongside the server work; the gate is shapes A, B, C, E plus the CLI |

## 11. Verification: what "done" means

On every push: everything in section 7 on macOS, Windows and Linux.

On real hardware before each release, recorded in the release checklist:

| Shape | Where | Pass when |
|---|---|---|
| A, Mac | hermes, a fresh macOS user | the signed DMG installs; "serve on this computer" runs a small model on llama.cpp; chat with tools works; Cluster shows the Mac; `bb ask` works and appears in the app live |
| A, Windows | the WSL2 box | the installer runs; the app serves a model on the local GPU through WSL2 |
| A, Linux | a Spark running the headless server and a browser | the same through `install.sh` |
| B | hermes as the node with the app on the laptop; the WSL2 box; one Spark solo | Add a rack pairs over SSH; chat, Cluster and Deploy work; the model survives closing the app, an SSH drop and a reboot |
| Platform flags | hermes, the WSL2 box, both Sparks | `rack pull` and `rack up` with `--mac` on hermes, `--windows` from PowerShell on the 2070 box, `--dgx` on the Sparks; a mismatched flag is refused with its reason; an interrupted pull resumes; `--linux` checked through `--plan` in CI |
| C | both Sparks | today's TP=2 recipe through the new launcher; back by itself after a reboot of both; 4-node plans verified as dry runs |
| E | the WSL2 box | Add a worker on a clean distro; a test goal passes; the laptop sleeps mid-run and re-attaches; after a Windows reboot without login the worker returns |
| CLI | macOS and Windows | every command against the rack; sessions, traces and usage appear in the app as they happen |
| Help | a clean machine | every guide followed literally, start to finish, nothing missing |

## Critical files

- Console: `server.py` (`_chat`, `run_job`, `_agents_run`, `_rack`, sessions, usage, `_guard`), `public/console.js` (`send`, `streamTurn`, `buildMsgs`, compress: lines 528–1205), `desktop/app.py` (instance and server lifecycle), `agents.py`, `mcp.py`, `jobs.py`, `gateways.py`, `monitors.py`; new `events.py`, `runs.py`, `upstream.py`, `runner.py`, `model_facts.py`, `commands.py`, `bb.py`, `docs/help/`.
- Harness: `graph/master.py`, `master/agent.py`, `harness/spawn.py`, `scripts/run_master.py`, `preflight.py`.
- dgx-serve (spark-1 `~/dgx/dgx-spark-serve`): `rack`, `scripts/launch-solo.sh`, `scripts/launch-cluster.sh`, `scripts/build.sh`, `scripts/sync-model.sh`, `scripts/stop-cluster.sh`, `monitor/rack-monitor.sh`, `monitor/rackmon.py`, `recipes/TEMPLATE.env`; new `lib/`, `py/`, `images/`.
