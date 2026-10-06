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
  await row.click();
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

  check("no page or console errors", errors.length === 0, errors.join(" || ").slice(0, 400));
  await browser.close();
  for (const [n, ok, d] of results) console.log((ok ? "ok    " : "FAIL  ") + n + (ok ? "" : "   -> " + d));
  process.exit(results.every((r) => r[1]) ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(2); });
