// Browser journeys for the phase-1 UI changes, against setup.sh's console.
const { chromium } = require("playwright-core");
const BASE = process.env.BB_UI_BASE || "http://127.0.0.1:18797";
const post = (path, body) => fetch(BASE + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json());
const results = [];
const check = (name, ok, detail) => { results.push([name, !!ok, detail || ""]); };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
  await page.goto(BASE + "/#jobs");
  await page.waitForTimeout(1500);

  // ---- Jobs: an event refreshes the list but keeps a half-typed form
  await page.click("#jobs-left .job-new button");            // + New job
  const nameBox = page.locator("#jobs-left .job-new input").first();
  await nameBox.fill("half-typed name");
  const saved = await post("/api/jobs", { action: "save", job: { name: "from elsewhere", kind: "chat", prompt: "hello", schedule: { kind: "cron", cron: "0 9 * * *" }, model: "fake-model", tools: false } });
  await page.waitForTimeout(1200);
  check("jobs: new job appears live", await page.locator("#jobs-left .job-item", { hasText: "from elsewhere" }).count() === 1);
  check("jobs: typed form survives the refresh", (await nameBox.inputValue()) === "half-typed name", await nameBox.inputValue());
  // run it; the card shows the run's result after the job's events
  await post("/api/jobs", { action: "run_now", id: saved.job.id });
  await page.waitForTimeout(1500);
  const cardText = await page.locator("#jobs-left .job-item", { hasText: "from elsewhere" }).innerText();
  check("jobs: card shows the finished run", /last .* ok/.test(cardText), cardText.replace(/\n/g, " | "));

  // ---- Agents: a goal started elsewhere is followed; Stop cancels it on the server
  await page.click('[data-nav="agents"]');
  await page.waitForTimeout(1200);
  const oneMachine = await page.locator("#agents-one-machine").innerText().catch(() => "");
  check("agents: with one machine, says to use the Playground, a Project or bb", oneMachine.includes("One machine?") && oneMachine.includes("Playground") && oneMachine.includes("bb"),
        oneMachine.slice(0, 120));
  await page.click('#agents-tabs [data-atab="settings"]');
  await page.waitForSelector('#agents-court [data-role="researcher"]', { timeout: 8000 }).catch(() => {});
  const courtText = await page.locator("#agents-court").innerText().catch(() => "");
  check("agents: Settings has the court, every role, yours to name", courtText.includes("court") && courtText.includes("researcher") &&
        courtText.includes("malikah") && await page.locator('#agents-court [data-role="researcher"] input.text-input').first().isVisible().catch(() => false),
        courtText.slice(0, 120) + " " + errors.slice(-3).join(" | "));
  // Access: a folder for the khazin, read-only; a network role is offered none
  check("agents: the khazin waits for access", (await page.locator('#agents-court [data-role="khazin"]').innerText().catch(() => "")).includes("nothing yet"));
  await page.locator("#agents-court .court-access-box summary").click();
  await page.fill("#court-folder-name", "sales");
  await page.fill("#court-folder-path", "~/data/sales");
  await page.click("#court-folder-add");
  await page.waitForTimeout(800);
  await page.locator('#agents-court [data-role="khazin"] .court-access input').first().check();
  await page.click("#court-save");
  await page.waitForTimeout(600);
  const savedCourt = await (await fetch(BASE + "/api/agents/court")).json();
  check("agents: a folder given to the khazin, saved", JSON.stringify((savedCourt.court.roles.khazin || {}).data) === '["sales"]' &&
        savedCourt.court.access.folders.sales === "~/data/sales", JSON.stringify(savedCourt.court).slice(0, 200));
  check("agents: a network role is offered no data", await page.locator('#agents-court [data-role="researcher"] .court-access').count() === 0);
  await fetch(BASE + "/api/agents", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "court", court: { roles: {} } }) });
  await page.click('#agents-tabs [data-atab="master"]');
  await page.waitForTimeout(400);
  check("agents: Master hides the settings", !(await page.locator("#agents-master").isVisible().catch(() => true)) &&
        !(await page.locator("#agents-court").isVisible().catch(() => true)));
  // start a slow goal from "another client" and walk away from it
  const ctl = new AbortController();
  fetch(BASE + "/api/agents", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ goal: "slow" }), signal: ctl.signal })
    .then(async (r) => { const rd = r.body.getReader(); await rd.read(); ctl.abort(); }).catch(() => {});
  await page.waitForTimeout(2500);
  const out = await page.locator("#agent-stream").innerText();
  check("agents: attaches to a goal started elsewhere", out.includes("following the goal already running") && out.includes("line 3"), out.slice(0, 160).replace(/\n/g, " | "));
  check("agents: Stop is shown", await page.locator("#agent-stop").isVisible());
  if (await page.locator("#agent-stop").isVisible()) await page.click("#agent-stop");
  await page.waitForTimeout(4000);
  const out2 = await page.locator("#agent-stream").innerText();
  check("agents: Stop ends the goal on the server", out2.includes("finished: stopped"), out2.slice(-120).replace(/\n/g, " | "));
  const runs = await (await fetch(BASE + "/api/runs?kind=agents")).json();
  check("agents: run recorded as cancelled", runs.runs[0] && runs.runs[0].state === "cancelled", runs.runs[0] && runs.runs[0].state);
  // a goal from this screen, reload mid-run, it is still there
  await page.fill("#agent-goal", "slow");
  await page.click("#agent-run");
  await page.waitForTimeout(1200);
  await page.reload();
  await page.waitForTimeout(2500);
  const out3 = await page.locator("#agent-stream").innerText();
  check("agents: a reload re-attaches to the running goal", out3.includes("following the goal already running") && /line \d+/.test(out3), out3.slice(0, 120).replace(/\n/g, " | "));
  if (await page.locator("#agent-stop").isVisible()) await page.click("#agent-stop");
  await page.waitForTimeout(3000);

  // ---- Sessions: a session saved elsewhere appears live; opening it loads its messages
  await page.click('[data-nav="sessions"]');
  await page.waitForTimeout(1000);
  await post("/api/sessions", { id: "s-ui-check", title: "saved by another client", model: "fake-model", turns: 2, chars: 20, updated: Date.now(),
    messages: [{ role: "user", content: "what is 2+2" }, { role: "assistant", content: "four" }] });
  await page.waitForTimeout(1200);
  const row = page.locator("#sessions-box .trow", { hasText: "saved by another client" });
  check("sessions: appears live", await row.count() === 1);
  // rename it in place; a later save from a client keeps the name
  await row.hover(); await row.locator(".ren").click();
  await page.locator("#sessions-box .ren-input").fill("Arithmetic with the fake");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(600);
  await post("/api/sessions", { id: "s-ui-check", title: "saved by another client", model: "fake-model", turns: 2, chars: 20, updated: Date.now(),
    messages: [{ role: "user", content: "what is 2+2" }, { role: "assistant", content: "four" }] });
  await page.waitForTimeout(1200);
  const renamed = page.locator("#sessions-box .trow", { hasText: "Arithmetic with the fake" });
  check("sessions: renamed in place, and the name outlives a later save", await renamed.count() === 1 &&
        await page.locator("#sessions-box .trow", { hasText: "saved by another client" }).count() === 0);
  await renamed.locator(".tname").click();
  await page.waitForTimeout(1000);
  const msgs = await page.locator("#msgs").innerText().catch(() => "");
  check("sessions: opening loads the messages", msgs.includes("what is 2+2") && msgs.includes("four"), msgs.slice(0, 100).replace(/\n/g, " | "));

  // ---- Playground: a chat turn through the proxy, streamed into the page
  await page.click('[data-nav="playground"]');
  await page.waitForTimeout(500);
  await page.click("#new-chat");
  await page.fill("#input", "ping from the browser");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  const chat = await page.locator("#msgs").innerText().catch(() => "");
  check("playground: streamed reply shown", chat.includes("echo: ping from the browser"), chat.slice(0, 160).replace(/\n/g, " | "));

  // ---- Playground: a turn bb adds to the chat open here shows up in it, no reload
  let open = null;
  for (let i = 0; i < 30 && !open; i++) {
    open = (await (await fetch(BASE + "/api/sessions")).json()).find((x) => x.title === "ping from the browser");
    if (!open) await page.waitForTimeout(200);
  }
  if (open) {
    await post("/api/sessions/" + open.id + "/turns", { text: "ping from the terminal", model: "fake-model", cwd: "/" });
    await page.waitForTimeout(3000);
  }
  const chat2 = await page.locator("#msgs").innerText().catch(() => "");
  check("playground: a turn from a terminal shows in the open chat", !!open && chat2.includes("echo: ping from the terminal") && chat2.includes("echo: ping from the browser"),
        chat2.slice(-200).replace(/\n/g, " | "));

  // ---- Projects: made in the form, its folder made, then picked in the Playground
  const projDir = require("path").join(require("os").tmpdir(), "bb-ui-project-" + Date.now(), "billing");
  await page.click('[data-nav="projects"]');
  await page.waitForTimeout(800);
  await page.click("#new-project");
  const form = page.locator("#projects-box .card").first();
  await form.locator("input").nth(0).fill("Billing");
  await form.locator("input").nth(1).fill(projDir);
  await form.locator("textarea").fill("Money is integer cents.");
  await form.locator("button", { hasText: "Create project" }).click();
  await page.waitForTimeout(1200);
  const cards = await page.locator("#projects-box").innerText();
  check("projects: made from the form", cards.includes("Billing") && cards.includes(projDir), cards.slice(0, 200).replace(/\n/g, " | "));
  check("projects: its folder is made", require("fs").existsSync(projDir), projDir);
  await page.click('[data-nav="playground"]');
  await page.waitForTimeout(500);
  const pick = page.locator("#project-select");
  check("projects: the Playground's picker has it, chosen", await pick.isVisible() && (await pick.locator("option:checked").innerText()) === "Billing",
        await pick.isVisible() ? await pick.locator("option:checked").innerText() : "hidden");
  // ---- Open in terminal: a server with no window of its own gives the command
  await page.click("#new-chat");
  await page.click("#terminal-btn");
  await page.waitForTimeout(800);
  const helpText = await page.locator("#help-body").innerText().catch(() => "");
  check("terminal: the command to continue in a terminal", helpText.includes("Continue in a terminal") && /bb chat -P p-[0-9a-f]+/.test(helpText),
        helpText.slice(0, 200).replace(/\n/g, " | "));
  try { require("fs").rmSync(require("path").dirname(projDir), { recursive: true, force: true }); } catch (e) {}

  // ---- Add a rack: pair over ssh, see what was found, keep it; the card then drives it
  // Deploy sits in the sidebar's Models group, as its second tab
  await page.click('[data-group="models"]');
  await page.waitForTimeout(300);
  check("sidebar: Models opens a group with Models, Deploy and Gateways tabs",
        (await page.locator("#group-models").innerText().catch(() => "")).replace(/\s+/g, " ").trim() === "Models Deploy Gateways");
  await page.click('[data-nav="recipes"]');
  await page.waitForTimeout(1200);
  // with no machine added, Deploy opens on this computer, and offers to install dgx-serve here
  const here = await page.locator("#deploy-card").innerText().catch(() => "");
  check("deploy: this computer first, with the install", here.startsWith("This computer") && await page.locator("#install-here-btn").isVisible().catch(() => false),
        here.slice(0, 160).replace(/\n/g, " | "));
  check("deploy: ask for a recipe", await page.locator("#ask-recipe").isVisible().catch(() => false));
  check("deploy: machines, found on a button press", await page.locator("#machines-find").isVisible().catch(() => false));
  await page.click("#add-rack");
  await page.fill("#rack-target", "you@box");
  await page.click("#rack-pair");
  await page.waitForSelector("#rack-found", { timeout: 20000 }).catch(() => {});
  const found = await page.locator("#rack-found").innerText().catch(() => "");
  check("racks: pairing shows what it found", found.startsWith("box \u00b7 dgx") && found.includes("engine: not reachable from here"),
        found.replace(/\n/g, " | "));
  check("racks: what did not answer cannot be ticked", await page.locator("#rack-add-gateway").isDisabled().catch(() => false));
  await page.click("#rack-save").catch(() => {});
  await page.waitForTimeout(2000);
  const rackHead = await page.locator("#deploy-card").innerText().catch(() => "");
  check("racks: saved, the card drives it", rackHead.startsWith("box") && rackHead.includes("Recipes for box") && rackHead.includes("qwen3-8b"),
        rackHead.slice(0, 200).replace(/\n/g, " | "));

  check("no page or console errors", errors.length === 0, errors.join(" || ").slice(0, 400));
  await browser.close();
  for (const [n, ok, d] of results) console.log((ok ? "ok    " : "FAIL  ") + n + (ok ? "" : "   -> " + d));
  process.exit(results.every((r) => r[1]) ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(2); });
