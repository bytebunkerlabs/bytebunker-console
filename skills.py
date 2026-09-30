"""Skills and plugins for the console.

A **skill** is a markdown specialization pack in the Claude Code / bytebunker-
harness format — `<name>/SKILL.md` (bundle) or `<name>.md` (flat) with YAML-ish
frontmatter:

    ---
    name: web-research            # kebab-case, must match the file/dir name
    description: one-liner used in the catalog        # required
    whenToUse: extra routing guidance                 # optional
    tools: [fetch_url, read_file]                      # optional tool allowlist
    network: true                                      # optional
    model: some-model-alias                            # optional model hint
    ---
    Markdown body — the instructions injected into the chat's system prompt.

Same on-disk format as the harness (src/bytebunker_agents/skills), so a single
skills directory feeds both: point `skills_dirs` in config.json at the harness's
`skills/`. Progressive disclosure — the catalog carries name + description only;
the body is reread from disk every load, so editing a skill takes effect at once.

A **plugin** is a directory that contributes skills and MCP servers at once. It
carries a manifest (`plugin.json`, or Claude Code's `.claude-plugin/plugin.json`)
naming what it adds; the console discovers plugins, remembers which are enabled,
folds enabled plugins' skills into the catalog, and hands their MCP servers to
the MCP host. Everything here is stdlib-only, to match the rest of the console.
"""

from __future__ import annotations

import json
import os
import re

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_DESC = 500


# ----------------------------------------------------------- frontmatter --
def split_frontmatter(text):
    """(meta_dict | None, body). None when there is no valid `--- ... ---`
    header. A tiny YAML subset — enough for the flat skill frontmatter, no
    PyYAML dependency so the console stays stdlib-only."""
    if not text.startswith("---"):
        return None, text
    rest = text[3:]
    # first line must be the fence's end-of-line; the header runs to the next
    # line that is exactly "---"
    lines = rest.split("\n")
    if not lines or lines[0].strip() != "":
        # allow "---\n" only; "---foo" is not frontmatter
        if lines and lines[0].strip():
            return None, text
    close = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close = i
            break
    if close is None:
        return None, text
    header = "\n".join(lines[1:close])
    body = "\n".join(lines[close + 1:])
    meta = _parse_yaml_ish(header)
    if not isinstance(meta, dict):
        return None, text
    return meta, body


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    return v


def _parse_yaml_ish(header):
    out = {}
    lines = header.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] in " \t":            # stray indented line without a key
            continue
        if ":" not in raw:
            continue
        key, val = raw.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):     # inline list
            inner = val[1:-1].strip()
            out[key] = [_scalar(x) for x in _split_list(inner)] if inner else []
        elif val == "":                                    # block list?
            items = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(_scalar(lines[i].lstrip()[2:]))
                i += 1
            out[key] = items if items else ""
        else:
            out[key] = _scalar(val)
    return out


def _split_list(inner):
    """Split a,b,"c,d" on commas outside quotes."""
    out, buf, q = [], [], None
    for ch in inner:
        if q:
            if ch == q:
                q = None
            buf.append(ch)
        elif ch in "\"'":
            q = ch
            buf.append(ch)
        elif ch == ",":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


# ----------------------------------------------------------------- skills --
class Skill:
    def __init__(self, name, description, path, source="", when_to_use="",
                 tools=None, network=False, model=None, rank=0):
        self.name = name
        self.description = description
        self.path = path                # the SKILL.md / <name>.md file
        self.source = source            # plugin or dir it came from, for the UI
        self.when_to_use = when_to_use
        self.tools = tools or []
        self.network = network
        self.model = model
        self.rank = rank                # lower wins on duplicate names

    def body(self):
        """Reread from disk every time — edits take effect immediately."""
        try:
            with open(self.path, encoding="utf-8") as f:
                _, body = split_frontmatter(f.read())
            return body.strip()
        except OSError:
            return ""

    def summary(self):
        return {"name": self.name, "description": self.description,
                "whenToUse": self.when_to_use, "tools": self.tools,
                "network": self.network, "model": self.model, "source": self.source,
                "path": self.path}


def _parse_skill(path, source, rank, warnings):
    try:
        with open(path, encoding="utf-8") as f:
            meta, _ = split_frontmatter(f.read())
    except OSError as e:
        warnings.append("%s: unreadable (%s)" % (path, e))
        return None
    if meta is None:
        warnings.append("%s: missing frontmatter" % path)
        return None
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not NAME_RE.match(name):
        warnings.append("%s: invalid skill name %r" % (path, name))
        return None
    if not description:
        warnings.append("%s: missing description" % path)
        return None
    tools = meta.get("tools") or []
    if not isinstance(tools, list):
        tools = [tools]
    return Skill(name=name, description=description[:MAX_DESC], path=path, source=source,
                 when_to_use=str(meta.get("whenToUse") or "").strip(),
                 tools=[str(t) for t in tools], network=bool(meta.get("network")),
                 model=meta.get("model"), rank=rank)


def scan_skills_dir(root, source, rank, cat, warnings):
    if not os.path.isdir(root):
        return
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            f = os.path.join(full, "SKILL.md")
            if os.path.isfile(f):
                _add_skill(cat, _parse_skill(f, source, rank, warnings), entry, warnings)
        elif entry.endswith(".md") and entry != "README.md":
            _add_skill(cat, _parse_skill(full, source, rank, warnings), entry[:-3], warnings)


def _add_skill(cat, skill, expected, warnings):
    if skill is None:
        return
    if skill.name != expected:
        warnings.append("%s: name %r != location %r" % (skill.path, skill.name, expected))
        return
    prev = cat.get(skill.name)
    if prev is not None and prev.rank <= skill.rank:
        return                          # earlier (lower-rank) root shadows this
    cat.skills[skill.name] = skill


class SkillCatalog:
    def __init__(self):
        self.skills = {}
        self.warnings = []

    def get(self, name):
        return self.skills.get(name)

    def names(self):
        return sorted(self.skills)

    def summaries(self):
        return [self.skills[n].summary() for n in self.names()]

    def bodies(self, names):
        return {n: self.skills[n].body() for n in names if n in self.skills}


def load_catalog(roots):
    """roots: list of (dir, source_label). Earlier roots win duplicate names."""
    cat = SkillCatalog()
    for rank, (root, source) in enumerate(roots):
        scan_skills_dir(os.path.expanduser(root), source, rank, cat, cat.warnings)
    return cat


# ---------------------------------------------------------------- plugins --
class Plugin:
    def __init__(self, name, root, manifest, warnings):
        self.name = name
        self.root = root
        self.description = str(manifest.get("description") or "").strip()
        self.version = str(manifest.get("version") or "").strip()
        self.author = manifest.get("author") or ""
        if isinstance(self.author, dict):
            self.author = self.author.get("name") or ""
        # skills dir: explicit, else a conventional skills/ subdir
        sd = manifest.get("skills")
        self.skills_dir = None
        if isinstance(sd, str):
            self.skills_dir = os.path.join(root, sd)
        elif os.path.isdir(os.path.join(root, "skills")):
            self.skills_dir = os.path.join(root, "skills")
        # mcp servers: inline map, or a referenced json file (Claude Code: .mcp.json)
        self.mcp_servers = {}
        m = manifest.get("mcp_servers") or manifest.get("mcpServers")
        if isinstance(m, dict):
            self.mcp_servers = m
        else:
            ref = m if isinstance(m, str) else (".mcp.json" if os.path.isfile(os.path.join(root, ".mcp.json")) else None)
            if ref:
                try:
                    with open(os.path.join(root, ref), encoding="utf-8") as f:
                        doc = json.load(f)
                    got = doc.get("mcpServers") or doc.get("mcp_servers") or doc
                    if isinstance(got, dict):
                        self.mcp_servers = got
                except (OSError, ValueError) as e:
                    warnings.append("%s: bad mcp file (%s)" % (name, e))
        self.warnings = warnings

    def info(self, enabled, catalog=None):
        skills = []
        if self.skills_dir and os.path.isdir(self.skills_dir):
            tmp = SkillCatalog()
            scan_skills_dir(self.skills_dir, self.name, 0, tmp, [])
            skills = tmp.names()
        return {"name": self.name, "description": self.description, "version": self.version,
                "author": self.author, "enabled": enabled, "root": self.root,
                "skills": skills, "mcp_servers": sorted(self.mcp_servers),
                "has_skills": bool(skills), "has_mcp": bool(self.mcp_servers)}


def _plugin_manifest(root):
    for rel in ("plugin.json", "bytebunker-plugin.json", ".claude-plugin/plugin.json"):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return None
    # a bare directory with a skills/ subdir is a valid, manifest-less plugin
    if os.path.isdir(os.path.join(root, "skills")):
        return {}
    return None


def discover_plugins(dirs):
    """dirs: list of directories that each contain plugin subdirectories.
    Returns {name: Plugin}. A malformed plugin is skipped, never fatal."""
    found = {}
    warnings = []
    for d in dirs:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for entry in sorted(os.listdir(d)):
            root = os.path.join(d, entry)
            if not os.path.isdir(root) or entry.startswith("."):
                continue
            manifest = _plugin_manifest(root)
            if manifest is None:
                continue
            name = str(manifest.get("name") or entry).strip()
            if not NAME_RE.match(name):
                warnings.append("%s: invalid plugin name %r" % (root, name))
                continue
            if name not in found:       # first dir wins
                found[name] = Plugin(name, root, manifest, warnings)
    return found, warnings
