---
id: profiles
title: Profiles
order: 65
summary: Named turn settings: effort, answer length, model, instructions, tool rules
---
A profile is a set of turn settings with a name: `bb -p Fast`, a workflow's profile, a job's. The settings a turn names itself win over its profile's.

| Profile | What it does |
|---|---|
| Default | the model's own defaults |
| Fast | no thinking, shorter answers, at most 8 tool hops |
| Deep | the most thinking the model offers, long answers |

Make your own, or change these, in [Settings](#settings), **Profiles**. **Make default** applies one to every turn that does not name another; **reset** puts a changed built-in back.

## Tool rules

One rule per line, `pattern = allow`, `ask` or `deny`:

```
fs__read_* = allow
terminal__run = ask
web__* = deny
```

Without a rule, a tool that says it only reads runs, and anything that may change something asks. A [job](help:jobs) never asks: a tool that would ask is refused there, and the model is told why.
