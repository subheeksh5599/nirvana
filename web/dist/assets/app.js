// Nirvana web app: a landing page at "/" and a dashboard with a left rail at "/app".
// Every number on screen comes from the API; nothing is filled in when the API has no answer.

const API = (window.NIRVANA_API || "").replace(/\/$/, "") + "/api";
const root = document.getElementById("root");

const VERDICTS = ["move", "move_note", "rewrite", "keep", "blocked"];
const VERDICT_LABEL = {
  move: "Moves",
  move_note: "Moves, with a note",
  rewrite: "Moves after a rewrite",
  keep: "Stays on Databricks",
  blocked: "Cannot be judged",
};
const VERDICT_SENTENCE = {
  move: "Same answer on both engines.",
  move_note: "Same answer up to float noise or tied rows.",
  rewrite: "Moves after a rewrite proven on the rows.",
  keep: "Stays on Databricks: no rewrite reproduced its answer.",
  blocked: "Cannot be judged: the input differs or the source refuses the query.",
};
const ALIGN_LABEL = { aligned: "Aligned", aligned_types: "Aligned, type changed", not_aligned: "Not aligned", missing: "Missing" };
const CAUSE_LABEL = {
  type_drift: "Type drift",
  integer_division: "Integer division",
  null_ordering: "NULL ordering",
  dialect: "Dialect",
  model_rewrite: "Model rewrite, checked",
  float_noise: "Float noise",
  tie_at_limit: "Tie at a LIMIT",
  order_ties: "Order of tied rows",
  truncated: "Truncated compare",
  input_not_aligned: "Input not aligned",
  source_error: "Source refuses it",
  target_error: "Target refuses it",
  unexplained: "Unexplained",
};

// ------------------------------------------------------------------ helpers

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n == null || Number.isNaN(Number(n)) ? "not measured" : Number(n).toLocaleString("en-US"));
const ms = (n) => (n == null ? "not measured" : n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n)} ms`);
const pct = (x) => `${Math.round(x * 100)}%`;
const short = (h) => (h ? `${h.slice(0, 12)}` : "not recorded");
const when = (t) => (t ? new Date(t * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
const causeLabel = (c) => CAUSE_LABEL[c] || (c ? c.replace(/_/g, " ").replace(/\+/g, " + ") : "");

const engineLabel = (run, role) => {
  const e = run[role] || {};
  if (e.dialect === "databricks") return "Databricks";
  if (e.name === "zetaris" || (e.dialect === "spark" && role === "target")) return "Zetaris";
  return e.name || role;
};

async function api(path, opts = {}) {
  const res = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { detail: text }; }
  if (!res.ok) throw Object.assign(new Error((body && body.detail) || `HTTP ${res.status}`), { status: res.status });
  return body;
}

function navigate(path) {
  history.pushState({}, "", path);
  render();
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-nav]");
  if (a && !e.metaKey && !e.ctrlKey) {
    e.preventDefault();
    navigate(a.getAttribute("href"));
  }
});
window.addEventListener("popstate", render);

const state = { runId: localStorage.getItem("nirvana.run") || null, runs: null, health: null, stream: null };
function setRun(id) {
  state.runId = id;
  if (id) localStorage.setItem("nirvana.run", id);
}

function verdictBar(counts, total) {
  if (!total) return "";
  const parts = VERDICTS.filter((v) => counts[v]).map((v) => `<span class="c-${v}" style="width:${(counts[v] / total) * 100}%" title="${esc(VERDICT_LABEL[v])}: ${counts[v]}"></span>`).join("");
  const legend = VERDICTS.filter((v) => counts[v]).map((v) => `<span><i class="c-${v}"></i>${esc(VERDICT_LABEL[v])} <b class="mono">${counts[v]}</b></span>`).join("");
  return `<div class="bar">${parts}</div><div class="legend">${legend}</div>`;
}

async function latestRun() {
  const runs = await api("/runs");
  state.runs = runs;
  // The headline is the newest finished run of the whole workload; a short run (a few
  // queries picked by hand) only stands in when no full run exists.
  const finished = runs.filter((r) => r.status === "done" && r.summary);
  const live = finished.filter((r) => r.mode !== "rehearsal");
  const pool = live.length ? live : finished;
  const most = Math.max(0, ...pool.map((r) => r.summary.queries));
  const done = pool.find((r) => r.summary.queries === most) || null;
  return { runs, done };
}

// ------------------------------------------------------------------ landing

async function landing() {
  document.title = "Nirvana: prove which queries can leave Databricks";
  root.innerHTML = `
  <header class="site-header"><div class="wrap">
    <a class="wordmark" href="/" data-nav><span class="mark"></span>Nirvana</a>
    <nav class="site-nav">
      <a class="plain" href="#how">How it works</a>
      <a class="plain" href="#causes">What it catches</a>
      <a class="btn primary small" href="/app" data-nav>Open dashboard</a>
    </nav>
  </div></header>
  <main>
    <section class="hero"><div class="wrap">
      <h1>Know which queries can leave Databricks before they do.</h1>
      <p class="lead">Every query runs on Databricks and on Zetaris against the same data. The answers are compared row by row, and a rewrite is kept only when it returns the exact rows.</p>
      <div class="ctas">
        <a class="btn primary" href="/app" data-nav>Open dashboard</a>
        <a class="btn" href="https://github.com/subheeksh5599/nirvana">Source on GitHub</a>
      </div>
    </div></section>
    <section class="board-wrap"><div class="wrap"><div class="board" id="board"><div class="empty">Reading the latest run</div></div></div></section>
    <section class="block" id="how"><div class="wrap">
      <div class="eyebrow">How it works</div>
      <h2>Four steps, every one executed.</h2>
      <p class="one">No verdict comes from a model. Each one is a comparison of rows that both engines actually returned.</p>
      <div class="steps">
        <div class="step"><div class="n">01</div><h4>Align the inputs</h4><p>Both engines profile every table. A query over a table that differs is blocked, not judged.</p></div>
        <div class="step"><div class="n">02</div><h4>Run on both</h4><p>The same query runs on Databricks and on Zetaris, which reads the data in place.</p></div>
        <div class="step"><div class="n">03</div><h4>Compare every row</h4><p>Results become canonical rows and fingerprints. A changed value is named by its column.</p></div>
        <div class="step"><div class="n">04</div><h4>Prove the cause</h4><p>Rewrites run on Zetaris until one returns the source's exact rows. Otherwise the query stays.</p></div>
      </div>
    </div></section>
    <section class="block" id="causes"><div class="wrap">
      <div class="eyebrow">What it catches</div>
      <h2>Answers that look right and are not.</h2>
      <p class="one">Counts come from the live runs above.</p>
      <div class="causes" id="causes-grid"></div>
    </div></section>
    <section class="block"><div class="wrap">
      <div class="eyebrow">Built on</div>
      <h2>Each part does real work.</h2>
      <p class="one">Remove any one of them and a step above stops.</p>
      <div class="sponsors">
        <div class="sponsor"><b>Zetaris</b><span>The target engine. Every query, profile and rewrite runs on Lightning.</span></div>
        <div class="sponsor"><b>Meterless</b><span>H-MEM remembers proven fixes. The provider contract carries model calls.</span></div>
        <div class="sponsor"><b>NVIDIA Nemotron</b><span>Proposes rewrites for differences no rule explains. Execution decides.</span></div>
        <div class="sponsor"><b>Databricks</b><span>The source of truth. Its answer is the one every rewrite must reproduce.</span></div>
      </div>
    </div></section>
  </main>
  <footer class="site-footer"><div class="wrap"><span>Nirvana</span><span>Apache-2.0</span></div></footer>`;

  const causeCards = [
    ["type_drift", "Type drift", "A DECIMAL column stored as DOUBLE or REAL changes sums and comparisons."],
    ["integer_division", "Integer division", "7 / 2 is 3.5 on one engine and 3 on another."],
    ["null_ordering", "NULL ordering", "NULLs sort first on one engine and last on the other, so a top 10 changes."],
    ["tie_at_limit", "Ties at a LIMIT", "Two engines pick different tied rows. Proven with two extra runs, never assumed."],
    ["float_noise", "Float noise", "Last-bit differences in floating sums, reported with the largest gap."],
    ["unexplained", "Everything else", "Kept on Databricks, with the rows that differ, until something proves otherwise."],
  ];
  const board = document.getElementById("board");
  const grid = document.getElementById("causes-grid");
  const SCENARIO = { NIRVANA_TPCDS: "The same parquet files on both engines", NIRVANA_CSV: "Zetaris loaded from CSV exports with inferred types", TPCDS_DB: "Zetaris' bundled sample" };
  let shown = [];
  try {
    const runs = await api("/runs");
    state.runs = runs;
    const full = runs.filter((r) => r.status === "done" && r.summary && r.mode !== "rehearsal" && r.summary.queries === 103);
    const seen = new Set();
    for (const r of full) { const k = r.target.location; if (!seen.has(k)) { seen.add(k); shown.push(r); } }
    shown = shown.slice(0, 2);
    if (!shown.length) { const { done } = await latestRun(); if (done) shown = [done]; }
  } catch (e) {
    board.innerHTML = `<div class="empty">The API did not answer: ${esc(e.message)}</div>`;
    return;
  }
  const causes = {};
  shown.forEach((r) => Object.entries(r.summary.causes || {}).forEach(([c, n]) => { causes[c] = (causes[c] || 0) + n; }));
  grid.innerHTML = causeCards.map(([k, t, d]) => `<div class="cause"><div><h4>${esc(t)}</h4><p>${esc(d)}</p></div><div class="count">${shown.length ? (Object.entries(causes).filter(([c]) => c.split("+").includes(k)).reduce((x, [, n]) => x + n, 0)) : ""}</div></div>`).join("");
  if (!shown.length) { board.innerHTML = `<div class="empty">No finished run yet. <a href="/app" data-nav>Start one from the dashboard.</a></div>`; return; }
  board.innerHTML = shown.map((run, idx) => {
    const sm = run.summary, v = sm.verdicts || {};
    return `<div ${idx ? 'style="margin-top:28px;padding-top:24px;border-top:1px solid var(--line)"' : ""}>
    <div class="board-head">
      <div><h3>${esc(SCENARIO[run.target.location] || run.target.location)}</h3><div class="note">${esc(engineLabel(run, "source"))} to ${esc(engineLabel(run, "target"))}, live run, ${esc(when(run.finished_at))}</div></div>
      <div class="big">${sm.movable}<small> of ${sm.queries} queries can move</small></div>
    </div>
    ${verdictBar(v, sm.queries)}
    <div class="stats" style="margin-top:18px">
      <div class="stat"><div class="k">Moved after a proven rewrite</div><div class="v">${v.rewrite || 0}</div></div>
      <div class="stat"><div class="k">Stay on Databricks</div><div class="v">${v.keep || 0}</div></div>
      <div class="stat"><div class="k">Tables aligned</div><div class="v">${sm.tables_usable}/${sm.tables}</div></div>
      <div class="stat"><div class="k">Tables that changed type</div><div class="v">${sm.tables_retyped}</div></div>
    </div>
    <div style="margin-top:14px"><a href="/app/queries?run=${encodeURIComponent(run.id)}" data-nav>See every query</a></div></div>`;
  }).join("");
}

// ------------------------------------------------------------------ app shell

const NAV = [
  ["Run", [["overview", "Overview"], ["run", "Live run"]]],
  ["Results", [["queries", "Queries"], ["tables", "Tables"], ["rows", "Row diff"], ["guard", "Guard rules"], ["receipt", "Receipt"]]],
  ["Library", [["workload", "Workload"], ["memory", "Memory"], ["history", "History"]]],
];

function shell(active, title) {
  const runQ = state.runId ? `?run=${encodeURIComponent(state.runId)}` : "";
  const items = NAV.map(([g, list]) => `<div class="group">${g}</div>` + list.map(([k, label]) => `<a class="item ${k === active ? "active" : ""}" href="/app/${k}${["overview", "workload", "memory", "history", "rows", "guard"].includes(k) ? "" : runQ}" data-nav><span>${label}</span><span class="count" id="count-${k}"></span></a>`).join("")).join("");
  root.innerHTML = `
  <div class="app">
    <aside class="rail">
      <a class="wordmark" href="/" data-nav><span class="mark"></span>Nirvana</a>
      ${items}
      <div class="foot" id="rail-foot"></div>
    </aside>
    <div class="main">
      <div class="topbar"><h1>${esc(title)}</h1><div class="right" id="topbar-right"></div></div>
      <div class="pane" id="pane"></div>
    </div>
  </div>`;
  fillChrome();
  return document.getElementById("pane");
}

async function fillChrome() {
  try {
    const h = state.health || (state.health = await api("/health"));
    const st = await api("/status");
    const c = st.configured || {};
    const model = c.openrouter || c.nvidia || c.meterless;
    document.getElementById("topbar-right").innerHTML = `
      <span><span class="dot ${c.databricks ? "ok" : ""}"></span>Databricks</span>
      <span><span class="dot ${c.zetaris ? "ok" : ""}"></span>Zetaris</span>
      <span><span class="dot ${model ? "ok" : ""}"></span>Nemotron</span>
      <span class="tag ${h.mode === "recorded" ? "v-blocked" : "v-move"}">${h.mode === "recorded" ? "Recorded runs" : "Live"}</span>`;
    document.getElementById("rail-foot").textContent = `v${h.version}`;
  } catch (e) {
    const el = document.getElementById("topbar-right");
    if (el) el.innerHTML = `<span class="error">API offline</span>`;
  }
  try {
    const runs = state.runs || (await api("/runs"));
    state.runs = runs;
    const el = document.getElementById("count-history");
    if (el) el.textContent = runs.length || "";
    const cur = runs.find((r) => r.id === state.runId);
    const qc = document.getElementById("count-queries");
    if (qc && cur && cur.summary) qc.textContent = cur.summary.queries;
    const tc = document.getElementById("count-tables");
    if (tc && cur && cur.summary) tc.textContent = cur.summary.tables;
  } catch { /* the pane shows its own error */ }
}

async function ensureRun() {
  const params = new URLSearchParams(location.search);
  if (params.get("run")) setRun(params.get("run"));
  if (!state.runs) state.runs = await api("/runs");
  if (state.runId && state.runs.some((r) => r.id === state.runId)) return state.runs.find((r) => r.id === state.runId);
  const pick = state.runs[0] || null;
  setRun(pick ? pick.id : null);
  return pick;
}

const noRun = `<div class="panel"><div class="empty">No run yet. <a href="/app" data-nav>Start one from Overview.</a></div></div>`;

// ------------------------------------------------------------------ overview

async function paneOverview() {
  const pane = shell("overview", "Overview");
  pane.innerHTML = `
    <div class="panel"><div class="panel-head"><h2>Engines</h2><button class="btn small" id="check">Check connections</button></div>
      <div class="panel-body"><div class="grid4" id="engines"><div class="note">Press Check connections to run SELECT 1 on each engine.</div></div></div></div>
    <div class="panel"><div class="panel-head"><h2>Start a run</h2><span class="sub" id="mode-note"></span></div>
      <div class="panel-body">
        <div class="form-row">
          <div class="seg" id="engines-seg"><button data-v="live" class="on">Databricks to Zetaris</button><button data-v="rehearsal">Local rehearsal</button></div>
          <select id="target-loc" class="btn" title="Which copy on Zetaris to judge against"><option value="">Zetaris: same parquet files</option><option value="NIRVANA_CSV">Zetaris: loaded from CSV exports</option></select>
          <label class="check"><input type="checkbox" id="use-model" checked> Ask Nemotron when rules fail</label>
          <label class="check"><input type="checkbox" id="use-memory" checked> Use H-MEM memory</label>
        </div>
        <div class="form-row">
          <input type="text" id="queries" placeholder="All 103 queries, or ids like q3 q42 q90">
          <button class="btn primary" id="start">Start run</button>
          <span id="start-msg" class="note"></span>
        </div>
        <div class="note" id="rehearsal-note" hidden>The rehearsal runs two local DuckDB engines over generated TPC-DS data, the second with every DECIMAL stored as DOUBLE. It is labelled as a rehearsal everywhere.</div>
      </div></div>
    <div class="panel"><div class="panel-head"><h2>Latest run</h2><span class="sub" id="latest-sub"></span></div><div class="panel-body" id="latest"><div class="note">Loading</div></div></div>`;

  let engines = "live";
  pane.querySelectorAll("#engines-seg button").forEach((b) => b.addEventListener("click", () => {
    engines = b.dataset.v;
    pane.querySelectorAll("#engines-seg button").forEach((x) => x.classList.toggle("on", x === b));
    document.getElementById("rehearsal-note").hidden = engines !== "rehearsal";
  }));

  document.getElementById("check").addEventListener("click", checkEngines);
  document.getElementById("start").addEventListener("click", async () => {
    const msg = document.getElementById("start-msg");
    const ids = document.getElementById("queries").value.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    msg.textContent = "Starting";
    msg.className = "note";
    try {
      const r = await api("/runs", { method: "POST", body: JSON.stringify({ engines, queries: ids.length ? ids : null, use_model: document.getElementById("use-model").checked, use_memory: document.getElementById("use-memory").checked, target_location: engines === "live" ? (document.getElementById("target-loc").value || null) : null }) });
      setRun(r.id);
      state.runs = null;
      navigate(`/app/run?run=${encodeURIComponent(r.id)}`);
    } catch (e) {
      msg.textContent = e.message;
      msg.className = "error";
    }
  });

  try {
    const h = await api("/health");
    if (h.mode === "recorded") {
      document.getElementById("start").disabled = true;
      document.getElementById("mode-note").textContent = "This server serves recorded runs only";
    }
  } catch { /* chrome shows it */ }

  try {
    const { done } = await latestRun();
    const el = document.getElementById("latest");
    if (!done) { el.innerHTML = `<div class="note">No finished run yet.</div>`; return; }
    const s = done.summary;
    document.getElementById("latest-sub").textContent = `${engineLabel(done, "source")} to ${engineLabel(done, "target")}, ${when(done.finished_at)}`;
    el.innerHTML = `<div class="board-head" style="margin:0"><div class="big">${s.movable}<small> of ${s.queries} queries can move</small></div><a class="btn small" href="/app/queries?run=${encodeURIComponent(done.id)}" data-nav>Open results</a></div>${verdictBar(s.verdicts, s.queries)}`;
  } catch (e) {
    document.getElementById("latest").innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

async function checkEngines() {
  const el = document.getElementById("engines");
  el.innerHTML = `<div class="note">Running SELECT 1 on each engine</div>`;
  try {
    const st = await api("/status?check=true");
    const e = st.engines || {};
    const card = (role, x, where) => `<div class="engine"><div class="role">${role}</div><div class="name"><span class="dot ${x && x.ok ? "ok" : "bad"}"></span>${esc(x ? x.engine : "not checked")}</div><div class="detail">${esc(x ? x.detail : "")}</div><div class="where">${esc(where || "")}</div></div>`;
    const m = st.model;
    const mem = st.memory || {};
    el.innerHTML = card("Source", e.source, st.locations.source) + card("Target", e.target, st.locations.target)
      + `<div class="engine"><div class="role">Model</div><div class="name"><span class="dot ${m && m.state === "online" ? "ok" : "bad"}"></span>${esc(m ? m.state : "not configured")}</div><div class="detail">${esc(m ? (m.detail || m.url) : "")}</div></div>`
      + `<div class="engine"><div class="role">Memory</div><div class="name"><span class="dot ${mem.error ? "bad" : "ok"}"></span>H-MEM</div><div class="detail">${mem.error ? esc(mem.error) : `${fmt(mem.memories)} proven fixes stored`}</div></div>`;
  } catch (err) {
    el.innerHTML = `<div class="error">${esc(err.message)}</div>`;
  }
}

// ------------------------------------------------------------------ live run

async function paneRun() {
  const pane = shell("run", "Live run");
  let run;
  try { run = await ensureRun(); } catch (e) { pane.innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  if (!run) { pane.innerHTML = noRun; return; }
  const total = run.query_ids.length;
  pane.innerHTML = `
    <div class="panel"><div class="panel-head"><div><h2 class="mono">${esc(run.id)}</h2><div class="sub">${esc(engineLabel(run, "source"))} to ${esc(engineLabel(run, "target"))}${run.mode === "rehearsal" ? ", local rehearsal" : ""}</div></div>
      <div style="display:flex;gap:10px;align-items:center"><span class="tag" id="run-status"></span><button class="btn small" id="cancel" hidden>Cancel</button></div></div>
      <div class="panel-body"><div class="progress"><span id="prog" style="width:0%"></span></div><div class="note" style="margin-top:8px" id="prog-text"></div><div id="run-summary"></div></div></div>
    <div class="panel"><div class="panel-head"><h2>Step 1. Source alignment</h2><span class="sub" id="align-sub"></span></div><div class="panel-body"><div class="tiles" id="tiles"></div></div></div>
    <div class="panel"><div class="panel-head"><h2>Step 2. Queries</h2><span class="sub">Click a finished cell to open it</span></div><div class="panel-body"><div class="cells" id="cells">${run.query_ids.map((q) => `<div class="cell" id="cell-${q}" data-q="${q}">${q}</div>`).join("")}</div></div></div>
    <div class="panel"><div class="panel-head"><h2>Events</h2></div><div class="panel-body events" id="events"></div></div>`;

  document.getElementById("cells").addEventListener("click", (e) => {
    const c = e.target.closest(".cell.done");
    if (c) navigate(`/app/q/${c.dataset.q}?run=${encodeURIComponent(run.id)}`);
  });
  document.getElementById("cancel").addEventListener("click", async () => {
    try { await api(`/runs/${run.id}/cancel`, { method: "POST" }); } catch (e) { alert(e.message); }
  });

  let judged = 0;
  const tiles = document.getElementById("tiles");
  const events = document.getElementById("events");
  const setStatus = (s) => {
    const tag = document.getElementById("run-status");
    if (!tag) return;
    const cls = { running: "v-rewrite", done: "v-move", failed: "v-keep", cancelled: "v-blocked" }[s] || "v-blocked";
    tag.className = `tag ${cls}`;
    tag.textContent = s.charAt(0).toUpperCase() + s.slice(1);
    document.getElementById("cancel").hidden = s !== "running";
  };
  setStatus(run.status);
  const progress = () => {
    document.getElementById("prog").style.width = `${total ? (judged / total) * 100 : 0}%`;
    document.getElementById("prog-text").textContent = `${judged} of ${total} queries judged`;
  };
  const addTile = (d) => {
    tiles.insertAdjacentHTML("beforeend", `<div class="tile"><div class="nm">${esc(d.table)}</div><div style="margin-top:6px"><span class="tag a-${d.status}">${esc(ALIGN_LABEL[d.status] || d.status)}</span></div><div class="rows">${fmt(d.source_rows)} / ${fmt(d.target_rows)} rows</div></div>`);
    document.getElementById("align-sub").textContent = `${tiles.children.length} tables`;
  };
  const mark = (d) => {
    const c = document.getElementById(`cell-${d.query_id}`);
    if (!c) return;
    c.className = `cell done v-${d.verdict}`;
    c.title = `${VERDICT_LABEL[d.verdict]}${d.cause ? ": " + causeLabel(d.cause) : ""}. ${d.note || ""}`;
  };
  const log = (e) => {
    const d = e.data || {};
    const t = {
      started: () => `${d.queries} queries`,
      ping: () => `${d.role} ${d.engine}: ${d.detail}`,
      aligned: () => `${d.table}: ${ALIGN_LABEL[d.status] || d.status}`,
      judging: () => d.query_id,
      judged: () => `${d.query_id}: ${VERDICT_LABEL[d.verdict]}${d.cause ? ", " + causeLabel(d.cause) : ""}`,
      finished: () => `${d.summary.movable} of ${d.summary.queries} can move`,
      failed: () => d.error,
      cancelled: () => "stopped",
    }[e.kind];
    const text = t ? t() : "";
    events.insertAdjacentHTML("afterbegin", `<div><span class="k">${new Date(e.at * 1000).toLocaleTimeString("en-GB")}</span><span>${esc(e.kind)}</span><span class="muted">${esc(text)}</span></div>`);
  };
  const summarize = (s) => {
    document.getElementById("run-summary").innerHTML = `<div class="board-head" style="margin:18px 0 0"><div class="big">${s.movable}<small> of ${s.queries} queries can move</small></div><a class="btn small" href="/app/queries?run=${encodeURIComponent(run.id)}" data-nav>Open results</a></div>${verdictBar(s.verdicts, s.queries)}`;
  };
  const handle = (e) => {
    if (e.kind === "aligned") addTile(e.data);
    if (e.kind === "judging") { document.querySelectorAll(".cell.now").forEach((c) => c.classList.remove("now")); const c = document.getElementById(`cell-${e.data.query_id}`); if (c) c.classList.add("now"); }
    if (e.kind === "judged") { judged++; mark(e.data); progress(); }
    if (e.kind === "finished") { setStatus("done"); summarize(e.data.summary); }
    if (e.kind === "failed") setStatus("failed");
    if (e.kind === "cancelled") setStatus("cancelled");
    log(e);
  };
  progress();
  if (state.stream) state.stream.close();
  if (["done", "failed", "cancelled"].includes(run.status)) {
    const evs = await api(`/runs/${run.id}/events`);
    evs.forEach(handle);
    if (run.summary) summarize(run.summary);
    setStatus(run.status);
    return;
  }
  const es = new EventSource(`${API}/runs/${encodeURIComponent(run.id)}/stream`);
  state.stream = es;
  ["started", "ping", "aligned", "judging", "judged", "finished", "failed", "cancelled"].forEach((k) => es.addEventListener(k, (m) => handle(JSON.parse(m.data))));
  es.addEventListener("end", () => { es.close(); state.runs = null; });
}

// ------------------------------------------------------------------ queries

async function paneQueries() {
  const pane = shell("queries", "Queries");
  let run;
  try { run = await ensureRun(); } catch (e) { pane.innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  if (!run) { pane.innerHTML = noRun; return; }
  const SN = esc(engineLabel(run, "source")), TN = esc(engineLabel(run, "target"));
  const filter = new URLSearchParams(location.search).get("verdict") || "";
  const rows = await api(`/runs/${run.id}/judgements`);
  const counts = rows.reduce((m, r) => ((m[r.verdict] = (m[r.verdict] || 0) + 1), m), {});
  const shown = filter ? rows.filter((r) => r.verdict === filter) : rows;
  pane.innerHTML = `
    <div class="panel"><div class="panel-head"><div><h2>${rows.length} queries judged</h2><div class="sub mono">${esc(run.id)}</div></div>
      <div class="chips">${["", ...VERDICTS].map((v) => `<button class="chip ${v === filter ? "on" : ""}" data-v="${v}">${v ? `${VERDICT_LABEL[v]} ${counts[v] || 0}` : `All ${rows.length}`}</button>`).join("")}</div></div>
      ${rows.length ? `<div class="scroll"><table class="t"><thead><tr><th>Query</th><th>Verdict</th><th>Cause</th><th>Why</th><th class="num">Rows (source / target)</th><th class="num">${SN}</th><th class="num">${TN}</th></tr></thead><tbody>
      ${shown.map((r) => `<tr class="click" data-q="${r.query_id}"><td class="mono">${r.query_id}</td><td><span class="tag v-${r.verdict}">${esc(VERDICT_LABEL[r.verdict])}</span></td><td>${esc(causeLabel(r.cause))}</td><td class="muted">${esc(r.note)}</td><td class="num">${r.source_rows == null ? "not run" : `${fmt(r.source_rows)} / ${fmt(r.target_rows)}`}</td><td class="num">${r.source_ms ? ms(r.source_ms) : ""}</td><td class="num">${r.target_ms ? ms(r.target_ms) : ""}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">This run has not judged any query yet.</div>`}</div>`;
  pane.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => navigate(`/app/queries?run=${encodeURIComponent(run.id)}${b.dataset.v ? `&verdict=${b.dataset.v}` : ""}`)));
  pane.querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => navigate(`/app/q/${tr.dataset.q}?run=${encodeURIComponent(run.id)}`)));
}

function lineDiff(a, b) {
  const x = a.split("\n"), y = b.split("\n");
  const n = x.length, m = y.length;
  const L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push(`<span>${esc(x[i])}</span>`); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) { out.push(`<span class="del">${esc(x[i])}</span>`); i++; }
    else { out.push(`<span class="add">${esc(y[j])}</span>`); j++; }
  }
  while (i < n) out.push(`<span class="del">${esc(x[i++])}</span>`);
  while (j < m) out.push(`<span class="add">${esc(y[j++])}</span>`);
  return out.join("\n");
}
const pretty = (sql) => (sql || "").replace(/\s+(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|UNION ALL|UNION|INNER JOIN|LEFT JOIN|JOIN|WITH|AND|SELECT)\b/g, "\n$1");

async function paneQuery(qid) {
  const pane = shell("queries", `Query ${qid}`);
  let run;
  try { run = await ensureRun(); } catch (e) { pane.innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  if (!run) { pane.innerHTML = noRun; return; }
  const SN = esc(engineLabel(run, "source")), TN = esc(engineLabel(run, "target"));
  let j;
  try { j = await api(`/runs/${run.id}/judgements/${qid}`); } catch (e) { pane.innerHTML = `<div class="panel"><div class="empty">${esc(e.message)}</div></div>`; return; }
  const c = j.comparison || {};
  const f = j.final_comparison || null;
  const sample = j.sample || { paired: [], only_in_source: [], only_in_target: [] };
  const cols = c.mismatched_columns || [];
  pane.innerHTML = `
    <div style="margin-bottom:16px"><a href="/app/queries?run=${encodeURIComponent(run.id)}" data-nav>All queries</a></div>
    <div class="panel"><div class="panel-body">
      <div class="headline"><h2>${esc(j.query_id)}</h2><span class="tag v-${j.verdict}">${esc(VERDICT_LABEL[j.verdict])}</span>${j.cause ? `<span class="tag v-blocked">${esc(causeLabel(j.cause))}</span>` : ""}</div>
      <div class="muted" style="font-size:15px">${esc(j.note)}</div>
      <div class="note" style="margin-top:6px">Reads ${esc((j.tables || []).join(", "))}</div>
    </div></div>
    <div class="grid2">
      <div class="panel"><div class="panel-head"><h2>First comparison</h2><span class="sub">${esc(c.status || "not compared")}</span></div><div class="panel-body"><dl class="kv">
        <dt>${SN} rows</dt><dd>${fmt(c.source_rows)}</dd><dt>${TN} rows</dt><dd>${fmt(c.target_rows)}</dd>
        <dt>${SN} fingerprint</dt><dd title="${esc(c.source_fp)}">${short(c.source_fp)}</dd><dt>${TN} fingerprint</dt><dd title="${esc(c.target_fp)}">${short(c.target_fp)}</dd>
        <dt>Columns that differ</dt><dd>${cols.length ? esc(cols.join(", ")) : "none"}</dd>
        <dt>Type changes</dt><dd>${(c.type_mismatches || []).length ? esc(c.type_mismatches.map((t) => `${t.name}: ${t.source} to ${t.target}`).join(", ")) : "none"}</dd>
        <dt>Largest relative gap</dt><dd>${c.max_rel_gap ? Number(c.max_rel_gap).toExponential(2) : "0"}</dd>
        <dt>Time</dt><dd>${ms(j.source_ms)} / ${ms(j.target_ms)}</dd>
      </dl></div></div>
      <div class="panel"><div class="panel-head"><h2>After the rewrite</h2><span class="sub">${f ? esc(f.status) : "no rewrite kept"}</span></div><div class="panel-body">${f ? `<dl class="kv">
        <dt>${TN} rows</dt><dd>${fmt(f.target_rows)}</dd><dt>${SN} fingerprint</dt><dd title="${esc(f.source_fp)}">${short(f.source_fp)}</dd><dt>${TN} fingerprint</dt><dd title="${esc(f.target_fp)}">${short(f.target_fp)}</dd>
        <dt>Same rows</dt><dd>${f.source_fp === f.target_fp ? "yes, fingerprints equal" : "no"}</dd></dl>` : `<div class="note">${j.verdict === "keep" ? "Every rewrite tried returned different rows, so none was kept." : "The first comparison already settled this query."}</div>`}</div></div>
    </div>
    ${(sample.paired || []).length ? `<div class="panel"><div class="panel-head"><h2>Rows that differ</h2><span class="sub">Up to 5, as each engine returned them</span></div><div class="scroll"><table class="t"><thead><tr><th>Engine</th>${sample.paired[0].source.map((_, i) => `<th>col ${i + 1}</th>`).join("")}</tr></thead><tbody>
      ${sample.paired.map((p) => `<tr><td>${SN}</td>${p.source.map((v) => `<td class="mono">${esc(v ?? "NULL")}</td>`).join("")}</tr><tr><td>${TN}</td>${p.target.map((v, i) => `<td class="mono" style="${String(p.source[i]) !== String(v) ? "background:var(--bad-wash)" : ""}">${esc(v ?? "NULL")}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div></div>` : ""}
    ${j.rewritten_sql ? `<div class="panel"><div class="panel-head"><h2>The rewrite that was kept</h2><span class="sub">${TN} text before and after</span></div><div class="panel-body"><pre class="sql diff">${lineDiff(pretty(j.target_sql), pretty(j.rewritten_sql))}</pre></div></div>` : ""}
    <div class="grid2">
      <div class="panel"><div class="panel-head"><h2>${SN} text</h2></div><div class="panel-body"><pre class="sql">${esc(pretty(j.source_sql))}</pre></div></div>
      <div class="panel"><div class="panel-head"><h2>${TN} text</h2></div><div class="panel-body"><pre class="sql">${esc(pretty(j.target_sql))}</pre></div></div>
    </div>
    <div class="panel"><div class="panel-head"><h2>Every step</h2><span class="sub">${(j.steps || []).length} executions</span></div><div class="panel-body"><ul class="timeline">
      ${(j.steps || []).map((s) => `<li><span class="dot ${s.ok ? "ok" : "bad"}" style="margin-top:6px"></span><div><div class="st">${esc(s.name)} <span class="faint">on ${esc(s.engine)}</span></div><div class="dt">${esc(s.detail)}${s.fingerprint ? ` <span class="mono faint">${short(s.fingerprint)}</span>` : ""}</div>${s.sql ? `<details><summary>Show SQL</summary><pre class="sql" style="margin-top:8px">${esc(pretty(s.sql))}</pre></details>` : ""}</div><div class="ms">${s.ms ? ms(s.ms) : ""}</div></li>`).join("") || `<li><span></span><div class="dt">No query ran: ${esc(j.note)}</div><span></span></li>`}
    </ul></div></div>`;
}

// ------------------------------------------------------------------ tables, receipt

async function paneTables() {
  const pane = shell("tables", "Tables");
  let run;
  try { run = await ensureRun(); } catch (e) { pane.innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  if (!run) { pane.innerHTML = noRun; return; }
  const SN = esc(engineLabel(run, "source")), TN = esc(engineLabel(run, "target"));
  const al = await api(`/runs/${run.id}/alignment`);
  pane.innerHTML = `<div class="panel"><div class="panel-head"><div><h2>Source alignment</h2><div class="sub">Each table profiled on both engines before any query is judged</div></div></div>
    ${al.length ? `<div class="scroll"><table class="t"><thead><tr><th>Table</th><th>Status</th><th class="num">${SN} rows</th><th class="num">${TN} rows</th><th>Type changes</th><th>Differences</th></tr></thead><tbody>
    ${al.map((a) => `<tr><td class="mono">${esc(a.table)}</td><td><span class="tag a-${a.status}">${esc(ALIGN_LABEL[a.status] || a.status)}</span></td><td class="num">${fmt(a.source_rows)}</td><td class="num">${fmt(a.target_rows)}</td>
      <td>${(a.type_changes || []).length ? `<details><summary>${a.type_changes.length} column${a.type_changes.length === 1 ? "" : "s"}</summary><div class="note mono">${a.type_changes.map((t) => esc(`${t.column}: ${t.source} to ${t.target}`)).join("<br>")}</div></details>` : `<span class="faint">none</span>`}</td>
      <td>${a.error ? `<span class="error">${esc(a.error)}</span>` : (a.differences || []).length ? `<details><summary>${a.differences.length} measure${a.differences.length === 1 ? "" : "s"}</summary><div class="note mono">${a.differences.slice(0, 20).map((d) => esc(`${d.measure}: ${d.source} vs ${d.target}`)).join("<br>")}</div></details>` : `<span class="faint">none</span>`}</td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">No table was aligned in this run.</div>`}</div>`;
}

async function paneReceipt() {
  const pane = shell("receipt", "Receipt");
  let run;
  try { run = await ensureRun(); } catch (e) { pane.innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  if (!run) { pane.innerHTML = noRun; return; }
  const SN = esc(engineLabel(run, "source")), TN = esc(engineLabel(run, "target"));
  const r = await api(`/runs/${run.id}/receipt`);
  const items = r.receipt.items;
  pane.innerHTML = `
    <div class="panel"><div class="panel-head"><div><h2>Receipt hash</h2><div class="sub">SHA-256 over every query hash, both fingerprints, the verdicts and each rewrite</div></div><a class="btn small" href="${API}/runs/${encodeURIComponent(run.id)}/recording">Download recording</a></div>
      <div class="panel-body"><div class="hash">${esc(r.sha256)}</div><div class="note" style="margin-top:8px">Importing a recording recomputes this hash and refuses the file if any verdict or fingerprint was edited.</div></div></div>
    <div class="panel"><div class="panel-head"><h2>${items.length} items</h2></div><div class="scroll"><table class="t"><thead><tr><th>Query</th><th>Verdict</th><th>${SN} fingerprint</th><th>${TN} fingerprint</th><th>Rewrite</th></tr></thead><tbody>
      ${items.map((i) => `<tr><td class="mono">${i.query_id}</td><td><span class="tag v-${i.verdict}">${esc(VERDICT_LABEL[i.verdict])}</span></td><td class="mono" title="${esc(i.source_fp)}">${short(i.source_fp)}</td><td class="mono" title="${esc(i.target_fp)}">${short(i.target_fp)}</td><td class="mono">${i.rewrite_sha256 ? short(i.rewrite_sha256) : ""}</td></tr>`).join("")}
    </tbody></table></div></div>`;
}

// ------------------------------------------------------------------ row diff

const TABLES = ["call_center", "catalog_page", "catalog_returns", "catalog_sales", "customer", "customer_address", "customer_demographics", "date_dim", "household_demographics", "income_band", "inventory", "item", "promotion", "reason", "ship_mode", "store", "store_returns", "store_sales", "time_dim", "warehouse", "web_page", "web_returns", "web_sales", "web_site"];

function diffCard(d) {
  const head = d.error
    ? `<span class="tag v-keep">Failed</span>`
    : d.identical ? `<span class="tag v-move">Same rows</span>` : `<span class="tag v-keep">Rows differ</span>`;
  const sample = (rows, label) => rows && rows.length ? `<div class="note" style="margin:12px 0 6px">${label}</div><div class="scroll"><table class="t"><thead><tr>${d.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((v) => `<td class="mono">${esc(v ?? "NULL")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "";
  return `<div class="panel"><div class="panel-head"><div><h2 class="mono">${esc(d.table)}</h2><div class="sub">${esc(d.reference)} against ${esc(d.candidate)}, ${esc(when(d.at))}, ${ms(d.ms)} on Zetaris</div></div>${head}</div>
    <div class="panel-body">${d.error ? `<div class="error">${esc(d.error)}</div>` : `
      <div class="grid2">
        <div class="stat"><div class="k">Rows only in ${esc(d.reference)}</div><div class="v">${fmt(d.only_in_reference)}</div></div>
        <div class="stat"><div class="k">Rows only in ${esc(d.candidate)}</div><div class="v">${fmt(d.only_in_candidate)}</div></div>
      </div>
      ${sample(d.sample_reference, `Rows in ${esc(d.reference)} that ${esc(d.candidate)} does not have`)}
      ${sample(d.sample_candidate, `Rows in ${esc(d.candidate)} that ${esc(d.reference)} does not have`)}
      <details style="margin-top:12px"><summary class="note" style="cursor:pointer">The federated SQL Zetaris ran</summary><pre class="sql" style="margin-top:8px">${esc((d.sql || []).join(";\n\n"))}</pre></details>`}
    </div></div>`;
}

async function paneRows() {
  const pane = shell("rows", "Row diff");
  pane.innerHTML = `
    <div class="panel"><div class="panel-head"><div><h2>Which rows differ, not just how many</h2><div class="sub">Zetaris reads both copies in one federated query, even when one of them is Databricks itself, and returns the rows each side lacks</div></div></div>
      <div class="panel-body">
        <div class="form-row">
          <select id="rd-table" class="btn">${TABLES.map((t) => `<option ${t === "catalog_page" ? "selected" : ""}>${t}</option>`).join("")}</select>
          <input type="text" id="rd-ref" value="DATABRICKS" style="min-width:180px" title="DATABRICKS reads the Databricks warehouse itself through Zetaris' JDBC datasource">
          <span class="note">against</span>
          <input type="text" id="rd-cand" value="TPCDS_DB" style="min-width:180px" title="The Zetaris database to check">
          <button class="btn primary" id="rd-go">Compare on Zetaris</button>
          <span class="note" id="rd-msg"></span>
        </div>
        <div class="note">DATABRICKS: the Databricks warehouse itself, read by Zetaris over JDBC. TPCDS_DB: the TPC-DS sample Zetaris ships, on a public S3 bucket. NIRVANA_TPCDS and NIRVANA_CSV: the same tables as parquet and as CSV exports, on local disk.</div>
      </div></div>
    <div id="rd-list"><div class="empty">Loading</div></div>`;
  const list = document.getElementById("rd-list");
  const load = async () => {
    try {
      const ds = await api("/diffs");
      list.innerHTML = ds.length ? ds.map(diffCard).join("") : `<div class="panel"><div class="empty">No comparison yet.</div></div>`;
      const c = document.getElementById("count-rows");
      if (c) c.textContent = ds.length || "";
    } catch (e) { list.innerHTML = `<div class="panel"><div class="empty">${esc(e.message)}</div></div>`; }
  };
  document.getElementById("rd-go").addEventListener("click", async () => {
    const btn = document.getElementById("rd-go"), msg = document.getElementById("rd-msg");
    btn.disabled = true; msg.className = "note"; msg.textContent = "Zetaris is comparing the two copies";
    const t0 = Date.now();
    const tick = setInterval(() => { msg.textContent = `Zetaris is comparing the two copies, ${Math.round((Date.now() - t0) / 1000)} s`; }, 1000);
    try {
      await api("/diffs", { method: "POST", body: JSON.stringify({ table: document.getElementById("rd-table").value, reference: document.getElementById("rd-ref").value.trim(), candidate: document.getElementById("rd-cand").value.trim(), sample: 5 }) });
      msg.textContent = "";
      await load();
    } catch (e) { msg.className = "error"; msg.textContent = e.message; }
    clearInterval(tick); btn.disabled = false;
  });
  load();
}

// ------------------------------------------------------------------ guard rules

function guardCard(g) {
  const broken = (g.rules || []).filter((r) => r.invalid);
  const head = g.error ? `<span class="tag v-keep">Failed</span>` : broken.length ? `<span class="tag v-keep">${broken.length} rule${broken.length === 1 ? "" : "s"} broken</span>` : `<span class="tag v-move">All rules hold</span>`;
  const rows = (g.rules || []).slice().sort((a, b) => (b.invalid || 0) - (a.invalid || 0)).map((r) => `
    <tr><td class="mono">${esc(r.column)}</td><td class="muted">${esc(r.why)}</td>
    <td class="num">${r.total == null ? "" : fmt(r.total)}</td><td class="num">${r.invalid == null ? (r.error ? "" : "") : fmt(r.invalid)}</td>
    <td>${r.error ? `<span class="error">${esc(r.error)}</span>` : r.invalid ? `<span class="tag v-keep">Broken</span>` : `<span class="tag v-move">Holds</span>`}</td></tr>
    ${r.sample && r.sample.length ? `<tr><td></td><td colspan="4"><div class="note" style="margin-bottom:6px">Rows Zetaris flags (${esc(r.expression)})</div>${(() => { const keep = g.columns.map((c, i) => i).filter((i) => i === 0 || g.columns[i] === r.column || /_(state|city|id)$/.test(g.columns[i])); return `<table class="t" style="width:auto"><thead><tr>${keep.map((i) => `<th>${esc(g.columns[i])}</th>`).join("")}</tr></thead><tbody>${r.sample.map((x) => `<tr>${keep.map((i) => `<td class="mono" style="${g.columns[i] === r.column ? "background:var(--bad-wash)" : ""}">${esc(x[i] ?? "NULL")}</td>`).join("")}</tr>`).join("")}</tbody></table>`; })()}</td></tr>` : ""}`).join("");
  return `<div class="panel"><div class="panel-head"><div><h2 class="mono">${esc(g.table)}</h2><div class="sub">rules from ${esc(g.source)}, enforced by Zetaris on ${esc(g.candidate)}, ${esc(when(g.at))}, ${ms(g.ms)}</div></div>${head}</div>
    ${g.error ? `<div class="panel-body"><div class="error">${esc(g.error)}</div></div>` : `<div class="scroll"><table class="t"><thead><tr><th>Column</th><th>Rule, from Databricks</th><th class="num">Rows checked</th><th class="num">Rows breaking it</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="panel-body"><details><summary class="note" style="cursor:pointer">The Zetaris statements (USL and DQ)</summary><pre class="sql" style="margin-top:8px">${esc((g.sql || []).join(";\n\n"))}</pre></details></div>`}</div>`;
}

async function paneGuard() {
  const pane = shell("guard", "Guard rules");
  pane.innerHTML = `
    <div class="panel"><div class="panel-head"><div><h2>Keep the target honest after the move</h2><div class="sub">Nirvana measures each column on Databricks and installs the result as Zetaris data-quality rules on the migrated copy</div></div></div>
      <div class="panel-body"><div class="form-row">
        <select id="gd-table" class="btn">${TABLES.map((t) => `<option ${t === "customer_address" ? "selected" : ""}>${t}</option>`).join("")}</select>
        <span class="note">migrated to</span>
        <input type="text" id="gd-cand" value="NIRVANA_CSV" style="min-width:180px">
        <button class="btn primary" id="gd-go">Install and run on Zetaris</button>
        <span class="note" id="gd-msg"></span>
      </div>
      <div class="note">NIRVANA_CSV: the same tables exported to CSV and loaded with inferred types, a common migration path.</div></div></div>
    <div id="gd-list"><div class="empty">Loading</div></div>`;
  const list = document.getElementById("gd-list");
  const load = async () => {
    try {
      const gs = await api("/guards");
      list.innerHTML = gs.length ? gs.map(guardCard).join("") : `<div class="panel"><div class="empty">No guard installed yet.</div></div>`;
      const c = document.getElementById("count-guard");
      if (c) c.textContent = gs.length || "";
    } catch (e) { list.innerHTML = `<div class="panel"><div class="empty">${esc(e.message)}</div></div>`; }
  };
  document.getElementById("gd-go").addEventListener("click", async () => {
    const btn = document.getElementById("gd-go"), msg = document.getElementById("gd-msg");
    btn.disabled = true; msg.className = "note";
    const t0 = Date.now();
    const tick = setInterval(() => { msg.textContent = `Measuring Databricks, installing rules on Zetaris, ${Math.round((Date.now() - t0) / 1000)} s`; }, 1000);
    try {
      await api("/guards", { method: "POST", body: JSON.stringify({ table: document.getElementById("gd-table").value, candidate: document.getElementById("gd-cand").value.trim(), sample: 5 }) });
      msg.textContent = ""; await load();
    } catch (e) { msg.className = "error"; msg.textContent = e.message; }
    clearInterval(tick); btn.disabled = false;
  });
  load();
}

// ------------------------------------------------------------------ library

async function paneWorkload(qid) {
  const pane = shell("workload", qid ? `Workload ${qid}` : "Workload");
  if (qid) {
    let q;
    try { q = await api(`/workload/${qid}`); } catch (e) { pane.innerHTML = `<div class="panel"><div class="empty">${esc(e.message)}</div></div>`; return; }
    pane.innerHTML = `<div style="margin-bottom:16px"><a href="/app/workload" data-nav>All queries</a></div>
      <div class="panel"><div class="panel-head"><h2>${esc(q.id)} as written</h2><span class="sub">Reads ${esc(q.tables.join(", "))}</span></div><div class="panel-body"><pre class="sql">${esc(q.sql)}</pre></div></div>
      <div class="grid2"><div class="panel"><div class="panel-head"><h2>On Databricks</h2></div><div class="panel-body"><pre class="sql">${esc(pretty(q.source_sql))}</pre></div></div>
      <div class="panel"><div class="panel-head"><h2>On Zetaris</h2></div><div class="panel-body"><pre class="sql">${esc(pretty(q.target_sql))}</pre></div></div></div>`;
    return;
  }
  const w = await api("/workload");
  pane.innerHTML = `<div class="panel"><div class="panel-head"><div><h2>${w.length} TPC-DS queries</h2><div class="sub">Apache Spark renderings, copied unchanged at a pinned commit</div></div></div><div class="scroll"><table class="t"><thead><tr><th>Query</th><th>Tables</th><th>Sorted</th><th class="num">LIMIT</th><th class="num">Lines</th></tr></thead><tbody>
    ${w.map((q) => `<tr class="click" data-q="${q.id}"><td class="mono">${q.id}</td><td class="muted">${esc(q.tables.join(", "))}</td><td>${q.ordered ? "yes" : "no"}</td><td class="num">${q.limit ?? ""}</td><td class="num">${q.lines}</td></tr>`).join("")}
  </tbody></table></div></div>`;
  pane.querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => navigate(`/app/workload/${tr.dataset.q}`)));
}

async function paneMemory() {
  const pane = shell("memory", "Memory");
  pane.innerHTML = `<div class="panel"><div class="panel-head"><div><h2>What H-MEM holds</h2><div class="sub">Fixes proven once are tried first next time, and lose trust when they fail</div></div>
    <div style="display:flex;gap:8px"><input type="search" id="mq" value="status:different has_division reads_retyped_columns"><button class="btn small" id="go">Search</button></div></div><div id="mem"></div></div>`;
  const load = async () => {
    const el = document.getElementById("mem");
    el.innerHTML = `<div class="empty">Searching</div>`;
    try {
      const r = await api(`/memory?q=${encodeURIComponent(document.getElementById("mq").value)}&top=20`);
      el.innerHTML = `<div class="panel-body"><div class="note">${fmt(r.stats.memories)} memories stored in the ${esc(r.stats.engine)} engine</div></div>` + (r.hits.length ? `<div class="scroll"><table class="t"><thead><tr><th>Memory</th><th>Tags</th><th class="num">Relevance</th><th class="num">Confidence</th></tr></thead><tbody>
        ${r.hits.map((h) => `<tr><td>${esc(h.content)}</td><td class="mono faint">${esc(h.tags.join(" "))}</td><td class="num">${Number(h.relevance).toFixed(2)}</td><td class="num">${Number(h.confidence).toFixed(2)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">Nothing stored matches. A memory is written the first time a rewrite proves a cause.</div>`);
    } catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  };
  document.getElementById("go").addEventListener("click", load);
  load();
}

async function paneHistory() {
  const pane = shell("history", "History");
  const runs = await api("/runs");
  state.runs = runs;
  pane.innerHTML = `<div class="panel"><div class="panel-head"><h2>${runs.length} runs</h2></div>${runs.length ? `<div class="scroll"><table class="t"><thead><tr><th>Run</th><th>When</th><th>Engines</th><th>Status</th><th class="num">Can move</th></tr></thead><tbody>
    ${runs.map((r) => `<tr class="click" data-r="${r.id}"><td class="mono">${esc(r.id)}${r.recorded ? ` <span class="tag v-blocked">recorded</span>` : ""}</td><td>${esc(when(r.created_at))}</td><td>${esc(engineLabel(r, "source"))} to ${esc(engineLabel(r, "target"))}</td><td>${esc(r.status)}</td><td class="num">${r.summary ? `${r.summary.movable} / ${r.summary.queries}` : ""}</td></tr>`).join("")}
  </tbody></table></div>` : `<div class="empty">No runs yet.</div>`}</div>`;
  pane.querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => { setRun(tr.dataset.r); navigate(`/app/queries?run=${encodeURIComponent(tr.dataset.r)}`); }));
}

// ------------------------------------------------------------------ router

async function render() {
  if (state.stream) { state.stream.close(); state.stream = null; }
  const p = location.pathname.replace(/\/+$/, "") || "/";
  window.scrollTo(0, 0);
  try {
    if (p === "/") return await landing();
    const m = p.match(/^\/app(?:\/([^/]+))?(?:\/([^/]+))?$/);
    if (!m) return await landing();
    const [, pane, arg] = m;
    if (!pane || pane === "overview") return await paneOverview();
    if (pane === "run") return await paneRun();
    if (pane === "queries") return await paneQueries();
    if (pane === "q" && arg) return await paneQuery(arg);
    if (pane === "tables") return await paneTables();
    if (pane === "receipt") return await paneReceipt();
    if (pane === "rows") return await paneRows();
    if (pane === "guard") return await paneGuard();
    if (pane === "workload") return await paneWorkload(arg);
    if (pane === "memory") return await paneMemory();
    if (pane === "history") return await paneHistory();
    return await paneOverview();
  } catch (e) {
    const pane = document.getElementById("pane") || root;
    pane.innerHTML = `<div class="panel"><div class="empty">${esc(e.message)}</div></div>`;
  }
}
render();
