# ByteBunker Console

**[Interactive demo →](https://claude.ai/code/artifact/329d8db0-1193-4c67-beb9-9c707527a4fe)** — the real interface replaying a real session: streaming replies, a tool call, both nodes' utilization moving together.

The console for the rack — implemented from the "ByteBunker Console" Claude Design
project. One stdlib-Python server (3.9+, zero dependencies — chatserve's successor),
one vanilla-JS page. Nothing leaves the rack.

    cp config.json.example config.json   # point upstream_url at the gateway or vLLM
    python3 server.py                    # http://127.0.0.1:8765

Real in v1: Playground (streaming, reasoning + code-block rendering, all sampling
params, measured ttft/tok-s meta line, View code), Sessions (server-persisted),
Models (live /v1/models), Cluster (Prometheus-backed when prometheus_url is set),
Usage (aggregated from this server's own request ledger).

Deliberately empty in v1: Fine-tuning, Batch jobs — the design's simulated data
does not ship. A number without provenance is a rumor.

## MCP tools

Manage servers from the params panel: **Add MCP server** (with presets for
filesystem / git / fetch / memory), then per-server enable, restart, remove.
Changes write to `config.json` and apply immediately — no console restart.
The Playground is then an agent loop: the model calls tools, the console runs
them, results feed back, up to 8 hops per turn.

Servers can also be declared by hand in `config.json`:

    "mcp_servers": {
      "fs": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/mo/rack"],
        "enabled": true
      }
    }

Tools appear as `<server>__<tool>` so two servers can share a tool name.
Toggle them per-conversation in the params panel; each call renders inline
with its arguments and result.

See [ROADMAP.md](ROADMAP.md) for what's next (agents, schedules) and what is
deliberately still an empty state.

**Trust:** an MCP server is a local process with whatever access its arguments
grant. The filesystem server can write anywhere under the roots you pass it —
scope them deliberately. The console does not sandbox servers.
