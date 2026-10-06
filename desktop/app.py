#!/usr/bin/env python3
"""ByteBunker desktop — the console in a native window.

    ByteBunker                          open the app
    ByteBunker --headless [--port N]    serve without a window and print the URL
    ByteBunker --mcp <script> [args]    run a built-in MCP server on stdio
                                        (the app launches itself this way)
    ByteBunker --smoke-gui <out.json>   open the window, check what rendered,
                                        write the result, quit (tests and CI)
    ByteBunker --version

Everything the user owns lives in one data directory, never inside the app:

    macOS    ~/Library/Application Support/ByteBunker
    Windows  %APPDATA%\\ByteBunker
    Linux    $XDG_CONFIG_HOME/bytebunker  (~/.config/bytebunker)

(BYTEBUNKER_HOME overrides it.) The server is the same stdlib console that
runs headless on a Mac mini: here it binds 127.0.0.1 on a free port and the
window is its only client. The one third-party package is pywebview, for
the window; the build adds PyInstaller.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import sys
import threading
import urllib.request

APP = "ByteBunker"


# ------------------------------------------------------------------ paths --
def code_root():
    """Where server.py, public/ and skills/ are: the repo when run from
    source, the unpacked bundle when frozen."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


if code_root() not in sys.path:
    sys.path.insert(0, code_root())
from version import VERSION  # noqa: E402  (the one place the version lives)
import instance as instancemod  # noqa: E402
import paths as pathsmod  # noqa: E402


home_dir = pathsmod.home_dir      # one answer for the app, the server and bb


def first_run_config(home):
    """A fresh install knows no engines: the UI opens on discovery. Nothing
    rack-specific from the server's own defaults survives this."""
    ws = os.path.join(home, "workspace")
    servers = {"jobs": {"command": "python3", "args": ["mcp_jobs.py"], "env": {}, "enabled": True},
               # a shell for the model on your own machine (PowerShell on Windows):
               # present, off until you turn it on
               "terminal": {"command": "python3", "args": ["mcp_terminal.py", ws], "env": {}, "enabled": False}}
    try:
        user = getpass.getuser()
    except Exception:   # noqa: BLE001
        user = "you"
    return {
        "bind": "127.0.0.1",
        "port": 0,
        "gateways": [],
        "identity": {"user": user, "host": (platform.node() or "this machine").split(".")[0]},
        "monitors": [],
        "h3_url": "", "netcheck_ssh": "",
        "skills_dirs": [os.path.join(home, "skills")],
        "plugins_dirs": [os.path.join(home, "plugins")],
        "plugins": {},
        "uploads_dir": os.path.join(ws, "uploads"),
        "mcp_servers": servers,
        "agents": {"enabled": False, "ssh": "", "dir": "~/bytebunker-harness", "python": "uv run",
                   "script": "scripts/run_master.py", "master_name": "Sultan", "master_instructions": "",
                   "run_timeout_s": 10800, "master_model": "", "thinking_model": "", "slave_model": ""},
        "frontier_rates_per_mtok": {"input": 3.0, "output": 15.0},
    }


def prepare_home():
    home = home_dir()
    for d in (home, os.path.join(home, "data"), os.path.join(home, "skills"), os.path.join(home, "plugins"),
              os.path.join(home, "workspace", "uploads"), os.path.join(home, "webview")):
        os.makedirs(d, exist_ok=True)
    cfg = os.path.join(home, "config.json")
    if not os.path.exists(cfg):
        tmp = cfg + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(first_run_config(home), f, indent=2)
            f.write("\n")
        os.replace(tmp, cfg)
        try:
            os.chmod(cfg, 0o600)          # gateway keys live here
        except OSError:
            pass
    os.environ["BYTEBUNKER_DATA"] = os.path.join(home, "data")
    os.environ["BYTEBUNKER_CONFIG"] = cfg
    os.environ["BYTEBUNKER_DESKTOP"] = "1"
    os.environ["BYTEBUNKER_VERSION"] = VERSION
    return home


# --------------------------------------------------------------- instance --
# The server itself holds the data folder's lock and publishes instance.json
# (instance.py). The launcher only asks whether one is already running.
def running_instance(home):
    """URL of the server already using this app's data folder, or None."""
    data = os.environ.get("BYTEBUNKER_DATA") or os.path.join(home, "data")
    found = instancemod.find(data)
    return found["url"] if found else None


# ---------------------------------------------------------------- windows --
def quiet_subprocesses():
    """A GUI app on Windows flashes a console window for every child process
    (ssh, npx, uvx, tailscale) unless told not to."""
    if sys.platform != "win32":
        return
    import subprocess
    base = subprocess.Popen

    class NoWindowPopen(base):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("creationflags", 0x08000000)    # CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    subprocess.Popen = NoWindowPopen


class Bridge:
    """Exposed to the page as window.pywebview.api."""

    def open_external(self, url):
        import webbrowser
        if str(url).startswith(("http://", "https://")):
            webbrowser.open(str(url))
            return True
        return False

    def version(self):
        return VERSION

    def open_folder(self, path):
        """Show a folder in Finder / Explorer — only the app's own data."""
        import subprocess
        home = home_dir()
        p = os.path.abspath(os.path.expanduser(str(path or home)))
        if not (p == home or p.startswith(home + os.sep)) or not os.path.isdir(p):
            return False
        if sys.platform == "darwin":
            subprocess.Popen(["open", p])
        elif sys.platform == "win32":
            os.startfile(p)          # noqa: S606 - opening the user's own folder
        else:
            subprocess.Popen(["xdg-open", p])
        return True


def open_window(url, home):
    import webview
    settings = getattr(webview, "settings", None)
    if isinstance(settings, dict):
        settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        settings["ALLOW_DOWNLOADS"] = True
    webview.create_window(APP, url, width=1380, height=900, min_size=(960, 640),
                          text_select=True, js_api=Bridge(), background_color="#FBFAF7")
    # not private: the console keeps its theme and per-viewer choices in localStorage
    webview.start(private_mode=False, storage_path=os.path.join(home, "webview"))


SMOKE_PROBE = """JSON.stringify({
  title: document.title,
  navs: document.querySelectorAll('[data-nav]').length,
  firstRun: !document.getElementById('first-run').hidden,
  bridge: !!(window.pywebview && window.pywebview.api && window.pywebview.api.open_external),
  serving: document.getElementById('serving-line').textContent,
  models: document.getElementById('model-select').options.length
})"""


def smoke_gui(url, out_path):
    """Open the real window on `url`, wait for the console to boot, write
    what rendered as JSON to out_path, and close."""
    import time
    import webview
    result = {"url": url, "ok": False}

    def check(window):
        deadline = time.time() + 45
        while time.time() < deadline:
            time.sleep(1)
            try:
                page = json.loads(window.evaluate_js(SMOKE_PROBE))
            except Exception as e:   # noqa: BLE001
                result["error"] = str(e)[:300]
                continue
            result["page"] = page
            if page.get("bridge") and page.get("navs", 0) > 5 and page.get("serving") not in ("connecting\u2026", ""):
                result["ok"] = True
                break
        window.destroy()

    w = webview.create_window(APP + " smoke", url, width=1200, height=800, js_api=Bridge())
    webview.start(check, w, private_mode=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0 if result["ok"] else 1


# -------------------------------------------------------------------- mcp --
def run_mcp(args):
    """Run a built-in MCP server in this process (stdio JSON-RPC). In a
    packaged app there is no `python3`, so the app is its own interpreter."""
    if not args:
        sys.stderr.write("usage: %s --mcp <mcp_terminal.py|mcp_jobs.py> [args]\n" % APP)
        return 2
    script, rest = os.path.basename(args[0]), args[1:]
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:   # noqa: BLE001
            pass
    sys.argv = [script] + rest          # the terminal server reads its root from argv at import
    root = code_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    if script == "mcp_terminal.py":
        import mcp_terminal as mod
    elif script == "mcp_jobs.py":
        import mcp_jobs as mod
    else:
        sys.stderr.write("unknown built-in MCP server: %s\n" % script)
        return 2
    mod.main()
    return 0


def serve_until_closed(srv):
    """serve_forever, restarted if it ever dies from something unexpected;
    returns when the window closes and the server is shut down."""
    import time
    while True:
        try:
            srv.serve_forever()
            return
        except Exception as e:   # noqa: BLE001
            sys.stderr.write("http server stopped (%s); restarting\n" % e)
            time.sleep(1)


# ------------------------------------------------------------------- main --
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--mcp"]:
        return run_mcp(argv[1:])
    if argv[:1] == ["--cli"]:
        # the bb command: the installed shim runs the app binary with --cli
        import bb
        return bb.main(argv[1:])
    if argv[:1] in (["--version"], ["-V"]):
        print(APP, VERSION)
        return 0
    headless = "--headless" in argv
    smoke_out = argv[argv.index("--smoke-gui") + 1] if "--smoke-gui" in argv else None
    port = 0
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])

    home = prepare_home()
    quiet_subprocesses()
    try:                                  # 0.2.0 kept this file in the home folder
        os.remove(os.path.join(home, "instance.json"))
    except OSError:
        pass
    existing = running_instance(home)
    if existing:
        # one server per data folder in every mode: never a second server and
        # a second job scheduler on the same files
        if headless:
            print("%s is already running on %s (data: %s)" % (APP, existing, home), flush=True)
            return 0
        if smoke_out:
            print("another %s is using %s; the smoke test needs its own BYTEBUNKER_HOME" % (APP, home), file=sys.stderr)
            return 2
        open_window(existing, home)
        return 0

    import server
    try:
        srv = server.serve("127.0.0.1", port)
    except SystemExit as e:               # lost a race with another launch
        existing = running_instance(home)
        if existing and not headless and not smoke_out:
            open_window(existing, home)
            return 0
        print(str(e), file=sys.stderr)
        return 2
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    if headless:
        # launchd and systemd stop services with SIGTERM, and a process started
        # in the background of a non-interactive shell inherits SIGINT ignored:
        # make both end serve_forever through the cleanup below
        import signal

        def _stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    try:
        if smoke_out:
            threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
            return smoke_gui(url, smoke_out)
        if headless:
            print("%s %s on %s  (data: %s)" % (APP, VERSION, url, home), flush=True)
            srv.serve_forever()
        else:
            threading.Thread(target=serve_until_closed, args=(srv,), name="http", daemon=True).start()
            open_window(url, home)
    except KeyboardInterrupt:
        pass
    finally:
        server.release_instance()
        try:
            if getattr(server, "_MCP", None) is not None:
                server._MCP.stop_all()
        except Exception:   # noqa: BLE001
            pass
        if not headless:
            srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
