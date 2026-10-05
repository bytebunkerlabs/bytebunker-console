#!/usr/bin/env bash
# Build ByteBunker.app and a drag-to-Applications DMG.
#   PYTHON=desktop/.venv/bin/python desktop/build-mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-desktop/.venv/bin/python}
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN --distpath desktop/dist --workpath desktop/build desktop/bytebunker.spec
VER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' version.py)
STAGE=desktop/dist/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R desktop/dist/ByteBunker.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
OUT="desktop/dist/ByteBunker-$VER-mac-$(uname -m).dmg"
# hdiutil fails intermittently on CI runners ("Resource busy"): retry, loudly
for attempt in 1 2 3 4 5; do
  if hdiutil create -volname "ByteBunker $VER" -srcfolder "$STAGE" -ov -format UDZO "$OUT"; then
    break
  fi
  [ "$attempt" = 5 ] && { echo "hdiutil failed 5 times" >&2; exit 1; }
  echo "hdiutil failed (attempt $attempt); retrying" >&2
  sleep $((attempt * 5))
done
rm -rf "$STAGE"
echo "$OUT"
