# ByteBunker runbook — reproduce the whole deployment, command by command

Each block says where it runs: `laptop$`, `hermes$` (the Mac mini, user `mo`), `spark-1$` / `spark-2$` (DGX Sparks, user `trickyfalcon`), `PS>` (admin PowerShell on the Windows box), `wsl$` (the Ubuntu WSL2 VM on that box, user `trickyfalcon`). Replace `<LITELLM_KEY>` with the litellm master key from `~/dgx/dgx-spark-setup/.env` on spark-1. Addresses are the live ones (LAN 172.16.25.0/24, Spark direct link 192.168.100.0/24, tailnet `tailed338.ts.net`).

The order below is the dependency order: model plane → control plane → agent plane → dashboards → smoke tests.

---

## 1. Model plane: spark-1 and spark-2

### 1.1 Host prep (both Sparks) — `dgx-spark-setup`
```
spark-1$ git clone https://github.com/bytebunkerlabs/dgx-spark-setup.git ~/dgx/dgx-spark-setup
spark-1$ cd ~/dgx/dgx-spark-setup && cp .env.example .env      # set LITELLM_MASTER_KEY and image tags
spark-1$ ./dgxsetup preflight
spark-1$ ./dgxsetup system            # docker group, tooling; open a new shell after
spark-1$ ./dgxsetup security          # ufw, fail2ban, ssh hardening, Tailscale
spark-1$ ./dgxsetup models            # HF cache
spark-1$ ./dgxsetup inference         # litellm gateway (+ Open WebUI)
spark-1$ ./dgxsetup monitoring        # Prometheus + Grafana + node/GPU exporters
spark-1$ ./dgxsetup health
```
Repeat `system`, `security`, `monitoring` on spark-2. Then add spark-2's exporters as scrape targets in `~/dgx/dgx-spark-setup/config/prometheus.yml` on spark-1:
```yaml
  - job_name: spark-2-node
    static_configs: [{ targets: ['192.168.100.2:9100'] }]
  - job_name: spark-2-gpu
    static_configs: [{ targets: ['192.168.100.2:9835'] }]
```
```
spark-1$ cd ~/dgx/dgx-spark-setup && docker compose -p dgx-monitoring restart prometheus
spark-1$ curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python3 -m json.tool | grep -E 'job|value' | head
spark-1$ docker ps --format '{{.Names}}\t{{.Ports}}'
# dgx-inference-litellm-1  127.0.0.1:4000, 172.16.25.186:4000, 100.90.164.11:4000
# dgx-monitoring-prometheus-1 127.0.0.1:9090 · grafana 127.0.0.1:3001 · node-exporter :9100 · nvidia-gpu-exporter :9835
```

### 1.2 The engine — `dgx-spark-serve` and `rack`
```
spark-1$ git clone https://github.com/bytebunkerlabs/dgx-spark-serve.git ~/dgx/dgx-spark-serve
spark-1$ cd ~/dgx/dgx-spark-serve && cp .env.example .env         # HEAD_IP=192.168.100.1, WORKER_SSH=spark-2, API_PORT=8888
spark-1$ rsync -a --exclude mods . spark-2:dgx/dgx-spark-serve/
spark-1$ ./rack install                                            # symlink into ~/.local/bin
spark-1$ rack preflight                                            # both nodes must PASS
spark-1$ rack build community                                      # image with sm_121 kernels, shipped to spark-2
spark-1$ rack fit  orcarouter/DeepSeek-V4-Flash-Vision-Uncensored
spark-1$ rack pull orcarouter/DeepSeek-V4-Flash-Vision-Uncensored  # download + replicate + verify
spark-1$ rack recipes                                              # dsv4-vision-ab is the one in use
spark-1$ rack up dsv4-vision-ab --debug                            # TP=2; registers GATEWAY_NAME with litellm
spark-1$ rack status
spark-1$ curl -s http://127.0.0.1:8888/v1/models | python3 -m json.tool | grep id
```
The recipe `recipes/dsv4-vision-ab.env` is the source of truth for the engine flags. Two of them matter to everything downstream:
```
--max-model-len 262144
--default-chat-template-kwargs '{"thinking":true,"reasoning_effort":"max"}'   # thinking ON by default; clients send thinking:false to turn it off
```
Gateway check from any LAN host:
```
laptop$ curl -s -H "Authorization: Bearer <LITELLM_KEY>" http://172.16.25.186:4000/v1/models | python3 -c 'import sys,json; print([m["id"] for m in json.load(sys.stdin)["data"]])'
laptop$ curl -s -H "Authorization: Bearer <LITELLM_KEY>" -H "Content-Type: application/json" http://172.16.25.186:4000/v1/chat/completions \
  -d '{"model":"deepseek-v4-vision-uncensored","messages":[{"role":"user","content":"one word: ready?"}],"max_tokens":20,"chat_template_kwargs":{"thinking":false}}'
```

### 1.3 If spark-2's GPU metrics go flat
```
spark-2$ docker restart monitoring-nvidia-gpu-exporter-1      # a long-lived container loses NVML
```

---

## 2. Control plane: hermes

### 2.1 ssh from hermes to the other hosts
```
hermes$ ssh-keygen -t ed25519 -C "mo@mos-Mac-mini" -f ~/.ssh/id_ed25519 -N ""      # skip if it exists
hermes$ cat >> ~/.ssh/config <<'SSHCFG'
Host agents-worker
    HostName 100.100.129.98
    User trickyfalcon
Host spark-1
  HostName 172.16.25.186
  User trickyfalcon
SSHCFG
hermes$ ssh-copy-id trickyfalcon@172.16.25.186
hermes$ ssh-copy-id agents-worker            # after §3.3 (sshd + tailscale in the VM)
hermes$ ssh -o BatchMode=yes spark-1 hostname && ssh -o BatchMode=yes agents-worker hostname
```

### 2.2 The console
Either the installer (`curl -fsSL https://raw.githubusercontent.com/bytebunkerlabs/bytebunker-console/main/install.sh | bash -s -- --upstream http://172.16.25.186:4000/v1 --key <LITELLM_KEY>`) or the manual path that reproduces the live box:
```
hermes$ git clone https://github.com/bytebunkerlabs/bytebunker-console.git ~/bytebunker-console
hermes$ cd ~/bytebunker-console && mkdir -p data && cp config.json.example config.json
```
Edit `~/bytebunker-console/config.json` to this (the live values, key redacted):
```json
{
  "bind": "127.0.0.1", "port": 8765,
  "gateways": [{"name": "upstream", "url": "http://172.16.25.186:4000/v1", "key": "<LITELLM_KEY>", "enabled": true, "kind": "litellm"}],
  "model_capabilities": { "qwen3-4b-fast": { "tools": true, "effort": [], "ctk": { "enable_thinking": false }, "strip_reasoning": true, "ctx": 24576 } },
  "prometheus_url": "http://127.0.0.1:19090",
  "nodes": [
    { "name": "spark-1", "instance": "(node-exporter|nvidia-gpu-exporter|localhost)", "spec": "GB10 Grace Blackwell · 128 GB unified" },
    { "name": "spark-2", "instance": "192.168.100.2", "spec": "GB10 Grace Blackwell · 128 GB unified" } ],
  "sparkdash_url": "http://127.0.0.1:15555", "telemetry_source": "sparkdash",
  "sparkdash_open_url": "https://burhan.tailed338.ts.net",
  "skills_dirs": ["~/bytebunker-console/skills-harness"], "plugins_dirs": [], "plugins": {},
  "agents": {
    "enabled": true, "ssh": "agents-worker", "dir": "~/bytebunker-harness",
    "python": "/home/trickyfalcon/.local/bin/uv run", "script": "scripts/run_master.py",
    "master_name": "Sultan", "master_instructions": "<see 2.5>", "run_timeout_s": 10800,
    "worker_model_metrics": "http://127.0.0.1:8001/metrics", "worker_model_label": "RTX 2070 on leagueofash" },
  "litellm": { "ssh": "spark-1", "config_path": "/home/trickyfalcon/dgx/dgx-spark-setup/config/litellm.config.yaml", "container": "dgx-inference-litellm-1" },
  "rack": { "ssh": "spark-1", "dir": "~/dgx/dgx-spark-serve" },
  "frontier_rates_per_mtok": { "input": 3.0, "output": 15.0 },
  "identity": { "user": "mo@bunker", "host": "mini" }
}
```
Service:
```
hermes$ cat > ~/Library/LaunchAgents/ai.bytebunker.console.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.bytebunker.console</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/Users/mo/bytebunker-console/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/mo/bytebunker-console</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/mo/bytebunker-console/data/console.log</string>
  <key>StandardErrorPath</key><string>/Users/mo/bytebunker-console/data/console.log</string>
</dict></plist>
PLIST
hermes$ launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.bytebunker.console.plist
hermes$ curl -s http://127.0.0.1:8765/api/models | head -c 200
```
Restart after server-side changes (never while an agent run streams): `hermes$ launchctl kickstart -k gui/501/ai.bytebunker.console`.

### 2.3 The tunnel to spark-1 (Prometheus, H3, sparkDash)
```
hermes$ cat > ~/Library/LaunchAgents/ai.bytebunker.tunnel.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.bytebunker.tunnel</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/ssh</string><string>-N</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-L</string><string>127.0.0.1:19090:127.0.0.1:9090</string>
    <string>-L</string><string>127.0.0.1:18091:127.0.0.1:8091</string>
    <string>-L</string><string>127.0.0.1:15555:127.0.0.1:5555</string>
    <string>trickyfalcon@172.16.25.186</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer>
</dict></plist>
PLIST
hermes$ launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.bytebunker.tunnel.plist
hermes$ curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:19090/api/v1/query?query=up'
```
To change forwards later: edit the plist, then `launchctl bootout gui/501/ai.bytebunker.tunnel; launchctl bootstrap gui/501 ~/Library/LaunchAgents/ai.bytebunker.tunnel.plist` (kickstart alone does not re-read the arguments).

### 2.4 The skills copy the console lists
```
laptop$ cd ~/Documents/AI/bytebunker-harness && COPYFILE_DISABLE=1 tar c --no-xattrs skills | ssh hermes 'mkdir -p ~/bytebunker-console/skills-harness && cd ~/bytebunker-console/skills-harness && tar x --strip-components=1; find . -name "._*" -delete'
hermes$ curl -s http://127.0.0.1:8765/api/skills | python3 -c 'import sys,json; print([s["name"] for s in json.load(sys.stdin)["skills"]])'
```

### 2.5 The Sultan's standing instructions
Paste on the Agents screen (Name: `Sultan`), or set `agents.master_instructions` in config.json:
```
You are the Sultan: you decide, delegate, and synthesise; you never do the legwork yourself. Give every agent you spawn a short human first name and use it in your updates.

Your court:
- minion: EXECUTION. One well-defined task each, quick depth, tight brief. Spawn as many as the task has independent parts and run them in parallel.
- wazir: DEEP THINKING. One wazir when a problem is hard, ambiguous or has many moving parts: to decompose the goal into a plan before you dispatch minions, or to weigh conflicting findings. Never for trivia.
- malikah (the queen): FINAL JUDGMENT. Before you give the human your final answer on any multi-part or research goal, write the synthesis yourself (one report, all sources), then spawn malikah with that report and the evidence the minions returned as material. If she says REVISE, make her corrections and resubmit once.

Multi-step goals: first decide the steps (use a wazir if the plan is not obvious), dispatch minions per step (in parallel when independent, in sequence when one depends on another), collect and read every result, then synthesise. Report progress to the human as steps complete, and report the final answer as soon as Malikah approves. Research needs network=true and the web-research skill. Give any networked slave timeout_s of at least 900. Never spawn a slave without a self-contained brief.

Choosing the role: gathering facts about a KNOWN item (a specific CVE, product version, config setting) is minion work at depth=quick, one item per minion. Use researcher only for open-ended investigation where the sources themselves are unknown. Use depth=deep only for the wazir and malikah. Give research minions timeout_s=900.
For any CVE or vulnerability lookup, spawn the minion with skills=["cve-lookup"] (API routes; the HTML pages are bot-gated).
```

---

## 3. Agent plane: the Windows box and its WSL2 VM

### 3.1 Windows: WSL2 and the GPU
```
PS> wsl --install -d Ubuntu                       # Windows Server 2022+ / Windows 11
PS> # older Server builds instead:
PS> dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
PS> dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
PS> wsl --set-default-version 2
```
Install the NVIDIA Windows driver (the live box runs 610.62; any driver ≥ 470 exposes the GPU to WSL2). No CUDA toolkit is needed in Windows. Check from the VM later with `/usr/lib/wsl/lib/nvidia-smi`.

### 3.2 Inside the VM: base system
```
wsl$ sudo tee /etc/wsl.conf <<'WSLCONF'
[boot]
systemd=true
WSLCONF
PS> wsl --shutdown          # then reopen the VM so systemd is PID 1
wsl$ sudo apt update && sudo apt install -y openssh-server podman uidmap slirp4netns fuse-overlayfs build-essential git curl python3
wsl$ sudo systemctl enable --now ssh
wsl$ grep "^$USER:" /etc/subuid /etc/subgid || sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER
wsl$ podman system migrate && podman info --format '{{.Host.Security.Rootless}} {{.Store.GraphDriverName}}'
wsl$ sudo loginctl enable-linger $USER              # rootless podman over non-interactive ssh needs this
wsl$ sed -i '1i export PATH=/usr/lib/wsl/lib:$HOME/.local/bin:$PATH' ~/.bashrc   # ssh command sessions find nvidia-smi and uv
wsl$ curl -LsSf https://astral.sh/uv/install.sh | sh
wsl$ /usr/lib/wsl/lib/nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader
```

### 3.3 Tailscale inside the VM (this is how hermes and spark-1 reach it)
```
wsl$ curl -fsSL https://tailscale.com/install.sh | sh
wsl$ sudo tailscale up            # approve in the browser; the VM becomes node "agents-worker"
wsl$ tailscale ip -4              # 100.100.129.98 on the live box
```
Authorise the two machines that ssh in:
```
hermes$  ssh-copy-id agents-worker
spark-1$ cat ~/.ssh/id_ed25519_shared.pub | ssh -o StrictHostKeyChecking=accept-new trickyfalcon@100.100.129.98 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
spark-1$ ssh -o BatchMode=yes trickyfalcon@100.100.129.98 'nvidia-smi --query-gpu=name --format=csv,noheader'
```

### 3.4 The harness
```
wsl$ ssh-keygen -t ed25519 -f ~/.ssh/harness_deploy -N "" -C agents-worker     # add the .pub as a read-only deploy key on the private repo
wsl$ cat >> ~/.ssh/config <<'SSHCFG'
Host github.com
  IdentityFile ~/.ssh/harness_deploy
  IdentitiesOnly yes
SSHCFG
wsl$ git clone git@github.com:bytebunkerlabs/bytebunker-harness.git ~/bytebunker-harness
wsl$ cd ~/bytebunker-harness && uv sync
```
`config/config.yaml` (live values, key redacted):
```yaml
llm:
  base_url: "http://172.16.25.186:4000/v1"      # LAN address of litellm, never the tailnet
  api_key: "<LITELLM_KEY>"
  master_model: "deepseek-v4-vision-uncensored"
  default_slave_model: "qwen3-4b-fast"          # set after §3.5; use the Spark model until then
  thinking_model: "deepseek-v4-vision-uncensored"
  timeout_s: 900
  master_max_tokens: 8000
  slave_transcript_chars: 36000
  fetch_max_chars: 8000

console_url: ""

network:
  default: false
  allowed_roles: [internet_agent, researcher, minion, wazir, malikah]

concurrency:
  max_concurrent_slaves: 3
  max_slaves_per_goal: 16
  default_slave_timeout_s: 1800
  master_digest_interval_s: 300

sandbox:
  runner: podman
  image: bytebunker-slave:latest
  memory: 2g
  cpus: 2.0
  llm_bridge: uds

skills_dir: skills
trajectories_dir: trajectories
```
Build the slave image and smoke-test a goal without the console:
```
wsl$ cd ~/bytebunker-harness && podman build -f docker/Dockerfile.slave -t bytebunker-slave:latest .
wsl$ uv run pytest -q
wsl$ PYTHONUNBUFFERED=1 BYTEBUNKER_MASTER_NAME=Sultan uv run scripts/run_master.py --goal "Spawn one minion (quick) whose brief is: reply with the single word READY. Then give me its answer."
wsl$ tail -1 trajectories/spawns.jsonl | python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d["role"], d["success"], d["tokens"], d.get("model"))'
```

### 3.5 The small model on the RTX 2070
Either the console (Recipes → vLLM on a CUDA GPU → host `agents-worker`, LAN address `172.16.25.83` → Deploy), or by hand, which is exactly what the recipe does:
```
wsl$ uv venv ~/vllm-env --python 3.12
wsl$ uv pip install --python ~/vllm-env/bin/python vllm huggingface_hub
wsl$ ~/vllm-env/bin/python -c 'from huggingface_hub import snapshot_download; print(snapshot_download("Qwen/Qwen3-4B-AWQ"))'
wsl$ cat > ~/vllm-qwen3-4b-fast.sh <<'SERVE'
#!/bin/bash
export PATH=/usr/lib/wsl/lib:$PATH
exec ~/vllm-env/bin/vllm serve Qwen/Qwen3-4B-AWQ --host 0.0.0.0 --port 8001 --served-model-name qwen3-4b-fast --max-model-len 24576 --max-num-seqs 3 --gpu-memory-utilization 0.85 --dtype half --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3
SERVE
wsl$ chmod +x ~/vllm-qwen3-4b-fast.sh && mkdir -p ~/.config/systemd/user
wsl$ cat > ~/.config/systemd/user/vllm-qwen3-4b-fast.service <<'UNIT'
[Unit]
Description=vLLM qwen3-4b-fast (Qwen/Qwen3-4B-AWQ)
After=network.target

[Service]
ExecStart=%h/vllm-qwen3-4b-fast.sh
Restart=on-failure
RestartSec=15
StandardOutput=append:%h/vllm-qwen3-4b-fast.log
StandardError=append:%h/vllm-qwen3-4b-fast.log

[Install]
WantedBy=default.target
UNIT
wsl$ systemctl --user daemon-reload && systemctl --user enable --now vllm-qwen3-4b-fast.service
wsl$ until curl -s -m 3 http://127.0.0.1:8001/v1/models | grep -q qwen3-4b-fast; do sleep 10; done; echo up
wsl$ curl -s http://127.0.0.1:8001/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"qwen3-4b-fast","messages":[{"role":"user","content":"one word: ready?"}],"max_tokens":20,"chat_template_kwargs":{"enable_thinking":false}}'
```
Turing (compute 7.5) runs vLLM's Triton attention backend and needs `--dtype half`; the first start compiles kernels, so it takes about two minutes.

### 3.6 Windows: expose the model on the LAN and register it
The VM's address is NAT'd and changes on reboot; forward the Windows LAN port to it:
```
wsl$ ip -4 -o addr show eth0 | awk '{print $4}'      # e.g. 172.22.36.141/20
PS> netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8001 connectaddress=172.22.36.141 connectport=8001
PS> netsh advfirewall firewall add rule name="vllm-8001" dir=in action=allow protocol=TCP localport=8001
PS> netsh interface portproxy show all
spark-1$ curl -s -m 5 http://172.16.25.83:8001/v1/models
```
After a reboot with a new VM address: `PS> netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=8001` and add it again.

Register in litellm (the console's Recipes → Preview → Register does exactly this):
```
spark-1$ F=~/dgx/dgx-spark-setup/config/litellm.config.yaml; cp $F $F.bak-$(date +%Y%m%d-%H%M)
spark-1$ python3 - <<'PY'
import re
p="/home/trickyfalcon/dgx/dgx-spark-setup/config/litellm.config.yaml"; s=open(p).read()
entry='''  - model_name: qwen3-4b-fast
    litellm_params:
      model: openai/qwen3-4b-fast
      api_base: http://172.16.25.83:8001/v1
      api_key: "not-needed"

'''
if "qwen3-4b-fast" not in s:
    m=re.search(r'(?m)^model_list:\s*$', s); s=s[:m.end()]+"\n"+entry+s[m.end():]; open(p,"w").write(s); print("added")
PY
spark-1$ docker restart dgx-inference-litellm-1 && sleep 8
laptop$ curl -s -H "Authorization: Bearer <LITELLM_KEY>" http://172.16.25.186:4000/v1/models | grep -o qwen3-4b-fast
```
Then set `default_slave_model: "qwen3-4b-fast"` in the harness config (§3.4). No image rebuild: the model name travels in each spawn's input.

### 3.7 Skills on the worker (the agents read these)
The harness repo carries `skills/{web-research,cve-lookup,deep-analysis,code-review}`. A skill created on the console's Skills screen is written to hermes and mirrored here over ssh as `~/bytebunker-harness/skills/<name>/SKILL.md`. To push skills from the laptop by hand:
```
laptop$ cd ~/Documents/AI/bytebunker-harness && COPYFILE_DISABLE=1 tar c --no-xattrs skills | ssh hermes 'ssh agents-worker "cd ~/bytebunker-harness && tar x; find . -name \"._*\" -delete"'
```

---

## 4. sparkDash on spark-1 and in the console

```
spark-1$ git clone https://github.com/MiaAI-Lab/sparkDash.git ~/dgx/sparkDash && cd ~/dgx/sparkDash
spark-1$ cat > docker-compose.override.yml <<'OVR'
services:
  sparkdash:
    volumes:
      - ${HOME}/.ssh/id_ed25519_shared:/root/.ssh/id_ed25519:ro
OVR
spark-1$ docker compose up --build -d
spark-1$ curl -s http://127.0.0.1:5555/api/settings | head -c 120
```
Register the three units through its API:
```
spark-1$ B=http://127.0.0.1:5555
spark-1$ curl -s -X POST $B/api/sparks -H 'Content-Type: application/json' -d '{"id":"spark-1","name":"spark-1","kind":"spark","isLocal":true,"role":"head","lanIp":"172.16.25.186","cx7Ip":"192.168.100.1","llmPorts":[8888]}'
spark-1$ curl -s -X POST $B/api/sparks -H 'Content-Type: application/json' -d '{"id":"spark-2","name":"spark-2","kind":"spark","isLocal":false,"role":"worker","lanIp":"172.16.25.185","cx7Ip":"192.168.100.2","ssh":{"host":"172.16.25.185","user":"trickyfalcon","auth":"key"},"llmMonitoring":false}'
spark-1$ curl -s -X POST $B/api/sparks -H 'Content-Type: application/json' -d '{"id":"agents-worker","name":"agents-worker","kind":"host","isLocal":false,"role":"standalone","lanIp":"100.100.129.98","ssh":{"host":"100.100.129.98","user":"trickyfalcon","auth":"key"},"llmPorts":[8001]}'
spark-1$ sleep 15; for u in spark-1 spark-2 agents-worker; do curl -s $B/api/sparks/$u/metrics | python3 -c "import sys,json; d=json.load(sys.stdin); g=d['metrics']['gpu']; print('$u', d['online'], g['usage'], g['vram']['used'], '/', g['vram']['total'])"; done
```
Expose it on the tailnet (one-time; needs MagicDNS + HTTPS certificates enabled in the Tailscale admin console):
```
spark-1$ sudo tailscale set --operator=$USER
spark-1$ tailscale serve --bg --https=443 5555
spark-1$ tailscale serve status              # https://burhan.tailed338.ts.net → 127.0.0.1:5555
laptop$  curl -s -o /dev/null -w '%{http_code}\n' https://burhan.tailed338.ts.net/api/settings
```
The console keys for it are in §2.2 (`sparkdash_url` via the tunnel of §2.3, `telemetry_source`, `sparkdash_open_url`). The ByteBunker palette inside the dashboard is a local commit in `~/dgx/sparkDash` (`git log --oneline -1` → `theme: bytebunker light/dark palettes…`, files `src/components/ThemeSwitch.tsx`, `src/index.css`, `index.html`); after pulling upstream, `git rebase` onto it and `docker compose up --build -d`.

---

## 5. Smoke tests, end to end
```
hermes$ curl -s http://127.0.0.1:8765/api/agents | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["enabled"], d["host"], d["master_name"])'
hermes$ curl -s http://127.0.0.1:8765/api/telemetry | python3 -c 'import sys,json; [print(n["name"], n["kind"], n.get("util"), n.get("llm",{}).get("model")) for n in json.load(sys.stdin)["nodes"]]'
hermes$ curl -s http://127.0.0.1:8765/api/rack | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["serving"], len(d["recipes"]))'
hermes$ curl -s http://127.0.0.1:8765/api/recipes | python3 -c 'import sys,json; d=json.load(sys.stdin); print([r["id"] for r in d["recipes"]], d["litellm"])'
hermes$ cat > /tmp/goal.json <<'GOAL'
{"goal": "Give me the vulnerability details of CVE-2026-88778 (Citrix NetScaler): affected versions, whether it is exploited in the wild per CISA KEV, the fix, CVSS and CWE. Every claim needs a source URL. One quick minion is enough."}
GOAL
hermes$ curl -s -N --max-time 3600 -H 'Content-Type: application/json' -X POST http://127.0.0.1:8765/api/agents -d @/tmp/goal.json | sed 's/^data: //' | grep -v '^$'
hermes$ curl -s http://127.0.0.1:8765/api/agents/slaves | python3 -c 'import sys,json; d=json.load(sys.stdin); [print(s["id"], s["role"], s["success"], s["tokens"]) for s in d["slaves"][:5]]'
hermes$ curl -s http://127.0.0.1:8765/api/usage | python3 -c 'import sys,json; a=json.load(sys.stdin)["agents"]; print(a["total"], a["goals"], a["by_model"])'
```
Expected: the goal spawns one minion on `qwen3-4b-fast`, it calls `cve_record` and `kev_lookup`, and the Sultan answers in about six minutes with "not in KEV" and the patched builds 14.1-73.37 / 13.1-64.23.

---

## 6. Day-2: updates, backups, rollback

**Update the console** (laptop checkout → hermes):
```
laptop$ cd ~/Documents/AI/bytebunker-console && git pull
laptop$ scp server.py agents.py recipes.py skills.py traces.py mcp.py mcp_terminal.py hermes:bytebunker-console/ && scp public/* hermes:bytebunker-console/public/
laptop$ ssh hermes 'ssh agents-worker "pgrep -fc \"python scripts/run_maste[r].py\""; launchctl kickstart -k gui/501/ai.bytebunker.console'
```
**Update the harness** (laptop checkout → worker; the worker's deploy key has a passphrase, so `git pull` there fails non-interactively):
```
laptop$ cd ~/Documents/AI/bytebunker-harness && git pull && uv run pytest -q
laptop$ COPYFILE_DISABLE=1 tar c --no-xattrs src scripts docker skills | ssh hermes 'ssh agents-worker "cd ~/bytebunker-harness && tar x; find . -name \"._*\" -delete; podman build -q -f docker/Dockerfile.slave -t bytebunker-slave:latest ."'
```
**Update the small model** (new model or flags): edit `~/vllm-qwen3-4b-fast.sh`, `systemctl --user restart vllm-qwen3-4b-fast`, watch `~/vllm-qwen3-4b-fast.log`; or change the recipe parameters on the Recipes screen, stop the unit, Deploy.

**Change what the Sparks serve**: Recipes screen → rack card, or `spark-1$ rack up <recipe>` (`rack down` first when switching between TP=2 recipes).

**Backups worth taking**: hermes `~/bytebunker-console/config.json` and `data/` (traces, usage, sessions); worker `~/bytebunker-harness/config/config.yaml` and `trajectories/`; spark-1 `~/dgx/dgx-spark-setup/config/litellm.config.yaml` (the console keeps `*.bak-*` copies beside it) and `~/dgx/sparkDash/config/`.

**Rollback**: console files are plain copies, so `git checkout <sha>` on the laptop and re-copy; harness image tags are not versioned, so rebuild from the checkout at the wanted commit; litellm from its `.bak-*`.
