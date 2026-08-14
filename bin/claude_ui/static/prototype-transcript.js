/* ===========================================================================
   PROTOTYPE — throwaway.

   Three variants of a session transcript viewer on the throwaway tab
   #prototype-transcript, switchable via ?variant=A|B|C:

     A — Reading view   a chat log you read top to bottom
     B — Inspector      three panes, keyboard-driven, raw JSON on the right
     C — Activity map   the shape of a session first, the text second

   Backed by prototype_transcript.py. No tests, no persistence, read-only.
   There is no production build to gate the switcher on — this dashboard only
   runs locally — so "prototype" in the tab name and the file names is the
   gate. Delete this file, prototype_transcript.py, and their four wiring
   lines to remove it.
   =========================================================================== */

const PT_TAB = "prototype-transcript";

const PT_VARIANTS = [
  { key: "A", name: "Reading view" },
  { key: "B", name: "Inspector" },
  { key: "C", name: "Activity map" },
];

const PT = {
  list: null,        // /api/prototype/sessions
  doc: null,         // /api/prototype/session for PT.path
  path: "",
  busy: false,
  err: "",
  sel: 0,            // selected entry index (variants B and C)
  raw: null,         // { i, raw } for the inspector's JSON pane
  q: "",             // session filter (variant B rail)
  hideMeta: true,    // housekeeping lines off by default
  kinds: null,       // Set of kinds to show, null = all
};

const PT_KINDS = ["user", "assistant", "thinking", "tool", "result", "meta",
                  "summary", "system"];

const PT_LABEL = { user: "You", assistant: "Claude", thinking: "Thinking",
                   tool: "Tool call", result: "Tool result", meta: "Housekeeping",
                   summary: "Summary", system: "System" };

/* ---------------------------------------------------------------- helpers */

const ptVariant = () => {
  const v = (new URLSearchParams(location.search).get("variant") || "A").toUpperCase();
  return PT_VARIANTS.some((x) => x.key === v) ? v : "A";
};

function ptSetVariant(key) {
  const p = new URLSearchParams(location.search);
  p.set("variant", key);
  // replaceState, not location.search — assigning that reloads the page and
  // throws away the fetched session.
  history.replaceState(null, "", location.pathname + "?" + p + location.hash);
  PT.raw = null;
  scrollTo(0, 0);   // the panes are viewport-height; a carried-over scroll cuts them off
  ptRender();
}

const ptTime = (ts) => {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit",
                                                    second: "2-digit" });
};
const ptDate = (ts) => {
  const d = new Date(ts || "");
  return isNaN(d) ? "" : d.toLocaleString([], { month: "short", day: "numeric",
                                                hour: "2-digit", minute: "2-digit" });
};
const ptBytes = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + " MB"
                     : n > 1024 ? Math.round(n / 1024) + " KB" : n + " B";
const ptNum = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M"
                   : n >= 1000 ? Math.round(n / 1000) + "k" : String(n || 0);
const ptDur = (a, b) => {
  const ms = new Date(b || "") - new Date(a || "");
  if (!ms || isNaN(ms)) return "";
  const m = Math.round(ms / 60000);
  return m < 60 ? m + " min" : (m / 60).toFixed(1) + " h";
};

const ptAllSessions = () =>
  (PT.list ? PT.list.projects : []).flatMap((p) =>
    p.sessions.map((s) => ({ ...s, project: p.tilde, cwd: p.cwd })));

function ptVisible(rows) {
  return rows.filter((r) => {
    if (PT.kinds && !PT.kinds.has(r.kind)) return false;
    if (PT.hideMeta && (r.kind === "meta" || (!r.chars && r.kind !== "summary")))
      return false;
    return true;
  });
}

/* ------------------------------------------------------------------ fetch */

async function ptLoadList() {
  PT.busy = true;
  try {
    PT.list = await api("/api/prototype/sessions");
    const first = ptAllSessions().find((s) => !s.subagent && s.msgs) || ptAllSessions()[0];
    if (!PT.path && first) PT.path = first.path;
  } catch (e) {
    PT.err = e.message;
  }
  PT.busy = false;
}

async function ptLoadDoc(start) {
  if (!PT.path) return;
  PT.busy = true;
  PT.raw = null;
  try {
    PT.doc = await api("/api/prototype/session?path=" + encodeURIComponent(PT.path)
                       + (start == null ? "" : "&start=" + start));
    PT.err = "";
    if (start == null) {
      // the last *visible* row: transcripts end on housekeeping lines, and
      // opening on one looks like the viewer failed to load anything
      const rows = ptVisible(PT.doc.index);
      PT.sel = rows.length ? rows[rows.length - 1].i : PT.doc.total - 1;
    }
  } catch (e) {
    PT.err = e.message;
    PT.doc = null;
  }
  PT.busy = false;
}

function ptOpen(path) {
  PT.path = path;
  PT.doc = null;
  PT.sel = 0;
  ptRender();
}

/* Entries live in a window; selecting outside it pulls a new one. */
async function ptSelect(i) {
  PT.sel = i;
  PT.raw = null;
  const d = PT.doc;
  if (d && (i < d.start || i >= d.start + d.entries.length)) {
    await ptLoadDoc(Math.max(0, i - 60));
    PT.sel = i;
  }
  ptRender();
}

/* The reading view grows upward: a fresh window would drop the messages you
   are already looking at, which reads as the viewer losing them. */
async function ptLoadEarlier() {
  const d = PT.doc;
  if (!d || !d.start) return;
  const start = Math.max(0, d.start - 300);
  const older = await api("/api/prototype/session?path=" + encodeURIComponent(PT.path)
                          + "&start=" + start + "&count=" + (d.start - start));
  d.entries = older.entries.filter((e) => e.i < d.start).concat(d.entries);
  d.start = start;
  ptRender();
}

const ptEntry = (i) => (PT.doc ? PT.doc.entries.find((e) => e.i === i) : null);

async function ptLoadRaw(i) {
  PT.raw = { i, raw: "loading…" };
  ptRender();
  try {
    PT.raw = await api("/api/prototype/entry?path=" + encodeURIComponent(PT.path)
                       + "&i=" + i);
  } catch (e) {
    PT.raw = { i, raw: e.message };
  }
  ptRender();
}

/* ------------------------------------------------------------------ pieces */

const ptChip = (text, kind, on, onclick) =>
  el("button.pt-chip", { class: (on ? "on " : "") + "k-" + (kind || "meta"),
                         text, onclick });

const ptTag = (text, kind) =>
  el("span.pt-tag", { class: "k-" + (kind || "meta"), text });

function ptSessionOption(s) {
  const when = ptDate(s.last_ts || "");
  return (s.title || s.prompt || s.short) + (when ? "  ·  " + when : "")
         + "  ·  " + s.msgs + " msgs" + (s.subagent ? "  ·  subagent" : "");
}

/* One tool call and, if it followed, its result — the pair reads as one thing
   in the reading view even though the transcript stores them two lines apart. */
function ptToolCard(b) {
  const box = el("div.pt-tool");
  const head = el("button.pt-tool-head", {
    onclick: () => box.classList.toggle("open") },
    el("span.pt-tool-name", { text: b.name }),
    el("span.pt-tool-brief", { text: b.brief || "" }));
  const body = el("pre.pt-pre.pt-tool-body", { text: b.input || "" });
  box.append(head, body);
  return box;
}

function ptBlock(b) {
  if (b.kind === "text")
    return el("div.pt-text", { text: b.text + (b.over ? "\n… +" + b.over + " chars" : "") });
  if (b.kind === "thinking") {
    const d = el("details.pt-think", {},
      el("summary", { text: b.redacted ? "Thinking (redacted)"
                                       : "Thinking · " + (b.text.length + b.over) + " chars" }));
    if (!b.redacted) d.append(el("div.pt-text.muted", { text: b.text }));
    return d;
  }
  if (b.kind === "tool_use") return ptToolCard(b);
  if (b.kind === "tool_result") {
    const box = el("div.pt-result", { class: b.error ? "err" : "" });
    const lines = (b.text || "").split("\n");
    const head = el("button.pt-result-head", {
      onclick: () => box.classList.toggle("open") },
      el("span", { text: (b.error ? "error · " : "result · ") + lines.length + " lines" }),
      el("span.pt-result-peek", { text: lines[0] ? lines[0].slice(0, 90) : "" }));
    box.append(head, el("pre.pt-pre.pt-result-body", {
      text: b.text + (b.over ? "\n… +" + b.over + " chars" : "") }));
    return box;
  }
  if (b.kind === "image") return el("div.pt-note", { text: "[image]" });
  return el("pre.pt-pre", { text: b.text || "" });
}

/* ------------------------------------------------------ A — Reading view */

function ptVariantA(root) {
  const d = PT.doc;
  const bar = el("div.pt-readbar");
  bar.append(ptSessionPicker());
  root.append(bar);

  const head = el("div.pt-readhead", {},
    el("h2.pt-title", { text: d.title || d.prompt || d.id.slice(0, 8) }),
    el("div.pt-sub", { text: [d.cwd_tilde, d.branch && "branch " + d.branch,
                              d.models[0] && d.models[0].model,
                              ptDate(d.first_ts), ptDur(d.first_ts, d.last_ts),
                              ptNum(d.totals.out) + " out",
                              d.total + " lines"].filter(Boolean).join("  ·  ") }));
  root.append(head);

  const tools = el("div.pt-toolbar-row", {},
    el("label.pt-check", {},
      el("input", { type: "checkbox", checked: !PT.hideMeta,
                    onchange: (e) => { PT.hideMeta = !e.target.checked; ptRender(); } }),
      el("span", { text: "show housekeeping lines" })));
  if (d.start > 0)
    tools.append(el("button.btn.btn-sm", {
      text: "Load earlier (" + d.start + " above)",
      onclick: ptLoadEarlier }));
  root.append(tools);

  const feed = el("div.pt-feed");
  const byTool = {};   // tool_use id -> the card its result belongs under
  for (const e of ptVisible(d.entries)) {
    const isUser = e.kind === "user";
    const row = el("div.pt-msg", { class: "k-" + e.kind + (e.side ? " side" : "") });
    row.append(el("div.pt-msg-head", {},
      el("span.pt-who", { text: e.agent || PT_LABEL[e.kind] || e.type }),
      e.side && ptTag("subagent", "meta"),
      el("span.pt-when", { text: ptTime(e.ts) }),
      e.out ? el("span.pt-when", { text: ptNum(e.out) + " out" }) : null));
    const body = el("div.pt-msg-body");
    for (const b of e.blocks) {
      if (b.kind === "tool_result" && byTool[b.id]) {
        // fold the result into the call it answers rather than starting a
        // new bubble two turns down the page
        byTool[b.id].append(ptBlock(b));
        continue;
      }
      const node = ptBlock(b);
      if (b.kind === "tool_use") byTool[b.id] = node;
      body.append(node);
    }
    if (!body.childNodes.length && !isUser) continue;
    row.append(body);
    feed.append(row);
  }
  root.append(feed);
}

/* --------------------------------------------------------- B — Inspector */

function ptVariantB(root) {
  const d = PT.doc;
  const wrap = el("div.pt-panes");

  /* rail: every session on the machine */
  const rail = el("div.pt-rail");
  rail.append(el("input.input.pt-search", {
    type: "search", placeholder: "filter sessions…", value: PT.q,
    oninput: (e) => { PT.q = e.target.value; ptRender(); } }));
  const q = PT.q.toLowerCase();
  const railList = el("div.pt-rail-list");
  for (const p of PT.list.projects) {
    const rows = p.sessions.filter((s) =>
      !q || (p.tilde + " " + s.title + " " + s.prompt + " " + s.id).toLowerCase().includes(q));
    if (!rows.length) continue;
    railList.append(el("div.pt-rail-proj", { text: p.tilde }));
    for (const s of rows)
      railList.append(el("button.pt-rail-row", {
        class: s.path === PT.path ? "on" : "", onclick: () => ptOpen(s.path) },
        el("span.pt-rail-title", { text: s.title || s.prompt || s.short }),
        el("span.pt-rail-meta", {
          text: [s.msgs + " msgs", ptBytes(s.bytes), s.subagent ? "sub" : "",
                 ptDate(s.last_ts)].filter(Boolean).join(" · ") })));
  }
  rail.append(railList);

  /* index: one dense line per transcript line */
  const idx = el("div.pt-index");
  const chips = el("div.pt-chips");
  for (const k of PT_KINDS)
    chips.append(ptChip(k, k, !PT.kinds || PT.kinds.has(k), () => {
      if (!PT.kinds) PT.kinds = new Set(PT_KINDS);
      PT.kinds.has(k) ? PT.kinds.delete(k) : PT.kinds.add(k);
      if (PT.kinds.size === PT_KINDS.length) PT.kinds = null;
      ptRender();
    }));
  idx.append(chips);
  const idxList = el("div.pt-index-list");
  for (const r of ptVisible(d.index)) {
    idxList.append(el("button.pt-idx-row", {
      class: r.i === PT.sel ? "on" : "", id: "pt-idx-" + r.i,
      onclick: () => ptSelect(r.i) },
      el("span.pt-idx-n", { text: String(r.i) }),
      el("span.pt-idx-k", { class: "k-" + r.kind, text: r.kind[0].toUpperCase() }),
      el("span.pt-idx-t", { text: r.tools.join(", ") || PT_LABEL[r.kind] || r.type }),
      el("span.pt-idx-c", { text: r.out ? ptNum(r.out) : ptNum(r.chars) })));
  }
  idx.append(idxList);

  /* detail: the selected line, blocks then raw JSON */
  const det = el("div.pt-detail");
  const e = ptEntry(PT.sel);
  if (!e) {
    det.append(el("div.muted", { text: "Select a line. j / k move, r loads raw JSON." }));
  } else {
    det.append(el("div.pt-det-head", {},
      ptTag(e.kind, e.kind),
      el("span.pt-mono", { text: "#" + e.i + " · " + e.type }),
      el("span.pt-mono.muted", { text: ptTime(e.ts) }),
      e.model && el("span.pt-mono.muted", { text: e.model }),
      e.side && ptTag(e.agent || "subagent", "meta"),
      el("span.spring"),
      el("button.btn.btn-sm", { text: PT.raw && PT.raw.i === e.i ? "Hide raw" : "Raw JSON",
        onclick: () => { if (PT.raw && PT.raw.i === e.i) { PT.raw = null; ptRender(); }
                         else ptLoadRaw(e.i); } })));
    if (e.in || e.out || e.cr)
      det.append(el("div.pt-det-usage", {
        text: ["in " + ptNum(e.in), "out " + ptNum(e.out),
               "cache read " + ptNum(e.cr)].join("   ") }));
    const body = el("div.pt-det-body");
    for (const b of e.blocks) body.append(ptBlock(b));
    det.append(body);
    if (PT.raw && PT.raw.i === e.i)
      det.append(el("pre.pt-pre.pt-raw", { text: PT.raw.raw }));
  }

  wrap.append(rail, idx, det);
  root.append(wrap);
  requestAnimationFrame(() =>
    document.getElementById("pt-idx-" + PT.sel)?.scrollIntoView({ block: "nearest" }));
}

/* ------------------------------------------------------ C — Activity map */

function ptVariantC(root) {
  const d = PT.doc;

  const strip = el("div.pt-strip");
  for (const s of ptAllSessions().slice(0, 14))
    strip.append(el("button.pt-strip-card", {
      class: s.path === PT.path ? "on" : "", onclick: () => ptOpen(s.path) },
      el("span.pt-strip-title", { text: s.title || s.prompt || s.short }),
      el("span.pt-strip-meta", { text: s.project }),
      el("span.pt-strip-meta", { text: ptDate(s.last_ts) + " · " + s.msgs + " msgs" })));
  root.append(strip);

  const stats = el("div.pt-stats");
  const stat = (v, l) => el("div.pt-stat", {}, el("b", { text: v }),
                            el("span", { text: l }));
  stats.append(stat(String(d.total), "lines"),
               stat(ptDur(d.first_ts, d.last_ts) || "—", "elapsed"),
               stat(ptNum(d.totals.out), "output tokens"),
               stat(ptNum(d.totals.cr), "cache reads"),
               stat(ptBytes(d.bytes), "on disk"),
               stat(String(d.tools.reduce((n, t) => n + t.n, 0)), "tool calls"));
  root.append(stats);

  /* the ribbon: every line in the session, tallest where the most text is */
  const rows = ptVisible(d.index);
  const max = Math.max(1, ...rows.map((r) => Math.log10(1 + r.chars)));
  const ribbon = el("div.pt-ribbon");
  for (const r of rows) {
    const h = Math.max(6, Math.round(Math.log10(1 + r.chars) / max * 100));
    ribbon.append(el("button.pt-bar", {
      class: "k-" + r.kind + (r.i === PT.sel ? " on" : ""),
      style: { height: h + "%" }, onclick: () => ptSelect(r.i),
      title: "#" + r.i + " " + r.kind + (r.tools.length ? " " + r.tools.join(",") : "")
             + " · " + r.chars + " chars · " + ptTime(r.ts) }));
  }
  root.append(el("div.pt-ribbon-wrap", {}, ribbon));

  const legend = el("div.pt-chips");
  const counts = {};
  for (const r of d.index) counts[r.kind] = (counts[r.kind] || 0) + 1;
  for (const k of PT_KINDS.filter((k) => counts[k]))
    legend.append(ptChip(k + " " + counts[k], k, !PT.kinds || PT.kinds.has(k), () => {
      if (!PT.kinds) PT.kinds = new Set(PT_KINDS);
      PT.kinds.has(k) ? PT.kinds.delete(k) : PT.kinds.add(k);
      if (PT.kinds.size === PT_KINDS.length) PT.kinds = null;
      ptRender();
    }));
  root.append(legend);

  const cols = el("div.pt-cols");
  const toolBox = el("div.pt-toolhist", {}, el("div.pt-colhead", { text: "Tools used" }));
  const top = d.tools.slice(0, 12);
  const tmax = Math.max(1, ...top.map((t) => t.n));
  for (const t of top)
    toolBox.append(el("div.pt-hist-row", {},
      el("span.pt-hist-name", { text: t.name }),
      el("span.pt-hist-bar", { style: { width: (t.n / tmax * 100) + "%" } }),
      el("span.pt-hist-n", { text: String(t.n) })));
  cols.append(toolBox);

  const sel = ptEntry(PT.sel);
  const detail = el("div.pt-mapdetail", {},
    el("div.pt-colhead", { text: sel ? "#" + PT.sel + " · " + sel.kind + " · " + ptTime(sel.ts)
                                     : "Click the ribbon" }));
  if (sel) for (const b of sel.blocks) detail.append(ptBlock(b));
  cols.append(detail);
  root.append(cols);
}

/* ------------------------------------------------------------- switcher */

function ptSwitcher() {
  const cur = ptVariant();
  const i = PT_VARIANTS.findIndex((v) => v.key === cur);
  const go = (d) => ptSetVariant(PT_VARIANTS[(i + d + PT_VARIANTS.length) % PT_VARIANTS.length].key);
  return el("div.pt-switch", {},
    el("button.pt-switch-arrow", { text: "‹", title: "Previous variant (←)",
                                   onclick: () => go(-1) }),
    el("span.pt-switch-label", { text: cur + " — " + PT_VARIANTS[i].name }),
    el("button.pt-switch-arrow", { text: "›", title: "Next variant (→)",
                                   onclick: () => go(1) }),
    el("span.pt-switch-tag", { text: "prototype" }));
}

function ptSessionPicker() {
  const sessions = ptAllSessions();
  const byProject = PT.list.projects;
  const cur = sessions.find((s) => s.path === PT.path);
  const projSel = el("select.select.pt-select", {
    onchange: (e) => {
      const p = byProject.find((x) => x.cwd === e.target.value);
      if (p && p.sessions[0]) ptOpen(p.sessions[0].path);
    } });
  for (const p of byProject)
    projSel.append(el("option", { value: p.cwd, text: p.tilde + "  (" + p.count + ")",
                                  selected: cur && cur.cwd === p.cwd }));
  const sessSel = el("select.select.pt-select.wide", {
    onchange: (e) => ptOpen(e.target.value) });
  for (const s of (byProject.find((p) => cur && p.cwd === cur.cwd) || byProject[0]).sessions)
    sessSel.append(el("option", { value: s.path, text: ptSessionOption(s),
                                  selected: s.path === PT.path }));
  return el("div.pt-picker", {}, projSel, sessSel);
}

/* ---------------------------------------------------------------- render */

async function renderPrototypeTranscript() {
  const view = document.getElementById(PT_TAB + "view");
  ptStyles();
  if (!PT.list && !PT.busy) {
    view.innerHTML = "";
    view.append(el("div.muted", { text: "Reading the transcripts under ~/.claude/projects…" }));
    await ptLoadList();
    if (TAB !== PT_TAB) return;
  }
  if (PT.list && PT.path && (!PT.doc || PT.doc.path !== PT.path) && !PT.busy) {
    await ptLoadDoc();
    if (TAB !== PT_TAB) return;
  }
  ptRender();
}

function ptRender() {
  const view = document.getElementById(PT_TAB + "view");
  view.innerHTML = "";
  view.append(el("div.view-head", {
    text: "PROTOTYPE — three transcript viewers over the same sessions. "
        + "Arrow keys or the bar at the bottom switch between them. Nothing "
        + "here is wired to write anything." }));
  if (PT.err) view.append(el("div.pt-err", { text: PT.err }));
  if (!PT.list) { view.append(el("div.muted", { text: "Loading…" })); return; }
  if (!PT.list.total) {
    view.append(emptyState("No transcripts", "Looked in " + PT.list.dir + ".", "layers"));
    return;
  }
  const root = el("div.pt-root", { class: "v" + ptVariant() });
  if (PT.doc) {
    const v = ptVariant();
    if (v === "A") ptVariantA(root);
    else if (v === "B") ptVariantB(root);
    else ptVariantC(root);
  } else {
    root.append(el("div.muted", { text: PT.busy ? "Reading session…" : "No session loaded." }));
  }
  view.append(root, ptSwitcher());
}

/* Keyboard: ← → cycle variants, j/k walk the index (B and C), r shows raw. */
addEventListener("keydown", (e) => {
  if (TAB !== PT_TAB || typeof PAL !== "undefined" && PAL) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT"
            || t.isContentEditable)) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault();
    const i = PT_VARIANTS.findIndex((v) => v.key === ptVariant());
    const d = e.key === "ArrowRight" ? 1 : -1;
    ptSetVariant(PT_VARIANTS[(i + d + PT_VARIANTS.length) % PT_VARIANTS.length].key);
  } else if ((e.key === "j" || e.key === "k") && PT.doc) {
    e.preventDefault();
    const rows = ptVisible(PT.doc.index);
    const at = rows.findIndex((r) => r.i === PT.sel);
    const next = rows[Math.min(rows.length - 1, Math.max(0, at + (e.key === "j" ? 1 : -1)))];
    if (next) ptSelect(next.i);
  } else if (e.key === "r" && PT.doc) {
    ptLoadRaw(PT.sel);
  }
});

/* ------------------------------------------------------------------- css --
   Injected rather than added to app.css so removing the prototype is a file
   delete. Colours are theme tokens only, so both themes and all four
   families work. */

function ptStyles() {
  if (document.getElementById("pt-css")) return;
  document.head.append(el("style", { id: "pt-css", text: `
.pt-root { --k-user: var(--primary); --k-assistant: var(--info);
  --k-tool: var(--warning); --k-result: var(--muted-foreground);
  --k-thinking: var(--accent-foreground); --k-meta: var(--border);
  --k-summary: var(--success); --k-system: var(--destructive);
  padding-bottom: 4.5rem; }
.pt-root .k-user { --k: var(--k-user); } .pt-root .k-assistant { --k: var(--k-assistant); }
.pt-root .k-tool { --k: var(--k-tool); } .pt-root .k-result { --k: var(--k-result); }
.pt-root .k-thinking { --k: var(--k-thinking); } .pt-root .k-meta { --k: var(--k-meta); }
.pt-root .k-summary { --k: var(--k-summary); } .pt-root .k-system { --k: var(--k-system); }
.pt-err { color: var(--destructive); margin: .5rem 0; }
/* components.css centres every <button>; these are rows, not buttons-with-a-label */
.pt-tool-head, .pt-result-head, .pt-rail-row, .pt-idx-row, .pt-strip-card,
.pt-bar { justify-content: flex-start; }
.pt-mono, .pt-pre { font-family: var(--font-mono); font-size: .75rem; }
.pt-tag { font-size: .6875rem; padding: .05rem .4rem; border-radius: 999px;
  background: color-mix(in oklab, var(--k, var(--muted)) 18%, transparent);
  color: var(--foreground); border: 1px solid color-mix(in oklab, var(--k) 45%, transparent); }
.pt-pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0;
  background: var(--muted); border-radius: var(--radius-sm); padding: .5rem .625rem;
  max-height: 22rem; overflow: auto; }
.pt-chips { display: flex; gap: .375rem; flex-wrap: wrap; margin: .5rem 0; }
.pt-chip { font-size: .75rem; padding: .15rem .55rem; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--border); background: transparent; color: var(--muted-foreground); }
.pt-chip.on { background: color-mix(in oklab, var(--k, var(--muted)) 22%, transparent);
  color: var(--foreground); border-color: color-mix(in oklab, var(--k) 55%, transparent); }
.pt-select { max-width: 22rem; } .pt-select.wide { max-width: 34rem; flex: 1 1 20rem; }
.pt-picker { display: flex; gap: .5rem; flex-wrap: wrap; width: 100%; }
.pt-check { display: flex; gap: .375rem; align-items: center; font-size: .8125rem;
  color: var(--muted-foreground); }
.pt-toolbar-row { display: flex; gap: .75rem; align-items: center; margin: .5rem 0 1rem; }

/* A — reading view */
.pt-root.vA { max-width: 54rem; margin: 0 auto; }
.pt-readbar { position: sticky; top: var(--header-h); z-index: 5; padding: .5rem 0;
  background: var(--background); }
.pt-readhead { border-bottom: 1px solid var(--border); padding-bottom: .75rem; }
.pt-title { font-size: 1.25rem; font-weight: 650; margin: 0; }
.pt-sub { color: var(--muted-foreground); font-size: .8125rem; margin-top: .25rem; }
.pt-feed { display: flex; flex-direction: column; gap: 1.5rem; }
.pt-msg { border-left: 3px solid color-mix(in oklab, var(--k) 70%, transparent);
  padding-left: .875rem; }
.pt-msg.k-user { background: color-mix(in oklab, var(--primary) 7%, transparent);
  border-radius: 0 var(--radius) var(--radius) 0; padding: .625rem .875rem .625rem .875rem; }
.pt-msg.side { opacity: .82; }
.pt-msg-head { display: flex; gap: .5rem; align-items: center; margin-bottom: .375rem; }
.pt-who { font-weight: 600; font-size: .8125rem; }
.pt-when { color: var(--muted-foreground); font-size: .75rem; }
.pt-text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6;
  font-size: .9375rem; }
.pt-think { margin: .5rem 0; font-size: .8125rem; color: var(--muted-foreground); }
.pt-think summary { cursor: pointer; }
.pt-tool { margin: .5rem 0; border: 1px solid var(--border); border-radius: var(--radius-sm);
  overflow: hidden; }
.pt-tool-head { display: flex; gap: .5rem; align-items: baseline; width: 100%;
  text-align: left; background: color-mix(in oklab, var(--warning) 10%, transparent);
  border: 0; cursor: pointer; padding: .375rem .625rem; color: var(--foreground); }
.pt-tool-name { font-weight: 600; font-size: .8125rem; }
.pt-tool-brief { font-family: var(--font-mono); font-size: .75rem;
  color: var(--muted-foreground); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.pt-tool-body, .pt-result-body { display: none; border-radius: 0; }
.pt-tool.open .pt-tool-body, .pt-result.open .pt-result-body { display: block; }
.pt-result { border-top: 1px dashed var(--border); }
.pt-result-head { display: flex; gap: .5rem; width: 100%; text-align: left; border: 0;
  background: transparent; cursor: pointer; padding: .375rem .625rem;
  color: var(--muted-foreground); font-size: .75rem; font-family: var(--font-mono); }
.pt-result.err .pt-result-head { color: var(--destructive); }
.pt-result-peek { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pt-note { font-size: .8125rem; color: var(--muted-foreground); }

/* B — inspector */
.pt-panes { display: grid; grid-template-columns: 16rem 20rem minmax(0, 1fr);
  gap: .75rem; height: calc(100vh - var(--header-h) - 11rem); }
.pt-rail, .pt-index, .pt-detail { border: 1px solid var(--border);
  border-radius: var(--radius); overflow: auto; padding: .5rem; }
.pt-search { width: 100%; margin-bottom: .5rem; }
.pt-rail-proj { font-size: .6875rem; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted-foreground); margin: .5rem 0 .25rem; }
.pt-rail-row, .pt-idx-row { display: flex; width: 100%; text-align: left; border: 0;
  background: transparent; cursor: pointer; color: var(--foreground); border-radius: var(--radius-sm); }
.pt-rail-row { flex-direction: column; align-items: flex-start; gap: .1rem;
  padding: .3rem .4rem; min-width: 0; }
.pt-rail-row:hover, .pt-idx-row:hover { background: var(--muted); }
.pt-rail-row.on, .pt-idx-row.on { background: color-mix(in oklab, var(--primary) 18%, transparent); }
.pt-rail-title { font-size: .8125rem; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; max-width: 100%; }
.pt-rail-meta { font-size: .6875rem; color: var(--muted-foreground); }
.pt-index-list { display: flex; flex-direction: column; }
.pt-idx-row { gap: .5rem; align-items: center; padding: .2rem .35rem;
  font-family: var(--font-mono); font-size: .75rem; }
.pt-idx-n { color: var(--muted-foreground); min-width: 2.5rem; }
.pt-idx-k { width: 1.1rem; height: 1.1rem; display: grid; place-items: center;
  border-radius: var(--radius-sm); font-size: .625rem; color: var(--background);
  background: var(--k); }
.pt-idx-t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pt-idx-c { color: var(--muted-foreground); }
.pt-det-head { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap;
  border-bottom: 1px solid var(--border); padding-bottom: .5rem; margin-bottom: .5rem; }
.pt-det-usage { font-family: var(--font-mono); font-size: .75rem;
  color: var(--muted-foreground); margin-bottom: .5rem; }
.pt-det-body { display: flex; flex-direction: column; gap: .5rem; }
.pt-raw { margin-top: .75rem; max-height: 30rem; }
.pt-panes .spring { flex: 1; }

/* C — activity map */
.pt-strip { display: flex; gap: .5rem; overflow-x: auto; padding-bottom: .5rem; }
/* min-height: fit-content because a <button> flex item does not grow to its
   own children's height on its own — the third line gets clipped without it */
.pt-strip-card { flex: 0 0 13rem; max-width: 13rem; min-width: 0; overflow: hidden;
  min-height: fit-content;
  display: flex; flex-direction: column; gap: .15rem; align-items: flex-start;
  text-align: left; cursor: pointer; padding: .5rem .625rem; border-radius: var(--radius);
  border: 1px solid var(--border); background: var(--card); color: var(--foreground); }
.pt-strip-card.on { border-color: var(--primary);
  background: color-mix(in oklab, var(--primary) 12%, transparent); }
/* overflow:hidden gives these an automatic minimum size of 0, so as flex
   children they collapse to nothing instead of setting the card's height */
.pt-strip-card > *, .pt-rail-row > * { flex: none; }
.pt-strip-title { font-size: .8125rem; font-weight: 600; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.pt-strip-meta { font-size: .6875rem; color: var(--muted-foreground); }
.pt-stats { display: flex; gap: 1.5rem; flex-wrap: wrap; margin: 1rem 0 .5rem; }
.pt-stat { display: flex; flex-direction: column; }
.pt-stat b { font-size: 1.25rem; } .pt-stat span { font-size: .75rem; color: var(--muted-foreground); }
.pt-ribbon-wrap { border: 1px solid var(--border); border-radius: var(--radius);
  padding: .5rem; background: var(--card); }
.pt-ribbon { display: flex; align-items: flex-end; gap: 1px; height: 7rem;
  overflow-x: auto; }
.pt-bar { flex: 1 0 3px; min-width: 3px; border: 0; padding: 0; cursor: pointer;
  background: var(--k); opacity: .75; border-radius: 1px 1px 0 0; }
.pt-bar:hover { opacity: 1; }
.pt-bar.on { outline: 2px solid var(--foreground); opacity: 1; }
.pt-cols { display: grid; grid-template-columns: 18rem minmax(0, 1fr); gap: .75rem;
  margin-top: .75rem; }
.pt-toolhist, .pt-mapdetail { border: 1px solid var(--border); border-radius: var(--radius);
  padding: .625rem; }
.pt-mapdetail { max-height: 32rem; overflow: auto; display: flex; flex-direction: column;
  gap: .5rem; }
.pt-colhead { font-size: .75rem; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted-foreground); margin-bottom: .5rem; }
.pt-hist-row { display: flex; gap: .5rem; align-items: center; font-size: .75rem;
  margin-bottom: .25rem; }
.pt-hist-name { flex: 0 0 7rem; font-family: var(--font-mono); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.pt-hist-bar { height: .5rem; background: var(--primary); border-radius: 999px; }
.pt-hist-n { color: var(--muted-foreground); }

/* the switcher, deliberately not part of the design being judged */
.pt-switch { position: fixed; bottom: 1.25rem; left: 50%; transform: translateX(-50%);
  z-index: 60; display: flex; align-items: center; gap: .25rem; padding: .3rem .4rem;
  border-radius: 999px; background: var(--foreground); color: var(--background);
  box-shadow: var(--shadow-lg); }
.pt-switch-arrow { border: 0; background: transparent; color: inherit; cursor: pointer;
  font-size: 1.1rem; line-height: 1; padding: .1rem .5rem; border-radius: 999px; }
.pt-switch-arrow:hover { background: color-mix(in oklab, var(--background) 25%, transparent); }
.pt-switch-label { font-size: .8125rem; font-weight: 600; min-width: 11rem;
  text-align: center; }
.pt-switch-tag { font-size: .625rem; text-transform: uppercase; letter-spacing: .08em;
  opacity: .7; padding-right: .4rem; }
` }));
}
