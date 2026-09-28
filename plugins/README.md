# Plugins

A plugin is a directory here (or in a dir listed under `plugins_dirs` in
config.json) that bundles skills and MCP servers. The console discovers it,
you enable it on the Plugins screen, and its skills join the catalog while its
MCP servers join the tool host.

Manifest — `plugin.json` (or `.claude-plugin/plugin.json` for a Claude Code
plugin):

```json
{
  "name": "research-kit",
  "description": "one line for the Plugins screen",
  "version": "0.1.0",
  "skills": "skills",
  "mcp_servers": { "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] } }
}
```

- `skills` — a subdirectory of SKILL.md packs (defaults to `skills/` if present).
- `mcp_servers` — inline, or a path to a JSON file, or a `.mcp.json` beside the
  manifest (Claude Code shape: `{ "mcpServers": { ... } }`).
- A bare directory with just a `skills/` subdirectory is a valid plugin with no
  manifest.
