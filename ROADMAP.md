# Roadmap

What exists, what's next, and what is deliberately still an empty state.
The rule for everything below: **it ships when it can show real data.** A
screen that renders a simulated number is worse than a screen that says it
isn't wired yet.

## Shipped

- **Playground** — streaming chat, reasoning blocks, code blocks, tool calls
  rendered inline; all sampling params; measured TTFT / tok-s / context usage
  per reply; provenance (model + effort) stamped at send time.
- **Sessions** — server-persisted, restore with model, delete, new chat.
- **Models** — live from the upstream `/v1/models`.
- **Cluster** — Prometheus-backed per-node telemetry, 5 s poll, sparklines.
- **Usage** — aggregated from this server's own request ledger.
- **MCP** — stdio servers managed from the panel (add / toggle / restart /
  remove); bounded agent loop; tools namespaced per server.

## Next

### 1. Agents
Named, saved configurations that are more than a system prompt: model,
sampling, an allow-list of MCP tools, and a goal. Run one from the Playground
or headless. Depends on a small agent-run store (id, config, transcript,
status) and a run view that streams the same way chat does.

Open questions worth settling before building:
- Where does an agent run live — in the console's process, or as a detached
  worker so a browser refresh doesn't kill it? (Detached, almost certainly.)
- Per-agent tool allow-lists are the security surface. An agent with the
  filesystem server bound is exactly as dangerous as its roots.

### 2. Schedules
Run an agent on a cron expression. Needs #1 first, plus a scheduler
(launchd on the mini is the boring correct answer over an in-process timer)
and a run history view. The natural first uses on this rack: a nightly
digest over the day's notes, and a periodic re-benchmark that appends to the
serving repo's results ledger.

### 3. Fine-tuning
Currently an honest empty state. Renders real runs when a job manager exists
— torchtune on a single node, per the serve-out/train-down split. Wants the
same run store as #1.

### 4. Batch jobs
Also an empty state. JSONL in, JSONL out, fanned through the serving
endpoint. Smallest useful version: submit a file, watch progress, download
results.

## Smaller, worth doing

- Model picker should show context length and quantization from the upstream
  where it reports them.
- Attachments — the model is multimodal and the console can't send an image.
- Export a session as markdown.
- Stop button already exists in behaviour; make it read as a stop.
- Per-conversation tool allow-list, not just a global on/off.
- Token-cost estimate per turn against the configured frontier rates.

## Not doing

- Multi-user auth. This is a single-operator console bound to loopback;
  adding accounts would imply a security model it doesn't have.
- A build step. Vanilla JS, stdlib Python, no dependencies — the whole thing
  should stay readable in one sitting.

## bytebunkercode (requested 2026-08-04)

A Claude Code-style agent UI on the same stack: terminal-first transcript,
tool-use rendering as first-class blocks, plan/execute loops, session
resume. The console's MCP host, caps manifest, and engine telemetry are the
foundation; what's missing is the agent loop with editing tools and a
permission model. Big enough to design deliberately, not bolt on.

## Video studio: archive completed renders (noted 2026-08-05)
The engine's job store is container-local scratch — a restart deletes every
MP4 not manually downloaded (learned when a crash-restart ate an undownloaded
render). The console proxy could archive completed clips server-side on the
mini (poll for completed → fetch content → data/renders/) so the Download
button becomes a convenience, not the only copy.
