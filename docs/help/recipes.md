---
id: recipes
title: Serve a model
screen: recipes
order: 140
summary: Pair a machine running dgx-serve, then serve models on it from here
---
dgx-serve runs models on your machines: a DGX Spark (one, or several in tensor parallel), a Linux box with an NVIDIA GPU, a Windows PC through WSL2, or an Apple Silicon Mac. ByteBunker drives each one as a **rack**.

## Set up the machine

On the machine that will serve, once:

```
rack init
rack monitor up
```

## Add a rack

On [Recipes](#recipes), press **Add a rack** and give the machine as you reach it with ssh: `you@host`, or a host from your `~/.ssh/config`. For a cluster, give the head.

ByteBunker asks it once, with your own ssh. dgx-serve there adds this app's own ssh key, allowed to run `rack` and nothing else (no shell, no forwarding), and says where its engine and its monitor are. ByteBunker checks which address answers from here and shows what it found. Tick what to add:

- **Chat with what it serves:** its engine becomes a [gateway](help:gateways), with its key.
- **Show it on Cluster:** its [monitor](help:cluster), with its token.

From then on ByteBunker reaches the rack with its own key only. Nothing is copied by hand.

> **Note:** if your ssh to the machine asks for a password, ByteBunker cannot type it. It shows one command to run on the machine (it adds the app's key); run it there, then pair again.

## Serve and stop

Pick a recipe and press **Read recipe** to see what it runs, then **rack up**: whatever the rack served before stops. **rack down** stops serving on every node. A deploy goes on when you leave the screen; **status** and **logs** show where it is.

From a terminal:

```
bb pair you@host            add a rack
bb deploy ls                its recipes; * marks what it serves
bb deploy up qwen3-8b       serve one (it asks first; --yes skips that)
bb deploy logs              the engine's log
```

## Remove a rack

**Remove** forgets the rack and takes this app's key back from it. What it serves keeps running. You choose whether the gateway and the monitor pairing added go too.
