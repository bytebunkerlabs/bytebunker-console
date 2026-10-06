// Parity: the same conversations through the Playground's own loop and
// through the server runner, compared by what the engine received and
// what the session saved.
const { chromium } = require("playwright-core");
const BASE = process.env.BB_UI_BASE || "http://127.0.0.1:18797", ENG = process.env.BB_UI_ENGINE || "http://127.0.0.1:18998";
const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json());
const get = (url) => fetch(url).then((r) => r.json());
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const echo = (t) => ({ name: "fake__echo", arguments: { text: t } });

const SCENARIOS = [
  { name: "plain answer", turns: [["capital of France?", [{ content: "Paris." }]]] },
  { name: "reasoning", turns: [["2+2?", [{ content: "4", reasoning: "two and two" }]]] },
  { name: "one tool hop", turns: [["echo hi", [{ tool_calls: [echo("hi")] }, { content: "it said hi" }]]] },
  { name: "two calls, one hop", turns: [["both", [{ tool_calls: [echo("a"), echo("b")] }, { content: "a and b" }]]] },
  { name: "two hops", turns: [["chain", [{ tool_calls: [echo("one")] }, { tool_calls: [echo("two")] }, { content: "end" }]]] },
  { name: "truncated call", turns: [["cut", [{ tool_calls: [{ name: "fake__echo", arguments: '{"text": "unfin' }], finish_reason: "length" }]]] },
  { name: "overflow then fit", turns: [["big", [{ overflow: { ctx: 16384, requested: 20000, prompt: 8000 } }, { content: "fits" }]]] },
  { name: "no tool support", turns: [["tools?", [{ status: 400, error: "'auto' tool choice requires --enable-auto-tool-choice" }, { content: "without tools" }]]] },
  { name: "the same call thrice", turns: [["loop", [{ tool_calls: [echo("x")] }, { tool_calls: [echo("x")] }, { tool_calls: [echo("x")] }]]] },
  { name: "system prompt", setup: async (page) => { await page.fill("#sys", "Answer in one word."); }, turns: [["why?", [{ content: "Because." }]]] },
  { name: "two turns, history", turns: [["first", [{ tool_calls: [echo("h")] }, { content: "one" }]], ["second", [{ content: "two" }]]] },
  { name: "json, seed, stops", setup: async (page) => { await page.click("#json-switch"); await page.fill("#seed", "42"); await page.fill("#stops", "END, STOP"); }, turns: [["json please", [{ content: "{\"a\": 1}" }]]] },
  { name: "effort max maps to the model's high", setup: async (page) => { await page.fill("#effort", "5"); await page.dispatchEvent("#effort", "input"); }, turns: [["think hard", [{ content: "thought", reasoning: "hmm" }]]] },
  { name: "text attachment", attach: { name: "notes.txt", text: "line one\nline two\n" }, turns: [["what is in it?", [{ content: "two lines" }]]] },
];

const sameUploads = (x) => JSON.parse(JSON.stringify(x).replace(/\/uploads\/[^/"]+\//g, "/uploads/SESSION/"));
function normReq(b) {
  const ids = {};
  const id = (x) => (x in ids ? ids[x] : (ids[x] = "id" + Object.keys(ids).length));
  return {
    model: b.model, stream: b.stream, max_tokens: b.max_tokens, temperature: b.temperature, top_p: b.top_p, top_k: b.top_k,
    repetition_penalty: b.repetition_penalty, seed: b.seed, response_format: b.response_format, stop: b.stop,
    reasoning_effort: b.reasoning_effort, chat_template_kwargs: b.chat_template_kwargs, tool_choice: b.tool_choice,
    tools: (b.tools || []).map((t) => t.function.name).filter((n) => !n.startsWith("ws__")).sort(),
    messages: (b.messages || []).map((m) => ({ role: m.role, content: m.content, reasoning_content: m.reasoning_content,
      tool_call_id: m.tool_call_id ? id(m.tool_call_id) : undefined,
      tool_calls: m.tool_calls ? m.tool_calls.map((c) => ({ id: id(c.id), name: c.function.name, args: c.function.arguments })) : undefined })),
  };
}
function normMsg(m) {
  return { role: m.role, content: m.content, reasoning: m.reasoning || "", error: m.error || "", kind: m.kind,
           toolUse: (m.toolUse || []).map((t) => ({ name: t.name, args: t.args, result: t.result, error: !!t.error })),
           hops: (m.hops || []).map((h) => ({ content: h.content, reasoning: h.reasoning, calls: (h.tool_calls || []).map((c) => c.function.name + c.function.arguments), results: (h.results || []).map((r) => r.content) })),
           attachments: (m.attachments || []).map((a) => a.name + ":" + a.kind) };
}

async function runScenario(page, sc, runner) {
  await post(BASE + "/api/settings", { features: { server_runner: runner } });
  await page.goto(BASE + "/");
  await page.waitForFunction(() => document.querySelector("#model-select") && document.querySelector("#model-select").value);
  await sleep(600);
  if (sc.setup) await sc.setup(page);
  if (sc.attach) {
    await page.setInputFiles("#attach-input", { name: sc.attach.name, mimeType: "text/plain", buffer: Buffer.from(sc.attach.text) });
    await sleep(800);
  }
  const before = (await get(ENG + "/_fake/requests")).length;
  for (const [text, script] of sc.turns) {
    await post(ENG + "/_fake/script", script);
    await page.fill("#input", text);
    await page.keyboard.press("Enter");
    await sleep(400);
    await page.waitForFunction(() => !document.querySelector("#send-btn").classList.contains("stop"), null, { timeout: 30000 });
    await sleep(500);
  }
  const reqs = sameUploads((await get(ENG + "/_fake/requests")).slice(before).map(normReq));
  const sessions = await get(BASE + "/api/sessions");
  const sid = sessions.sort((a, b) => b.updated - a.updated)[0].id;
  const sess = await get(BASE + "/api/sessions?id=" + sid);
  return { reqs, msgs: sameUploads((sess.messages || []).map(normMsg)) };
}

function diff(a, b, path) {
  if (JSON.stringify(a) === JSON.stringify(b)) return [];
  if (typeof a !== "object" || typeof b !== "object" || !a || !b) return [path + ": " + JSON.stringify(a) + " ≠ " + JSON.stringify(b)];
  const out = [];
  for (const k of new Set(Object.keys(a).concat(Object.keys(b)))) out.push(...diff(a[k], b[k], path + "." + k));
  return out;
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  let pass = 0;
  for (const sc of SCENARIOS) {
    const a = await runScenario(page, sc, false);
    const b = await runScenario(page, sc, true);
    const d = diff(a.reqs, b.reqs, "requests").concat(diff(a.msgs, b.msgs, "session"));
    if (d.length) console.log("DIFF  " + sc.name + "\n      " + d.slice(0, 6).join("\n      "));
    else { pass++; console.log("same  " + sc.name + "  (" + a.reqs.length + " requests)"); }
  }
  console.log(pass + " of " + SCENARIOS.length + " the same" + (errors.length ? "; page errors: " + errors.join(" | ") : ""));
  await browser.close();
  process.exit(pass === SCENARIOS.length && !errors.length ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(2); });
