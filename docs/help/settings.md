---
id: settings
title: Settings and your data
screen: settings
order: 130
summary: Where everything lives, editing config.json by hand, profiles, bb
---
## Your data

Everything ByteBunker keeps is in one folder, shown at the top of [Settings](#settings): `config.json`, sessions, the trace log, usage, attachments, skills you made. **Export everything** writes it as JSONL.

## Editing config.json by hand

You can. ByteBunker picks up an edit made while it runs before its next change, so its save never undoes yours. If a file does not parse, it is never overwritten unseen: it is kept beside the new one (`config.json.replaced-…` or `.unreadable-…`), and a banner says so. An older config is upgraded once on start, with the original kept as `config.json.before-v2-…`.

## Also here

- **Profiles**: named turn settings. → [Profiles](help:profiles)
- **Chats**: run the Playground's turns on the server. → [The Playground](help:playground)
- **Command line**: install bb, and keep the server running in the background. → [bb](help:cli)
