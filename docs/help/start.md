---
id: start
title: Start here
order: 10
summary: What ByteBunker is, and the three things to set up first
---
ByteBunker talks to the models you run yourself: on this computer, on a box under your desk, or on a rack of DGX Sparks. Nothing you type leaves your machines unless you send it somewhere.

## The first three things

1. **Connect a model server.** Open [Gateways](#gateways) and press **Find engines**, or add one by address. Any OpenAI-compatible server works. → [Connect a model server](help:gateways)
2. **Say something in the [Playground](#playground).** Pick the model at the top and type. → [The Playground](help:playground)
3. **Watch your machines.** If you serve models with dgx-serve, run `rack monitor up` on the rack and add the address it prints on [Cluster](#cluster). → [Rack monitors](help:cluster)

The checklist on the Help home measures each of these and links to the fix.

## Then, when you want them

- **bb**, the same app in a terminal: chats, agents, jobs and workflows from your shell, visible here as they happen. → [bb, the command line](help:cli)
- **Workflows**: a prompt you run again, with blanks to fill. → [Workflows](help:workflows)
- **Jobs**: the model does a task on a schedule. → [Jobs](help:jobs)
- **Agents**: a goal for a team of agents on a separate worker. → [Agents](help:agents)
- **Tools**: let the model read files, search, or run commands. → [Tools (MCP)](help:mcp)

## Where things live

Everything ByteBunker keeps (sessions, the trace log, usage, settings) is in one folder on this machine, shown in [Settings](#settings). Deleting the app does not delete it.
