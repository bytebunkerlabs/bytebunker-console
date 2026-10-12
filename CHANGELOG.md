# Changelog

What changed in each version of ByteBunker, newest first. After an update the
app opens on the new version's section, and Settings → What's new shows them all.

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
