---
id: agents
title: Agents
screen: agents
order: 80
summary: A goal for a team of agents, run on a separate worker
---
A master agent plans each goal and hands the work to its court of helpers: fast workers, a deep thinker, a judge (→ [The court](help:court)). Each helper runs in its own sandbox with no network unless it is granted: bubblewrap on Linux and WSL2 (no image; it starts in a tenth of a second), or a podman container elsewhere. ByteBunker starts goals where the agents work and watches.

Agents has two tabs: **Master**, to run goals and follow them, and **Settings**, for where agents work, the master's models and instructions, and the court.

## Choose where agents work

On [Agents](#agents) → Settings → **Agent plane**, every machine your ssh reaches is listed (**Find machines** looks). Pick one and press **Work here**: ByteBunker checks it, brings the agents harness, sets up the lightest sandbox that works there (bubblewrap where it can, else podman or docker with a sandbox image), tests it, and switches agents to it.

Machines that serve models are shown and not offered: agents there would slow the model down. **This computer** is allowed as the exception, each task in its own sandbox (on a Mac, a container inside podman's Linux VM: `brew install podman && podman machine init && podman machine start` first).

> **Note:** with one machine doing everything, agents compete with your own chats for the same GPU and everything slows down. Use the [Playground](#playground), a [Project](#projects) or `bb` instead.

Connecting a worker by hand (an SSH address and **Test**) is still there, folded, under Settings.

## Run a goal

Type the goal and press **Run**. It runs on the server: closing or reloading this tab does not stop it, and opening Agents again follows it. **Stop** ends it on the worker. From a terminal: `bb agents "goal"`, `bb agents stop`.

One goal runs at a time; a second one is refused with the one that is running.
