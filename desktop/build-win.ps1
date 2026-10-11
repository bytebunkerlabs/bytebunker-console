# Build ByteBunker (window + console twin) and zip it.
#   python -m pip install pywebview pyinstaller ; ./desktop/build-win.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m PyInstaller --noconfirm --clean --log-level WARN --distpath desktop/dist --workpath desktop/build desktop/bytebunker.spec
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
$ver = (Select-String -Path version.py -Pattern '^VERSION = "([^"]+)"').Matches[0].Groups[1].Value
$out = "desktop/dist/ByteBunker-$ver-win-x64.zip"
Compress-Archive -Path desktop/dist/ByteBunker -DestinationPath $out -Force
# the checksum the app's updater checks before it unpacks
$hash = (Get-FileHash -Algorithm SHA256 $out).Hash.ToLower()
Set-Content -Path "$out.sha256" -Value ("{0}  {1}" -f $hash, (Split-Path -Leaf $out)) -Encoding ascii
Write-Output $out
