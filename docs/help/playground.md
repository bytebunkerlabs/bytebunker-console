---
id: playground
title: The Playground
screen: playground
order: 30
summary: Chat with a model, with tools, skills, attachments and long conversations
---
Pick a model at the top, type, press Enter. Shift+Enter makes a new line. Stop ends the answer and keeps what came.

## The panel

The panel stays open unless you close it with the panel button at the top right; the app remembers which you chose.

| Setting | What it does |
|---|---|
| Max tokens | the longest answer; thinking counts against it. **Auto** (the default) lets the model use whatever its window has left; the hint under it names the window, such as 256k. Or pin a number up to 1M: a request that would not fit is given what fits |
| Reasoning effort | how long a thinking model thinks. Default sends nothing; Off to Max are mapped to the model's own nearest level |
| Tool hops | how many rounds of tool calls before it pauses |
| Auto-compress | folds older turns into a summary when the window fills |
| Advanced | temperature, top-p, top-k, repetition penalty, seed, JSON mode and stop sequences, folded; it says "changed" when any differs from the default |
| System prompt | standing instructions for this chat |

## What a chat's commands can reach

When the model runs terminal commands, the chip next to **Terminal** says what they can reach. You can change it at any point in a chat; the next command uses the new setting.

| | Files | Network |
|---|---|---|
| 🟢 **Local** (every new chat) | the project's folder, writable; your installed tools, readable; never your keys or the rest of your home folder | off |
| 🟡 **Web** | the same | on |
| 🔴 **Full access** | everything, as you: no sandbox | on |

Full access is for this chat only, asks once when you choose it, and shows a red bar. A new chat always starts Local.

When a command really needs more (a `git push` with your keys, a system-wide install), the model asks to run that one command outside the sandbox, and you choose **Allow once**, **Allow every time in this chat** or **Don't allow**. On [Sessions](#sessions), a dot shows the most each chat ever had: green, amber or red.

The sandbox is macOS's Seatbelt or Linux's bubblewrap. On Windows, it is WSL2: until it is set up (Settings → **Command sandbox** → **Enable WSL2**, a restart, then **Finish setup**), every command asks first.

## Tools and skills

With tools on, the model can call the [MCP servers](help:mcp) you added. In chats (they run on the server, below), a tool that may change something asks first: a card appears with **Allow**, **Always allow** and **Deny**. Attach skills from the [Skills](#skills) screen; their instructions go ahead of your system prompt.

## Attachments

Drop files on the box. Images go to models that read images; text files go inline; anything else is saved and its path given to the model, so a file tool can open it.

## Long conversations

The window is shared by the prompt and the answer. When a conversation passes 80% of the model's window (sooner if a pinned Max tokens needs the room, but never more than half the window is held for the answer), auto-compress writes a summary of the older turns and keeps the originals in the archive. Type `/compress` to do it yourself. If an engine refuses a prompt as too long, its answer names its real window, and ByteBunker believes it from then on.

## Projects and the terminal

Next to the model, pick a [project](help:projects): the chat follows its instructions and is kept with its other chats. **Terminal** opens this chat in [bb](help:cli), in a terminal window in the project's folder, where the model can work on your files with your tools. Each turn bb adds shows here too.

## Chats run on the server

The Playground's turns run on the ByteBunker server: they keep going when you close the tab, and a turn started from a terminal with [bb](help:cli) in the open session appears here as it streams. To run them in the tab instead, as before 0.8, turn off [Settings](#settings), **Chats**.
