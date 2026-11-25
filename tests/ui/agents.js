// Browser journey for the Agents tab: make an agent, ask it, read its memory, change its rules,
// give it a schedule, mention it in the Playground, ask nobody in particular.
const { chromium } = require("playwright-core");
const BASE = process.env.BB_UI_BASE || "http://127.0.0.1:18797";
const ENGINE = process.env.BB_UI_ENGINE || "http://127.0.0.1:18998";
const get = (path) => fetch(BASE + path).then((r) => r.json());
const script = (replies) => fetch(ENGINE + "/_fake/script", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(replies) });
const results = [];
const check = (name, ok, detail) => { results.push([name, !!ok, detail || ""]); };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push("console: " + m.text()); });
  page.on("dialog", (d) => d.accept());
  await page.evaluate(() => { try { localStorage.removeItem("bb.agentsTab"); } catch (e) {} }).catch(() => {});
  await page.goto(BASE + "/#agents");
  await page.waitForTimeout(1200);
  await page.click('#agents-tabs [data-atab="agents"]');
  await page.waitForTimeout(300);

  // ---- the list starts empty and says what an agent is
  const empty = await page.locator("#pa-list").innerText();
  check("agents: an empty list says what an agent is", empty.includes("No agents yet") && empty.includes("remembers"), empty.slice(0, 80));
  check("agents: the router says what auto picks now", (await page.locator("#pa-models").innerText()).includes("master: fake-model"),
        (await page.locator("#pa-models").innerText()).slice(0, 200));

  // ---- make one
  await page.click("#pa-new");
  const form = page.locator("#pa-new-form");
  await form.locator("input").nth(0).fill("ops");
  await form.locator("input").nth(1).fill("Ops");
  await form.locator("textarea").fill("Keeps the rack serving. Reports what changed.");
  await form.locator("button", { hasText: "Create" }).click();
  await page.waitForSelector('#pa-list .pa-row[data-agent="ops"]', { timeout: 5000 });
  await page.waitForSelector("#pa-ask-text", { timeout: 5000 });
  check("agents: a new agent is listed and opens on Ask", (await page.locator('#pa-list .pa-row[data-agent="ops"]').innerText()).includes("Keeps the rack serving"));
  const bad = await fetch(BASE + "/api/agents/agent", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "create", agent: { id: "ops", purpose: "x" } }) });
  check("agents: the same id twice is refused", bad.status === 400);

  // ---- ask it: it remembers a fact, then answers
  await script([{ tool_calls: [{ name: "remember", arguments: { fact: "spark-2 had 83 GB free at 07:00", type: "project", source: "rack_status", name: "disk-spark-2" } }] },
                { content: "spark-2 has **83 GB** free. Noted for tomorrow." }]);
  await page.fill("#pa-ask-text", "how is the disk on spark-2?");
  await page.click("#pa-ask-go");
  await page.waitForSelector("#pa-answer .answer-body", { timeout: 15000 }).catch(() => {});
  const ans = await page.locator("#pa-answer").innerText().catch(() => "");
  check("agents: the answer comes back in the pane, rendered", ans.includes("83 GB") && ans.includes("how is the disk on spark-2?"), ans.slice(0, 160) + " | " + errors.slice(-2).join(" | "));
  check("agents: markdown in the answer is drawn", await page.locator("#pa-answer .answer-body b, #pa-answer .answer-body strong").count() === 1);

  // ---- memory: the fact it wrote, with its source; one added by hand; one forgotten
  await page.click('.pa-tabs [data-patab="memory"]');
  await page.waitForSelector(".pa-fact", { timeout: 5000 });
  const mem = await page.locator("#pa-body").innerText();
  check("agents: memory shows the fact with its type and source", mem.includes("disk-spark-2") && mem.includes("spark-2 had 83 GB free at 07:00") && mem.includes("source: rack_status"), mem.slice(0, 200));
  await page.locator("#pa-body textarea").fill("Reports go out before 07:30.");
  await page.locator("#pa-body button", { hasText: "Remember" }).click();
  await page.waitForFunction(() => document.querySelectorAll(".pa-fact").length === 2, null, { timeout: 5000 }).catch(() => {});
  check("agents: a fact added by hand is the owner's word", (await page.locator("#pa-body").innerText()).includes("source: the owner said"));
  await page.locator(".pa-fact", { hasText: "disk-spark-2" }).locator("button", { hasText: "forget" }).click();
  await page.waitForFunction(() => document.querySelectorAll(".pa-fact").length === 1, null, { timeout: 5000 }).catch(() => {});
  const left = (await get("/api/agents/agent?id=ops")).memory.map((f) => f.name);
  check("agents: forget removes the fact", left.length === 1 && !left.includes("disk-spark-2"), JSON.stringify(left));

  // ---- rules: a class changed and an egress domain, saved
  await page.click('.pa-tabs [data-patab="rules"]');
  await page.waitForSelector('#pa-body select[data-cls="rack"]', { timeout: 5000 });
  check("agents: outward actions ask by default", (await page.locator('#pa-body select[data-cls="outward"]').inputValue()) === "ask" &&
        (await page.locator('#pa-body select[data-cls="spend"]').inputValue()) === "deny");
  await page.selectOption('#pa-body select[data-cls="rack"]', "deny");
  await page.fill("#pa-egress", "developer.nvidia.com");
  await page.locator("#pa-body button", { hasText: "Save" }).click();
  await page.waitForTimeout(700);
  const saved = (await get("/api/agents/agent?id=ops")).agent;
  check("agents: rules are saved", saved.rules.rack === "deny" && JSON.stringify(saved.rules.egress) === '["developer.nvidia.com"]', JSON.stringify(saved.rules));

  // ---- setup: a schedule becomes a job that names the agent
  await page.click('.pa-tabs [data-patab="setup"]');
  await page.waitForSelector("#pa-body button:has-text('Add a wake')", { timeout: 5000 });
  await page.locator("#pa-body button", { hasText: "Add a wake" }).click();
  const row = page.locator("#pa-body .pa-line").filter({ has: page.locator('[data-f="spec"]') }).last();
  await row.locator('[data-f="spec"]').fill("0 7 * * 1-5");
  await row.locator('[data-f="prompt"]').fill("Morning report: what served overnight.");
  await page.locator("#pa-body button", { hasText: /^Save$/ }).click();
  await page.waitForTimeout(900);
  const jobs = (await get("/api/jobs")).jobs.filter((j) => j.agent === "ops");
  check("agents: a cron wake is a job that names the agent", jobs.length === 1 && jobs[0].id === "agent-ops-w0" && jobs[0].schedule.cron === "0 7 * * 1-5", JSON.stringify(jobs.map((j) => [j.id, j.schedule])));
  check("agents: setup shows what auto picks and why", (await page.locator("#pa-body").innerText()).includes("now: fake-model"));
  const roleOpts = await page.locator("#pa-body .pa-grid select").first().locator("option").allInnerTexts();
  check("agents: a role can be given a live model by hand", roleOpts.includes("auto") && roleOpts.includes("fake-model") && roleOpts.every((o) => o && o !== "undefined"), JSON.stringify(roleOpts));

  // ---- setup keeps what it does not show: a wake's hand-set options survive a Save
  const postAgent = (body) => fetch(BASE + "/api/agents/agent", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json());
  const before = (await get("/api/agents/agent?id=ops")).agent.wake;
  await postAgent({ action: "save", agent: { id: "ops", wake: before.concat([{ kind: "rack", events: ["disk_high"], disk_pct: 85, prompt: "Say which disk is filling." }]) } });
  await page.click('.pa-tabs [data-patab="inbox"]');
  await page.click('.pa-tabs [data-patab="setup"]');
  await page.waitForSelector("#pa-body button:has-text('Add a wake')", { timeout: 5000 });
  await page.locator("#pa-body button", { hasText: /^Save$/ }).click();
  await page.waitForTimeout(900);
  const kept = (await get("/api/agents/agent?id=ops")).agent.wake;
  check("agents: Save in Setup keeps a wake's options the form does not show", kept.length === 2 && kept[1].kind === "rack" && kept[1].disk_pct === 85 && kept[0].cron === "0 7 * * 1-5", JSON.stringify(kept));
  await postAgent({ action: "save", agent: { id: "ops", wake: before } });

  // ---- a request in the app names the agent and reads as words; "Always" can be taken back in Rules
  const rulesNow = (await get("/api/agents/agent?id=ops")).agent.rules;
  await postAgent({ action: "save", agent: { id: "ops", rules: Object.assign({}, rulesNow, { write: "ask" }) } });
  await script([{ tool_calls: [{ name: "remember", arguments: { fact: "The weekly report goes out on Fridays.", type: "feedback", source: "the owner said", name: "report-day" } }] },
                { content: "Noted." }]);
  await page.click('.pa-tabs [data-patab="ask"]');
  await page.fill("#pa-ask-text", "remember that the weekly report goes out on Fridays");
  await page.click("#pa-ask-go");
  await page.waitForSelector("#approvals .approval", { timeout: 15000 }).catch(() => {});
  const card = await page.locator("#approvals .approval").first().innerText().catch(() => "");
  check("approvals: an agent's request says who asks and reads as words", card.includes("Ops wants to remember") && card.includes("The weekly report goes out on Fridays.") &&
        card.includes("source: the owner said") && !card.includes('"fact"'), card.slice(0, 240));
  await page.locator("#approvals .approval button", { hasText: "Always allow" }).click().catch(() => {});
  await page.waitForFunction(() => (document.querySelector("#pa-answer") || {}).innerText && document.querySelector("#pa-answer").innerText.includes("Noted."), null, { timeout: 15000 }).catch(() => {});
  await page.click('.pa-tabs [data-patab="rules"]');
  await page.waitForSelector("#pa-always", { timeout: 5000 }).catch(() => {});
  check("agents: Rules lists what Always left behind", (await page.locator("#pa-always").innerText().catch(() => "")).includes("remember"), JSON.stringify((await get("/api/agents/agent?id=ops")).agent.rules));
  await page.locator("#pa-always button", { hasText: "take back" }).click().catch(() => {});
  await page.waitForTimeout(900);
  const tookBack = (await get("/api/agents/agent?id=ops")).agent.rules;
  check("agents: an Always can be taken back there", !tookBack.tools && tookBack.write === "ask" && tookBack.rack === "deny", JSON.stringify(tookBack));
  await postAgent({ action: "save", agent: { id: "ops", rules: rulesNow } });
  await postAgent({ action: "forget", id: "ops", name: "report-day" });

  // ---- inbox: the ask and what became of it
  await page.click('.pa-tabs [data-patab="inbox"]');
  await page.waitForSelector(".pa-wake", { timeout: 5000 });
  const inbox = await page.locator("#pa-body").innerText();
  check("agents: the inbox shows each wake and its state", inbox.includes("how is the disk on spark-2?") && inbox.includes("done"), inbox.slice(0, 200));

  // ---- the Playground: @ops answers in the chat
  await script([{ content: "All quiet on the rack." }]);
  await page.click('[data-nav="playground"]');
  await page.waitForTimeout(500);
  await page.fill("#input", "@ops anything to report?");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector("#msgs") && document.querySelector("#msgs").innerText.includes("All quiet on the rack."), null, { timeout: 15000 }).catch(() => {});
  const chat = await page.locator("#msgs").innerText().catch(() => "");
  check("playground: @ops is answered by the agent, in the chat", chat.includes("All quiet on the rack.") && chat.includes("@ops anything to report?"), chat.slice(-200));

  // ---- an ask to nobody goes to the concierge
  await script([{ content: "It is a test day." }]);
  await page.click('[data-nav="agents"]');
  await page.waitForTimeout(800);
  await page.click('#agents-tabs [data-atab="agents"]');
  await page.click("#pa-ask-any");
  await page.locator("#pa-form textarea").fill("what day is it?");
  await page.locator("#pa-form button", { hasText: /^Ask$/ }).click();
  await page.waitForSelector('#pa-list .pa-row[data-agent="concierge"]', { timeout: 8000 }).catch(() => {});
  await page.waitForFunction(() => (document.querySelector("#pa-answer") || {}).innerText && document.querySelector("#pa-answer").innerText.includes("test day"), null, { timeout: 15000 }).catch(() => {});
  check("agents: an ask to nobody is answered by the concierge", (await page.locator("#pa-answer").innerText().catch(() => "")).includes("It is a test day."),
        (await page.locator("#agents-detail").innerText().catch(() => "")).slice(0, 200));

  // an agent's row is clicked and its pane is waited for: a tab clicked sooner is still the last agent's
  const openAgent = async (id) => {
    await page.click('#pa-list .pa-row[data-agent="' + id + '"]');
    await page.waitForFunction((id) => ((document.querySelector("#agents-detail .skill-id .src") || {}).textContent || "").startsWith(id + " "),
                               id, { timeout: 8000 }).catch(() => {});
  };

  // ---- its home on the worker: its size, and Reset
  const fs = require("fs"), pathmod = require("path");
  const homeDir = pathmod.join(process.env.BB_UI_HOMES || "/nonexistent", "bb-agent-ops");
  await openAgent("ops");
  await page.waitForSelector('.pa-tabs [data-patab="setup"]', { timeout: 5000 });
  await page.click('.pa-tabs [data-patab="setup"]');
  await page.waitForFunction(() => (document.querySelector("#pa-home") || {}).innerText && !document.querySelector("#pa-home").innerText.includes("\u2026"), null, { timeout: 8000 }).catch(() => {});
  check("agents: an unused home says so", (await page.locator("#pa-home").innerText().catch(() => "")).includes("bb-agent-ops") &&
        (await page.locator("#pa-home").innerText()).includes("nothing kept yet"), await page.locator("#pa-home").innerText().catch(() => "(no home line)"));
  fs.mkdirSync(homeDir, { recursive: true });
  fs.writeFileSync(pathmod.join(homeDir, "notes.md"), "x".repeat(3000));
  await page.click('.pa-tabs [data-patab="inbox"]');
  await page.click('.pa-tabs [data-patab="setup"]');
  await page.waitForSelector("#pa-home button", { timeout: 8000 }).catch(() => {});
  check("agents: a home with files shows its size and offers Reset", (await page.locator("#pa-home").innerText().catch(() => "")).includes("1 file"), await page.locator("#pa-home").innerText().catch(() => ""));
  await page.locator("#pa-home button", { hasText: "Reset home" }).click().catch(() => {});
  await page.waitForFunction(() => (document.querySelector("#pa-home") || {}).innerText && document.querySelector("#pa-home").innerText.includes("nothing kept yet"), null, { timeout: 8000 }).catch(() => {});
  check("agents: Reset home empties it, and the memory stays", !fs.existsSync(homeDir) && (await get("/api/agents/agent?id=ops")).memory.length === 1,
        await page.locator("#pa-home").innerText().catch(() => ""));

  // ---- Channels: connect the bot, bind a chat, be answered in it
  const TG = process.env.BB_UI_TELEGRAM;
  const tg = (path, body) => fetch(TG + path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined).then((r) => r.json());
  await page.click('#agents-tabs [data-atab="agents"]');
  await page.waitForSelector("#tg-token", { timeout: 8000 }).catch(() => {});
  const card0 = await page.locator("#pa-channels").innerText().catch(() => "");
  check("channels: the card says how to connect, and that nothing listens", card0.includes("Telegram") && card0.includes("Mail") && card0.includes("nothing here listens"), card0.slice(0, 160));
  await page.fill("#tg-token", "000000:WRONGwrongWRONGwrongWRONGwrongWRONG");
  await page.locator("#pa-channels button", { hasText: "Connect" }).click();
  await page.waitForFunction(() => /refused|Unauthorized|token/i.test((document.querySelector("#pa-channels .hint:last-child") || {}).innerText || ""), null, { timeout: 8000 }).catch(() => {});
  check("channels: a token Telegram refuses is not kept", !(await get("/api/channels")).telegram.connected, (await page.locator("#pa-channels").innerText()).slice(-160));
  await page.fill("#tg-token", "123456:TESTtokenTESTtokenTESTtokenTEST");
  await page.locator("#pa-channels button", { hasText: "Connect" }).click();
  await page.waitForFunction(() => document.querySelector("#pa-channels").innerText.includes("@rack_test_bot"), null, { timeout: 10000 }).catch(() => {});
  check("channels: a good token connects the bot", (await page.locator("#pa-channels").innerText()).includes("@rack_test_bot"), (await page.locator("#pa-channels").innerText()).slice(0, 200));
  await tg("/_fake/message", { chat: 4242, text: "/start" });
  await page.waitForSelector('#pa-channels [data-pending="4242"]', { timeout: 10000 }).catch(() => {});
  check("channels: a chat that wrote /start waits to be bound", await page.locator('#pa-channels [data-pending="4242"]').count() === 1, (await page.locator("#pa-channels").innerText()).slice(0, 300));
  await page.locator('#pa-channels [data-pending="4242"] select').selectOption("ops").catch(() => {});
  await page.locator('#pa-channels [data-pending="4242"] button', { hasText: "Bind" }).click().catch(() => {});
  await page.waitForFunction(() => document.querySelector("#pa-channels").innerText.includes("\u2192 ops"), null, { timeout: 8000 }).catch(() => {});
  check("channels: the chat is bound to the agent", ((await get("/api/channels")).telegram.chats[0] || {}).agent === "ops", JSON.stringify((await get("/api/channels")).telegram.chats));
  await script([{ content: "GLM is up." }]);
  await tg("/_fake/message", { chat: 4242, text: "is GLM up?" });
  let said = [];
  for (let i = 0; i < 80 && !said.some((m) => (m.text || "").includes("GLM is up.")); i++) { await page.waitForTimeout(150); said = (await tg("/_fake/sent")).sent.filter((m) => String(m.chat_id) === "4242"); }
  check("channels: a message in the bound chat is answered there", said.some((m) => (m.text || "").includes("GLM is up.")), JSON.stringify(said.map((m) => m.text)).slice(0, 200));
  await page.locator("#pa-channels button", { hasText: "Disconnect" }).click().catch(() => {});
  await page.waitForSelector("#tg-token", { timeout: 8000 }).catch(() => {});
  check("channels: Disconnect removes the bot", !(await get("/api/channels")).telegram.connected);

  // ---- a worker whose harness predates agents that persist: the tab says what is missing, and where to fix it
  const asWorker = async (known) => {
    await page.unroute("**/api/agents/list").catch(() => {});
    await page.route("**/api/agents/list", async (route) => {
      const r = await route.fetch();
      await route.fulfill({ response: r, json: Object.assign(await r.json(), { goals: true, court_agent: known }) });
    });
    await page.click('[data-nav="playground"]');
    await page.click('[data-nav="agents"]');
    await page.click('#agents-tabs [data-atab="agents"]');
    await page.waitForSelector('#pa-list .pa-row[data-agent="ops"]', { timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(400);
  };
  await asWorker(false);
  const oldNote = await page.locator("#pa-old-worker").innerText().catch(() => "");
  check("agents: an old worker is named, with where to update it", oldNote.includes("predates") && oldNote.includes("Work here"), oldNote.slice(0, 120));
  await asWorker(true);
  check("agents: an up-to-date worker gets no such note", await page.locator("#pa-old-worker").count() === 0);
  await page.unroute("**/api/agents/list").catch(() => {});

  // ---- delete: gone from the list, its schedule with it
  await openAgent("ops");
  await page.waitForSelector('.pa-tabs [data-patab="setup"]', { timeout: 5000 });
  await page.click('.pa-tabs [data-patab="setup"]');
  await page.waitForSelector("#pa-body button:has-text('Delete')", { timeout: 5000 });
  await page.locator("#pa-body button", { hasText: "Delete" }).click();
  await page.waitForTimeout(900);
  const after = await get("/api/agents/list");
  check("agents: delete removes it and its jobs", !after.agents.some((a) => a.id === "ops") && !(await get("/api/jobs")).jobs.some((j) => j.id === "agent-ops-w0"));
  await fetch(BASE + "/api/agents/agent", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete", id: "concierge" }) });

  check("agents: no page errors", errors.length === 0, errors.slice(0, 4).join(" | "));
  await browser.close();
  let failed = 0;
  for (const [name, ok, detail] of results) { if (!ok) failed++; console.log((ok ? "  ok  " : "  FAIL") + "  " + name + (ok || !detail ? "" : "   -> " + detail)); }
  console.log(failed ? failed + " failed" : "all " + results.length + " passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
