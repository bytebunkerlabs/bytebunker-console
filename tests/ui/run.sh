#!/usr/bin/env bash
# Browser tests: a real server on a temporary home, with the fake engine, a
# fake MCP server and a fake agents harness, driven by Playwright.
#
#   npm install --no-save playwright-core && npx playwright install chromium
#   bash tests/ui/run.sh
set -u
cd "$(dirname "$0")/../.."
R="$(pwd)"
PY="${PYTHON:-python3}"
H="$(mktemp -d)"
ENGINE_PORT="${BB_UI_ENGINE_PORT:-18998}"
SERVER_PORT="${BB_UI_SERVER_PORT:-18797}"
export BB_UI_BASE="http://127.0.0.1:$SERVER_PORT" BB_UI_ENGINE="http://127.0.0.1:$ENGINE_PORT"

cleanup() { kill $EPID $SPID 2>/dev/null; wait 2>/dev/null; rm -rf "$H"; }
trap cleanup EXIT

"$PY" tests/fakes/fake_engine.py --port "$ENGINE_PORT" > "$H/engine.log" 2>&1 &
EPID=$!
mkdir -p "$H/data"
"$PY" - "$H" "$R" "$ENGINE_PORT" "$SERVER_PORT" <<'PYEOF'
import json, os, sys
h, r, eport, sport = sys.argv[1:5]
src = open(os.path.join(r, "tests", "test_runs_api.py")).read()
harness = src.split("FAKE_HARNESS = r'''", 1)[1].split("'''", 1)[0]
open(os.path.join(h, "fake_harness.py"), "w").write(harness)
json.dump({"gateways": [{"name": "fake", "url": "http://127.0.0.1:%s/v1" % eport, "key": "", "enabled": True}],
           "monitors": [], "port": int(sport),
           "agents": {"enabled": True, "ssh": "", "dir": h, "python": sys.executable, "script": "fake_harness.py"},
           "mcp_servers": {"fake": {"command": sys.executable, "args": [os.path.join(r, "tests", "fakes", "fake_mcp.py")]}},
           "model_capabilities": {"fake-model": {"effort": ["low", "medium", "high"]}}},
          open(os.path.join(h, "config.json"), "w"))
PYEOF
BYTEBUNKER_DATA="$H/data" BYTEBUNKER_CONFIG="$H/config.json" "$PY" -c "import sys; sys.path.insert(0, '$R'); import server; srv = server.serve('127.0.0.1', $SERVER_PORT); srv.serve_forever()" > "$H/server.log" 2>&1 &
SPID=$!
for i in $(seq 1 100); do curl -s -o /dev/null "$BB_UI_BASE/api/hello" && break; sleep 0.1; done
for i in $(seq 1 100); do curl -s "$BB_UI_BASE/api/tools" | grep -q fake__echo && break; sleep 0.1; done

rc=0
for t in journeys app parity hidden fonts; do
  echo "== $t"
  node "tests/ui/$t.js" || { rc=1; echo "   (server log: $H/server.log)"; tail -20 "$H/server.log"; }
done
exit $rc
