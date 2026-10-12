---
id: court
title: The court
screen: agents
order: 82
summary: The roles your master agent spawns, and how to name them, turn them off or add your own
---
Your master agent (the Sultan, unless you named it otherwise) does not do the work itself: it decides, delegates and puts the answer together. The helpers it spawns are its **court**.

## The built-in court

| Role | What it does | How it works |
|---|---|---|
| `minion` | one well-defined task, fast; many run in parallel | quick, 2,000 tokens, 10 minutes |
| `wazir` | the advisor: hard or ambiguous questions, plans, judges conflicting results | thinks deeply, one at a time |
| `malikah` | the queen: final judgment on the report before you see it; approves it or sends it back | thinks deeply, read-only, one per goal |
| `researcher` | research on the web; every finding cites its source | may use the network |
| `verifier` | a skeptic that audits another helper's answer against its evidence | read-only |
| `internet_agent` | fetches and distills online content | may use the network |
| `coder` | writes and changes code, and runs it to check | in its workspace |
| `analyst` | reads and analyses; changes nothing | read-only |
| `general` | general work: read, write, run commands | in its workspace |
| `khazin` | keeper of the treasury: reads the data you give it and answers from it | never has network; listed once it has access |

The master's model, the thinking model (wazir, malikah) and the doers' model are chosen under **Models** on [Agents](#agents) → Settings.

## Make it yours

On [Agents](#agents) → Settings → **Court**:

- **Name** any role in your own words. The master still spawns it by its id (`researcher`); your name is how it and ByteBunker speak of it.
- **Turn a role off**: the master is told it is not in the court and cannot spawn it.
- **Change its limits**: the longest answer (tokens) and the time limit (minutes).
- **Add a role of your own**: an id, your name for it, which role it starts from (its tools and limits), and what it does, in a sentence the master reads.
- **Workers at once** and **per goal** cap how much runs together.

**Save the court**, and the next run uses it. **Restore the built-in court** forgets your changes. The court travels with each run; nothing on the worker is edited.

> **Note:** the worker's harness needs to know about courts. If Settings says it is older, **Work here** on the Agent plane brings the newer one.

## Agents and your data

A role reads your data only when you give it some, on [Agents](#agents) → Settings → Court → **Access**:

1. **Add a folder**: a name (`sales`) and its path on the worker (`~/data/sales`). A role you give it reads it at `/data/sales`, read-only.
2. **Share an MCP server** you added in Tools → MCP servers. It runs on the worker, so its command must be installed there; its settings and keys travel with each run and never enter a sandbox.
3. On a role's row, tick what it **reads**: folders, a server's tools, and **and changes** only if it may use that server's tools that change things.

The rules, which the app and the harness both enforce:

- **A role holding data never has network**, and a network role can never hold data, so nothing it reads can be sent out. Researchers and minions are offered no data; give it to the khazin, the analyst, or a role of your own without network.
- Tools come through a **broker** on the worker: a role sees only the tools it was given, read-only ones unless you ticked **and changes**, and every call is in the run's trace.
- The same call with the same arguments is answered once per run; each server takes at most four calls at once.

For big jobs the master gives each khazin one chunk (a month, a table, a folder) and runs several at once. A khazin can write a short script that makes many tool calls in one step, puts full results in the goal's shared folder (`/shared`, for helpers without network) and keeps a progress file, so another khazin can carry on if one runs out of time.

## Names, if you want the court's own idiom

| Role | A name from a royal court | Meaning |
|---|---|---|
| `researcher` | alim | a scholar, one who knows |
| `verifier` | qazi | the judge |
| `internet_agent` | qasid | the courier |
| `coder` | mimar | the builder |
| `analyst` | daftardar | the keeper of records |
| `general` | khadim | the servant |
| `khazin` | khazin | the keeper of the treasury |
| a writer of your own | munshi | the secretary, the scribe |
| a scout of your own | jasus | the spy |

Or none of these: call them whatever your team calls them.

## How the master uses the court

How the master chooses among its helpers is written in its **Standing instructions**, on [Agents](#agents) → Settings. Say there, in plain words, when to use whom: "minions for anything with independent parts, the wazir before a hard decision, the queen before every answer to me".
