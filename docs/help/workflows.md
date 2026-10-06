---
id: workflows
title: Workflows
screen: workflows
order: 60
summary: A prompt you run again, with blanks to fill, from here, a job or bb
---
A workflow is a prompt with blanks, saved under a name you can type. Write `{{name}}` where a value goes:

```
Review this diff for bugs and risky changes. Focus on {{focus}}.

{{diff}}
```

## Run it

- **Here:** pick it on [Workflows](#workflows), fill in the blanks, press **Run**. The answer streams in and is saved as a session.
- **From a terminal:** `bb run review -p focus=security -p diff="$(git diff)"`. bb gives the model its tools in the workflow's folder, or the one you are in.
- **On a schedule:** make a [job](help:jobs) that names the workflow and its values.

A blank with no value and no default is refused before anything runs, with the line that fills it.

## Kinds

- **chat**: one turn, with your tools and skills, under the workflow's profile.
- **agents**: the prompt becomes a goal for the [agents](help:agents).

The **profile** decides the effort, answer length and tool rules. → [Profiles](help:profiles)
