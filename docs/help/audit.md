---
id: audit
title: Audit log
screen: audit
order: 125
summary: Every change made in ByteBunker, who made it, when, and whether it worked
---
The [Audit log](#audit) keeps the changes and the decisions, not the conversations (those are in [Sessions](#sessions) and the trace log). One line each:

- **Access**: a chat's access changed to Local, Web or Full access; every terminal command that ran outside the sandbox, with the command and why it could (full access, allowed once, allowed every time); every command you refused; tool approvals.
- **Settings**: settings, profiles, updates installed, the `bb` command, WSL2 setup.
- **Tools**: MCP servers, skills and plugins added, changed or removed.
- **Serving**: gateways, monitors, machines, pairing, deploys, and how each ended.
- **Agents**: goals started and how they ended, the court, where agents work.
- **Runs**: jobs and workflows saved, run or stopped.
- **Data**: chats deleted, projects, exports.

Each line says who did it: **app** (this window), **cli** (`bb`), or the job or run that did it. A green dot means it worked, red that it failed or was refused. Click a line to see everything it recorded.

Secrets never go in: keys, tokens, passwords and every value of an MCP server's environment are written as `***`.

## Verify and export

Each line carries a fingerprint of the line before it, so a line that was edited, removed or moved breaks the chain from there on. **Verify** checks the whole log and says where, if anywhere, it was changed. It shows tampering; it cannot prevent someone who can write the file from rewriting all of it.

**Export** downloads the whole log as JSON lines, exactly as written. The file is `audit/audit.jsonl` in ByteBunker's data folder; the app never deletes or trims it.
