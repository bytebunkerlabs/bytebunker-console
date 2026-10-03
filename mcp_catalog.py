"""A catalog of MCP servers the console can add in one click.

Every entry is a local stdio server (the console starts it as a subprocess;
nothing is hosted elsewhere), with the exact command, the parameters and
environment it needs, and the commands that pre-install it so first use is
not a download. `status` is honest about upstream: some reference servers
were archived by the MCP project but still install and run from npm.
"""

from __future__ import annotations

import os
import shutil

CATALOG = [
    {
        "id": "playwright", "name": "Playwright", "status": "official (Microsoft)",
        "description": "Drive a real browser: navigate, click, fill, read the page as an accessibility snapshot, screenshots. Headless by default.",
        "runtime": "node", "command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless"],
        "params": [], "env": [],
        "install": ["npm install -g @playwright/mcp@latest", "npx -y playwright@latest install chromium"],
        "install_note": "installs the package globally and the Chromium build (~170 MB) into ~/Library/Caches/ms-playwright",
        "docs": "https://github.com/microsoft/playwright-mcp",
    },
    {
        "id": "filesystem", "name": "Filesystem", "status": "reference (MCP project)",
        "description": "Read, write, search and move files under one root directory.",
        "runtime": "node", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "{root}"],
        "params": [{"name": "root", "label": "root directory", "default": "~/rack", "help": "the only tree the model may touch"}],
        "env": [], "install": ["npm install -g @modelcontextprotocol/server-filesystem"],
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    },
    {
        "id": "fetch", "name": "Fetch", "status": "reference (MCP project)",
        "description": "Fetch a URL and return it as markdown, with pagination for long pages.",
        "runtime": "uv", "command": "uvx", "args": ["mcp-server-fetch"], "params": [], "env": [],
        "install": ["uv tool install mcp-server-fetch"],
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
    },
    {
        "id": "git", "name": "Git", "status": "reference (MCP project)",
        "description": "Status, diff, log, commit, branch operations on one repository.",
        "runtime": "uv", "command": "uvx", "args": ["mcp-server-git", "--repository", "{repo}"],
        "params": [{"name": "repo", "label": "repository path", "default": "~/rack", "help": "must be a git repository on the console host, or the server exits at start"}],
        "env": [], "install": ["uv tool install mcp-server-git"],
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/git",
    },
    {
        "id": "memory", "name": "Memory", "status": "reference (MCP project)",
        "description": "A persistent knowledge graph the model can add entities and relations to across chats.",
        "runtime": "node", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"], "params": [],
        "env": [{"name": "MEMORY_FILE_PATH", "label": "memory file", "default": "~/bytebunker-console/data/memory.jsonl", "secret": False}],
        "install": ["npm install -g @modelcontextprotocol/server-memory"],
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
    },
    {
        "id": "sequential-thinking", "name": "Sequential thinking", "status": "reference (MCP project)",
        "description": "A scratchpad tool that lets the model work a problem in explicit, revisable steps.",
        "runtime": "node", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"], "params": [], "env": [],
        "install": ["npm install -g @modelcontextprotocol/server-sequential-thinking"],
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
    },
    {
        "id": "time", "name": "Time", "status": "reference (MCP project)",
        "description": "Current time and timezone conversions.",
        "runtime": "uv", "command": "uvx", "args": ["mcp-server-time", "--local-timezone", "{tz}"],
        "params": [{"name": "tz", "label": "local timezone", "default": "America/New_York", "help": "IANA name"}],
        "env": [], "install": ["uv tool install mcp-server-time"],
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/time",
    },
    {
        "id": "sqlite", "name": "SQLite", "status": "archived upstream, still installs",
        "description": "Query and modify one SQLite database file.",
        "runtime": "uv", "command": "uvx", "args": ["mcp-server-sqlite", "--db-path", "{db}"],
        "params": [{"name": "db", "label": "database file", "default": "~/bytebunker-console/data/notes.db", "help": "created if missing"}],
        "env": [], "install": ["uv tool install mcp-server-sqlite"],
        "docs": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/sqlite",
    },
    {
        "id": "github", "name": "GitHub", "status": "archived upstream (the official server is now a Go binary), still installs",
        "description": "Repos, issues, pull requests, file contents through the GitHub API.",
        "runtime": "node", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "params": [],
        "env": [{"name": "GITHUB_PERSONAL_ACCESS_TOKEN", "label": "GitHub token", "default": "", "secret": True}],
        "install": ["npm install -g @modelcontextprotocol/server-github"],
        "docs": "https://github.com/github/github-mcp-server",
    },
    {
        "id": "brave-search", "name": "Brave Search", "status": "official (Brave)",
        "description": "Web, news, image and local search through the Brave Search API.",
        "runtime": "node", "command": "npx", "args": ["-y", "@brave/brave-search-mcp-server"], "params": [],
        "env": [{"name": "BRAVE_API_KEY", "label": "Brave API key", "default": "", "secret": True}],
        "install": ["npm install -g @brave/brave-search-mcp-server"],
        "docs": "https://github.com/brave/brave-search-mcp-server",
    },
    {
        "id": "context7", "name": "Context7", "status": "official (Upstash)",
        "description": "Up-to-date library documentation and code examples for the model to cite.",
        "runtime": "node", "command": "npx", "args": ["-y", "@upstash/context7-mcp"], "params": [],
        "env": [{"name": "CONTEXT7_API_KEY", "label": "Context7 API key (optional, higher limits)", "default": "", "secret": True}],
        "install": ["npm install -g @upstash/context7-mcp"],
        "docs": "https://github.com/upstash/context7",
    },
    {
        "id": "puppeteer", "name": "Puppeteer", "status": "archived upstream, still installs",
        "description": "Browser automation through Puppeteer. Prefer Playwright above; this is here for scripts that expect it.",
        "runtime": "node", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-puppeteer"], "params": [], "env": [],
        "install": ["npm install -g @modelcontextprotocol/server-puppeteer"],
        "docs": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/puppeteer",
    },
    {
        "id": "postgres", "name": "PostgreSQL", "status": "archived upstream, still installs",
        "description": "Read-only SQL against a PostgreSQL database with schema inspection.",
        "runtime": "node", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-postgres", "{url}"],
        "params": [{"name": "url", "label": "connection URL", "default": "postgresql://user:pass@127.0.0.1:5432/db", "help": "stored in config.json"}],
        "env": [], "install": ["npm install -g @modelcontextprotocol/server-postgres"],
        "docs": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/postgres",
    },
    {
        "id": "slack", "name": "Slack", "status": "archived upstream, still installs",
        "description": "List channels, post and read messages, react, through a Slack bot token.",
        "runtime": "node", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-slack"], "params": [],
        "env": [{"name": "SLACK_BOT_TOKEN", "label": "bot token (xoxb-…)", "default": "", "secret": True},
                {"name": "SLACK_TEAM_ID", "label": "team id (T…)", "default": "", "secret": False}],
        "install": ["npm install -g @modelcontextprotocol/server-slack"],
        "docs": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/slack",
    },
    {
        "id": "prometheus", "name": "Prometheus", "status": "community (pab1it0)",
        "description": "PromQL queries and metric discovery against the rack's Prometheus, through the console's tunnel.",
        "runtime": "uv", "command": "uvx", "args": ["prometheus-mcp-server"], "params": [],
        "env": [{"name": "PROMETHEUS_URL", "label": "Prometheus URL", "default": "http://127.0.0.1:19090", "secret": False}],
        "install": ["uv tool install prometheus-mcp-server"],
        "docs": "https://github.com/pab1it0/prometheus-mcp-server",
    },
    {
        "id": "everything", "name": "Everything (test server)", "status": "reference (MCP project)",
        "description": "Exercises every MCP feature: echo, add, long-running ops, sampling. For testing the client, not for work.",
        "runtime": "node", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"], "params": [], "env": [],
        "install": ["npm install -g @modelcontextprotocol/server-everything"],
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/everything",
    },
    {
        "id": "jobs", "name": "Jobs (built-in)", "status": "built-in",
        "description": "Let the model in the Playground file, list, run and delete the console's scheduled jobs — 'every morning summarise X' becomes a job on the Jobs screen.",
        "runtime": "python", "command": "python3", "args": ["mcp_jobs.py"],
        "params": [], "env": [], "install": [],
        "docs": "",
    },
    {
        "id": "terminal", "name": "Terminal (built-in)", "status": "built-in",
        "description": "The console's own shell tool: runs commands in one working directory, cwd persists between calls.",
        "runtime": "python", "command": "python3", "args": ["mcp_terminal.py", "{root}"],
        "params": [{"name": "root", "label": "working directory", "default": "~/rack", "help": ""}],
        "env": [], "install": [],
        "docs": "",
    },
]

_BY_ID = {c["id"]: c for c in CATALOG}

EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"),
              os.path.expanduser("~/.npm-global/bin")]


def shell_env():
    """launchd gives the console a bare PATH; servers and installs need the
    same roots the MCP host searches."""
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + ":" + ":".join(EXTRA_PATH)
    return env


def runtimes():
    env = shell_env()
    return {t: bool(shutil.which(t, path=env["PATH"])) for t in ("node", "npm", "npx", "uv", "uvx", "python3")}


def get(cid):
    return _BY_ID.get(cid)


def render(entry, params):
    """Fill {placeholders} in args and env defaults from the submitted params."""
    p = {x["name"]: str(params.get(x["name"]) or x.get("default") or "") for x in entry.get("params", [])}
    args = [a.format(**p) if "{" in a else a for a in entry["args"]]
    return entry["command"], args


def catalog_view(configured):
    """The catalog with an `installed_as` hint: which configured server, if
    any, runs this entry (matched by package token in its args)."""
    out = []
    for c in CATALOG:
        token = next((a for a in c["args"] if a.startswith(("@", "mcp-server", "prometheus-mcp", "mcp_terminal"))), None)
        installed_as = None
        for name, spec in (configured or {}).items():
            if not isinstance(spec, dict):
                continue
            if token and token in (spec.get("args") or []):
                installed_as = name
                break
            if name == c["id"] and spec.get("command") == c["command"]:
                installed_as = name
                break
        d = {k: v for k, v in c.items()}
        d["installed_as"] = installed_as
        d["requires"] = c["runtime"]
        out.append(d)
    return out
