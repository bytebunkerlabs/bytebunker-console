#!/usr/bin/env bash
# ByteBunker one-command install.
#
#   curl -fsSL https://raw.githubusercontent.com/bytebunkerlabs/bytebunker-console/main/install.sh | bash
#   ./install.sh --upstream http://192.0.2.10:8000/v1 --key KEY
#
# Installs the console (stdlib Python 3.9+, no pip) as a background service
# on macOS (launchd) or Linux (systemd --user) and writes config.json.
# --upstream adds a model server as the first gateway; without it, the
# Gateways screen finds the servers on your network. Agents run on a
# separate worker, connected from the Agents screen, never on this machine.
# Re-running updates in place. Nothing needs sudo.
set -euo pipefail

DIR="${BYTEBUNKER_DIR:-$HOME/bytebunker}"
PORT=8765; BIND=127.0.0.1
UPSTREAM=""; KEY=""
NO_SERVICE=0
CONSOLE_REPO="${CONSOLE_REPO:-https://github.com/bytebunkerlabs/bytebunker-console.git}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --bind) BIND="$2"; shift 2;;
    --upstream) UPSTREAM="$2"; shift 2;;
    --key) KEY="$2"; shift 2;;
    --with-harness)
      echo "!! --with-harness is gone: agents run on a separate worker, never on the console's machine."
      echo "   Install without it, then connect a worker on the Agents screen."
      exit 2;;
    --no-service) NO_SERVICE=1; shift;;
    -h|--help) sed -n '2,13p' "$0"; exit 0;;
    *) echo "unknown option: $1"; exit 2;;
  esac
done

say() { printf '\033[1;36m==\033[0m %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "!! need $1"; exit 1; }; }
need git; need python3
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "!! python3 >= 3.9 required (have $PYV)"; exit 1; }

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

# ---------------------------------------------------------------- config
python3 - "$C" "$PORT" "$BIND" "$UPSTREAM" "$KEY" <<'PY'
import json, os, sys
c, port, bind, up, key = sys.argv[1:6]
path = os.path.join(c, "config.json")
cfg = json.load(open(os.path.join(c, "config.json.example")))
if os.path.exists(path):
    existing = json.load(open(path))
    if "gateways" not in existing:
        cfg.pop("gateways", None)      # an older config: the server turns its upstream_url into a gateway
    cfg.update(existing)
cfg["port"] = int(port); cfg["bind"] = bind
if up:
    url = up.rstrip("/")
    url = url if "://" in url else "http://" + url
    url = url if url.endswith("/v1") else url + "/v1"
    gws = [g for g in (cfg.get("gateways") or []) if g.get("url", "").rstrip("/") != url]
    cfg["gateways"] = [{"name": "upstream", "url": url, "key": key, "enabled": True}] + gws
cfg.setdefault("skills_dirs", []); cfg.setdefault("plugins_dirs", [])
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.chmod(tmp, 0o600)                    # gateway keys live here
os.replace(tmp, path)
print("   config:", path)
PY

# ---------------------------------------------------------------- bb
# the command line, on this install's data: bytebunker always, bb unless
# another program (Babashka) has that name
mkdir -p "$HOME/.local/bin"
for name in bytebunker bb; do
  target="$HOME/.local/bin/$name"
  if [ "$name" = bb ] && command -v bb >/dev/null 2>&1 && ! grep -q ByteBunker "$(command -v bb)" 2>/dev/null; then
    say "not writing bb: $(command -v bb) is another program's (use bytebunker)"
    continue
  fi
  printf '#!/bin/sh\n# ByteBunker'"'"'s command line (written by install.sh)\nBYTEBUNKER_DATA="%s" exec "%s" "%s" "$@"\n' \
    "$C/data" "$(command -v python3)" "$C/bb.py" > "$target"
  chmod +x "$target"
done
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "add ~/.local/bin to your PATH to use bb";; esac

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

if [ "$NO_SERVICE" = 1 ]; then
  :
elif sleep 2 && curl -fs -m 5 "$URL/api/models" >/dev/null 2>&1; then
  say "console is up at $URL"
else
  say "console installed at $URL; it did not answer yet (see $C/data/console.log)"
fi
cat <<TXT

Next:
  1. Open $URL (on another machine: ssh -L $PORT:127.0.0.1:$PORT this-host, then the same URL).
  2. Gateways: add your model servers, or let it find them on your network.
  3. Cluster: add a rack monitor (rack monitor up on the rack prints its address).
  4. Agents: connect a separate worker to run goals there.
  5. In a terminal: bb "hello" (bb help lists everything).
TXT
