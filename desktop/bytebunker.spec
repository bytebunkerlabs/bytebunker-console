# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for ByteBunker desktop.
#
#   pyinstaller --noconfirm --clean --distpath desktop/dist --workpath desktop/build desktop/bytebunker.spec
#
# macOS  -> desktop/dist/ByteBunker.app
# Windows-> desktop/dist/ByteBunker/ByteBunker.exe (window) + ByteBunker-cli.exe
#           (console twin: --headless, and the built-in MCP servers' stdio)
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
HERE = SPECPATH
with open(os.path.join(ROOT, "version.py"), encoding="utf-8") as f:
    VERSION = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M).group(1)

datas = [
    (os.path.join(ROOT, "public"), "public"),
    (os.path.join(ROOT, "skills"), "skills"),
    (os.path.join(ROOT, "plugins"), "plugins"),
    (os.path.join(ROOT, "config.json.example"), "."),
]
# the console is imported at run time by the launcher, and the built-in MCP
# servers by --mcp: name them so the analysis bundles them and their imports
hidden = ["server", "version", "instance", "gateways", "monitors", "jobs", "agents", "recipes", "skills", "traces",
          "mcp", "mcp_catalog", "mcp_jobs", "mcp_terminal"]

a = Analysis(
    [os.path.join(HERE, "app.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "PIL", "numpy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = os.path.join(HERE, "icon.icns" if sys.platform == "darwin" else "icon.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ByteBunker",
          console=False, icon=icon, upx=False, argv_emulation=False)
targets = [exe]
if sys.platform == "win32":
    targets.append(EXE(pyz, a.scripts, [], exclude_binaries=True, name="ByteBunker-cli",
                       console=True, icon=icon, upx=False))

coll = COLLECT(*targets, a.binaries, a.datas, name="ByteBunker", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ByteBunker.app",
        icon=icon,
        bundle_identifier="ai.bytebunker.desktop",
        version=VERSION,
        info_plist={
            "CFBundleName": "ByteBunker",
            "CFBundleDisplayName": "ByteBunker",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "NSLocalNetworkUsageDescription": (
                "ByteBunker finds model servers (litellm, vLLM, Ollama, LM Studio, llama.cpp) "
                "on your network and tailnet, and talks to the ones you add."),
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
