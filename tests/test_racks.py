"""Add a rack: pairing over ssh, then every rack action with the app's own
key. A stand-in ssh plays sshd: the app's key reaches only its forced
command (`rack remote --app <name>`), your own ssh can be made to want a
password or to time out, and `ssh -G` resolves to this machine, where a fake
engine (with a key) and a fake monitor (with a token) answer."""
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "fakes"))
from test_api import Server  # noqa: E402
from test_monitors import FakeMonitor  # noqa: E402
from fake_engine import FakeEngine  # noqa: E402

SSH = r'''#!/bin/sh
F=__F__
printf '%s\n' "$*" >> "$F/ssh.log"
key=""
while [ $# -gt 0 ]; do
  case "$1" in
    -G) echo "hostname 127.0.0.1"; exit 0 ;;
    -i) key=$2; shift 2 ;;
    -o|-p|-l|-F|-J) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
target=$1; shift
[ -f "$F/down" ] && { echo "ssh: connect to host ${target#*@} port 22: Operation timed out" >&2; exit 255; }
if [ -n "$key" ]; then
  body=$(cut -d' ' -f2 "$key.pub")
  grep -q "$body" "$F/authorized_keys" 2>/dev/null || { echo "$target: Permission denied (publickey)." >&2; exit 255; }
  app=$(grep "$body" "$F/authorized_keys" | sed -n 's/.* bytebunker:\(.*\)$/\1/p')
  R="$F/bin/rack"; [ -x "$R" ] || R="$F/home/.local/bin/rack"
  SSH_ORIGINAL_COMMAND="$*" exec "$R" remote --app "$app"
fi
[ -f "$F/password-only" ] && { echo "$target: Permission denied (publickey,password)." >&2; exit 255; }
HOME="$F/home" exec /bin/sh -c "$*"
'''

RACK = r'''#!/bin/sh
F=__F__
case "$1" in
  install) mkdir -p "$HOME/.local/bin"; ln -sf "$(cd "$(dirname "$0")" && pwd)/rack" "$HOME/.local/bin/rack"; echo "installed: $HOME/.local/bin/rack" ;;
  init) echo "box: a DGX Spark, ready to serve" ;;
  version) echo "rack 1.0.0-test" ;;
  up) echo "serving $2 here: healthy" ;;
  platform) echo '{"schema":1,"platform":"mac","description":"Mac (test)","supported":true,"model_budget_mb":8192}' ;;
  recipes) echo '{"schema":1,"here":"mac","budget_mb":8192,"recipes":[{"name":"qwen3-8b","layout":"v2","location":"/r/qwen3-8b","model":"Qwen/Qwen3-8B","platforms":["mac","dgx"],"variants":{"mac":{"file":"/r/qwen3-8b/mac.env","engine":"llamacpp","weights_gb":5.0,"fits":true}}},{"name":"big","layout":"v2","model":"org/Big","platforms":["dgx"],"variants":{"dgx":{"file":"/r/big/dgx.env"}}}]}' ;;
  status) echo "serving"; echo "  nothing" ;;
  pair)
    shift; key=""; name=app
    while [ $# -gt 0 ]; do case "$1" in --key) key=$2; shift 2;; --name) name=$2; shift 2;; *) shift;; esac; done
    if [ -n "$key" ]; then
      set -- $key
      touch "$F/authorized_keys"; grep -v "$2" "$F/authorized_keys" > "$F/ak.tmp"; mv "$F/ak.tmp" "$F/authorized_keys"
      echo "command=\"rack remote --app $name\",restrict $1 $2 bytebunker:$name" >> "$F/authorized_keys"
    fi
    printf '{"schema":1,"rack_version":"1.0.0-test","name":"box","platform":"dgx","head":true,"hostname":"box",'
    printf '"addresses":[{"ip":"127.0.0.1","kind":"lan"}],'
    printf '"engine":{"port":__EPORT__,"key":"ek","answering":true,"serving":{"recipe":"qwen3-8b","model":"Qwen/Qwen3-8B","served_name":"fake-model"}},'
    printf '"monitor":{"port":__MPORT__,"token":"mt","running":true},"apps":[]}\n' ;;
  remote)
    app=$3
    printf '%s\n' "$app $SSH_ORIGINAL_COMMAND" >> "$F/remote.log"
    set -- $SSH_ORIGINAL_COMMAND
    [ "$1" = rack ] && shift
    case "$*" in
      "pair --json") exec "$0" pair --json ;;
      "nodes --json") echo '{"schema":1,"nodes":[{"name":"box","role":"head"},{"name":"w1","ssh":"w1","role":"worker"}]}' ;;
      "recipes --json") echo '{"schema":1,"here":"dgx","recipes":[{"name":"qwen3-8b","layout":"v2","model":"Qwen/Qwen3-8B","platforms":["dgx"],"variants":{"dgx":{"file":"/r/qwen3-8b/dgx.env"}}}]}' ;;
      status) echo "serving"; echo "  qwen3-8b (Qwen/Qwen3-8B) with vllm under docker on box, since now" ;;
      "recipes show "*) echo "# /home/you/.config/dgx-serve/recipes/$3/model.env"; echo "MODEL=Qwen/Qwen3-8B" ;;
      "up "*) echo "serving $2: healthy" ;;
      unpair) grep -v "bytebunker:$app\$" "$F/authorized_keys" > "$F/ak.tmp"; mv "$F/ak.tmp" "$F/authorized_keys"; echo "removed 1 app key ($app)" ;;
      *) echo "rack remote: not one of the commands an app may run here: $*" >&2; exit 1 ;;
    esac ;;
  *) echo "unknown command: $1" >&2; exit 1 ;;
esac
'''


def clean_path():
    """The test host's PATH without a real rack on it (dgx-serve may be installed here)."""
    return os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                           if d and not os.path.exists(os.path.join(d, "rack")))


def text(path):
    with open(path) as f:
        return f.read()


def script(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


@unittest.skipIf(sys.platform == "win32", "the stand-in ssh and rack are POSIX shell")
@unittest.skipIf(not shutil.which("ssh-keygen"), "pairing makes a key with ssh-keygen")
class AddARack(unittest.TestCase):
    def setUp(self):
        self.f = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.f, "bin"))
        os.makedirs(os.path.join(self.f, "home"))
        self.eng = FakeEngine(models=[{"id": "fake-model"}], api_key="ek").start()
        self.mon = FakeMonitor("mt", [{"name": "box"}])
        sub = lambda t: t.replace("__F__", self.f).replace("__EPORT__", str(self.eng.port)) \
            .replace("__MPORT__", self.mon.url.rsplit(":", 1)[1])
        script(os.path.join(self.f, "bin", "ssh"), sub(SSH))
        script(os.path.join(self.f, "bin", "rack"), sub(RACK))
        data = os.path.join(self.f, "data")
        os.makedirs(data)
        self.srv = Server(data, {}, env={"PATH": os.path.join(self.f, "bin") + os.pathsep + clean_path()})

    def tearDown(self):
        self.srv.stop()
        self.eng.stop()
        self.mon.close()
        shutil.rmtree(self.f, ignore_errors=True)

    def pair(self, target="you@box"):
        import time
        from test_api import HttpError
        deadline = time.time() + 5
        while True:
            try:
                frames = self.srv.sse("/api/racks", method="POST", body={"action": "pair", "target": target},
                                      timeout=60, until=lambda fr: json.loads(fr[-1]["data"]).get("phase") == "done")
                return [json.loads(x["data"]) for x in frames]
            except HttpError as e:
                # the last pairing's run lets go of its key just after its done frame: a moment later it is free
                if e.status != 409 or time.time() > deadline:
                    raise
                time.sleep(0.1)

    def found(self, frames):
        return next((x["found"] for x in frames if x.get("found")), None)

    def keys(self):
        p = os.path.join(self.f, "authorized_keys")
        return text(p).splitlines() if os.path.exists(p) else []

    def test_pairing_finds_the_engine_and_monitor_and_then_uses_only_the_apps_key(self):
        got = self.found(self.pair())
        self.assertEqual((got["name"], got["platform"]), ("box", "dgx"))
        self.assertEqual(got["engine"]["url"], "http://127.0.0.1:%d" % self.eng.port)
        self.assertEqual(got["monitor"]["url"], self.mon.url)
        self.assertEqual(got["engine"]["serving"]["recipe"], "qwen3-8b")
        self.assertNotIn("ek", json.dumps(got))                         # the secrets stay on the server
        (line,) = self.keys()
        self.assertTrue(line.startswith('command="rack remote --app ') and "bytebunker:" in line, line)
        st, r = self.srv.request("POST", "/api/racks", {"action": "save", "pairing": got["pairing"]})
        self.assertEqual(st, 200, r)
        self.assertEqual((r["rack"]["name"], r["rack"]["gateway"], r["rack"]["monitor"]), ("box", "box", "box"))
        cfg = json.loads(text(self.srv.cfg))
        self.assertEqual([(g["name"], g["url"], g["key"]) for g in cfg["gateways"]],
                         [("box", "http://127.0.0.1:%d/v1" % self.eng.port, "ek")])
        self.assertEqual([(m["name"], m["token"]) for m in cfg["monitors"]], [("box", "mt")])
        self.assertIn("fake-model", [m["id"] for m in self.srv.request("GET", "/api/models")[1]["data"]])
        # the rack card, the recipe and a deploy, all through the forced command
        st, d = self.srv.request("GET", "/api/rack")
        self.assertEqual((d["ok"], d["name"], d["serving"], d["paired"]), (True, "box", "qwen3-8b", True), d)
        st, d = self.srv.request("POST", "/api/rack", {"action": "show", "recipe": "qwen3-8b"})
        self.assertIn("MODEL=Qwen/Qwen3-8B", d["text"])
        self.assertTrue(d["file"].endswith("/qwen3-8b/model.env"), d["file"])
        out = self.srv.sse("/api/rack", method="POST", body={"action": "up", "recipe": "qwen3-8b"}, timeout=30,
                           until=lambda fr: json.loads(fr[-1]["data"]).get("phase") == "done")
        self.assertIn("serving qwen3-8b: healthy", [json.loads(x["data"]).get("line") for x in out])
        app = r["rack"]["app"]
        self.assertEqual(text(os.path.join(self.f, "remote.log")).splitlines(),
                         ["%s rack %s" % (app, c) for c in ("recipes --json", "status", "platform --json", "version --json",
                                                             "recipes show qwen3-8b", "up qwen3-8b",
                                                             # after serving, its monitor (for Cluster), started when it is not running
                                                             "monitor status", "monitor up",
                                                             # and what it says now, so that monitor joins Cluster
                                                             "pair --json")])
        # after pairing, ssh only ever went as the app, with nothing else offered
        calls = [l for l in text(os.path.join(self.f, "ssh.log")).splitlines() if not l.startswith("-G")]
        self.assertTrue(all("IdentitiesOnly=yes" in c and "IdentityAgent=none" in c and "ControlPath=none" in c
                            for c in calls[1:]), calls)
        self.assertIn("StrictHostKeyChecking=accept-new", calls[0])
        self.assertIn("StrictHostKeyChecking=yes", calls[-1])

    def test_password_only_ssh_gets_the_one_command_to_run_there(self):
        open(os.path.join(self.f, "password-only"), "w").close()
        frames = self.pair()
        need = next(x["needs_key"] for x in frames if x.get("needs_key"))
        self.assertTrue(need["command"].startswith("rack pair --key 'ssh-ed25519 "), need)
        self.assertEqual(self.keys(), [])
        # what a person types on the machine, then Pair again: the app gets in with its own key
        argv = shlex.split(need["command"])
        subprocess.run([os.path.join(self.f, "bin", "rack")] + argv[1:], check=True, capture_output=True)
        got = self.found(self.pair())
        self.assertEqual(got["engine"]["url"], "http://127.0.0.1:%d" % self.eng.port)

    def test_what_cannot_work_says_why(self):
        os.rename(os.path.join(self.f, "bin", "rack"), os.path.join(self.f, "rack.off"))
        errs = [x["error"] for x in self.pair() if x.get("error")]
        self.assertTrue(errs and "dgx-serve is not on you@box" in errs[0], errs)
        os.rename(os.path.join(self.f, "rack.off"), os.path.join(self.f, "bin", "rack"))
        open(os.path.join(self.f, "down"), "w").close()
        errs = [x["error"] for x in self.pair() if x.get("error")]
        self.assertTrue(errs and errs[0].startswith("could not reach you@box: ssh: connect to host box"), errs)
        for bad in ("-oProxyCommand=touch /tmp/x", "you@-oProxyCommand=x", "a b", ""):
            st, r = self.srv.request("POST", "/api/racks", {"action": "pair", "target": bad})
            self.assertEqual(st, 400, bad)
        st, r = self.srv.request("POST", "/api/racks", {"action": "save", "pairing": "pair-nope"})
        self.assertEqual(st, 410)

    def test_a_streamed_run_closes_when_it_finishes(self):
        # the run's finished event is the stream's last; a stream that waited
        # for done_event between the two used to hold the connection 15 s more
        import http.client
        import time
        got = self.found(self.pair())
        self.srv.request("POST", "/api/racks", {"action": "save", "pairing": got["pairing"]})
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=30)
        t = time.time()
        c.request("POST", "/api/rack", body=json.dumps({"action": "status"}),
                  headers={"Host": "127.0.0.1:%d" % self.srv.port, "Content-Type": "application/json",
                           "X-BB-Token": self.srv.token})
        body = c.getresponse().read().decode()                             # to the end: the server closes it
        c.close()
        self.assertIn('"phase": "done"', body)
        self.assertLess(time.time() - t, 5)

    def bb(self, *args):
        from test_bb import bb_env
        env = dict(bb_env(self.srv.data), PATH=os.path.join(self.f, "bin") + os.pathsep + clean_path())
        return subprocess.run([sys.executable, os.path.join(HERE, "..", "bb.py")] + list(args), input="",
                              capture_output=True, text=True, timeout=90, env=env, cwd=self.f)

    def test_bb_pairs_and_deploys(self):
        r = self.bb("pair", "you@box")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "paired box: gateway box, monitor box")
        self.assertIn("serving qwen3-8b", r.stderr)
        r = self.bb("racks")
        self.assertIn("this app's key", r.stdout)
        r = self.bb("deploy", "ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("* qwen3-8b", r.stdout)
        r = self.bb("deploy", "up", "qwen3-8b")                         # nobody to ask: it does not go ahead
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("Rerun with --yes", r.stderr)
        r = self.bb("deploy", "up", "qwen3-8b", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("serving qwen3-8b: healthy", r.stdout)

    def git_repo(self):
        """dgx-serve as the install clones it: a repo with the stand-in rack."""
        repo = os.path.join(self.f, "dgx-serve.git")
        os.makedirs(repo)
        shutil.copy(os.path.join(self.f, "bin", "rack"), os.path.join(repo, "rack"))
        git = lambda *a: subprocess.run(["git", "-C", repo] + list(a), check=True, capture_output=True)
        git("init", "-q")
        git("add", "rack")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "rack")
        return repo

    def install(self, target):
        frames = self.srv.sse("/api/racks", method="POST", body={"action": "install", "target": target}, timeout=60,
                              until=lambda fr: json.loads(fr[-1]["data"]).get("phase") == "done")
        return [json.loads(x["data"]) for x in frames]

    def test_install_over_ssh_then_pair(self):
        repo = self.git_repo()
        os.remove(os.path.join(self.f, "bin", "rack"))
        self.srv.stop()
        self.srv = Server(os.path.join(self.f, "data"), {}, env={
            "PATH": os.path.join(self.f, "bin") + os.pathsep + clean_path(),
            "BYTEBUNKER_DGX_SERVE_GIT": repo})
        errs = [x["error"] for x in self.pair() if x.get("error")]
        self.assertTrue(errs and "dgx-serve is not on you@box" in errs[0], errs)
        frames = self.install("you@box")
        lines = [x.get("line") for x in frames if x.get("line")]
        self.assertIn("downloading dgx-serve into %s/home/dgx-serve (git clone)" % self.f, lines)
        self.assertIn("box: a DGX Spark, ready to serve", lines)
        self.assertEqual([x["error"] for x in frames if x.get("error")], [])
        got = self.found(frames)
        self.assertEqual(got["engine"]["url"], "http://127.0.0.1:%d" % self.eng.port)
        self.assertEqual(len(self.keys()), 1)
        again = [x.get("line") for x in self.install("you@box")]     # a second time: it is there already
        self.assertIn("dgx-serve is here already: rack 1.0.0-test", again)

    def test_install_says_what_is_missing(self):
        os.remove(os.path.join(self.f, "bin", "rack"))
        os.makedirs(os.path.join(self.f, "home", "dgx-serve"))       # a folder in the way
        frames = self.install("you@box")
        self.assertIn("is there and is not dgx-serve", " ".join(x.get("error") or "" for x in frames))
        self.assertNotEqual(frames[-1]["exit"], 0)

    def test_machines_find_then_one_click_deploy(self):
        cfgfile = os.path.join(self.f, "ssh_config")
        with open(cfgfile, "w") as f:
            f.write("Host box spark-x\n  HostName 127.0.0.1\nHost github.com\nHost w1\n  HostName 127.0.0.3\n")
        self.srv.stop()
        self.srv = Server(os.path.join(self.f, "data"), {}, env={
            "PATH": os.path.join(self.f, "bin") + os.pathsep + clean_path(),
            "BYTEBUNKER_SSH_CONFIG": cfgfile, "BYTEBUNKER_TAILNET": "off", "BYTEBUNKER_LOCAL_RACK": "off"})
        st, d = self.srv.request("POST", "/api/machines", {"action": "find"})
        self.assertEqual(st, 200, d)
        m, w1 = d["machines"]                                          # github.com is not a computer
        self.assertEqual((m["target"], m["aliases"], m["ok"]), ("box", ["box", "spark-x"], True), m)
        self.assertTrue(m["rack"], m)                                  # the stand-in rack is on its PATH
        self.assertTrue(d["here"]["ok"], d["here"])
        st, again = self.srv.request("GET", "/api/machines")
        self.assertEqual(again["at"], d["at"])
        frames = self.srv.sse("/api/machines", method="POST", body={"action": "deploy", "target": "box", "recipe": "qwen3-8b"},
                              timeout=60, until=lambda fr: json.loads(fr[-1]["data"]).get("phase") == "done")
        frames = [json.loads(x["data"]) for x in frames]
        lines = [x.get("line") for x in frames if x.get("line")]
        self.assertEqual(frames[-1]["exit"], 0, frames)
        self.assertIn("serving qwen3-8b: healthy", lines)
        self.assertIn("this app talks to it now: gateway box, monitor box", lines)
        self.assertEqual(next(x["deployed"] for x in frames if x.get("deployed"))["rack"], "box")
        cfg = json.loads(text(self.srv.cfg))
        self.assertEqual([r["name"] for r in cfg["racks"]], ["box"])
        self.assertEqual([g["name"] for g in cfg["gateways"]], ["box"])
        st, d = self.srv.request("POST", "/api/machines", {"action": "find"})
        self.assertEqual([(x["target"], x["paired_as"], x["worker_of"]) for x in d["machines"]],
                         [("box", "box", ""), ("w1", "", "box")])
        st, r = self.srv.request("POST", "/api/machines", {"action": "deploy", "target": "w1", "recipe": "qwen3-8b"})
        self.assertEqual(st, 409, r)
        self.assertIn("a worker in box's rack", r["error"])
        for bad in ({"action": "deploy", "target": "-oProxyCommand=x", "recipe": "a"},
                    {"action": "deploy", "target": "box", "recipe": "a b"}):
            st, r = self.srv.request("POST", "/api/machines", bad)
            self.assertEqual(st, 400, bad)

    def test_agent_plane_found_machines_then_work_here(self):
        cfgfile = os.path.join(self.f, "ssh_config")
        with open(cfgfile, "w") as f:
            f.write("Host box\n  HostName 127.0.0.1\nHost worker1\n  HostName 127.0.0.7\n")
        for name, body in (("podman", 'case "$1" in --version) echo "podman version 5.0.0";; build) echo "built";; *) exit 0;; esac'),
                           ("uv", 'echo "uv 0.11.0"')):
            script(os.path.join(self.f, "bin", name), "#!/bin/sh\n" + body + "\n")
        src = os.path.join(self.f, "harness")
        os.makedirs(os.path.join(src, "scripts"))
        os.makedirs(os.path.join(src, "config"))
        with open(os.path.join(src, "scripts", "run_master.py"), "w") as f:
            f.write("# --goal-id --proto --spec\n")
        with open(os.path.join(src, "config", "config.example.yaml"), "w") as f:
            f.write("sandbox:\n  runner: docker\n")
        git = lambda *a: subprocess.run(["git", "-C", src] + list(a), check=True, capture_output=True)
        git("init", "-q"); git("add", "."); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "h")
        self.srv.stop()
        self.srv = Server(os.path.join(self.f, "data"), {}, env={
            "PATH": os.path.join(self.f, "bin") + os.pathsep + clean_path(), "BYTEBUNKER_HARNESS_SRC": src,
            "BYTEBUNKER_SSH_CONFIG": cfgfile, "BYTEBUNKER_TAILNET": "off", "BYTEBUNKER_LOCAL_RACK": "off"})
        got = self.found(self.pair())                                  # box serves models once it is paired
        self.srv.request("POST", "/api/racks", {"action": "save", "pairing": got["pairing"]})
        self.srv.request("POST", "/api/machines", {"action": "find"})
        st, d = self.srv.request("GET", "/api/agents/planes")
        kinds = {p["target"]: (p["kind"], p["selectable"]) for p in d["planes"]}
        self.assertEqual(kinds["local"], ("this computer", True))
        self.assertEqual(kinds["box"], ("model plane", False))
        self.assertEqual(kinds["worker1"], ("machine", True))
        st, r = self.srv.request("POST", "/api/agents", {"action": "plane", "target": "box"})
        self.assertEqual(st, 409, r)
        frames = self.srv.sse("/api/agents", method="POST", body={"action": "plane", "target": "worker1"}, timeout=60,
                              until=lambda fr: json.loads(fr[-1]["data"]).get("phase") == "done")
        frames = [json.loads(x["data"]) for x in frames]
        lines = [x.get("line") for x in frames if x.get("line")]
        self.assertEqual(frames[-1]["exit"], 0, frames)
        # the stand-in ssh runs here: a Mac picks Seatbelt (nothing to install, no image); elsewhere,
        # without bubblewrap, the stand-in podman
        # (a Linux CI runner may have, or install, bubblewrap: then it is that, with no image either)
        mac = sys.platform == "darwin"
        light = mac or any(l.startswith("sandbox: bubblewrap") for l in lines)
        self.assertIn("sandbox: Seatbelt, macOS's own: nothing to install" if mac else
                      next((l for l in lines if l.startswith("sandbox: bubblewrap")), "sandbox: podman version 5.0.0"), lines)
        runner = "auto" if light else "podman"
        self.assertIn("config/config.yaml made from the example (runner: %s)" % runner, lines)
        self.assertIn("agents work on worker1 now", lines)
        with open(os.path.join(self.f, "home", "bytebunker-harness", "config", "config.yaml")) as f:
            self.assertIn("runner: " + runner, f.read())
        cfg = json.loads(text(self.srv.cfg))
        self.assertEqual((cfg["agents"]["enabled"], cfg["agents"]["ssh"]), (True, "worker1"))
        st, d = self.srv.request("GET", "/api/agents/planes")
        self.assertTrue(next(p for p in d["planes"] if p["target"] == "worker1")["current"])

    def test_removing_a_rack_takes_its_key_back(self):
        got = self.found(self.pair())
        self.srv.request("POST", "/api/racks", {"action": "save", "pairing": got["pairing"]})
        self.assertEqual(len(self.keys()), 1)
        st, r = self.srv.request("POST", "/api/racks", {"action": "remove", "name": "box", "gateway": True, "monitor": True})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["note"], "its key is off you@box")
        self.assertEqual(self.keys(), [])
        cfg = json.loads(text(self.srv.cfg))
        self.assertEqual((cfg["racks"], cfg["gateways"], cfg["monitors"]), ([], [], []))


@unittest.skipIf(sys.platform == "win32", "the stand-in rack is POSIX shell")
class ThisComputer(unittest.TestCase):
    """Deploy with no rack: this computer, before and after dgx-serve is here."""

    def setUp(self):
        self.f = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.f, "bin"))
        self.home = os.path.join(self.f, "home")
        os.makedirs(self.home)
        self.eng = FakeEngine(models=[{"id": "local-model"}], api_key="ek").start()
        script(os.path.join(self.f, "rack"), RACK.replace("__F__", self.f).replace("__EPORT__", str(self.eng.port))
               .replace("__MPORT__", "1").replace('"running":true', '"running":false'))
        self.repo = os.path.join(self.f, "dgx-serve.git")
        os.makedirs(self.repo)
        shutil.move(os.path.join(self.f, "rack"), os.path.join(self.repo, "rack"))
        for a in (["init", "-q"], ["add", "rack"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "r"]):
            subprocess.run(["git", "-C", self.repo] + a, check=True, capture_output=True)
        data = os.path.join(self.f, "data")
        os.makedirs(data)
        self.srv = Server(data, {}, env={"HOME": self.home, "BYTEBUNKER_LOCAL_RACK": "",
                                         "BYTEBUNKER_DGX_SERVE_GIT": self.repo,
                                         "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})

    def tearDown(self):
        self.srv.stop()
        self.eng.stop()
        shutil.rmtree(self.f, ignore_errors=True)

    def test_install_here_then_serve_adds_the_gateway(self):
        st, d = self.srv.request("GET", "/api/rack")
        self.assertEqual((d["ok"], d["name"], d["local"], d["installed"]), (False, "local", True, False), d)
        self.assertTrue(d["platform"]["description"], d)
        self.assertEqual(d["racks"][-1]["name"], "local")
        st, r = self.srv.request("POST", "/api/rack", {"action": "up", "recipe": "qwen3-8b"})
        self.assertEqual(st, 400)
        self.assertIn("not on this computer yet", r["error"])
        frames = self.srv.sse("/api/racks", method="POST", body={"action": "install"}, timeout=60,
                              until=lambda fr: json.loads(fr[-1]["data"]).get("phase") == "done")
        frames = [json.loads(x["data"]) for x in frames]
        self.assertEqual(frames[-1]["exit"], 0, frames)
        self.assertTrue(any(x.get("installed") for x in frames))
        self.assertTrue(os.path.islink(os.path.join(self.home, ".local", "bin", "rack")))
        st, d = self.srv.request("GET", "/api/rack")
        self.assertEqual((d["ok"], d["local"], d["here"], d["rack_version"]), (True, True, "mac", "1.0.0-test"), d)
        self.assertEqual(d["platform"]["description"], "Mac (test)")
        q = d["recipes"][0]
        self.assertEqual((q["name"], q["here"], q["fits"], q["weights_gb"]), ("qwen3-8b", True, True, 5.0))
        self.assertFalse(d["recipes"][1]["here"])
        out = self.srv.sse("/api/rack", method="POST", body={"action": "up", "recipe": "qwen3-8b", "rack": "local"},
                           timeout=30, until=lambda fr: json.loads(fr[-1]["data"]).get("phase") == "done")
        lines = [json.loads(x["data"]).get("line") for x in out]
        self.assertIn("serving qwen3-8b here: healthy", lines)
        self.assertIn("this app talks to it now: gateway this computer", lines)
        cfg = json.loads(text(self.srv.cfg))
        self.assertEqual([(g["name"], g["url"], g["key"]) for g in cfg["gateways"]],
                         [("this computer", "http://127.0.0.1:%d/v1" % self.eng.port, "ek")])
        self.assertEqual(cfg.get("racks") or [], [])                   # this computer is never a configured rack
        self.assertIn("local-model", [m["id"] for m in self.srv.request("GET", "/api/models")[1]["data"]])


if __name__ == "__main__":
    unittest.main()
