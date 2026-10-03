#!/usr/bin/env bash
# Build ByteBunker.app and a drag-to-Applications DMG.
#   PYTHON=desktop/.venv/bin/python desktop/build-mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-desktop/.venv/bin/python}
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN --distpath desktop/dist --workpath desktop/build desktop/bytebunker.spec
VER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' desktop/app.py)
STAGE=desktop/dist/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R desktop/dist/ByteBunker.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
OUT="desktop/dist/ByteBunker-$VER-mac-$(uname -m).dmg"
hdiutil create -quiet -volname "ByteBunker $VER" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
rm -rf "$STAGE"
echo "$OUT"
