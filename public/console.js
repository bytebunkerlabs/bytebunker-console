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
    cfg: { nodes: [], identity: {}, upstream: "", telemetry: false },
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
    agentAbort: null,       // AbortController for a live agent run
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
  const screens = ["playground", "sessions", "video", "skills", "plugins", "agents", "models", "tuning", "batch", "cluster", "usage"];
  function go(s) {
    state.screen = s;
    screens.forEach((id) => {
      $("screen-" + id).classList.toggle("on", id === s);
      document.querySelector(`[data-nav="${id}"]`).classList.toggle("on", id === s);
    });
    $("panel").classList.toggle("on", s === "playground" && panelWanted);
    if (s === "sessions") renderSessions();
    if (s === "skills") renderSkills();
    if (s === "plugins") renderPlugins();
    if (s === "agents") renderAgents();
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
  function estimatePrompt(msgs, tools, model) {
    const chars = JSON.stringify({ m: msgs, t: tools || null }).length;
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
    for (const t of (m.toolUse || [])) {
      const d = document.createElement("div");
      d.className = "part-tool" + (t.error ? " err" : "");
      d.innerHTML = '<div class="thead"><span class="tname mono"></span><span class="tstate mono"></span></div><pre class="targs"></pre><pre class="tres"></pre>';
      d.querySelector(".tname").textContent = t.name;
      d.querySelector(".tstate").textContent = t.result === "running…" ? "running…" : (t.error ? "error" : "ok");
      d.querySelector(".targs").textContent = t.args;
      d.querySelector(".tres").textContent = String(t.result).slice(0, 4000);
      bot.appendChild(d);
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
        msgs.push({ role: "user", content: m.content });
      }
    }
    return msgs;
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
    if (!text || !text.trim() || state.streaming || !state.model) return;
    text = text.trim();
    if (/^\/compress\b/i.test(text)) { await compress("manual"); return; }
    if (P.autoCompress) {
      // Fold older turns away BEFORE this one joins the transcript, so what
      // goes out fits with room to answer. A loop, because one round only
      // summarizes as much as fits in the summarizer's own window.
      for (let round = 0; round < 3; round++) {
        const c0 = capsFor(state.model);
        const probe = buildMsgs(state.messages.concat([{ role: "user", content: text }]), c0);
        const est = estimatePrompt(probe, toolDefs(c0), state.model);
        if (est.tokens <= compressLimit(c0.ctx || 131072)) break;
        const did = await compress("auto", est.tokens);
        if (did === "aborted") { $("input").value = text; return; }
        if (!did) break;
      }
    }
    if (!state.session) state.session = "s-" + Date.now().toString(36);
    const user = { role: "user", content: text };
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
          bot.toolUse[bot.toolUse.length - 1].result = out.content;
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
    const first = state.messages.find((m) => m.role === "user" && (m.content || "").trim());
    const toks = state.messages.reduce((a, m) => a + (m.content || "").length, 0);
    fetch("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: state.session,
        title: first ? first.content.slice(0, 80) : "untitled",
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
      r.onclick = () => {
        state.session = s.id;
        state.messages = (s.messages || []).map((m) => ({ ...m }));
        state.ctxUsed = null;
        state.calib = null;
        setActiveSkills(s.activeSkills || []);
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
    // The server publishes what each model actually supports; without this the
    // client guesses, and a wrong guess is a 400 that reads like a crash.
    state.caps = {};
    for (const m of (data.data || [])) if (m.caps) state.caps[m.id] = m.caps;
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

  async function renderSkills() {
    await loadSkills();
    const box = $("skills-box");
    $("skills-sub").textContent = state.skills.length
      ? state.skills.length + " available · " + state.activeSkills.length + " attached"
      : "none installed";
    box.textContent = "";
    if (!state.skills.length) {
      box.innerHTML = '<div class="empty-state"><b>No skills installed</b><span>' +
        'Drop a <span class="mono">&lt;name&gt;/SKILL.md</span> into the skills folder, point ' +
        '<span class="mono">skills_dirs</span> at the agent harness to share its packs, or enable a plugin that ships skills.</span></div>';
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
      card.appendChild(view); card.appendChild(pre);
      box.appendChild(card);
    }
  }

  /* ---------------- plugins ---------------- */
  async function renderPlugins() {
    let d = { plugins: [], warnings: [] };
    try { d = await (await fetch("/api/plugins")).json(); } catch (e) {}
    state.plugins = d.plugins || [];
    const box = $("plugins-box");
    const on = state.plugins.filter((p) => p.enabled).length;
    $("plugins-sub").textContent = state.plugins.length
      ? state.plugins.length + " found · " + on + " enabled" : "none found";
    box.textContent = "";
    if (!state.plugins.length) {
      box.innerHTML = '<div class="empty-state"><b>No plugins found</b><span>' +
        'A plugin is a folder under <span class="mono">plugins/</span> with a ' +
        '<span class="mono">plugin.json</span> that contributes skills and MCP servers. See plugins/README.md.</span></div>';
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

  /* ---------------- agents (harness) ---------------- */
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
        ? "a separate host with Docker isolation" : "NOT a separate host — no isolation"],
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
    if (!d.enabled) {
      const warn = document.createElement("div");
      warn.className = "msg-note";
      warn.textContent = "Agents are disabled. Enable them in config.json (agents.enabled) and set " +
        "agents.dir on a dedicated worker host reached by agents.ssh — not a Spark, not this console. " +
        "Until then the launcher is inert.";
      topo.appendChild(warn);
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

    $("agent-run").disabled = !d.enabled || state.streaming;
    $("agent-launch-note").textContent = d.enabled ? "" : "enable agents in config.json first";

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

  async function openRunLog(id) {
    const pre = $("agent-rundetail");
    pre.hidden = false; pre.textContent = "loading…"; pre.scrollIntoView({ block: "nearest" });
    try {
      const d = await (await fetch("/api/agents/log?id=" + encodeURIComponent(id))).json();
      if (d.error) { pre.textContent = "error: " + d.error; return; }
      const head = "goal: " + (d.goal || "") + "\nhost: " + (d.host || "") +
        "  ·  " + (d.killed ? d.killed : "exit " + d.exit) + "\n" + "─".repeat(40) + "\n";
      pre.textContent = head + (d.output || []).join("\n");
    } catch (e) { pre.textContent = "error: " + e.message; }
  }

  async function renderAgentSlaves() {
    const box = $("agents-slaves");
    let d = { ok: false };
    try { d = await (await fetch("/api/agents/slaves")).json(); } catch (e) {}
    box.textContent = "";
    if (!d.ok || !(d.slaves || []).length) {
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
      const ok = sl.success ? "✓ done" : (sl.error ? "✗ failed" : "…");
      head.innerHTML = "<div class='skill-id'><b></b><span class='src mono'></span></div><span class='src mono'></span>";
      head.querySelector("b").textContent = sl.role || "slave";
      head.querySelectorAll(".src")[0].textContent =
        (sl.network ? "network" : "no-network") + " · " + (sl.depth || "");
      head.querySelectorAll(".src")[1].textContent = ok + " · " + (sl.tokens || 0) + " tok";
      const brief = document.createElement("div");
      brief.className = "skill-desc";
      brief.textContent = sl.brief || "";
      card.appendChild(head); card.appendChild(brief);
      const detail = sl.success ? sl.answer : sl.error;
      if (detail) {
        const dv = document.createElement("div");
        dv.className = "skill-tags mono"; dv.style.whiteSpace = "pre-wrap";
        dv.textContent = detail;
        card.appendChild(dv);
      }
      box.appendChild(card);
    }
  }

  async function saveMaster() {
    const btn = $("master-save"); btn.disabled = true;
    try {
      await fetch("/api/agents", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "config",
          master_name: $("master-name").value.trim(),
          master_instructions: $("master-instructions").value.trim() }),
      });
      $("master-note").textContent = "Saved. The master uses this on the next run.";
    } catch (e) { $("master-note").textContent = "save failed: " + e.message; }
    btn.disabled = false;
  }

  async function runAgent() {
    const goal = $("agent-goal").value.trim();
    if (!goal || state.streaming) return;
    const out = $("agent-stream");
    out.hidden = false; out.textContent = "";
    state.streaming = true;
    $("agent-run").disabled = true;
    $("agent-stop").hidden = false;
    const ctl = new AbortController();
    state.agentAbort = ctl;
    const append = (t) => { out.textContent += t + "\n"; out.scrollTop = out.scrollHeight; };
    try {
      const r = await fetch("/api/agents", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal }), signal: ctl.signal,
      });
      if (!r.ok) { append("error: " + upstreamText(await r.text())); }
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
            if (o.phase === "start") append("— launching on " + o.host + " (" + o.mode + (o.isolated ? ", isolated" : ", NOT isolated") + ") —");
            else if (o.phase === "done") append("— finished: " + (o.killed ? o.killed : "exit " + o.exit) + " —");
            else if (o.error) append("error: " + o.error);
            else if (o.line != null) append(o.line);
          }
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") append("error: " + e.message);
    } finally {
      state.streaming = false;
      state.agentAbort = null;
      $("agent-run").disabled = false;
      $("agent-stop").hidden = true;
      renderAgents();
    }
  }

  function renderServers(status, cfg) {
    const box = $("tools-box");
    if (!box) return;
    box.textContent = "";
    const names = Object.keys(Object.assign({}, cfg, status));
    if (!names.length) {
      box.innerHTML = '<span class="hint">No MCP servers yet. Add one below — the model can then read and write through it.</span>';
      return;
    }
    for (const name of names) {
      const st = status[name] || { state: "unknown", tools: 0 };
      const on = !cfg[name] || cfg[name].enabled !== false;
      const r = document.createElement("div");
      r.className = "srv-row" + (on ? "" : " off");
      r.innerHTML = '<span class="d"></span><span class="n mono"></span><span class="s"></span>' +
        '<span class="acts"><button class="t" title="Enable/disable">\u25cf</button>' +
        '<button class="r" title="Restart">\u21bb</button>' +
        '<button class="x danger" title="Remove">\u2715</button></span>';
      r.querySelector(".d").style.background =
        st.state === "ready" ? "var(--ok)" : st.state === "error" ? "var(--err)" : "var(--faint)";
      r.querySelector(".n").textContent = name;
      r.querySelector(".s").textContent =
        st.state === "ready" ? st.tools + " tools"
        : st.state === "error" ? "error" : st.state;
      if (st.error) r.querySelector(".s").title = st.error;
      r.querySelector(".t").onclick = () => mcpAdmin({ action: "toggle", name });
      r.querySelector(".r").onclick = () => mcpAdmin({ action: "restart" });
      r.querySelector(".x").onclick = () => {
        if (confirm("Remove MCP server \"" + name + "\"?")) mcpAdmin({ action: "remove", name });
      };
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
      if (!r.ok) { $("mcp-form-err").textContent = d.error || "failed"; }
      else { $("mcp-form").hidden = true; $("mcp-form-err").textContent = ""; }
    } catch (e) {
      $("mcp-form-err").textContent = e.message;
    }
    await loadTools();   // re-read status and tool schemas after any change
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

  $("export-all").onclick = () => window.open("/api/export", "_blank");
  $("agent-run").onclick = runAgent;
  $("agent-stop").onclick = () => { if (state.agentAbort) state.agentAbort.abort(); };
  $("master-save").onclick = saveMaster;
  $("export-good").onclick = () => window.open("/api/export?rated=up", "_blank");
  $("new-chat").onclick = newChat;
  $("new-chat-2").onclick = newChat;
  document.addEventListener("keydown", (e) => {
    // cmd/ctrl-shift-O: new chat, the convention every chat app shares
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") {
      e.preventDefault(); newChat();
    }
  });

  /* ---------------- MCP form ---------------- */
  $("mcp-add-btn").onclick = () => {
    const f = $("mcp-form");
    f.hidden = !f.hidden;
    if (!f.hidden) $("mcp-name").focus();
  };
  $("mcp-cancel").onclick = () => { $("mcp-form").hidden = true; $("mcp-form-err").textContent = ""; };
  document.querySelectorAll(".mcp-preset").forEach((b) => (b.onclick = () => {
    $("mcp-name").value = b.dataset.name;
    $("mcp-cmd").value = b.dataset.cmd;
    $("mcp-args").value = b.dataset.args;
  }));
  $("mcp-save").onclick = () => mcpAdmin({
    action: "add",
    name: $("mcp-name").value.trim(),
    command: $("mcp-cmd").value.trim(),
    args: $("mcp-args").value.trim(),
  });

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
    $("code-endpoint").textContent = state.cfg.upstream;
    if (state.cfg.nodes.length) $("cluster-sub").textContent = state.cfg.nodes.length + " nodes configured";
    if (state.cfg.video) { $("nav-video").hidden = false; vidRefresh(); }
    if (state.cfg.netcheck) $("netcheck-card").hidden = false;
    await loadModels();
    if (state.cfg.mcp) loadTools();
    loadSkills();
    pollTelemetry();
    setInterval(pollTelemetry, 5000);
    setInterval(() => { if (!state.models.length) loadModels(); }, 15000);
  })();
})();
