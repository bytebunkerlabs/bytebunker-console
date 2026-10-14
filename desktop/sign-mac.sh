#!/usr/bin/env bash
# Sign with a Developer ID and notarize, when the credentials are there; else
# leave the ad-hoc signature PyInstaller made (and say so).
#
#   desktop/sign-mac.sh app desktop/dist/ByteBunker.app    sign, notarize, staple the app
#   desktop/sign-mac.sh dmg desktop/dist/ByteBunker-X.dmg  sign, notarize, staple the disk image
#
# From the environment (GitHub secrets on release builds):
#   MACOS_CERT_P12        the Developer ID Application certificate, a .p12, base64
#   MACOS_CERT_PASSWORD   its password
#   APPLE_API_KEY_P8      an App Store Connect API key (.p8 contents), for notarytool
#   APPLE_API_KEY_ID, APPLE_API_ISSUER
#   APPLE_TEAM_ID         (checked against the certificate)
# MACOS_SIGN_IDENTITY instead of the .p12: an identity already in the keychain
# (a local build on the owner's Mac). MACOS_NOTARIZE=0 signs without notarizing.
set -euo pipefail
kind=$1 target=$2
ENT="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"

if [ -z "${MACOS_CERT_P12:-}" ] && [ -z "${MACOS_SIGN_IDENTITY:-}" ]; then
  echo "sign-mac: no Developer ID (MACOS_CERT_P12): $target stays ad-hoc signed"
  exit 0
fi

# ---- the identity: a throwaway keychain on CI, deleted when this script ends
if [ -n "${MACOS_CERT_P12:-}" ]; then
  KC="$RUNNER_TEMP/bb-sign-$$.keychain-db"
  KCPW=$(uuidgen)
  P12="$RUNNER_TEMP/bb-cert-$$.p12"
  trap 'security delete-keychain "$KC" 2>/dev/null || true; rm -f "$P12" "${KEY:-}"' EXIT
  printf '%s' "$MACOS_CERT_P12" | base64 --decode > "$P12"
  security create-keychain -p "$KCPW" "$KC"
  security set-keychain-settings -lut 3600 "$KC"
  security unlock-keychain -p "$KCPW" "$KC"
  security import "$P12" -k "$KC" -P "${MACOS_CERT_PASSWORD:-}" -T /usr/bin/codesign >/dev/null
  security set-key-partition-list -S apple-tool:,apple: -s -k "$KCPW" "$KC" >/dev/null
  security list-keychains -d user -s "$KC" $(security list-keychains -d user | tr -d '"')
  IDENTITY=$(security find-identity -v -p codesigning "$KC" | sed -n 's/.*"\(Developer ID Application:.*\)"/\1/p' | head -1)
else
  IDENTITY=$MACOS_SIGN_IDENTITY
fi
[ -n "$IDENTITY" ] || { echo "sign-mac: no 'Developer ID Application' identity in the certificate" >&2; exit 1; }
if [ -n "${APPLE_TEAM_ID:-}" ] && [[ "$IDENTITY" != *"($APPLE_TEAM_ID)"* ]]; then
  echo "sign-mac: the certificate is not team $APPLE_TEAM_ID" >&2; exit 1
fi
echo "sign-mac: signing $target as $IDENTITY"

notarize() {   # notarize <file to submit>
  [ "${MACOS_NOTARIZE:-1}" = 0 ] && { echo "sign-mac: MACOS_NOTARIZE=0, not notarized"; return; }
  [ -n "${APPLE_API_KEY_P8:-}" ] || { echo "sign-mac: no APPLE_API_KEY_P8: signed, not notarized" >&2; exit 1; }
  KEY="$RUNNER_TEMP/AuthKey_$$.p8"
  printf '%s' "$APPLE_API_KEY_P8" > "$KEY"
  out=$(xcrun notarytool submit "$1" --key "$KEY" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER" \
        --wait --timeout 30m --output-format json)
  echo "$out"
  if ! printf '%s' "$out" | grep -q '"status" *: *"Accepted"'; then
    id=$(printf '%s' "$out" | sed -n 's/.*"id" *: *"\([^"]*\)".*/\1/p' | head -1)
    [ -n "$id" ] && xcrun notarytool log "$id" --key "$KEY" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER" || true
    echo "sign-mac: Apple did not accept $1" >&2; exit 1
  fi
}

case "$kind" in
  app)
    # inside out: every Mach-O file first (deepest paths first), then the bundle itself
    find "$target/Contents" -type f \( -name '*.dylib' -o -name '*.so' -o -perm -111 \) -print0 \
      | xargs -0 file | grep -E 'Mach-O' | cut -d: -f1 | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2- \
      | while IFS= read -r f; do
          codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$IDENTITY" "$f"
        done
    codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$IDENTITY" "$target"
    codesign --verify --deep --strict --verbose=2 "$target"
    Z="$(dirname "$target")/notarize-$$.zip"
    ditto -c -k --keepParent "$target" "$Z"
    notarize "$Z"
    rm -f "$Z"
    [ "${MACOS_NOTARIZE:-1}" = 0 ] || { xcrun stapler staple "$target"; spctl --assess --type execute -vv "$target"; }
    ;;
  dmg)
    codesign --force --timestamp --sign "$IDENTITY" "$target"
    notarize "$target"
    [ "${MACOS_NOTARIZE:-1}" = 0 ] || { xcrun stapler staple "$target"; spctl --assess --type open --context context:primary-signature -vv "$target"; }
    ;;
  *) echo "usage: sign-mac.sh app|dmg <path>" >&2; exit 2 ;;
esac
