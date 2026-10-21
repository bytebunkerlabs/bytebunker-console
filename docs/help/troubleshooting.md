---
id: troubleshooting
title: When something says no
order: 200
summary: Common messages, what they mean, and what to do
---
## "context is full"

The prompt does not fit the model's window with room to answer. Type `/compress` to fold older turns into a summary, or start a new chat. → [Long conversations](help:playground)

## "Max tokens clamped"

Not an error: the answer was given less room so the request fits the window.

## "This model is not served with tool support"

The engine was started without tool calling, so the model answered without tools. With dgx-serve, pick a recipe that serves tools.

## "approval needed" (bb exits 4)

A tool asks before it runs and nobody could answer: bb was in a script. Allow it for one turn with `--yes`, or for good with a [profile](help:profiles) rule.

## "the model server closed the stream before the reply finished"

The engine stopped mid-answer: it restarted, ran out of memory, or the network dropped. Ask again; if it keeps happening, look at the engine on [Cluster](#cluster).

## "no gateway serves …" or no models at all

No connected server lists that model. Check [Gateways](#gateways): a red one shows the error it gave. From a terminal:

```
bb doctor
```

## Nothing on the network can be found (Mac)

The app needs **Local Network** access: System Settings, Privacy & Security, Local Network, ByteBunker on.

## "This page needs ByteBunker's access link"

ByteBunker answers only its own window and the `bb` command on this computer, so other accounts on the same machine cannot use it. A browser tab gets in through a one-time link, good for two minutes:

```
bb open
```

A tab left open across a restart of the app needs a new link too. `bb open --print` prints the link instead of opening it.

## The server stopped answering

```
bb doctor
```

says whether a ByteBunker server is running for your data, and starts nothing. `bb serve` runs one in the terminal so you can see what it says.
