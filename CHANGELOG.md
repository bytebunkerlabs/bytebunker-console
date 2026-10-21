# Changelog

What changed in each version of ByteBunker, newest first. After an update the
app opens on the new version's section, and Settings → What's new shows them all.

## 0.8.0 — 2026-10-21

### Safer
- **Only this app and bb reach its API.** Other accounts on the same
  computer could call ByteBunker on 127.0.0.1; now every API request needs
  the server's own token. The app's window and `bb` carry it by themselves. A
  browser tab gets it from a one-time link (two minutes, once): `bb open`.
  A tab left open across a restart asks for a new link instead of failing
  quietly. Commands a model runs in a chat never see the token.
- **Deploy refuses a rack's worker even when its head is not paired.**
  Find machines now reads what each machine serves: a head names its recipe
  ("serving glm53-flash-keys now"), and a worker, which runs its share of a
  model served from another machine, has no Deploy button. Deploying on a
  machine that serves something names what will stop.
- **Agents do not start on models nobody serves, even the worker's own.**
  A role left blank here, or a worker whose harness is too old to take the
  models picked here, falls back to the worker's config.yaml: those models are
  checked too, and a goal that would fail at its first call says so and how to
  fix it.

### Changed
- **Chats run on the server by default.** A turn keeps going when the tab
  closes, and the app and bb follow each other's turns live. Settings →
  Chats turns it off.
- **Settings in tabs**: General, Models & chats, Sandbox, Command line.
- **Gateways**: Add a gateway and Find engines open from two buttons.

### New
- **What a machine needs, installed from Deploy.** Find machines says what
  is missing (python3, git and curl; Docker on Linux; Apple's command line
  tools on a Mac), and **Install in a terminal** opens a terminal with the one
  line that installs it, where it can ask for your password. Deploy waits
  until it is there. Terminals open on macOS, Linux and Windows now, for the
  agents' installs too.
- **`bb open`**: the app in your browser, with a one-time link.

### Fixed
- Find machines never saw python3 (a probe key with a digit was dropped).

### Also in this release (from 0.7.3, which was not published)
- **Private skill + tool bundles from your own GitHub.** Add a plugin from a
  git URL, then **Sync from GitHub** on its card pulls new skills and MCP
  servers in place — it shows the remote and how many commits it is behind. In
  bb: `bb plugins add|sync|enable|disable|remove`. Enabling or syncing a bundle
  also copies its skills to the agent worker, so the court can attach them on
  the next run.
- **Connect a git host for private repos.** A private repo now opens a short
  *Connect* step — paste a read-only access token (any host: GitHub, GitLab,
  self-hosted) and it installs; SSH `git@` keeps working with no token. The
  token lives in a new owner-only `secrets.json`, never in `config.json`, logs
  or the audit trail, and reaches git through an env-fed helper, never the
  saved URL. A **Connected** line shows linked hosts; `bb git connect|ls|
  disconnect` mirrors it. A one-click "Connect with GitHub" sign-in is next.
- **Burp Suite in the tools catalog.** One-click add for Burp's MCP Server
  extension (HTTP/1.1 and HTTP/2 requests, Repeater, Intruder, proxy history,
  scanner issues, Collaborator), through a built-in stdio bridge. Share it
  under Access to let agents use it too, with their sandboxes still offline.

## 0.7.2 — 2026-10-13

### New
- **The answer, on top of the Agents screen.** What you asked, the master's
  answer from its workers' results (tables and links shown properly, a Copy
  button), how long it took, or how it ended without one.
- **Thinking, per role**, in Agents > Settings: how hard the master thinks
  while it plans and spawns (quick by default), and while it answers; the
  thinking roles; the minions.

### Faster
- **A simple question is three steps**: plan and spawn, one wait that brings
  back every worker's answer, the answer. Measured on GLM: 49 s, three rounds
  (before: 83 s to the first spawn, then four or more rounds).
- **The master plans with a quick think** and thinks fully once results come
  back.
- **No second check of an answer already checked** by the worker's own judge;
  the queen's review only for a deep, multi-source report.
- **A web task asked of a minion goes to an internet agent** instead of being
  refused.

## 0.7.1 — 2026-10-13

### Fixed
- **A goal does not start with a model nobody serves.** Each role's model (the
  master, the thinking roles, the minions, the minion engines) is checked
  first; a missing one stops the goal at once and says which and where to fix
  it, instead of every minion failing with "HTTP 404". Agents > Settings marks
  such a model "not served anywhere".
- **A model nobody serves is never sent to the first engine** by default.
- **The master stops spawning when its workers cannot reach their model**: two
  in a row and it is told it is the setup, not the task.

### New
- **The agents know today's date**, so "latest" means latest.
- **Minions with network can search the web** (no key needed), instead of
  guessing addresses.

## 0.7.0 — 2026-10-13

### New
- **Minions spread over several machines.** Agents → Settings → More engines
  for the minions: tick every model on your other machines (or the same model
  on a second one) and how many each runs at once. Each minion goes to the
  engine with the most free room and stays there; when an engine stops
  answering, its minions' calls move to another within about 20 seconds and
  they carry on. The master is told how many minions can run at once. Each
  minion shows the engine it ran on.

### Fixed
- **A goal never ends silently.** If the master fails, the goal ends with the
  reason; its answer is always kept; a goal without an answer says why.
- **A goal on this computer stops with the app** (quitting or an update's
  restart), instead of running on and losing its answer.
- **Minions keep time for their answer to be checked**: on a slow model a
  retry no longer comes back unchecked.

## 0.6.3 — 2026-10-13

### New
- **Audit log** (sidebar → App → Audit log, and `bb audit`): every change
  made in ByteBunker, who made it (the app, `bb`, a job) and whether it
  worked. Settings, a chat's access, every terminal command that ran outside
  the sandbox (and every one you refused), tool approvals, MCP servers,
  gateways, machines, deploys, agents goals, deletions and exports. Filter,
  search, click a line for everything it recorded. **Verify** shows whether
  any line was changed, removed or moved since it was written; **Export**
  saves a copy to Downloads. Keys and passwords are never written to it.
- **Stop serving** on the Models screen, beside each model a machine of
  yours serves.

### Fixed
- **Agents on this computer start.** A goal run on the Mac the app runs on
  stopped at once with "missing required key 'llm.base_url'": the agents
  read the app's own settings file in place of theirs.
- **Agents on this computer reach the models it serves.** A model served on
  this machine (127.0.0.1) was left out of a run's models, as it must be for
  a separate worker, so every helper's call was refused.
- The Agents help page no longer says a Mac needs podman: it uses Seatbelt.

## 0.6.2 — 2026-10-13

### New
- **Every terminal command a chat runs is sandboxed.** Next to Terminal in
  the Playground: 🟢 **Local** (every new chat: the project's folder, your
  installed tools, never your keys or the rest of your home folder, no
  network), 🟡 **Web** (the same, with network) or 🔴 **Full access** (no
  sandbox, this chat only, with a red bar). Change it any time; the next
  command uses it. A command that needs more asks: **Allow once**, **Allow
  every time in this chat** or **Don't allow**. bb takes `--web` and
  `--full-access`.
- **Sessions show a dot** for the most each chat ever had: green, amber, red.
- **Agents on a Mac with nothing to install**: Seatbelt, macOS's own
  sandbox. **Work here** installs what a machine is missing, and a line that
  needs your password opens in a terminal.
- **Wrap up**: the master answers now with what it has (Stop gives no
  answer). While it works, a progress line: round, minutes, workers done.
- **A quicker Sultan**: it plans first and spawns only the workers the plan
  needs, sees its time budget and wraps up by itself at 70%, and waits for
  workers without spending model rounds. Goals get an hour by default.
- **Windows**: Settings → Command sandbox → **Enable WSL2**, a restart, then
  **Finish setup**, and commands run sandboxed in WSL2. Until then each one
  asks.
- **Six Mac recipes**: gpt-oss-120b (128 GB), and for 48–64 GB Macs
  qwen3.6-35b-a3b, nemotron-3.5-lightning, glm-4.7-flash, gemma-4-31b and
  gemma-4-26b-a4b.

### Fixed
- **Check for updates works again.** The app looked for trusted certificates
  where the build machine keeps them, so it could not reach GitHub (0.6.0
  included: download this version once by hand, and updates work from here on).
- **A machine you deploy on shows on Cluster.** Deploying (and `rack up` itself)
  starts the machine's monitor and adds it: GPU, memory and what its engine
  served. Usage says it counts what went through ByteBunker.
- Work here's result stays on screen; it used to vanish with its error.
- The master's badge said "deciding" forever; it now says so only while a
  goal runs.
- "Agents are off" takes you to where you set them up.
- How agents run opens bytebunkerlabs.ai instead of a page inside the app.

The sandbox for agents, Wrap up and the quicker Sultan need the newer agents
harness: **Update** on your worker under Agents → Settings. Deploying starts
the monitor with dgx-serve from 2026-10-13.

## 0.6.0 — 2026-10-12

### New
- **Agents that read your data, and keep it home.** On Agents → Settings →
  the court, **Access** takes folders on the worker and the MCP servers you
  share with agents. Each role ticks what it reads: folders, read-only, at
  `/data/<name>`; MCP tools through a broker on the worker that allows only
  what you granted, read-only tools unless you allow changes, answers
  repeated calls once per run, and logs every call. The servers' keys never
  enter a sandbox.
- **A role holding data never has network.** The app refuses a court that
  gives one role both, and so does the harness.
- **The khazin**, keeper of the treasury: a role for data work, listed once
  it has access. Big jobs go in chunks to several at once; a khazin can
  script many tool calls in one step and keeps a progress file another can
  continue from.
- **A lighter sandbox.** On Linux and WSL2, each agent runs in bubblewrap:
  no image to build, ready in about a tenth of a second, with memory, CPU and
  process limits. **Work here** picks it where it works, podman or docker
  elsewhere.
- **Max tokens: Auto**, the new default, lets the model use what its window
  has left; or pin a number up to 1M, cut to what fits each time.
  Auto-compress follows the model's real window.
- **Install and restart.** The sidebar shows your version; click it to
  check. When a newer one is out it lights up, and one click downloads,
  checks, installs and restarts ByteBunker.
- **Rename a chat** on Sessions: the pencil, or double-click its name.
- `bb ask --max-tokens N|auto`.

### Changed
- The Playground's side panel stays open in any app window and remembers
  whether you closed it.
- Token counts read 8k, 128k, 1M.

Agents' data access and the bubblewrap sandbox need the newer agents
harness on the worker: **Work here** on the Agent plane brings it.

## 0.5.0 — 2026-10-12

### New
- **Deploy replaces Recipes.** Serve a model on this computer or on any
  machine you add. With nothing set up, Deploy opens on this computer and
  offers to install dgx-serve here; on another machine it installs over your
  own ssh, then pairs. Each recipe says whether it fits the machine.
- **Ask for a recipe.** Name a model, and your mail app opens a request to
  support@bytebunkerlabs.ai with the machine's details filled in.
- **Machines.** Find every computer your ssh reaches, on a button press:
  what it is, its GPU and memory, the recipes that suit it, and one-click
  deploy. A worker of a paired rack says so and is deployed through its head.
- **Choose where agents work.** Agents → Settings lists every machine found;
  **Work here** checks it, sends the agents, builds their sandbox and tests it.
  A Mac works too, inside podman's VM. With one machine, Agents says to use the
  Playground, Projects or bb instead.
- **Agents has two tabs.** Master shows the master agent, its runs and its
  workers; Settings holds everything you set up once.
- **The court is yours.** Name every role in your own words, turn roles off,
  add your own, and set limits; it travels with each run. Help has a page on
  the court, with names from a royal court to borrow.
- **Cmd-K (Ctrl-K).** Jump to any screen, guide or action by typing.
- **A tidier app.** Models, Deploy and Gateways share one sidebar entry, and
  MCP servers, Skills and Plugins another. Sampling and output controls fold
  under Advanced. Remove buttons look the same everywhere.
- **Help links to ByteBunker Labs**: the website, docs, blog and support.

### Fixed
- The app's ssh to a rack never rides a shared connection (a shared one could
  answer "rack: command not found").
- The real ByteBunker mark in the sidebar and as the page icon.
- "1 model", not "1 models"; the sidebar's bars say GPU, with memory on hover.

Needs dgx-serve from 2026-10-12 on machines you deploy to.

## 0.4.2 — 2026-10-10

### New
- **Add a rack.** On Recipes, give a machine that serves with dgx-serve as you
  reach it with ssh (`you@host`). ByteBunker asks it once with your own ssh;
  dgx-serve there adds this app's own key, allowed to run `rack` and nothing
  else, and says where its engine and monitor are. ByteBunker finds the
  address that answers from here and adds what you tick: the engine as a
  gateway (with its key), the monitor on Cluster (with its token). Nothing is
  copied by hand. If your ssh wants a password, it shows the one command to
  run on the machine.
- **More than one rack.** The rack card drives whichever you pick; Remove
  forgets one and takes this app's key back from it.
- **bb pair, bb racks, bb deploy.** `bb pair you@host` adds a rack from a
  terminal; `bb deploy ls | status | up | down | logs` drives one. `up` and
  `down` ask first, and in a script go ahead only with `--yes`.
- **A rack on the setup checklist**, and the network check asks your first
  rack (it ran an old checkout's rack net).

### Fixed
- **A deploy's stream ends when it does.** Streams from the server stayed
  open 15 seconds after a run finished.

Needs dgx-serve with `rack pair` (from 2026-10-10) on the machine you pair.

## 0.4.1 — 2026-10-10

### New
- **Projects.** Related work in one place: a project has a folder, instructions
  every chat in it follows, and a default profile. Pick one next to the model
  in the Playground; its chats, from the app and from bb, are kept together,
  and Sessions can show one project's chats. Its folder is made when it is not
  there.
- **bb joins the project of the folder it runs in.** `bb -P name` picks one
  from anywhere, and `bb projects` lists them, marking the one you are in.
- **Open in terminal.** Terminal in the Playground continues the chat in bb, in
  a terminal window in the project's folder, where the model can work on your
  files with your tools; bb starts by showing the chat's last question and
  answer. A project's card opens a new chat there.
- **Workflows can belong to a project** and follow its instructions.

### Fixed
- **A chat open in the app shows what bb added to it** in a terminal, instead
  of saving over it with the next turn sent from the app.
- **Options with no command in front work:** `bb -m model` and `bb -P name`
  start a chat, as `bb` alone does, instead of a usage error.

## 0.4.0 — 2026-10-10

### New
- **Update from the app.** Settings → Updates asks GitHub for a newer version
  only when you press the button, shows what it brings, and with one click
  downloads it, checks it against the checksum its release publishes, and
  restarts into it. The version you had is kept, and when anything goes wrong
  before the switch, the app you have stays exactly as it was.
- **What's new.** This list, in the app: under Settings, and right after an
  update.
- **Check when ByteBunker starts**, if you want it. It is off unless you turn
  it on: the app does not phone home on its own.

### Fixed
- **A tool server that will not start says why**, in its own words (for
  example "None of the specified directories are accessible") instead of
  "server closed its stdout".
- **Folders are made for you.** A Filesystem server's folder, the terminal's
  working folder and a SQLite database's folder are created when they are
  missing. A Git server still needs a real repository, and says so before
  anything starts.
- **One terminal.** Adding Terminal from the catalog turns the built-in one on,
  or moves its folder, instead of adding a second copy; a terminal that is off
  says what it is.
- **Restart on a tool server restarts it.** It did nothing before.
- **Catalog defaults fit your machine:** your own time zone, and no folders
  from somebody else's computer.

## 0.3.1 — 2026-10-10

### Fixed
- **Recipes works with rack 1.0:** each recipe's model and platforms, the one
  serving now, and Read recipe opens the real file, wherever your own recipes
  live.
- **rack's output reads as plain text** in the app (status, logs, deploys),
  without terminal colour codes.
- **A model behind a gateway route of another name** is marked as the one
  serving, and its context, tools and vision come from the rack.

## 0.3.0 — 2026-10-06

### New
- **bb: ByteBunker in a terminal.** Chat, one-off questions with piped input,
  sessions, agents, jobs, runs, workflows, profiles and a doctor, on the same
  server as the app. Its own tools work in the folder you run it in and ask
  before changing anything.
- **Work that outlives the window.** Chats, agent goals, deploys, installs and
  jobs run on the server: close the app and they go on; open it again and they
  are still there.
- **Workflows and profiles.** A named prompt with blanks to fill, run from the
  app, bb or a job; a profile sets the model, effort, tools and approvals.
- **Approvals.** A tool that may change something asks first, in the app or in
  the terminal.
- **Help in the app.** A guide for every screen behind its "?", and a checklist
  of what is set up and what is missing.
- **Roles.** Say "fast" or "big" instead of a model name; jobs, workflows and
  agents follow whatever the role points at.
- **Model facts from the rack.** The context window, tools, vision and thinking
  switch come from what dgx-serve says it serves.

### Changed
- **Sessions are one file each.** The old single file is migrated once, and the
  original is kept.
- **config.json is versioned** and migrated once; edits you make while the app
  runs are kept.
- **Nothing is loaded from the internet:** the fonts come with the app.

## 0.2.0 — 2026-10-05

### New
- **One Cluster screen** for every node and engine, fed by rack monitors: GPU,
  memory, engines, throughput, and decode-step latency, with the tokens per step
  that speculative decoding gets.

### Fixed
- **An agents worker that cannot be reached** shows as unreachable, not idle.
- **Headless mode stops cleanly** when the system stops it.

## 0.1.0 — 2026-10-03

### New
- **The desktop app** for macOS and Windows: the console in its own window, your
  data in one folder, one copy running at a time, and engines found on this
  computer, your tailnet or, if you allow it, your local network.
- **Several gateways at once:** models from every engine or router in one list,
  each request sent to the one that serves its model.
- **The MCP screen:** tool servers with their tools, environment, restart and
  remove, and a catalog of local servers to add in one click.
- **Jobs:** chats or agent goals on a schedule, with their run history.
- **The Playground:** attachments (images for models that see), and tool
  activity folded above the answer.
- **Settings:** your data folder, your name, rates, an update check and an
  export.
