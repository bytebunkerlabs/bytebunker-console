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
bb -P Billing                       chat in a project
git diff | bb "review this"         piped input is attached
bb ask -p Fast -e off "…"           a profile, an effort level
bb ask --json "…"                   for scripts
bb ask --max-tokens 4096 "…"        a cap on the answer; auto (the default) uses what the window has left
bb --web  /  bb --full-access      this folder's commands may use the network / run with no sandbox (default: sandboxed, no network)
bb run review -p file=app.py        a workflow
bb sessions ls                      recent sessions, the app's and bb's
bb projects                         projects; * marks the one this folder is in
bb pair you@host                    add a rack: its engine and monitor come too
bb deploy up qwen3-8b               serve a recipe on a rack
bb agents "goal"                    give the agents a goal and follow it
bb doctor                           what is reachable, and what to do if not
bb open                             the app in your browser, with a one-time link
```

## Projects

Run bb inside a [project's](help:projects) folder and it joins the project: its chats are kept with the project's, and they follow the project's instructions and profile. `-P name` picks a project from anywhere. In the app, **Open in terminal** starts bb in a project's folder, or continues the chat open in the Playground.

## Racks

`bb pair you@host` does what **Add a rack** does: it asks the machine once with your ssh, and from then on reaches it with this app's own key, allowed to run `rack` and nothing else. `bb racks` lists them; `bb deploy ls | status | up | down | logs` drives one (`-r name` picks which). `up` and `down` ask first; in a script, `--yes` lets them go ahead, and without it bb exits with code 4. → [Serve a model](help:recipes)

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
