# Deploy: serve a model anywhere, from the app

The owner's ask (2026-10-11): deploy recipes from the app over ssh onto any machine we connect (a Spark, a Linux
GPU box, a Windows PC, a Mac); with nothing configured, deploy on this computer; an Ask button that emails
support@bytebunkerlabs.ai for a recipe for a newer model. This is plan 1.0, section 4.3 ("Deploy replaces Recipes")
plus "serve on this computer" from section 2, shape A.

## What a user sees

The Recipes screen becomes **Deploy**. At the top, a machine picker: **This computer** and every paired rack, plus
**Add a machine** (today's Add a rack). For the chosen machine:

- **What it is**: "Mac (Apple M3 Max, 128 GiB unified memory, about 96 GiB usable for models)", what it serves now,
  dgx-serve's version.
- **Recipes that run there**, each with its download size and a verdict from dgx-serve (`fits`, `too big`, or
  `size unknown`), **Read** and **Serve**. Recipes with no variant for that machine sit in a folded list "for other
  machines".
- **Stop serving**, **Status**, **Logs**.
- **Ask for a recipe**: a model name or Hugging Face link and **Ask**. It opens the user's own mail app with a
  message to support@bytebunkerlabs.ai filled in (the model, the machine, the dgx-serve version). Nothing is sent
  from the app.

With no rack configured, the screen opens on **This computer**. When dgx-serve is not here yet, one card says what
serving here needs and offers **Install dgx-serve on this computer**; after it, the recipes appear.

Adding a machine without dgx-serve: pairing finds no `rack` there and offers **Install dgx-serve on user@host**, which
installs it over your own ssh and then pairs, in one run.

After a successful **Serve** on this computer, the app adds the local engine as a gateway (and the monitor, when one
runs) by itself, so the model shows in the Playground without a pairing step.

## How it works

- **This computer is a rack** with no ssh: `racks.entries()` always lists it (name `local`, `local: true`) after the
  configured racks. Its `rack` is the one on PATH or `~/.local/bin/rack` (on Windows, inside WSL2 through
  `wsl.exe -e bash -lc`); `BYTEBUNKER_LOCAL_RACK` points at another one, or `off`.
- **Install** (`POST /api/racks {action: install, target}`, a streamed run; `bb deploy install [--rack user@host]`):
  one bash script, the same here and over ssh: stop if `rack` is already there; check python3 and git or curl (with
  the fix when missing: Xcode's tools on a Mac, apt on Linux); `git clone --depth 1` dgx-serve into `~/dgx-serve`
  (or its tarball with curl), `./rack install`, `rack init`. `rack init` refuses an unsupported machine with its
  reason, which the app shows. Over ssh it runs with your own ssh (as pairing's first step does), then pairs.
- **Fit**: `rack recipes --json` already carries `here`, the model budget, and per variant `fits` and `weights_gb` for
  this machine; the app passes them through. `rack platform --json` gives the machine's description.
- **Local gateway**: after `rack up` on this computer, `rack pair --json` (no key) gives the engine's port and key and
  the monitor's; the app saves gateway `this computer` → `http://127.0.0.1:<port>/v1`.
- **Ask**: a `mailto:` link; the desktop app opens it through its bridge (the system's mail app), a browser opens it
  itself.

## Not in this step

- A Windows PC reached over ssh is reached at its WSL2 Ubuntu (ssh into WSL), not Windows' own OpenSSH.
- The signed, versioned installer from get.bytebunkerlabs.ai (decision 7) replaces `git clone` later; until then the
  app installs dgx-serve's main branch.
- The old console recipes (vllm-cuda, ollama-windows) stay, folded under "Older recipes", until 1.0 removes them.

## Done when

- Unit tests: the local rack, install here and over the stand-in ssh then pairing, fit and platform in the overview,
  the local gateway after Serve, the Ask link.
- A browser journey: Deploy with no rack shows This computer and the install card.
- On this Mac for real: install dgx-serve into a scratch home, serve `qwen3-8b` on llama.cpp, chat with it.
