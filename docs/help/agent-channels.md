---
id: agent-channels
title: Channels and wakes
order: 82
summary: Telegram, mail, feeds, a drop folder and the rack as ways to reach and wake an agent
---
A channel is where you reach an agent from outside the app, and where it reports. A wake is anything that starts it. All of them are set per agent on [Agents](#agents), in **Setup**; connecting Telegram and a mailbox is on the **Channels** card of the Agents tab.

Nothing here listens for connections. The app's own API stays on this computer only, and every channel asks out on its own interval.

## Telegram

One bot, shared by your agents; each chat belongs to one agent.

1. In Telegram, write to @BotFather, send `/newbot`, and copy the token it gives you.
2. On [Agents](#agents) → Channels, paste the token and press **Connect**. It goes into the app's vault, never into its config.
3. From the chat you want to use (your own, or a group you made), send your bot `/start`. The chat appears under Channels: pick an agent and press **Bind**.

From then on a message in that chat is an ask to that agent, and the answer, its questions and its approval requests (with **Yes** and **No**) come back in the same chat. A long answer arrives as a file. Files you send are attached to the ask.

| In the chat | Does |
|---|---|
| `/new` | starts a fresh conversation; its memory stays |
| `/stop` | stops what it is doing |
| `/goal …`, `/chat …` | hands the ask to the court, or answers it in one turn |

A chat that is not bound to an agent is ignored and listed once in the [Audit log](#audit). Bind a chat to **the concierge** for asks to no agent in particular.

Everyone in a group you bind can ask the agent and answer its **Yes** and **No**, so bind only groups whose members you trust. One program listens for a bot: connect the token in this app or in the console service, not in both. With two listeners the Channels card says Telegram reported a conflict.

Telegram is an outside service: what you and the agent say in the chat passes through it. Nothing else does. An agent's files do not leave: only its answer does, a long one as a text file.

## Mail

On the Channels card, **Add a mailbox**: the IMAP host, the user and an app password (kept in the vault). Then give an agent a mail wake in Setup: who may wake it, and what to do with a mail.

```
{"kind": "mail", "from": ["you@example.com", "*@yourcompany.com"], "prompt": "Summarize it and say what needs my attention."}
```

Any mailbox that takes a password over IMAP works: Gmail and iCloud with an app password, Fastmail, your own server. Mailboxes that only accept OAuth sign-in for IMAP (Outlook.com and Microsoft 365 today) are not supported yet. The connection is always encrypted, on port 993 or with STARTTLS on 143, and a server whose certificate is not trusted is refused. A folder's name may have spaces or accents.

The mailbox is only read: nothing is marked or moved, and what was there before the watch began is not a wake. Of a very large mail the first 2 MB are read. A mail from a listed sender becomes one wake within a poll (three minutes). Your prompt is the ask and the mail rides along as data, since anyone can write a From line. A mail from anyone else is ignored and logged.

The answer goes to the agent's channel. `"reply": "thread"` answers the mail itself instead, which needs an SMTP host on the mailbox and asks you first (→ [Rules and approvals](help:agent-rules)). Adding an SMTP host to a saved mailbox, or changing its server, port or user, asks for the password again: the kept one goes only where it was typed for. A note, such as why the agent is waiting, is never mailed: it shows in the app and in the agent's chat.

## Feeds, GitHub and CI

A feed wake asks on its own interval and wakes the agent once per new item. The first look only takes note of what is there.

```
{"kind": "poller", "what": "rss", "url": "https://bytebunkerlabs.ai/feed.xml", "every_min": 30, "prompt": "Does this matter to the rack?"}
{"kind": "poller", "what": "ci", "repo": "owner/name"}
{"kind": "poller", "what": "github"}
```

`rss` reads RSS and Atom; `json` reads a list inside a JSON document (`"path": "data.items"`). `github` is your notifications and `ci` the workflow runs that did not pass; both use the token you connected for the git host under [Plugins](#plugins).

## A drop folder

```
{"kind": "folder", "path": "~/Drop/ops", "prompt": "Read it and file what matters."}
```

A file put in the folder wakes the agent once it has stopped growing. The file stays where you put it; the agent gets a copy, as data from a file.

## The rack

For an agent that watches what serves. The rack monitors on [Cluster](#cluster) are compared with themselves every half minute:

| Event | Means |
|---|---|
| engine_down, engine_up | a node that serves a recipe has no engine that answers (seen twice in a row), or has one again |
| model_starting, model_switched | a model began loading; a node serves a different recipe than before |
| node_down, node_up | a node stopped or resumed answering its monitor |
| disk_high | a disk went over 90% (or the wake's own `disk_pct`) |

```
{"kind": "rack", "events": ["engine_down", "engine_up", "disk_high"], "recipe": "the-recipe-that-should-serve"}
```

On **engine_down** the agent's rack rule is applied at once, before any model is asked: with **allow** (and nothing else running) the rack is switched back to the recipe; with **ask** you get the question on its channel; with **deny** it only reports. What happened, and how long the engine was down, is written to the agent's memory with the monitor as its source, so its next report can say so.

## When something is wrong

The Channels card shows each source's last error: a token Telegram refused, a mailbox that would not open, a feed that did not answer. From a terminal: `bb agents channels`.

An agent runs one wake at a time. A source that wakes it faster than it can run fills its inbox: past 200 waiting wakes, what else arrives from a feed, a mailbox, a folder or the rack is not kept until the inbox clears, and the inbox says so once. Your own asks, from the app, bb or Telegram, are always taken.
