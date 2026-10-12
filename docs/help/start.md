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
3. **Add your machines.** If you serve models with dgx-serve, **Add a machine** on [Deploy](#recipes) pairs a machine over ssh: its engine comes in as a gateway and its monitor on [Cluster](#cluster). → [Serve a model](help:recipes)

The checklist on the Help home measures each of these and links to the fix.

## Then, when you want them

- **bb**, the same app in a terminal: chats, agents, jobs and workflows from your shell, visible here as they happen. → [bb, the command line](help:cli)
- **Workflows**: a prompt you run again, with blanks to fill. → [Workflows](help:workflows)
- **Jobs**: the model does a task on a schedule. → [Jobs](help:jobs)
- **Agents**: a goal for a team of agents on a separate worker. → [Agents](help:agents)
- **Tools**: let the model read files, search, or run commands. → [Tools (MCP)](help:mcp)

## Finding your way

- **Models** in the sidebar holds three tabs: Models, Deploy and Gateways. **Tools** holds MCP servers, Skills and Plugins.
- Press **Cmd-K** on a Mac, or **Ctrl-K** elsewhere, to jump to any screen, guide or action by typing part of its name.
- The Playground keeps the controls most people use in view. Temperature, top-p, seed, JSON mode and the rest are under **Advanced**, which says "changed" when any of them differs from the default.

## Where things live

Everything ByteBunker keeps (sessions, the trace log, usage, settings) is in one folder on this machine, shown in [Settings](#settings). Deleting the app does not delete it.

## More from ByteBunker Labs

The website is [bytebunkerlabs.ai](https://bytebunkerlabs.ai). These guides and more are online at [docs.bytebunkerlabs.ai](https://docs.bytebunkerlabs.ai). Questions, bugs, or a recipe for a new model: [support@bytebunkerlabs.ai](mailto:support@bytebunkerlabs.ai).
