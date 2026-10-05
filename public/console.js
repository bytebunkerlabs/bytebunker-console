/* ByteBunker Console — vanilla JS, no build, no dependencies.
   Everything on screen is real: streamed from the upstream, measured on the
   wire, or read from this server's own log. Nothing is simulated. */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const EFFORT = ["none", "minimal", "low", "medium", "high", "xhigh"];
  const EFFORT_LABEL = ["None", "Min", "Low", "Medium", "High", "XHigh"];
  // Seconds before the heartbeat stops saying "working" and starts saying
  // "this is longer than normal". Prefill is generous: a 100k-token prompt on
  // a two-node rack legitimately takes tens of seconds before the first token.
  const STALL_PREFILL = 45;
  const STALL_STREAM = 10;   // mid-stream gaps this long are not normal decode

  const state = {
    screen: "playground",
    cfg: { identity: {}, upstream: "", monitors: 0 },
    models: [],
    model: null,
    params: { temp: 0.7, topP: 0.95, topK: 40, rep: 1.05, maxTok: 8192,
              seed: 0, effort: 4, json: false, stops: "", sys: "",
              autoCompress: true, maxHops: 40 },
    messages: [],           // {role:'user'|'bot', content, reasoning, meta, error}
    streaming: false,
    abort: null,
    session: null,          // current session id
    ctxUsed: null,          // prompt+completion tokens of the last turn
    ctxLearned: {},         // model id -> window the engine really serves, per its own 400
    calib: null,            // {model, chars, tokens}: the last prompt the engine counted for us
    tools: [],              // MCP tools discovered via /api/tools
    toolsOn: true,          // send them to the model?
    skills: [],             // skill catalog summaries from /api/skills
    activeSkills: [],       // skills attached to the current chat (names)
    skillBodies: {},        // name -> body, fetched lazily and cached
    plugins: [],            // plugin list from /api/plugins
    agentAbort: null,       // AbortController for a live agent run's stream
    agentRun: null,         // the server's id for that run: Stop cancels it there
    agentDetail: null,      // {kind:"run"|"slave", id} shown in the right-hand pane
    hist: {},               // the agents worker's GPU and output traces (monitor nodes bring their own)
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
  const screens = ["playground", "sessions", "video", "skills", "plugins", "mcp", "agents", "jobs", "gateways", "models", "recipes", "cluster", "settings", "usage"];
  function go(s) {
    state.screen = s;
    // the screen lives in the URL hash: a reload stays put, a link can open one
    try { if (location.hash.slice(1) !== s) history.replaceState(null, "", s === "playground" ? location.pathname + location.search : "#" + s); } catch (e) {}
    screens.forEach((id) => {
      $("screen-" + id).classList.toggle("on", id === s);
      document.querySelector(`[data-nav="${id}"]`).classList.toggle("on", id === s);
    });
    $("panel").classList.toggle("on", s === "playground" && panelWanted);
    if (s === "sessions") renderSessions();
    if (s === "skills") renderSkills();
    if (s === "plugins") renderPlugins();
    if (s === "mcp") renderMcp();
    if (s === "jobs") renderJobs();
    if (s === "gateways") renderGateways();
    if (s === "settings") renderSettings();
    if (s === "agents") renderAgents();
    if (s === "recipes") renderRecipes();
    if (s === "cluster") pollCluster(true);
    if (s === "usage") renderUsage();
    if (s === "video") vidRefresh();
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
  bindRange("maxhops", "maxhops-val", "maxHops", String);
  $("seed").oninput = (e) => { P.seed = parseInt(e.target.value || "0", 10) || 0; };
  $("stops").oninput = (e) => { P.stops = e.target.value; };
  $("sys").oninput = (e) => { P.sys = e.target.value; $("sys-len").textContent = P.sys.length + " chars"; };
  $("json-switch").onclick = () => { P.json = !P.json; $("json-switch").classList.toggle("on", P.json); };
  $("compress-switch").onclick = () => {
    P.autoCompress = !P.autoCompress;
    $("compress-switch").classList.toggle("on", P.autoCompress);
  };
  $("tools-switch").onclick = () => { state.toolsOn = !state.toolsOn; $("tools-switch").classList.toggle("on", state.toolsOn); };
  $("reset-params").onclick = () => location.reload();
  $("model-select").onchange = (e) => { state.model = e.target.value; servingLine(); };

  function snippet() {
    const lines = [
      "from openai import OpenAI", "",
      "client = OpenAI(",
      `    base_url="${gatewayUrl(state.model) || "http://HOST:PORT/v1"}",`,
      '    api_key="YOUR_GATEWAY_KEY",', ")", "",
      "stream = client.chat.completions.create(",
      `    model="${baseId(state.model) || "MODEL"}",`,
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

  // Capabilities published by the server for the selected model. The fallback
  // is deliberately conservative-but-working: assume tools are fine, assume no
  // effort dial, assume prior thinking should be stripped.
  const CAPS_FALLBACK = { tools: true, effort: [], ctk: {}, strip_reasoning: true,
                          ctx: 131072 };
  function capsFor(id) {
    const c = (state.caps && state.caps[id]) || CAPS_FALLBACK;
    // The manifest says what the model can do; the engine may be serving
    // less (measured: DeepSeek-V4 vision at 262k under a 1M manifest). Its
    // overflow error names the real window, and that figure wins.
    const learned = state.ctxLearned[id];
    return learned ? Object.assign({}, c, { ctx: learned }) : c;
  }

  // The server forwards upstream errors as {"error": text}. Dig the sentence
  // out of that (and out of any envelope the proxy chain left inside it);
  // hand back what we were given if it is not JSON at all.
  function upstreamText(raw) {
    let s = String(raw || "");
    for (let i = 0; i < 3; i++) {
      let o;
      try { o = JSON.parse(s); } catch (e) { break; }
      if (!o || typeof o !== "object") break;
      let e = "error" in o ? o.error : ("message" in o ? o.message : o.detail);
      if (e && typeof e === "object") e = e.message;
      if (typeof e !== "string") break;
      s = e;
    }
    return s;
  }

  // vLLM's refusal of prompt + max_tokens > window names the window and a
  // prompt size, in one of two phrasings. The newer one says "at least N"
  // because its tokenizer stops at N = window - max_tokens + 1: a floor, not
  // the prompt's length. `bound` says which kind of number `prompt` is.
  function parseCtxError(text) {
    const win = /maximum context length is (\d+)/.exec(text);
    if (!win) return null;
    let m = /prompt contains (at least )?(\d+) input tokens/.exec(text);
    if (m) return { win: +win[1], prompt: +m[2], bound: !!m[1] };
    m = /\((\d+) in the messages/.exec(text);
    return m ? { win: +win[1], prompt: +m[1], bound: false } : null;
  }

  // How many tokens is this prompt? Chars ÷ 3.2 is a guess good to a few
  // percent, and at 200k tokens a few percent is thousands of tokens — more
  // than the output budget can absorb near the edge of the window. So the
  // engine's own count of the last prompt it saw (usage.prompt_tokens, kept
  // in state.calib with the chars that produced it) calibrates the ratio for
  // this model and conversation. `margin` is what to leave unspent: 1% when
  // calibrated plus a quarter of whatever is new since the measured prompt
  // (a fresh paste of code tokenizes unlike the prose before it), 2% when
  // guessing. Tool definitions ride along on every request, so they count.
  // Image parts are base64 blobs; the engine bills them as a few hundred to a
  // couple of thousand tokens, not one per three characters.
  function promptShape(msgs) {
    return msgs.map((m) => {
      if (!Array.isArray(m.content)) return m;
      return Object.assign({}, m, { content: m.content.map((p) =>
        p && p.type === "image_url" ? { type: "image", placeholder: "x".repeat(4000) } : p) });
    });
  }
  function estimatePrompt(msgs, tools, model) {
    const chars = JSON.stringify({ m: promptShape(msgs), t: tools || null }).length;
    const k = state.calib;
    const cal = !!(k && k.model === model && k.tokens > 1000 && k.chars > 4000);
    const ratio = cal ? Math.min(6, Math.max(2, k.chars / k.tokens)) : 3.2;
    const tokens = Math.ceil(chars / ratio);
    const fresh = cal ? Math.max(0, chars - k.chars) : 0;
    const margin = 256 + Math.ceil(tokens * (cal ? 0.01 : 0.02)) +
                   Math.ceil(fresh / ratio * 0.25);
    return { tokens, margin, calibrated: cal };
  }

  function copyBtn(getText) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "copy-btn";
    b.textContent = "copy";
    b.setAttribute("aria-label", "Copy to clipboard");
    b.onclick = (ev) => {
      // lives inside a <summary>: a plain click must copy, not toggle
      ev.preventDefault(); ev.stopPropagation();
      const done = () => {
        b.textContent = "copied"; b.classList.add("did");
        setTimeout(() => { b.textContent = "copy"; b.classList.remove("did"); }, 1200);
      };
      const txt = getText();
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done, done);
      } else {
        const t = document.createElement("textarea");
        t.value = txt; document.body.appendChild(t); t.select();
        try { document.execCommand("copy"); } catch (e) {}
        t.remove(); done();
      }
    };
    return b;
  }

  function buildThink(m, text) {
    // A <details>, because thinking is context, not content: open while the
    // turn streams (watching the model work is the point), auto-collapsed on
    // completion, and the reader's own toggle is remembered on the message so
    // streaming rebuilds don't fight them.
    const d = document.createElement("details");
    d.className = "part-think";
    d.open = !!m.thinkOpen;
    const sum = document.createElement("summary");
    const lab = document.createElement("span");
    lab.className = "label";
    // provenance is stamped on the message at send time — the label must
    // not follow the slider after the fact
    lab.textContent =
      (m.effort != null ? "Reasoning · " + EFFORT_LABEL[m.effort] : "Reasoning") +
      " · " + (text.length > 1000 ? (text.length / 1000).toFixed(1) + "k" : text.length) + " chars";
    sum.appendChild(lab);
    sum.appendChild(copyBtn(() => text));
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = text;
    d.appendChild(sum); d.appendChild(body);
    d.addEventListener("toggle", () => { m.thinkOpen = d.open; });
    return d;
  }

  function buildMessageNode(m) {
    const wrap = document.createElement("div");
    wrap.className = "msg";
    if (m.kind === "summary-ack") {
      // the model sees this turn; the reader has the card above it
      wrap.hidden = true;
      return wrap;
    }
    if (m.kind === "summary") {
      const d = document.createElement("details");
      d.className = "msg-summary";
      const sum = document.createElement("summary");
      const lab = document.createElement("span");
      lab.className = "label";
      lab.textContent = "Compressed " + (m.count || 0) + " earlier messages into a summary";
      sum.appendChild(lab);
      if (m.archive) {
        const a = document.createElement("a");
        a.href = "/api/archive/" + m.archive + ".md";
        a.target = "_blank"; a.rel = "noopener";
        a.textContent = "originals · " + m.archive + ".md";
        sum.appendChild(a);
      }
      sum.appendChild(copyBtn(() => m.content));
      const body = document.createElement("div");
      body.className = "body";
      body.textContent = m.content;
      d.appendChild(sum); d.appendChild(body);
      wrap.appendChild(d);
      return wrap;
    }
    if (m.role === "user") {
      const u = document.createElement("div");
      u.className = "msg-user";
      u.style.flexDirection = "column"; u.style.alignItems = "flex-end";
      if (m.attachments && m.attachments.length) {
        const row = document.createElement("div"); row.className = "msg-attach";
        for (const a of m.attachments) {
          if (a.kind === "image" && (a.url || a.dataURL)) {
            const img = document.createElement("img"); img.src = a.url || a.dataURL; img.alt = a.name; img.title = a.name;
            img.onclick = () => openOut(a.url || a.dataURL); img.style.cursor = "zoom-in";
            row.appendChild(img);
          } else {
            const f = document.createElement(a.url ? "a" : "span"); f.className = "f";
            if (a.url) { f.href = a.url; f.target = "_blank"; f.rel = "noopener"; }
            f.textContent = "\ud83d\udcc4 " + a.name + (a.size ? " \u00b7 " + fmtBytes(a.size) : "");
            row.appendChild(f);
          }
        }
        u.appendChild(row);
      }
      if ((m.content || "").trim()) {
        const b = document.createElement("div");
        b.textContent = m.content;
        u.appendChild(b);
      }
      wrap.appendChild(u);
      return wrap;
    }
    const bot = document.createElement("div");
    bot.className = "msg-bot";
    // What the turn did, folded into one line above the answer: the answer
    // is what you came for; the tool traffic is a click away (and all of it
    // is on the Activity tab).
    if (m.toolUse && m.toolUse.length) {
      const d = document.createElement("details"); d.className = "msg-activity";
      const running = m.toolUse.some((t) => t.result === "running\u2026");
      const errs = m.toolUse.filter((t) => t.error).length;
      if (running) d.open = true;
      const sum = document.createElement("summary");
      sum.innerHTML = '<span class="n"></span><span class="what"></span>';
      sum.querySelector(".n").textContent = m.toolUse.length + (m.toolUse.length === 1 ? " tool call" : " tool calls");
      const names = [...new Set(m.toolUse.map((t) => t.name.replace(/^[^_]+__/, "")))];
      sum.querySelector(".what").textContent = (running ? "running \u00b7 " : "") + names.slice(0, 4).join(", ") + (names.length > 4 ? " +" + (names.length - 4) : "") + (errs ? " \u00b7 " + errs + " failed" : "");
      d.appendChild(sum);
      for (const t of m.toolUse) {
        const r = document.createElement("div"); r.className = "row";
        r.innerHTML = '<span class="tn mono"></span><span class="ta mono"></span><span class="ts"></span><button class="more" type="button">details</button>';
        r.querySelector(".tn").textContent = t.name;
        r.querySelector(".ta").textContent = oneLine(t.args, 140);
        const ts = r.querySelector(".ts"); ts.textContent = t.result === "running\u2026" ? "running\u2026" : (t.error ? "error" : "ok"); if (t.error) ts.classList.add("err");
        const pre = document.createElement("pre"); pre.hidden = true;
        pre.textContent = "arguments\n" + prettyJson(t.args) + "\n\nresult\n" + String(t.result).slice(0, 6000);
        r.querySelector(".more").onclick = () => { pre.hidden = !pre.hidden; };
        d.appendChild(r); d.appendChild(pre);
      }
      bot.appendChild(d);
    }
    for (const p of parseParts(m)) {
      if (p.kind === "think") {
        const d = document.createElement("div");
        bot.appendChild(buildThink(m, p.text));
      } else if (p.kind === "code") {
        const d = document.createElement("div");
        d.className = "part-code";
        const h = document.createElement("div");
        h.className = "head";
        const lang = document.createElement("span");
        lang.textContent = p.lang;
        const right = document.createElement("span");
        right.className = "head-right";
        const model = document.createElement("span");
        model.textContent = m.model || "";
        right.appendChild(model);
        right.appendChild(copyBtn(() => p.text));
        h.appendChild(lang); h.appendChild(right);
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
    if (m.notice) {
      const n = document.createElement("div");
      n.className = "msg-note";
      n.textContent = m.notice;
      bot.appendChild(n);
    }
    if (m.error) {
      const e = document.createElement("div");
      e.className = "msg-err";
      e.textContent = m.error;
      bot.appendChild(e);
    }
    // Live status while a turn is in flight. Without this an empty bubble is
    // indistinguishable from a dead connection — prefill on a long prompt
    // emits no deltas for many seconds, so nothing would repaint at all.
    if (m.status) {
      const s = document.createElement("div");
      s.className = "msg-status" + (m.status.slow ? " slow" : "");
      s.innerHTML = '<span class="dot"></span><span class="txt"></span>';
      s.querySelector(".txt").textContent = m.status.text;
      bot.appendChild(s);
    }
    if (m.meta) {
      const mt = document.createElement("div");
      mt.className = "msg-meta mono";
      const txt = document.createElement("span");
      txt.textContent = m.meta;
      mt.appendChild(txt);
      // Good / bad: written to the trace log next to the exact request, so
      // the export can hand back only the turns you'd train on.
      if (m.turn) {
        for (const [val, label] of [[1, "good"], [-1, "bad"]]) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "rate-btn" + (val < 0 ? " bad" : "") + (m.rating === val ? " did" : "");
          b.textContent = label;
          b.title = val > 0 ? "Mark this answer as good" : "Mark this answer as bad";
          b.onclick = () => rate(m, m.rating === val ? 0 : val);
          mt.appendChild(b);
        }
      }
      bot.appendChild(mt);
    }
    wrap.appendChild(bot);
    return wrap;
  }

  function rate(m, val) {
    m.rating = val;
    fetch("/api/rate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: state.session, turn: m.turn, traces: m.traces || [],
                             model: m.model, rating: val }),
    }).catch(() => {});
    renderMessages();
    saveSession();
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
    box.hidden = empty || state.chatView !== "chat";
    $("chat-tabs").hidden = empty;
    updateChatTabs();
    if (state.chatView === "activity") renderActivityView();
    if (state.chatView === "files") renderFilesView();
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
  // A model id may carry a pin ("id@gateway") when two gateways serve it.
  function modelInfo(id) { return (state.modelInfo || {})[id] || {}; }
  function baseId(id) { return modelInfo(id).base_id || id; }
  function gatewayOf(id) { return modelInfo(id).gateway || ""; }
  function gatewayUrl(id) { return ((state.cfg && state.cfg.gateway_urls) || {})[gatewayOf(id)] || ""; }
  function servingLine(txt) {
    const haveGw = !!((state.cfg && state.cfg.gateways) || []).length;
    let base = state.model ? `${baseId(state.model)} · ${gatewayOf(state.model) || "?"}`
      : (haveGw ? "no models reachable — see Gateways" : "no engine connected — Find engines on the Gateways screen");
    if ($("code-endpoint")) $("code-endpoint").textContent = gatewayUrl(state.model) || "";
    if (!txt && state.ctxUsed) {
      // denominator from the manifest — a hardcoded window is wrong the
      // moment the served model changes (262k Inkling vs 1M DeepSeek)
      const win = capsFor(state.model).ctx || 131072;
      const fmt = (v) => v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Math.round(v / 1000) + "k";
      const pct = Math.round(state.ctxUsed / win * 100);
      base += ` · ctx ${fmt(state.ctxUsed)} / ${fmt(win)} (${pct}%)`;
    }
    $("serving-line").textContent = txt || base;
  }

  // One upstream turn. Returns {toolCalls, usage, finishReason} so the caller
  // can decide whether to run tools and go around again.
  async function streamTurn(msgs, bot, body0) {
    const t0 = performance.now();
    let tFirst = null, tLast = null, usage = null, chunks = 0, finishReason = null;
    const calls = [];      // accumulated by index: {id, name, args}
    const ctl = new AbortController();
    state.abort = ctl;
    let raf = 0;
    const queue = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; renderMessages(true); }); };

    // A heartbeat, not a spinner. It reports which phase the turn is actually
    // in and how long it has been there, so "the model is thinking" and "the
    // connection died" stop looking the same. Driven by a timer because the
    // interesting case is precisely when no data is arriving.
    let phase = "connect", shown = "", lastKind = "text", toolChars = 0, toolName = "";
    // When the wire goes quiet, ask the engine itself. Some tool parsers
    // (measured: deepseek_v4 — 64s of silence, then a whole file in 15
    // bursts) buffer the call server-side while the GPU streams into a
    // buffer. A dead stream and a busy-but-buffered one look identical from
    // here; the server-side tok/s from Prometheus is what tells them apart.
    let eng = null, engTick = 0;
    const pollEngine = () => {
      fetch("/api/engine").then((r) => r.json()).then((d) => {
        if (d && d.ok) eng = d;
      }).catch(() => {});
    };
    const beat = setInterval(() => {
      const el = (performance.now() - t0) / 1000;
      let text, slow = false;
      if (phase === "connect") {
        text = "connecting to " + (bot.model || "model") + "…";
        slow = el > 15;
      } else if (tFirst === null) {
        // prefill: no deltas by definition. Long prompts legitimately sit
        // here — at a 1M window, for minutes — so past 20s ask the engine
        // for its prompt-processing rate instead of assuming the worst.
        text = "prefilling · " + el.toFixed(1) + "s";
        if (el > 20 && engTick++ % 4 === 0) pollEngine();
        if (eng && eng.prompt_rate > 50) {
          text += " · engine chewing " + Math.round(eng.prompt_rate).toLocaleString() + " tok/s of prompt";
        } else if (el > STALL_PREFILL) {
          slow = true; text += " — no first token yet; Stop to cancel";
        }
      } else {
        const gap = (performance.now() - tLast) / 1000;
        if (gap >= STALL_STREAM) {
          if (engTick++ % 4 === 0) pollEngine();   // every ~2s while quiet
          if (eng && eng.rate > 0.5) {
            text = "wire quiet " + gap.toFixed(0) + "s · engine generating " +
                   eng.rate.toFixed(0) + " tok/s — output is buffered upstream" +
                   " (usually a tool call being assembled)";
            slow = gap > 180;
          } else if (eng && eng.rate <= 0.5 && eng.running === 0) {
            slow = true;
            text = "no tokens for " + gap.toFixed(0) +
                   "s — nothing running on the engine. Stop and retry.";
          } else {
            slow = true;
            text = "no tokens for " + gap.toFixed(0) + "s — stream may have stalled";
          }
        } else {
          eng = null; engTick = 0;   // wire is live again; stale samples lie
          if (lastKind === "tool") {
            // args stream invisibly — narrate the work or it looks like a hang
            text = "writing a tool call" + (toolName ? " · " + toolName : "") +
                   " · " + toolChars.toLocaleString() + " chars of arguments";
          } else {
            text = "";   // visible tokens are their own feedback
          }
        }
      }
      if (text === shown) return;          // don't fight the rAF renderer
      shown = text;
      bot.status = text ? { text, slow } : null;
      renderMessages(true);
    }, 500);

    try {
    // Never request more output than the window has room for: vLLM rejects
    // prompt + max_tokens > ctx outright instead of trimming, so a big Max
    // tokens dial turns a long conversation into a 400. The window is the
    // manifest's figure until the engine corrects it (see capsFor); the
    // prompt is estimated — calibrated against the engine's own count of the
    // last prompt when there is one — and clamped per hop, since the prompt
    // grows every time a tool result lands.
    const mid = bot.model || state.model;
    let ctxWin = capsFor(mid).ctx || 131072;
    const est = estimatePrompt(msgs, body0.tools, mid);
    const room = ctxWin - est.tokens - est.margin;
    if (room < 256) {
      throw new Error("context is full: the prompt is ~" + est.tokens.toLocaleString() +
        " tokens of a " + ctxWin.toLocaleString() + "-token window. Type /compress to fold" +
        " older turns into a summary, or start a New chat.");
    }
    const body1 = Object.assign({}, body0, { messages: msgs });
    if (body1.max_tokens && body1.max_tokens > room) {
      body1.max_tokens = room;
      bot.notice = "Max tokens clamped to " + room.toLocaleString() + " for this hop — " +
        "the prompt already uses ~" + est.tokens.toLocaleString() + " of " +
        ctxWin.toLocaleString() + " context tokens.";
    }
    // Session, turn and purpose ride as headers so the server's trace log
    // can file this request without anything extra travelling upstream.
    const post = (b) => fetch("/api/chat", {
      method: "POST", headers: traceHeaders(bot), body: JSON.stringify(b), signal: ctl.signal,
    });
    let r = await post(body1);
    for (let attempt = 0; !r.ok; attempt++) {
      const err = upstreamText(await r.text()).slice(0, 800);
      const over = parseCtxError(err);
      if (over) {
        // Context overflow: the estimate lost to the real tokenizer, or the
        // manifest's window was wrong. The error names the engine's real
        // window — believe it, for the meter and every clamp from now on.
        if (over.win !== ctxWin) {
          state.ctxLearned[mid] = over.win;
          ctxWin = over.win;
          servingLine();
        }
        if (attempt >= 4) {
          throw new Error("still over the context window after 4 retries — type /compress" +
            " to fold older turns into a summary. Engine said: " + err.slice(0, 300));
        }
        // "at least N input tokens" is not the prompt's size. vLLM stops
        // tokenizing at window - max_tokens + 1 and reports THAT (renderers/
        // params.py, _token_len_check), so N is a floor and the real prompt
        // is N plus an unknown amount. Retrying at N + 64 is how this used to
        // fail by exactly one token, twice. Back off below the floor
        // geometrically instead: 0.5%, 2%, 8%, 32% of the window — each
        // refusal is cheap, the engine checks before it schedules. The older
        // phrasing gives an exact count and fits on the first go.
        // Capped at half of what the floor leaves, so a late retry cannot
        // talk itself into "full" while room remains.
        const left = over.win - over.prompt;
        const margin = over.bound
          ? Math.min(Math.ceil(over.win * 0.005 * Math.pow(4, attempt)), Math.floor(left / 2))
          : 64;
        const fit = left - margin;
        if (fit < 128) {
          throw new Error("context is full: the prompt is " + (over.bound ? "over " : "") +
            over.prompt.toLocaleString() + " tokens of a " + over.win.toLocaleString() +
            "-token window. Type /compress, or start a New chat.");
        }
        body1.max_tokens = Math.min(body1.max_tokens || fit, fit);
        bot.notice = "Max tokens clamped to " + fit.toLocaleString() + " — the prompt is " +
          (over.bound ? "over " : "") + over.prompt.toLocaleString() + " of " +
          over.win.toLocaleString() + " context tokens.";
        r = await post(body1);
        continue;
      }
      // A server started without --enable-auto-tool-choice rejects the whole
      // request. That is a served-with-the-wrong-flags problem, not a crash, so
      // say it in a sentence and answer without tools rather than dumping JSON.
      const noTools = body1.tools &&
        /enable-auto-tool-choice|tool-call-parser|tool choice/i.test(err);
      if (!noTools) throw new Error(err.slice(0, 400));
      delete body1.tools; delete body1.tool_choice;
      bot.notice = "This model is not served with tool support " +
        "(needs --enable-auto-tool-choice and --tool-call-parser). Answered without tools.";
      r = await post(body1);
    }
    phase = "stream";
    const tid = r.headers.get("X-BB-Trace");
    if (tid) bot.traces = (bot.traces || []).concat([tid]);
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
        if (obj.error) throw new Error(obj.error);
        if (obj.usage) usage = obj.usage;
        const c0 = obj.choices && obj.choices[0];
        if (c0 && c0.finish_reason) finishReason = c0.finish_reason;
        const delta = c0 && c0.delta;
        if (!delta) continue;
        // tool calls stream in fragments keyed by index
        for (const tc of (delta.tool_calls || [])) {
          const i = tc.index || 0;
          calls[i] = calls[i] || { id: "", name: "", args: "" };
          if (tc.id) calls[i].id = tc.id;
          if (tc.function && tc.function.name) calls[i].name += tc.function.name;
          if (tc.function && tc.function.arguments) calls[i].args += tc.function.arguments;
        }
        if (delta.tool_calls && delta.tool_calls.length) {
          // Argument fragments ARE tokens — a model writing a whole file into
          // a tool call streams here for minutes with zero visible content.
          // Without this the heartbeat reads that as a stall and says so.
          const now = performance.now();
          if (tFirst === null) tFirst = now;
          tLast = now;
          lastKind = "tool";
          toolChars = calls.reduce((n, c) => n + (c ? c.args.length : 0), 0);
          toolName = (calls[calls.length - 1] || {}).name || toolName;
        }
        const got = (delta.content || "") + (delta.reasoning_content || "") + (delta.reasoning || "");
        if (got) {
          const now = performance.now();
          if (tFirst === null) tFirst = now;
          tLast = now; chunks++; lastKind = "text";
          if (delta.reasoning_content) bot.reasoning += delta.reasoning_content;
          if (delta.reasoning) bot.reasoning += delta.reasoning;
          if (delta.content) bot.content += delta.content;
          queue();
        }
      }
    }
    // A call whose arguments don't parse is a stream that died mid-call (or a
    // model that hit the token ceiling mid-JSON). Executing it is wrong and
    // recording it poisons the session — the server json-parses arguments
    // while rendering the prompt, so one unterminated string 400s every
    // subsequent request. Quarantine instead of trusting.
    const okCalls = [], brokenCalls = [];
    for (const c of calls.filter(Boolean)) {
      try { JSON.parse(c.args || "{}"); okCalls.push(c); }
      catch (err) { brokenCalls.push(c); }
    }
    // The engine counted this prompt; that count calibrates the next
    // estimate (see estimatePrompt).
    if (usage && usage.prompt_tokens > 0) {
      state.calib = { model: bot.model || state.model, tokens: usage.prompt_tokens,
                      chars: JSON.stringify({ m: msgs, t: body0.tools || null }).length };
    }
    return { calls: okCalls, brokenCalls, usage, finishReason, chunks,
             ttft: tFirst ? (tFirst - t0) / 1000 : null,
             span: (tFirst && tLast && tLast > tFirst) ? (tLast - tFirst) / 1000 : null };
    } finally {
      // must clear on every exit — success, upstream error, or user Stop —
      // or a dead heartbeat keeps repainting a finished message
      clearInterval(beat);
      bot.status = null;
    }
  }

  // The transcript, rendered the way the upstream wants it: system prompt
  // first, prior thinking stripped or carried per the manifest, broken tool
  // calls scrubbed. One builder, because compression sends history too.
  // Attached skills become part of the system prompt, ahead of the user's own
  // system text. The trace log captures the whole prompt, so a skilled turn is
  // recorded exactly as the model saw it.
  function effectiveSystem() {
    const parts = [];
    for (const name of state.activeSkills) {
      const body = state.skillBodies[name];
      if (body) parts.push("# Skill: " + name + "\n\n" + body);
    }
    if (P.sys.trim()) parts.push(P.sys.trim());
    return parts.join("\n\n---\n\n");
  }

  function buildMsgs(list, caps, sys) {
    const msgs = [];
    const system = sys !== undefined ? sys : effectiveSystem();
    if (system) msgs.push({ role: "system", content: system });
    for (const m of list) {
      if (m.role === "bot") {
        if (m.kind === "compress") continue;   // a summary in flight, or one that failed
        // A tool turn is several hops: text + calls, results, text + calls,
        // results ... answer. Every hop is replayed, because a model that is
        // shown only its last hop re-does the other seven. Sessions saved
        // before hops were recorded carry one flattened hop instead.
        const hops = m.hops || (m.tool_calls
          ? [{ content: "", reasoning: m.reasoning || "", tool_calls: m.tool_calls, results: m.toolResults || [] }]
          : []);
        for (const h of hops) {
          // Scrub broken tool calls on EVERY rebuild, not just at record time:
          // a truncated call that slipped into a saved session would otherwise
          // 400 every request forever — the server json-parses arguments while
          // rendering the prompt, and history is resent whole each turn.
          const okCalls = (h.tool_calls || []).filter((tc) => {
            try { JSON.parse((tc.function && tc.function.arguments) || "{}"); return true; }
            catch (err) { return false; }
          });
          const text = caps.strip_reasoning ? stripThink(h.content || "") : (h.content || "");
          if (!okCalls.length) {
            if (text) msgs.push({ role: "assistant", content: text });
            continue;
          }
          const e = { role: "assistant", content: text, tool_calls: okCalls };
          // Most reasoning models want prior thinking dropped. DeepSeek-V4 is
          // the exception once tools are in play: it 400s when
          // reasoning_content is missing from a tool exchange — so the thinking
          // that produced each call rides with it, and only there.
          if (!caps.strip_reasoning && h.reasoning) e.reasoning_content = h.reasoning;
          msgs.push(e);
          const okIds = new Set(okCalls.map((tc) => tc.id));
          for (const t of (h.results || [])) {
            if (okIds.has(t.id))
              msgs.push({ role: "tool", tool_call_id: t.id, content: t.content });
          }
        }
        const answer = caps.strip_reasoning ? stripThink(m.content) : m.content;
        if (answer) msgs.push({ role: "assistant", content: answer });
      } else {
        msgs.push({ role: "user", content: userContent(m) });
      }
    }
    return msgs;
  }

  // Attachments go to the model three ways: images as image parts (the
  // vision engine reads them), text-like files inline, everything else as
  // the path it was saved under — the filesystem and terminal tools can
  // open it from there.
  function userContent(m) {
    const atts = m.attachments || [];
    if (!atts.length) return m.content;
    const parts = [];
    let text = m.content || "";
    for (const a of atts) {
      if (a.kind === "image") {
        if (a.dataURL) parts.push({ type: "image_url", image_url: { url: a.dataURL } });
        else text += "\n\n[image attached: " + a.name + " — not available in this session; re-attach to show it to the model]";
      } else if (a.kind === "text" && a.text != null) {
        text += "\n\n--- attached file: " + a.name + (a.path ? " (saved at " + a.path + ")" : "") + " ---\n" + a.text + "\n--- end of " + a.name + " ---";
      } else {
        text += "\n\n[attached file: " + a.name + (a.size ? ", " + fmtBytes(a.size) : "") + (a.path ? ", saved at " + a.path + " — read it with the filesystem or terminal tools" : "") + "]";
      }
    }
    parts.unshift({ type: "text", text: text.trim() || "(see attachments)" });
    return parts;
  }
  function fmtBytes(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? Math.round(n / 1024) + " KB" : n + " B"; }
  function oneLine(sv, n) { const t = String(sv || "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n - 1) + "\u2026" : t; }
  function prettyJson(sv) { try { return JSON.stringify(JSON.parse(sv), null, 2); } catch (e) { return String(sv || ""); } }

  // Images are kept in sessions only as their saved URL; before a send they
  // are read back into base64 for the request.
  async function hydrateImages(list) {
    for (const m of list) {
      for (const a of (m.attachments || [])) {
        if (a.kind === "image" && !a.dataURL && a.url) {
          try {
            const blob = await (await fetch(a.url)).blob();
            a.dataURL = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(blob); });
          } catch (e) {}
        }
      }
    }
  }

  function traceHeaders(bot) {
    const h = { "Content-Type": "application/json" };
    if (state.session) h["X-BB-Session"] = state.session;
    if (bot && bot.turn) h["X-BB-Turn"] = bot.turn;
    if (bot && bot.kind === "compress") h["X-BB-Purpose"] = "compress";
    return h;
  }

  function toolDefs(caps) {
    return (caps.tools && state.toolsOn && state.tools.length)
      ? state.tools.map((t) => t.def) : null;
  }

  // Where the prompt has to stay for the chat to keep going: room under the
  // window for the Max tokens dial, and never past 80% of it regardless.
  function compressLimit(win) {
    return Math.max(Math.floor(win * 0.3),
                    Math.min(Math.floor(win * 0.8), win - P.maxTok - 4096));
  }

  async function send(text) {
    const atts = (state.attachments || []).slice();
    if ((!text || !text.trim()) && !atts.length) return;
    if (state.streaming || !state.model) return;
    text = (text || "").trim();
    if (atts.some((a) => a.kind === "image") && !capsFor(state.model).vision) {
      if (!confirm("The selected model is not marked as accepting images. Send anyway?")) return;
    }
    await hydrateImages(state.messages);
    if (/^\/compress\b/i.test(text)) { await compress("manual"); return; }
    if (P.autoCompress) {
      // Fold older turns away BEFORE this one joins the transcript, so what
      // goes out fits with room to answer. A loop, because one round only
      // summarizes as much as fits in the summarizer's own window.
      for (let round = 0; round < 3; round++) {
        const c0 = capsFor(state.model);
        const probe = buildMsgs(state.messages.concat([{ role: "user", content: text, attachments: atts }]), c0);
        const est = estimatePrompt(probe, toolDefs(c0), state.model);
        if (est.tokens <= compressLimit(c0.ctx || 131072)) break;
        const did = await compress("auto", est.tokens);
        if (did === "aborted") { $("input").value = text; return; }
        if (!did) break;
      }
    }
    if (!state.session) state.session = "s-" + Date.now().toString(36);
    const user = { role: "user", content: text, attachments: atts };
    state.attachments = []; renderAttachChips();
    autosizeInput();
    // model + effort stamped now: the transcript renders provenance, not
    // whatever the controls happen to say later
    const bot = { role: "bot", content: "", reasoning: "", meta: "",
                  model: state.model, effort: P.effort, turn: newTurnId(),
                  skills: state.activeSkills.slice(),
                  thinkOpen: true };   // watch it stream; collapsed on completion
    state.messages.push(user, bot);
    state.streaming = true;
    $("send-btn").classList.add("stop");
    renderMessages();

    const caps = capsFor(state.model);
    const msgs = buildMsgs(state.messages.slice(0, -1), caps);

    const body = {
      model: state.model,
      temperature: P.temp, top_p: P.topP, max_tokens: P.maxTok,
      top_k: P.topK, repetition_penalty: P.rep,
    };
    if (P.seed) body.seed = P.seed;
    if (P.json) body.response_format = { type: "json_object" };
    if (P.stops.trim()) body.stop = P.stops.split(",").map((s) => s.trim()).filter(Boolean);
    // Only send an effort the model actually accepts. DeepSeek's encoder
    // asserts on the value, so "low" is a 500, not a no-op.
    const eff = EFFORT[P.effort];
    if (P.effort !== 4 && (caps.effort || []).includes(eff)) body.reasoning_effort = eff;
    // Thinking is off by default on vLLM's DeepSeek-V4 path and must be asked
    // for; other models ignore an empty object.
    if (caps.ctk && Object.keys(caps.ctk).length) body.chat_template_kwargs = caps.ctk;
    const tools = toolDefs(caps);
    if (tools) { body.tools = tools; body.tool_choice = "auto"; }

    let usage = null, finishReason = null, ttft = null, span = null, chunks = 0;
    let totToks = 0, totRToks = 0;   // summed across hops — last-hop usage alone lies
    // The Stop button is the real brake. The hop ceiling (panel: Tool hops)
    // is for a runaway model nobody is watching — and a model that repeats
    // the identical call is stopped well before it.
    const maxHops = Math.max(1, P.maxHops | 0);
    let lastSig = "", repeats = 0;
    try {
      for (let hop = 0; hop < maxHops; hop++) {
        const rLen = bot.reasoning.length;
        const r = await streamTurn(msgs, bot, body);
        usage = r.usage || usage;
        if (r.usage) {
          totToks += r.usage.completion_tokens || 0;
          totRToks += (r.usage.completion_tokens_details || {}).reasoning_tokens || 0;
        }
        finishReason = r.finishReason;
        chunks += r.chunks;
        if (ttft === null) ttft = r.ttft;
        if (r.span) span = (span || 0) + r.span;
        if (r.brokenCalls && r.brokenCalls.length) {
          const b = r.brokenCalls[0];
          bot.notice = "Dropped a truncated tool call — " + (b.name || "unnamed") + " arrived with " +
            (b.args || "").length.toLocaleString() + " chars of arguments and no closing brace. " +
            (r.finishReason === "length"
              ? "It hit the Max tokens ceiling: raise Max tokens and ask again."
              : "The stream was cut mid-call: just ask again.");
        }
        if (!r.calls.length) break;

        const sig = r.calls.map((c) => c.name + ":" + (c.args || "")).join("\n");
        repeats = sig === lastSig ? repeats + 1 : 0;
        lastSig = sig;

        // Record the hop on the message — text, calls, then results as they
        // land. Later turns and the archive rebuild the exchange from here.
        const hopRec = {
          content: bot.content || "",
          tool_calls: r.calls.map((c) => ({
            id: c.id, type: "function",
            function: { name: c.name, arguments: c.args || "{}" },
          })),
          results: [],
        };
        // DeepSeek's rule: the thinking that produced a tool call must ride
        // along on the next hop of THIS exchange. Most models must never see
        // prior thinking again — the manifest decides.
        if (!capsFor(state.model).strip_reasoning && bot.reasoning.length > rLen)
          hopRec.reasoning = bot.reasoning.slice(rLen);
        bot.hops = (bot.hops || []).concat([hopRec]);
        const hopMsg = { role: "assistant", content: hopRec.content, tool_calls: hopRec.tool_calls };
        if (hopRec.reasoning) hopMsg.reasoning_content = hopRec.reasoning;
        msgs.push(hopMsg);
        for (const c of r.calls) {
          let args = {};
          try { args = JSON.parse(c.args || "{}"); } catch (e) {}
          bot.toolUse = (bot.toolUse || []).concat([{ name: c.name, args: c.args || "{}", result: "running…", error: false }]);
          renderMessages(true);
          let out = { content: "tool call failed", isError: true };
          try {
            out = await (await fetch("/api/tool-call", {
              method: "POST", headers: traceHeaders(bot),
              body: JSON.stringify({ name: c.name, arguments: args }),
            })).json();
          } catch (e) { out = { content: "console could not reach the tool: " + e.message, isError: true }; }
          // the model gets 20k of it and the screen shows 20k: storing all of it
          // made one session 45 MB, re-sent and rewritten after every turn
          const full = String(out.content);
          bot.toolUse[bot.toolUse.length - 1].result = full.length > 65536
            ? full.slice(0, 65536) + "\n\u2026[stored copy truncated: " + full.length + " characters in total]" : full;
          bot.toolUse[bot.toolUse.length - 1].error = !!out.isError;
          const text = String(out.content).slice(0, 20000);
          hopRec.results.push({ id: c.id, content: text });
          msgs.push({ role: "tool", tool_call_id: c.id, content: text });
          renderMessages(true);
        }
        bot.content = "";   // the next hop writes the real answer
        if (repeats >= 2) {
          bot.notice = "Stopped: the model made the identical tool call three times in a row (" +
            r.calls[0].name + "). Tell it what to do differently, or send \"continue\".";
          break;
        }
        if (hop === maxHops - 1) {
          bot.notice = "Paused after " + maxHops + " tool hops. Send \"continue\" to keep going," +
            " or raise Tool hops in the panel.";
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") bot.error = "upstream error: " + e.message;
    }

    const exact = totToks > 0;
    const toks = exact ? totToks : chunks;
    const approx = exact ? "" : "~";
    const decode = (span && toks > 1) ? (toks - 1) / span : null;
    const rtok = totRToks || (usage && usage.completion_tokens_details &&
                 usage.completion_tokens_details.reasoning_tokens);
    bot.meta = [
      bot.model,
      approx + toks + " tok" + (rtok ? " (" + rtok + " thinking)" : ""),
      decode ? approx + decode.toFixed(1) + " tok/s" : null,
      ttft !== null ? "ttft " + Math.round(ttft * 1000) + " ms" : null,
      (bot.toolUse || []).length ? bot.toolUse.length + " tool call" + (bot.toolUse.length > 1 ? "s" : "") : null,
      (bot.skills || []).length ? bot.skills.length + " skill" + (bot.skills.length > 1 ? "s" : "") + ": " + bot.skills.join(", ") : null,
      finishReason === "length"
        ? "\u26a0 stopped at Max tokens \u2014 thinking shares the budget; raise it in the panel"
        : null,
    ].filter(Boolean).join("  \u00b7  ");
    state.streaming = false;
    state.abort = null;
    // thinking is scaffolding: fold it away once the answer exists, and give
    // the reader back the toggle from here on
    if (bot.reasoning) bot.thinkOpen = false;
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

  /* ---------------- compression ---------------- */
  // The model writes a dense summary of the older part of the conversation,
  // the originals go to a file on the server (data/archive/), and the
  // summary takes their place in the window. The newest exchanges stay
  // verbatim. Typed as /compress, or run by send() when the prompt nears
  // the limit — which is what lets a chat go on for as long as you want.
  const KEEP_RECENT = 2;        // user turns kept verbatim
  const SUMMARY_TOKENS = 8192;  // output budget for the summary itself
  const COMPRESS_SYS =
    "You are compressing a conversation so that it can continue in a smaller context window. " +
    "Write a dense, factual summary a colleague could continue from without the original. " +
    "Keep: the user's goals and constraints; decisions made and why; facts, numbers, file paths, " +
    "commands, identifiers, URLs and error messages, quoted exactly when they will be needed again; " +
    "results of tool calls that still matter; what was tried and failed; open questions and next " +
    "steps; standing instructions about tone or format. Drop pleasantries and superseded drafts. " +
    "Plain text with short headed sections. No preamble, no commentary, no offer to help.";
  const COMPRESS_ASK =
    "Compress everything above into that summary now. Detail over brevity — up to about 3,000 words.";

  // the message boundary at or before `j` that starts a user turn, so the
  // kept tail begins with the user speaking
  function cutAtUser(list, j) {
    for (let i = Math.min(j, list.length - 1); i > 0; i--) if (list[i].role === "user") return i;
    return j;
  }

  async function compress(why, estTokens) {
    if (state.streaming || !state.model) return false;
    const caps = capsFor(state.model);
    const win = caps.ctx || 131072;
    const list = state.messages.filter((m) => m.kind !== "compress");
    // keep the last KEEP_RECENT user turns; everything before them is "old"
    let cut = list.length, seen = 0;
    for (let i = list.length - 1; i >= 0; i--) {
      if (list[i].role === "user" && ++seen === KEEP_RECENT) { cut = i; break; }
    }
    let old = list.slice(0, cut);
    if (!old.length) {
      const last = state.messages[state.messages.length - 1];
      if (last) { last.notice = "Nothing older than the last " + KEEP_RECENT + " turns to compress."; renderMessages(); }
      return false;
    }
    // the summarizer's own request must fit: shrink the slice until it does
    let convo;
    for (;;) {
      convo = buildMsgs(old, caps, COMPRESS_SYS);
      const e = estimatePrompt(convo, null, state.model);
      if (e.tokens + e.margin + SUMMARY_TOKENS <= win) break;
      if (old.length <= 1) {
        const last = state.messages[state.messages.length - 1];
        if (last) { last.error = "cannot compress: even one message is too big for the summarizer's window"; renderMessages(); }
        return false;
      }
      old = list.slice(0, cutAtUser(list, Math.ceil(old.length / 2)));
    }
    // the instruction is a user turn; merge if the slice already ends on one
    const lastC = convo[convo.length - 1];
    if (lastC && lastC.role === "user") convo[convo.length - 1] = { role: "user", content: lastC.content + "\n\n" + COMPRESS_ASK };
    else convo.push({ role: "user", content: COMPRESS_ASK });

    if (!state.session) state.session = "s-" + Date.now().toString(36);
    const tmp = { role: "bot", kind: "compress", content: "", reasoning: "", meta: "",
                  model: state.model, effort: P.effort, thinkOpen: false, turn: newTurnId(),
                  notice: (why === "auto" && estTokens
                            ? "Context is at ~" + estTokens.toLocaleString() + " of " + win.toLocaleString() + " tokens: "
                            : "") + "compressing " + old.length + " older messages into a summary…" };
    state.messages.push(tmp);
    state.streaming = true;
    $("send-btn").classList.add("stop");
    renderMessages();

    const body = { model: state.model, temperature: 0.2, top_p: 0.9, max_tokens: SUMMARY_TOKENS };
    if (caps.ctk && Object.keys(caps.ctk).length) body.chat_template_kwargs = caps.ctk;
    // transcription, not reasoning: the lowest effort the model offers
    for (const e of ["low", "minimal", "none"]) {
      if ((caps.effort || []).includes(e)) { body.reasoning_effort = e; break; }
    }
    let result = false;
    try {
      await streamTurn(convo, tmp, body);
      const summary = stripThink(tmp.content).trim();
      if (!summary) throw new Error("the model returned an empty summary");
      // file the originals away first, then swap the summary in
      if (!state.session) state.session = "s-" + Date.now().toString(36);
      let filed = {};
      try {
        filed = await (await fetch("/api/archive", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session: state.session, model: state.model, summary,
                                 messages: old.map(serializeMsg) }),
        })).json();
      } catch (e) { filed = {}; }
      const marker = {
        role: "user", kind: "summary", count: old.length, archive: filed.file || "",
        content: "[Earlier conversation compressed. What follows is a summary of " + old.length +
                 " messages" + (filed.file ? ", archived as " + filed.file : "") + ".]\n\n" + summary,
      };
      const ack = { role: "bot", kind: "summary-ack", model: state.model,
                    content: "Understood. I have the summary of the earlier conversation and will continue from it." };
      state.messages = [marker, ack].concat(list.slice(old.length));
      state.ctxUsed = null;   // the meter's last reading described the old prompt
      result = true;
    } catch (e) {
      if (e.name === "AbortError") {
        state.messages = state.messages.filter((m) => m !== tmp);
        result = "aborted";
      } else {
        tmp.notice = null;
        tmp.error = "compression failed: " + e.message;
      }
    } finally {
      tmp.status = null;
      state.streaming = false;
      state.abort = null;
      $("send-btn").classList.remove("stop");
    }
    servingLine();
    renderMessages();
    saveSession();
    return result;
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
  // the box grows with what you type, up to the CSS max-height, then scrolls
  function autosizeInput() {
    const el = $("input"); el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 260) + "px";
  }
  $("input").addEventListener("input", autosizeInput);
  autosizeInput();

  /* ---------------- attachments ---------------- */
  state.attachments = [];
  const TEXT_EXT = /\.(txt|md|markdown|json|jsonl|csv|tsv|yaml|yml|toml|ini|cfg|conf|log|py|js|ts|tsx|jsx|html|css|sh|bash|zsh|sql|xml|env|rs|go|java|c|h|cpp|hpp|rb|php|swift|kt|m|r|tex|diff|patch)$/i;
  function kindOf(file) {
    if ((file.type || "").startsWith("image/")) return "image";
    if ((file.type || "").startsWith("text/") || TEXT_EXT.test(file.name) || file.type === "application/json") return "text";
    return "file";
  }
  // images are resized before upload: a phone photo is 12 MB of pixels the
  // model does not need, and every later turn resends it
  function shrinkImage(file) {
    return new Promise((resolve) => {
      const img = new Image(); const url = URL.createObjectURL(file);
      img.onload = () => {
        const max = 1568; let w = img.width, h = img.height;
        if (Math.max(w, h) > max) { const k = max / Math.max(w, h); w = Math.round(w * k); h = Math.round(h * k); }
        const c = document.createElement("canvas"); c.width = w; c.height = h;
        c.getContext("2d").drawImage(img, 0, 0, w, h);
        URL.revokeObjectURL(url);
        const keepPng = file.type === "image/png" && file.size < 600000;
        resolve(c.toDataURL(keepPng ? "image/png" : "image/jpeg", 0.85));
      };
      img.onerror = () => { URL.revokeObjectURL(url); const r = new FileReader(); r.onload = () => resolve(r.result); r.readAsDataURL(file); };
      img.src = url;
    });
  }
  async function uploadToConsole(name, dataURLorText, isText) {
    const data = isText ? btoa(unescape(encodeURIComponent(dataURLorText))) : dataURLorText.split(",", 2)[1];
    const r = await fetch("/api/upload", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: state.session || (state.session = "s-" + Date.now().toString(36)), name, data }) });
    const d = await r.json(); if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  async function addFiles(files) {
    for (const file of files) {
      if (file.size > 25 * 1024 * 1024) { alert(file.name + " is larger than 25 MB"); continue; }
      const a = { kind: kindOf(file), name: file.name || ("pasted-" + Date.now() + ".png"), size: file.size, mime: file.type, status: "reading" };
      state.attachments.push(a); renderAttachChips();
      try {
        if (a.kind === "image") {
          a.dataURL = await shrinkImage(file);
          const up = await uploadToConsole(a.name, a.dataURL, false);
          a.url = up.url; a.path = up.path; a.size = up.size;
        } else if (a.kind === "text") {
          a.text = await file.text();
          if (a.text.length > 200000) { a.text = a.text.slice(0, 200000) + "\n\u2026[truncated at 200k chars]"; }
          const up = await uploadToConsole(a.name, await file.text(), true);
          a.url = up.url; a.path = up.path;
        } else {
          const dataURL = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(file); });
          const up = await uploadToConsole(a.name, dataURL, false);
          a.url = up.url; a.path = up.path;
        }
        a.status = "ready";
      } catch (e) { a.status = "failed: " + e.message; }
      renderAttachChips();
    }
  }
  function renderAttachChips() {
    const box = $("attach-chips"); box.textContent = "";
    box.hidden = !state.attachments.length;
    state.attachments.forEach((a, i) => {
      const c = document.createElement("div"); c.className = "attach-chip";
      if (a.kind === "image" && a.dataURL) { const img = document.createElement("img"); img.src = a.dataURL; c.appendChild(img); }
      else { const k = document.createElement("span"); k.className = "k"; k.textContent = a.kind === "text" ? "text" : "file"; c.appendChild(k); }
      const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = a.name; nm.title = a.name; c.appendChild(nm);
      const sz = document.createElement("span"); sz.className = "sz"; sz.textContent = a.status === "ready" ? fmtBytes(a.size || 0) : a.status; c.appendChild(sz);
      const x = document.createElement("button"); x.type = "button"; x.textContent = "\u2715"; x.title = "remove"; x.onclick = () => { state.attachments.splice(i, 1); renderAttachChips(); }; c.appendChild(x);
      box.appendChild(c);
    });
  }
  $("attach-btn").onclick = () => $("attach-input").click();
  $("attach-input").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; };
  $("input").addEventListener("paste", (e) => {
    const files = [...(e.clipboardData && e.clipboardData.files ? e.clipboardData.files : [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  (function () {
    const box = $("composer-box"), tr = $("transcript");
    for (const el of [box, tr]) {
      el.addEventListener("dragover", (e) => { e.preventDefault(); box.classList.add("drop"); });
      el.addEventListener("dragleave", () => box.classList.remove("drop"));
      el.addEventListener("drop", (e) => { e.preventDefault(); box.classList.remove("drop"); if (e.dataTransfer && e.dataTransfer.files.length) addFiles([...e.dataTransfer.files]); });
    }
  })();

  /* ---------------- chat tabs: Activity and Files ---------------- */
  state.chatView = "chat";
  function chatActivity() {
    const rows = [];
    state.messages.forEach((m, i) => { if (m.role === "bot") for (const t of (m.toolUse || [])) rows.push({ turn: i, model: m.model, t }); });
    return rows;
  }
  function chatFiles() {
    const out = [];
    state.messages.forEach((m) => {
      for (const a of (m.attachments || [])) out.push({ kind: a.kind, name: a.name, path: a.path, url: a.url, size: a.size, from: "attached", img: a.kind === "image" ? (a.url || a.dataURL) : null });
      for (const t of (m.toolUse || [])) {
        let args = {}; try { args = JSON.parse(t.args || "{}"); } catch (e) {}
        const p = args.path || args.file_path || args.filename || args.destination || args.target;
        if (p && /write|edit|create|save|move|copy|mkdir|append/i.test(t.name) && !t.error) out.push({ kind: "written", name: String(p).split("/").pop(), path: String(p), from: t.name.replace(/^[^_]+__/, "") });
      }
    });
    return out;
  }
  function updateChatTabs() {
    const a = chatActivity().length, f = chatFiles().length;
    $("tab-activity-n").textContent = a ? String(a) : ""; $("tab-files-n").textContent = f ? String(f) : "";
    document.querySelectorAll("#chat-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === state.chatView));
    $("activity-view").hidden = state.chatView !== "activity";
    $("files-view").hidden = state.chatView !== "files";
  }
  function renderActivityView() {
    const box = $("activity-view"); box.textContent = "";
    const rows = chatActivity();
    if (!rows.length) { box.innerHTML = '<div class="hint">No tool calls in this chat yet. Every call the model makes — arguments and results — lands here.</div>'; return; }
    rows.forEach((r, k) => {
      const d = document.createElement("div"); d.className = "act-row";
      d.innerHTML = '<div class="h"><span class="t"></span><b></b><span class="st"></span><span class="mono" style="color:var(--muted);font-size:11.5px;flex:1;min-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span></div>';
      d.querySelector(".t").textContent = "#" + (k + 1) + " \u00b7 turn " + Math.floor(r.turn / 2 + 1);
      d.querySelector("b").textContent = r.t.name;
      const st = d.querySelector(".st"); st.textContent = r.t.error ? "error" : (r.t.result === "running\u2026" ? "running" : "ok"); if (r.t.error) st.classList.add("err");
      d.querySelector(".mono").textContent = oneLine(r.t.args, 160);
      const det = document.createElement("details"); const sum = document.createElement("summary"); sum.textContent = "arguments and result"; det.appendChild(sum);
      const pre = document.createElement("pre"); pre.textContent = "arguments\n" + prettyJson(r.t.args) + "\n\nresult\n" + String(r.t.result).slice(0, 20000); det.appendChild(pre);
      d.appendChild(det); box.appendChild(d);
    });
  }
  function renderFilesView() {
    const box = $("files-view"); box.textContent = "";
    const files = chatFiles();
    if (!files.length) { box.innerHTML = '<div class="hint">Nothing yet. Files you attach and files the tools write show up here.</div>'; return; }
    for (const f of files) {
      const d = document.createElement("div"); d.className = "file-row";
      if (f.img) { const img = document.createElement("img"); img.src = f.img; d.appendChild(img); }
      else { const k = document.createElement("span"); k.className = "k"; k.textContent = f.kind === "written" ? "written" : f.kind; d.appendChild(k); }
      const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = f.name; d.appendChild(nm);
      const pth = document.createElement("span"); pth.className = "pth"; pth.textContent = (f.path || "") + (f.size ? "  \u00b7  " + fmtBytes(f.size) : "") + (f.from && f.from !== "attached" ? "  \u00b7  by " + f.from : ""); pth.title = f.path || ""; d.appendChild(pth);
      if (f.url) { const a = document.createElement("a"); a.href = f.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = "open"; d.appendChild(a); }
      box.appendChild(d);
    }
  }
  document.querySelectorAll("#chat-tabs button").forEach((b) => (b.onclick = () => { state.chatView = b.dataset.view; renderMessages(); }));
  document.querySelectorAll("[data-chip]").forEach((b) => (b.onclick = () => send(b.dataset.chip)));

  /* ---------------- sessions ---------------- */
  // Without this, state.session is set once and never cleared, so every later
  // conversation appends to the first one forever. A "new chat" is simply:
  // drop the transcript and forget the id, so the next save mints a fresh one.
  function newChat() {
    if (state.streaming && state.abort) state.abort.abort();
    state.messages = [];
    state.session = null;
    state.ctxUsed = null;
    state.calib = null;
    state.attachments = []; renderAttachChips();
    state.chatView = "chat";
    go("playground");
    // a new chat keeps whatever skills are attached — they are a working set,
    // not a per-conversation choice, and clearing them surprises people
    servingLine();
    renderMessages();
    const el = $("input");
    if (el) el.focus();
  }

  function saveSession() {
    if (!state.messages.length) return;
    if (!state.session) state.session = "s-" + Date.now().toString(36);
    const first = state.messages.find((m) => m.role === "user" && ((m.content || "").trim() || (m.attachments || []).length));
    const toks = state.messages.reduce((a, m) => a + (m.content || "").length, 0);
    fetch("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: state.session,
        title: first ? ((first.content || "").trim() || ("\ud83d\udcc4 " + (first.attachments || []).map((a) => a.name).join(", "))).slice(0, 80) : "untitled",
        model: state.model,
        turns: state.messages.length,
        chars: toks,
        activeSkills: state.activeSkills,
        updated: Date.now(),
        messages: state.messages.map(serializeMsg),
      }),
    }).catch(() => {});
  }

  function newTurnId() {
    return "t-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  }

  function serializeMsg(m) {
    const o = { role: m.role, content: m.content, reasoning: m.reasoning || "",
                meta: m.meta || "", model: m.model || "", effort: m.effort,
                error: m.error || "", toolUse: m.toolUse || [],
                tool_calls: m.tool_calls || null, toolResults: m.toolResults || [],
                hops: m.hops || null };
    if (m.kind) { o.kind = m.kind; o.archive = m.archive || ""; o.count = m.count || 0; }
    if (m.turn) o.turn = m.turn;
    if (m.traces) o.traces = m.traces;
    if (m.rating) o.rating = m.rating;
    if (m.skills && m.skills.length) o.skills = m.skills;
    if (m.attachments && m.attachments.length) {
      o.attachments = m.attachments.map((a) => ({ kind: a.kind, name: a.name, size: a.size, mime: a.mime, url: a.url, path: a.path,
                                                 text: a.kind === "text" ? (a.text || "").slice(0, 120000) : undefined }));
    }
    return o;
  }

  function relTime(ms) {
    if (!ms) return "";
    const d = (Date.now() - ms) / 1000;
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + " min ago";
    if (d < 86400) return Math.floor(d / 3600) + " h ago";
    if (d < 604800) return Math.floor(d / 86400) + " d ago";
    return new Date(ms).toLocaleDateString();
  }

  async function renderSessions() {
    const box = $("sessions-box");
    let list = [];
    try { list = await (await fetch("/api/sessions")).json(); } catch (e) {}
    fetch("/api/traces").then((r) => r.json()).then((t) => {
      const mb = (t.bytes || 0) / 1e6;
      $("traces-sub").textContent = t.days
        ? "trace log: " + t.days + " day" + (t.days > 1 ? "s" : "") + " · " +
          (mb >= 1 ? mb.toFixed(1) + " MB" : Math.round(mb * 1000) + " kB") +
          " · " + (t.today_events || 0) + " events today"
        : "trace log: empty";
    }).catch(() => {});
    if (!list.length) {
      box.innerHTML = '<div class="empty-state"><b>No sessions yet</b><span>Conversations save here automatically, on this host only. Start one from the Playground.</span></div>';
      return;
    }
    box.textContent = "";
    const t = document.createElement("div");
    t.className = "table";
    t.innerHTML = '<div class="trow head"><span>Session</span><span>Model</span><span>Turns</span><span>Chars</span><span>Last active</span></div>';
    for (const s of list) {
      const r = document.createElement("div");
      r.className = "trow" + (s.id === state.session ? " active" : "");
      r.innerHTML = '<span class="t"></span><span class="m mono"></span><span class="m mono"></span><span class="m mono"></span>' +
                    '<span class="w"><span class="when"></span><button class="del" title="Delete">\u2715</button></span>';
      r.children[0].textContent = s.title || "untitled";
      r.children[1].textContent = s.model || "";
      r.children[2].textContent = s.turns || 0;
      r.children[3].textContent = (s.chars || 0).toLocaleString();
      r.querySelector(".when").textContent = relTime(s.updated || 0);
      r.querySelector(".del").onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm("Delete this session?")) return;
        await fetch("/api/sessions?id=" + encodeURIComponent(s.id), { method: "DELETE" })
          .catch(() => {});
        if (s.id === state.session) newChat();
        renderSessions();
      };
      r.onclick = async () => {
        // the list is summaries; the messages come with the one session
        let full = null;
        try {
          const res = await fetch("/api/sessions?id=" + encodeURIComponent(s.id));
          if (res.ok) full = await res.json();
        } catch (e) {}
        if (!full) { alert("This session could not be opened: the server did not return it."); renderSessions(); return; }
        state.session = s.id;
        state.messages = (full.messages || []).map((m) => ({ ...m }));
        state.ctxUsed = null;
        state.calib = null;
        setActiveSkills(full.activeSkills || []);
        if (full.model && state.models.includes(full.model)) {
          state.model = full.model;
          $("model-select").value = full.model;
          servingLine();
        }
        go("playground");
        renderMessages();
      };
      t.appendChild(r);
    }
    box.appendChild(t);
  }

  /* ---------------- live events ---------------- */
  // One stream per tab. The server says what changed (jobs, sessions, runs)
  // and each screen refreshes only its own list. EventSource reconnects by
  // itself and resumes after the last event it saw (Last-Event-ID); "reset"
  // means it fell too far behind and everything should reload.
  const live = { handlers: {}, es: null };
  function onLive(topic, fn) { (live.handlers[topic] = live.handlers[topic] || []).push(fn); }
  function startLive() {
    if (live.es || typeof EventSource === "undefined") return;
    const topics = ["jobs", "sessions", "runs"];
    const es = new EventSource("/api/events?topics=" + topics.join(","));
    live.es = es;
    const fire = (topic, evt) => { for (const fn of live.handlers[topic] || []) { try { fn(evt); } catch (e) { console.error(e); } } };
    for (const t of topics) {
      es.addEventListener(t, (e) => { let evt; try { evt = JSON.parse(e.data); } catch (x) { return; } fire(t, evt); });
    }
    es.addEventListener("reset", () => { for (const t of topics) fire(t, { type: "reset" }); });
  }
  onLive("jobs", () => { if (state.screen === "jobs") renderJobs(true); });
  onLive("sessions", () => { if (state.screen === "sessions") renderSessions(); });
  onLive("runs", (e) => {
    // a goal started elsewhere (another tab, a job, bb) shows up here
    if (state.screen === "agents" && !state.streaming && e.type === "started" && (e.data || {}).kind === "agents") attachAgentRun();
  });

  /* ---------------- models ---------------- */
  async function loadModels() {
    let data = { data: [] };
    try { data = await (await fetch("/api/models")).json(); } catch (e) {}
    state.models = (data.data || []).map((m) => m.id);
    state.modelGateway = {}; for (const m of (data.data || [])) state.modelGateway[m.id] = m.gateway || "";
    state.modelInfo = {}; for (const m of (data.data || [])) state.modelInfo[m.id] = m;
    state.gatewayStatus = data.gateways || {};
    // The server publishes what each model actually supports; without this the
    // client guesses, and a wrong guess is a 400 that reads like a crash.
    state.caps = {};
    for (const m of (data.data || [])) if (m.caps) state.caps[m.id] = m.caps;
    const sel = $("model-select");
    sel.textContent = "";
    const gwNames = [...new Set((data.data || []).map((m) => m.gateway || ""))];
    for (const id of state.models) {
      const o = document.createElement("option");
      o.value = id;
      o.textContent = gwNames.length > 1 ? baseId(id) + "  \u00b7  " + gatewayOf(id) + (modelInfo(id).pinned ? " (direct)" : "") : id;
      sel.appendChild(o);
    }
    // a gateway's list is what it is configured for, not what is up: start
    // on the model that last worked, then the first one listed
    if (state.model && !state.models.includes(state.model)) state.model = "";
    if (!state.model && data.last_used && state.models.includes(data.last_used)) state.model = data.last_used;
    if (!state.model) {
      // a router lists every route it is configured for, live or not; an engine
      // (vLLM, Ollama, LM Studio, llama.cpp) lists only what it has loaded
      const direct = state.models.find((id) => { const k = ((state.gatewayStatus || {})[gatewayOf(id)] || {}).kind; return k && k !== "litellm" && !modelInfo(id).pinned; });
      if (direct) state.model = direct;
    }
    if (!state.model && state.models.length) state.model = state.models[0];
    if (state.model) sel.value = state.model;
    servingLine();
    const gwCount = Object.keys(state.gatewayStatus || {}).length;
    $("models-sub").textContent = state.models.length
      ? `${state.models.length} models across ${gwCount} gateway${gwCount === 1 ? "" : "s"}`
      : (gwCount ? "no models reachable — see the Gateways screen" : "no gateway configured — add one on the Gateways screen");
    const facts = $("model-facts");
    facts.innerHTML = "";
    const add = (k, v) => {
      const d = document.createElement("div");
      d.innerHTML = '<span class="k"></span><span class="mono"></span>';
      d.children[0].textContent = k;
      d.children[1].textContent = v;
      facts.appendChild(d);
    };
    add("Gateways", String(Object.keys(state.gatewayStatus || {}).length || "—"));
    add("Models", String(state.models.length || "—"));
    const lm = $("loaded-models");
    lm.textContent = "";
    for (const id of state.models) {
      const c = document.createElement("div");
      c.className = "row-card";
      c.innerHTML = '<div class="name-col"><b></b><small class="mono"></small></div>';
      c.querySelector("b").textContent = id;
      const gw = state.modelGateway[id]; const st = (state.gatewayStatus || {})[gw] || {};
      c.querySelector("small").textContent = "via " + (gw || "?") + (st.kind ? " \u00b7 " + st.kind : "") + ((state.caps[id] || {}).vision ? " \u00b7 vision" : "");
      lm.appendChild(c);
    }
    if (!state.models.length) {
      lm.innerHTML = '<div class="empty-state"><b>Upstream unreachable</b><span>Point config.json upstream_url at the gateway or a vLLM server, then reload.</span></div>';
    }
  }

  /* ---------------- tools (MCP) ---------------- */
  async function loadTools() {
    let d = { servers: {}, tools: [] };
    try { d = await (await fetch("/api/tools")).json(); } catch (e) {}
    // pair each flat tool name with its OpenAI definition for the request body
    state.tools = (d.tools || []).map((t) => ({
      name: t.name, description: t.description,
      def: { type: "function", function: { name: t.name, description: t.description,
             parameters: { type: "object", properties: {}, additionalProperties: true } } },
    }));
    // the server knows the real schemas; fetch them in full
    try {
      const full = await (await fetch("/api/tools?full=1")).json();
      if (full.defs) state.tools = full.defs.map((def) => ({ name: def.function.name, description: def.function.description, def: def }));
    } catch (e) {}
    renderServers(d.servers || {}, d.config || {});
    $("tools-count").textContent = state.tools.length
      ? state.tools.length + " tools available" : "no tools";
    $("tools-switch").classList.toggle("on", state.toolsOn);
  }

  /* ---------------- skills ---------------- */
  async function loadSkills() {
    let d = { skills: [], warnings: [] };
    try { d = await (await fetch("/api/skills")).json(); } catch (e) {}
    state.skills = d.skills || [];
    // drop any attached skill that no longer exists (a plugin was disabled)
    const names = new Set(state.skills.map((s) => s.name));
    state.activeSkills = state.activeSkills.filter((n) => names.has(n));
    renderActiveSkills();
  }

  async function skillBody(name) {
    if (state.skillBodies[name] != null) return state.skillBodies[name];
    try {
      const d = await (await fetch("/api/skills/" + encodeURIComponent(name))).json();
      state.skillBodies[name] = d.body || "";
    } catch (e) { state.skillBodies[name] = ""; }
    return state.skillBodies[name];
  }

  async function toggleSkill(name, on) {
    const set = new Set(state.activeSkills);
    if (on) { set.add(name); await skillBody(name); } else { set.delete(name); }
    state.activeSkills = state.skills.map((s) => s.name).filter((n) => set.has(n));
    renderActiveSkills();
    if (state.screen === "skills") renderSkills();
    saveSession();
  }

  async function setActiveSkills(names) {
    const have = new Set((state.skills || []).map((s) => s.name));
    state.activeSkills = (names || []).filter((n) => have.has(n));
    await Promise.all(state.activeSkills.map(skillBody));
    renderActiveSkills();
  }

  function renderActiveSkills() {
    const box = $("active-skills");
    if (!box) return;
    box.textContent = "";
    if (!state.activeSkills.length) { box.hidden = true; return; }
    box.hidden = false;
    const lab = document.createElement("span");
    lab.className = "as-label";
    lab.textContent = "skills";
    box.appendChild(lab);
    for (const name of state.activeSkills) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "as-chip";
      chip.innerHTML = '<span></span><span class="x">\u2715</span>';
      chip.querySelector("span").textContent = name;
      chip.title = "Detach " + name;
      chip.onclick = () => toggleSkill(name, false);
      box.appendChild(chip);
    }
  }

  function formRow(label, el) {
    const w = document.createElement("label");
    w.style.cssText = "display:flex;flex-direction:column;gap:4px;font-size:11.5px;color:var(--muted)";
    w.appendChild(document.createTextNode(label)); w.appendChild(el);
    return w;
  }
  function inputEl(ph, cls) { const i = document.createElement("input"); i.className = cls || "text-input"; i.placeholder = ph || ""; return i; }
  function textareaEl(ph, rows) { const t = document.createElement("textarea"); t.className = "sys-input"; t.rows = rows || 6; t.placeholder = ph || ""; return t; }

  function skillForm() {
    const card = document.createElement("div");
    card.className = "card"; card.style.gap = "10px";
    const head = document.createElement("div"); head.className = "skill-head";
    head.innerHTML = '<div class="skill-id"><b>New skill</b><span class="src mono">written as &lt;name&gt;/SKILL.md and mirrored to the agent worker</span></div>';
    const tgl = document.createElement("button"); tgl.type = "button"; tgl.className = "solid-btn"; tgl.textContent = "\uff0b New skill";
    head.appendChild(tgl); card.appendChild(head);
    const form = document.createElement("div"); form.hidden = true;
    form.style.cssText = "display:flex;flex-direction:column;gap:10px";
    const name = inputEl("kebab-case name, e.g. netscaler-triage");
    const desc = inputEl("one line: what it does (the catalog shows this to the Sultan)");
    const when = inputEl("when to use (optional routing hint)");
    const tools = inputEl("tools allowlist, comma-separated (optional): fetch_url, cve_record, run_shell");
    const model = inputEl("model hint (optional)");
    const net = document.createElement("input"); net.type = "checkbox";
    const netRow = document.createElement("label"); netRow.style.cssText = "display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)";
    netRow.appendChild(net); netRow.appendChild(document.createTextNode("needs network"));
    const over = document.createElement("input"); over.type = "checkbox";
    const overRow = document.createElement("label"); overRow.style.cssText = "display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)";
    overRow.appendChild(over); overRow.appendChild(document.createTextNode("overwrite if it exists"));
    const body = textareaEl("The instructions (markdown). Numbered discipline works best: what to do first, what counts as evidence, what to report.", 10);
    const row = document.createElement("div"); row.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const save = document.createElement("button"); save.type = "button"; save.className = "solid-btn"; save.textContent = "Save skill";
    const note = document.createElement("span"); note.className = "hint";
    row.appendChild(save); row.appendChild(netRow); row.appendChild(overRow); row.appendChild(note);
    [formRow("name", name), formRow("description", desc), formRow("when to use", when), formRow("tools", tools), formRow("model", model), formRow("instructions", body), row].forEach((e) => form.appendChild(e));
    card.appendChild(form);
    tgl.onclick = () => { form.hidden = !form.hidden; if (!form.hidden) name.focus(); };
    save.onclick = async () => {
      save.disabled = true; note.textContent = "saving\u2026";
      try {
        const r = await fetch("/api/skills", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "create", name: name.value.trim(), description: desc.value.trim(), whenToUse: when.value.trim(),
            tools: tools.value.trim(), network: net.checked, model: model.value.trim(), body: body.value, overwrite: over.checked }) });
        const d = await r.json();
        if (!r.ok || d.error) { note.textContent = d.error || ("HTTP " + r.status); save.disabled = false; return; }
        const w = d.worker || {};
        note.textContent = "saved \u00b7 " + (w.pushed ? "mirrored to the agent worker" : "not on the worker: " + (w.reason || "agents off"));
        setTimeout(renderSkills, 900);
      } catch (e) { note.textContent = "failed: " + e.message; save.disabled = false; }
    };
    return card;
  }

  async function deleteSkill(name) {
    if (!confirm("Delete skill \"" + name + "\" from the console and the agent worker?")) return;
    try {
      const r = await fetch("/api/skills", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete", name }) });
      const d = await r.json();
      if (d.error) alert(d.error);
    } catch (e) { alert("delete failed: " + e.message); }
    renderSkills();
  }

  async function renderSkills() {
    await loadSkills();
    const box = $("skills-box");
    $("skills-sub").textContent = state.skills.length
      ? state.skills.length + " available · " + state.activeSkills.length + " attached"
      : "none installed";
    box.textContent = "";
    box.appendChild(skillForm());
    if (!state.skills.length) {
      const e = document.createElement("div");
      e.innerHTML = '<div class="empty-state"><b>No skills installed</b><span>' +
        'Create one above, drop a <span class="mono">&lt;name&gt;/SKILL.md</span> into the skills folder, point ' +
        '<span class="mono">skills_dirs</span> at the agent harness to share its packs, or enable a plugin that ships skills.</span></div>';
      box.appendChild(e);
      return;
    }
    for (const sk of state.skills) {
      const on = state.activeSkills.includes(sk.name);
      const card = document.createElement("div");
      card.className = "card skill-card" + (on ? " on" : "");
      const head = document.createElement("div");
      head.className = "skill-head";
      head.innerHTML = '<div class="skill-id"><b></b><span class="src mono"></span></div>' +
        '<button class="switch' + (on ? " on" : "") + '"><span></span></button>';
      head.querySelector("b").textContent = sk.name;
      head.querySelector(".src").textContent = sk.source || "";
      head.querySelector(".switch").onclick = () => toggleSkill(sk.name, !state.activeSkills.includes(sk.name));
      const desc = document.createElement("div");
      desc.className = "skill-desc";
      desc.textContent = sk.description;
      card.appendChild(head); card.appendChild(desc);
      if (sk.whenToUse) {
        const w = document.createElement("div");
        w.className = "skill-when";
        w.textContent = "When to use: " + sk.whenToUse;
        card.appendChild(w);
      }
      const tags = [];
      if (sk.network) tags.push("needs network");
      if ((sk.tools || []).length) tags.push("tools: " + sk.tools.join(", "));
      if (sk.model) tags.push("model: " + sk.model);
      if (tags.length) {
        const t = document.createElement("div");
        t.className = "skill-tags mono";
        t.textContent = tags.join("  ·  ");
        card.appendChild(t);
      }
      const view = document.createElement("button");
      view.type = "button"; view.className = "linky"; view.textContent = "view instructions";
      const pre = document.createElement("pre");
      pre.className = "skill-body"; pre.hidden = true;
      view.onclick = async () => {
        if (pre.hidden) { pre.textContent = await skillBody(sk.name); pre.hidden = false; view.textContent = "hide instructions"; }
        else { pre.hidden = true; view.textContent = "view instructions"; }
      };
      card.appendChild(view);
      if (sk.source && !sk.source.startsWith("plugin:")) {
        const del = document.createElement("button");
        del.type = "button"; del.className = "linky"; del.textContent = "delete"; del.style.marginLeft = "12px";
        del.onclick = () => deleteSkill(sk.name);
        card.appendChild(del);
      }
      card.appendChild(pre);
      box.appendChild(card);
    }
  }

  /* ---------------- plugins ---------------- */
  function pluginForm() {
    const card = document.createElement("div");
    card.className = "card"; card.style.gap = "10px";
    const head = document.createElement("div"); head.className = "skill-head";
    head.innerHTML = '<div class="skill-id"><b>Add a plugin</b><span class="src mono">a git URL or a local folder with plugin.json, or start an empty one</span></div>';
    const tgl = document.createElement("button"); tgl.type = "button"; tgl.className = "solid-btn"; tgl.textContent = "\uff0b Add plugin";
    head.appendChild(tgl); card.appendChild(head);
    const form = document.createElement("div"); form.hidden = true;
    form.style.cssText = "display:flex;flex-direction:column;gap:10px";
    const src = inputEl("https://github.com/org/plugin.git  \u00b7  or  ~/path/to/plugin");
    const nm = inputEl("name (optional; derived from the source)");
    const addRow = document.createElement("div"); addRow.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const addBtn = document.createElement("button"); addBtn.type = "button"; addBtn.className = "solid-btn"; addBtn.textContent = "Install";
    const note = document.createElement("span"); note.className = "hint";
    addRow.appendChild(addBtn); addRow.appendChild(note);
    const sep = document.createElement("div"); sep.className = "hint"; sep.textContent = "\u2014 or create an empty plugin \u2014";
    const cname = inputEl("kebab-case name");
    const cdesc = inputEl("description");
    const cmcp = textareaEl('MCP servers as JSON (optional), e.g. {"fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]}}', 4);
    const cRow = document.createElement("div"); cRow.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const cBtn = document.createElement("button"); cBtn.type = "button"; cBtn.className = "solid-btn"; cBtn.textContent = "Create";
    const cnote = document.createElement("span"); cnote.className = "hint";
    cRow.appendChild(cBtn); cRow.appendChild(cnote);
    [formRow("source", src), formRow("name", nm), addRow, sep, formRow("name", cname), formRow("description", cdesc), formRow("MCP servers", cmcp), cRow].forEach((e) => form.appendChild(e));
    card.appendChild(form);
    tgl.onclick = () => { form.hidden = !form.hidden; };
    const post = async (payload, n, btn) => {
      btn.disabled = true; n.textContent = "working\u2026";
      try {
        const r = await fetch("/api/plugins", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const d = await r.json();
        if (d.error) { n.textContent = d.error; btn.disabled = false; return; }
        n.textContent = "done \u00b7 enable it below";
        setTimeout(renderPlugins, 700);
      } catch (e) { n.textContent = "failed: " + e.message; btn.disabled = false; }
    };
    addBtn.onclick = () => post({ action: "add", source: src.value.trim(), name: nm.value.trim() }, note, addBtn);
    cBtn.onclick = () => post({ action: "create", name: cname.value.trim(), description: cdesc.value.trim(), mcp_servers: cmcp.value.trim() }, cnote, cBtn);
    return card;
  }

  async function removePlugin(name) {
    if (!confirm("Remove plugin \"" + name + "\" (deletes its folder under the console's plugins dir)?")) return;
    try {
      const r = await fetch("/api/plugins", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "remove", name }) });
      const d = await r.json(); if (d.error) alert(d.error);
    } catch (e) { alert("remove failed: " + e.message); }
    await renderPlugins(); await loadSkills();
  }

  async function renderPlugins() {
    let d = { plugins: [], warnings: [] };
    try { d = await (await fetch("/api/plugins")).json(); } catch (e) {}
    state.plugins = d.plugins || [];
    const box = $("plugins-box");
    const on = state.plugins.filter((p) => p.enabled).length;
    $("plugins-sub").textContent = state.plugins.length
      ? state.plugins.length + " found · " + on + " enabled" : "none found";
    box.textContent = "";
    box.appendChild(pluginForm());
    if (!state.plugins.length) {
      const e = document.createElement("div");
      e.innerHTML = '<div class="empty-state"><b>No plugins found</b><span>' +
        'Add one above, or drop a folder under <span class="mono">plugins/</span> with a ' +
        '<span class="mono">plugin.json</span> that contributes skills and MCP servers. See plugins/README.md.</span></div>';
      box.appendChild(e);
      return;
    }
    for (const p of state.plugins) {
      const card = document.createElement("div");
      card.className = "card plugin-card" + (p.enabled ? " on" : "");
      const head = document.createElement("div");
      head.className = "skill-head";
      head.innerHTML = '<div class="skill-id"><b></b><span class="src mono"></span></div>' +
        '<button class="switch' + (p.enabled ? " on" : "") + '"><span></span></button>';
      head.querySelector("b").textContent = p.name;
      head.querySelector(".src").textContent = p.version ? "v" + p.version : "";
      head.querySelector(".switch").onclick = () => togglePlugin(p.name, !p.enabled);
      const desc = document.createElement("div");
      desc.className = "skill-desc";
      desc.textContent = p.description || "(no description)";
      card.appendChild(head); card.appendChild(desc);
      const bits = [];
      if (p.skills && p.skills.length) bits.push("skills: " + p.skills.join(", "));
      if (p.mcp_servers && p.mcp_servers.length) bits.push("MCP: " + p.mcp_servers.join(", "));
      const t = document.createElement("div");
      t.className = "skill-tags mono";
      t.textContent = bits.length ? bits.join("  ·  ") : "contributes nothing loadable";
      card.appendChild(t);
      const rm = document.createElement("button");
      rm.type = "button"; rm.className = "linky"; rm.textContent = "remove";
      rm.onclick = () => removePlugin(p.name);
      card.appendChild(rm);
      box.appendChild(card);
    }
  }

  async function togglePlugin(name, on) {
    try {
      await fetch("/api/plugins", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: on ? "enable" : "disable", name }),
      });
    } catch (e) {}
    await renderPlugins();
    await loadSkills();              // a plugin's skills came or went
    if (state.cfg.mcp || on) loadTools();   // and its MCP servers
  }

  /* ---------------- settings ---------------- */
  async function renderSettings() {
    let d = {};
    try { d = await (await fetch("/api/settings")).json(); } catch (e) {}
    $("settings-sub").textContent = (d.desktop ? "ByteBunker " + (d.version || "") + " \u00b7 desktop" : "console") + " \u00b7 " + (d.platform || "") + " \u00b7 Python " + (d.python || "");
    const box = $("settings-box"); box.textContent = "";
    const card = (title, sub) => { const c = document.createElement("div"); c.className = "card"; c.style.gap = "10px"; const h = document.createElement("div"); h.className = "skill-head"; h.innerHTML = '<div class="skill-id"><b></b><span class="src mono"></span></div>'; h.querySelector("b").textContent = title; h.querySelector(".src").textContent = sub || ""; c.appendChild(h); box.appendChild(c); return c; };
    const line = (c, k, v) => { const r = document.createElement("div"); r.className = "mono"; r.style.cssText = "font-size:12px;color:var(--muted);word-break:break-all"; r.textContent = k + "  " + v; c.appendChild(r); return r; };
    const api = window.pywebview && window.pywebview.api;
    // where things live
    const where = card("Your data", "everything you own lives here; the app itself holds none of it");
    line(where, "folder", d.home_dir || ""); line(where, "config", d.config_path || ""); line(where, "traces, usage, sessions", d.data_dir || ""); line(where, "attachments", d.uploads_dir || "");
    const wrow = document.createElement("div"); wrow.style.cssText = "display:flex;gap:10px;flex-wrap:wrap";
    if (api && api.open_folder) { const b = document.createElement("button"); b.type = "button"; b.className = "ghost-btn"; b.textContent = "Open folder"; b.onclick = () => api.open_folder(d.home_dir); wrow.appendChild(b); }
    const ex = document.createElement("button"); ex.type = "button"; ex.className = "ghost-btn"; ex.textContent = "Export everything (JSONL)"; ex.onclick = () => openOut("/api/export"); wrow.appendChild(ex);
    where.appendChild(wrow);
    // name
    const who = card("Name", "shown in the sidebar and stamped on exports");
    const user = inputEl("name"); user.value = (d.identity || {}).user || ""; const host = inputEl("machine"); host.value = (d.identity || {}).host || "";
    who.appendChild(formRow("name", user)); who.appendChild(formRow("machine", host));
    // rates
    const rt = card("Frontier-API equivalent", "the price per million tokens Usage compares your local tokens against");
    const rin = inputEl("input $/Mtok"); rin.type = "number"; rin.step = "0.01"; rin.value = (d.rates || {}).input ?? 3;
    const rout = inputEl("output $/Mtok"); rout.type = "number"; rout.step = "0.01"; rout.value = (d.rates || {}).output ?? 15;
    rt.appendChild(formRow("input, $ per million tokens", rin)); rt.appendChild(formRow("output, $ per million tokens", rout));
    const srow = document.createElement("div"); srow.style.cssText = "display:flex;gap:10px;align-items:center";
    const save = document.createElement("button"); save.type = "button"; save.className = "solid-btn"; save.textContent = "Save";
    const note = document.createElement("span"); note.className = "hint";
    save.onclick = async () => {
      try { await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user: user.value, host: host.value, rates: { input: parseFloat(rin.value), output: parseFloat(rout.value) } }) }); note.textContent = "saved"; await refreshConfig(); $("who-user").textContent = state.cfg.identity.user || "local"; $("who-host").textContent = state.cfg.identity.host || ""; } catch (e) { note.textContent = e.message; }
    };
    srow.appendChild(save); srow.appendChild(note); rt.appendChild(srow);
    // updates: only when asked — the app does not phone home on its own
    if (d.desktop) {
      const up = card("Updates", "checked only when you press the button; nothing is downloaded or installed");
      const urow = document.createElement("div"); urow.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
      const ub = document.createElement("button"); ub.type = "button"; ub.className = "ghost-btn"; ub.textContent = "Check for updates";
      const un = document.createElement("span"); un.className = "hint";
      ub.onclick = async () => {
        un.textContent = "asking GitHub\u2026";
        try {
          const r = await (await fetch("https://api.github.com/repos/bytebunkerlabs/bytebunker-console/releases/latest")).json();
          const latest = String(r.tag_name || "").replace(/^v/, "");
          if (!latest) { un.textContent = "no published release yet"; return; }
          if (latest === d.version) { un.textContent = "you have the latest (" + latest + ")"; return; }
          un.textContent = latest + " is available \u2014 ";
          const a = document.createElement("button"); a.type = "button"; a.className = "linky"; a.textContent = "open the release page"; a.onclick = () => openOut(r.html_url); un.appendChild(a);
        } catch (e) { un.textContent = "could not reach GitHub: " + e.message; }
      };
      urow.appendChild(ub); urow.appendChild(un); up.appendChild(urow);
    }
  }

  /* ---------------- gateways ---------------- */
  function openOut(u) {
    const abs = new URL(u, location.href).href;
    const api = window.pywebview && window.pywebview.api;
    if (api && api.open_external) return api.open_external(abs);
    window.open(abs, "_blank");
  }
  function gatewayEditor(g) {
    const pane = $("gateways-detail"); pane.textContent = "";
    const h = document.createElement("div"); h.className = "skill-head"; h.innerHTML = '<div class="skill-id"><b></b><span class="src mono">changes apply at once; the key is stored in the console config on this machine</span></div>';
    h.querySelector("b").textContent = "Edit " + g.name; pane.appendChild(h);
    const nm = inputEl("name"); nm.value = g.name;
    const url = inputEl("http://host:port/v1"); url.value = g.url;
    const key = inputEl(g.has_key ? "a key is set \u2014 type to replace, or clear below" : "API key (optional)"); key.type = "password";
    const clear = document.createElement("input"); clear.type = "checkbox";
    const clearRow = document.createElement("label"); clearRow.style.cssText = "display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)"; clearRow.appendChild(clear); clearRow.appendChild(document.createTextNode("remove the stored key"));
    const kind = document.createElement("select"); kind.className = "text-input";
    for (const k of ["", "litellm", "vllm", "ollama", "lmstudio", "llama.cpp", "openai-compatible"]) { const o = document.createElement("option"); o.value = k; o.textContent = k || "(detect)"; kind.appendChild(o); }
    kind.value = g.kind || "";
    const row = document.createElement("div"); row.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const save = document.createElement("button"); save.type = "button"; save.className = "solid-btn"; save.textContent = "Save";
    const note = document.createElement("span"); note.className = "hint";
    save.onclick = async () => {
      const body = { action: "update", name: g.name, url: url.value.trim(), kind: kind.value };
      if (nm.value.trim() && nm.value.trim() !== g.name) body.new_name = nm.value.trim();
      if (key.value) body.key = key.value; else if (clear.checked) body.key = "";
      save.disabled = true;
      try { await gwPost(body); note.textContent = "saved"; renderGateways(); afterGatewayChange(); } catch (e) { note.textContent = e.message; save.disabled = false; }
    };
    row.appendChild(save); row.appendChild(note);
    [formRow("name", nm), formRow("address", url), formRow("key", key), clearRow, formRow("kind", kind), row].forEach((e) => pane.appendChild(e));
  }
  async function refreshConfig() {
    try { state.cfg = await (await fetch("/api/config")).json(); } catch (e) {}
    updateFirstRun();
  }
  function updateFirstRun() {
    const none = !((state.cfg && state.cfg.gateways) || []).length;
    $("first-run").hidden = !none;
    $("starter-chips").hidden = none;
  }
  $("first-run-find").onclick = () => { state.autoDiscover = true; go("gateways"); };
  $("first-run-add").onclick = () => { state.focusAddGateway = true; go("gateways"); };
  async function afterGatewayChange() { await refreshConfig(); await loadModels(); }
  async function gwPost(payload) {
    const r = await fetch("/api/gateways", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const d = await r.json(); if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status)); return d;
  }
  async function renderGateways() {
    let d = { gateways: [], models: 0 };
    try { d = await (await fetch("/api/gateways")).json(); } catch (e) {}
    const left = $("gateways-left"); left.textContent = "";
    const up = d.gateways.filter((g) => g.ok).length;
    $("gateways-sub").textContent = d.gateways.length + " configured \u00b7 " + up + " reachable \u00b7 " + d.models + " models";
    for (const g of d.gateways) {
      const card = document.createElement("div"); card.className = "card"; card.style.gap = "8px"; if (!g.enabled) card.style.opacity = ".6";
      const head = document.createElement("div"); head.className = "skill-head";
      head.innerHTML = '<div class="skill-id"><b></b><span class="src mono"></span></div><span class="pill"><span class="d"></span><span class="pt"></span></span>';
      head.querySelector("b").textContent = g.name;
      head.querySelector(".src").textContent = g.url + (g.kind ? "  \u00b7  " + g.kind : "") + (g.has_key ? "  \u00b7  key set" : "  \u00b7  no key");
      head.querySelector(".pt").textContent = !g.enabled ? "disabled" : g.ok ? g.models.length + " models \u00b7 " + g.ms + " ms" : "unreachable";
      if (!g.ok) head.querySelector(".pill").style.opacity = ".6";
      card.appendChild(head);
      if (g.error) { const e = document.createElement("div"); e.className = "hint"; e.style.color = "var(--err)"; e.textContent = g.error; card.appendChild(e); }
      const ml = document.createElement("div"); ml.className = "skill-tags mono"; ml.textContent = g.models.length ? g.models.slice(0, 12).join(", ") + (g.models.length > 12 ? " +" + (g.models.length - 12) : "") : ""; card.appendChild(ml);
      const lv = document.createElement("div"); lv.className = "mono gw-live"; lv.dataset.gw = g.name; lv.style.cssText = "font-size:11.5px;color:var(--muted)"; card.appendChild(lv);
      const acts = document.createElement("div"); acts.style.cssText = "display:flex;gap:8px;flex-wrap:wrap;align-items:center";
      const mk = (label, cls, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = cls; b.textContent = label; b.onclick = fn; acts.appendChild(b); };
      mk(g.enabled ? "disable" : "enable", "ghost-btn", async () => { try { await gwPost({ action: "toggle", name: g.name }); } catch (e) { alert(e.message); } renderGateways(); afterGatewayChange(); });
      mk("edit", "ghost-btn", () => gatewayEditor(g));
      mk("remove", "rate-btn bad", async () => { if (!confirm("Remove gateway " + g.name + "?")) return; try { await gwPost({ action: "remove", name: g.name }); } catch (e) { alert(e.message); } renderGateways(); afterGatewayChange(); });
      card.appendChild(acts); left.appendChild(card);
    }
    // add by address
    const add = document.createElement("div"); add.className = "card"; add.style.gap = "8px";
    add.innerHTML = '<div class="skill-head"><div class="skill-id"><b>Add a gateway</b><span class="src mono">an IP and port is enough: 172.16.25.83:8001 becomes http://172.16.25.83:8001/v1</span></div></div>';
    const url = inputEl("host:port, or a full http://host:port/v1"), nm = inputEl("name (optional)"), key = inputEl("API key (optional)"); key.type = "password";
    const row = document.createElement("div"); row.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const btn = document.createElement("button"); btn.type = "button"; btn.className = "solid-btn"; btn.textContent = "Add"; const note = document.createElement("span"); note.className = "hint";
    btn.onclick = async () => { btn.disabled = true; note.textContent = "checking\u2026"; try { const r = await gwPost({ action: "add", url: url.value.trim(), name: nm.value.trim(), key: key.value }); note.textContent = "added \u00b7 " + r.models + " models now"; url.value = ""; nm.value = ""; key.value = ""; renderGateways(); afterGatewayChange(); } catch (e) { note.textContent = e.message; } btn.disabled = false; };
    row.appendChild(btn); row.appendChild(note);
    [formRow("address", url), formRow("name", nm), formRow("key", key), row].forEach((e) => add.appendChild(e)); left.appendChild(add);
    // discovery
    const disc = document.createElement("div"); disc.className = "card"; disc.style.gap = "8px";
    disc.innerHTML = '<div class="skill-head"><div class="skill-id"><b>Find engines</b><span class="src mono">this machine \u00b7 hosts of known gateways \u00b7 tailnet peers \u00b7 optionally the whole LAN \u00b7 ports 11434 1234 4000 8000 8001 8080 8888 5000 3000</span></div></div>';
    const hosts = inputEl("extra hosts to probe, comma-separated (optional)");
    const lan = document.createElement("input"); lan.type = "checkbox"; const lanRow = document.createElement("label"); lanRow.style.cssText = "display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)"; lanRow.appendChild(lan); lanRow.appendChild(document.createTextNode("scan my whole LAN (/24, ~20 s)"));
    const drow = document.createElement("div"); drow.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const dbtn = document.createElement("button"); dbtn.type = "button"; dbtn.className = "solid-btn"; dbtn.textContent = "Find engines"; const dnote = document.createElement("span"); dnote.className = "hint";
    drow.appendChild(dbtn); drow.appendChild(lanRow); drow.appendChild(dnote);
    disc.appendChild(formRow("hosts", hosts)); disc.appendChild(drow); left.appendChild(disc);
    if (state.focusAddGateway) { state.focusAddGateway = false; url.focus(); }
    dbtn.onclick = async () => {
      dbtn.disabled = true; dnote.textContent = "probing\u2026";
      const pane = $("gateways-detail"); pane.textContent = "";
      try {
        const r = await gwPost({ action: "discover", hosts: hosts.value, scan_lan: lan.checked });
        dnote.textContent = r.found.length + " found on " + r.hosts_probed + " hosts in " + Math.round(r.ms / 1000) + " s";
        const h = document.createElement("div"); h.className = "skill-head"; h.innerHTML = '<div class="skill-id"><b>Engines found</b></div>'; pane.appendChild(h);
        if (!r.found.length) {
          const n = document.createElement("div"); n.className = "hint";
          n.textContent = "Nothing answered on the usual ports. Add by address if you know it, or tick the LAN scan." +
            (state.cfg.platform === "darwin" ? " On a Mac, the first search can come back empty while macOS asks whether ByteBunker may use the local network: allow it in the prompt (or System Settings \u203a Privacy & Security \u203a Local Network) and search again." : "") +
            (state.cfg.platform === "win32" ? " On Windows, allow ByteBunker through the firewall if it asked, then search again." : "");
          pane.appendChild(n);
        }
        for (const f of r.found) {
          const c = document.createElement("div"); c.className = "card"; c.style.gap = "6px";
          const t = document.createElement("div"); t.className = "skill-head"; t.innerHTML = '<div class="skill-id"><b class="mono"></b><span class="src mono"></span></div>';
          t.querySelector("b").textContent = f.url; t.querySelector(".src").textContent = (f.kind || "openai-compatible") + (f.configured ? "  \u00b7  already configured" : "") + (f.needs_key ? "  \u00b7  needs a key" : "");
          c.appendChild(t);
          if (f.models && f.models.length) { const m = document.createElement("div"); m.className = "skill-tags mono"; m.textContent = f.models.slice(0, 10).join(", ") + (f.models.length > 10 ? " +" + (f.models.length - 10) : ""); c.appendChild(m); }
          if (!f.configured) {
            const rowx = document.createElement("div"); rowx.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap";
            const kin = f.needs_key ? inputEl("API key") : null; if (kin) { kin.type = "password"; rowx.appendChild(kin); }
            const ab = document.createElement("button"); ab.type = "button"; ab.className = "solid-btn"; ab.textContent = "Add"; const an = document.createElement("span"); an.className = "hint";
            ab.onclick = async () => { ab.disabled = true; try { const x = await gwPost({ action: "add", url: f.url, name: f.host + ":" + f.port, key: kin ? kin.value : "", kind: f.kind }); an.textContent = "added \u00b7 " + x.models + " models now"; renderGateways(); afterGatewayChange(); } catch (e) { an.textContent = e.message; ab.disabled = false; } };
            rowx.appendChild(ab); rowx.appendChild(an); c.appendChild(rowx);
          }
          pane.appendChild(c);
        }
      } catch (e) { dnote.textContent = e.message; }
      dbtn.disabled = false;
    };
    if (state.autoDiscover) { state.autoDiscover = false; dbtn.click(); }
    pollGatewayLive();
    clearInterval(state.gwLiveTimer);
    state.gwLiveTimer = setInterval(() => { if (state.screen === "gateways") pollGatewayLive(); else clearInterval(state.gwLiveTimer); }, 15000);
  }
  // what each engine is doing now, filled into the cards without rebuilding
  // the screen (a rebuild would eat a half-typed address)
  async function pollGatewayLive() {
    let d = {};
    try { d = await (await fetch("/api/gateways?live=1")).json(); } catch (e) { return; }
    const live = d.live || {};
    document.querySelectorAll(".gw-live").forEach((el) => {
      const st = live[el.dataset.gw] || {};
      const bits = [];
      if ("running" in st) {
        bits.push(st.running + " running \u00b7 " + st.waiting + " waiting", "KV " + st.kv_pct + "%");
        if (st.gen_tps != null) bits.push(st.gen_tps + " tok/s out \u00b7 " + st.prompt_tps + " tok/s in");
      }
      if (st.loaded) bits.push(st.loaded.length ? "loaded: " + st.loaded.map((m) => m.name + (m.vram_gb ? " (" + m.vram_gb + " GB)" : "")).join(", ") : "nothing loaded");
      if (st.error) bits.push("live stats unavailable: " + st.error);
      el.textContent = bits.join("  \u00b7  ");
    });
  }

  /* ---------------- scheduled jobs ---------------- */
  let jobsTimer = null, jobsSelected = null, jobsDetail = "runs";   // jobsDetail: "runs" or "edit"
  function fmtWhen(ts) { if (!ts) return "\u2014"; const d = new Date(ts * 1000); return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }
  function jobForm(existing) {
    const j = existing || {};
    const card = document.createElement("div"); card.className = "card"; card.style.gap = "8px";
    const head = document.createElement("div"); head.className = "skill-head";
    head.innerHTML = '<div class="skill-id"><b></b><span class="src mono">cron or every N minutes \u00b7 one prompt, or one goal for the Sultan</span></div>';
    head.querySelector("b").textContent = existing ? "Edit job" : "New job";
    const tgl = document.createElement("button"); tgl.type = "button"; tgl.className = existing ? "ghost-btn" : "solid-btn"; tgl.textContent = existing ? "Close" : "\uff0b New job";
    head.appendChild(tgl); card.appendChild(head);
    const form = document.createElement("div"); form.hidden = !existing; form.style.cssText = "display:flex;flex-direction:column;gap:8px";
    const name = inputEl("name"); name.value = j.name || "";
    const kind = document.createElement("select"); kind.className = "text-input";
    for (const [v, l] of [["chat", "chat: one prompt, with tools and skills"], ["agent", "agent: a goal for the Sultan and the court"]]) { const o = document.createElement("option"); o.value = v; o.textContent = l; kind.appendChild(o); }
    kind.value = j.kind || "chat";
    const skind = document.createElement("select"); skind.className = "text-input";
    for (const [v, l] of [["cron", "cron expression"], ["interval", "every N minutes"]]) { const o = document.createElement("option"); o.value = v; o.textContent = l; skind.appendChild(o); }
    skind.value = (j.schedule || {}).kind || "cron";
    const cron = inputEl("minute hour day month weekday \u2014 e.g. 0 9 * * mon-fri"); cron.value = (j.schedule || {}).cron || "0 9 * * mon-fri";
    const every = inputEl("minutes"); every.type = "number"; every.min = "1"; every.value = (j.schedule || {}).every_min || 60;
    const prompt = textareaEl("What should the model do each time? Be concrete: what to look at, what to produce, where to write it.", 5); prompt.value = j.prompt || "";
    const model = document.createElement("select"); model.className = "text-input";
    for (const m of (state.models || [])) { const o = document.createElement("option"); o.value = m.id || m; o.textContent = m.id || m; model.appendChild(o); }
    model.value = j.model || state.model || "";
    const tools = document.createElement("input"); tools.type = "checkbox"; tools.checked = j.tools !== false;
    const toolsRow = document.createElement("label"); toolsRow.style.cssText = "display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)"; toolsRow.appendChild(tools); toolsRow.appendChild(document.createTextNode("let it use the MCP tools"));
    const skills = inputEl("skills to attach, comma-separated (optional): web-research, cve-lookup"); skills.value = (j.skills || []).join(", ");
    const sys = textareaEl("standing instructions for this job (optional)", 2); sys.value = j.system || "";
    const hops = inputEl("max tool hops"); hops.type = "number"; hops.min = "1"; hops.max = "30"; hops.value = j.max_hops || 12;
    const chatOnly = [formRow("model", model), toolsRow, formRow("skills", skills), formRow("instructions", sys), formRow("max tool hops", hops)];
    const cronRow = formRow("cron", cron), everyRow = formRow("every (minutes)", every);
    const sync = () => { const c = kind.value === "chat"; chatOnly.forEach((e) => e.hidden = !c); cronRow.hidden = skind.value !== "cron"; everyRow.hidden = skind.value !== "interval"; prompt.placeholder = c ? "What should the model do each time? Be concrete: what to look at, what to produce, where to write it." : "The goal for the Sultan, as you would type it on the Agents screen."; };
    kind.onchange = sync; skind.onchange = sync;
    [formRow("name", name), formRow("type", kind), formRow("schedule", skind), cronRow, everyRow, formRow(kind.value === "chat" ? "prompt" : "goal", prompt)].concat(chatOnly).forEach((e) => form.appendChild(e));
    sync();
    const row = document.createElement("div"); row.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const save = document.createElement("button"); save.type = "button"; save.className = "solid-btn"; save.textContent = existing ? "Save changes" : "Create job";
    const note = document.createElement("span"); note.className = "hint";
    row.appendChild(save); row.appendChild(note); form.appendChild(row); card.appendChild(form);
    tgl.onclick = () => { if (existing) { jobsDetail = "runs"; renderJobs(); } else { form.hidden = !form.hidden; } };
    save.onclick = async () => {
      save.disabled = true; note.textContent = "saving\u2026";
      const job = { id: j.id, name: name.value.trim(), kind: kind.value, prompt: prompt.value, enabled: j.enabled !== false,
        schedule: skind.value === "cron" ? { kind: "cron", cron: cron.value.trim() } : { kind: "interval", every_min: parseInt(every.value, 10) || 60 },
        model: model.value, tools: tools.checked, skills: skills.value.split(",").map((x) => x.trim()).filter(Boolean), system: sys.value, max_hops: parseInt(hops.value, 10) || 12 };
      try {
        const r = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "save", job }) });
        const d = await r.json(); if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
        jobsSelected = d.job.id; jobsDetail = "runs"; renderJobs();
      } catch (e) { note.textContent = e.message; save.disabled = false; }
    };
    return card;
  }
  // listOnly: what changed is the jobs, not the screen. The new-job form and
  // an open edit form stay as they are (a refresh used to wipe them mid-typing).
  async function renderJobs(listOnly) {
    let d = { jobs: [], running: {} };
    try { d = await (await fetch("/api/jobs")).json(); } catch (e) { if (listOnly) return; }
    const running = d.running || {};
    const left = $("jobs-left");
    if (listOnly && left.querySelector(".job-new")) {
      left.querySelectorAll(".job-item").forEach((e) => e.remove());
    } else {
      left.textContent = "";
      const f = jobForm(null); f.classList.add("job-new"); left.appendChild(f);
    }
    const on = d.jobs.filter((j) => j.enabled).length;
    const nrun = Object.keys(running).length;
    $("jobs-sub").textContent = d.jobs.length + " jobs \u00b7 " + on + " enabled" + (nrun ? " \u00b7 " + nrun + " running" : "");
    if (!d.jobs.length) { const e = document.createElement("div"); e.className = "hint job-item"; e.textContent = "No jobs yet. A morning summary of the trace log, a nightly CVE sweep for your products, a weekly report written to a file: anything you would ask in the Playground, on a timer."; left.appendChild(e); }
    for (const j of d.jobs) {
      const card = document.createElement("div"); card.className = "card job-item" + (jobsSelected === j.id ? " skill-card on" : ""); card.style.gap = "8px"; card.style.cursor = "pointer";
      const head = document.createElement("div"); head.className = "skill-head";
      head.innerHTML = '<div class="skill-id"><b></b><span class="src mono"></span></div><span class="src mono" style="text-align:right"></span>';
      head.querySelector("b").textContent = (j.enabled ? "" : "\u23f8 ") + j.name;
      head.querySelectorAll(".src")[0].textContent = j.kind + " \u00b7 " + j.schedule_text + (j.model && j.kind === "chat" ? " \u00b7 " + j.model : "") + (j.created_by && j.created_by !== "you" ? " \u00b7 filed by " + j.created_by : "");
      head.querySelectorAll(".src")[1].textContent = (j.enabled && j.next_run ? "next " + fmtWhen(j.next_run) : "paused") + (running[j.id] ? " \u00b7 " + (running[j.id].state === "queued" ? "QUEUED" : "RUNNING") : "");
      const desc = document.createElement("div"); desc.className = "skill-desc"; desc.textContent = j.prompt.length > 220 ? j.prompt.slice(0, 220) + "\u2026" : j.prompt;
      const last = document.createElement("div"); last.className = "skill-tags mono";
      last.textContent = j.last ? ("last " + fmtWhen(j.last.ts) + " \u00b7 " + (j.last.ok ? "ok" : "failed") + " \u00b7 " + Math.round((j.last.ms || 0) / 1000) + " s" + (j.last.tokens ? " \u00b7 " + j.last.tokens + " tok" : "") + " \u00b7 " + (j.runs || 0) + " runs") : "never run";
      const acts = document.createElement("div"); acts.style.cssText = "display:flex;gap:8px;flex-wrap:wrap";
      const mk = (label, cls, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = cls; b.textContent = label; b.onclick = (e) => { e.stopPropagation(); fn(); }; acts.appendChild(b); };
      const post = async (payload) => { try { const r = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); const x = await r.json(); if (x.error) alert(x.error); } catch (e) { alert(e.message); } renderJobs(true); };
      mk("run now", "solid-btn", () => post({ action: "run_now", id: j.id }));
      mk(j.enabled ? "pause" : "resume", "ghost-btn", () => post({ action: "toggle", id: j.id }));
      mk("edit", "ghost-btn", () => { jobsDetail = "edit"; const pane = $("jobs-detail"); pane.textContent = ""; pane.appendChild(jobForm(j)); });
      mk("delete", "rate-btn bad", () => { if (confirm("Delete job \"" + j.name + "\" and its history?")) post({ action: "delete", id: j.id }); });
      card.appendChild(head); card.appendChild(desc); card.appendChild(last); card.appendChild(acts);
      card.onclick = () => { jobsSelected = j.id; showJobRuns(j); };
      left.appendChild(card);
    }
    if (jobsSelected && jobsDetail === "runs") { const j = d.jobs.find((x) => x.id === jobsSelected); if (j) showJobRuns(j); }
    // the event stream refreshes this as jobs change; the timer is a fallback
    clearInterval(jobsTimer);
    jobsTimer = setInterval(() => { if (state.screen === "jobs") renderJobs(true); else clearInterval(jobsTimer); }, 60000);
  }
  async function showJobRuns(j) {
    jobsDetail = "runs";
    const pane = $("jobs-detail"); pane.textContent = "";
    const h = document.createElement("div"); h.className = "skill-head"; h.innerHTML = '<div class="skill-id"><b></b><span class="src mono"></span></div>';
    h.querySelector("b").textContent = j.name; h.querySelector(".src").textContent = j.kind + " \u00b7 " + j.schedule_text; pane.appendChild(h);
    let d = { runs: [] };
    try { d = await (await fetch("/api/jobs/runs?id=" + encodeURIComponent(j.id))).json(); } catch (e) {}
    if (!d.runs.length) { const n = document.createElement("div"); n.className = "hint"; n.textContent = "No runs yet. Use \"run now\" to try it."; pane.appendChild(n); return; }
    for (const r of d.runs) {
      const c = document.createElement("div"); c.className = "card"; c.style.gap = "6px";
      const top = document.createElement("div"); top.className = "skill-tags mono";
      top.textContent = fmtWhen(r.ts) + " \u00b7 " + r.trigger + " \u00b7 " + (r.missed ? "MISSED" : (r.ok ? "ok" : "FAILED") + " \u00b7 " + Math.round((r.ms || 0) / 1000) + " s") + (r.hops ? " \u00b7 " + r.hops + " hops" : "") + (r.tokens ? " \u00b7 " + r.tokens + " tok" : "");
      c.appendChild(top);
      const pre = document.createElement("pre"); pre.className = "skill-body"; pre.style.whiteSpace = "pre-wrap"; pre.style.maxHeight = "420px";
      pre.textContent = r.error ? ("error: " + r.error) : (r.output || "(no output)");
      c.appendChild(pre); pane.appendChild(c);
    }
  }

  /* ---------------- MCP screen ---------------- */
  function mcpPane(title) {
    const pane = $("mcp-detail"); pane.textContent = "";
    const h = document.createElement("div"); h.className = "skill-head"; h.innerHTML = '<div class="skill-id"><b></b></div>';
    h.querySelector("b").textContent = title; pane.appendChild(h);
    return pane;
  }
  async function showServerTools(name) {
    const pane = mcpPane(name + " \u00b7 tools");
    let full = { defs: [] };
    try { full = await (await fetch("/api/tools?full=1")).json(); } catch (e) {}
    const defs = (full.defs || []).filter((d) => d.function.name.startsWith(name + "__"));
    if (!defs.length) { const n = document.createElement("div"); n.className = "hint"; n.textContent = "no tools reported (server not ready, or disabled)"; pane.appendChild(n); return; }
    for (const d of defs) {
      const c = document.createElement("div"); c.className = "card"; c.style.gap = "4px";
      const b = document.createElement("b"); b.className = "mono"; b.style.fontSize = "12.5px"; b.textContent = d.function.name.slice(name.length + 2);
      const t = document.createElement("div"); t.className = "skill-desc"; t.textContent = d.function.description || "";
      const props = Object.keys((d.function.parameters || {}).properties || {});
      const a = document.createElement("div"); a.className = "skill-tags mono"; a.textContent = props.length ? "args: " + props.join(", ") : "no arguments";
      c.appendChild(b); c.appendChild(t); c.appendChild(a); pane.appendChild(c);
    }
  }
  async function mcpPost(payload) {
    const r = await fetch("/api/mcp", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  async function renderMcp() {
    let d = { catalog: [], runtimes: {}, servers: {}, config: {} };
    try { d = await (await fetch("/api/mcp/catalog")).json(); } catch (e) {}
    const left = $("mcp-left"); left.textContent = "";
    const names = Object.keys(Object.assign({}, d.config, d.servers)).filter((n) => !n.startsWith("_"));
    const ready = names.filter((n) => (d.servers[n] || {}).state === "ready").length;
    $("mcp-sub").textContent = names.length + " configured \u00b7 " + ready + " ready \u00b7 runtimes: " + Object.entries(d.runtimes || {}).filter(([k, v]) => v).map(([k]) => k).join(", ");

    // configured servers
    const cfgCard = document.createElement("div"); cfgCard.className = "card"; cfgCard.style.gap = "8px";
    cfgCard.innerHTML = '<div class="skill-head"><div class="skill-id"><b>Configured servers</b><span class="src mono">started by the console as subprocesses \u00b7 config.json \u2192 mcp_servers</span></div></div>';
    if (!names.length) { const n = document.createElement("div"); n.className = "hint"; n.textContent = "None yet. Add one from the catalog below."; cfgCard.appendChild(n); }
    for (const name of names) {
      const st = d.servers[name] || { state: "unknown", tools: 0 };
      const cfg = d.config[name] || {};
      const on = cfg.enabled !== false;
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--border);border-radius:9px;flex-wrap:wrap" + (on ? "" : ";opacity:.6");
      row.innerHTML = '<span class="d" style="width:8px;height:8px;border-radius:50%;flex:none"></span><b class="mono" style="font-size:13px"></b><span class="s mono" style="font-size:11.5px;color:var(--muted)"></span><span class="cmd mono" style="font-size:11px;color:var(--faint);flex:1;min-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span><span class="acts" style="display:flex;gap:6px"></span>';
      row.querySelector(".d").style.background = st.state === "ready" ? "var(--ok)" : st.state === "error" ? "var(--err)" : "var(--faint)";
      row.querySelector("b").textContent = name;
      row.querySelector(".s").textContent = st.state === "ready" ? st.tools + " tools" : (st.state || "") + (st.error ? " \u00b7 " + st.error.slice(0, 80) : "");
      row.querySelector(".cmd").textContent = (cfg.command || "") + " " + (cfg.args || []).join(" ");
      row.querySelector(".cmd").title = row.querySelector(".cmd").textContent;
      const acts = row.querySelector(".acts");
      const mk = (label, cls, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = cls; b.textContent = label; b.onclick = fn; acts.appendChild(b); };
      mk("tools", "ghost-btn", () => showServerTools(name));
      mk(on ? "disable" : "enable", "ghost-btn", async () => { try { await mcpPost({ action: "toggle", name }); } catch (e) { alert(e.message); } renderMcp(); loadTools(); });
      mk("restart", "ghost-btn", async () => { try { await mcpPost({ action: "restart" }); } catch (e) { alert(e.message); } renderMcp(); loadTools(); });
      const envKeys = Object.keys(cfg.env || {});
      mk("env" + (envKeys.length ? " (" + envKeys.length + ")" : ""), "ghost-btn", () => {
        const pane = mcpPane(name + " \u00b7 environment");
        const hint = document.createElement("div"); hint.className = "hint"; hint.textContent = "Values are stored in the console's config.json and passed only to this server's process. Empty a value to remove it."; pane.appendChild(hint);
        const form = document.createElement("div"); form.style.cssText = "display:flex;flex-direction:column;gap:8px;margin-top:8px";
        const inputs = {};
        const addRow = (k, v) => { const i = inputEl("value"); i.value = v || ""; if (/token|key|secret|password/i.test(k)) i.type = "password"; inputs[k] = i; form.appendChild(formRow(k, i)); };
        envKeys.forEach((k) => addRow(k, cfg.env[k]));
        const nk = inputEl("NEW_VARIABLE"); const nv = inputEl("value"); form.appendChild(formRow("add a variable", nk)); form.appendChild(formRow("its value", nv));
        const save = document.createElement("button"); save.type = "button"; save.className = "solid-btn"; save.textContent = "Save and restart";
        const note = document.createElement("span"); note.className = "hint";
        save.onclick = async () => { const env = {}; for (const k in inputs) env[k] = inputs[k].value; if (nk.value.trim()) env[nk.value.trim()] = nv.value; try { await mcpPost({ action: "set_env", name, env }); note.textContent = "saved"; renderMcp(); loadTools(); } catch (e) { note.textContent = e.message; } };
        const row2 = document.createElement("div"); row2.style.cssText = "display:flex;gap:10px;align-items:center"; row2.appendChild(save); row2.appendChild(note);
        form.appendChild(row2); pane.appendChild(form);
      });
      mk("remove", "rate-btn bad", async () => { if (!confirm("Remove MCP server \"" + name + "\"?")) return; try { await mcpPost({ action: "remove", name }); } catch (e) { alert(e.message); } renderMcp(); loadTools(); });
      cfgCard.appendChild(row);
    }
    left.appendChild(cfgCard);

    // catalog
    const cat = document.createElement("div"); cat.className = "card"; cat.style.gap = "8px";
    cat.innerHTML = '<div class="skill-head"><div class="skill-id"><b>Catalog</b><span class="src mono">local stdio servers \u00b7 Add starts it on first use; Install locally pre-installs the package first</span></div></div>';
    const grid = document.createElement("div"); grid.style.cssText = "display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px";
    for (const c of d.catalog || []) {
      const card = document.createElement("div"); card.style.cssText = "border:1px solid var(--border);border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:6px;background:var(--surface)";
      const head = document.createElement("div"); head.style.cssText = "display:flex;align-items:baseline;gap:8px";
      const b = document.createElement("b"); b.textContent = c.name; b.style.fontSize = "13.5px";
      const rt = document.createElement("span"); rt.className = "mono"; rt.style.cssText = "font-size:10.5px;color:var(--faint)"; rt.textContent = c.runtime + (d.runtimes && d.runtimes[c.command] === false ? " \u00b7 " + c.command + " missing" : "");
      head.appendChild(b); head.appendChild(rt); card.appendChild(head);
      const desc = document.createElement("div"); desc.className = "skill-desc"; desc.textContent = c.description; card.appendChild(desc);
      const st = document.createElement("div"); st.className = "skill-tags mono"; st.textContent = c.status + (c.installed_as ? "  \u00b7  configured as " + c.installed_as : ""); card.appendChild(st);
      const cmdl = document.createElement("div"); cmdl.className = "mono"; cmdl.style.cssText = "font-size:10.5px;color:var(--faint);white-space:pre-wrap"; cmdl.textContent = c.command + " " + c.args.join(" "); card.appendChild(cmdl);
      const acts = document.createElement("div"); acts.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:2px";
      const addBtn = document.createElement("button"); addBtn.type = "button"; addBtn.className = c.installed_as ? "ghost-btn" : "solid-btn"; addBtn.textContent = c.installed_as ? "Add another" : "Add";
      acts.appendChild(addBtn);
      if (c.docs) { const a = document.createElement("a"); a.href = c.docs; a.target = "_blank"; a.rel = "noopener"; a.className = "linky"; a.textContent = "docs"; acts.appendChild(a); }
      card.appendChild(acts);
      const form = document.createElement("div"); form.hidden = true; form.style.cssText = "display:flex;flex-direction:column;gap:8px;margin-top:4px";
      const nameIn = inputEl("server name"); nameIn.value = c.installed_as ? c.id + "-2" : c.id;
      form.appendChild(formRow("name", nameIn));
      const pin = {}; for (const p of c.params || []) { const i = inputEl(p.help || ""); i.value = p.default || ""; pin[p.name] = i; form.appendChild(formRow(p.label, i)); }
      const ein = {}; for (const e of c.env || []) { const i = inputEl(e.label); i.value = e.default || ""; if (e.secret) i.type = "password"; ein[e.name] = i; form.appendChild(formRow(e.name + (e.secret ? "  \u00b7  stored in config.json" : ""), i)); }
      const row = document.createElement("div"); row.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap";
      const go1 = document.createElement("button"); go1.type = "button"; go1.className = "solid-btn"; go1.textContent = "Add";
      const go2 = document.createElement("button"); go2.type = "button"; go2.className = "ghost-btn"; go2.textContent = (c.install || []).length ? "Install locally + add" : "Add"; if (!(c.install || []).length) go2.hidden = true;
      const note = document.createElement("span"); note.className = "hint"; if (c.install_note) note.textContent = c.install_note;
      row.appendChild(go1); row.appendChild(go2); row.appendChild(note); form.appendChild(row); card.appendChild(form);
      addBtn.onclick = () => { form.hidden = !form.hidden; };
      const payload = () => { const params = {}; for (const k in pin) params[k] = pin[k].value; const env = {}; for (const k in ein) env[k] = ein[k].value; return { id: c.id, name: nameIn.value.trim(), params, env }; };
      go1.onclick = async () => { go1.disabled = true; try { const r = await mcpPost(Object.assign({ action: "catalog_add" }, payload())); const st2 = (r.servers || {})[nameIn.value.trim()] || {}; note.textContent = st2.state === "ready" ? "added \u00b7 " + st2.tools + " tools" : "added \u00b7 " + (st2.state || "") + (st2.error ? ": " + st2.error.slice(0, 120) : ""); renderMcp(); loadTools(); } catch (e) { note.textContent = e.message; go1.disabled = false; } };
      go2.onclick = () => installCatalog(payload(), c);
      grid.appendChild(card);
    }
    cat.appendChild(grid); left.appendChild(cat);

    // custom server
    const cust = document.createElement("div"); cust.className = "card"; cust.style.gap = "8px";
    cust.innerHTML = '<div class="skill-head"><div class="skill-id"><b>Custom server</b><span class="src mono">any stdio MCP server: a command and its arguments</span></div></div>';
    const cn = inputEl("name (letters, digits, - _)"), cc = inputEl("command, e.g. npx or uvx or python3"), ca = inputEl("arguments, space-separated, e.g. -y some-mcp-package --flag"), ce = textareaEl('environment as JSON (optional), e.g. {"API_KEY": "..."}', 3);
    [formRow("name", cn), formRow("command", cc), formRow("arguments", ca), formRow("environment", ce)].forEach((e) => cust.appendChild(e));
    const crow = document.createElement("div"); crow.style.cssText = "display:flex;gap:10px;align-items:center";
    const cb = document.createElement("button"); cb.type = "button"; cb.className = "solid-btn"; cb.textContent = "Add server"; const cnote = document.createElement("span"); cnote.className = "hint";
    cb.onclick = async () => { let env = {}; try { env = ce.value.trim() ? JSON.parse(ce.value) : {}; } catch (e) { cnote.textContent = "environment must be a JSON object"; return; } try { await mcpPost({ action: "add", name: cn.value.trim(), command: cc.value.trim(), args: ca.value.trim(), env }); cnote.textContent = "added"; renderMcp(); loadTools(); } catch (e) { cnote.textContent = e.message; } };
    crow.appendChild(cb); crow.appendChild(cnote); cust.appendChild(crow); left.appendChild(cust);
  }
  async function installCatalog(payload, c) {
    const pane = mcpPane("installing " + c.name + " \u00b7 " + payload.name);
    const pre = document.createElement("pre"); pre.className = "skill-body"; pre.style.whiteSpace = "pre-wrap"; pane.appendChild(pre);
    const append = (t) => { pre.textContent += t + "\n"; pre.scrollTop = pre.scrollHeight; };
    try {
      const resp = await fetch("/api/mcp", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.assign({ action: "install" }, payload)) });
      if (!resp.ok) { append("error: " + upstreamText(await resp.text())); return; }
      const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { done, value } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true }); const parts = buf.split("\n"); buf = parts.pop();
        for (const ln of parts) {
          if (!ln.startsWith("data:")) continue; let o; try { o = JSON.parse(ln.slice(5).trim()); } catch (e) { continue; }
          if (o.phase === "start") append("\u2014 " + (o.steps || []).length + " install step(s) on the console host \u2014");
          else if (o.phase === "done") { append("\u2014 " + (o.added ? "installed and added" : "stopped: exit " + o.exit) + " \u2014"); renderMcp(); loadTools(); }
          else if (o.error) append("error: " + o.error);
          else if (o.line != null) append(o.line);
        }
      }
    } catch (e) { append("error: " + e.message); }
  }

  /* ---------------- recipes (model deployment) ---------------- */
  let recipesCat = null;
  async function renderRecipes() {
    let d = { recipes: [], defaults: {}, litellm: {} };
    try { d = await (await fetch("/api/recipes")).json(); } catch (e) {}
    recipesCat = d;
    const left = $("recipes-left"); left.textContent = "";
    $("recipes-sub").textContent = d.recipes.length + " recipes \u00b7 litellm " + (d.litellm && d.litellm.configured ? "on " + d.litellm.ssh : "not configured");
    left.appendChild(await rackCard());
    for (const r of d.recipes) {
      const card = document.createElement("div");
      card.className = "card"; card.style.gap = "8px";
      const head = document.createElement("div"); head.className = "skill-head";
      head.innerHTML = '<div class="skill-id"><b></b><span class="src mono"></span></div>';
      head.querySelector("b").textContent = r.title;
      head.querySelector(".src").textContent = r.target === "ssh" ? "runs over ssh" : "manual steps";
      const btn = document.createElement("button"); btn.type = "button"; btn.className = "solid-btn"; btn.textContent = "Open";
      head.appendChild(btn);
      const desc = document.createElement("div"); desc.className = "skill-desc"; desc.textContent = r.summary;
      card.appendChild(head); card.appendChild(desc);
      const form = document.createElement("div"); form.hidden = true; form.style.cssText = "display:flex;flex-direction:column;gap:8px";
      const inputs = {};
      for (const p of r.params) {
        let el;
        if (p.kind === "select") {
          el = document.createElement("select"); el.className = "text-input";
          for (const c of p.choices) { const o = document.createElement("option"); o.value = c; o.textContent = c || "(none)"; el.appendChild(o); }
          el.value = p.default;
        } else {
          el = inputEl(p.help || "", "text-input");
          if (p.kind === "number") el.type = "number";
          el.value = (p.name === "host" && !p.default) ? (d.defaults.host || "") : p.default;
        }
        el.title = p.help || "";
        inputs[p.name] = el;
        const row = formRow(p.label + (p.help ? "  \u2014  " + p.help : ""), el);
        form.appendChild(row);
      }
      const acts = document.createElement("div"); acts.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
      const prev = document.createElement("button"); prev.type = "button"; prev.className = "ghost-btn"; prev.textContent = "Preview";
      const dep = document.createElement("button"); dep.type = "button"; dep.className = "solid-btn"; dep.textContent = r.target === "ssh" ? "Deploy over ssh" : "Show steps";
      const note = document.createElement("span"); note.className = "hint";
      acts.appendChild(prev); acts.appendChild(dep); acts.appendChild(note);
      form.appendChild(acts);
      card.appendChild(form);
      const params = () => { const o = {}; for (const k in inputs) o[k] = inputs[k].value; return o; };
      btn.onclick = () => { form.hidden = !form.hidden; btn.textContent = form.hidden ? "Open" : "Close"; };
      prev.onclick = () => previewRecipe(r, params(), note);
      dep.onclick = () => (r.target === "ssh" ? deployRecipe(r, params(), note) : previewRecipe(r, params(), note));
      left.appendChild(card);
    }
  }

  async function rackCard() {
    // the Sparks: driven by `rack` (dgx-spark-serve), never by a console-made docker command
    let d = { ok: false };
    try { d = await (await fetch("/api/rack")).json(); } catch (e) { d = { ok: false, error: e.message }; }
    const card = document.createElement("div");
    card.className = "card"; card.style.gap = "8px";
    const head = document.createElement("div"); head.className = "skill-head";
    head.innerHTML = '<div class="skill-id"><b>DGX Spark cluster \u00b7 rack</b><span class="src mono"></span></div>';
    head.querySelector(".src").textContent = d.enabled ? (d.host + ":" + d.dir + (d.serving ? "  \u00b7  serving " + d.serving : "")) : "not configured";
    card.appendChild(head);
    const desc = document.createElement("div"); desc.className = "skill-desc";
    desc.textContent = "The Sparks are served by rack: its recipes decide solo vs tensor-parallel and the gateway name, and `rack up` registers the model with litellm. Pick a recipe to read it, then Up. Down stops serving on both nodes.";
    card.appendChild(desc);
    if (!d.ok) {
      const n = document.createElement("div"); n.className = "hint";
      n.textContent = d.enabled === false ? "Set rack.ssh and rack.dir in config.json to drive the Sparks from here." : ("rack not reachable: " + (d.error || ""));
      card.appendChild(n); return card;
    }
    const row = document.createElement("div"); row.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const sel = document.createElement("select"); sel.className = "text-input"; sel.style.minWidth = "220px";
    for (const r of d.recipes || []) { const o = document.createElement("option"); o.value = r.name; o.textContent = r.name + "  \u00b7  " + r.mode + (r.model ? "  \u00b7  " + r.model : ""); if (d.serving && r.name === d.serving) { o.textContent += "  (serving now)"; o.selected = true; } sel.appendChild(o); }
    const show = document.createElement("button"); show.type = "button"; show.className = "ghost-btn"; show.textContent = "Read recipe";
    const up = document.createElement("button"); up.type = "button"; up.className = "solid-btn"; up.textContent = "rack up";
    const down = document.createElement("button"); down.type = "button"; down.className = "rate-btn bad"; down.textContent = "rack down";
    const st = document.createElement("button"); st.type = "button"; st.className = "ghost-btn"; st.textContent = "status";
    const logs = document.createElement("button"); logs.type = "button"; logs.className = "ghost-btn"; logs.textContent = "logs";
    const note = document.createElement("span"); note.className = "hint";
    [sel, show, up, down, st, logs, note].forEach((e) => row.appendChild(e));
    card.appendChild(row);
    const pre = document.createElement("pre"); pre.className = "skill-body"; pre.style.whiteSpace = "pre-wrap";
    pre.textContent = d.status_raw || "";
    const lab = document.createElement("div"); lab.className = "glabel"; lab.textContent = "rack status (cached 15 s)";
    card.appendChild(lab); card.appendChild(pre);
    show.onclick = async () => {
      note.textContent = "reading\u2026";
      try {
        const x = await (await fetch("/api/rack", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "show", recipe: sel.value }) })).json();
        note.textContent = "";
        const pane = recipePane("recipes/" + sel.value + ".env");
        preBlock(pane, "recipe", x.text || x.error || "");
      } catch (e) { note.textContent = "failed: " + e.message; }
    };
    const stream = async (action, recipe, confirmText) => {
      if (confirmText && !confirm(confirmText)) return;
      const pane = recipePane("rack " + action + (recipe ? " " + recipe : ""));
      const out = preBlock(pane, "output", "");
      const append = (t) => { out.textContent += t + "\n"; out.scrollTop = out.scrollHeight; };
      note.textContent = "running\u2026";
      try {
        const resp = await fetch("/api/rack", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, recipe }) });
        if (!resp.ok) { append("error: " + upstreamText(await resp.text())); note.textContent = ""; return; }
        const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const parts = buf.split("\n"); buf = parts.pop();
          for (const ln of parts) {
            if (!ln.startsWith("data:")) continue;
            let o; try { o = JSON.parse(ln.slice(5).trim()); } catch (e) { continue; }
            if (o.phase === "start") append("\u2014 " + o.cmd + " on " + o.host + " \u2014");
            else if (o.phase === "done") { append("\u2014 finished: " + (o.killed ? o.killed : "exit " + o.exit) + " \u2014"); note.textContent = o.exit === 0 ? "done" : "exit " + o.exit; if (state.screen === "recipes") setTimeout(renderRecipes, 1500); }
            else if (o.error) append("error: " + o.error);
            else if (o.line != null) append(o.line);
          }
        }
      } catch (e) { append("error: " + e.message); note.textContent = ""; }
    };
    up.onclick = () => stream("up", sel.value, "rack up " + sel.value + " \u2014 this stops whatever the Sparks serve now and launches this recipe. Continue?");
    down.onclick = () => stream("down", "", "rack down \u2014 stop serving on both Sparks?");
    st.onclick = () => stream("status", "");
    logs.onclick = () => stream("logs", "");
    return card;
  }

  function recipePane(title) {
    const pane = $("recipes-detail"); pane.textContent = "";
    const h = document.createElement("div"); h.className = "skill-head";
    h.innerHTML = '<div class="skill-id"><b></b></div>';
    h.querySelector("b").textContent = title;
    pane.appendChild(h);
    return pane;
  }

  function preBlock(pane, label, text) {
    const l = document.createElement("div"); l.className = "glabel"; l.textContent = label;
    const pre = document.createElement("pre"); pre.className = "skill-body"; pre.style.whiteSpace = "pre-wrap"; pre.textContent = text;
    pane.appendChild(l); pane.appendChild(pre);
    return pre;
  }

  async function previewRecipe(r, params, note) {
    note.textContent = "rendering\u2026";
    let d;
    try { d = await (await fetch("/api/recipes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "render", id: r.id, params }) })).json(); }
    catch (e) { note.textContent = "failed: " + e.message; return; }
    if (d.error) { note.textContent = d.error; return; }
    note.textContent = "";
    const pane = recipePane(r.title + " \u00b7 preview");
    for (const [name, text] of d.files) preBlock(pane, name, text);
    const lit = preBlock(pane, "litellm entry (model_list item)", d.litellm_entry);
    const row = document.createElement("div"); row.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const reg = document.createElement("button"); reg.type = "button"; reg.className = "solid-btn"; reg.textContent = "Register in litellm";
    const rn = document.createElement("span"); rn.className = "hint";
    reg.onclick = async () => {
      reg.disabled = true; rn.textContent = "registering\u2026";
      try {
        const x = await (await fetch("/api/recipes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "register", entry: d.litellm_entry }) })).json();
        rn.textContent = x.error ? x.error : (x.ok ? "registered \u00b7 " + (x.output || "").split("\n").slice(-1)[0] : "failed: " + (x.output || ""));
        if (x.ok) loadModels && loadModels();
      } catch (e) { rn.textContent = "failed: " + e.message; }
      reg.disabled = false;
    };
    row.appendChild(reg); row.appendChild(rn); pane.appendChild(row);
    if ((d.notes || []).length) {
      const n = document.createElement("div"); n.className = "glabel"; n.textContent = "notes"; pane.appendChild(n);
      const ul = document.createElement("ul"); ul.style.cssText = "margin:0;padding-left:18px;font-size:12.5px;color:var(--muted);display:flex;flex-direction:column;gap:6px";
      for (const t of d.notes) { const li = document.createElement("li"); li.style.whiteSpace = "pre-wrap"; li.textContent = t; ul.appendChild(li); }
      pane.appendChild(ul);
    }
  }

  async function deployRecipe(r, params, note) {
    if (!confirm("Run this recipe on " + (params.host || "?") + " now? Preview first if you have not read the script.")) return;
    const pane = recipePane(r.title + " \u00b7 deploying on " + params.host);
    const out = preBlock(pane, "log", "");
    const append = (t) => { out.textContent += t + "\n"; out.scrollTop = out.scrollHeight; };
    note.textContent = "running\u2026";
    try {
      const resp = await fetch("/api/recipes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "deploy", id: r.id, params }) });
      if (!resp.ok) { append("error: " + upstreamText(await resp.text())); note.textContent = ""; return; }
      const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n"); buf = parts.pop();
        for (const ln of parts) {
          if (!ln.startsWith("data:")) continue;
          let o; try { o = JSON.parse(ln.slice(5).trim()); } catch (e) { continue; }
          if (o.phase === "start") append("\u2014 running on " + o.host + " \u2014");
          else if (o.phase === "done") { append("\u2014 finished: " + (o.killed ? o.killed : "exit " + o.exit) + " \u2014"); note.textContent = o.exit === 0 ? "deployed \u00b7 now Preview \u2192 Register in litellm" : "exit " + o.exit; }
          else if (o.error) append("error: " + o.error);
          else if (o.line != null) append(o.line);
        }
      }
    } catch (e) { append("error: " + e.message); note.textContent = ""; }
  }

  /* ---------------- agents (harness) ---------------- */
  function workerSetupForm(d) {
    const box = document.createElement("div"); box.style.cssText = "display:flex;flex-direction:column;gap:8px;margin-top:6px";
    const ssh = inputEl("ssh host: an alias from ~/.ssh/config, or user@host"); ssh.value = d.host && d.host !== "this machine" ? d.host : "";
    const dir = inputEl("harness folder on the worker"); dir.value = d.dir || "~/bytebunker-harness";
    const py = inputEl("launcher"); py.value = "uv run";
    const row = document.createElement("div"); row.style.cssText = "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    const test = document.createElement("button"); test.type = "button"; test.className = "ghost-btn"; test.textContent = "Test";
    const go = document.createElement("button"); go.type = "button"; go.className = "solid-btn"; go.textContent = "Enable agents";
    const note = document.createElement("span"); note.className = "hint";
    const send = async (action) => {
      note.textContent = "checking over ssh\u2026"; test.disabled = go.disabled = true;
      try {
        const r = await (await fetch("/api/agents", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, ssh: ssh.value.trim(), dir: dir.value.trim(), python: py.value.trim() }) })).json();
        if (r.error) note.textContent = r.error;
        else note.textContent = Object.entries(r.checks || {}).map(([k, v]) => (v ? "\u2713 " : "\u2717 ") + k).join("   ") + (r.system ? "   \u00b7 " + r.system : "") + (r.enabled ? "   \u2014 enabled" : (action === "setup" && !r.ok ? "   \u2014 not enabled: fix the \u2717 first" : ""));
        if (r.enabled) setTimeout(renderAgents, 800);
      } catch (e) { note.textContent = e.message; }
      test.disabled = go.disabled = false;
    };
    test.onclick = () => send("test_worker");
    go.onclick = () => send("setup");
    row.appendChild(test); row.appendChild(go); row.appendChild(note);
    [formRow("worker", ssh), formRow("harness folder", dir), formRow("launcher", py), row].forEach((e) => box.appendChild(e));
    return box;
  }

  async function renderAgents() {
    let d = {};
    try { d = await (await fetch("/api/agents")).json(); } catch (e) {}
    $("agents-sub").textContent = d.enabled
      ? (d.mode === "ssh" ? "on " + d.host : "local (not isolated)")
      : "disabled";

    const topo = $("agents-topology");
    const planes = [
      ["Model plane", "the Sparks", "serve the LLM only — no agent code runs here"],
      ["Control plane", "this console", "launches goals and watches; runs no agent code"],
      ["Agent plane", d.host || "unset", d.isolated
        ? "a separate host; one rootless podman container per agent, no network unless granted" : "NOT a separate host — no isolation"],
    ];
    topo.innerHTML = "<div class='glabel'>Topology</div>";
    for (const [name, host, note] of planes) {
      const row = document.createElement("div");
      row.className = "plane-row";
      row.innerHTML = "<b></b><span class='mono'></span><span class='note'></span>";
      row.children[0].textContent = name;
      row.children[1].textContent = host;
      row.children[2].textContent = note;
      topo.appendChild(row);
    }
    if (d.enabled) {
      const off = document.createElement("button"); off.type = "button"; off.className = "linky"; off.style.marginTop = "6px";
      off.textContent = "disconnect this worker";
      off.onclick = async () => {
        if (!confirm("Turn agents off? The worker and its harness are not touched; you can reconnect any time.")) return;
        await fetch("/api/agents", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "disable" }) });
        renderAgents();
      };
      topo.appendChild(off);
    }
    if (!d.enabled) {
      const warn = document.createElement("div");
      warn.className = "msg-note";
      warn.textContent = "Agents are off. They run on a separate worker host reached over ssh (not a model server, " +
        "not this machine): the worker has the harness and podman or docker; this console launches goals there and watches.";
      topo.appendChild(warn);
      topo.appendChild(workerSetupForm(d));
    } else if (!d.isolated) {
      const warn = document.createElement("div");
      warn.className = "msg-note";
      warn.textContent = "Running locally on the console host: slaves are NOT sandboxed. " +
        "Set agents.ssh to a dedicated worker host for real isolation.";
      topo.appendChild(warn);
    }

    // master persona (only overwrite when not focused/editing)
    if (document.activeElement !== $("master-name")) $("master-name").value = d.master_name || "";
    if (document.activeElement !== $("master-instructions")) $("master-instructions").value = d.master_instructions || "";
    if (document.activeElement !== $("run-timeout") && d.run_timeout_s) $("run-timeout").value = Math.round(d.run_timeout_s / 60);
    // which engine each role uses; blank = whatever the worker's config.yaml says
    for (const [id, key] of [["model-master", "master_model"], ["model-thinking", "thinking_model"], ["model-slave", "slave_model"]]) {
      const sel = $(id);
      if (document.activeElement === sel) continue;
      sel.textContent = "";
      const o0 = document.createElement("option"); o0.value = ""; o0.textContent = "(worker default)"; sel.appendChild(o0);
      const seen = new Set([""]);
      for (const m of (state.models || []).filter((x) => !modelInfo(x).pinned).concat(d[key] ? [d[key]] : [])) { if (seen.has(m)) continue; seen.add(m); const o = document.createElement("option"); o.value = m; o.textContent = m; sel.appendChild(o); }
      sel.value = d[key] || "";
    }

    $("agent-run").disabled = !d.enabled || state.streaming;
    $("agent-launch-note").textContent = d.enabled ? "" : "connect a worker first";
    if (d.enabled && !state.streaming) attachAgentRun();

    renderAgentSlaves();

    const runs = $("agents-runs");
    runs.textContent = "";
    if (!(d.recent || []).length) {
      runs.innerHTML = "<div class='empty-state'><b>No runs yet</b><span>Launched goals show here. Click a run to read its full log. Each run is also written to the trace log for the training export.</span></div>";
    } else {
      for (const r of d.recent) {
        const card = document.createElement("div");
        card.className = "card run-card";
        card.style.gap = "6px"; card.style.cursor = "pointer";
        const top = document.createElement("div");
        top.className = "skill-head";
        top.innerHTML = "<b style='font-size:13px;font-weight:500'></b><span class='src mono'></span>";
        top.children[0].textContent = (r.goal || "").slice(0, 120) || "(no goal)";
        top.children[1].textContent = (r.killed ? r.killed : ("exit " + r.exit)) + " · " + (r.lines || 0) + " lines";
        const when = document.createElement("div");
        when.className = "skill-tags mono";
        when.textContent = (r.host || "") + " · " + relTime((r.ts || 0) * 1000) + (r.id ? "  ·  click to read" : "");
        card.appendChild(top); card.appendChild(when);
        if (r.id) card.onclick = () => openRunLog(r.id);
        runs.appendChild(card);
      }
    }
  }

  function detailPane(title, sub) {
    const box = $("agents-detail");
    box.textContent = "";
    const head = document.createElement("div"); head.className = "skill-head";
    head.innerHTML = "<div class='skill-id'><b></b><span class='src mono'></span></div><button class='linky' type='button'>close</button>";
    head.querySelector("b").textContent = title;
    head.querySelector(".src").textContent = sub || "";
    head.querySelector("button").onclick = () => {
      state.agentDetail = null;
      box.innerHTML = "<div class='empty-state'><b>Nothing open</b><span>Click a run or an agent on the left to read it here.</span></div>";
    };
    box.appendChild(head);
    return box;
  }

  async function openRunLog(id) {
    state.agentDetail = { kind: "run", id };
    const box = detailPane("run " + id, "loading\u2026");
    try {
      const d = await (await fetch("/api/agents/log?id=" + encodeURIComponent(id))).json();
      if (d.error) { box.appendChild(document.createTextNode("error: " + d.error)); return; }
      box.querySelector(".src").textContent = (d.host || "") + "  \u00b7  " + (d.killed ? d.killed : "exit " + d.exit) +
        "  \u00b7  " + (d.output || []).length + " lines";
      const g = document.createElement("div"); g.className = "skill-desc"; g.textContent = d.goal || ""; box.appendChild(g);
      const pre = document.createElement("pre"); pre.className = "skill-body";
      pre.textContent = (d.output || []).join("\n") || "(no output recorded)";
      box.appendChild(pre);
    } catch (e) { box.appendChild(document.createTextNode("error: " + e.message)); }
  }

  function fmtEvent(e) {
    const t = e.ts ? new Date(e.ts * 1000).toLocaleTimeString([], { hour12: false }) : "";
    let what = e.kind || "";
    if (e.kind === "llm_call") what = "step " + e.step + " \u2192 model (" + Math.round((e.prompt_chars || 0) / 1000) + "k chars)";
    else if (e.kind === "llm_reply") what = "model \u2192 " + ((e.tool_calls || []).length ? "calls " + e.tool_calls.join(", ") : (e.text || "").slice(0, 120));
    else if (e.kind === "tool") what = (e.ok ? "\u2713 " : "\u2717 ") + e.name + " " + (e.args || "").slice(0, 160);
    else if (e.kind === "verdict") what = "verifier: " + (e.passed ? "passed" : "failed \u2014 " + (e.failures || []).join("; ").slice(0, 200));
    else if (e.kind === "transcript_trim") what = "trimmed " + e.chars + " chars of old tool output";
    else if (e.kind === "bad_submission") what = "submission rejected: " + (e.error || "");
    else if (e.kind === "spawn") what = "spawned " + (e.slave_id || "") + " (" + (e.role || "") + ", " + (e.depth || "") + ")";
    else if (e.kind === "llm") what = "round " + (e.step != null ? e.step : "?") + " \u00b7 " + (e.elapsed_s != null ? e.elapsed_s + " s" : "") + " \u00b7 " + (e.completion_tokens || 0) + " tok out \u00b7 " + ((e.tools || []).length ? "called " + e.tools.join(", ") : "wrote prose: " + (e.text || "").slice(0, 200));
    else if (e.kind === "final_answer") what = "final answer" + (e.source ? " (" + e.source + ")" : "") + ": " + (e.text || "").slice(0, 300);
    else if (e.kind === "bail") what = "gave up: " + (e.reason || "");
    else if (e.kind === "panel") what = "skeptic panel on " + (e.slave_id || "") + ": " + (e.passed ? "passed" : (e.unverifiable ? "unverifiable" : "failed")) + ((e.failures || []).length ? " \u2014 " + e.failures.join("; ").slice(0, 200) : "");
    else if (e.kind === "llm_timeout_retry") what = "model call timed out; retry " + (e.attempt || "");
    else if (e.kind === "reply_cut") what = "reply cut off at the output cap (" + (e.completion_tokens || 0) + " tok); lost call: " + ((e.tools || []).join(", ") || "none");
    else if (e.kind === "spawn_result") what = "finished: " + (e.success ? "success" : (/unverified|partial result recovered/.test(e.error || "") ? "answered, unverified" : "failed")) + " \u00b7 " + (e.tokens || 0) + " tok" + (e.error ? " \u00b7 " + e.error : "");
    else { const rest = Object.assign({}, e); delete rest.ts; delete rest.kind; delete rest.actor; what = e.kind + " " + JSON.stringify(rest).slice(0, 160); }
    return t + "  " + what;
  }

  async function openSlaveDetail(id, quiet) {
    state.agentDetail = { kind: "slave", id };
    let d = {};
    try { d = await (await fetch("/api/agents/slave?id=" + encodeURIComponent(id))).json(); } catch (e) { d = { error: e.message }; }
    if (!state.agentDetail || state.agentDetail.id !== id) return;   // user opened something else meanwhile
    const box = detailPane(id, "");
    if (!d.ok || !d.found) { box.appendChild(document.createTextNode(d.error || "nothing recorded for " + id)); return; }
    const r = d.record || {};
    const add = (label, text, mono) => {
      if (text == null || text === "" || (Array.isArray(text) && !text.length)) return;
      const h = document.createElement("div"); h.className = "glabel"; h.textContent = label; box.appendChild(h);
      const b = document.createElement(mono ? "pre" : "div");
      b.className = mono ? "skill-body" : "skill-desc"; b.style.whiteSpace = "pre-wrap";
      b.textContent = Array.isArray(text) ? text.join("\n") : String(text);
      box.appendChild(b);
    };
    const head = box.querySelector(".skill-head");
    head.querySelector("b").textContent = (r.role || id) + " \u00b7 " + id;
    if (d.running) {
      const sb = document.createElement("button"); sb.type = "button"; sb.className = "rate-btn bad"; sb.textContent = "stop";
      sb.style.marginRight = "8px"; sb.onclick = () => stopSlave(id);
      head.insertBefore(sb, head.querySelector("button"));
    }
    head.querySelector(".src").textContent = [
      d.running ? "running" : (r.success === true ? "\u2713 success" : (r.success === false ? (r.answer ? "\u25b3 answered, unverified" : "\u2717 failed") : "")),
      r.depth, r.network ? "network" : (r.network === false ? "no-network" : null),
      r.tokens != null ? r.tokens + " tok" : null,
      r.confidence != null ? "confidence " + r.confidence : null,
      (r.skills || []).length ? "skills: " + r.skills.join(", ") : null,
    ].filter(Boolean).join("  \u00b7  ");
    add("Brief", d.brief || r.brief, true);
    add("Answer", r.answer, true);
    if ((r.evidence || []).length) add("Evidence", r.evidence.map((e, i) => (i + 1) + ". " + (e.claim || "") + "\n   \u2190 " + (e.source || "")), true);
    add("Unknowns", r.unknowns, true);
    add("Error", r.error, true);
    add("What it did \u00b7 " + (d.events || []).length + " events", (d.events || []).map(fmtEvent), true);
  }

  async function renderAgentSlaves() {
    const box = $("agents-slaves");
    let d = { ok: false };
    try { d = await (await fetch("/api/agents/slaves")).json(); } catch (e) {}
    box.textContent = "";
    // The master's own decisions, live: spawns, waits, results collected,
    // synthesis. This is the Sultan working, not just its minions.
    if ((d.master || []).length) {
      const mc = document.createElement("div");
      mc.className = "card live-card"; mc.style.gap = "6px";
      const mh = document.createElement("div"); mh.className = "skill-head";
      mh.innerHTML = "<div class='skill-id'><b></b><span class='src mono'></span></div><span class='src mono live-dot'>\u25c6 deciding</span>";
      mh.querySelector("b").textContent = (d.master_name || "Sultan") + " \u00b7 master";
      mh.querySelector(".src").textContent = d.master.length + " recent decisions";
      mc.appendChild(mh);
      const ev = document.createElement("div");
      ev.className = "skill-tags mono"; ev.style.whiteSpace = "pre-wrap"; ev.style.lineHeight = "1.55";
      ev.textContent = d.master.slice(-10).map((e) => {
        const t = e.ts ? new Date(e.ts * 1000).toLocaleTimeString([], { hour12: false }) : "";
        if (e.kind === "tool") return t + "  " + (e.name || "tool") + (e.args ? " " + String(e.args).slice(0, 110) : "") + (e.result ? " \u2192 " + String(e.result).slice(0, 90) : "");
        return fmtEvent(e);
      }).join("\n");
      mc.appendChild(ev);
      box.appendChild(mc);
    }
    // Running right now: each live slave streams its events to the worker,
    // and this is them, as they happen — the LLM steps, every tool call,
    // verdicts — so a working agent is never a black box.
    for (const l of (d.live || [])) {
      const card = document.createElement("div");
      card.className = "card live-card"; card.style.gap = "6px";
      const head = document.createElement("div");
      head.className = "skill-head";
      head.innerHTML = "<div class='skill-id'><b></b><span class='src mono'></span></div><span style='display:flex;gap:8px;align-items:center'><span class='src mono live-dot'>\u25cf running</span><button class='rate-btn bad' type='button' title='Stop this agent now'>stop</button></span>";
      head.querySelector("b").textContent = l.name || "slave";
      head.querySelector(".src").textContent = (l.events || []).length + " recent events";
      head.querySelector("button").onclick = (ev) => { ev.stopPropagation(); stopSlave(l.name); };
      card.appendChild(head);
      const ev = document.createElement("div");
      ev.className = "skill-tags mono"; ev.style.whiteSpace = "pre-wrap"; ev.style.lineHeight = "1.55";
      ev.textContent = (l.events || []).slice(-8).map(fmtEvent).join("\n");
      card.appendChild(ev);
      card.style.cursor = "pointer"; card.title = "Click to read everything this agent has done";
      card.onclick = () => openSlaveDetail(l.name);
      box.appendChild(card);
    }
    if (!d.ok || (!(d.slaves || []).length && !(d.live || []).length)) {
      box.innerHTML = "<div class='empty-state'><b>No agent activity yet</b><span>" +
        (d.error ? "Could not read the worker's trajectory (" + d.error + ")." :
         "Each slave the master spawns appears here — its role, whether it had network, whether it succeeded, and what it found.") + "</span></div>";
      return;
    }
    for (const sl of d.slaves) {
      const card = document.createElement("div");
      card.className = "card"; card.style.gap = "6px";
      const head = document.createElement("div");
      head.className = "skill-head";
      const ok = sl.success ? "✓ done" : (sl.answer ? "△ answered, unverified" : (sl.error ? "✗ failed" : "…"));
      head.innerHTML = "<div class='skill-id'><b></b><span class='src mono'></span></div><span class='src mono'></span>";
      head.querySelector("b").textContent = sl.role || "slave";
      head.querySelectorAll(".src")[0].textContent =
        (sl.network ? "network" : "no-network") + " · " + (sl.depth || "");
      head.querySelectorAll(".src")[1].textContent = ok + " · " + (sl.tokens || 0) + " tok";
      const brief = document.createElement("div");
      brief.className = "skill-desc";
      brief.textContent = sl.brief || "";
      card.appendChild(head); card.appendChild(brief);
      const detail = sl.success ? sl.answer : (sl.answer ? sl.answer + (sl.error ? "\n[unverified: " + sl.error + "]" : "") : sl.error);
      if (detail) {
        const dv = document.createElement("div");
        dv.className = "skill-tags mono"; dv.style.whiteSpace = "pre-wrap";
        dv.textContent = detail;
        card.appendChild(dv);
      }
      if (sl.id) {
        card.style.cursor = "pointer"; card.title = "Click to read the full answer, evidence and every step";
        card.onclick = () => openSlaveDetail(sl.id);
      }
      box.appendChild(card);
    }
  }

  async function stopSlave(id) {
    if (!confirm("Stop agent " + id + " now? The Sultan will see it end and decide what to do.")) return;
    try {
      const r = await (await fetch("/api/agents/slave", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "kill", id }) })).json();
      if (!r.ok) alert("could not stop " + id + ": " + (r.error || r.result || "unknown"));
    } catch (e) { alert("could not stop " + id + ": " + e.message); }
    renderAgentSlaves();
    if (state.agentDetail && state.agentDetail.id === id) openSlaveDetail(id, true);
  }

  async function saveMaster() {
    const btn = $("master-save"); btn.disabled = true;
    try {
      await fetch("/api/agents", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "config",
          master_name: $("master-name").value.trim(),
          master_instructions: $("master-instructions").value.trim(),
          run_timeout_s: Math.max(5, parseInt($("run-timeout").value, 10) || 180) * 60,
          master_model: $("model-master").value, thinking_model: $("model-thinking").value, slave_model: $("model-slave").value }),
      });
      $("master-note").textContent = "Saved. The master uses this on the next run.";
    } catch (e) { $("master-note").textContent = "save failed: " + e.message; }
    btn.disabled = false;
  }

  async function runAgent() {
    const goal = $("agent-goal").value.trim();
    if (!goal || state.streaming) return;
    await followAgentRun((signal) => fetch("/api/agents", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal }), signal,
    }));
  }

  // A goal runs on the server, not in this tab: closing or reloading the tab
  // leaves it going, and opening the Agents screen again re-attaches to it.
  async function attachAgentRun() {
    if (state.streaming) return;
    let d = { runs: [] };
    try { d = await (await fetch("/api/runs?kind=agents&limit=5")).json(); } catch (e) { return; }
    const live = (d.runs || []).find((r) => r.state === "running" || r.state === "queued");
    if (!live || state.streaming) return;
    await followAgentRun((signal) => fetch("/api/runs/" + encodeURIComponent(live.id) + "/events?format=data", { signal }),
                         "following the goal already running: " + (live.title || live.id));
  }

  async function followAgentRun(open, banner) {
    const out = $("agent-stream");
    out.hidden = false; out.textContent = "";
    state.streaming = true;
    state.agentRun = null;
    $("agent-run").disabled = true;
    $("agent-stop").hidden = false;
    $("agent-stop").disabled = false;
    const ctl = new AbortController();
    state.agentAbort = ctl;
    const append = (t) => { out.textContent += t + "\n"; out.scrollTop = out.scrollHeight; };
    if (banner) append("\u2014 " + banner + " \u2014");
    const liveTimer = setInterval(() => {
      if (state.screen !== "agents") return;
      renderAgentSlaves();
      if (state.agentDetail && state.agentDetail.kind === "slave") openSlaveDetail(state.agentDetail.id, true);
    }, 5000);
    let attachTo = null;
    try {
      const r = await open(ctl.signal);
      if (r.status === 409) {
        const x = await r.json().catch(() => ({}));
        if (x.run) attachTo = x.run; else append("error: " + (x.error || "busy"));
      } else if (!r.ok) { append("error: " + upstreamText(await r.text())); }
      else {
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const parts = buf.split("\n");
          buf = parts.pop();
          for (const ln of parts) {
            if (!ln.startsWith("data:")) continue;
            let o; try { o = JSON.parse(ln.slice(5).trim()); } catch (e) { continue; }
            if (o.run) { state.agentRun = o.run; continue; }
            if (o.phase === "start") append("— launching on " + o.host + " (" + o.mode + (o.isolated ? ", isolated" : ", NOT isolated") + ") —");
            else if (o.job) { append("\u2014 job filed: " + o.job.name + " \u2014"); }
            else if (o.phase === "done") append("— finished: " + (o.killed ? o.killed : "exit " + o.exit) + " —");
            else if (o.error) append("error: " + o.error);
            else if (o.line != null) append(o.line);
          }
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") append("error: " + e.message);
    } finally {
      clearInterval(liveTimer);
      state.streaming = false;
      state.agentAbort = null;
      state.agentRun = null;
      $("agent-run").disabled = false;
      $("agent-stop").hidden = true;
      if (!attachTo) renderAgents();
    }
    if (attachTo) {
      await followAgentRun((signal) => fetch("/api/runs/" + encodeURIComponent(attachTo) + "/events?format=data", { signal }),
                           "a goal is already running; one runs at a time. Following it");
    }
  }

  async function stopAgentRun() {
    // the goal runs on the server: stop it there; the stream ends when the worker has stopped it
    if (state.agentRun) {
      $("agent-stop").disabled = true;
      try {
        await fetch("/api/runs/" + encodeURIComponent(state.agentRun) + "/cancel",
                    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      } catch (e) { $("agent-stop").disabled = false; }
    } else if (state.agentAbort) state.agentAbort.abort();
  }

  function renderServers(status, cfg) {
    const box = $("tools-box");
    if (!box) return;
    box.textContent = "";
    const names = Object.keys(Object.assign({}, cfg, status));
    if (!names.length) {
      box.innerHTML = '<span class="hint">No MCP servers yet. Add one on the MCP screen — the model can then read and write through it.</span>';
      return;
    }
    for (const name of names) {
      const st = status[name] || { state: "unknown", tools: 0 };
      const on = !cfg[name] || cfg[name].enabled !== false;
      const r = document.createElement("div");
      r.className = "srv-row" + (on ? "" : " off");
      r.innerHTML = '<span class="d"></span><span class="n mono"></span><span class="s"></span>' +
        '<span class="acts"><button class="t" title="Enable/disable">\u25cf</button></span>';
      r.querySelector(".d").style.background =
        st.state === "ready" ? "var(--ok)" : st.state === "error" ? "var(--err)" : "var(--faint)";
      r.querySelector(".n").textContent = name;
      r.querySelector(".s").textContent =
        st.state === "ready" ? st.tools + " tools"
        : st.state === "error" ? "error" : st.state;
      if (st.error) r.querySelector(".s").title = st.error;
      r.querySelector(".t").onclick = () => mcpAdmin({ action: "toggle", name });
      box.appendChild(r);
    }
  }

  async function mcpAdmin(payload) {
    const box = $("tools-box");
    box.innerHTML = '<span class="hint">applying…</span>';
    try {
      const r = await fetch("/api/mcp", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (!r.ok) alert(d.error || "failed");
    } catch (e) {
      alert(e.message);
    }
    await loadTools();   // re-read status and tool schemas after any change
  }

  /* ---------------- cluster ---------------- */
  // One view of every machine. Nodes come from rack monitors: `rack monitor
  // up` on a head node prints a URL, `rack monitor token` its token, and the
  // console fetches /v1/cluster server-side. Each node is drawn as a rack
  // unit: a faceplate, four meters (GPU, memory, CPU, power and heat), the
  // engines it serves, its links and disk, and its containers. The monitor
  // keeps 15 minutes of samples, so the traces are full the moment the
  // screen opens. The agents worker's own GPU (read over ssh by the agents
  // poll) is drawn as one more unit of the same shape.
  const GiB = 1073741824;
  const fmtUp = (s) => {
    if (s == null) return "—";
    const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600);
    return d ? d + "d " + String(hr).padStart(2, "0") + "h" : hr + "h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
  };
  const fmtGiB = (b) => (b == null ? "—" : (b / GiB >= 100 ? Math.round(b / GiB) : (b / GiB).toFixed(1)) + " GiB");
  const fmtRate = (bps) => {
    if (bps == null) return "—";
    const units = ["B/s", "kB/s", "MB/s", "GB/s"];
    let v = bps, i = 0;
    while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    return (i === 0 || v >= 100 ? Math.round(v) : v.toFixed(1)) + " " + units[i];
  };
  const fmtCount = (n) => (n == null ? "—" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "k" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(Math.round(n)));
  const fmtPct = (v) => (v == null ? "—" : Math.round(v) + "%");
  const avg = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  const BAD_THROTTLE = ["power cap", "hw slowdown", "thermal", "hw thermal", "power brake"];
  state.rkOpen = {};          // node name -> containers list unfolded
  state.cluster = null;

  function rkEl(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // An area trace over the monitor's history. Gaps (null) are skipped, not
  // drawn as zero; `max` pins the scale (100 for percentages), else it fits
  // the data with headroom but never below `floor`, so an idle 10 W reads as
  // a low line rather than a full-height block.
  function rkSpark(values, max, cls, floor) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 100 32");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("class", "rk-spark" + (cls ? " " + cls : ""));
    const vals = (values || []).map((v) => (v == null || isNaN(v) ? null : +v));
    const real = vals.filter((v) => v != null);
    if (real.length < 2) { svg.classList.add("empty"); return svg; }
    const top = max != null && max > 0 ? max : Math.max(floor || 1e-9, Math.max(...real) * 1.25);
    const n = vals.length;
    let line = "", first = null, last = null;
    vals.forEach((v, i) => {
      if (v == null) return;
      const x = (i * 100) / (n - 1), y = 31 - Math.max(0, Math.min(1, v / top)) * 29;
      line += (line ? " L" : "M") + x.toFixed(2) + " " + y.toFixed(2);
      if (first == null) first = x;
      last = x;
    });
    const area = document.createElementNS(NS, "path");
    area.setAttribute("d", line + " L" + last.toFixed(2) + " 32 L" + first.toFixed(2) + " 32 Z");
    area.setAttribute("class", "fill");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", line);
    path.setAttribute("class", "line");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(area, path);
    return svg;
  }

  function rkMeter(label, big) {
    const m = rkEl("div", "rk-meter");
    m.appendChild(rkEl("div", "rk-k", label));
    m.appendChild(rkEl("div", "rk-big", big));
    return m;
  }

  function rkEngine(e, hist) {
    const row = rkEl("div", "rk-engine");
    const head = rkEl("div", "rk-ehead");
    const names = { vllm: "vLLM", sglang: "SGLang", "llama.cpp": "llama.cpp", ollama: "Ollama", openai: "OpenAI API" };
    head.appendChild(rkEl("span", "rk-kind", names[e.kind] || e.kind || "engine"));
    head.appendChild(rkEl("span", "rk-model", (e.models || []).join(", ") || "no model listed"));
    const meta = [];
    if (e.port != null) meta.push(":" + e.port);
    if (e.max_model_len) meta.push(Math.round(e.max_model_len / 1024) + "k context");
    head.appendChild(rkEl("span", "rk-emeta", meta.join(" · ")));
    const busy = (e.running || 0) + (e.waiting || 0);
    const stateTxt = e.ok === false ? "not answering" : busy ? "serving" : "idle";
    head.appendChild(rkEl("span", "rk-state " + (e.ok === false ? "err" : busy ? "busy" : "idle"), stateTxt));
    row.appendChild(head);
    if (e.ok === false) { row.appendChild(rkEl("div", "rk-sub", e.error || "")); return row; }
    if (e.kind === "ollama") { row.appendChild(rkEl("div", "rk-sub", e.loaded_bytes ? fmtGiB(e.loaded_bytes) + " loaded" : "nothing loaded")); return row; }
    if (e.kind === "openai") return row;
    const grid = rkEl("div", "rk-egrid");
    const cell = (label, big, sub, extra) => {
      const c = rkEl("div", "rk-cell");
      c.appendChild(rkEl("div", "rk-k", label));
      c.appendChild(rkEl("div", "rk-mid", big));
      if (extra) c.appendChild(extra);
      if (sub) c.appendChild(rkEl("div", "rk-sub", sub));
      grid.appendChild(c);
    };
    cell("Output", e.gen_tps != null ? e.gen_tps.toFixed(1) + " tok/s" : "—",
      "prompt " + (e.prompt_tps != null ? fmtCount(e.prompt_tps) + " tok/s" : "—"), rkSpark(hist.gen_tps, null, null, 10));
    cell("Requests", (e.running || 0) + " running", (e.waiting || 0) + " waiting" + (e.e2e_avg != null ? " · " + e.e2e_avg.toFixed(1) + " s each" : ""));
    const kv = rkEl("div", "rk-stack");
    const kb = rkEl("b", "kv");
    kb.style.width = Math.min(100, e.kv_pct || 0) + "%";
    kv.appendChild(kb);
    cell("KV cache", fmtPct(e.kv_pct), e.kv_tokens ? "of " + fmtCount(e.kv_tokens) + " tokens" : "", kv);
    cell("First token", e.ttft_p50 != null ? e.ttft_p50.toFixed(2) + " s" : "—",
      e.ttft_p95 != null ? "p95 " + e.ttft_p95.toFixed(2) + " s, last minute" : "no requests in the last minute");
    // one decode step can emit several tokens (speculative decoding: MTP,
    // EAGLE), so the step's latency is not a per-token latency
    const tps = e.tokens_per_step;
    cell("Decode step", e.itl_ms != null ? Math.round(e.itl_ms) + " ms" : "—",
      tps ? tps.toFixed(1) + " tokens a step" + (e.spec_accept_pct != null ? " · " + Math.round(e.spec_accept_pct) + "% of drafts kept" : "") + (e.spec_window === "start" ? " (since start)" : "")
        : e.itl_ms != null ? "one token a step" : "");
    const hit = e.prefix_hit_pct != null ? e.prefix_hit_pct : (e.prefix_queries ? (100 * (e.prefix_hits || 0)) / e.prefix_queries : null);
    cell("Prefix cache", fmtPct(hit), e.prefix_hit_pct != null ? "hits, last minute" : e.prefix_queries ? "hits since start" : "");
    cell("Served", fmtCount(e.requests_ok) + " requests",
      (e.preemptions ? fmtCount(e.preemptions) + " preempted · " : "") + fmtCount(e.gen_total) + " tokens out");
    row.appendChild(grid);
    return row;
  }

  function rackUnit(n) {
    const sys = n.system || {};
    const gpus = n.gpus || [];
    const hist = n.history || {};
    const down = n.ok === false;
    const age = n.sampled_at ? Date.now() / 1000 - n.sampled_at : 0;
    const u = rkEl("article", "rk-unit" + (down ? " down" : ""));

    // the faceplate: LED, name, role, what the box is, where the numbers come from
    const face = rkEl("div", "rk-face");
    face.appendChild(rkEl("span", "rk-led" + (down ? " err" : age > 20 ? " warn" : "")));
    const id = rkEl("div", "rk-id");
    const l1 = rkEl("div", "rk-line");
    l1.appendChild(rkEl("span", "rk-name", n.name || "?"));
    if (n.role) l1.appendChild(rkEl("span", "rk-role", n.role));
    const bad = gpus.flatMap((g) => g.throttle || []).filter((r) => BAD_THROTTLE.includes(r));
    if (bad.length) l1.appendChild(rkEl("span", "rk-role bad", "throttled: " + [...new Set(bad)].join(", ")));
    id.appendChild(l1);
    const gname = gpus.length ? gpus[0].name.replace(/^NVIDIA (GeForce )?/, "") + (gpus.length > 1 ? " ×" + gpus.length : "") : null;
    const spec = [sys.product, gname, sys.cpu_model,
      n.mem && n.mem.total ? fmtGiB(n.mem.total) + (sys.unified_memory ? " unified" : " RAM") : null].filter(Boolean).join(" · ");
    id.appendChild(rkEl("div", "rk-spec", down ? (n.error || "not answering") : spec));
    face.appendChild(id);
    face.appendChild(rkEl("div", "rk-vents"));
    const meta = rkEl("div", "rk-meta");
    meta.appendChild(rkEl("span", null, [sys.hostname && sys.hostname !== n.name ? sys.hostname : null, sys.os, sys.uptime_s ? "up " + fmtUp(sys.uptime_s) : null].filter(Boolean).join(" · ")));
    meta.appendChild(rkEl("span", "rk-src", [n.monitor ? "via " + n.monitor : null, n.latency_ms != null ? n.latency_ms + " ms" : null,
      age > 20 ? Math.round(age) + " s old" : null].filter(Boolean).join(" · ")));
    face.appendChild(meta);
    u.appendChild(face);
    if (down) return u;

    const body = rkEl("div", "rk-body");
    // GPU
    {
      const util = avg(gpus.map((g) => g.util).filter((v) => v != null));
      const g = gpus[0] || {};
      const m = rkMeter("GPU", gpus.length ? fmtPct(util) : "—");
      m.appendChild(rkSpark(hist.gpu, 100));
      const sub = [];
      if (g.sm_clock != null) sub.push(g.sm_clock + (g.sm_clock_max ? " / " + g.sm_clock_max : "") + " MHz");
      if (g.pstate) sub.push(g.pstate);
      if (g.mem_total) sub.push("VRAM " + fmtGiB(g.mem_used) + " / " + fmtGiB(g.mem_total));
      m.appendChild(rkEl("div", "rk-sub", gpus.length ? sub.join(" · ") : (n.gpu_error || "no GPU on this node")));
      body.appendChild(m);
    }
    // memory: on unified memory, what the GPU processes hold is part of this bar
    {
      const mem = n.mem || {};
      const m = rkMeter(sys.unified_memory ? "Unified memory" : "Memory", mem.total ? fmtGiB(mem.used) + " / " + fmtGiB(mem.total) : "—");
      const bar = rkEl("div", "rk-stack");
      if (mem.total) {
        const gp = Math.min(mem.gpu_procs || 0, mem.used || 0);
        const seg = (cls, v, title) => { const b = rkEl("b", cls); b.style.width = ((100 * v) / mem.total).toFixed(2) + "%"; b.title = title; bar.appendChild(b); };
        if (gp) seg("gpu", gp, "GPU processes " + fmtGiB(gp));
        seg("used", Math.max(0, (mem.used || 0) - gp), "everything else in use");
      }
      m.appendChild(bar);
      m.appendChild(rkSpark(hist.mem, mem.total ? mem.total / 1e9 : null, "mem"));
      const sub = [];
      if (mem.gpu_procs) sub.push("GPU processes " + fmtGiB(mem.gpu_procs));
      if (mem.available != null) sub.push(fmtGiB(mem.available) + " free");
      if (mem.swap_used) sub.push("swap " + fmtGiB(mem.swap_used));
      m.appendChild(rkEl("div", "rk-sub", sub.join(" · ")));
      body.appendChild(m);
    }
    // CPU: every core as its own bar
    {
      const c = n.cpu || {};
      const m = rkMeter("CPU" + (sys.cores ? " · " + sys.cores + " cores" : ""), fmtPct(c.pct));
      const cores = c.cores_pct || [];
      if (cores.length) {
        const strip = rkEl("div", "rk-cores");
        strip.style.gridTemplateColumns = "repeat(" + cores.length + ",1fr)";
        cores.forEach((v, i) => { const b = rkEl("i"); b.style.height = Math.max(5, v || 0) + "%"; b.title = "core " + i + ": " + fmtPct(v); strip.appendChild(b); });
        m.appendChild(strip);
      } else {
        m.appendChild(rkSpark(hist.cpu, 100));
      }
      m.appendChild(rkEl("div", "rk-sub", c.load ? "load " + c.load.map((x) => x.toFixed(2)).join(" · ") : ""));
      body.appendChild(m);
    }
    // power and heat
    {
      const watts = gpus.map((g) => g.power_w).filter((v) => v != null);
      const m = rkMeter("Power · heat", watts.length ? Math.round(watts.reduce((a, b) => a + b, 0)) + " W" : "—");
      m.appendChild(rkSpark(hist.power, null, "warm", 60));
      const temps = rkEl("div", "rk-temps");
      const t = n.temps || {};
      for (const [k, label] of [["gpu", "GPU"], ["soc", "SoC"], ["cpu", "CPU"], ["nvme", "NVMe"], ["nic", "NIC"]]) {
        if (t[k] == null) continue;
        const s = rkEl("span", t[k] >= 90 ? "hot" : t[k] >= 80 ? "warm" : "");
        s.appendChild(rkEl("b", null, label));
        s.appendChild(document.createTextNode(" " + Math.round(t[k]) + "°C"));
        temps.appendChild(s);
      }
      m.appendChild(temps);
      body.appendChild(m);
    }
    u.appendChild(body);

    for (const e of n.engines || []) u.appendChild(rkEngine(e, hist));
    if (n.engineNote) u.appendChild(rkEl("div", "rk-row rk-note", n.engineNote));

    // links and disk
    const links = (n.net || []).filter((x) => x.up !== false);
    const disks = n.disks || [];
    if (links.length || disks.length) {
      const row = rkEl("div", "rk-row");
      for (const x of links) {
        const c = rkEl("span", "rk-chip " + x.kind);
        c.title = x.iface + (x.ip ? "  " + x.ip : "");
        c.appendChild(rkEl("b", null, x.kind));
        const speed = x.speed_mbps ? (x.speed_mbps >= 1000 ? x.speed_mbps / 1000 + "G" : x.speed_mbps + "M") + " " : "";
        c.appendChild(document.createTextNode(" " + speed + "↓ " + fmtRate(x.rx_bps) + "  ↑ " + fmtRate(x.tx_bps)));
        row.appendChild(c);
      }
      for (const d of disks) {
        const c = rkEl("span", "rk-chip disk");
        c.appendChild(rkEl("b", null, "disk " + d.mount));
        c.appendChild(document.createTextNode(" " + (d.used / 1e12).toFixed(1) + " / " + (d.total / 1e12).toFixed(1) + " TB "));
        const bar = rkEl("span", "rk-mini");
        const fill = rkEl("i");
        fill.style.width = ((100 * d.used) / d.total).toFixed(1) + "%";
        bar.appendChild(fill);
        c.appendChild(bar);
        row.appendChild(c);
      }
      u.appendChild(row);
    }

    // containers, folded; the ones holding GPU memory are marked
    const ctrs = n.containers || [];
    if (ctrs.length || n.containers_error) {
      const det = rkEl("details", "rk-ctrs");
      const running = ctrs.filter((c) => c.state === "running");
      const gpuHolders = [...new Set(gpus.flatMap((g) => (g.procs || []).map((p) => p.container)).filter(Boolean))];
      det.appendChild(rkEl("summary", null, n.containers_error ? "containers: " + n.containers_error
        : running.length + " containers running" + (ctrs.length > running.length ? " · " + (ctrs.length - running.length) + " stopped" : "")
          + (gpuHolders.length ? " · on the GPU: " + gpuHolders.join(", ") : "")));
      if (ctrs.length) {
        const tbl = rkEl("div", "rk-table");
        for (const c of ctrs) {
          const r = rkEl("div", "rk-tr" + (c.state === "running" ? "" : " off"));
          r.appendChild(rkEl("span", "d"));
          r.appendChild(rkEl("span", "nm", c.name + (gpuHolders.includes(c.name) ? "  ◆ GPU" : "")));
          r.appendChild(rkEl("span", "im", c.image || ""));
          r.appendChild(rkEl("span", "st", c.status || c.state || ""));
          r.appendChild(rkEl("span", "num", c.cpu_cores != null ? c.cpu_cores.toFixed(2) + " cores" : ""));
          r.appendChild(rkEl("span", "num", c.mem != null ? fmtGiB(c.mem) : ""));
          tbl.appendChild(r);
        }
        det.appendChild(tbl);
      }
      det.open = !!state.rkOpen[n.name];
      det.addEventListener("toggle", () => { state.rkOpen[n.name] = det.open; });
      u.appendChild(det);
    }
    return u;
  }

  // The agents worker's GPU and model, from the agents poll, in the shape of
  // a monitor node so it draws as one more unit.
  function workerUnit() {
    const fm = state.fastModel, w = state.workerStats || {};
    if (!fm || !(fm.gpu || fm.up)) return null;
    const g = fm.gpu;
    return {
      name: (fm.label || "agent worker").split(" on ").slice(-1)[0],
      role: "agents", ok: true, monitor: "the agents poll (ssh)",
      system: { hostname: w.host || null },
      gpus: g ? [{ name: g.name, util: g.util, temp: g.temp, mem_used: g.mem_used_mb * 1048576, mem_total: g.mem_total_mb * 1048576, procs: [] }] : [],
      mem: w.mem_total_gb ? { total: w.mem_total_gb * GiB, used: w.mem_used_gb * GiB } : null,
      cpu: w.load1 != null ? { load: [w.load1] } : null,
      temps: g ? { gpu: g.temp } : {},
      engines: fm.up ? [{ kind: "vllm", models: fm.model ? [fm.model] : [], ok: true, running: fm.running, waiting: fm.waiting,
        kv_pct: fm.kv_pct, gen_tps: fm.gen_tps, prompt_tps: fm.prompt_tps, requests_ok: fm.requests_total, gen_total: fm.gen_total,
        prefix_hits: fm.prefix_hits, prefix_queries: fm.prefix_queries }] : [],
      history: { gpu: state.hist.wgpu, gen_tps: state.hist.wgen },
      engineNote: fm.up ? null : "model server not reachable from the worker" + (fm.error ? ": " + fm.error : ""),
    };
  }

  function rkSummary(nodes) {
    const box = $("cluster-summary");
    box.textContent = "";
    box.hidden = !nodes.length;
    if (!nodes.length) return;
    const up = nodes.filter((n) => n.ok !== false);
    const gpus = up.flatMap((n) => n.gpus || []);
    const util = avg(gpus.map((g) => g.util).filter((v) => v != null));
    const memT = up.reduce((a, n) => a + ((n.mem || {}).total || 0), 0);
    const memU = up.reduce((a, n) => a + ((n.mem || {}).used || 0), 0);
    const watts = gpus.map((g) => g.power_w).filter((v) => v != null);
    const engines = up.flatMap((n) => n.engines || []).filter((e) => e.ok !== false);
    const tps = engines.map((e) => e.gen_tps).filter((v) => v != null);
    const fabric = up.flatMap((n) => n.net || []).filter((x) => x.kind === "fabric");
    const cell = (label, v, s) => {
      const c = rkEl("div");
      c.appendChild(rkEl("span", "rk-k", label));
      c.appendChild(rkEl("span", "v", v));
      c.appendChild(rkEl("span", "s", s));
      box.appendChild(c);
    };
    cell("Nodes", up.length + " / " + nodes.length, up.length === nodes.length ? "all reporting" : nodes.length - up.length + " not answering");
    cell("GPU", fmtPct(util), gpus.length + " GPU" + (gpus.length === 1 ? "" : "s") + ", average");
    cell("Memory", memT ? Math.round(memU / GiB) + "/" + Math.round(memT / GiB) + " GiB" : "—", memT ? fmtPct((100 * memU) / memT) + " in use" : "");
    cell("Power", watts.length ? Math.round(watts.reduce((a, b) => a + b, 0)) + " W" : "—", "GPUs, now");
    cell("Output", tps.length ? tps.reduce((a, b) => a + b, 0).toFixed(1) + " tok/s" : "—",
      engines.length ? engines.length + " engine" + (engines.length === 1 ? "" : "s") + " · " + engines.reduce((a, e) => a + (e.running || 0), 0) + " running" : "no engine");
    if (fabric.length) cell("Fabric", fmtRate(fabric.reduce((a, x) => a + (x.rx_bps || 0) + (x.tx_bps || 0), 0) / 2), "between nodes");
  }

  function renderClusterSide(d) {
    const side = $("side-nodes");
    side.textContent = "";
    const nodes = ((d && d.nodes) || []).slice();
    const w = workerUnit();
    if (w) nodes.push(w);
    let healthy = 0;
    for (const n of nodes) {
      const util = avg((n.gpus || []).map((g) => g.util).filter((v) => v != null));
      if (n.ok !== false) healthy++;
      const mini = rkEl("div", "node-mini");
      const row = rkEl("div", "row");
      const nm = rkEl("span", "mono", n.name);
      nm.style.color = "var(--muted)";
      row.appendChild(nm);
      row.appendChild(rkEl("b", "mono", n.ok === false ? "down" : util != null ? Math.round(util) + "%" : "—"));
      mini.appendChild(row);
      const bar = rkEl("div", "bar");
      const f = rkEl("div");
      f.style.width = (util || 0) + "%";
      bar.appendChild(f);
      mini.appendChild(bar);
      side.appendChild(mini);
    }
    const hEl = $("side-health");
    if (!d || !d.configured) hEl.innerHTML = '<span class="dot" style="background:var(--faint);animation:none"></span>no monitor';
    else if (healthy === nodes.length && healthy) hEl.innerHTML = '<span class="dot"></span>healthy';
    else hEl.innerHTML = '<span class="dot err"></span>' + healthy + "/" + nodes.length;
  }
  $("side-nodes").onclick = () => go("cluster");

  function renderCluster(d) {
    const nodes = (d && d.nodes) || [];
    const mons = (d && d.monitors) || [];
    const configured = d ? d.configured : 0;
    $("cluster-sub").textContent = configured
      ? nodes.length + " node" + (nodes.length === 1 ? "" : "s") + " · " + mons.map((m) => m.cluster && m.cluster !== m.name ? m.name + " (" + m.cluster + ")" : m.name).join(", ")
      : "every node, GPU, engine and container, from one rack monitor";
    const failing = mons.filter((m) => !m.ok);
    $("cluster-poll").innerHTML = !configured ? '<span class="dot" style="background:var(--faint);animation:none"></span>no monitor'
      : failing.length ? '<span class="dot err"></span>' + failing.length + " monitor" + (failing.length === 1 ? "" : "s") + " failing"
      : '<span class="dot"></span>live · 5 s';
    const note = $("cluster-note");
    note.textContent = "";
    for (const m of failing) {
      const e = rkEl("div", "rk-alert");
      e.appendChild(rkEl("b", null, m.name));
      e.appendChild(document.createTextNode(" (" + m.url + "): " + (m.error || "not answering")));
      note.appendChild(e);
    }
    const all = nodes.slice();
    const w = workerUnit();
    if (w) all.push(w);
    rkSummary(all);
    const units = $("node-cards");
    units.textContent = "";
    for (const n of all) units.appendChild(rackUnit(n));
    if (!configured && !state.monPanelShown) { state.monPanelShown = true; renderMonitorsPanel(true); }
  }

  // ---- monitors: add, find, toggle, remove ----
  async function monPost(payload) {
    const r = await fetch("/api/monitors", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    let d = {};
    try { d = await r.json(); } catch (e) {}
    return Object.assign({ status: r.status }, d);
  }

  async function renderMonitorsPanel(open) {
    const box = $("cluster-monitors");
    if (open === false) { box.hidden = true; return; }
    box.hidden = false;
    let list = [];
    try { list = (await (await fetch("/api/monitors")).json()).monitors || []; } catch (e) {}
    box.textContent = "";
    const card = rkEl("div", "card rk-mons");
    const head = rkEl("div", "skill-head");
    const title = rkEl("div", "skill-id");
    title.appendChild(rkEl("b", null, "Monitors"));
    title.appendChild(rkEl("span", "src mono", list.length ? list.length + " configured" : "none yet"));
    head.appendChild(title);
    const close = rkEl("button", "ghost-btn", "close");
    close.type = "button";
    close.onclick = () => { box.hidden = true; };
    head.appendChild(close);
    card.appendChild(head);

    for (const m of list) {
      const row = rkEl("div", "rk-monrow");
      const st = m.status || {};
      row.appendChild(rkEl("span", "rk-led" + (!m.enabled ? " off" : st.ok ? "" : st.ok === false ? " err" : " warn")));
      const id = rkEl("div", "rk-monid");
      id.appendChild(rkEl("b", "mono", m.name));
      id.appendChild(rkEl("span", "mono", m.url + (m.has_token ? "" : " · no token")));
      id.appendChild(rkEl("span", "rk-sub", !m.enabled ? "disabled" : st.ok ? (st.nodes || 0) + " nodes · " + st.ms + " ms · rack-monitor " + (st.version || "") : st.error || "checking…"));
      row.appendChild(id);
      const act = async (payload) => { const r = await monPost(payload); if (r.error) alert(r.error); renderMonitorsPanel(true); pollCluster(true); };
      const tok = rkEl("button", "ghost-btn", "token");
      tok.type = "button";
      tok.onclick = () => {
        const f = rkEl("form", "rk-inline");
        const inp = rkEl("input");
        inp.type = "password"; inp.placeholder = "new token (rack monitor token)"; inp.autocomplete = "off";
        const ok = rkEl("button", "solid-btn", "save");
        f.append(inp, ok);
        f.onsubmit = (ev) => { ev.preventDefault(); act({ action: "update", name: m.name, token: inp.value.trim() }); };
        id.appendChild(f);
        inp.focus();
      };
      const tg = rkEl("button", "ghost-btn", m.enabled ? "disable" : "enable");
      tg.type = "button";
      tg.onclick = () => act({ action: "toggle", name: m.name });
      const rm = rkEl("button", "ghost-btn", "remove");
      rm.type = "button";
      rm.onclick = () => { if (confirm("Remove monitor " + m.name + "? The rack keeps running; this console stops reading it.")) act({ action: "remove", name: m.name }); };
      row.append(tok, tg, rm);
      card.appendChild(row);
    }

    // add
    const form = rkEl("form", "rk-add");
    const url = rkEl("input", "mono");
    url.placeholder = "http://100.90.164.11:9177";
    url.autocomplete = "off"; url.spellcheck = false;
    const tok = rkEl("input", "mono");
    tok.type = "password"; tok.placeholder = "token"; tok.autocomplete = "off";
    const name = rkEl("input");
    name.placeholder = "name (optional)";
    const add = rkEl("button", "solid-btn", "Connect");
    const find = rkEl("button", "ghost-btn", "Find on this network");
    find.type = "button";
    const lbl = (t, inp) => { const l = rkEl("label"); l.appendChild(rkEl("span", "rk-k", t)); l.appendChild(inp); return l; };
    form.append(lbl("Endpoint", url), lbl("Token", tok), lbl("Name", name));
    const btns = rkEl("div", "rk-btns");
    btns.append(add, find);
    form.appendChild(btns);
    const hint = rkEl("div", "hint");
    hint.innerHTML = "On the head node, <code>rack monitor up</code> starts a monitor on every node and prints the endpoint; <code>rack monitor token</code> prints the token. Pasting <code>http://rack:TOKEN@host:9177</code> into Endpoint fills both. Any other Linux box: <code>python3 rackmon.py serve</code> from dgx-spark-serve's <code>monitor/</code>.";
    const out = rkEl("div", "rk-out");
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      out.textContent = "checking " + (url.value.trim() || "…");
      add.disabled = true;
      const payload = { action: "add", url: url.value.trim(), token: tok.value.trim(), name: name.value.trim() };
      const r = await monPost(payload);
      add.disabled = false;
      if (r.ok) {
        // the units appearing below is the confirmation; the panel steps aside
        state.cfg.monitors = (r.monitors || []).length;
        tok.value = "";
        $("cluster-monitors").hidden = true;
        pollCluster(true);
        return;
      }
      out.textContent = "";
      out.appendChild(rkEl("span", "rk-bad", (r.stage === "auth" ? "Reached it, but " : "") + (r.error || "failed")));
      if (r.status === 422) {
        const force = rkEl("button", "ghost-btn", "save anyway");
        force.type = "button";
        force.onclick = async () => { const f = await monPost(Object.assign({}, payload, { force: true })); if (f.ok) { renderMonitorsPanel(true); pollCluster(true); } else { out.textContent = f.error || "failed"; } };
        out.appendChild(force);
      }
    };
    find.onclick = async () => {
      out.textContent = "asking this machine, known hosts and tailnet peers on :9177…";
      find.disabled = true;
      const r = await monPost({ action: "discover" });
      find.disabled = false;
      out.textContent = "";
      const found = r.found || [];
      if (!found.length) {
        out.textContent = "No monitor answered (" + (r.hosts_probed || 0) + " hosts, " + (r.ms || 0) + " ms). Run rack monitor up on the head node. On macOS, allow ByteBunker under System Settings › Privacy & Security › Local Network.";
        return;
      }
      for (const f of found) {
        const line = rkEl("div", "rk-found");
        line.appendChild(rkEl("b", "mono", f.name || f.host));
        line.appendChild(rkEl("span", "mono", f.url));
        line.appendChild(rkEl("span", "rk-sub", (f.cluster ? f.cluster + " · " : "") + (f.role || "") + (f.peers ? " · covers " + (f.peers + 1) + " nodes" : "")));
        if (f.configured) line.appendChild(rkEl("span", "rk-sub", "already added"));
        else {
          const use = rkEl("button", "ghost-btn", "use");
          use.type = "button";
          use.onclick = () => { url.value = f.url; if (!name.value) name.value = f.cluster || ""; tok.focus(); };
          line.appendChild(use);
        }
        out.appendChild(line);
      }
    };
    card.append(form, hint, out);
    box.appendChild(card);
    if (!list.length) url.focus();
  }
  $("cluster-monitors-btn").onclick = () => renderMonitorsPanel($("cluster-monitors").hidden);

  let agentsObsTick = 0, clusterBusy = false;
  async function pollCluster(force) {
    if (clusterBusy && !force) return;
    clusterBusy = true;
    try {
      if (force || (agentsObsTick++ % 2) === 0) pollAgentsObs();      // every 10 s alongside
      const onScreen = state.screen === "cluster";
      let d = null;
      try { d = await (await fetch("/api/cluster?history=" + (onScreen ? 150 : 0))).json(); } catch (e) { d = null; }
      if (d && !onScreen && state.cluster && state.cluster.nodes) {
        // keep the last full history for when the screen opens again
        for (const n of d.nodes || []) { const old = state.cluster.nodes.find((x) => x.name === n.name); if (old && old.history) n.history = old.history; }
      }
      state.cluster = d;
      renderClusterSide(d);
      if (onScreen) renderCluster(d);
    } finally {
      clusterBusy = false;
    }
  }

  async function pollAgentsObs() {
    const box = $("agents-obs");
    if (!box) return;
    let d = {};
    try { d = await (await fetch("/api/agents/stats")).json(); } catch (e) { d = { ok: false, error: e.message }; }
    state.fastModel = d.fast_model || null;
    state.workerStats = d.ok ? d : null;
    const fm = state.fastModel;
    if (fm && fm.gpu) { (state.hist.wgpu = state.hist.wgpu || []).push(fm.gpu.util); state.hist.wgpu = state.hist.wgpu.slice(-150); }
    if (fm && fm.up) { (state.hist.wgen = state.hist.wgen || []).push(fm.gen_tps); state.hist.wgen = state.hist.wgen.slice(-150); }
    box.textContent = "";
    if (!d.enabled && !d.ok) { renderClusterSide(state.cluster); return; }
    const card = document.createElement("div");
    card.className = "card";
    const title = document.createElement("div");
    title.style.cssText = "display:flex;align-items:baseline;gap:10px";
    title.innerHTML = "<span class='mono' style='font-size:15px;font-weight:600'>agents</span><span class='spec' style='font-size:11.5px;color:var(--faint)'></span><div style='flex:1'></div><span class='pill'><span class='d'></span><span class='pt'></span></span>";
    title.querySelector(".spec").textContent = d.host ? (d.host + (d.isolated ? " · isolated worker" : " · NOT isolated")) : "";
    title.querySelector(".pt").textContent = d.ok ? ((d.containers || []).length ? "working" : "idle") : "unreachable";
    if (!d.ok) title.querySelector(".pill").style.opacity = ".6";
    card.appendChild(title);
    if (!d.ok) {
      const n = document.createElement("div"); n.className = "hint"; n.textContent = "Worker not reachable: " + (d.error || ""); card.appendChild(n);
      box.appendChild(card);
      renderClusterSide(state.cluster);
      return;
    }
    const day = d.day || {};
    const stat = (label, value, sub) => {
      const w = document.createElement("div");
      w.innerHTML = "<div style='font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)'></div><div class='mono' style='font-size:20px;font-weight:600;line-height:1.2'></div><div style='font-size:11.5px;color:var(--muted)'></div>";
      w.children[0].textContent = label; w.children[1].textContent = value; w.children[2].textContent = sub || "";
      return w;
    };
    const grid = document.createElement("div");
    grid.style.cssText = "display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:14px";
    grid.appendChild(stat("containers", String((d.containers || []).length), (d.containers || []).slice(0, 4).join(", ") || "none running"));
    grid.appendChild(stat("masters", d.masters == null ? "—" : String(d.masters), d.masters ? "goal in progress" : "no goal running"));
    grid.appendChild(stat("live agents", String((d.live || []).length), (d.live || []).slice(0, 3).join(", ") || "—"));
    grid.appendChild(stat("spawns · 24h", String(day.n || 0), (day.ok || 0) + " ok · " + (day.unverified || 0) + " unverified · " + (day.failed || 0) + " failed"));
    grid.appendChild(stat("tokens · 24h", (day.tokens || 0) >= 1000 ? Math.round((day.tokens || 0) / 1000) + "k" : String(day.tokens || 0), Object.keys(day.roles || {}).length ? Object.entries(day.roles).map(([r, n]) => n + " " + r).join(", ").slice(0, 60) : ""));
    card.appendChild(grid);
    box.appendChild(card);
    renderClusterSide(state.cluster);
    if (state.screen === "cluster") renderCluster(state.cluster);
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
    const ag = u.agents && !u.agents.error ? u.agents : null;
    const fmtTok = (n) => (n || 0) >= 1e6 ? ((n || 0) / 1e6).toFixed(1) + " M" : (n || 0).toLocaleString();
    if (ag) mk("Agent tokens", fmtTok(ag.total), (ag.goals || 0) + " goals \u00b7 " + (ag.slaves || 0) + " agents \u00b7 " + (ag.master_rounds || 0) + " master rounds, 14 days");
    mk("Frontier-API equivalent", "$" + (u.frontier_saved_usd || 0).toFixed(2), ag ? "chat $" + (u.chat_saved_usd || 0).toFixed(2) + " + agents $" + (ag.frontier_saved_usd || 0).toFixed(2) : "not spent, at configured rates");
    box.appendChild(cards);
    if (u.agents && u.agents.error) {
      const n = document.createElement("div"); n.className = "hint"; n.textContent = "Agent usage not available: " + u.agents.error; box.appendChild(n);
    }

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

    // the agent plane: per-day stack (agents / Sultan / panels), then by role and by model
    if (ag && ag.days && Object.keys(ag.days).length) {
      const dk = Object.keys(ag.days).sort();
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = '<div style="display:flex;align-items:baseline;gap:10px"><span style="font-size:13.5px;font-weight:600">Agent tokens per day</span><span style="font-size:11.5px;color:var(--faint)">prompt + completion \u00b7 agents, the Sultan, skeptic panels</span></div><div class="bars"></div>';
      const bars = card.querySelector(".bars");
      const tot = (d) => (d.slaves || 0) + (d.master || 0) + (d.panels || 0);
      const mx = Math.max(...dk.map((k) => tot(ag.days[k]))) || 1;
      dk.forEach((k, i) => {
        const d = ag.days[k];
        const w = document.createElement("div");
        w.innerHTML = '<div class="b" style="display:flex;flex-direction:column-reverse;overflow:hidden"></div><small class="mono"></small>';
        const b = w.querySelector(".b");
        b.style.height = (18 + (tot(d) / mx) * 82) + "%";
        b.style.background = "transparent";
        const seg = (v, color, label) => { if (!v) return; const s2 = document.createElement("div"); s2.style.cssText = "flex:0 0 " + (v / tot(d) * 100) + "%;background:" + color; s2.title = label + ": " + v.toLocaleString(); b.appendChild(s2); };
        seg(d.slaves, "var(--accent)", "agents"); seg(d.master, "var(--warn)", "the Sultan"); seg(d.panels, "var(--faint)", "skeptic panels");
        if (i === dk.length - 1) b.classList.add("hot");
        w.querySelector("small").textContent = k;
        b.title = tot(d).toLocaleString() + " tokens";
        bars.appendChild(w);
      });
      const legend = document.createElement("div"); legend.className = "hint"; legend.style.marginTop = "6px";
      legend.textContent = "agents " + fmtTok(ag.slave_tokens) + " \u00b7 the Sultan " + fmtTok((ag.master_prompt || 0) + (ag.master_completion || 0)) + " (" + fmtTok(ag.master_completion) + " out) \u00b7 panels " + fmtTok(ag.panel_tokens) + " over " + (ag.panels || 0) + " panels";
      card.appendChild(legend);
      box.appendChild(card);
      const mix = (title, obj) => {
        const ks = Object.keys(obj || {}).sort((a, b) => obj[b] - obj[a]);
        if (!ks.length) return;
        const c = document.createElement("div"); c.className = "card";
        c.innerHTML = '<span style="font-size:13.5px;font-weight:600"></span>'; c.firstChild.textContent = title;
        const top = obj[ks[0]] || 1;
        for (const k of ks) {
          const r = document.createElement("div"); r.className = "mix-row";
          r.innerHTML = '<span class="n mono"></span><div class="bar8"><div></div></div><span class="v mono"></span>';
          r.querySelector(".n").textContent = k; r.querySelector(".bar8>div").style.width = (obj[k] / top) * 100 + "%"; r.querySelector(".v").textContent = fmtTok(obj[k]);
          c.appendChild(r);
        }
        box.appendChild(c);
      };
      mix("Agents by role", ag.by_role);
      mix("Agents by model", ag.by_model);
      const note = document.createElement("div"); note.className = "hint";
      note.textContent = "By model is exact for runs since the spawn ledger started recording the model (30 Sep 2026); older agent records are attributed to the current default slave model.";
      box.appendChild(note);
    }

    const gws = u.by_gateway || {};
    const gkeys = Object.keys(gws).sort((a, b) => gws[b] - gws[a]);
    if (gkeys.length) {
      const card = document.createElement("div"); card.className = "card";
      card.innerHTML = '<span style="font-size:13.5px;font-weight:600">By gateway</span><span class="hint">chat tokens in + out, plus agent tokens routed by model</span>';
      const mx = gws[gkeys[0]] || 1;
      for (const k of gkeys) {
        const r = document.createElement("div"); r.className = "mix-row";
        r.innerHTML = '<span class="n mono"></span><div class="bar8"><div></div></div><span class="v mono"></span>';
        r.querySelector(".n").textContent = k; r.querySelector(".bar8>div").style.width = (gws[k] / mx) * 100 + "%";
        r.querySelector(".v").textContent = gws[k] >= 1e6 ? (gws[k] / 1e6).toFixed(1) + " M" : gws[k].toLocaleString();
        card.appendChild(r);
      }
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
    if (!keys.length && !ag) {
      box.innerHTML += '<div class="empty-state"><b>Nothing logged yet</b><span>Every completion through the Playground lands in this ledger.</span></div>';
    }
  }

  $("export-all").onclick = () => openOut("/api/export");
  $("agent-run").onclick = runAgent;
  $("agent-stop").onclick = stopAgentRun;
  $("master-save").onclick = saveMaster;
  $("export-good").onclick = () => openOut("/api/export?rated=up");
  $("new-chat").onclick = newChat;
  $("new-chat-2").onclick = newChat;
  document.addEventListener("keydown", (e) => {
    // cmd/ctrl-shift-O: new chat, the convention every chat app shares
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") {
      e.preventDefault(); newChat();
    }
  });

  /* ---------------- panel links into screens ---------------- */
  $("mcp-manage").onclick = () => go("mcp");
  $("models-to-recipes").onclick = () => go("recipes");

  /* ---------------- netcheck (Cluster: "is it local?") ---------------- */
  // The verdict comes from rack net on the head node — an in-container /proc
  // audit of every serving engine's established connections. The server
  // caches it for 5 minutes; the button forces a fresh run.
  async function netcheckRun(fresh) {
    const btn = $("netcheck-btn"), out = $("netcheck-out"), ver = $("netcheck-verdict");
    btn.disabled = true; btn.textContent = "auditing…";
    ver.textContent = ""; ver.className = "netcheck-verdict mono";
    try {
      const r = await fetch("/api/netcheck" + (fresh ? "?fresh=1" : ""));
      const d = await r.json();
      if (!d.ok && d.error) throw new Error(d.error);
      const lines = d.lines || [];
      out.hidden = !lines.length;
      out.textContent = lines.filter((l) => !/^VERDICT|verdict:/i.test(l.trim())).join("\n");
      const bad = lines.some((l) => l.includes("INTERNET"));
      ver.classList.add(bad ? "bad" : "good");
      ver.textContent = (bad
        ? "✗ internet traffic present — see audit above"
        : "✓ all connections local — nothing leaves the rack")
        + "  ·  checked " + new Date((d.at || 0) * 1000).toLocaleTimeString();
    } catch (e) {
      out.hidden = true;
      ver.classList.add("bad");
      ver.textContent = "audit failed: " + (e.message || e);
    } finally {
      btn.disabled = false; btn.textContent = "Verify now";
    }
  }
  $("netcheck-btn").onclick = () => netcheckRun(true);

  /* ---------------- video studio (MiniMax-H3 via /api/video) ---------------- */
  // A different animal from the chat upstream: multipart form in, an async job
  // out, an MP4 with its own soundtrack at the end. Jobs live on the engine,
  // not in this tab — everything re-renders from GET /api/video, and cards
  // update their mutable bits in place so a poll never tears down a playing
  // <video>. FL2VA partition serves t2va + fl2va; ref2va needs the Ref2VA
  // partition racked instead, and the engine says so honestly if it isn't.
  const VID = {
    files: [],          // File objects attached to the composer
    prompts: {},        // job id -> prompt, best-effort (jobs born in this tab)
    cards: new Map(),   // job id -> live DOM refs
    steps: 50,
    timer: null,        // 5 s status poll
    tick: null,         // 1 s elapsed repaint
  };
  const VID_DONE = new Set(["completed", "succeeded"]);
  const VID_FAIL = new Set(["failed", "error", "cancelled"]);
  const VID_TASK_HINT = {
    t2va: "prompt only — the model invents the shot and its soundtrack",
    fl2va: "one image; the clip starts on that exact frame (size follows it, 768p short edge)",
    ref2va: "needs the Ref2VA partition serving — one image + one audio, or 1–3 videos",
  };

  const vidKind = (f) =>
    /^image\//.test(f.type) ? "image" : /^video\//.test(f.type) ? "video" :
    /^audio\//.test(f.type) ? "audio" : "other";
  const fmtMB = (n) => (n / 1048576).toFixed(1) + " MB";
  const fmtElapsed = (s) => s >= 3600
    ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
    : s >= 60 ? `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s` : `${Math.floor(s)}s`;
  const asDataURL = (f) => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result); r.onerror = () => rej(r.error);
    r.readAsDataURL(f);
  });

  function vidTaskUI() {
    const t = $("vid-task").value;
    $("vid-task-hint").textContent = VID_TASK_HINT[t];
    $("vid-ref-row").hidden = t === "t2va";
    $("vid-attach-label").textContent =
      t === "fl2va" ? "Attach first frame" : "Attach image · audio · video";
    $("vid-file").accept = t === "fl2va" ? "image/*" : "image/*,audio/*,video/*";
    $("vid-file").multiple = t === "ref2va";
    $("vid-size").disabled = t === "fl2va";
    if (t === "fl2va") VID.files = VID.files.filter((f) => vidKind(f) === "image").slice(0, 1);
    if (t === "t2va") VID.files = [];
    vidChips();
  }

  function vidChips() {
    const box = $("vid-chips");
    box.textContent = "";
    VID.files.forEach((f, i) => {
      const c = document.createElement("span"); c.className = "vfile-chip";
      const n = document.createElement("span"); n.className = "fn"; n.textContent = f.name;
      const s = document.createElement("small"); s.textContent = fmtMB(f.size);
      const x = document.createElement("button"); x.textContent = "×"; x.title = "remove";
      x.onclick = () => { VID.files.splice(i, 1); vidChips(); };
      c.append(n, s, x);
      box.appendChild(c);
    });
  }

  function vidProblem() {
    const task = $("vid-task").value;
    const prompt = $("vid-prompt").value.trim();
    if (!prompt) return "write a prompt first";
    if (prompt.length > 7000) return "prompt is over the 7,000-character limit";
    const dur = parseFloat($("vid-dur").value);
    if (!(dur >= 4 && dur <= 15)) return "duration must be 4–15 seconds";
    const kinds = VID.files.map(vidKind);
    if (VID.files.reduce((a, f) => a + f.size, 0) > 60 * 1048576)
      return "references exceed 60 MB — the engine caps the whole request at 64";
    if (task === "fl2va" && kinds.join() !== "image")
      return "first-frame mode needs exactly one image";
    if (task === "ref2va") {
      const img = kinds.filter((k) => k === "image").length;
      const aud = kinds.filter((k) => k === "audio").length;
      const vid = kinds.filter((k) => k === "video").length;
      if (!((vid >= 1 && vid <= 3 && !img && !aud) || (img === 1 && aud === 1 && !vid)))
        return "reference mode takes one image + one audio, or 1–3 videos";
    }
    return null;
  }

  async function vidGenerate() {
    const bad = vidProblem();
    $("vid-err").textContent = bad || "";
    if (bad) return;
    const task = $("vid-task").value;
    const fd = new FormData();
    fd.append("prompt", $("vid-prompt").value.trim());
    fd.append("fps", "24");
    fd.append("num_inference_steps", String(VID.steps));
    fd.append("flow_shift", "12");
    const seed = $("vid-seed").value.trim();
    if (seed) fd.append("seed", seed);
    if (task !== "fl2va") {
      const wh = $("vid-size").value.split("x");
      fd.append("width", wh[0]); fd.append("height", wh[1]);
    }
    fd.append("extra_params", JSON.stringify(
      { task, duration: parseFloat($("vid-dur").value), audio_flow_shift: 3.0 }));
    const vids = VID.files.filter((f) => vidKind(f) === "video");
    if (task === "fl2va") fd.append("input_reference", VID.files[0]);
    else if (task === "ref2va" && vids.length)
      vids.forEach((f) => fd.append("input_references", f));
    else if (task === "ref2va") {
      fd.append("input_reference", VID.files.find((f) => vidKind(f) === "image"));
      fd.append("audio_reference", JSON.stringify(
        { audio_url: await asDataURL(VID.files.find((f) => vidKind(f) === "audio")) }));
    }
    $("vid-go").disabled = true;
    try {
      const r = await fetch("/api/video", { method: "POST", body: fd });
      const body = await r.json();
      if (!r.ok || body.error) throw new Error(body.error || "HTTP " + r.status);
      VID.prompts[body.id] = $("vid-prompt").value.trim();
      $("vid-prompt").value = "";
      VID.files = []; vidChips();
      await vidRefresh();
    } catch (e) {
      $("vid-err").textContent = "submit failed: " + (e.message || e);
    } finally {
      $("vid-go").disabled = false;
    }
  }

  function vidLive(ok) {
    const box = $("video-live");
    while (box.childNodes.length > 1) box.removeChild(box.lastChild);
    box.appendChild(document.createTextNode(ok ? "engine ready" : "offline"));
    box.style.color = ok ? "var(--ok)" : "var(--muted)";
  }

  function vidEmpty(title, body) {
    $("vid-empty").hidden = false;
    $("vid-empty-title").textContent = title;
    $("vid-empty-body").textContent = body;
  }

  function vidTime(c) {
    if (!c.created) { c.time.textContent = ""; return; }
    c.time.textContent = c.pending
      ? fmtElapsed(Math.max(0, Date.now() / 1000 - c.created)) + " elapsed"
      : "started " + new Date(c.created * 1000).toLocaleTimeString();
  }

  function vidCard(j) {
    let c = VID.cards.get(j.id);
    if (!c) {
      const root = document.createElement("div"); root.className = "vjob";
      const head = document.createElement("div"); head.className = "vjob-head";
      const pill = document.createElement("span"); pill.className = "vpill";
      const id = document.createElement("span"); id.className = "mono";
      id.style.cssText = "font-size:12px;color:var(--faint)"; id.textContent = j.id;
      const time = document.createElement("span"); time.className = "mono";
      time.style.cssText = "font-size:12px;color:var(--muted)";
      const grow = document.createElement("span"); grow.className = "grow";
      const del = document.createElement("button"); del.className = "ghost-btn";
      del.textContent = "Delete";
      del.onclick = async () => {
        del.disabled = true;
        try { await fetch("/api/video/" + j.id, { method: "DELETE" }); } catch (e) {}
        vidRefresh();
      };
      head.append(pill, id, time, grow, del);
      const prompt = document.createElement("div"); prompt.className = "vjob-prompt";
      prompt.textContent = VID.prompts[j.id] || j.prompt || "";
      const media = document.createElement("div");
      media.style.cssText = "display:flex;flex-direction:column;gap:8px;align-items:flex-start";
      const err = document.createElement("div"); err.className = "verr";
      root.append(head, prompt, media, err);
      c = { root, pill, time, media, err, created: j.created_at, done: false, pending: true };
      VID.cards.set(j.id, c);
      $("vid-jobs").appendChild(root);
    }
    c.created = j.created_at || c.created;
    const st = (j.status || "queued").toLowerCase();
    const done = VID_DONE.has(st), fail = VID_FAIL.has(st);
    c.pending = !done && !fail;
    c.pill.textContent = st;
    c.pill.className = "vpill " + (done ? "done" : fail ? "fail" : "run");
    c.err.textContent = fail
      ? (typeof j.error === "string" ? j.error : (j.error && j.error.message) || "generation failed")
      : "";
    if (done && !c.done) {
      c.done = true;
      const v = document.createElement("video");
      v.controls = true; v.preload = "metadata";
      v.src = "/api/video/" + j.id + "/content";
      const dl = document.createElement("a"); dl.className = "ghost-btn";
      dl.style.textDecoration = "none";
      dl.textContent = "Download MP4"; dl.href = v.src; dl.download = j.id + ".mp4";
      c.media.append(v, dl);
    }
    vidTime(c);
  }

  async function vidRefresh() {
    let jobs;
    try {
      const r = await fetch("/api/video");
      const body = await r.json();
      if (!r.ok || body.error) throw new Error(body.error || "HTTP " + r.status);
      jobs = body.data || body.jobs || (Array.isArray(body) ? body : []);
    } catch (e) {
      vidLive(false);
      if (!VID.cards.size) vidEmpty("Engine offline",
        "Nothing answers on the H3 tunnel. On spark-1: rack down, then rack up h3 — " +
        "loading takes a while, and the first render pays a torch.compile warmup on top.");
      vidPollControl([]);
      return;
    }
    vidLive(true);
    jobs.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    if (!jobs.length) vidEmpty("No renders yet",
      "Describe a shot above and hit Generate. The job runs on the engine, so you can " +
      "close this tab and come back — it will still be here.");
    else $("vid-empty").hidden = true;
    for (const j of jobs) vidCard(j);
    for (const [id, c] of VID.cards)
      if (!jobs.some((j) => j.id === id)) { c.root.remove(); VID.cards.delete(id); }
    // settle order without touching nodes already in place — moving a node
    // reloads its <video>, so only genuinely out-of-place cards move
    let prev = null;
    for (const j of jobs) {
      const node = VID.cards.get(j.id).root;
      const want = prev ? prev.nextSibling : $("vid-jobs").firstChild;
      if (node !== want) $("vid-jobs").insertBefore(node, want);
      prev = node;
    }
    vidPollControl(jobs);
  }

  function vidPollControl(jobs) {
    const pending = jobs.some((j) => {
      const s = (j.status || "queued").toLowerCase();
      return !VID_DONE.has(s) && !VID_FAIL.has(s);
    });
    const want = pending || state.screen === "video";
    if (want && !VID.timer) {
      VID.timer = setInterval(vidRefresh, 5000);
      VID.tick = setInterval(() => {
        for (const c of VID.cards.values()) if (c.pending) vidTime(c);
      }, 1000);
    } else if (!want && VID.timer) {
      clearInterval(VID.timer); clearInterval(VID.tick);
      VID.timer = VID.tick = null;
    }
  }

  $("vid-task").onchange = vidTaskUI;
  $("vid-attach").onclick = () => $("vid-file").click();
  $("vid-file").onchange = (e) => {
    const task = $("vid-task").value;
    for (const f of e.target.files) {
      const kind = vidKind(f);
      if (kind === "other") continue;
      if (task === "fl2va") { if (kind === "image") VID.files = [f]; }
      else VID.files.push(f);
    }
    e.target.value = "";
    vidChips();
  };
  $("vid-steps").oninput = (e) => {
    VID.steps = +e.target.value;
    $("vid-steps-val").textContent = e.target.value;
  };
  $("vid-go").onclick = vidGenerate;
  vidTaskUI();

  /* ---------------- boot ---------------- */
  (async () => {
    try { state.cfg = await (await fetch("/api/config")).json(); } catch (e) {}
    $("who-user").textContent = state.cfg.identity.user || "local";
    $("who-host").textContent = state.cfg.identity.host || "";
    $("avatar").textContent = (state.cfg.identity.user || "B")[0].toUpperCase();
    updateFirstRun();
    if (state.cfg.video) { $("nav-video").hidden = false; vidRefresh(); }
    if (state.cfg.netcheck) $("netcheck-card").hidden = false;
    await loadModels();
    if (state.cfg.mcp) loadTools();
    loadSkills();
    pollCluster();
    setInterval(pollCluster, 5000);
    startLive();
    const deep = location.hash.slice(1);
    if (deep && screens.includes(deep) && deep !== state.screen) go(deep);
    setInterval(() => { if (!state.models.length) loadModels(); }, 15000);
  })();
})();
