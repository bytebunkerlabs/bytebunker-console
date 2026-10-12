---
id: recipes
title: Serve a model
screen: recipes
order: 140
summary: Serve a model on this computer, or on any machine you add, with dgx-serve
---
dgx-serve runs models on your machines: a DGX Spark (one, or several in tensor parallel), a Linux box with an NVIDIA GPU, a Windows PC through WSL2, or an Apple Silicon Mac. On [Deploy](#recipes) you pick the machine, then a recipe.

## On this computer

With no machine added, [Deploy](#recipes) opens on **This computer**. The first time, press **Install dgx-serve on this computer**: it downloads dgx-serve into `~/dgx-serve`, adds the `rack` command and checks what serving here needs. If something is missing (Apple's command line tools on a Mac, for example), it says what to run.

Then pick a recipe and press **Serve**. When the model answers, ByteBunker adds it as the [gateway](help:gateways) "this computer" by itself, so it shows in the Playground.

> **Note:** on Windows, dgx-serve runs inside WSL2 (Ubuntu 24.04) with the GPU passed through. Install WSL2 first: `wsl --install -d Ubuntu-24.04` in an administrator PowerShell.

## Add a machine

Press **Add a machine** and give it as you reach it with ssh: `you@host`, or a host from your `~/.ssh/config`. For a cluster, give the head. For a Windows PC, give its WSL2 Ubuntu.

ByteBunker asks it once, with your own ssh. dgx-serve there adds this app's own ssh key, allowed to run `rack` and nothing else (no shell, no forwarding), and says where its engine and its monitor are. Tick what to add:

- **Chat with what it serves:** its engine becomes a [gateway](help:gateways), with its key.
- **Show it on Cluster:** its [monitor](help:cluster), with its token.

If dgx-serve is not on that machine yet, ByteBunker offers **Install dgx-serve** there: the same install as on this computer, over your own ssh, then the pairing.

> **Note:** if your ssh to the machine asks for a password, ByteBunker cannot type it. It shows one command to run on the machine (it adds the app's key); run it there, then pair again.

## Serve and stop

Each recipe shows its download size and whether it fits the machine. Recipes for other kinds of machines are folded below. **Read** shows what a recipe runs; **Serve** starts it, and whatever the machine served before stops. **Stop serving** stops it on every node. A deploy goes on when you leave the screen; **Status** and **Logs** show where it is.

From a terminal:

```
bb deploy install           dgx-serve on this computer (--rack you@host: on that machine)
bb pair you@host            add a machine
bb deploy ls                its recipes; * marks what it serves
bb deploy up qwen3-8b       serve one (it asks first; --yes skips that)
bb deploy logs              the engine's log
```

## A model with no recipe

Type the model's name or Hugging Face link next to **Ask for a recipe**. It opens your mail app with a message to support@bytebunkerlabs.ai, filled in with the model and your machine. Nothing is sent until you send it.

## Remove a machine

**Remove** forgets the machine and takes this app's key back from it. What it serves keeps running. You choose whether the gateway and the monitor pairing added go too.
