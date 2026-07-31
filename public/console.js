/* ByteBunker Console — vanilla JS, no build, no dependencies.
   Everything on screen is real: streamed from the upstream, measured on the
   wire, or read from this server's own log. Nothing is simulated. */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const EFFORT = ["none", "minimal", "low", "medium", "high", "xhigh"];
  const EFFORT_LABEL = ["None", "Min", "Low", "Medium", "High", "XHigh"];

  const state = {
    screen: "playground",
    cfg: { nodes: [], identity: {}, upstream: "", telemetry: false },
    models: [],
    model: null,
    params: { temp: 0.7, topP: 0.95, topK: 40, rep: 1.05, maxTok: 8192,
              seed: 0, effort: 4, json: false, stops: "", sys: "" },
    messages: [],           // {role:'user'|'bot', content, reasoning, meta, error}
    streaming: false,
    abort: null,
    session: null,          // current session id
    ctxUsed: null,          // prompt+completion tokens of the last turn
    hist: {},               // node name -> util history for sparklines
  };

  /* ---------------- theme ---------------- */
  function applyTheme(dark) {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    try { localStorage.setItem("bb.theme", dark ? "dark" : "light"); } catch (e) {}
  }
  applyTheme((() => {
    try { return localStorage.getItem("bb.theme") === "dark"; } catch (e) { return false; }
  })());
  $("theme-btn").onclick = () =>
    applyTheme(document.documentElement.getAttribute("data-theme") !== "dark");

  /* ---------------- nav ---------------- */
  const screens = ["playground", "sessions", "models", "tuning", "batch", "cluster", "usage"];
  function go(s) {
    state.screen = s;
    screens.forEach((id) => {
      $("screen-" + id).classList.toggle("on", id === s);
      document.querySelector(`[data-nav="${id}"]`).classList.toggle("on", id === s);
    });
    $("panel").classList.toggle("on", s === "playground" && panelWanted);
    if (s === "sessions") renderSessions();
    if (s === "usage") renderUsage();
  }
  document.querySelectorAll("[data-nav]").forEach((b) => (b.onclick = () => go(b.dataset.nav)));

  let panelWanted = window.innerWidth >= 1080;
  $("panel-btn").onclick = () => { panelWanted = !panelWanted; go(state.screen); };
  $("code-btn").onclick = () => { panelWanted = true; showCode(true); go("playground"); };
  $("code-close").onclick = () => showCode(false);
  function showCode(on) {
    $("panel-params").style.display = on ? "none" : "flex";
    $("panel-code").style.display = on ? "flex" : "none";
    if (on) $("code-snippet").textContent = snippet();
  }

  /* ---------------- params ---------------- */
  const P = state.params;
  const fmtTok = (v) => (v >= 1000 ? (v / 1000).toFixed(1).replace(".0", "") + "k" : String(v));
  function bindRange(id, valId, key, fmt) {
    $(id).oninput = (e) => { P[key] = +e.target.value; $(valId).textContent = fmt(P[key]); };
  }
  bindRange("temp", "temp-val", "temp", (v) => v.toFixed(2));
  bindRange("topp", "topp-val", "topP", (v) => v.toFixed(2));
  bindRange("topk", "topk-val", "topK", (v) => String(v));
  bindRange("rep", "rep-val", "rep", (v) => v.toFixed(2));
  bindRange("maxtok", "max-val", "maxTok", fmtTok);
  bindRange("effort", "effort-val", "effort", (v) => EFFORT_LABEL[v]);
  $("seed").oninput = (e) => { P.seed = parseInt(e.target.value || "0", 10) || 0; };
  $("stops").oninput = (e) => { P.stops = e.target.value; };
  $("sys").oninput = (e) => { P.sys = e.target.value; $("sys-len").textContent = P.sys.length + " chars"; };
  $("json-switch").onclick = () => { P.json = !P.json; $("json-switch").classList.toggle("on", P.json); };
  $("reset-params").onclick = () => location.reload();
  $("model-select").onchange = (e) => { state.model = e.target.value; servingLine(); };

  function snippet() {
    const lines = [
      "from openai import OpenAI", "",
      "client = OpenAI(",
      `    base_url="${state.cfg.upstream || "http://HOST:PORT/v1"}",`,
      '    api_key="bb-local",', ")", "",
      "stream = client.chat.completions.create(",
      `    model="${state.model || "MODEL"}",`,
      "    messages=[",
      '        {"role": "system", "content": SYSTEM},',
      '        {"role": "user", "content": prompt},',
      "    ],",
      `    temperature=${P.temp},`,
      `    top_p=${P.topP},`,
      `    max_tokens=${P.maxTok},`,
      P.seed ? `    seed=${P.seed},` : null,
      P.json ? '    response_format={"type": "json_object"},' : null,
      "    stream=True,",
      "    extra_body={",
      `        "top_k": ${P.topK},`,
      `        "repetition_penalty": ${P.rep},`,
      P.effort !== 4 ? `        "reasoning_effort": "${EFFORT[P.effort]}",` : null,
      "    },", ")", "",
      "for chunk in stream:",
      '    print(chunk.choices[0].delta.content or "", end="")',
    ];
    return lines.filter(Boolean).join("\n");
  }

  /* ---------------- transcript rendering ---------------- */
  // One left-to-right pass so parts keep document order and a literal <think>
  // inside a code fence stays inside the code. An unterminated fence or think
  // block (mid-stream) runs to end of text.
  function parseParts(m) {
    const parts = [];
    if (m.reasoning) parts.push({ kind: "think", text: m.reasoning });
    const text = m.content || "";
    const re = /```([\w+-]*)\n?([\s\S]*?)(```|$)|<think>([\s\S]*?)(<\/think>|$)/g;
    let last = 0, mt;
    while ((mt = re.exec(text))) {
      const before = text.slice(last, mt.index).trim();
      if (before) parts.push({ kind: "text", text: before });
      if (mt[4] !== undefined) {
        if (mt[4].trim()) parts.push({ kind: "think", text: mt[4].trim() });
      } else {
        parts.push({ kind: "code", lang: mt[1] || "text", text: mt[2].replace(/\n$/, "") });
      }
      last = re.lastIndex;
      if (mt.index === re.lastIndex) re.lastIndex++; // safety on empty match
    }
    const tail = text.slice(last).trim();
    if (tail) parts.push({ kind: "text", text: tail });
    return parts;
  }

  // History resent to the model must not include think blocks: reasoning
  // models expect prior thinking stripped, and it balloons context otherwise.
  function stripThink(s) {
    return (s || "").replace(/<think>[\s\S]*?(<\/think>|$)/g, "").trim();
  }

  function buildMessageNode(m) {
    const wrap = document.createElement("div");
    wrap.className = "msg";
    if (m.role === "user") {
      const u = document.createElement("div");
      u.className = "msg-user";
      const b = document.createElement("div");
      b.textContent = m.content;
      u.appendChild(b);
      wrap.appendChild(u);
      return wrap;
    }
    const bot = document.createElement("div");
    bot.className = "msg-bot";
    for (const p of parseParts(m)) {
      if (p.kind === "think") {
        const d = document.createElement("div");
        d.className = "part-think";
        d.innerHTML = '<span class="label"></span><div class="body"></div>';
        // provenance is stamped on the message at send time — the label must
        // not follow the slider after the fact
        d.querySelector(".label").textContent =
          m.effort != null ? "Reasoning · " + EFFORT_LABEL[m.effort] : "Reasoning";
        d.querySelector(".body").textContent = p.text;
        bot.appendChild(d);
      } else if (p.kind === "code") {
        const d = document.createElement("div");
        d.className = "part-code";
        const h = document.createElement("div");
        h.className = "head";
        h.innerHTML = "<span></span><span></span>";
        h.children[0].textContent = p.lang;
        h.children[1].textContent = m.model || "";
        const pre = document.createElement("pre");
        pre.textContent = p.text;
        d.appendChild(h); d.appendChild(pre);
        bot.appendChild(d);
      } else {
        const d = document.createElement("div");
        d.className = "part-text";
        d.textContent = p.text;
        bot.appendChild(d);
      }
    }
    if (m.error) {
      const e = document.createElement("div");
      e.className = "msg-err";
      e.textContent = m.error;
      bot.appendChild(e);
    }
    if (m.meta) {
      const mt = document.createElement("div");
      mt.className = "msg-meta mono";
      mt.textContent = m.meta;
      bot.appendChild(mt);
    }
    wrap.appendChild(bot);
    return wrap;
  }

  function atBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  // lastOnly: during streaming, rebuild just the in-flight message — a full
  // transcript rebuild per frame is O(n²) in stream length and janks long
  // replies. Scroll pins only if the user is already at the bottom.
  function renderMessages(lastOnly) {
    const box = $("msgs");
    const tr = $("transcript");
    const pin = atBottom(tr);
    const empty = state.messages.length === 0;
    $("empty-state").style.display = empty ? "flex" : "none";
    box.hidden = empty;
    if (lastOnly && box.lastElementChild && state.messages.length &&
        box.childElementCount === state.messages.length) {
      box.replaceChild(buildMessageNode(state.messages[state.messages.length - 1]),
                       box.lastElementChild);
    } else {
      box.textContent = "";
      for (const m of state.messages) box.appendChild(buildMessageNode(m));
    }
    if (pin) tr.scrollTop = tr.scrollHeight;
  }

  /* ---------------- chat ---------------- */
  function servingLine(txt) {
    let base = state.model ? `${state.model} · ${state.cfg.upstream}` : "no models at upstream";
    if (!txt && state.ctxUsed) {
      const pct = Math.round(state.ctxUsed / 262144 * 100);
      base += ` · ctx ${(state.ctxUsed / 1000).toFixed(1)}k / 262k (${pct}%)`;
    }
    $("serving-line").textContent = txt || base;
  }

  async function send(text) {
    if (!text || !text.trim() || state.streaming || !state.model) return;
    const user = { role: "user", content: text.trim() };
    // model + effort stamped now: the transcript renders provenance, not
    // whatever the controls happen to say later
    const bot = { role: "bot", content: "", reasoning: "", meta: "",
                  model: state.model, effort: P.effort };
    state.messages.push(user, bot);
    state.streaming = true;
    $("send-btn").classList.add("stop");
    renderMessages();

    const msgs = [];
    if (P.sys.trim()) msgs.push({ role: "system", content: P.sys.trim() });
    for (const m of state.messages.slice(0, -1)) {
      msgs.push({ role: m.role === "bot" ? "assistant" : "user",
                  content: m.role === "bot" ? stripThink(m.content) : m.content });
    }
    const body = {
      model: state.model, messages: msgs,
      temperature: P.temp, top_p: P.topP, max_tokens: P.maxTok,
      top_k: P.topK, repetition_penalty: P.rep,
    };
    if (P.seed) body.seed = P.seed;
    if (P.json) body.response_format = { type: "json_object" };
    if (P.stops.trim()) body.stop = P.stops.split(",").map((s) => s.trim()).filter(Boolean);
    if (P.effort !== 4) body.reasoning_effort = EFFORT[P.effort];

    const t0 = performance.now();
    let tFirst = null, tLast = null, usage = null, chunks = 0, finishReason = null;
    const ctl = new AbortController();
    state.abort = ctl;

    let raf = 0;
    const paint = () => { raf = 0; renderMessages(true); };
    const queue = () => { if (!raf) raf = requestAnimationFrame(paint); };

    try {
      const r = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body), signal: ctl.signal,
      });
      if (!r.ok) throw new Error((await r.text()).slice(0, 400));
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (payload === "[DONE]") continue;
          let obj;
          try { obj = JSON.parse(payload); } catch (e) { continue; }
          if (obj.usage) usage = obj.usage;
          const c0 = obj.choices && obj.choices[0];
          if (c0 && c0.finish_reason) finishReason = c0.finish_reason;
          const delta = c0 && c0.delta;
          if (!delta) continue;
          const got = (delta.content || "") + (delta.reasoning_content || "") + (delta.reasoning || "");
          if (got) {
            const now = performance.now();
            if (tFirst === null) tFirst = now;
            tLast = now; chunks++;
            if (delta.reasoning_content) bot.reasoning += delta.reasoning_content;
            if (delta.reasoning) bot.reasoning += delta.reasoning;
            if (delta.content) bot.content += delta.content;
            queue();
          }
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") bot.error = "upstream error: " + e.message;
    }

    // Chunk count is not a token count (speculative decoding packs several
    // tokens per delta). Without server usage, mark everything estimated —
    // on screen with "~", in the ledger with a flag the medians exclude.
    const exact = !!(usage && usage.completion_tokens);
    const toks = exact ? usage.completion_tokens : chunks;
    const approx = exact ? "" : "~";
    const ttft = tFirst ? ((tFirst - t0) / 1000) : null;
    let decode = null;
    if (tFirst && tLast && tLast > tFirst && toks > 1) {
      decode = (toks - 1) / ((tLast - tFirst) / 1000);
    }
    const rtok = usage && usage.completion_tokens_details &&
                 usage.completion_tokens_details.reasoning_tokens;
    bot.meta = [
      bot.model,
      approx + toks + " tok" + (rtok ? " (" + rtok + " thinking)" : ""),
      decode ? approx + decode.toFixed(1) + " tok/s" : null,
      ttft !== null ? "ttft " + Math.round(ttft * 1000) + " ms" : null,
      finishReason === "length"
        ? "\u26a0 stopped at Max tokens \u2014 thinking shares the budget; raise it in the panel"
        : null,
    ].filter(Boolean).join("  \u00b7  ");
    state.streaming = false;
    state.abort = null;
    if (usage && usage.prompt_tokens) {
      state.ctxUsed = (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
    }
    $("send-btn").classList.remove("stop");
    servingLine();
    renderMessages();
    saveSession();
    fetch("/api/usage-event", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: bot.model,
        prompt_tokens: usage && usage.prompt_tokens,
        completion_tokens: toks,
        ttft_s: ttft, decode_tok_s: decode,
        estimated: !exact,
      }),
    }).catch(() => {});
  }

  $("send-btn").onclick = () => {
    if (state.streaming) { state.abort && state.abort.abort(); return; }
    const el = $("input"); const v = el.value; el.value = ""; send(v);
  };
  $("input").onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!state.streaming) { const v = e.target.value; e.target.value = ""; send(v); }
    }
  };
  document.querySelectorAll("[data-chip]").forEach((b) => (b.onclick = () => send(b.dataset.chip)));

  /* ---------------- sessions ---------------- */
  function saveSession() {
    if (!state.messages.length) return;
    if (!state.session) state.session = "s-" + Date.now().toString(36);
    const first = state.messages.find((m) => m.role === "user");
    const toks = state.messages.reduce((a, m) => a + (m.content || "").length, 0);
    fetch("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: state.session,
        title: first ? first.content.slice(0, 80) : "untitled",
        model: state.model,
        turns: state.messages.length,
        chars: toks,
        updated: Date.now(),
        messages: state.messages.map((m) => ({
          role: m.role, content: m.content, reasoning: m.reasoning || "",
          meta: m.meta || "", model: m.model || "", effort: m.effort,
          error: m.error || "",
        })),
      }),
    }).catch(() => {});
  }

  async function renderSessions() {
    const box = $("sessions-box");
    let list = [];
    try { list = await (await fetch("/api/sessions")).json(); } catch (e) {}
    if (!list.length) {
      box.innerHTML = '<div class="empty-state"><b>No sessions yet</b><span>Conversations save here automatically, on this host only.</span></div>';
      return;
    }
    box.textContent = "";
    const t = document.createElement("div");
    t.className = "table";
    t.innerHTML = '<div class="trow head"><span>Session</span><span>Model</span><span>Turns</span><span>Chars</span><span>Last active</span></div>';
    for (const s of list) {
      const r = document.createElement("div");
      r.className = "trow";
      const when = new Date(s.updated || 0);
      r.innerHTML = '<span class="t"></span><span class="m mono"></span><span class="m mono"></span><span class="m mono"></span><span class="w"></span>';
      r.children[0].textContent = s.title || "untitled";
      r.children[1].textContent = s.model || "";
      r.children[2].textContent = s.turns || 0;
      r.children[3].textContent = (s.chars || 0).toLocaleString();
      r.children[4].textContent = when.toLocaleString();
      r.onclick = () => {
        state.session = s.id;
        state.messages = (s.messages || []).map((m) => ({ ...m }));
        // restore the session's model too — otherwise the next turn silently
        // sends yesterday's conversation to whatever is selected today
        if (s.model && state.models.includes(s.model)) {
          state.model = s.model;
          $("model-select").value = s.model;
          servingLine();
        }
        go("playground");
        renderMessages();
      };
      t.appendChild(r);
    }
    box.appendChild(t);
  }

  /* ---------------- models ---------------- */
  async function loadModels() {
    let data = { data: [] };
    try { data = await (await fetch("/api/models")).json(); } catch (e) {}
    state.models = (data.data || []).map((m) => m.id);
    const sel = $("model-select");
    sel.textContent = "";
    for (const id of state.models) {
      const o = document.createElement("option");
      o.value = id; o.textContent = id;
      sel.appendChild(o);
    }
    if (!state.model && state.models.length) state.model = state.models[0];
    if (state.model) sel.value = state.model;
    servingLine();
    $("models-sub").textContent = state.models.length
      ? `${state.models.length} served · OpenAI-compatible · ${state.cfg.upstream}`
      : "upstream unreachable — check config.json";
    const facts = $("model-facts");
    facts.innerHTML = "";
    const add = (k, v) => {
      const d = document.createElement("div");
      d.innerHTML = '<span class="k"></span><span class="mono"></span>';
      d.children[0].textContent = k;
      d.children[1].textContent = v;
      facts.appendChild(d);
    };
    add("Upstream", state.cfg.upstream.replace(/^https?:\/\//, ""));
    add("Models", String(state.models.length || "—"));
    const lm = $("loaded-models");
    lm.textContent = "";
    for (const id of state.models) {
      const c = document.createElement("div");
      c.className = "row-card";
      c.innerHTML = '<div class="name-col"><b></b><small class="mono">served via upstream</small></div>';
      c.querySelector("b").textContent = id;
      lm.appendChild(c);
    }
    if (!state.models.length) {
      lm.innerHTML = '<div class="empty-state"><b>Upstream unreachable</b><span>Point config.json upstream_url at the gateway or a vLLM server, then reload.</span></div>';
    }
  }

  /* ---------------- telemetry ---------------- */
  const fmtUp = (s) => {
    if (s == null) return "—";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
    return d + "d " + String(h).padStart(2, "0") + "h";
  };
  function sparkline(arr) {
    if (!arr || arr.length < 2) return "";
    const n = arr.length;
    return arr.map((v, i) =>
      ((i * 100) / (n - 1)).toFixed(2) + "," +
      (25 - Math.max(0, Math.min(1, v / 100)) * 23).toFixed(2)).join(" ");
  }

  async function pollTelemetry() {
    if (!state.cfg.telemetry) {
      $("side-health").innerHTML = '<span class="dot" style="background:var(--faint);animation:none"></span>no telemetry';
      $("cluster-poll").textContent = "prometheus not configured";
      $("cluster-note").innerHTML = '<div class="empty-state" style="margin-top:14px"><b>Telemetry off</b><span>Set prometheus_url in config.json to light this screen up with real numbers from your existing exporter stack.</span></div>';
      return;
    }
    let t = { nodes: [] };
    try { t = await (await fetch("/api/telemetry")).json(); } catch (e) {}
    const side = $("side-nodes");
    side.textContent = "";
    const cards = $("node-cards");
    cards.textContent = "";
    let healthy = 0;
    for (const n of t.nodes) {
      if (n.util != null || n.mem_used_gb != null) healthy++;
      const util = n.util != null ? Math.round(n.util) : null;
      (state.hist[n.name] = state.hist[n.name] || []).push(util || 0);
      state.hist[n.name] = state.hist[n.name].slice(-44);

      const mini = document.createElement("div");
      mini.className = "node-mini";
      mini.innerHTML = '<div class="row"><span class="mono" style="color:var(--muted)"></span><b class="mono"></b></div><div class="bar"><div></div></div>';
      mini.querySelector("span").textContent = n.name;
      mini.querySelector("b").textContent = util != null ? util + "%" : "—";
      mini.querySelector(".bar>div").style.width = (util || 0) + "%";
      side.appendChild(mini);

      // spec line comes from config.json (operator-declared) or is omitted —
      // the console never invents hardware
      const spec = (state.cfg.nodes.find((c) => c.name === n.name) || {}).spec || "";
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML =
        '<div style="display:flex;align-items:flex-start;gap:10px">' +
        '<div style="display:flex;flex-direction:column;gap:2px"><span class="mono" style="font-size:15px;font-weight:600"></span>' +
        (spec ? '<span class="spec" style="font-size:11.5px;color:var(--faint)"></span>' : '') +
        '<div style="flex:1"></div><span class="pill"><span class="d"></span>reporting</span></div>' +
        '<div style="display:flex;flex-direction:column;gap:6px">' +
        '<div style="display:flex;align-items:baseline;justify-content:space-between"><span style="font-size:12px;color:var(--muted)">GPU utilization</span>' +
        '<span class="mono" style="font-size:19px;font-weight:600"></span></div>' +
        '<svg viewBox="0 0 100 26" preserveAspectRatio="none" style="width:100%;height:44px;display:block"><polyline fill="none" stroke="var(--accent)" stroke-width="1.1" vector-effect="non-scaling-stroke"/></svg></div>' +
        '<div style="display:flex;flex-direction:column;gap:5px">' +
        '<div style="display:flex;justify-content:space-between;font-size:12px"><span style="color:var(--muted)">Unified memory</span><span class="mono 0"></span></div>' +
        '<div class="bar5"><div></div></div></div>' +
        '<div class="stat-grid">' +
        '<div><span class="kv-label">Temp</span><span class="v"></span></div>' +
        '<div><span class="kv-label">Power</span><span class="v"></span></div>' +
        '<div><span class="kv-label">CPU</span><span class="v"></span></div>' +
        '<div><span class="kv-label">Uptime</span><span class="v"></span></div></div>';
      card.querySelector(".mono").textContent = n.name;
      if (spec) card.querySelector(".spec").textContent = spec;
      card.querySelectorAll(".mono")[1].textContent = util != null ? util + "%" : "—";
      card.querySelector("polyline").setAttribute("points", sparkline(state.hist[n.name]));
      const memLine = card.querySelectorAll(".mono")[2];
      memLine.textContent = (n.mem_used_gb != null && n.mem_total_gb)
        ? n.mem_used_gb + " / " + n.mem_total_gb + " GB" : "—";
      card.querySelector(".bar5>div").style.width =
        (n.mem_used_gb != null && n.mem_total_gb)
          ? (n.mem_used_gb / n.mem_total_gb) * 100 + "%" : "0";
      const vs = card.querySelectorAll(".stat-grid .v");
      vs[0].textContent = n.temp != null ? Math.round(n.temp) + "°C" : "—";
      vs[1].textContent = n.power != null ? Math.round(n.power) + " W" : "—";
      vs[2].textContent = n.cpu != null ? Math.round(n.cpu) + "%" : "—";
      vs[3].textContent = fmtUp(n.uptime_s);
      cards.appendChild(card);
    }
    $("side-health").innerHTML = healthy === t.nodes.length && healthy > 0
      ? '<span class="dot"></span>healthy'
      : '<span class="dot err"></span>' + healthy + "/" + t.nodes.length;
    $("cluster-poll").innerHTML = '<span class="dot"></span>polling 5s';
  }

  /* ---------------- usage ---------------- */
  async function renderUsage() {
    let u = {};
    try { u = await (await fetch("/api/usage")).json(); } catch (e) {}
    const box = $("usage-box");
    box.textContent = "";
    const cards = document.createElement("div");
    cards.className = "usage-cards";
    const mk = (label, big, small) => {
      const c = document.createElement("div");
      c.className = "card";
      c.innerHTML = '<span class="kv-label"></span><span class="big"></span><small></small>';
      c.children[0].textContent = label;
      c.children[1].textContent = big;
      c.children[2].textContent = small;
      cards.appendChild(c);
    };
    const tot = u.total_out || 0;
    mk("Tokens generated", tot >= 1e6 ? (tot / 1e6).toFixed(1) + " M" : tot.toLocaleString(), "completion tokens, 14 days");
    mk("Median throughput", u.median_tok_s ? u.median_tok_s.toFixed(1) : "—", "tok/s per request");
    mk("Requests", u.requests != null ? String(u.requests) : "—", "through this console");
    mk("Frontier-API equivalent", "$" + (u.frontier_saved_usd || 0).toFixed(2), "not spent, at configured rates");
    box.appendChild(cards);

    const days = u.days || {};
    const keys = Object.keys(days).sort();
    if (keys.length) {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = '<div style="display:flex;align-items:baseline;gap:10px"><span style="font-size:13.5px;font-weight:600">Tokens per day</span><span style="font-size:11.5px;color:var(--faint)">output only</span></div><div class="bars"></div>';
      const bars = card.querySelector(".bars");
      const mx = Math.max(...keys.map((k) => days[k]));
      keys.forEach((k, i) => {
        const d = document.createElement("div");
        d.innerHTML = '<div class="b"></div><small class="mono"></small>';
        d.querySelector(".b").style.height = (18 + (days[k] / mx) * 82) + "%";
        if (i === keys.length - 1) d.querySelector(".b").classList.add("hot");
        d.querySelector("small").textContent = k;
        d.querySelector(".b").title = days[k].toLocaleString() + " tokens";
        bars.appendChild(d);
      });
      box.appendChild(card);
    }

    const models = u.by_model || {};
    const mkeys = Object.keys(models).sort((a, b) => models[b] - models[a]);
    if (mkeys.length) {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = '<span style="font-size:13.5px;font-weight:600">By model</span>';
      const mx = models[mkeys[0]] || 1;
      for (const k of mkeys) {
        const r = document.createElement("div");
        r.className = "mix-row";
        r.innerHTML = '<span class="n mono"></span><div class="bar8"><div></div></div><span class="v mono"></span>';
        r.querySelector(".n").textContent = k;
        r.querySelector(".bar8>div").style.width = (models[k] / mx) * 100 + "%";
        r.querySelector(".v").textContent = models[k] >= 1e6
          ? (models[k] / 1e6).toFixed(1) + " M" : models[k].toLocaleString();
        card.appendChild(r);
      }
      box.appendChild(card);
    }
    if (!keys.length) {
      box.innerHTML += '<div class="empty-state"><b>Nothing logged yet</b><span>Every completion through the Playground lands in this ledger.</span></div>';
    }
  }

  /* ---------------- boot ---------------- */
  (async () => {
    try { state.cfg = await (await fetch("/api/config")).json(); } catch (e) {}
    $("who-user").textContent = state.cfg.identity.user || "local";
    $("who-host").textContent = state.cfg.identity.host || "";
    $("avatar").textContent = (state.cfg.identity.user || "B")[0].toUpperCase();
    $("code-endpoint").textContent = state.cfg.upstream;
    if (state.cfg.nodes.length) $("cluster-sub").textContent = state.cfg.nodes.length + " nodes configured";
    await loadModels();
    pollTelemetry();
    setInterval(pollTelemetry, 5000);
    setInterval(() => { if (!state.models.length) loadModels(); }, 15000);
  })();
})();
