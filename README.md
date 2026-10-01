# ByteBunker Console

**[Interactive demo →](https://blog.bytebunkerlabs.ai/demo/console/)** — the real
interface replaying a real session: streaming replies, a tool call, both nodes'
utilization moving together.

A playground console for a model you host yourself. Streaming chat with reasoning
and tool calls, MCP servers you manage from the UI, live cluster telemetry, and a
usage ledger — in one file of standard-library Python and one page of vanilla
JavaScript. No build step, no dependencies, no account.

It talks to anything that speaks the OpenAI chat API: vLLM, llama.cpp's server,
LiteLLM, Ollama.

---

## Install

One command, no sudo, no pip: installs the console as a background service
(launchd on macOS, systemd user unit on Linux), writes `config.json`, and can
place the agent harness next to it.

```
curl -fsSL https://raw.githubusercontent.com/bytebunkerlabs/bytebunker-console/main/install.sh | bash -s -- \
  --upstream http://<gateway>:4000/v1 --key <litellm-key>            # add --with-harness for agents in local mode
```

Then open http://127.0.0.1:8765. See `docs/ARCHITECTURE.md` for how the console,
the harness, the gateway and the engines fit, `docs/RUNBOOK.md` to reproduce the
whole deployment command by command, and `docs/OPERATIONS.md` for the operator's
handbook (services, config, debugging, extension).

### From this checkout

Requires **Python 3.9+** and nothing else. macOS and most Linux ship it already —
check with `python3 --version`.

```bash
git clone https://github.com/bytebunkerlabs/bytebunker-console.git
cd bytebunker-console
cp config.json.example config.json
```

Point it at your model server by editing `upstream_url` in `config.json`:

```json
{
  "bind": "127.0.0.1",
  "port": 8765,
  "upstream_url": "http://127.0.0.1:8000/v1",
  "upstream_key": "not-needed"
}
```

Run it:

```bash
python3 server.py
```

Open **http://127.0.0.1:8765**. If the upstream is serving a model it appears in
the picker and you can start typing.

### No model server yet?

Any of these gives you an endpoint to point at:

```bash
# vLLM (NVIDIA GPU)
vllm serve Qwen/Qwen3-8B --host 127.0.0.1 --port 8000

# llama.cpp (CPU or Apple Silicon)
llama-server -hf unsloth/Qwen3-8B-GGUF --port 8000

# Ollama — note the /v1 suffix goes in upstream_url
ollama serve     # then set upstream_url to http://127.0.0.1:11434/v1
```

---

## Cluster telemetry (optional)

Two sources feed the node cards. **Prometheus** (`prometheus_url` + `nodes`)
reads node-exporter and the nvidia-smi exporter. **sparkDash**
([MiaAI-Lab/sparkDash](https://github.com/MiaAI-Lab/sparkDash), `sparkdash_url`)
reads its per-unit snapshot instead, which also carries each unit's LLM probe
(model, tokens/s, KV cache, queue, TTFT p95, prefix-cache hit rate) and treats
a non-Spark GPU box as a first-class unit. sparkDash binds to loopback on the
head Spark, so point `sparkdash_url` at an ssh tunnel (the console's tunnel
service forwards 127.0.0.1:15555 → spark-1:5555) and set
`telemetry_source: "sparkdash"`. `sparkdash_open_url` adds an "open sparkDash"
link on the Cluster screen (a Tailscale Serve URL works well).


The Cluster screen and the sidebar bars read from a Prometheus you already run:

```json
{
  "prometheus_url": "http://127.0.0.1:9090",
  "nodes": [
    { "name": "spark-1", "instance": "node-exporter", "spec": "GB10 · 128 GB unified" },
    { "name": "spark-2", "instance": "192.168.100.2", "spec": "GB10 · 128 GB unified" }
  ]
}
```

`instance` is matched against the Prometheus `instance` label as a prefix regex.
Queries assume [node_exporter](https://github.com/prometheus/node_exporter) and
[nvidia_gpu_exporter](https://github.com/utkuozdemir/nvidia_gpu_exporter); any
metric that doesn't resolve renders as an em-dash rather than a guess. Leave
`prometheus_url` empty and the screen says so instead of inventing numbers.

> **On GB10 specifically:** node_exporter's default collector set hangs on this
> hardware — scrapes pile up until every metric goes blank. Start it with an
> explicit minimal set:
> `--collector.disable-defaults --collector.meminfo --collector.cpu --collector.stat --collector.loadavg`

---

## Model deployment recipes

The **Recipes** screen deploys a model from the console. A recipe is a
parameter form that renders the exact files it would run — install script,
serve script, systemd unit, litellm entry — so you read them before anything
executes. **Deploy** runs the script over ssh on the target host with the log
streamed into the right pane; **Register in litellm** appends the entry to the
gateway's config (`litellm.ssh`, `litellm.config_path`, `litellm.container` in
`config.json`) and restarts it, so the new model shows up by name in the
Models screen and in the harness. Shipped recipes: vLLM on any CUDA GPU (Linux
or WSL2, tool calling on, rootless service), Ollama on Windows (manual steps),
vLLM on DGX Spark (docker, one or two nodes).

## Skills and plugins

Skills can be created in the UI (**Skills → ＋ New skill**): the form writes
`<name>/SKILL.md` into the first configured skills dir and mirrors it to the
agent worker, so the master can attach it on the next run. Plugins can be
added from a git URL or a local folder, created empty (with MCP servers), and
removed (**Plugins → ＋ Add plugin**).


A **skill** is a markdown instruction pack — `<name>/SKILL.md` (or `<name>.md`)
with frontmatter (`name`, `description`, optional `whenToUse`, `tools`,
`network`, `model`) and a body. It is the same format the agent harness uses,
so one directory feeds both: point `skills_dirs` at the harness's `skills/`.
The console ships a `skills/` folder and reads any dir in `skills_dirs` plus the
skills inside enabled plugins.

On the **Skills** screen you see the catalog and can read each pack's
instructions. Attach one and its body is prepended to the system prompt for
your chats, shown as a chip above the composer; the trace log captures the full
prompt, so a skilled turn is recorded exactly as the model saw it. Editing a
skill file takes effect on the next send — bodies are reread from disk, never
cached.

A **plugin** is a folder under `plugins/` (or a dir in `plugins_dirs`) that
bundles skills and MCP servers behind one switch. Enable it on the **Plugins**
screen: its skills join the catalog and its MCP servers join the tool host.
Manifest and layout are in `plugins/README.md`; the Claude Code plugin shape
(`.claude-plugin/plugin.json` + `.mcp.json`) is understood too.

## Agents (the harness)

The **Agents** screen launches your agent harness (`bytebunker-agents`) on a
goal and streams the Master's progress, while the console itself runs no agent
code. This follows the separation NVIDIA's agentic-safety guidance calls for —
three planes, each on its own host:

- **Model plane** — the Sparks. Serve the LLM only.
- **Control plane** — this console, on hermes. Launches goals, watches, holds
  the kill switch.
- **Agent plane** — a *dedicated worker host* (not a Spark, not the console).
  Runs the harness and Docker, so a slave gets `--network none` and reaches the
  model only through the harness's unix-socket proxy.

Point the console at that host with the `agents` config block. It is **disabled
by default and inert until you set it** — the launcher refuses to run without
`agents.enabled` and `agents.dir`. Set `agents.ssh` to the worker host; leaving
it empty runs the harness locally on hermes with **no isolation** (dev only,
and the screen says so). Each run is written to the trace log, so agent goals
feed the same training export as chats.

**Watching a run.** The Agents screen streams the master's narration live
(launched unbuffered), and *What your agents are doing* shows every running
slave's steps, tool calls and verifier verdicts as they happen — slaves write
their events to their task dir on the worker and the console tails them.
Every finished run is stored; click one under *Recent runs* to reopen its log.

**Stopping a run.** Stop (or closing the tab) reaches the worker: the harness
runs in its own process group and is terminated when the console drops the
connection, and any slave container it leaves behind is swept. A run that is
silent for minutes still notices a gone client thanks to SSE keepalives.

**After changing slave code** in the harness (anything under `src/` or
`docker/`), rebuild the image on the worker — slaves run from the image, not
the checkout: `podman build -f docker/Dockerfile.slave -t bytebunker-slave:latest .`

> **Why a separate host.** An agent that is compromised or goes wrong should
> not sit next to the model weights, the GPUs, or the LiteLLM key (the Sparks),
> nor next to the console that watches it. A dedicated worker box keeps the
> blast radius contained and keeps monitoring independent of the host running
> the agents.

## MCP tools

Give the model the ability to read files, search a repo, fetch a URL — anything
with an [MCP](https://modelcontextprotocol.io/) server behind it.

Add one on the **MCP** screen: a catalog of local stdio servers (Playwright, filesystem, fetch, git, memory, sequential thinking, time, SQLite, GitHub, Brave Search, Context7, Puppeteer, PostgreSQL, Slack, Prometheus, a test server, the built-in terminal) with one-click add, a streamed local install, environment editing, and a custom-server form. The params panel keeps a compact list with per-chat enable/disable.

```json
{
  "mcp_servers": {
    "fs": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/workspace"],
      "enabled": true
    },
    "git":   { "command": "uvx", "args": ["mcp-server-git", "--repository", "/Users/you/repo"] },
    "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] },
    "terminal": { "command": "python3", "args": ["mcp_terminal.py", "~/rack"] }
  }
}
```

Needs `npx` (Node) or `uvx` (uv) on PATH depending on the server; `~` in an
argument is expanded. Tools appear as `server__tool` so two servers can share a
name. The Playground then runs an agent loop — the model requests a call, the
console executes it, the result feeds back — and every call renders inline with
its arguments and result as it runs. **Tool hops** in the panel (default 40) is
how many calls the model may chain in one turn before it pauses; send
`continue` to let it carry on. A model that makes the identical call three times
in a row is stopped early. Stop always works.

**Terminal.** `mcp_terminal.py` ships with the console: one tool, `run`, that
executes a shell command on the host and returns exit code, stdout and stderr.
The working directory follows the model's `cd`s from call to call; the argument
is where it starts. Commands are killed after 60 s (the model can ask for up to
600) and long output is elided in the middle. Add it with the **terminal** preset.

> **Trust boundary.** An MCP server is a local process with exactly the access its
> arguments grant. The filesystem server can write anywhere under the roots you
> pass it, and the terminal server is a shell running as you — files, keys,
> network, everything. The console does not sandbox that. Enable the terminal
> because you want the model to have one, and switch it off (one click in the
> panel) when you don't.

---

## Run it in the background

**macOS (launchd)** — survives reboots, restarts on crash:

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/ai.bytebunker.console.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.bytebunker.console</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>$PWD/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PWD/data/console.log</string>
  <key>StandardErrorPath</key><string>$PWD/data/console.log</string>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/ai.bytebunker.console.plist
```

**Linux (systemd user unit):**

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/bb-console.service <<UNIT
[Unit]
Description=ByteBunker Console
[Service]
ExecStart=/usr/bin/python3 $PWD/server.py
WorkingDirectory=$PWD
Restart=always
[Install]
WantedBy=default.target
UNIT
systemctl --user enable --now bb-console
```

### Reaching it from another machine

The console has **no authentication** and refuses to bind anything but loopback —
that is the security model, not an oversight. To use it from a laptop, forward the
port over SSH:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@the-host
```

Then open `http://127.0.0.1:8765` on the laptop. An overlay network (Tailscale,
WireGuard) works the same way.

---

## Configuration reference

| key | default | what it does |
|---|---|---|
| `bind` | `127.0.0.1` | loopback only; anything else refuses to start |
| `port` | `8765` | |
| `upstream_url` | `http://127.0.0.1:8000/v1` | any OpenAI-compatible endpoint |
| `upstream_key` | `bb-local` | sent as a bearer token |
| `prometheus_url` | *(empty)* | enables the Cluster screen |
| `nodes` | two examples | `name`, `instance` (Prometheus label prefix), `spec` |
| `mcp_servers` | *(empty)* | `command`, `args`, `env`, `enabled` |
| `frontier_rates_per_mtok` | 3 / 15 | used for the "not spent" figure on Usage |
| `skills_dirs` | *(empty)* | extra directories of skill packs; point one at the agent harness's `skills/` to share them |
| `plugins_dirs` | *(empty)* | extra directories of plugins (beyond the repo's `plugins/`) |
| `agents` | *(disabled)* | harness launcher: `enabled`, `ssh` (worker host; `""` = local, not isolated), `dir` (harness checkout), `python`, `script` |
| `model_capabilities` | *(built-in table)* | per-model overrides keyed by a substring of the model id: `ctx` (the window the engine *serves*, `--max-model-len`), `tools`, `effort`, `ctk`, `strip_reasoning` |

State lives in `data/` — `sessions.json`, `usage.jsonl` and `archive/`, plain
files on the host, all gitignored.

---

## Long conversations

The window is shared between the prompt and the answer, and vLLM refuses a
request that asks for more than fits rather than trimming it. The console
keeps you inside it three ways:

- **Max tokens is clamped per request** to what the window has left. The prompt
  size is estimated, then calibrated against the engine's own count of the
  previous prompt. If the engine still says no, its error names the real
  window; the console remembers that figure for the session and backs off
  below the reported floor (vLLM's "at least N input tokens" is where its
  tokenizer *stopped*, not the prompt's length).
- **Auto-compress** (on by default, in the panel under *Context*): when the
  prompt nears the limit, the model writes a dense summary of the older turns
  and that summary takes their place. The last two exchanges stay verbatim,
  so the chat carries on for as long as you want.
- **`/compress`** typed into the chat does the same on demand.

Nothing is thrown away. Every compression files the original messages —
reasoning, tool calls and results included — under `data/archive/` as JSON
plus a readable `.md` transcript, and the marker in the transcript links to
it. If the served window ever changes, set `ctx` in `model_capabilities`.

---

## The trace log, and training data

Everything the console does is written down, append-only, under
`data/traces/` — one JSONL file per day, yesterday's gzipped, never pruned by
the console:

- every model request, with the exact payload the engine saw (system prompt,
  tools, sampling) and the response assembled from the stream: text,
  reasoning, tool calls, finish reason, token usage, time to first token, or
  the error;
- every tool run — arguments, result, exit status, duration — which for the
  terminal server means every command the model typed;
- every rating (**good** / **bad** under an answer), every compression
  archive, and the full record of any session deleted or aged out of the
  Sessions screen, so pruning the UI never loses the conversation.

**Export** (Sessions → *Export all* / *Export good turns*, or
`GET /api/export`) turns all of that — plus sessions and archives from before
the log existed — into one JSONL example per turn in the chat format most
fine-tuning stacks accept: `{"messages": [...], "tools": [...], "model",
"meta"}`, with `reasoning_content` on assistant turns and OpenAI-shape
`tool_calls`. Multi-hop tool turns come out as one trajectory. Query
parameters: `from`/`to` (dates), `model` (substring), `rated=up|any`,
`purpose=chat|compress`, `errors=1` to include failed turns, `redact=0` to
skip the heuristic pass that blanks things shaped like keys and passwords
(terminal output ends up in here — leave it on unless you have checked).

The server's own log lives at `data/console.log` when started with the
launchd recipe above, so it survives a reboot.

---

## What's real, and what isn't yet

Everything on screen is streamed from the upstream, measured on the wire, or read
from this server's own ledger. Screens without a backend say so rather than render
a plausible number — see [ROADMAP.md](ROADMAP.md) for what's coming (agents,
schedules) and what is deliberately still an empty state.

## Layout

```
server.py     HTTP server: static files, streaming chat proxy, sessions,
              usage ledger, Prometheus queries, MCP admin
mcp.py        MCP host — stdio JSON-RPC clients, tool discovery and calls
public/       index.html · console.css · console.js   (no build step)
config.json   yours, gitignored; config.json.example is the template
data/         sessions and usage, created on first run
```

## Background

Built for a two-node DGX Spark cluster; the write-up is
[Building a local console](https://blog.bytebunkerlabs.ai/posts/building-a-local-console/).
The serving side it talks to is
[dgx-spark-serve](https://github.com/bytebunkerlabs/dgx-spark-serve).
