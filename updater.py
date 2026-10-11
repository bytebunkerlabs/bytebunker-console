"""Updating the desktop app from its GitHub releases, when asked and never on
its own.

    check(current, desktop)        the latest published release, whether it is
                                   newer, and whether this app can take it
    download(url, dest, progress)  fetch this platform's build; its SHA-256
    expected_sha256(url, name)     the checksum the release publishes for it
    unpack(zip_path, staging)      the new app, unpacked beside the old one
    unpacked_version(app)          what the new app says it is (it runs --version)
    start_swap(...)                a helper, detached, that waits for the app to
                                   quit, moves it aside, moves the new one in and
                                   opens it (not with BYTEBUNKER_NO_RELAUNCH set,
                                   for scripted installs and tests)

An app replaces itself only where it can write; anywhere else the release
page does it by hand. Builds are ad-hoc signed until signing lands, so the
checksum the release publishes is what vouches for a download (and on a Mac
the bundle's own signature must still verify). Stdlib only.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

API = "https://api.github.com/repos/bytebunkerlabs/bytebunker-console/releases/latest"
UA = {"User-Agent": "ByteBunker", "Accept": "application/vnd.github+json"}


def api_url():
    return os.environ.get("BYTEBUNKER_UPDATE_URL") or API


def vtuple(v):
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def asset_name(version, plat=None, machine=None):
    """The release file that holds this platform's app."""
    plat = plat or sys.platform
    if plat == "darwin":
        return "ByteBunker-%s-mac-%s.zip" % (version, machine or platform.machine())
    if plat == "win32":
        return "ByteBunker-%s-win-x64.zip" % version
    return None


def app_location():
    """The installed app this process runs from (the .app bundle on a Mac, the
    folder on Windows); None when it runs from a checkout."""
    if not getattr(sys, "frozen", False):
        return None
    exe = os.path.realpath(sys.executable)
    if sys.platform == "darwin":
        p = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        return p if p.endswith(".app") else None
    if sys.platform == "win32":
        return os.path.dirname(exe)
    return None


def why_not(location, desktop):
    """Why this app cannot replace itself, or None when it can."""
    if not desktop:
        return "updates from the app are for the desktop app; a server updates with its installer"
    if location is None:
        return "this ByteBunker runs from a source checkout: update it with git pull"
    parent = os.path.dirname(location)
    if not (os.access(parent, os.W_OK) and os.access(location, os.W_OK)):
        return "ByteBunker cannot replace itself in %s (it belongs to another user): download it from the release page" % parent
    return None


def _get(url, timeout=20):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def check(current, desktop, location=None):
    with _get(api_url()) as r:
        rel = json.load(r)
    latest = str(rel.get("tag_name") or "").lstrip("v")
    assets = {a.get("name"): a for a in rel.get("assets") or [] if isinstance(a, dict)}
    name = asset_name(latest) if latest else None
    asset, checksum = assets.get(name), assets.get((name or "") + ".sha256")
    newer = bool(latest) and vtuple(latest) > vtuple(current)
    why = why_not(location, desktop) if newer else None
    if newer and not why and not asset:
        why = "the %s release has no build for this computer: download it from the release page" % latest
    if newer and not why and not checksum:
        why = "the %s release publishes no checksum for its build: download it from the release page" % latest
    return {"current": current, "latest": latest or None, "newer": newer, "notes": rel.get("body") or "",
            "page": rel.get("html_url") or "", "published": rel.get("published_at") or "",
            "asset": {"name": name, "size": asset.get("size"), "url": asset.get("browser_download_url")} if asset else None,
            "checksum_url": checksum.get("browser_download_url") if checksum else None,
            "can_update": bool(newer and not why), "why_not": why}


def download(url, dest, progress=None, timeout=60):
    """Fetch url to dest (through dest.part); returns its SHA-256."""
    h, got, last = hashlib.sha256(), 0, 0.0
    req = urllib.request.Request(url, headers={"User-Agent": "ByteBunker", "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest + ".part", "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if progress and time.time() - last > 0.25:
                progress(got, total)
                last = time.time()
    if progress:
        progress(got, total)
    os.replace(dest + ".part", dest)
    return h.hexdigest()


def expected_sha256(url, name):
    """The checksum a release publishes for one file ("<sha256>  <name>")."""
    with _get(url) as r:
        text = r.read().decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) and (len(parts) == 1 or parts[-1].lstrip("*") == name):
            return parts[0].lower()
    raise ValueError("the checksum file does not name %s" % name)


def unpack(zip_path, staging):
    """The one app inside the download, unpacked into staging; its path."""
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    if sys.platform == "darwin":
        # ditto keeps the bundle as built: links, permissions, its signature
        subprocess.run(["ditto", "-x", "-k", zip_path, staging], check=True, capture_output=True)
        found = [n for n in os.listdir(staging) if n.endswith(".app")]
    else:
        with zipfile.ZipFile(zip_path) as z:     # extract() drops absolute paths and ".."
            z.extractall(staging)
        found = [n for n in os.listdir(staging) if os.path.isdir(os.path.join(staging, n))]
    if len(found) != 1:
        raise ValueError("the download does not hold exactly one app")
    return os.path.join(staging, found[0])


def unpacked_version(app):
    """What the unpacked app says it is, or None."""
    if sys.platform == "darwin":
        exe = os.path.join(app, "Contents", "MacOS", "ByteBunker")
    else:
        exe = os.path.join(app, "ByteBunker-cli.exe")
        if not os.path.exists(exe):
            exe = os.path.join(app, "ByteBunker.exe")
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", (p.stdout or "") + (p.stderr or ""))
    return m.group(1) if m else None


def signature_ok(app):
    """On a Mac the bundle's own signature (ad-hoc for now) must verify."""
    if sys.platform != "darwin":
        return True
    return subprocess.run(["codesign", "--verify", "--deep", "--strict", app], capture_output=True).returncode == 0


SWAP_SH = r'''#!/bin/sh
# ByteBunker's updater: wait for the app to quit, move it aside, move the new
# one in, then open whichever is in place.
pid=$1 current=$2 new=$3 backup=$4 log=$5
shift 5
i=0
while kill -0 "$pid" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 600 ]; then echo "the app did not quit: nothing was changed" >> "$log"; exit 1; fi
  sleep 0.2
done
rm -rf "$backup"
mkdir -p "$(dirname "$backup")"
if mv "$current" "$backup"; then
  if mv "$new" "$current"; then
    echo "updated" >> "$log"
  else
    mv "$backup" "$current"
    echo "could not move the new app in: the old one is back" >> "$log"
  fi
else
  echo "could not move the old app aside: nothing was changed" >> "$log"
fi
"$@"
'''

SWAP_PS1 = r'''param([int]$AppPid, [string]$Current, [string]$New, [string]$Backup, [string]$Log, [string]$Relaunch)
# ByteBunker's updater: wait for the app to quit, move it aside, move the new
# one in, then start whichever is in place.
$deadline = (Get-Date).AddSeconds(120)
while (Get-Process -Id $AppPid -ErrorAction SilentlyContinue) {
  if ((Get-Date) -gt $deadline) { Add-Content -Path $Log -Value "the app did not quit: nothing was changed"; exit 1 }
  Start-Sleep -Milliseconds 200
}
$done = $false
for ($i = 0; $i -lt 30 -and -not $done; $i++) {
  try {
    if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Backup) | Out-Null
    Move-Item -LiteralPath $Current -Destination $Backup -ErrorAction Stop
    try {
      Move-Item -LiteralPath $New -Destination $Current -ErrorAction Stop
      Add-Content -Path $Log -Value "updated"
    } catch {
      Move-Item -LiteralPath $Backup -Destination $Current
      Add-Content -Path $Log -Value "could not move the new app in: the old one is back"
    }
    $done = $true
  } catch { Start-Sleep -Milliseconds 500 }    # a scanner holding a file: try again
}
if (-not $done) { Add-Content -Path $Log -Value "could not move the old app aside: nothing was changed" }
if ($Relaunch) { Start-Process -FilePath $Relaunch }
'''


def start_swap(pid, current, new, backup, log, relaunch, workdir):
    """Start the helper, detached from this process, which it outlives."""
    os.makedirs(workdir, exist_ok=True)
    if sys.platform == "win32":
        script = os.path.join(workdir, "swap.ps1")
        with open(script, "w", encoding="utf-8") as f:
            f.write(SWAP_PS1)
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS, NEW_PROCESS_GROUP, NO_WINDOW
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
               "-AppPid", str(pid), "-Current", current, "-New", new, "-Backup", backup, "-Log", log,
               "-Relaunch", relaunch[0] if relaunch else ""]
        return subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    script = os.path.join(workdir, "swap.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write(SWAP_SH)
    return subprocess.Popen(["/bin/sh", script, str(pid), current, new, backup, log] + list(relaunch or []),
                            start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def relaunch_command(location):
    if os.environ.get("BYTEBUNKER_NO_RELAUNCH"):
        return []
    if sys.platform == "darwin":
        return ["open", location]
    if sys.platform == "win32":
        return [os.path.join(location, "ByteBunker.exe")]
    return []
