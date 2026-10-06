---
id: playground
title: The Playground
screen: playground
order: 30
summary: Chat with a model, with tools, skills, attachments and long conversations
---
Pick a model at the top, type, press Enter. Shift+Enter makes a new line. Stop ends the answer and keeps what came.

## The panel

| Setting | What it does |
|---|---|
| Temperature, top-p, top-k | how adventurous the wording is |
| Max tokens | the longest answer; thinking counts against it |
| Reasoning effort | how long a thinking model thinks. Default sends nothing; Off to Max are mapped to the model's own nearest level |
| Tool hops | how many rounds of tool calls before it pauses |
| Auto-compress | folds older turns into a summary when the window fills |
| JSON mode, seed, stops | for structured or repeatable output |
| System prompt | standing instructions for this chat |

## Tools and skills

With tools on, the model can call the [MCP servers](help:mcp) you added. When chats run on the server (below), a tool that may change something asks first: a card appears with **Allow**, **Always allow** and **Deny**. Attach skills from the [Skills](#skills) screen; their instructions go ahead of your system prompt.

## Attachments

Drop files on the box. Images go to models that read images; text files go inline; anything else is saved and its path given to the model, so a file tool can open it.

## Long conversations

The window is shared by the prompt and the answer. When a conversation nears it, auto-compress writes a summary of the older turns and keeps the originals in the archive. Type `/compress` to do it yourself. If an engine refuses a prompt as too long, its answer names its real window, and ByteBunker believes it from then on.

## Run chats on the server

In [Settings](#settings), **Chats**, you can run the Playground's turns on the ByteBunker server: they keep going when you close the tab, and a turn started from a terminal with [bb](help:cli) in the open session appears here as it streams.
