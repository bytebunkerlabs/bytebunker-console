---
id: projects
title: Projects
screen: projects
order: 35
summary: Related chats and workflows together, with a folder and instructions
---
A project keeps related work in one place: its chats, from the app and from [bb](help:cli), and the workflows that belong to it. Each project has:

| Part | What it does |
|---|---|
| **Folder** | where bb works for it, and where a chat opened in a terminal starts. It is made when it is not there |
| **Instructions** | what every chat in it should know: the codebase, its conventions, what to avoid. They go ahead of each chat's own system prompt |
| **Profile** | what bb, workflows and jobs in it use when they name none. In the Playground, the panel's settings come first |

## Make one

On [Projects](#projects), press **New project**, then give it a name, a folder and its instructions. In the desktop app, **Choose…** picks the folder in the system's own dialog.

## Work in it

- **In the Playground:** pick the project next to the model. New chats start in it, and its instructions go with every turn. Picking another project for a chat that already has messages moves the chat there, or starts a new chat in it.
- **In a terminal:** run `bb` anywhere inside the project's folder and it joins the project. From anywhere else, name it:

```
bb -P Billing                       chat in the project
bb -P Billing "what does this do?"  one answer, in the project
bb projects                         every project; * marks the one this folder is in
```

- **Workflows:** a workflow can belong to a project: what it runs follows the project's instructions and is kept with its chats. → [Workflows](help:workflows)

## Continue in a terminal

**Open in terminal** on a project, or **Terminal** in the Playground, opens bb in a terminal window, in the project's folder. From the Playground it continues the chat open there, starting with its last question and answer. Each turn bb takes shows in the app too, in Sessions and in that chat.

::: mac
It opens in the app that opens `.command` files: Terminal, unless you chose another.
:::
::: windows
It opens in a PowerShell window.
:::
::: linux
A server reached from a browser cannot open a window on your screen, so it shows the command to run on the computer it runs on.
:::

## Keeping it tidy

On [Sessions](#sessions), show one project's chats, or those in none. Deleting a project keeps its chats, without a project, and leaves its folder alone.
