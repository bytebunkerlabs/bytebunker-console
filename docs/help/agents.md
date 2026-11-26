---
id: agents
title: Agents
screen: agents
order: 80
summary: Agents that persist: a purpose, rules, a budget and a memory, on your own machines
---
An agent keeps working between your visits. It has a purpose, rules about what it may do alone, a budget, and a memory it reads when it wakes and writes when it learns something. An ask, a schedule, a chat on your phone, a mail, a feed or the rack itself wakes it; it answers where the ask came from. Its models are picked from what is serving at that moment.

Agents has three tabs: **Agents**, for the ones that persist; **Master**, to hand one goal to the court of sandboxed workers and follow it; and **Settings**, for where agents work, the master and the court.

## Make an agent

On [Agents](#agents) press **New agent**: an id (lower-case, used in `bb` and in @mentions), a name, and its purpose in a sentence or two. A schedule is optional. From a terminal:

```
bb agents new ops --purpose "Keeps the rack serving. Reports what changed." --cron "0 7 * * 1-5" --prompt "Morning report: what served overnight, what failed, the disks."
```

An agent is a folder on this computer, under the app's data folder in `agents/<id>/`: `agent.json` (who it is), `memory/` (one file per fact), `inbox.jsonl` (what woke it, and what became of each wake) and `runs/` (its answers). The files are yours to read and edit. Only this app writes them: the worker that runs its goals never holds them.

## Ask it

Open the agent and type in **Ask**, or from a terminal `bb agents ask ops "what served overnight?"`, or in the [Playground](#playground) start a message with `@ops`. The answer comes back in the same place.

A short ask is answered in one turn, in seconds. Work with several parts, or research, it hands to the court on the worker, and that answer arrives later in the same place (minutes to an hour). **Auto** lets the agent decide; **Answer in one turn** and **Hand to the court** force either, as do `/chat` and `/goal` in front of an ask.

One run per agent at a time: a second ask waits its turn and says so. Several agents run at once (three by default), and two goals on the worker.

**Ask anyone** sends an ask to no agent in particular. The concierge, a small built-in agent on the fastest model, answers a short question itself or hands the ask to the agent whose purpose fits, and says which it did.

## What it remembers

The **Memory** tab lists its facts. Each is one file with a type (about you, how to work, the state of the work, where things are) and its source: a web address, a run, or "the owner said". The agent writes facts itself as it works, with the `remember` tool, and drops them with `forget`. You can add, edit and remove them here, in the files, or with `bb agents memory ops add "Reports go out before 07:30."`.

At each wake its standing guidance (the facts about you and about how to work) comes first, then the facts nearest the ask, then the newest. A fact read from a mail, a web page or a file is marked as such, and is never treated as an instruction. Every night the tidy merges facts that say the same thing and lists the ones from untrusted content for your review.

If you make the `agents` folder a git repository, every memory write is a commit.

## What wakes it

In **Setup**, under Channels and wakes:

| Wake | What it is |
|---|---|
| cron, every N minutes | a schedule; it shows on [Jobs](#jobs) as a job that names the agent |
| the rack | a change the rack monitors saw: an engine down or back, a model switched, a disk filling |
| mail | a mail from a sender on its list |
| a feed | an RSS or JSON feed, your GitHub notifications, CI runs that did not pass |
| a folder | a file dropped in a folder on this computer |

Channels, the rack and the rest are in → [Channels and wakes](help:agent-channels). Every one of them asks out; nothing listens for connections.

An agent can also wake itself again (it files a job with `schedule_job`) and ask another agent (the answer comes back as a new wake; a chain of agents asking agents stops at two hops).

## Which model it runs on

Each role (master, thinking, fast) is **auto** unless you pick a model. Auto means the router chooses, each time a run starts, the live model that fits the role best: the most capable one for the master and the thinking roles, the quickest for the fast ones. The card **Models, picked by the router** shows what it would pick now and why, and each model's card (its tier is a guess from its name and recipe; change it there if the guess is wrong).

When the model a role wants is not serving:

| Situation | What happens |
|---|---|
| it is loading | a scheduled goal waits, and says "loading, 6 min so far" on the agent's channel |
| a smaller model is live | a chat ask runs on it, and the answer says so ("on …; … was down") |
| nothing fits but a recipe could | the agent asks to switch the rack; its rule decides whether it may do so alone |
| nothing fits | the wake is kept and tried again later |

A run keeps the models it started with: a switch on the rack does not move a running master.

## Rules and budget

What an agent may do alone, what asks you first and what it may never do are in → [Rules and approvals](help:agent-rules). The short version: anything that reaches outward asks by default, nothing spends money, and nothing outside is reachable until you list domains.

> **Note:** a new agent cannot browse. Its court reaches only the domains on its egress list, and the list starts empty. For an agent that researches on the open web: `bb agents rules scout egress=any`.

## The court behind a goal

A goal, an agent's or one you run by hand, goes to a master that plans it and hands the work to its court of helpers: fast workers, a deep thinker, a judge (→ [The court](help:court)). Each helper runs in its own sandbox with no network unless it is granted: Seatbelt on a Mac and bubblewrap on Linux and WSL2 (both built in or installed for you; no image, and a task starts in a tenth of a second), or a podman container where neither works. ByteBunker starts goals where the agents work and watches. With no worker set up, an agent still answers asks in one turn.

## Choose where agents work

On [Agents](#agents) → Settings → **Agent plane**, every machine your ssh reaches is listed (**Find machines** looks). Pick one and press **Work here**: ByteBunker checks it, brings the agents harness, sets up the lightest sandbox that works there (bubblewrap where it can, else podman or docker with a sandbox image), tests it, and switches agents to it.

Machines that serve models are shown and not offered: agents there would slow the model down. **This computer** is allowed as the exception, each task in its own sandbox (on a Mac, Seatbelt, which macOS already has: nothing to install).

> **Note:** with one machine doing everything, agents compete with your own chats for the same GPU and everything slows down. Use the [Playground](#playground), a [Project](#projects) or `bb` instead.

Connecting a worker by hand (an SSH address and **Test**) is still there, folded, under Settings.

## Run a goal

On the **Master** tab, type the goal and press **Run**. This is the court without an agent around it: no memory, no rules of its own, one answer. It runs on the server: closing or reloading this tab does not stop it, and opening Agents again follows it. **Stop** ends it on the worker. From a terminal: `bb agents "goal"`, `bb agents stop`.

One goal runs at a time from this tab; a second one is refused with the one that is running. Your agents' own goals run beside it, two at once by default.

A goal that ends without an answer says how it ended and why (it timed out, it was stopped, the master failed). A goal on this computer stops cleanly when you quit or restart the app, instead of running on with no one to report to.

## Spread the minions over several machines

Minions are most of a goal's work, and each one spends its time waiting on its model. One machine serving their model runs a few at once; every machine that serves a minion model adds room.

On [Agents](#agents) → Settings, under **Models**:

1. The minions' main model is the **fast** role's ([Settings](#settings), **Roles**); **at once** is how many it runs together (its engine's parallel slots: 4 for the recipes rack ships).
2. **More engines for the minions** lists every other model your gateways serve. Tick the ones to add, with how many each runs at once. The same model on another machine shows with that machine's name.
3. The line under it says where the minions will spread, and how many can run at once.

During a goal each new minion goes to the engine with the most free room, and stays there (its prompt cache stays warm, its model the same). If its engine stops answering (refused, gone quiet mid-answer, overloaded), its calls move to another engine within about 20 seconds and it carries on. The master is told how many minions the engines can run, and plans to that. Each minion shows the engine it ran on.

The master and the thinking roles keep their own models. A remote worker leaves out engines on this computer's loopback, which it cannot reach. Spreading needs the worker's harness from 0.7 (**Update** under Agent plane).
