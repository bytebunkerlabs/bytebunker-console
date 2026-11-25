---
id: jobs
title: Jobs
screen: jobs
order: 70
summary: The model does a task on a schedule, with no one watching
---
A job is a prompt (or a goal for the agents, or a [workflow](help:workflows)) on a schedule: a cron expression like `0 9 * * mon-fri`, or every N minutes.

## What a run does

Each run is one turn on the model, with your tools and skills, and its result lands in the job's history: the answer, how long it took, the tokens. **run now** runs it at once; `bb jobs run JOB` does too, and shows it as it happens.

- Jobs run one at a time unless you raise `jobs_parallel` in `config.json`; a job waits for its turn instead of being skipped.
- If a job is still running when its next minute comes, that minute is recorded as **missed**.
- A job never asks before a tool runs: a tool that would ask is refused, and the model is told why. Allow it in the job's profile to use it. → [Profiles](help:profiles)
- Deleting a job deletes its history.

Agents can file jobs too: a goal that needs doing every morning becomes a job, marked **filed by agents**.

A job can also name one of your [agents](help:agents). Firing it starts nothing by itself: it writes a line in that agent's inbox, and the agent runs it with its memory, its rules and its models, and reports on its channel. An agent's own schedule (the cron and interval entries of its wake list) shows here as such jobs, and editing the agent changes them. **Run now** does what the schedule would.
