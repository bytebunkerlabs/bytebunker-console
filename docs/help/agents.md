---
id: agents
title: Agents
screen: agents
order: 80
summary: A goal for a team of agents, run on a separate worker
---
The agents (a master that plans and a court of helpers) run on a separate worker machine, never on the machine that serves the models and never on this one. Each helper runs in its own container with no network unless it is granted. ByteBunker starts goals there over SSH and watches.

## Connect a worker

On [Agents](#agents), enter the worker's SSH address (an alias from `~/.ssh/config`, or `user@host`) and press **Test**. The test checks the harness, the container runtime and the launcher, and says what is missing.

The worker needs: Linux, podman or docker, and the ByteBunker agents harness. Test that this machine can reach it without a password:

```
ssh worker-host true && echo ok
```

## Run a goal

Type the goal and press **Run**. It runs on the server: closing or reloading this tab does not stop it, and opening Agents again follows it. **Stop** ends it on the worker. From a terminal: `bb agents "goal"`, `bb agents stop`.

One goal runs at a time; a second one is refused with the one that is running.
