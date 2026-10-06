"""Where ByteBunker keeps its files: one answer for the app, the server and
bb, so a terminal and a window always find the same sessions.

    macOS    ~/Library/Application Support/ByteBunker
    Windows  %APPDATA%\\ByteBunker
    Linux    $XDG_CONFIG_HOME/bytebunker (~/.config/bytebunker)

BYTEBUNKER_HOME moves all of it; BYTEBUNKER_DATA moves only data/ (a
server installed from a checkout keeps its data beside the code).
"""
import os
import sys


def home_dir():
    if os.environ.get("BYTEBUNKER_HOME"):
        return os.path.abspath(os.path.expanduser(os.environ["BYTEBUNKER_HOME"]))
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/ByteBunker")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "ByteBunker")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "bytebunker")


def data_dir():
    if os.environ.get("BYTEBUNKER_DATA"):
        return os.path.abspath(os.path.expanduser(os.environ["BYTEBUNKER_DATA"]))
    return os.path.join(home_dir(), "data")
