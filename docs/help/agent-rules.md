---
id: agent-rules
title: Rules and approvals
order: 81
summary: What an agent may do alone, what asks you first, its budget and quiet hours
---
An agent that works while you sleep needs rules it cannot talk itself out of. The rules sit in the app, outside the model, on the **Rules** tab of each agent on [Agents](#agents), or `bb agents rules ops`.

## Classes of action

Every action belongs to a class, and each agent has one policy per class: **allow**, **ask** or **deny**.

| Class | Covers | Default |
|---|---|---|
| read | reading files it was given, fetching allowlisted pages, the monitor | allow |
| write | files in its own home and workspace | allow |
| shell | commands inside its sandbox | allow |
| outward | a message to any channel other than the asking thread; mail; a post; a push | ask |
| rack | rack switch, rack down: anything that changes what is served | ask |
| spend | anything that costs money | deny |
| peers | asking another agent, two hops at most | allow |
| egress | the domains its court, its browser and its fetch tools may reach | none, until you list domains |

```
bb agents rules ops rack=deny outward=ask egress=+developer.nvidia.com
```

Tool policies from [MCP](#mcp) and Settings stay as they are. For a tool an agent uses, the stricter of the tool's own policy and its class wins. A tool that says it only reads is **read**, one that says it stays on this computer is **write**, and one that says nothing of itself is **outward**: it may post, mail or file somewhere. **Always** on a request allows that one tool for that one agent from then on. It wins over a pattern set before it, and you take it back on the Rules tab.

The app's own jobs server files and runs jobs as you, so no agent gets it. An agent schedules its own work with its `schedule_job` tool, which is under these rules.

**rack: allow** has a condition: an agent switches the rack alone only when nothing else is running. With a chat, a goal or a deploy going, it asks.

**In a goal** the rules hold as they do in a turn. What the court's master does itself (keep a fact, ask another agent, file a job) goes through them. A goal does not wait for you: what asks is asked beside it and done on a yes, at most five requests at a time, and a request nobody answered ends with the goal. The court's workers run commands and write files inside their sandbox without asking anyone, so an agent whose **shell** or **write** rule is ask or deny does not use the court: it answers in one turn and its answer says why.

## Where an ask shows

In the place the ask came from: Yes and No buttons in the Telegram chat, the prompt in `bb`, and always in the app. The request names the agent, the action, the exact command or message, and the run. The first answer wins; ten minutes of silence is a no. Every answer is one line in the [Audit log](#audit).

## Mail, the web and files are data

Text that arrives from a mail, a feed, a web page or a dropped file reaches the agent marked as data from that source. Your prompt on the wake is the ask; the content is material to work on, never an instruction. A fact it remembers from such a run carries where the run came from.

On top of that, **a wake from mail, the web or a file always asks before anything outward**, whatever the class says. It is on by default, per agent. Answering a mail by mail is an outward action, so it asks first.

That mark stays on everything the run starts:

- **Always** on a request does not cover such a wake. It allowed the tool for your own asks; a message off the asking chat still asks.
- **Another agent it asks** gets the ask as data from nobody you know, and the answer that comes back is no more trusted than the ask was.
- **A job it files** starts switched off, with the reason on the Jobs screen and a note where the agent reports. A job is an instruction to a later run, so you read it first. Switch it on in Jobs if you want it.

## Budget and quiet hours

| Limit | Default | When it is reached |
|---|---|---|
| tokens a day | 2,000,000 | everything waits until midnight, your own asks too |
| runs a day | 12 | what the agent starts itself waits until midnight; your own asks still run |
| minutes a run | 90 | the run is stopped, and says so |
| quiet hours | 23:00 to 07:00 | what it starts itself is held until they end |

One reply of a turn also has an end, and this one is not a setting. An agent's reply is asked to stop at 16,384 tokens: its thinking, its answer and any tool call it writes. A reply that was all thinking is asked for once more with less thinking, when the model has a thinking level to turn down; thinking that goes round in circles, the same block three times or more in a row, is stopped for that while it is still being written. If the second reply has no answer either, you are told why. Work that needs more room than one reply is a goal.

A stopped agent says so once on its channel. Quiet hours do not hold back your own asks or what the rack reports: an engine that goes down at night is exactly what an agent that watches the rack is for.

```
bb agents rules ops runs_day=20 quiet=22:00-06:00
```

## What it can reach

An agent's **egress list** is the complete answer to "where can this agent's work go on the internet". Nothing is listed by default, so by default nothing outside is reachable.

| You list | Its court can reach |
|---|---|
| nothing | nothing: no role of this agent has network, whatever its flag says |
| `developer.nvidia.com` | that name and its subdomains, on ports 80 and 443 |
| `*.github.com` | github.com and everything under it |
| `*` | any public site |

A role in the court gets the network only when both say so: the role has the network flag (→ [The court](help:court)) and the address is on the list. Such a role's sandbox has no network of its own. It gets one door, a proxy on the worker that checks every connection: the name must be listed, and where the name leads must be a public address. A bare IP address, this computer, your own network, a tailnet and a cloud's metadata service are refused whatever is listed, `*` included.

```
bb agents rules ops egress=+developer.nvidia.com      add a domain
bb agents rules ops egress=-developer.nvidia.com      take it off (egress=none: all of them)
bb agents rules scout egress=any                      a research agent: any public site (the same as *)
```

The same list holds in a chat turn. A tool that reaches outside this computer, such as a fetch from an [MCP](#mcp) server you gave the agent, runs without asking for a listed address. For any other address the call counts as an outward action: it asks you first, and the request names the address. Only a value that is one plainly written address is checked against the list. A link inside a sentence or a page, and an address that two programs could read as two different hosts, are treated as off the list, so those calls ask as well. With **outward: deny** it is refused. **Always** on such a request puts the addresses it names on the list (three at most, the ones the request shows), unless the run began with a mail, a feed or a file. An address on your own network cannot be listed, so a call to one asks each time.

The goal you start yourself on the **Master** tab is not an agent's: there the court's network roles work as before.

## Its own computer

An agent's sandboxes are thrown away after each task. What lasts is its **home**, a folder of its own on the worker.

| A role | Has | Does not have |
|---|---|---|
| without network | the home, mounted at `/home/agent`: notes, scripts, what it will want again | any way out |
| with network, or driving the browser | the door to the egress list, and a folder of its own under the home's `downloads/` for what it fetches | the rest of the home, and the goal's shared folder |

That split is one wall: what is kept at home, and whatever a role that read your data wrote down, cannot be read by a role that can reach out. What a network role downloads is there for the others on the next run.

Setup shows the home's size, and **Reset home** empties it. An agent's memory lives in the app and is not part of its home.

With an egress list, and podman or docker and the browser image on the worker, the court also gets a **browser** role: Chromium in a container of its own, with no network but the same door. Its profile is kept in a folder of its own beside the agent's home, so a site it logged into is still logged in on the next run, and no worker can touch it.

## The guard

When ByteBunker Guard is installed, every tool call an agent makes and every message it sends passes it first, judged on the call itself. Until then the hook allows everything; the rules above are what holds.
