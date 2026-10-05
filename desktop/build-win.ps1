# Build ByteBunker (window + console twin) and zip it.
#   python -m pip install pywebview pyinstaller ; ./desktop/build-win.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m PyInstaller --noconfirm --clean --log-level WARN --distpath desktop/dist --workpath desktop/build desktop/bytebunker.spec
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
$ver = (Select-String -Path version.py -Pattern '^VERSION = "([^"]+)"').Matches[0].Groups[1].Value
$out = "desktop/dist/ByteBunker-$ver-win-x64.zip"
Compress-Archive -Path desktop/dist/ByteBunker -DestinationPath $out -Force
Write-Output $out
