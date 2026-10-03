# ByteBunker Desktop — implementation plan

Hand-off document for the engineer or agent that turns the ByteBunker console into a standalone desktop application for macOS and Windows that finds, connects to and uses models on the user's machines and rack. Written 2026-10-03 from the live deployment. Read `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md` and `docs/RUNBOOK.md` first; this plan assumes them.

## 0. The ask, in the owner's words

- "A proper standalone application that just works" on Mac and Windows: install it, open it, it finds the models it can reach and runs them.
- Multiple gateways, like the rack already has: a litellm router in front of the DGX Sparks, a vLLM on an RTX 2070 in a Windows/WSL2 box, and anything `rack` (dgx-spark-serve) brings up. Adding a gateway should be "an IP and a port".
- A **Gateways** tab.
- Usage accounting that still works when models come from several gateways.
- Model deployment **recipes stay out of scope** for the app: the Sparks are served by `rack` and that stays manual. Anything already serving on a host should be auto-detected.

## 1. Where the code stands today

| Piece | State | Where |
|---|---|---|
| Console (server + UI) | shipped, running on hermes as a launchd service | `bytebunkerlabs/bytebunker-console` main @ `f3ebc66`; deployed copy on hermes `~/bytebunker-console` (a file copy, not a checkout) |
| Multi-gateway slice | **implemented, committed, NOT deployed, NOT run against the rack** | branch `feat/gateways` @ `9cf054a` (files: `gateways.py`, `server.py`, `public/index.html`, `public/console.js`) |
| Harness (agents) | shipped, on the worker | `bytebunkerlabs/bytebunker-harness` main @ `d33a12e` (private) |
| Desktop shell, packaging, installers | not started | — |

Working checkouts on the owner's laptop: `~/bb/bytebunker-console` and `~/bb/bytebunker-harness` (use these; the copies under `~/Documents/AI/` intermittently return EPERM to every reader for minutes, which is a macOS/iCloud effect, not a repo problem). Deploy to hermes with `scp` from `~/bb`; never restart the console while an agent run streams (check `ssh hermes 'ssh agents-worker "pgrep -fc \"python scripts/run_maste[r].py\""'` is 0).

### What the gateways branch contains (review before building on it)

`gateways.py`
- `normalize(cfg)`: `cfg["gateways"]` is a list of `{name, url, key, enabled, kind}`; a console with only the old `upstream_url` gets one gateway named `upstream`; the first enabled gateway is mirrored back into `upstream_url`/`upstream_key` for code that still reads them (`engine_stats`, `netcheck`).
- `Registry(cfg, caps_for)`: `refresh(force)` probes every enabled gateway's `/v1/models` concurrently (TTL 60 s) → `models` (each with `gateway`, `caps`, `also_on` for duplicates), `model_map` (id → gateway), `status` (ok, count, ms, kind, error). `resolve(model)` maps `id` or `id@gateway` to `(id, gateway)`; unknown ids fall back to the first enabled gateway (the single-upstream behaviour).
- `detect_kind(url)`: ollama (`/api/tags`), vllm (`/metrics` has `vllm:`), litellm (`/health/liveliness`), lmstudio/llama.cpp heuristics, port hint fallback.
- `discover(cfg, extra_hosts, scan_lan, include_tailnet)`: hosts = localhost + hosts of known gateways + extra + tailnet peers (`tailscale status --json`, CLI paths for mac/win/linux) + optionally the local /24; ports `11434 1234 4000 8000 8001 8080 8888 5000 3000`; TCP connect (0.5 s, 96 threads) then `GET /v1/models` (3 s); 401/403 → `needs_key`.

`server.py` (on the branch)
- `GW = gwmod.Registry(CFG, caps_for)`; `upstream_request(path, payload, method, model=None)` routes by the payload's model (rewrites `id@gw` to `id` in place); `/api/models` returns the merged list + `gateways` status; `GET/POST /api/gateways` (add by `host:port`, update key/name/kind, toggle, remove with last-enabled guard, refresh, discover); usage events get `gateway`; `usage_summary()` adds `by_gateway` (chat in+out plus agent tokens mapped by model); `/api/config` lists gateway names.
- UI: Gateways screen (Serving nav), Models screen shows "via <gateway> · kind · vision", model picker labels models with their gateway when more than one gateway is configured, Usage screen has "By gateway".

Known gaps on the branch (do these first):
1. It has never been run. Deploy to hermes and verify: migration creates the `upstream` gateway from the existing config; `POST /api/gateways {action:add, url:"172.16.25.83:8001"}` adds the 2070 vLLM; `/api/models?refresh=1` shows `qwen3-4b-fast` via the litellm gateway with `also_on` the direct one; `discover` finds spark-1 :4000/:8888 and the worker :8001 over the tailnet; `/api/usage` shows `by_gateway`.
2. `jobs.py`'s runner falls back to `upstream_request("/models")` for a model: switch it to `GW.models`.
3. `engine_stats()` / `netcheck()` still read `CFG["upstream_url"]`; fine while the first gateway is litellm, wrong otherwise. Make the Cluster "Is it local?" card per gateway kind (vLLM only) or drop it in the app.
4. Discovery probes plain HTTP only; add `https://` for 443/8443 targets (Tailscale Serve endpoints).

## 2. Target architecture

```
ByteBunker.app / ByteBunker.exe
└─ desktop/app.py            native window (pywebview) + lifecycle
   └─ server.py (in-process) the same stdlib HTTP server, bound to 127.0.0.1:<free port>
      ├─ gateways.py         registry · routing · discovery
      ├─ jobs.py             scheduler (runs while the app is open; see 4.6)
      ├─ mcp.py              MCP host; built-in servers run via `ByteBunker --mcp <script>`
      ├─ agents.py           harness launcher over ssh (unchanged) or local podman/docker
      └─ public/             UI (unchanged, plus Gateways screen and first-run flow)
user data: ~/Library/Application Support/ByteBunker  |  %APPDATA%\ByteBunker  |  ~/.config/bytebunker
   config.json · data/{traces,usage.jsonl,sessions,uploads,jobs.json,jobs/} · skills/ · plugins/
```

Principles that must survive the port:
- **Loopback only, no auth.** The server binds 127.0.0.1 on a random free port; the window is the only client. Never add a LAN bind "for convenience".
- **Nothing leaves the machine except to configured gateways** (and to the agent worker over ssh if configured). Discovery probes are local/LAN/tailnet only and explicit.
- **Stdlib Python for the server stays.** The desktop shell adds exactly two third-party packages at build time: `pywebview` and `pyinstaller`.
- **Agents never run where models are served.** The app may run the harness locally only in the explicit "local mode" that already exists (podman/docker present); the default remains a remote worker over ssh.

Why pywebview + PyInstaller rather than Electron or Tauri: the app is Python already; pywebview gives a native WKWebView/WebView2 window in ~40 lines; PyInstaller produces a self-contained `.app`/folder with the Python runtime; no Node or Rust toolchain; one codebase. Cost: builds must run on each OS (GitHub Actions does that), and the first launch of an unsigned app needs the usual Gatekeeper/SmartScreen click-through until signing is set up (§6).

## 3. Phases

### Phase 1 — Gateways (finish the branch) · 1–2 days
1. Deploy `feat/gateways` to hermes behind the checks in §1; fix what breaks; merge to main.
2. Fix the four gaps listed above.
3. Add `kind`-aware extras per gateway: for `vllm`, read `/metrics` (tokens/s, KV %, running/waiting) and show them on the Gateways card (the worker stats script already parses vLLM metrics — reuse `agents.py` `_STATS_PY` parsing logic server-side); for `ollama`, `/api/tags` sizes and `/api/ps` loaded models; for `litellm`, `/model/info` when the key allows.
4. Per-gateway usage: the usage event already carries `gateway`. Add `gateway` to the trace log's chat records (`TRACE.log("chat", …, gateway=…)`), and to agent spawn records by mapping the model at collection time (console side, `agent_usage`).
5. Acceptance: with litellm + the 2070 configured, the Playground lists models from both, routes correctly (check litellm logs vs the vLLM log), Usage shows both gateways, removing the last enabled gateway is refused, `id@gateway` pins a duplicate model to a specific gateway.

### Phase 2 — Desktop shell · 2–3 days
1. `server.py`: make paths overridable — `ROOT` stays the code dir; add `DATA = os.environ.get("BYTEBUNKER_DATA") or ROOT/data`, `CONFIG_PATH = os.environ.get("BYTEBUNKER_CONFIG") or ROOT/config.json`; use them in `load_config`, `save_config`, `TraceLog(DATA)`, `JOBS = JobStore(DATA)`, uploads, sessions. Add `serve(bind, port) -> ThreadingHTTPServer` so the app can start it without argparse; keep `__main__` behaviour.
2. `desktop/app.py`:
   - Resolve the user data dir per OS; create `config.json` from `config.json.example` on first run with `gateways: []`, `uploads_dir` under the data dir, `skills_dirs` pointing at `<data>/skills`.
   - Pick a free port, start the server thread, start the scheduler, open `webview.create_window("ByteBunker", url, width=1380, height=900, min_size=(960, 640))`, `webview.start()`; on close, stop MCP servers (`mcp_host().stop_all()`) and exit.
   - `--mcp <script> [args]` mode: when launched with this flag, run the named built-in MCP script (`mcp_terminal.py`, `mcp_jobs.py`) as a stdio server and exit. In `mcp.py` `start()`, when `getattr(sys, "frozen", False)` and the configured command is `python3`/`python` and `args[0]` is a built-in script, run `[sys.executable, "--mcp", script, *rest]` instead. This is how built-in tools work on a machine with no Python.
   - `--headless` flag: run the server only and print the URL (for the launchd/systemd use the rack has today; `install.sh` keeps working).
3. First-run experience in the UI: when `/api/config` reports zero gateways, the Playground's empty state says so and offers "Find engines" (opens the Gateways screen with discovery pre-run, localhost + tailnet). After the first gateway is added, select the first model automatically.
4. Node/uv runtimes for catalog MCP servers are the user's; the MCP screen already shows which runtimes are present and hides "Install locally" when `npm`/`uv` are missing — verify that path on a clean machine.
5. Acceptance: `python3 desktop/app.py` on the laptop opens a window, discovers the rack's gateways over the tailnet, chats, attaches an image, files a job, and reopens with the same config.

### Phase 3 — Packaging and installers · 2 days
1. `desktop/bytebunker.spec` (PyInstaller): entry `desktop/app.py`, name `ByteBunker`, `--windowed`, onedir; datas: `public/`, `skills/`, `plugins/`, `mcp_terminal.py`, `mcp_jobs.py`, `config.json.example`; hidden imports as PyInstaller reports; an icon (`desktop/icon.icns`, `desktop/icon.ico`).
2. macOS: `pyinstaller desktop/bytebunker.spec` → `dist/ByteBunker.app`; `hdiutil create -volname ByteBunker -srcfolder dist/ByteBunker.app -ov -format UDZO dist/ByteBunker-mac.dmg`.
3. Windows: same spec on a Windows runner → `dist/ByteBunker/ByteBunker.exe`; zip the folder as `ByteBunker-win.zip` (an NSIS or MSIX installer is a later nicety; the zip "just works" and WebView2 is present on Windows 10/11).
4. `.github/workflows/desktop.yml`: on tag `v*` and `workflow_dispatch`; matrix `macos-14`, `windows-latest`; `actions/setup-python@v5` 3.12; `pip install pywebview pyinstaller`; build; `actions/upload-artifact`; on tags `softprops/action-gh-release` with both assets. Public repo → free runners.
5. Smoke test in CI: launch `ByteBunker --headless --port 0` in the background, `curl /api/config` until 200, kill. Catches missing datas/hidden imports on both OSes.
6. Acceptance: a tagged push produces a dmg and a zip; a fresh Mac and a fresh Windows machine open the app with no Python installed, and it reaches a gateway added by IP:port.

### Phase 4 — Agents from the app · 1–2 days
1. The Agents screen already supports a remote worker over ssh and a local mode. In the app, ssh must be available: on macOS it is; on Windows use the built-in OpenSSH client (`C:\Windows\System32\OpenSSH\ssh.exe`) and surface a clear error if absent.
2. Model dropdowns on the Agents screen list the merged model set; the three env overrides already exist in the harness (`BYTEBUNKER_MASTER_MODEL`, `BYTEBUNKER_THINKING_MODEL`, `BYTEBUNKER_SLAVE_MODEL`). The harness itself still talks to one `llm.base_url`: either keep litellm as the agents' router (today's design) or teach `harness/spawn.py`'s LLM proxy to route by model like the console does (port `gateways.Registry` into the harness as `bytebunker_agents/gateways.py`, config `llm.gateways: [...]`). Recommended: the second, so a worker with no litellm still works.
3. Acceptance: from the app, run a goal against a worker; models chosen from two gateways; the run log shows the filed job line when the Sultan schedules one.

### Phase 5 — Polish for "just works" · ongoing
- Keep-alive health: gateway cards show reachability every 15 s; a model that disappears from a gateway is greyed in the picker, not removed from sessions.
- Settings screen (new): data dir, uploads dir, theme, rates for the frontier-equivalent figure, agents worker, Tailscale status, "export everything".
- Auto-update check: read the GitHub release feed, show a banner, never auto-install.
- Crash safety: the server thread wraps `serve_forever` in a restart loop; MCP servers that die are restarted lazily (the host already starts them on first use).

## 4. Detailed design notes

### 4.1 Configuration schema (desktop)
```json
{
  "bind": "127.0.0.1", "port": 0,
  "gateways": [
    {"name": "spark-1 litellm", "url": "http://172.16.25.186:4000/v1", "key": "…", "enabled": true, "kind": "litellm"},
    {"name": "2070 vllm", "url": "http://172.16.25.83:8001/v1", "key": "", "enabled": true, "kind": "vllm"}
  ],
  "model_capabilities": {"…": {"tools": true, "effort": [], "ctk": {}, "strip_reasoning": true, "ctx": 131072, "vision": false}},
  "skills_dirs": ["<data>/skills"], "plugins_dirs": ["<data>/plugins"], "plugins": {},
  "mcp_servers": {"terminal": {"command": "python3", "args": ["mcp_terminal.py", "~/rack"]}, "jobs": {"command": "python3", "args": ["mcp_jobs.py"]}},
  "uploads_dir": "<data>/uploads",
  "agents": {"enabled": false, "ssh": "", "dir": "~/bytebunker-harness", "python": "uv run", "script": "scripts/run_master.py", "master_name": "Sultan", "master_instructions": "", "run_timeout_s": 10800, "master_model": "", "thinking_model": "", "slave_model": ""},
  "prometheus_url": "", "sparkdash_url": "", "sparkdash_open_url": "", "telemetry_source": "",
  "frontier_rates_per_mtok": {"input": 3.0, "output": 15.0}
}
```
`upstream_url`/`upstream_key` are derived, never edited by the user in the app.

### 4.2 Routing rules
- A request names a model. `GW.resolve(model)` → the gateway that listed it; `id@gateway` pins; unknown ids go to the first enabled gateway.
- Capabilities (`caps_for`) are by model id substring, independent of gateway; add a per-gateway `caps` override later if two gateways serve the same id with different settings.
- The trace log records `gateway` on every chat record; the export (`/api/export`) carries it through.

### 4.3 Usage across gateways
- Chat: `usage.jsonl` events carry `gateway`; `by_gateway` sums in+out per gateway; `by_model` stays.
- Agents: the worker's ledgers carry model names; the console maps model → gateway at read time (`agent_usage` + `GW.model_map`). When the harness routes by gateway itself (Phase 4.2), record `gateway` in `spawns.jsonl` directly.
- Jobs: chat jobs go through `upstream_json` → routed; record `gateway` in the run record.

### 4.4 Discovery UX
- "Find engines" probes localhost, hosts of known gateways, tailnet peers (via the Tailscale CLI when installed; otherwise skipped with a note), and optionally the LAN /24 (explicit checkbox; ~20 s). Results list url, kind, model names, "needs a key", "already configured"; one click adds.
- A `rack up` on the Sparks is detected the same way: the head's :8888 (vLLM) and :4000 (litellm) both appear; recommend adding litellm (one name space) and note that adding both shows duplicates with `also_on`.

### 4.5 Built-in MCP servers in a frozen app
`mcp.py start()`: if frozen and `spec.command in ("python3","python")` and `args[0]` ∈ {`mcp_terminal.py`,`mcp_jobs.py`} → `[sys.executable, "--mcp", args[0], *args[1:]]`, cwd = the bundle's resource dir (`sys._MEIPASS`). `desktop/app.py` handles `--mcp` before importing pywebview.

### 4.6 Jobs in a desktop app
The scheduler runs only while the app is open. State that on the Jobs screen ("runs while ByteBunker is open"). For always-on schedules, point users at the headless install (`install.sh` on a Mac mini or Linux box, as the rack does today). Optional later: a login item / Windows startup entry that launches `ByteBunker --headless`.

### 4.7 Security review items
- Loopback bind, Host/Origin checks (already in `_guard`), no auth: unchanged. Document that another local user on a shared machine could reach the port.
- Keys are stored in `config.json` in the user data dir (0600 on mac/linux). Offer the OS keychain later (`keyring` would be a third dependency; defer).
- Discovery never sends keys to unknown hosts; it only adds a key when the user types one for a specific endpoint.

## 5. Testing

- Unit: `gateways.normalize` migration; `Registry.resolve` with duplicates and `@` pins; `detect_kind` against recorded responses; `discover` with a fake socket layer. Add `tests/` to the console repo (none exist today; stdlib `unittest`).
- Integration on the rack: §1 checklist; then the RUNBOOK §5 smoke tests.
- Packaging: CI headless smoke on both OSes (§3.5).
- Manual acceptance per phase as listed.

## 6. Decisions the owner must make

1. **Code signing.** macOS notarisation needs an Apple Developer ID ($99/yr) or users right-click → Open on first launch; Windows SmartScreen warns until a signing cert earns reputation. Plan for unsigned first, signed later.
2. **Harness visibility and licence.** The harness is private and unlicensed; the app's Agents feature points at a worker that has it. Publishing the app does not require publishing the harness, but "anyone can install and run agents" does. Decide before Phase 4.
3. **Name and bundle id** (`ai.bytebunker.console` is used by the launchd label today).
4. **Should the app also start local engines** (Ollama/llama.cpp on the laptop)? Out of scope here; discovery will find them if the user starts them.

## 7. Order of work for the implementer

1. Branch `feat/gateways` → deploy, verify, fix, merge (Phase 1.1–1.2).
2. `server.py` path overrides + `serve()` (Phase 2.1). Keep hermes working: `install.sh` and the launchd plist must not change behaviour.
3. `desktop/app.py` + `--mcp` + `--headless` (Phase 2.2–2.3). Run from source on the laptop.
4. PyInstaller spec + local mac build; fix hidden imports; dmg.
5. GitHub Actions matrix; tag `v0.1.0`; test both artifacts on clean machines.
6. Phase 1.3–1.4 and Phase 4 as follow-ups.

Everything above the line is small, deliberate, stdlib-first work; the only genuinely new dependencies are the two build-time packages. The rack keeps running exactly as it does now throughout: the app is another client of the same gateways.
