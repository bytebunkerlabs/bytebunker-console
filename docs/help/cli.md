---
id: cli
title: bb, the command line
order: 50
summary: The same app in a terminal, with tools that work in your folder
---
`bb` talks to this same ByteBunker: what you ask in a terminal shows up in [Sessions](#sessions) as it happens, with the same models, profiles, workflows, trace log and usage.

## Install it

In [Settings](#settings), **Command line**, press **Install command-line tool**. It writes `bb` and `bytebunker` (the same command) to your user's bin folder.

::: mac
The folder is `~/.local/bin`. If `bb` is not found afterwards, add it to your PATH:

```
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```
:::
::: linux
The folder is `~/.local/bin`; most distributions already have it on the PATH. If not:

```
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```
:::
::: windows
The folder is `%LOCALAPPDATA%\ByteBunker\bin`. Add it to your PATH in **System settings, Environment Variables**, then open a new terminal.
:::

If another program already owns `bb`, only `bytebunker` is written.

## Use it

```
bb                                  chat here; /help for the slash commands
bb "what does this error mean?"     one answer
git diff | bb "review this"         piped input is attached
bb ask -p Fast -e off "…"           a profile, an effort level
bb ask --json "…"                   for scripts
bb run review -p file=app.py        a workflow
bb sessions ls                      recent sessions, the app's and bb's
bb agents "goal"                    give the agents a goal and follow it
bb doctor                           what is reachable, and what to do if not
```

## Tools in your folder

In a terminal, the model can work in the folder you started bb in: read, search, list, write and edit files, and run commands, as you, with your environment. Reading never asks. Writing, editing and running ask first:

```
allow ws__write {"path": "notes.md", …}? [y]es, [n]o, [a]lways:
```

`--yes` allows them for one turn. In a script with nobody to ask, a call that would ask stops the turn and bb exits with code 4.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | the model or the run failed |
| 2 | a mistake in the command |
| 3 | stopped (Ctrl-C) |
| 4 | a tool needed approval and nobody could give it |
| 5 | no ByteBunker server |

Ctrl-C stops the turn on the server and keeps what came. bb starts the server when it is not running; a server bb started leaves after 30 idle minutes unless **Keep the server running** is on in Settings.
