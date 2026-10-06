// Browser journeys for phase 2's app side: workflows, approvals, profiles, bb sessions.
const { chromium } = require("playwright-core");
const BASE = process.env.BB_UI_BASE || "http://127.0.0.1:18797", ENG = process.env.BB_UI_ENGINE || "http://127.0.0.1:18998";
const post = (url, body, headers) => fetch(url, { method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, headers || {}), body: JSON.stringify(body) }).then((r) => r.json());
const results = [];
const check = (name, ok, detail) => results.push([name, !!ok, detail || ""]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
  await page.goto(BASE + "/");
  await sleep(1500);

  // ---- Workflows: create one in the app, run it, watch the answer come in
  await page.click('[data-nav="workflows"]');
  await sleep(500);
  await page.click("#workflows-left .wf-new button");                 // + New workflow
  const form = page.locator("#workflows-left .wf-new");
  await form.locator("input").nth(0).fill("greet");
  await form.locator("textarea").fill("Say hello to {{who}}");
  await form.locator("button.solid-btn", { hasText: "Create workflow" }).click();
  await sleep(1000);
  check("workflows: created and listed", await page.locator("#workflows-left .wf-item", { hasText: "greet" }).count() === 1);
  await page.locator("#workflows-left .wf-item", { hasText: "greet" }).click();
  await sleep(400);
  check("workflows: run form asks for the blank", await page.locator("#workflows-detail label", { hasText: "who" }).count() === 1);
  await page.locator("#workflows-detail label", { hasText: "who" }).locator("input").fill("Ada");
  await page.locator("#workflows-detail button", { hasText: "Run" }).click();
  await sleep(2500);
  const out = await page.locator("#workflows-detail pre").innerText();
  check("workflows: the answer streams in", out.includes("echo: Say hello to Ada"), out.slice(0, 120));
  check("workflows: offers the session", await page.locator("#workflows-detail button", { hasText: "open the session" }).count() === 1);

  // ---- Approvals: a terminal's turn asks; the app answers
  await post(ENG + "/_fake/script", [{ tool_calls: [{ name: "fake__ping_client", arguments: {} }] }, { content: "the tool ran" }]);
  const t = await post(BASE + "/api/sessions/new/turns", { text: "use the tool", interactive: true }, { "X-BB-Client": "cli" });
  await sleep(2000);
  const card = page.locator("#approvals .approval");
  check("approvals: the app shows the question", await card.count() === 1 && (await card.innerText()).includes("fake__ping_client"));
  await card.locator("button", { hasText: /^Allow$/ }).click();
  await sleep(2500);
  check("approvals: answered, the card goes", await page.locator("#approvals .approval").count() === 0);
  const runs = await (await fetch(BASE + "/api/runs?kind=chat&limit=5")).json();
  const r0 = runs.runs.find((r) => r.id === t.run) || {};
  check("approvals: the turn finished after the answer", r0.state === "done", r0.state);

  // ---- Sessions: bb's session has a terminal badge
  await page.click('[data-nav="sessions"]');
  await sleep(800);
  check("sessions: bb's session marked terminal", await page.locator("#sessions-box .src-chip", { hasText: "terminal" }).count() >= 1);

  // ---- Settings: profiles listed, a new one saved
  await page.click('[data-nav="settings"]');
  await sleep(800);
  check("settings: built-in profiles listed", (await page.locator("#settings-box").innerText()).includes("Deep"));
  await page.locator("#settings-box button", { hasText: "New profile" }).click();
  const pf = page.locator('#settings-box input[placeholder="Careful"]').locator("xpath=ancestor::div[1]");
  await pf.locator("input").nth(0).fill("Careful");
  await pf.locator("select").nth(0).selectOption("low");
  await pf.locator("textarea").nth(1).fill("fake__* = ask");
  await pf.locator("button", { hasText: "Save profile" }).click();
  await sleep(1000);
  const profs = await (await fetch(BASE + "/api/profiles")).json();
  check("settings: profile saved", profs.profiles.Careful && profs.profiles.Careful.effort === "low" && profs.profiles.Careful.tool_policy["fake__*"] === "ask", JSON.stringify(profs.profiles.Careful));

  // ---- a terminal's turn in the session open here streams into the Playground
  await post(BASE + "/api/settings", { features: { server_runner: true } });
  await page.goto(BASE + "/");
  await sleep(1200);
  await post(ENG + "/_fake/script", [{ content: "first answer" }]);
  await page.fill("#input", "hello from the app");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("#send-btn").classList.contains("stop"), null, { timeout: 20000 });
  await sleep(500);
  const sid = (await (await fetch(BASE + "/api/sessions")).json()).sort((a, b) => b.updated - a.updated)[0].id;
  await post(ENG + "/_fake/script", [{ content: "streaming from the terminal ".repeat(12), chunk: 6, pace: 0.05 }]);
  await post(BASE + "/api/sessions/" + sid + "/turns", { text: "and this is bb", interactive: false }, { "X-BB-Client": "cli" });
  await sleep(1500);
  const mid = await page.locator("#msgs").innerText();
  check("live: bb's turn appears while it streams", mid.includes("and this is bb") && mid.includes("streaming from the terminal"), mid.slice(-160));
  await page.waitForFunction(() => !document.querySelector("#send-btn").classList.contains("stop"), null, { timeout: 30000 });
  await sleep(600);
  const end = await page.locator("#msgs").innerText();
  check("live: the finished turn stays, as saved", (end.match(/streaming from the terminal/g) || []).length >= 10 && end.includes("first answer"), end.slice(-120));
  await post(BASE + "/api/settings", { features: { server_runner: false } });

  check("no page or console errors", errors.length === 0, errors.join(" || ").slice(0, 400));
    await browser.close();
  for (const [n, ok, d] of results) console.log((ok ? "ok    " : "FAIL  ") + n + (ok ? "" : "   -> " + d));
  process.exit(results.every((r) => r[1]) ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(2); });
