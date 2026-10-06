---
id: sessions
title: Sessions and the trace log
screen: sessions
order: 40
summary: Every conversation, from the app and from bb, and the record behind it
---
Every chat is saved as a session on this machine. Sessions from a terminal are marked **terminal**, with the folder bb ran in. Click one to continue it in the Playground.

## The trace log

Every request to a model, every tool call and every rating is also written to the trace log (one file a day, compressed after the day ends). It is the full record: what was sent, what came back, how long it took. **Export** writes it as JSONL for fine-tuning; **rated up only** keeps the turns you marked good.

## Deleting

Deleting a session removes it from the list and from disk. The trace log keeps the requests that were made; that is its job. To remove everything, delete the data folder shown in [Settings](#settings).
