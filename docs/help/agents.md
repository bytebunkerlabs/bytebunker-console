---
id: agents
title: Agents
screen: agents
order: 80
summary: A goal for a team of agents, run on a separate worker
---
A master agent plans each goal and hands the work to its court of helpers: fast workers, a deep thinker, a judge (→ [The court](help:court)). Each helper runs in its own sandbox with no network unless it is granted: Seatbelt on a Mac and bubblewrap on Linux and WSL2 (both built in or installed for you; no image, and a task starts in a tenth of a second), or a podman container where neither works. ByteBunker starts goals where the agents work and watches.

Agents has two tabs: **Master**, to run goals and follow them, and **Settings**, for where agents work, the master's models and instructions, and the court.

## Choose where agents work

On [Agents](#agents) → Settings → **Agent plane**, every machine your ssh reaches is listed (**Find machines** looks). Pick one and press **Work here**: ByteBunker checks it, brings the agents harness, sets up the lightest sandbox that works there (bubblewrap where it can, else podman or docker with a sandbox image), tests it, and switches agents to it.

Machines that serve models are shown and not offered: agents there would slow the model down. **This computer** is allowed as the exception, each task in its own sandbox (on a Mac, Seatbelt, which macOS already has: nothing to install).

> **Note:** with one machine doing everything, agents compete with your own chats for the same GPU and everything slows down. Use the [Playground](#playground), a [Project](#projects) or `bb` instead.

Connecting a worker by hand (an SSH address and **Test**) is still there, folded, under Settings.

## Run a goal

Type the goal and press **Run**. It runs on the server: closing or reloading this tab does not stop it, and opening Agents again follows it. **Stop** ends it on the worker. From a terminal: `bb agents "goal"`, `bb agents stop`.

One goal runs at a time; a second one is refused with the one that is running.

A goal that ends without an answer says how it ended and why (it timed out, it was stopped, the master failed). A goal on this computer stops cleanly when you quit or restart the app, instead of running on with no one to report to.

## Spread the minions over several machines

Minions are most of a goal's work, and each one spends its time waiting on its model. One machine serving their model runs a few at once; every machine that serves a minion model adds room.

On [Agents](#agents) → Settings, under **Models**:

1. **doers (minions)** is the main minion model; **at once** is how many it runs together (its engine's parallel slots: 4 for the recipes rack ships).
2. **More engines for the minions** lists every other model your gateways serve. Tick the ones to add, with how many each runs at once. The same model on another machine shows with that machine's name.
3. The line under it says where the minions will spread, and how many can run at once.

During a goal each new minion goes to the engine with the most free room, and stays there (its prompt cache stays warm, its model the same). If its engine stops answering (refused, gone quiet mid-answer, overloaded), its calls move to another engine within about 20 seconds and it carries on. The master is told how many minions the engines can run, and plans to that. Each minion shows the engine it ran on.

The master and the thinking roles keep their own models. A remote worker leaves out engines on this computer's loopback, which it cannot reach. Spreading needs the worker's harness from 0.7 (**Update** under Agent plane).
