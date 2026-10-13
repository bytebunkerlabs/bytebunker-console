"""Where a chat's terminal commands run: a sandbox, unless you said otherwise.

Three modes, per chat (the Playground's chip, bb --web / --full-access):

    local   the project's folder writable; system files and your installed tools
            (Homebrew, ~/.local, ~/.cargo, nvm, pyenv, uv...) readable; the rest of
            your home folder, ~/.ssh, keychains and cloud credentials invisible;
            no network
    web     the same, with network
    full    no sandbox: your user, everything (per chat, never a default)

The sandbox is macOS's Seatbelt or Linux's bubblewrap, as for agents. On Windows
a command runs inside WSL2 under bubblewrap when WSL2 is there; without it there
is no sandbox, and every command asks first (the app's job, not this module's).

    kind()                       "seatbelt" | "bwrap" | "wsl" | None
    wrap(argv, root, mode)       (argv, env) to run, or None when no sandbox here
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

MODES = ("local", "web", "full")
HOME = os.path.expanduser("~")
# your tools, readable inside the sandbox (never the secrets beside them)
TOOL_DIRS = [".local", ".cargo", ".rustup", ".nvm", ".pyenv", ".rbenv", ".bun", ".deno", ".volta",
             ".gitconfig", ".config/git", ".gitignore_global", ".npmrc.d"]
CACHE_DIRS = [".cache", ".npm"]          # writable with the web on: installs fill them
SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE)|^(SSH_AUTH_SOCK|AWS_|GH_|GITHUB_|"
                        r"OPENAI_|ANTHROPIC_|HF_|GOOGLE_APPLICATION)", re.I)
_kind = {}


def kind():
    """Which sandbox this machine offers a chat's commands."""
    if "k" in _kind:
        return _kind["k"]
    k = None
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        k = "seatbelt"
    elif sys.platform.startswith("linux") and shutil.which("bwrap"):
        k = "bwrap" if _bwrap_works() else None
    elif sys.platform == "win32":
        k = "wsl" if wsl_ready() else None
    _kind["k"] = k
    return k


def reset():
    _kind.clear()


def _bwrap_works():
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--die-with-parent", "true"],
                              capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def clean_env(env):
    """The environment a sandboxed command gets: yours, without keys and agent sockets."""
    return {k: v for k, v in env.items() if not SECRET_ENV.search(k)}


def _sb(p):
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(root, tmp, web):
    real = os.path.realpath
    reads = ["/usr", "/bin", "/sbin", "/System", "/Library", "/Applications", "/opt", "/private/etc",
             "/private/var/db", "/private/var/select", "/dev", "/var/select"]
    reads += [os.path.join(HOME, d) for d in TOOL_DIRS]
    writes = [real(root), real(tmp)] + ([os.path.join(HOME, d) for d in CACHE_DIRS] if web else [])
    lines = ["(version 1)", "(deny default)",
             "(allow process-fork)", "(allow process-exec)", "(allow signal (target same-sandbox))",
             "(allow sysctl-read)", "(allow file-read-metadata)", "(allow mach-lookup)", "(allow ipc-posix-shm)",
             "(allow iokit-open)", "(allow system-socket)",
             # the keychain answers over mach: closed
             '(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd") '
             '(global-name "com.apple.security.agent") (global-name "com.apple.secd"))',
             '(allow file-read* (literal "/") ' + " ".join("(subpath %s)" % _sb(p) for p in reads) + ")",
             "(allow file-read* file-write* " + " ".join("(subpath %s)" % _sb(p) for p in writes)
             + ' (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/ttys[0-9]+$"))']
    if web:
        lines += ["(allow network*)", '(allow file-read* (subpath "/private/var/run"))']
    return "\n".join(lines) + "\n"


def bwrap_args(root, tmp, web):
    root = os.path.realpath(root)
    a = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
         "--tmpfs", HOME, "--tmpfs", "/run/user",            # your home and your agent sockets: gone
         "--unshare-all", "--die-with-parent"]
    if web:
        a += ["--share-net"]
    for d in TOOL_DIRS:
        p = os.path.join(HOME, d)
        a += ["--ro-bind-try", p, p]
    if web:
        for d in CACHE_DIRS:
            p = os.path.join(HOME, d)
            a += ["--bind-try", p, p]
    a += ["--bind", root, root, "--bind", tmp, tmp,
          "--remount-ro", HOME,                        # the stand-in home: read-only, so a write there fails plainly
          "--chdir", root]
    return a


def wsl_ready():
    """WSL2 with a Linux distribution that has bubblewrap."""
    if sys.platform != "win32" or not shutil.which("wsl.exe"):
        return False
    try:
        r = subprocess.run(["wsl.exe", "-e", "sh", "-c", "command -v bwrap"], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wsl_path(p):
    """C:\\Users\\me\\proj -> /mnt/c/Users/me/proj."""
    p = os.path.abspath(p)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if not m:
        return p.replace("\\", "/")
    return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/"))


def wrap(argv, root, mode, env=None):
    """argv run in this chat's mode, as (argv, env); None when this machine has no sandbox
    (Windows without WSL2), which means the command may only run outside one, with your yes."""
    env = dict(env if env is not None else os.environ)
    if mode == "full":
        return argv, env
    k = kind()
    web = mode == "web"
    if k is None:
        return None
    if k == "wsl":
        # the command itself is a shell line for Linux; root is a Windows folder
        line = argv[-1] if argv else ""
        r = wsl_path(root)
        b = ("bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs \"$HOME\" --tmpfs /run/user "
             "--unshare-all --die-with-parent %s--bind %s %s --chdir %s bash -lc %s") % (
            "--share-net " if web else "", _q(r), _q(r), _q(r), _q(line))
        return ["wsl.exe", "-e", "bash", "-c", b], clean_env(env)
    tmp = tempfile.mkdtemp(prefix="bb-cmd-")
    env = clean_env(env)
    env.update(TMPDIR=tmp, TMP=tmp, TEMP=tmp)
    if k == "seatbelt":
        prof = os.path.join(tmp, ".sandbox.sb")
        with open(prof, "w") as f:
            f.write(seatbelt_profile(root, tmp, web))
        return ["sandbox-exec", "-f", prof] + list(argv), env
    return bwrap_args(root, tmp, web) + list(argv), env


def _q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- Windows: WSL2
def wsl_status():
    """Where a Windows PC is on the way to a sandbox: WSL2, a Linux in it, bubblewrap in that."""
    if sys.platform != "win32":
        return None
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    st = {"wsl": bool(shutil.which("wsl.exe")), "distro": False, "bwrap": False}
    if st["wsl"]:
        try:
            r = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=30, **flags)
            st["distro"] = r.returncode == 0 and bool(r.stdout.replace(b"\x00", b"").strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    st["bwrap"] = st["distro"] and wsl_ready()
    return st


def wsl_enable():
    """Step 1, admin: Windows asks for permission, installs WSL2 and Ubuntu; then a restart."""
    ps = "Start-Process -Verb RunAs -FilePath wsl.exe -ArgumentList '--install','-d','Ubuntu','--no-launch'"
    subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def wsl_finish(say=lambda t: None):
    """Step 2, after the restart: Ubuntu set up with no user prompt, bubblewrap in it (as root inside
    WSL, which needs no password)."""
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0), "capture_output": True}
    ubuntu = shutil.which("ubuntu.exe")
    if ubuntu and not (wsl_status() or {}).get("distro"):
        say("setting up Ubuntu")
        subprocess.run([ubuntu, "install", "--root"], timeout=900, **flags)
    say("installing bubblewrap in Ubuntu")
    r = subprocess.run(["wsl.exe", "-u", "root", "-e", "sh", "-c",
                        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bubblewrap"],
                       timeout=1800, **flags)
    reset()
    ok = wsl_ready()
    say("the sandbox is ready: commands run in WSL2 under bubblewrap" if ok else
        "bubblewrap did not install: " + (r.stderr or b"").decode("utf-8", "replace")[-300:])
    return ok
