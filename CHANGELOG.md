# Changelog

What changed in each version of ByteBunker, newest first. After an update the
app opens on the new version's section, and Settings → What's new shows them all.

## 0.9.0 — 2026-11-25

### New
- **Agents that persist.** An agent is a name, a purpose, rules, a budget and
  a memory, kept as a folder on this computer. It wakes on an ask, a schedule,
  a chat, a mail, a feed, a dropped file or a change on the rack, does one run,
  and answers where the ask came from. Agents → **New agent**, or
  `bb agents new ops --purpose "..."`. The court is still there, on the Master
  tab, for a goal with no agent around it.
- **Memory, one fact per file.** An agent writes what is worth keeping with
  `remember(fact, type, source)` and drops it with `forget`; every fact names
  where it came from. The files are yours to read, edit and put under git. A
  nightly tidy merges duplicates and lists what was read from mail or the web.
- **A model router.** A role left on auto gets the live model that fits it
  best each time a run starts. When the model it wants is down, a chat ask
  settles for a smaller one and says so, a scheduled goal waits for a model
  that is loading, an agent that may asks to switch the rack, and anything
  else is kept and tried again. Agents → Models shows what it would pick now
  and why. `bb agents router`.
- **Telegram.** One bot, long polling, no port opened: a chat is bound to one
  agent, the answer comes back in the same chat, and an approval is a Yes and
  a No button there. The token lives in the app's vault. The bot speaks the
  Bot API as it is documented today, waits when Telegram says it is sending
  too fast, and says so on the Channels card when a second program listens
  for the same bot.
- **Mail, feeds, a drop folder, the rack.** A mail from a sender on an agent's
  list, a new item in an RSS or JSON feed, a GitHub notification, a CI run
  that did not pass, a file put in a folder, an engine that went down: each
  can wake an agent. All of them ask out on their own interval. Mail works
  with any mailbox that takes a password over IMAP, always encrypted, and a
  server whose certificate is not trusted is refused. A folder's name may
  have spaces or accents, only what arrived since the last look is asked for,
  and a mailbox the server rebuilt starts again from what is there.
- **Its own computer.** An agent has a home folder on the worker that outlives
  a run, and an egress list: the only domains its court may reach, through one
  proxy on the worker that refuses everything else, your own network included.
  A role's network flag opens that list, never the whole network, and the list
  starts empty: a new agent reaches nothing outside until you list domains
  (`*` is any public site). A role that can reach out never sees the home; it
  gets a downloads folder of its own there. With the browser image on the
  worker the court gets a browser role whose logins last between runs.
- **Ask anyone.** An ask to no agent goes to the concierge, which answers a
  short question itself or hands it to the agent whose purpose fits.
- **@mention in the Playground.** `@ops what served overnight?` is answered by
  that agent, in the chat.
- `bb agents ls | new | show | ask | memory | rules | inbox | rm | router |
  channels`. `bb agents ls` now lists your agents; the court's runs are
  `bb agents runs`.

### Safer
- **Rules outside the model.** Each agent has a policy per class of action:
  reading, writing and commands in its sandbox are allowed; a message to
  another channel, a mail, anything that changes what the rack serves ask
  first; spending is denied. An ask shows where the request came from, in the
  app and in bb, and ten minutes of silence is a no.
- **Mail, pages and files are data.** What arrives from outside reaches an
  agent marked as data from that source, beside your own prompt. A fact it
  remembers from such a run says so, and such a run always asks before
  anything outward.
- **Budgets.** Tokens a day, runs a day, minutes a run and quiet hours, per
  agent. One that reaches a cap stops and says so once.
- **A reply has an end.** One reply of an agent's turn is asked to stop at
  16,384 tokens: its thinking, its answer and any tool call it writes. (A
  model whose recipe says it needs more room to think gets that. An engine
  that refuses a request with the limit in it is asked again without it.)
  Where nobody is watching, in an agent's turn or a job, a reply that was
  all thinking is asked for once more with less thinking, when the model
  has a thinking level to turn down. That is a reply that ran to its limit
  with no answer, and thinking that went round in circles: the same block
  three times or more in a row, to the letter, over a page or more, which
  is stopped while it streams. If the second reply has no answer either, an
  agent's asker and a job's record say why. A small model can otherwise
  think for an hour and say nothing: one did, for ten minutes, until a
  person stopped it. An answer is never judged this way (it may be a
  thousand equal lines that were asked for), and a turn in the Playground
  is never stopped for you. Thinking that repeats itself with a number that
  counts up is not seen as circles; the reply's limit ends it.
- **A note never leaves by mail.** Why an agent waits, or that it stopped for
  the day, is said in the app and in its chat. It is never mailed, least of
  all to an address a mail named, and an answer by mail still asks first.
- **A fetch that reaches anywhere asks first.** In a chat turn, a tool that
  reaches outside this computer runs without asking only for an address on the
  agent's egress list. Any other address makes the call an outward action: it
  asks, naming the address, or is refused when the agent's rule says deny.
  "Always" on such a request puts the addresses it names on the agent's list,
  unless the run began with a mail, a feed or a file. An address on your own
  network cannot be listed, so it asks each time.
- **A tool that says nothing of itself counts as outward.** MCP reads a
  missing hint as "may reach outside", and so does an agent's rule now: such
  a tool asks under the outward rule, and never runs unasked in a run that
  began with a mail, a feed or a file. The app's own jobs server, which files
  and runs jobs as you, is never an agent's, and neither it nor the app's
  terminal is handed to an agent's court.
- **What nobody vouches for stays marked.** A run woken by a mail, a feed or
  a file keeps that mark on what it starts. "Always" on a request does not
  cover it: a message off the asking chat still asks. An answer from another
  agent comes back no more trusted than the ask was. A job it files starts
  switched off, with the reason on the Jobs screen and a note where the agent
  reports, so content that arrived from outside cannot become the agent's
  own schedule.
- **A message goes where the call said.** An agent that names a channel it
  does not have is told so. The message is not sent to its first channel
  instead, and nobody is asked about it.
- **An agent runs the tools it was given, and no other.** A model can name a
  tool it was never shown, and text it read can supply the name. A turn that
  was given a list of tools now runs those only.
- **Its rules hold in a goal as in a turn.** What the court's master does
  itself (keep a fact, ask another agent, file a job) goes through the
  agent's rules and the guard. An agent whose rules do not let it run
  commands and write files alone answers in one turn, since the court's
  workers do both without asking. An agent's goal gets the folders and MCP
  servers the agent was given, not the ones set for your own goals. At most
  five requests of one goal wait for you at a time, and they end with the goal.
- **Nothing a model writes can pass for the harness.** The harness signs
  every line it sends the app with a key made up for that run. A line that
  only looks like one, in an answer, a quoted page or a file's name, is text.
  A worker whose harness predates this cannot be given an agent's network
  list either, so an agent answers in one turn there until it is updated. A
  harness that takes the key and signs nothing (a copy updated in part) makes
  the goal fail and say so, not end as if it had gone well.
- **The address a tool will reach is the one that is checked.** A value
  counts as an address on the agent's list only when the whole value is one
  plainly written address: its scheme, its host, a path. Then what follows
  the last @ is the host. An address inside other text (a sentence, a page,
  code) is not read out of it, and one that two programs could read as two
  hosts is not guessed at: the call asks. So does a name in another script
  that an older program would map onto an ASCII one. Names such as
  `printer.local` are never public. In the sandbox, a host name with a
  hidden end no longer slips past the list.
- **A link in a watched folder or in an agent's home is not followed.** A
  link is not a file someone put there, and a link left in a home by one run
  is not mounted by the next.
- **What a request shows is what is sent.** An answer by mail leaves exactly
  as the request showed it, and a request cut short in the chat says how
  much more there is. A command is shown with everything else its call
  names, such as the host it runs on, and a fact with the name it is kept
  under.
- **Names from outside stay inside the data.** A file's name is told inside
  the block that marks its content as data. A sender's own words in front of
  the @ and a closing tag inside the content cannot step out of it, and a
  name the rack monitor sends ends at its first space.
- **Rules as they are now.** A rule changed while an agent works holds from
  its next action, for the app's tools as for its own, and "Always" on a
  request keeps what you changed while it waited. A rule by tool name is a
  pattern and the last line that matches wins, as in Settings, so "Always"
  wins over a pattern set before it. The rack's "only when nothing else
  runs" holds for a rule by name too.
- **A kept mail password goes only where it was typed for.** Another server,
  a first SMTP host, another port or a connection without TLS asks for the
  password again.
- **A hook for ByteBunker Guard.** Every tool call an agent makes and every
  reply it sends passes one function first. It allows everything until the
  guard is installed.

### Better
- **An agent's conversation is not read again on every message.** The date
  and time ride on each ask, and what an agent is told about itself no
  longer changes from one ask to the next, so an engine can keep what it has
  read. How much that saves is the engine's to say: GLM on the Sparks keeps
  a conversation in steps of 9,216 tokens, so a 24,000-token thread went
  from 15 s before the first word to 4, and a thread shorter than one step
  is read whole as before.
- **A turn that says nothing is a failure you hear about.** No text, only
  thinking, a tool loop that was stopped: the asker is told. An answer cut
  at the model's limit says so.
- **A llama.cpp engine says its own window.** The window a llama.cpp server
  was started with is read from its model list, as vLLM's is. Its refusal of
  a prompt that does not fit is understood as vLLM's is: the turn says the
  context is full, and the window is remembered for next time.
- **A second refusal is told in the server's words.** When a server refuses
  a request, is asked again without the extras it does not know, and refuses
  that too, the turn now says what the server said the second time. It used
  to say "HTTP Error 400: Bad Request".
- **A job that says nothing has failed.** A job whose last reply ran to its
  limit, or had been asked for a second time, and wrote nothing is recorded
  as failed, with the reason. It used to be recorded as done, with an empty
  result. A job whose tools did the work and whose last reply is empty is
  done, as before.
- **An answer Telegram did not take is sent again,** after one minute, then
  five, then fifteen, then twice an hour apart: the text as it was sent, a
  failure's own words included, and of a long answer only the parts that did
  not arrive. A very long answer is one message: the file, with its first
  lines as the caption. A chat that is slow to take it holds up nobody
  else's ask.
- **Your own ask goes first.** Then the rack, then the agent's schedule, then
  another agent, then what arrived from outside. A busy feed does not hold
  back the morning report.
- **A goal waits for a free slot** instead of failing, also when a goal of
  your own holds one.
- **A turn nobody waits for waits for a model that is loading,** and settles
  for a smaller one only when the big one is not coming.
- **A node that goes dark is reported,** also when its monitor went with it.
  Taking a monitor out of the app is not an outage, and a node that stays
  missing for a week is forgotten. An engine that goes down is brought back
  on the recipe that node was serving. A node keeps its own name in what the
  agent is told, underscores and spaces included.
- **Every item of a busy feed arrives,** ten a look, oldest first.
- **A drop folder with a long path wakes for every file.** What tells a wake
  from a repeat was cut at 200 characters, so the second file in a deep
  folder, or the second item of a feed with long addresses, was taken for the
  first again and woke nobody.
- **A mail that cannot be read is skipped,** and the ones after it arrive.
  One that was read and could not be handed to its agent is tried at the next
  looks, not lost.
- **An agent file edited by hand cannot keep the app from starting.**
- **A request on the phone reads as a sentence.** "Ops wants to remember",
  then the fact and its source. An agent's own actions showed their
  arguments as JSON.
- **The rack report knows one engine can span two nodes.** What an agent
  reads about the rack says since when a recipe has served and which nodes
  serve it together. The second node of a pair read as "serving nothing".
- **The router's card takes vision and thinking from what is served.** A
  model served with a reasoning parser counts as one that thinks, and the
  card on the Agents screen is read as fresh as a run's.
- **Two goals at once.** A goal's state on the worker is keyed by its run, so
  the one-goal lock is gone: two run side by side by default, and several
  agents at once.
- **Stop is immediate while a model is still reading the prompt.** A turn
  that had not received its first byte could not be stopped until it did.
- **Run now on a job that wakes an agent** does what the schedule would, the
  report on the agent's channel included.
- **Nothing waiting is lost to tidying.** An agent's inbox keeps every wake
  that has not ended, however many came after it. Finished runs are let go
  even while an older run is still going.
- **Nothing is kept for every wake.** The size the engine reported for a
  session's last prompt is remembered for the 256 newest sessions. It was
  kept for every wake an agent ever ran, a little memory each, for ever.
- **The audit log speaks plainly about agents.** A line reads "Made an
  agent", "Agent run, from telegram" or "Agent action: send_message, allowed
  once", and names the agent or the chat it is about. These lines showed
  the action's internal name, and a new agent's line did not say which.
- **A wake's instruction has room.** In an agent's Setup, what a wake should
  do has a line of its own under its schedule. It sat in a box too narrow
  to read. In the list, a schedule or a path moves to the next line whole.
- **A flood has a bound.** Past 200 waiting wakes, what a feed, a mailbox, a
  folder or the rack sends an agent is not kept until its inbox clears, and
  the inbox says so once. Your own asks are always taken.
- **An ask that waited for a model runs when one is back,** within seconds,
  not when its wait would have run out. An agent's next wake starts as the
  run before it ends, not up to two seconds later.
- **Names that travel.** An agent or a fact cannot take a name Windows keeps
  for a device (con, nul, aux and the like), so a data folder moves between a
  Mac, Linux and Windows.
- **Checked beyond the unit tests.** `tests/soak.py` runs three agents on
  their schedules and channels and passes when the console's memory, threads
  and open files stay flat and no secret is in what it wrote or said.
  `tests/real/` puts the mail channel in front of Dovecot and Postfix over
  TLS. CI runs both, and the suite on Linux.

### Fixed
- A request whose body had a field named target, ok or who lost its line in
  the audit log.
- `bb ask` no longer waits for ever when it is started with a socket as its
  standard input that never closes, as some schedulers and tools do. A pipe or
  a file is still read to its end.
- A worker's harness updated by hand is noticed within the hour, and at once
  by Test and Work here. Until now what it could do was asked once and kept.
- The first goal on a harness set up by hand no longer fails on the
  placeholder models in the worker's own config.
- A master on an engine with no reasoning parser no longer delivers its
  reasoning as part of the answer.

## 0.8.1 — 2026-11-02

### Better
- **A model that is loading shows as loading.** While a rack loads a model, the
  picker and the Models screen show it greyed out with the time so far ("loading,
  6 min so far"), and it becomes selectable by itself once it answers. One that
  could not start says so, and why. A big model takes minutes, and until now
  that looked the same as a broken connection. `bb models` says the same. This
  needs dgx-serve from 2026-11-02 or newer.

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
