---
id: plugins
title: Plugins
screen: plugins
order: 105
summary: Bundles of skills and MCP servers, switched on and off together
---
A plugin is a folder with skills and MCP servers that belong together. Switch it on to load both; switch it off to remove both. Installed plugins live in your data folder.

## Add one from a git repo

**Add plugin** takes a git URL or a local folder. A git URL is cloned into your plugins folder; a `plugin.json` at its root names the skills and MCP servers it adds (a bare `skills/` folder is also a valid plugin). This is how you keep your own skills and tools in one place: a private bundle repo you own, with `plugin.json` listing your MCP servers and a `skills/` folder of your packs.

**Public repos** install straight away. **A private repo** opens a short *Connect* step: paste a read-only access token (on GitHub: Settings → Developer settings → Fine-grained tokens, with *Contents: Read-only* on the repo) and the app installs it — a one-click "Connect with GitHub" sign-in is coming. The token is kept in a `secrets.json` file with owner-only permissions, never in `config.json`, never shown again, never written to logs or the audit trail; it is handed to git through an environment-fed helper, never put in the saved repo URL. Once a host is connected it is reused for every repo from it, and syncs. A **Connected:** line on this screen shows which hosts are linked, with a **disconnect**. (SSH `git@…` URLs still work too, using your own key — no token needed.) In [bb](help:cli): `bb git connect <url>` (prompts for the token, hidden), `bb git ls`, `bb git disconnect <host>`.

## Keep it up to date

A plugin cloned from git shows its remote and, after a sync, how many commits it is behind. **Sync from GitHub** on its card runs `git pull` and reloads its skills and MCP servers in place — no remove and re-add. In [bb](help:cli): `bb plugins add <git-url>`, `bb plugins sync <name>` (or `--all`), and `bb plugins enable|disable|remove <name>`.

When [agents](help:agents) are set up, enabling or syncing a bundle also copies its skills to the agent worker, so the court can attach them on the next run. To let agents use a bundle's MCP tools as well, share the server under [Access](help:court) and add its tools to a role.
