#!/usr/bin/env bash
# ByteBunker one-command install.
#
#   curl -fsSL https://raw.githubusercontent.com/bytebunkerlabs/bytebunker-console/main/install.sh | bash
#   ./install.sh --upstream http://172.16.25.186:4000/v1 --key sk-... --with-harness
#
# Installs the console (stdlib Python, no pip) as a background service on
# macOS (launchd) or Linux (systemd --user), writes config.json, and can
# place the agent harness next to it so the Agents screen works in local
# mode. Re-running updates in place. Nothing needs sudo.
set -euo pipefail

DIR="${BYTEBUNKER_DIR:-$HOME/bytebunker}"
PORT=8765; BIND=127.0.0.1
UPSTREAM=""; KEY=""
WITH_HARNESS=0; NO_SERVICE=0
CONSOLE_REPO="${CONSOLE_REPO:-https://github.com/bytebunkerlabs/bytebunker-console.git}"
HARNESS_REPO="${HARNESS_REPO:-git@github.com:bytebunkerlabs/bytebunker-harness.git}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --bind) BIND="$2"; shift 2;;
    --upstream) UPSTREAM="$2"; shift 2;;
    --key) KEY="$2"; shift 2;;
    --with-harness) WITH_HARNESS=1; shift;;
    --no-service) NO_SERVICE=1; shift;;
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    *) echo "unknown option: $1"; exit 2;;
  esac
done

say() { printf '\033[1;36m==\033[0m %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "!! need $1"; exit 1; }; }
need git; need python3
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "!! python3 >= 3.10 required (have $PYV)"; exit 1; }

mkdir -p "$DIR"
# ---------------------------------------------------------------- console
if [ -f "$DIR/console/server.py" ]; then
  say "updating console in $DIR/console"
  git -C "$DIR/console" pull --ff-only -q || echo "   (not a clean checkout; left as is)"
elif [ -f "$(dirname "$0")/server.py" ] && [ "$(cd "$(dirname "$0")" && pwd)" != "$DIR/console" ]; then
  say "installing console from this checkout into $DIR/console"
  mkdir -p "$DIR/console"
  (cd "$(dirname "$0")" && tar c --exclude=data --exclude=.git --exclude=config.json .) | (cd "$DIR/console" && tar x)
elif [ -f "$(dirname "$0")/server.py" ]; then
  say "console is this checkout"
else
  say "cloning console into $DIR/console"
  git clone -q "$CONSOLE_REPO" "$DIR/console"
fi
C="$DIR/console"
mkdir -p "$C/data"

# ---------------------------------------------------------------- harness
if [ "$WITH_HARNESS" = 1 ]; then
  if [ -d "$DIR/harness/.git" ]; then
    say "updating harness"; git -C "$DIR/harness" pull --ff-only -q || true
  else
    say "cloning harness into $DIR/harness (needs access to the repo)"
    git clone -q "$HARNESS_REPO" "$DIR/harness" || { echo "!! could not clone the harness; continuing without it"; WITH_HARNESS=0; }
  fi
  if [ "$WITH_HARNESS" = 1 ]; then
    command -v uv >/dev/null 2>&1 || { say "installing uv"; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
    (cd "$DIR/harness" && uv sync -q) || echo "   (uv sync failed; run it by hand in $DIR/harness)"
    [ -f "$DIR/harness/config/config.yaml" ] || cp "$DIR/harness/config/config.example.yaml" "$DIR/harness/config/config.yaml" 2>/dev/null || true
  fi
fi

# ---------------------------------------------------------------- config
python3 - "$C" "$PORT" "$BIND" "$UPSTREAM" "$KEY" "$WITH_HARNESS" "$DIR" <<'PY'
import json, os, sys
c, port, bind, up, key, harness, root = sys.argv[1:8]
path = os.path.join(c, "config.json")
cfg = json.load(open(os.path.join(c, "config.json.example")))
if os.path.exists(path):
    cfg.update(json.load(open(path)))
cfg["port"] = int(port); cfg["bind"] = bind
if up: cfg["upstream_url"] = up
if key: cfg["upstream_key"] = key
cfg.setdefault("skills_dirs", []); cfg.setdefault("plugins_dirs", [])
if harness == "1":
    a = cfg.setdefault("agents", {})
    a.update(enabled=True, ssh="", dir=os.path.join(root, "harness"), python="uv run",
             script="scripts/run_master.py")
    a.setdefault("master_name", "Sultan")
    sd = os.path.join(root, "harness", "skills")
    if sd not in cfg["skills_dirs"]:
        cfg["skills_dirs"].append(sd)
json.dump(cfg, open(path, "w"), indent=2)
print("   config:", path)
PY

# ---------------------------------------------------------------- service
URL="http://$BIND:$PORT"
if [ "$NO_SERVICE" = 1 ]; then
  say "not installing a service; run:  cd $C && python3 server.py"
elif [ "$(uname)" = "Darwin" ]; then
  P="$HOME/Library/LaunchAgents/ai.bytebunker.console.plist"
  say "installing launchd service $P"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$P" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.bytebunker.console</string>
  <key>ProgramArguments</key><array><string>$(command -v python3)</string><string>$C/server.py</string></array>
  <key>WorkingDirectory</key><string>$C</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$C/data/console.log</string>
  <key>StandardErrorPath</key><string>$C/data/console.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/ai.bytebunker.console" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$P"
  launchctl kickstart -k "gui/$(id -u)/ai.bytebunker.console"
else
  U="$HOME/.config/systemd/user/bytebunker-console.service"
  say "installing systemd user service $U"
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$U" <<UN
[Unit]
Description=ByteBunker console
After=network.target

[Service]
WorkingDirectory=$C
ExecStart=$(command -v python3) $C/server.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$C/data/console.log
StandardError=append:$C/data/console.log

[Install]
WantedBy=default.target
UN
  systemctl --user daemon-reload
  systemctl --user enable --now bytebunker-console.service
  systemctl --user restart bytebunker-console.service
  loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes' || echo "   tip: sudo loginctl enable-linger $USER  (keeps it running when you log out)"
fi

sleep 2
if curl -fs -m 5 "$URL/api/models" >/dev/null 2>&1; then
  say "console is up at $URL"
else
  say "console installed; it answers at $URL once your upstream_url is reachable (edit $C/config.json)"
fi
cat <<TXT

Next:
  1. Open $URL. The Models screen lists what your gateway serves.
  2. Recipes screen: deploy a model (vLLM on a CUDA GPU, Ollama on Windows, vLLM on DGX Spark) and register it in litellm.
  3. Agents screen: name your master and write its standing orders; goals run on the harness ($( [ "$WITH_HARNESS" = 1 ] && echo "local mode, $DIR/harness" || echo "add --with-harness, or point agents.ssh at a worker host" )).
  4. Skills screen: create skills in the UI; they are shared with the agents.
TXT
