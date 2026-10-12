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

## Names, if you want the court's own idiom

| Role | A name from a royal court | Meaning |
|---|---|---|
| `researcher` | alim | a scholar, one who knows |
| `verifier` | qazi | the judge |
| `internet_agent` | qasid | the courier |
| `coder` | mimar | the builder |
| `analyst` | daftardar | the keeper of records |
| `general` | khadim | the servant |
| a writer of your own | munshi | the secretary, the scribe |
| a scout of your own | jasus | the spy |

Or none of these: call them whatever your team calls them.

## How the master uses the court

How the master chooses among its helpers is written in its **Standing instructions**, on [Agents](#agents) → Settings. Say there, in plain words, when to use whom: "minions for anything with independent parts, the wazir before a hard decision, the queen before every answer to me".
