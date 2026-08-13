/* ===========================================================================
   app.js — data, routing, and the views.

   Component primitives (el/icon/toast/modal/openMenu/filterSelect/…) come from
   ui.js, which is loaded first. Nothing in here builds a toast, dialog, menu
   or combobox by hand, and nothing hard-codes a colour: every surface is a
   class from components.css over a token from theme.css.
   =========================================================================== */

let DATA = { items: {}, config_files: [], config_dir: "", settings: {}, mcp: {}, statusline: {} };

// skills stays in ITEM_TABS: the deep link #skills/<name>/<file> has to keep
// opening the editor, and that is what ITEM_TABS means to routeFromHash().
// What it is *not* any more is a plain inventory — hence the second list.
const ITEM_TABS = ["skills", "commands", "agents", "output-styles"];
const INVENTORY_TABS = ["commands", "agents", "output-styles"];
// the types core.ITEM_TYPES calls kind "dir": a folder of files, not one file
const DIR_TYPES = new Set(["skills"]);
const TABS = [...ITEM_TABS, "mcp", "statusline", "projects", "setup", "settings", "insight", "context", "costs", "doctor", "backup"];

/* The Skills tab's three segments. Three tabs used to answer overlapping
   questions and could not answer each other's: what is in your skills folder,
   what an installed plugin brings, and what exists that you do not have yet.
   They are one tab with a segment each now, so a skill from a plugin lists
   beside one of your own. */
const SKILL_SEGS = [{ key: "installed", label: "Installed" },
                    { key: "plugins", label: "Plugins" },
                    { key: "browse", label: "Browse" }];
// old bookmarks: #plugins and #discover land on the segment that replaced them
const LEGACY_TABS = { plugins: "plugins", discover: "browse" };
let SEG = "installed";
const onSeg = (s) => TAB === "skills" && SEG === s;

const TAB_META = {
  "skills": { icon: "sparkles", label: "Skills" },
  "commands": { icon: "terminal", label: "Commands" },
  "agents": { icon: "bot", label: "Agents" },
  "output-styles": { icon: "droplet", label: "Output styles" },
  "mcp": { icon: "server", label: "MCP" },
  "statusline": { icon: "panel", label: "Statusline" },
  "projects": { icon: "folder", label: "Projects" },
  "setup": { icon: "wrench", label: "Setup" },
  "settings": { icon: "settings", label: "Settings" },
  "insight": { icon: "chart", label: "Insight" },
  "context": { icon: "layers", label: "Context" },
  "costs": { icon: "dollar", label: "Costs" },
  "doctor": { icon: "pulse", label: "Doctor" },
  "backup": { icon: "archive", label: "Backup" },
};

/* The hash carries a query string as well as a path, so it cannot be read with
   a bare slice(1) — `skills?seg=browse` is not an unknown tab, it is the Browse
   segment, and reading it as the former silently drops the segment on reload. */
const parseHash = () => {
  const [head, query] = location.hash.slice(1).split("?");
  return {
    segs: head.split("/").filter(Boolean).map((s) => {
      try { return decodeURIComponent(s); } catch (e) { return s; }
    }),
    params: new URLSearchParams(query || ""),
  };
};

let TAB = "skills";
{
  const { segs, params } = parseHash();
  if (LEGACY_TABS[segs[0]] && segs.length === 1) SEG = LEGACY_TABS[segs[0]];
  else if (TABS.includes(segs[0])) {
    TAB = segs[0];
    if (TAB === "skills" && SKILL_SEGS.some((s) => s.key === params.get("seg")))
      SEG = params.get("seg");
  }
}
let IQ = "";  // inventory filter

async function api(path, body) {
  const res = await fetch(path, body
    ? { method: "POST",
        headers: { "content-type": "application/json", "x-claude-ui": TOKEN },
        body: JSON.stringify(body) }
    : {});
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || res.statusText);
  return json;
}

/* `seg` names a Skills segment. Omitting it on a jump to Skills means
   Installed: the tab bar is the tab, and the segment resets when you leave and
   come back, which is what makes `#skills` mean one thing. A deep link that
   wants another segment says so, and routeFromHash() reads it. */
function goTab(t, seg) {
  if (LEGACY_TABS[t]) { seg = LEGACY_TABS[t]; t = "skills"; }
  if (!TABS.includes(t) || (EDITING && !confirmDiscard())) return;
  EDITING = null;   // leaving for a tab closes the editor
  closeNewStyle();  // …and discards a half-filled new-style form
  TAB = t;
  if (t === "skills")
    SEG = SKILL_SEGS.some((s) => s.key === seg) ? seg : "installed";
  location.hash = currentHash();
  render();
}

const goSeg = (seg) => goTab("skills", seg);

/* ---------------------------------------------------------------- routing --
   The hash carries the open file, not just the tab: #skills/pdf/SKILL.md and
   #file/<path>, both with an optional ?line=. That is what makes "click a
   doctor finding" survive a back button and a reload.

   A Skills segment is a query param, not a path segment: routeFromHash() reads
   `<item tab>/<something>` as "open this item's editor", so #skills/browse
   would open a skill named "browse" — and a skill may legally be called that.

   Four things have to agree on the string exactly — this function, goTab(),
   routeFromHash() and the hashchange listener — or a segment click loops: the
   hash is set, the listener sees a mismatch, it re-routes, and any open editor
   prompts to discard. */

const currentHash = () => {
  if (!EDITING)
    return TAB === "skills" && SEG !== "installed" ? "skills?seg=" + SEG : TAB;
  const enc = encodeURIComponent;
  // A project's item has the same type and name as one of yours, so the root
  // rides along as a query param — without it the hash names two different
  // files and the back button opens whichever the config dir happens to hold.
  // `off` goes with it: which side of disabled/ an item is on is read from
  // DATA on the way back in, and DATA has never heard of a project's items.
  const q = EDITING.root
    ? "?root=" + enc(EDITING.root) + (EDITING.enabled === false ? "&off=1" : "")
    : "";
  return EDITING.item
    ? EDITING.type + "/" + enc(EDITING.name) + "/" + enc(EDITING.file || "") + q
    : "file/" + enc(EDITING.abs || EDITING.path);
};

function edSyncHash() {
  const want = currentHash();
  if (location.hash.slice(1) !== want) location.hash = want;
}

function routeFromHash() {
  const { segs, params } = parseHash();
  const line = +params.get("line") || 0;
  const locate = line ? { line } : null;
  const root = params.get("root") || null;

  if (segs[0] === "file" && segs[1]) return openPath(segs[1], locate);
  if (ITEM_TABS.includes(segs[0]) && segs.length >= 2) {
    const it = root ? null
      : ((DATA.items || {})[segs[0]] || []).find((i) => i.name === segs[1]);
    const enabled = root ? params.get("off") !== "1" : (it ? it.enabled : true);
    return openItemEditor(segs[0], segs[1], segs[2] || null, enabled, locate, root);
  }
  if (EDITING) {
    if (!confirmDiscard()) { edSyncHash(); return; }
    EDITING = null;
  }
  // A bare #plugins or #discover is an old bookmark: land on the segment that
  // replaced it and rewrite the hash in place, so the back button goes where
  // it came from rather than bouncing off a redirect. #plugins/foo is not a
  // legacy hash and falls through to the default below.
  if (LEGACY_TABS[segs[0]] && segs.length === 1) {
    TAB = "skills";
    SEG = LEGACY_TABS[segs[0]];
    render();
    location.replace("#" + currentHash());
    return;
  }
  TAB = TABS.includes(segs[0]) ? segs[0] : "skills";
  if (TAB === "skills") {
    SEG = SKILL_SEGS.some((s) => s.key === params.get("seg"))
      ? params.get("seg") : "installed";
    // #skills?seg=browse&q=… is addressable, so a search can be linked to.
    // It is never written back: DQ changes on every keystroke, and a hash
    // that follows the cursor fills the back button with noise.
    if (SEG === "browse" && params.get("q") !== null) {
      DQ = params.get("q");
      DHITS = null;
      DSH = null;
      DSHQ = "";
    }
  }
  render();
}

// ------------------------------------------------------------------- header

let CFGEDIT = false;

function renderHeader() {
  const chip = document.getElementById("cfgchip");
  chip.innerHTML = "";
  // add(), not chip.append() — Element.append(null) inserts the text "null"
  add(chip, [
    icon("folder"),
    el("span.cc-path", { text: DATA.config_dir || "…" }),
    DATA.default_dir ? null : el("span.cc-warn", { title: "non-default config directory" }, icon("warn")),
    el("span.cc-caret", {}, icon("chevronDown"))]);
  chip.title = DATA.default_dir
    ? DATA.config_dir + " — the default; Claude Code reads it automatically"
    : DATA.config_dir + " — non-default; Claude Code only uses it when CLAUDE_CONFIG_DIR is exported";

  const notice = document.getElementById("cfgnotice");
  notice.innerHTML = "";
  if (!DATA.default_dir) {
    notice.append(el("div.alert.alert-warning", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("warn")),
      el("div.alert-body", {},
        el("div.alert-title", { text: "Non-default config directory" }),
        el("div", { text: "Claude Code only reads " + DATA.config_dir
          + " when CLAUDE_CONFIG_DIR is exported in your shell. Otherwise it uses ~/.claude." }))));
  }
}

function openCfgMenu(anchor) {
  const entries = [
    { label: "Change directory…", icon: "pencil", fn: changeCfgDir },
    { label: "Copy path", icon: "copy", fn: () => copyText(DATA.config_dir, "config directory path") },
  ];
  if (!DATA.default_dir)
    entries.push({ separator: true },
      { label: "Reset to default (~/.claude)", icon: "refresh", fn: resetCfgDir });
  openMenu(anchor, entries);
}

async function changeCfgDir() {
  const r = await modal({
    title: "Config directory",
    text: "Absolute path (or ~/…) of the Claude Code config directory this dashboard manages. "
        + "Stored machine-locally in .claude-ui.json beside the checkout.",
    fields: [{ id: "p", label: "Path", value: DATA.config_dir, mono: true }],
    ok: "Save",
  });
  if (!r) return;
  try {
    await api("/api/config-dir", { path: r.p === DATA.config_dir ? "" : r.p });
    toast("Config directory updated");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function resetCfgDir() {
  try {
    await api("/api/config-dir", { path: "" });
    toast("Config directory reset to the default");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied " + (what || "to clipboard"));
  } catch (e) { toast("Could not copy: " + e.message, true); }
}

// ---------------------------------------------------- open-in-editor bridge
// Anywhere the UI names a file it should also be able to open it. These three
// are the shared affordance; the editor itself lives in editor.js.

const cfgPath = (name) => (DATA.config_dir || "~/.claude") + "/" + name;

/* Pull a line number back out of a JSONDecodeError string ("… line 12 column
   3"). The parse position is the one piece of a syntax error worth acting on
   and it is only ever delivered inside prose. */
function jsonErrLineFromText(msg) {
  const m = /line (\d+)/i.exec(msg || "");
  return m ? +m[1] : 0;
}

function openFileBtn(path, label, locate, title) {
  const b = mkbtn("btn-sm", label || "Open", () => openPath(path, locate),
    title || ("Open " + path + " in the editor"));
  b.prepend(icon("pencil"));
  return b;
}

/* The config files card. items.config_files_state() has always been in
   /api/state and nothing rendered it, so CLAUDE.md and keybindings.json were
   reachable only through the command palette. */
function configFilesCard() {
  const files = DATA.config_files || [];
  if (!files.length) return null;
  const card = el("div.card", { style: { marginBottom: "1.25rem" } });
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Config files" }),
      el("div.card-description", {
        text: "The single files in your config directory, edited in place." }))));
  const body = el("div.card-content.flush");
  for (const f of files) {
    body.append(el("div.drow", {},
      icon("file"),
      el("span.dmsg.dmono", { text: f.path }),
      f.symlink ? badge("symlink", "outline") : null,
      f.broken ? badge("broken", "destructive") : null,
      el("div.dactions", {},
        f.broken ? mkbtn("btn-sm btn-ghost", "Copy path", () => copyText(f.path, "path"))
                 : openFileBtn(cfgPath(f.name), "Edit"))));
  }
  card.append(body);
  return card;
}

// --------------------------------------------------------------------- tabs

function tabBadge(t) {
  // Skills counts by file location, not by settings: a skill turned off with
  // skillOverrides still counts, deliberately. The badge says what is
  // installed, and the file is still there. Archived skills are not in
  // DATA.items and so do not count, which is the same rule.
  if (ITEM_TABS.includes(t))
    return String(((DATA.items || {})[t] || []).filter((i) => i.enabled).length);
  if (t === "settings") return String(Object.keys((DATA.settings || {}).data || {}).length);
  if (t === "mcp") return String(((DATA.mcp || {}).servers || []).filter((s) => s.enabled).length);
  if (t === "doctor" && DOCTOR && DOCTOR.warns) return String(DOCTOR.warns);
  if (t === "context" && CONTEXT)
    return String(CONTEXT.pointers.filter((f) => f.level === "warn").length);
  if (t === "projects" && PROJECTS) return String(PROJECTS.projects.length);
  return null;
}

function renderTabs() {
  const nav = document.getElementById("tabs");
  nav.innerHTML = "";
  for (const t of TABS) {
    const meta = TAB_META[t];
    const on = t === TAB;
    const b = el("button.btn.tabs-trigger", {
      role: "tab",
      id: "tab-" + t,
      "aria-selected": String(on),
      "aria-controls": t + "view",
      tabIndex: on ? 0 : -1,
      onclick: () => goTab(t),
      onkeydown: (e) => {
        const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (!d) return;
        e.preventDefault();
        goTab(TABS[(TABS.indexOf(t) + d + TABS.length) % TABS.length]);
        document.getElementById("tab-" + TAB)?.focus();
      },
    }, icon(meta.icon), el("span", { text: meta.label }));

    const count = tabBadge(t);
    if (count && count !== "0")
      b.append(el("span.tab-count", {
        class: t === "doctor" || t === "context" ? "warn" : "", text: count }));
    if (t === "statusline" && (DATA.statusline || {}).applied)
      b.append(el("span.tab-dot.ok", { title: "statusLine is set in settings.json" }));
    nav.append(b);
  }
}

// -------------------------------------------------- schema-driven settings
// Every settings control resolves to a `collect()` that returns the value to
// write, `undefined` to clear the key, or throws on invalid input. Selects
// (bool/enum) commit on change; everything else commits on the "Set" button.

function settingsGet(key) {
  let node = (DATA.settings || {}).data || {};
  for (const p of key.split(".")) {
    if (node == null || typeof node !== "object") return undefined;
    node = node[p];
  }
  return node;
}

async function commitSetting(key, value) {
  try {
    const r = await api("/api/settings-set", { key, value });
    // the server may repair the value (outputStyle name normalization) —
    // say so, or the correction looks like a bug on the next refresh
    const stored = r && r.value !== undefined && r.value !== null ? r.value : value;
    toast(value === null ? key + " cleared"
      : stored !== value ? key + " set to " + JSON.stringify(stored)
      : key + " set",
      false, value === null ? null : undefined);
    await refresh();
  } catch (e) { toast(e.message, true); }
}

const clearSetting = (key) => commitSetting(key, null);

function trySet(key, collect) {
  let v;
  try { v = collect(); }
  catch (e) { toast("Invalid value: " + e.message, true); return; }
  if (v === undefined) clearSetting(key);
  else commitSetting(key, v);
}

const opt = (v, label) => el("option", { value: v, text: label == null ? v : label });

const mkbtn = (cls, label, onclick, title) =>
  el("button.btn", { class: cls, text: label, onclick, title: title || "" });

/* --------------------------------------------------- the two shared shapes --

   Nine views fetch a payload once, keep it in a module global, and redraw from
   it; nine actions write, toast, and redraw. Both were written out longhand
   every time, and the copies had already drifted — a missing stale-tab guard
   here, a skeleton on a reload that flickers there. */

/* Fetch-once-then-render. Returns false when the caller should stop: the fetch
   failed, or you switched away while it was in flight and the answer is for a
   view no longer on screen.

   Two per-view quirks are kept rather than normalised. A view with a `note`
   (the ones whose fetch scans transcripts) shows its busy state on every load,
   because a rescan you asked for should look like it is running; one without
   shows it only on a cold load, where there is nothing on screen to replace.
   And `errorAsPayload` is renderDiscover's: it renders its consent card even
   when the index failed, so the failure has to arrive as data rather than as
   an alert that returns. */
async function cached({ view, get, set, url, reload, alive, note, skeleton = 3,
                        errorAsPayload }) {
  if (get() && !reload) return true;
  const node = document.getElementById(view);
  if (note || !get()) {
    node.innerHTML = "";
    if (note)
      node.append(el("div.muted", {
        style: { marginBottom: ".75rem", fontSize: ".8125rem" }, text: note }));
    if (skeleton) node.append(skeletonList(skeleton));
  }
  try {
    set(await api(url));
  } catch (e) {
    if (!errorAsPayload) {
      node.innerHTML = "";
      node.append(errorAlert(e.message));
      return false;
    }
    set({ error: e.message });
  }
  return alive ? alive() : true;
}

/* Write, say what happened, redraw. `msg` is the success wording — the
   "· applies to new sessions" tail included, verbatim, because it is what
   tells you the change is not retroactive. A function instead gets the
   response and returns {text, err}, for the calls whose result reports its own
   outcome (the plugin CLI's last line, an installer's detail) better than we
   could guess. `then` replaces the default full refresh(). */
async function act(url, body, msg, opts = {}) {
  const t = opts.busy
    ? toast({ title: opts.busy, variant: "loading", duration: 0 }) : null;
  try {
    const res = await api("/api/" + url, body);
    if (t) t.close();
    const say = typeof msg === "function" ? msg(res) : { text: msg };
    const undo = (say && say.undo) || opts.undo || null;
    if (say && say.text)
      toast(say.text, !!say.err, say.err ? null : undo);
    if (opts.then) await opts.then(res);
    else await refresh();
    return res;
  } catch (e) {
    if (t) t.close();
    toast(e.message, true);
    return null;
  }
}

let DL_SEQ = 0;
function datalist(values) {
  const dl = el("datalist", { id: "dl_" + (++DL_SEQ) });
  for (const v of values) dl.append(opt(v));
  return dl;
}

// Live datalist suggestions per setting key; a ":key" suffix targets a kv
// control's key input. Merged with static schema values and the server's
// machine-local DATA.suggest payload by suggestFor().
const itemNames = (t) =>
  ((DATA.items || {})[t] || []).map((it) => it.name).filter(Boolean);
const mcpNames = () => ((DATA.mcp || {}).servers || []).map((sv) => sv.name);
// Claude Code matches outputStyle against a style's frontmatter name when
// set, else the file basename (exact case) — never a subdirectory prefix
const styleSettingName = (it) =>
  it.meta_name || (it.name || "").split("/").pop();
const styleNames = () =>
  ((DATA.items || {})["output-styles"] || [])
    .map(styleSettingName).filter(Boolean);
const LIVE_SUGGEST = {
  "outputStyle": styleNames,
  "skillOverrides:key": () => itemNames("skills"),
  "mcpServerTimeouts:key": mcpNames,
  "enabledMcpjsonServers": mcpNames,
  "disabledMcpjsonServers": mcpNames,
  "agents:key": () => itemNames("agents"),
  "alwaysAllowedSkills": () => itemNames("skills"),
};
function suggestFor(key, base) {
  const out = (base || []).map(String);
  const live = ((DATA.suggest || {})[key] || [])
    .concat(LIVE_SUGGEST[key] ? LIVE_SUGGEST[key]() : []);
  for (const v of live) if (!out.includes(String(v))) out.push(String(v));
  return out;
}

// A single scalar control (used standalone and inside object/map forms).
// Returns { node, aux?, collect } — aux is an optional <datalist> to append.
function scalarControl(f, value, ph) {
  if (f.type === "bool") {
    const sel = el("select");
    sel.append(opt("", "Unset" + (f.default !== undefined ? " · default " + f.default : "")),
      opt("true", "true"), opt("false", "false"));
    if (value === true) sel.value = "true";
    else if (value === false) sel.value = "false";
    return { node: sel, collect: () => sel.value === "" ? undefined : sel.value === "true" };
  }
  if (f.type === "enum") {
    const sel = el("select");
    sel.append(opt("", "Unset" + (f.default !== undefined ? " · default " + f.default : "")));
    // schema values plus any live docs-discovered values for this key
    const vals = suggestFor(f.key, f.values);
    for (const v of vals) sel.append(opt(v));
    // keep an out-of-vocabulary current value visible instead of showing "Unset"
    if (value !== undefined && value !== null && !vals.includes(String(value)))
      sel.append(opt(String(value), String(value) + " (current)"));
    if (value !== undefined && value !== null) sel.value = String(value);
    return { node: filterSelect(sel), collect: () => sel.value === "" ? undefined : sel.value };
  }
  const sugg = suggestFor(f.key, f.values);
  // datalist on type=number is ignored by Safari/Firefox, so suggested
  // numbers use a text input; collect() still validates numerically
  const inp = el("input.mono", { type: f.type === "number" && !sugg.length ? "number" : "text" });
  if (f.type === "number") inp.inputMode = "decimal";
  if (value === "") inp.value = '""';
  else if (value !== undefined && value !== null) inp.value = String(value);
  if (ph) inp.placeholder = ph;
  // a long suggestion list gets the filterable picker instead of a datalist
  let aux = null, node = filterInput(inp, sugg);
  if (!node) {
    node = inp;
    if (sugg.length) { aux = datalist(sugg); inp.setAttribute("list", aux.id); }
  }
  const collect = () => {
    const r = inp.value.trim();
    if (!r) return undefined;
    if (f.type === "number") {
      const n = Number(r);
      if (Number.isNaN(n)) throw new Error((f.key || "value") + ": not a number");
      return n;
    }
    return r === '""' ? "" : r;
  };
  return { node, aux, collect };
}

// list → one input per entry, with add/remove; optional per-row suggestions.
function listForm(ctrl, s, cur) {
  const box = el("div.formrows");
  ctrl.append(box);
  const sugg = suggestFor(s.key, s.item_values);
  // long lists filter in a popup instead (see filterInput), so no datalist
  const dl = sugg.length && sugg.length <= FSEL_MIN ? datalist(sugg) : null;
  if (dl) ctrl.append(dl);
  const addRow = (val) => {
    const inp = el("input.mono", { type: "text", value: val || "" });
    if (dl) inp.setAttribute("list", dl.id);
    const r = el("div.formrow", {}, filterInput(inp, sugg) || inp);
    r.append(mkbtn("btn-sm danger btn-icon", "", () => r.remove(), "Remove"));
    r.lastChild.append(icon("x"));
    box.append(r);
    return inp;
  };
  (Array.isArray(cur) ? cur : []).forEach((v) => addRow(String(v)));
  ctrl.append(mkbtn("btn-sm", "Add", () => addRow("").focus()));
  ctrl.lastChild.prepend(icon("plus"));
  return () => {
    const vals = [...box.querySelectorAll("input")]
      .map((i) => i.value.trim()).filter(Boolean);
    return vals.length ? vals : undefined;
  };
}

// kv → key/value row editor; value control is a dropdown (s.values),
// number input (s.value_type === "number"), or free text.
function mapForm(ctrl, s, cur) {
  const box = el("div.formrows");
  ctrl.append(box);
  const ksugg = suggestFor(s.key + ":key", s.key_values);
  // env alone suggests 300-odd names — those filter in a popup, not a datalist
  const kdl = ksugg.length && ksugg.length <= FSEL_MIN ? datalist(ksugg) : null;
  // Size the key column to the names this setting actually suggests: env's run
  // to 53 characters, an MCP server timeout's are short. Clamped so one outlier
  // cannot eat the value field, and settings with no suggestions keep 14ch.
  box.style.setProperty("--kk-ch",
    Math.min(56, Math.max(14, ...ksugg.map((v) => String(v).length))));
  if (kdl) ctrl.append(kdl);
  const addRow = (k, v) => {
    const kin = el("input.kk.mono", { type: "text", placeholder: "key", value: k || "" });
    if (kdl) kin.setAttribute("list", kdl.id);
    const val = scalarControl(
      s.values ? { type: "enum", values: s.values }
        : s.value_type === "number" ? { type: "number" } : { type: "string" },
      v, "value");
    const r = el("div.formrow", {}, filterInput(kin, ksugg) || kin, val.node);
    if (val.aux) r.append(val.aux);
    const del = mkbtn("btn-sm danger btn-icon", "", () => r.remove(), "Remove");
    del.append(icon("x"));
    r.append(del);
    box.append(r);
    return () => {
      if (!r.isConnected) return null;
      const key = kin.value.trim();
      let out;
      try { out = val.collect(); } catch (e) { throw new Error(key + ": " + e.message); }
      if (!key && out === undefined) return null;
      if (!key) throw new Error("missing key for value: " + out);
      if (out === undefined) throw new Error(key + ": missing value");
      return [key, out];
    };
  };
  const collectors = [];
  const entries = cur && typeof cur === "object" && !Array.isArray(cur)
    ? Object.entries(cur) : [];
  entries.forEach(([k, v]) => collectors.push(addRow(k, v)));
  const addBtn = mkbtn("btn-sm", "Add", () => collectors.push(addRow("", "")));
  addBtn.prepend(icon("plus"));
  ctrl.append(addBtn);
  return () => {
    const out = {};
    for (const c of collectors) {
      const pair = c();
      if (pair) out[pair[0]] = pair[1];
    }
    return Object.keys(out).length ? out : undefined;
  };
}

// object → labeled mini-form over declared fields; const fields are always written.
function objectForm(ctrl, s, cur) {
  const box = el("div.formobj");
  ctrl.append(box);
  const obj = cur && typeof cur === "object" && !Array.isArray(cur) ? cur : {};
  const collectors = [];
  for (const f of s.fields) {
    if (f.const !== undefined) continue;
    const line = el("label.formfield", {},
      el("span.flabel", { text: f.key + (f.desc ? " — " + f.desc : "") }));
    // dotted path so subfields resolve live/server suggestions by full key
    const sc = scalarControl({ ...f, key: s.key + "." + f.key }, obj[f.key]);
    line.append(sc.node);
    if (sc.aux) line.append(sc.aux);
    box.append(line);
    collectors.push([f.key, sc.collect]);
  }
  return () => {
    const out = {};
    let any = false;
    for (const [k, collect] of collectors) {
      const v = collect();
      if (v !== undefined) { out[k] = v; any = true; }
    }
    if (!any) return undefined;
    for (const f of s.fields) if (f.const !== undefined) out[f.key] = f.const;
    return out;
  };
}

function jsonForm(ctrl, s, cur, isSet) {
  const ta = el("textarea.mono", { placeholder: "JSON", spellcheck: false });
  ta.value = isSet ? JSON.stringify(cur, null, 2) : "";
  const fit = () => { ta.rows = Math.min(16, Math.max(3, ta.value.split("\n").length + 1)); };
  fit();
  ta.oninput = fit;
  if ((s.templates || []).length) {
    const sel = el("select");
    const ph = opt("", "Insert template…");
    ph.disabled = true;
    sel.append(ph, ...s.templates.map((t, i) => opt(String(i), t.name)));
    sel.value = "";
    sel.onchange = async () => {
      const t = s.templates[Number(sel.value)];
      sel.value = "";
      if (!t) return;
      if (ta.value.trim() && !(await mconfirm("Replace " + s.key + "?",
          "Replace the editor contents with the “" + t.name + "” template?", "Replace")))
        return;
      ta.value = JSON.stringify(t.value, null, 2);
      fit();
    };
    ctrl.append(filterSelect(sel));
  }
  ctrl.append(ta);
  return () => {
    const r = ta.value.trim();
    if (!r) return undefined;
    try { return JSON.parse(r); }
    catch (e) { throw new Error("JSON: " + e.message); }
  };
}

// Must match schema.MANAGED_CAT — the category settings.py files the
// managed/enterprise-only keys under.
const MANAGED_CAT = "managed & enterprise";

// ------------------------------------------------------------ setting help
// Every row already carries what it needs to render — desc, type, default,
// allowed values, and the exact docs URL for the key (resolved server-side from
// the official schema, so it lands on the right anchor rather than a page).
// Only the long official description is fetched, once, on the first popover.

let HELP = null, HELP_PROMISE = null;
const loadHelp = () => HELP_PROMISE ||= api("/api/schema-help")
  .then((r) => (HELP = r.keys || {}))
  .catch(() => (HELP = {}));

// "https://…/docs/en/model-config#adjust-effort-level" → "model-config#adjust-effort-level"
const docLabel = (url) => (url || "").split("/docs/en/")[1] || "the docs";

// Official descriptions end with "See <url>" — sometimes several. The panel
// renders that link as a button right underneath, so the bare URLs are noise.
const prose = (text) => (text || "")
  .replace(/\s*See (https:\/\/\S+(?:\s+and\s+https:\/\/\S+)*)\.?\s*$/, "")
  .trim() || text || "";

function settingHelpPanel(s) {
  const help = (HELP || {})[s.key] || {};
  const body = el("div.pop-body", {
    text: prose(help.description) || s.desc || "No description available.",
  });

  const head = el("div.pop-head", {}, el("code.skey", { text: s.key }));
  if (help.type) head.append(badge(String(help.type), "outline"));
  if (s.default !== undefined) {
    head.append(badge("default: " + JSON.stringify(s.default), "outline"));
  }
  if (s.managed) head.append(badge("managed only", "outline"));
  if (s.unverified) head.append(badge("unverified", "outline"));

  const panel = el("div.popover", { role: "dialog", "aria-label": s.key }, head, body);

  const allowed = help.enum || (s.type === "enum" ? s.values : null);
  if (allowed && allowed.length) {
    panel.append(el("div.pop-enum", {},
      el("span.pop-enum-label", { text: "allowed values" }),
      ...allowed.map((v) => badge(String(v), "default"))));
  }

  panel.append(el("div.pop-foot", {},
    el("a.btn.btn-sm", {
      href: s.doc, target: "_blank", rel: "noreferrer",
    }, icon("link"), el("span", { text: "Docs · " + docLabel(s.doc) }))));

  // if the long text hasn't arrived yet, swap it in when it does — the panel is
  // already useful without it, so there is never a spinner-only state. The
  // panel was measured while short, so it has to be re-placed after it grows.
  if (!help.description) {
    loadHelp().then(() => {
      const fresh = (HELP || {})[s.key];
      if (fresh && fresh.description && body.isConnected) {
        body.textContent = prose(fresh.description);
        if (fresh.type) head.insertBefore(badge(String(fresh.type), "outline"),
                                          head.children[1] || null);
        repositionDropdown();
      }
    });
  }
  return panel;
}

function settingRow(s) {
  const cur = settingsGet(s.key);
  const isSet = cur !== undefined;
  const row = el("div.srow", { class: isSet ? "is-set" : "" });

  const meta = el("div.smeta", {},
    el("div.row-flex", { style: { gap: ".375rem" } },
      el("span.skey", { text: s.key }),
      isSet ? badge("set", "default") : null,
      s.unverified ? el("span", {
        title: "Not listed in the official JSON Schema. It sets " +
               "additionalProperties, so absence is not disproof — the key may " +
               "be real and documented elsewhere.",
      }, badge("unverified", "outline")) : null,
      infoTrigger(s.key, () => settingHelpPanel(s)),
      el("a.sdoc", {
        href: s.doc, target: "_blank", rel: "noreferrer",
        title: "Open the documentation for " + s.key,
      }, icon("link"))),
    s.desc ? el("div.sdesc", { text: s.desc }) : null);

  const ctrl = el("div.sctrl");
  if (["object", "list", "kv", "json"].includes(s.type)) ctrl.classList.add("wide");

  if (s.type === "bool" || s.type === "enum") {
    // fixed-choice dropdown that commits immediately
    const sc = scalarControl(s, cur);
    sc.node.onchange = () => {
      const v = sc.collect();
      v === undefined ? clearSetting(s.key) : commitSetting(s.key, v);
    };
    ctrl.append(sc.node);
  } else {
    let collect;
    if (s.type === "object") collect = objectForm(ctrl, s, cur);
    else if (s.type === "list") collect = listForm(ctrl, s, cur);
    else if (s.type === "kv") collect = mapForm(ctrl, s, cur);
    else if (s.type === "json") collect = jsonForm(ctrl, s, cur, isSet);
    else {
      const ph = s.default !== undefined ? "default: " + s.default : "unset";
      const sc = scalarControl(s, cur, ph);
      ctrl.append(sc.node);
      if (sc.aux) ctrl.append(sc.aux);
      collect = sc.collect;
    }
    const actions = el("div.row-flex", { style: { gap: ".375rem" } },
      mkbtn("btn-sm btn-primary", "Set", () => trySet(s.key, collect)));
    if (isSet) actions.append(mkbtn("btn-sm danger", "Clear", () => clearSetting(s.key)));
    ctrl.append(actions);
  }

  row.append(meta, ctrl);
  return row;
}

// ------------------------------------------------------------------- hooks

const HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse",
  "PostToolUse", "Notification", "Stop", "SubagentStop", "PreCompact",
  "SessionEnd"];

function hooksList(data) {
  const hooks = data.hooks;
  if (hooks == null) return [];
  if (typeof hooks !== "object" || Array.isArray(hooks)) return null;
  const out = [];
  for (const [event, matchers] of Object.entries(hooks)) {
    if (!Array.isArray(matchers)) return null;
    matchers.forEach((m, mi) => {
      if (!m || typeof m !== "object") return;
      (Array.isArray(m.hooks) ? m.hooks : []).forEach((h, hi) => {
        out.push({ event, mi, hi, matcher: m.matcher || "",
          command: (h && h.command) || "", timeout: h && h.timeout });
      });
    });
  }
  return out;
}

async function hooksSave(newHooks) {
  await api("/api/settings-set", { key: "hooks",
    value: Object.keys(newHooks).length ? newHooks : null });
  await refresh();
}

async function hookAdd() {
  const r = await modal({ title: "Add hook",
    text: "The command receives the event JSON on stdin; exit code 2 blocks the action (tool events).",
    fields: [
      { id: "e", label: "Event", type: "select", options: HOOK_EVENTS },
      { id: "m", label: "Matcher — tool name pattern, tool events only (blank = all)",
        placeholder: "e.g. Bash or Edit|Write", mono: true },
      { id: "c", label: "Command", mono: true },
      { id: "t", label: "Timeout in seconds (optional)" }], ok: "Add" });
  if (!r || !r.c) return;
  const hooks = JSON.parse(JSON.stringify(((DATA.settings || {}).data || {}).hooks || {}));
  const arr = (hooks[r.e] = hooks[r.e] || []);
  let entry = arr.find((m) => (m.matcher || "") === (r.m || ""));
  if (!entry) {
    entry = r.m ? { matcher: r.m, hooks: [] } : { hooks: [] };
    arr.push(entry);
  }
  const h = { type: "command", command: r.c };
  if (r.t && !isNaN(+r.t)) h.timeout = +r.t;
  (entry.hooks = entry.hooks || []).push(h);
  try {
    await hooksSave(hooks);
    toast("Hook added · applies to new sessions");
  } catch (e) { toast(e.message, true); }
}

async function hookDelete(row) {
  if (!(await mconfirm("Delete hook?", row.event + ": " + row.command, "Delete"))) return;
  const hooks = JSON.parse(JSON.stringify(DATA.settings.data.hooks));
  const m = hooks[row.event][row.mi];
  m.hooks.splice(row.hi, 1);
  if (!m.hooks.length) hooks[row.event].splice(row.mi, 1);
  if (!hooks[row.event].length) delete hooks[row.event];
  try {
    await hooksSave(hooks);
    toast("Hook removed · applies to new sessions");
  } catch (e) { toast(e.message, true); }
}

async function hookFire(row) {
  const t = toast({ title: "Firing a sample " + row.event + " event…", variant: "loading", duration: 0 });
  try {
    const r = await api("/api/hook-test", { command: row.command, event: row.event });
    t.close();
    const bits = [];
    if ((r.stdout || "").trim()) bits.push("stdout: " + r.stdout.trim().slice(0, 300));
    if ((r.stderr || "").trim()) bits.push("stderr: " + r.stderr.trim().slice(0, 300));
    toast({ title: row.event + " test: " + r.detail,
      description: bits.join("\n") || undefined,
      variant: r.ok ? "success" : "error" });
  } catch (e) { t.close(); toast(e.message, true); }
}

// ---------------------------------------------------------------- settings

let SFILTER = { q: "", set: false };
const SOPEN = new Set();      // categories the user has explicitly toggled open
const SCLOSED = new Set();    // …and closed

function renderSettings() {
  const view = document.getElementById("settingsview");
  const st = DATA.settings || {};
  view.innerHTML = "";

  view.append(el("div.view-head", {
    html: "Editing <b>" + esc(st.path || "") + "</b>"
      + (st.exists ? "" : " (created on the first set)")
      + " · changes apply to new sessions",
  }));

  if (st.error) {
    view.append(el("div.alert.alert-destructive", {},
      el("span.alert-icon", {}, icon("error")),
      el("div.alert-body", {},
        el("div.alert-title", { text: "settings.json has invalid JSON" }),
        el("div", { text: "Form editing is disabled until the file parses. " + st.error }),
        el("div", { style: { marginTop: ".5rem" } },
          openFileBtn(cfgPath("settings.json"), "Fix it in the editor",
            { line: jsonErrLineFromText(st.error) })))));
    add(view, [configFilesCard()]);  // add() drops a null; append() would print it
    return;
  }

  // warm the popover help now rather than on the first hover: one request, and
  // it means a panel is measured at its full height when it opens
  loadHelp();

  // ---- hooks builder
  const rows = hooksList(st.data || {});
  const hookCard = el("div.card", { style: { marginBottom: "1.25rem" } });
  hookCard.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Lifecycle hooks" }),
      el("div.card-description", {
        text: rows === null
          ? "The hooks config has a non-standard shape — edit it as raw JSON under “environment & hooks” below."
          : "Commands run at session and tool events. Each receives the event JSON on stdin; Test fires a sample event.",
      })),
    rows === null ? null : el("div.card-action", {},
      (() => {
        const b = mkbtn("btn-sm btn-primary", "Add hook", hookAdd);
        b.prepend(icon("plus"));
        return b;
      })())));

  if (rows && rows.length) {
    const body = el("div.card-content.flush");
    for (const row of rows) {
      body.append(el("div.drow", {},
        badge(row.event, "default"),
        row.matcher ? badge(row.matcher, "outline") : null,
        el("span.dmsg.dmono", { text: row.command }),
        row.timeout ? badge(row.timeout + "s", "secondary") : null,
        el("div.dactions", {},
          (() => { const b = mkbtn("btn-sm", "Test", () => hookFire(row), "Pipe a sample event into this command"); b.prepend(icon("play")); return b; })(),
          openFileBtn(cfgPath("settings.json"), "Edit", { find: row.command },
            "Open settings.json at this hook"),
          mkbtn("btn-sm danger", "Delete", () => hookDelete(row)))));
    }
    hookCard.append(body);
  } else if (rows) {
    hookCard.append(el("div.card-content", {},
      el("div.muted", { style: { fontSize: ".78125rem" },
        text: "No hooks configured." })));
  }
  view.append(hookCard);
  add(view, [configFilesCard()]);  // add() drops a null; append() would print it

  // ---- schema-driven settings
  const setCount = Object.keys(st.data || {}).length;
  const bar = el("div.toolbar");
  const fin = el("input", {
    type: "search", id: "setq", placeholder: "Filter settings by key or description…",
    value: SFILTER.q,
    oninput: (e) => {
      SFILTER.q = fin.value;
      // re-rendering mid-composition destroys the composition (CJK input)
      if (e.isComposing) return;
      refilter("setq", renderSettings);
    },
  });
  bar.append(fin);
  bar.append(el("div.toolbar-end", {},
    switchToggle("Only set", SFILTER.set, (v) => { SFILTER.set = v; renderSettings(); },
      "Show only keys present in settings.json"),
    el("span.hint", {
      text: setCount + " of " + SCHEMA.length + " documented keys set" })));
  view.append(bar);

  const q = SFILTER.q.toLowerCase();
  // aka is optional, and absent entirely on the synthetic "other keys" rows
  const match = (s) =>
    (!q || s.key.toLowerCase().includes(q)
      || (s.desc || "").toLowerCase().includes(q)
      || (s.aka || []).some((a) => a.toLowerCase().includes(q)))
    && (!SFILTER.set || settingsGet(s.key) !== undefined);

  const cats = new Map();
  for (const s of SCHEMA) if (match(s)) {
    if (!cats.has(s.cat)) cats.set(s.cat, []);
    cats.get(s.cat).push(s);
  }

  const covered = new Set(SCHEMA.map((s) => s.key.split(".")[0]));
  const extra = Object.keys(st.data || {})
    .filter((k) => !covered.has(k))
    .map((k) => ({ key: k, type: "json", cat: "other keys in this file",
      desc: "Not listed in the official schema — edited as raw JSON",
      doc: "https://code.claude.com/docs/en/settings", unverified: true }))
    .filter(match);
  if (extra.length) cats.set("other keys in this file", extra);

  if (!cats.size) {
    view.append(emptyState("No matching settings",
      q ? "Nothing matches “" + SFILTER.q + "”." : "No keys are set yet.", "filter"));
    return;
  }

  for (const [cat, items] of cats) {
    const nSet = items.filter((s) => settingsGet(s.key) !== undefined).length;
    const managed = cat === MANAGED_CAT;
    // filtering forces everything open so results are never hidden behind a
    // fold. The managed group stays shut unless asked for: setting one of these
    // in user scope does nothing, so "one is set" is no reason to open it.
    const open = q || SFILTER.set ? true
      : SOPEN.has(cat) ? true
      : SCLOSED.has(cat) || managed ? false
      : nSet > 0 || cat === "model";
    const group = el("details.setgroup", { open, class: managed ? "managed" : "" });
    group.ontoggle = () => {
      if (q || SFILTER.set) return;
      (group.open ? SOPEN : SCLOSED).add(cat);
      (group.open ? SCLOSED : SOPEN).delete(cat);
    };
    group.append(el("summary", {},
      el("span.sg-caret", {}, icon("chevronRight")),
      el("span.sg-name", { text: cat }),
      el("span.sg-meta", {},
        managed ? badge("no-op in user scope", "outline") : null,
        nSet ? badge(nSet + " set", "default") : null,
        el("span.hint", { text: items.length + " keys" }))));
    if (managed) {
      group.append(el("div.setgroup-note", { text:
        "These keys are only honored in managed settings — /Library/Application " +
        "Support/ClaudeCode/managed-settings.json, /etc/claude-code/" +
        "managed-settings.json, or an MDM policy. Setting one in your user " +
        "settings.json has no effect; they are listed here so you can look them up." }));
    }
    for (const s of items) group.append(settingRow(s));
    view.append(group);
  }
}

// --------------------------------------------------------------------- MCP

let MCPEDIT = null;

const MCP_TEMPLATE = {
  stdio: { type: "stdio", command: "/path/to/server", args: [], env: {} },
  http: { type: "http", url: "https://example.com/mcp", headers: {} },
};

function mcpSummary(cfg) {
  if (!cfg || typeof cfg !== "object") return "?";
  const t = cfg.type || (cfg.command ? "stdio" : cfg.url ? "http" : "?");
  const what = cfg.command
    ? cfg.command + (cfg.args && cfg.args.length ? " " + cfg.args.join(" ") : "")
    : cfg.url || "";
  return t + " · " + what;
}

function mcpEditPanel() {
  const name = el("input.mono", {
    type: "text", id: "mcpname", placeholder: "server name",
    value: MCPEDIT.name || "", disabled: !MCPEDIT.isNew,
  });
  const ta = el("textarea.fedit.mono", {
    id: "mcpjson", rows: 12, spellcheck: false, value: MCPEDIT.json,
    style: { minHeight: "14rem" },
  });
  return el("div.mcppanel", {},
    el("div.toolbar", {},
      name,
      MCPEDIT.enabled === false ? badge("disabled", "outline") : null,
      el("div.toolbar-end", {},
        MCPEDIT.isNew ? null : mkbtn("btn-sm danger", "Delete",
          () => mcpDelete(MCPEDIT.name, MCPEDIT.enabled !== false)))),
    ta,
    el("div.toolbar", { style: { marginTop: ".625rem", marginBottom: 0 } },
      mkbtn("btn-primary", "Save", mcpSave),
      mkbtn("", "Cancel", () => { MCPEDIT = null; render(); })));
}

async function mcpSave() {
  let config;
  try { config = JSON.parse(document.getElementById("mcpjson").value); }
  catch (e) { toast("Invalid JSON: " + e.message, true); return; }
  const name = (document.getElementById("mcpname").value || "").trim();
  const enabled = MCPEDIT.enabled !== false;
  try {
    await api("/api/mcp-save", { name, config, enabled });
    toast(name + " saved" + (enabled ? "" : " (still disabled)") + " · applies to new sessions");
    MCPEDIT = null;
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function mcpDelete(name, enabled) {
  if (!(await mconfirm("Delete " + name + "?",
    enabled ? "Removes it from " + DATA.mcp.machine_path + "."
      : "Removes it from disabled/mcp-servers.json.", "Delete"))) return;
  try {
    await api("/api/mcp-delete", { name, enabled });
    toast(name + " deleted · applies to new sessions");
    if (MCPEDIT && MCPEDIT.name === name) MCPEDIT = null;
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function mcpNew() {
  const r = await modal({ title: "Add MCP server", fields: [
    { id: "n", label: "Server name", mono: true },
    { id: "k", label: "Transport", type: "select", options: [
      { value: "stdio", label: "stdio — local command" },
      { value: "http", label: "http/sse — remote URL" }] }], ok: "Create" });
  if (!r || !r.n) return;
  MCPEDIT = { name: r.n, isNew: true, json: JSON.stringify(MCP_TEMPLATE[r.k], null, 2) };
  render();
}

async function mcpToggle(name, enabled) {
  try {
    await api("/api/mcp-toggle", { name, enabled });
    toast(name + (enabled ? " enabled" : " disabled — parked in disabled/mcp-servers.json")
      + " · applies to new sessions");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function mcpTest(name) {
  const t = toast({ title: "Testing " + name + "…", variant: "loading", duration: 0 });
  try {
    const r = await api("/api/mcp-test", { name });
    t.close();
    toast(name + ": " + r.detail, !r.ok);
  } catch (e) { t.close(); toast(e.message, true); }
}

function renderMcp() {
  const view = document.getElementById("mcpview");
  const st = DATA.mcp || { servers: [] };
  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "User-scope MCP servers in <b>" + esc(st.machine_path) + "</b> — Claude Code's machine store"
      + (st.machine_exists ? "" : " (created on the first save)")
      + ". Changes apply to new sessions.",
  }));

  const machineOk = !st.machine_error;
  if (st.machine_error) {
    view.append(el("div.alert.alert-destructive", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("error")),
      el("div.alert-body", {},
        el("div.alert-title", { text: "~/.claude.json has invalid JSON" }),
        el("div", { text: "Server editing is disabled until it parses. " + st.machine_error }),
        el("div", { style: { marginTop: ".5rem" } },
          openFileBtn("~/.claude.json", "Fix it in the editor",
            { line: jsonErrLineFromText(st.machine_error) })))));
  }

  if (machineOk) {
    const addBtn = mkbtn("btn-primary", "Add server", mcpNew);
    addBtn.prepend(icon("plus"));
    view.append(el("div.toolbar", {}, el("div.toolbar-end", {}, addBtn)));
    if (MCPEDIT) view.append(mcpEditPanel());
  }

  if (!st.servers.length) {
    view.append(emptyState("No MCP servers on this machine",
      "Add one to give Claude Code extra tools — a local stdio command or a remote HTTP endpoint.",
      "server"));
    return;
  }

  const list = el("div.list");
  for (const s of st.servers) {
    const actions = el("div.li-actions", {});
    const testBtn = mkbtn("btn-sm", "Test", () => mcpTest(s.name), "Start the server and list its tools");
    testBtn.prepend(icon("play"));
    actions.append(testBtn);
    if (machineOk) {
      const ed = mkbtn("btn-sm", "Edit", () => {
        MCPEDIT = { name: s.name, isNew: false, enabled: s.enabled,
          json: JSON.stringify(s.config, null, 2) };
        render();
      });
      ed.prepend(icon("pencil"));
      actions.append(ed);
      actions.append(mkbtn("btn-sm" + (s.enabled ? " danger" : ""),
        s.enabled ? "Disable" : "Enable", () => mcpToggle(s.name, !s.enabled)));
      // the same overflow every other entity list has: deleting a server used
      // to mean opening the editor first, which nothing else asks of you
      const more = mkbtn("btn-sm btn-icon btn-ghost", "", (e) => openMenu(e.currentTarget, [
        { label: "Copy name", icon: "copy", fn: () => copyText(s.name, "name") },
        // enabled servers only: the disabled store is this app's parking
        // area, and a move is of the live entry Claude Code actually reads
        ...(s.enabled ? [{ label: "Move to a project…", icon: "arrowRight",
          fn: () => mcpMoveToProject(s) }] : []),
        { label: "Delete server", icon: "trash", danger: true,
          fn: () => mcpDelete(s.name, s.enabled) },
      ]), "More actions");
      more.append(icon("chevronDown"));
      actions.append(more);
    }
    list.append(el("div.list-item", { class: s.enabled ? "" : "off" },
      el("div.li-main", {},
        el("span.li-name", { text: s.name }),
        s.enabled ? null : badge("disabled", "outline")),
      el("span.li-desc.mono", { text: mcpSummary(s.config) }),
      actions));
  }
  view.append(list);
}

/* Scope moves. Claude Code keeps MCP servers in three places: user
   (~/.claude.json, every project on this machine), project (.mcp.json,
   committed), and local (~/.claude.json again, but under projects.<root> —
   one project, this machine, and where `claude mcp add` writes by default).
   The entry moves verbatim; only where it is recorded changes. */

async function mcpMoveToProject(s) {
  try { if (!PROJECTS) PROJECTS = await api("/api/projects"); }
  catch (e) { toast(e.message, true); return; }
  const projs = PROJECTS.projects || [];
  if (!projs.length) {
    toast("No registered projects — add one on the Projects tab first", true);
    return;
  }
  const r = await modal({
    title: "Move " + s.name + " to a project",
    text: "The entry leaves ~/.claude.json and lands, verbatim, at the scope "
        + "you pick. Project scope is .mcp.json — committed, so everyone who "
        + "clones the repo is offered it; it stays approved for you on this "
        + "machine. Local scope stays private to you and that one project.",
    fields: [
      { id: "p", label: "Project", type: "select",
        options: projs.map((p) => ({ value: p.root, label: p.tilde })) },
      { id: "s", label: "Scope", type: "select", options: [
        { value: "project", label: "project — .mcp.json, shared with the team" },
        { value: "local", label: "local — just you, just that project" }] },
    ],
    ok: "Move",
  });
  if (!r || !r.p) return;
  await mcpMove(s.name, { scope: "user" }, { scope: r.s, root: r.p });
}

async function mcpMove(name, from, to) {
  try {
    await api("/api/mcp-move", { name, from, to });
    toast(name + " moved");
    PROJECTS = null;    // both stores changed; refetch each on next look
    await refresh();
    render();
  } catch (e) { toast(e.message, true); }
}

// ----------------------------------------------------------------- plugins

let PLUGINS = null;
let PQ = "";

const KIND_LABEL = { agents: "agent", commands: "command", skills: "skill",
  "output-styles": "output style", mcp: "MCP server", hooks: "hooks" };

// Both registry cards wait on the same thing: `claude plugin marketplace list`
// and `claude plugin list --available`, two subprocesses. Nothing is asked of a
// model, and saying otherwise made a slow CLI call look like a slow answer.
const CLI_WAIT = "Running claude plugin list…";

const pluralKind = (k, n) =>
  n + " " + KIND_LABEL[k] + (n === 1 || k === "hooks" ? "" : "s");

const countLine = (p) =>
  Object.entries(p.counts).map(([k, n]) => pluralKind(k, n)).join(" · ");

/* What every write on the Plugins tab redraws: /api/state for the inventory
   and settings.json, then the plugin tree itself. */
const reloadPlugins = async () => { await refresh(); renderPlugins(true); };

async function renderPlugins(reload) {
  if (!onSeg("plugins")) return;
  const view = document.getElementById("skillseg");
  // a rescan re-reads the plugin tree, so the per-plugin detail cached from
  // the old one is stale by definition
  if (!PLUGINS || reload) PDETAIL = {};
  if (!await cached({ view: "skillseg", url: "/api/plugins", reload,
                      get: () => PLUGINS, set: (v) => { PLUGINS = v; },
                      alive: () => onSeg("plugins") })) return;
  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "Plugins on disk under <b>" + esc(PLUGINS.root) + "</b>, as set in <b>settings.json</b>. "
      + "A plugin is enabled as a whole — <b>Split</b> copies the parts you want into your own "
      + "config and turns the plugin off, so they survive the next plugin update.",
  }));

  view.append(subagentModelBar());
  view.append(userRegistryCard());

  if (PLUGINS.error)
    view.append(errorAlert(PLUGINS.error, "Could not read the plugin config"));

  const all = PLUGINS.plugins;
  const q = PQ.trim().toLowerCase();
  const hit = (p) => !q || (p.id + " " + p.description).toLowerCase().includes(q)
    || p.components.some((c) => c.name.toLowerCase().includes(q));
  const shown = all.filter(hit);

  const inp = el("input", {
    type: "search", id: "pq", placeholder: "Filter plugins by name, description or component…",
    value: PQ,
    oninput: (e) => {
      PQ = inp.value;
      if (e.isComposing) return;
      refilter("pq", renderPlugins);
    },
  });
  const rescan = mkbtn("btn-sm", "Rescan", () => renderPlugins(true), "Re-read the plugin tree");
  rescan.prepend(icon("refresh"));
  view.append(el("div.toolbar", {}, inp,
    el("div.toolbar-end", {},
      el("span.hint", {
        text: shown.length + " of " + all.length + " shown" }),
      rescan)));

  if (!all.length) {
    view.append(emptyState("No plugins on this machine",
      "Install one with `claude plugin install <name>`, then rescan.", "plug"));
    return;
  }
  if (!shown.length) {
    view.append(noMatches(PQ));
    view.append(catalogSearchLink(PQ));
    return;
  }

  const section = (state, label, hint) => {
    const rows = shown.filter((p) => p.state === state);
    if (!rows.length) return;
    view.append(sectionTitle(label, rows.length));
    if (hint) view.append(el("div.section-hint", { text: hint }));
    const box = el("div.list");
    for (const p of rows) box.append(pluginRow(p));
    view.append(box);
  };
  section("enabled", "Enabled");
  section("disabled", "Disabled");
  section("available", "Available",
    "On disk from a marketplace, with no entry in settings.json — Claude Code decides these "
    + "by the plugin's own default. You can split one without ever enabling it.");

  adoptedSection(view);
}

/* --------------------------------------- marketplaces, for this machine --
   The project card has had this since marketplaces arrived; this is the same
   bridge at user scope, which is where most people actually install. Adding a
   marketplace clones a repository and installing a plugin unpacks a version
   into the shared cache — both Claude Code's decisions, made by its own CLI,
   run here with --scope user. What lands is one key in your settings.json,
   and the plugin applies in every project on this machine.

   Collapsed until asked, because unlike everything else on this tab it costs
   two subprocess calls that can take seconds: the inventory below must stay
   instant. Where a plugin's tree is already on disk the row says what it
   brings — skills and MCP servers arrive inside plugins, and that is the only
   way either of them is distributed. A catalogue entry not yet fetched cannot
   say, and says so rather than guessing. */

let UREG = null;        // user_registry_state payload — fetched on demand
let UREGOPEN = false;

function userRegistryCard() {
  const toggle = mkbtn("btn-sm btn-icon btn-ghost", "", () => {
    UREGOPEN = !UREGOPEN;
    if (UREGOPEN && !UREG) userRegistryLoad(); else renderPlugins();
  }, UREGOPEN ? "Collapse" : "Load marketplaces and installable plugins (runs claude plugin list)");
  toggle.append(icon("chevronDown"));

  const card = el("div.card", {},
    el("div.drow", {},
      icon("download"),
      el("span.dmsg", {},
        el("div", { text: "Marketplaces & installs" }),
        el("div.hint", { text: "Recorded in your settings.json — installs apply "
          + "in every project on this machine. Skills and MCP servers are "
          + "distributed inside plugins; there is no separate marketplace for "
          + "them." })),
      el("div.dactions", {}, toggle)));
  if (!UREGOPEN) return card;
  if (!UREG) {
    card.append(el("div.drow", {}, el("span.dmsg", { text: CLI_WAIT })));
    return card;
  }

  if (UREG.error)
    card.append(el("div.drow", {},
      icon("warn"), el("span.dmsg", { text: UREG.error })));

  for (const m of UREG.marketplaces)
    card.append(el("div.drow", {},
      icon("download"),
      el("span.dmsg.dmono", { text: m.name || String(m) }),
      el("div.dactions", {}, mkbtn("btn-sm danger", "Remove", () =>
        userMarketRemove(m.name || String(m))))));

  for (const s of UREG.suggested)
    if (!UREG.marketplaces.some((m) => (m.name || "") === s.source.split("/")[1]))
      card.append(el("div.drow", {},
        icon("download"),
        el("span.dmsg", {},
          el("div.dmono", { text: s.source }),
          el("div.hint", { text: s.desc })),
        badge(s.label, "outline"),
        el("div.dactions", {}, mkbtn("btn-sm", "Add", () =>
          userRegistryRun("user-marketplace-add", { source: s.source },
            "Marketplace added")))));

  card.append(el("div.drow", {},
    el("span.dmsg", { text: "Any GitHub repo, git URL or local path works too." }),
    el("div.dactions", {}, mkbtn("btn-sm", "Add marketplace…", userMarketAdd))));

  for (const p of UREG.installed) {
    const id = p.id || p.name || "";
    card.append(el("div.drow", {},
      icon("plug"),
      el("span.dmsg", {},
        el("div.dmono", { text: id }),
        el("div.hint", { text: (pluginBrings(id) || "installed")
          + (p.version ? " · v" + p.version : "") })),
      p.scope === "user" ? badge("yours", "success")
                         : badge(p.scope || "?", "outline"),
      el("div.dactions", {}, p.scope === "user"
        // only the entry this scope owns: a project's install is the
        // project card's to remove, and pretending otherwise would fail
        ? mkbtn("btn-sm danger", "Uninstall", () => userPluginRemove(id))
        : null)));
  }

  for (const p of UREG.available) {
    const id = p.pluginId || p.name || "";
    card.append(el("div.drow", {},
      icon("plug"),
      el("span.dmsg", {},
        el("div.dmono", { text: id }),
        el("div.hint", { text: pluginBrings(id)
          || p.description || "Components are listed once it is installed." })),
      el("div.dactions", {}, mkbtn("btn-sm btn-primary", "Install", () =>
        userRegistryRun("user-plugin-install", { id }, "Installed")))));
  }

  if (!UREG.marketplaces.length && !UREG.installed.length)
    card.append(el("div.drow", {},
      el("span.dmsg", { text: "No marketplaces yet. Adding one only records "
        + "where to look; nothing runs until you install a plugin." })));
  return card;
}

// What a plugin brings, when its tree is already on disk — the same count
// line the inventory below uses, so the two never disagree.
function pluginBrings(id) {
  const p = (PLUGINS.plugins || []).find((x) => x.id === id);
  return p && p.counts ? countLine(p) : "";
}

async function userRegistryLoad() {
  renderPlugins();                // draw the CLI_WAIT line first
  try { UREG = await api("/api/user-registry", {}); }
  catch (e) {
    UREG = { error: e.message, marketplaces: [], installed: [],
             available: [], suggested: [] };
  }
  if (onSeg("plugins")) renderPlugins();
}

async function userRegistryRun(action, body, msg) {
  // the CLI's own last line, not our guess at what it did
  await act(action, body, (r) => ({ text: r.ok ? msg + " · " + r.detail : r.detail,
                                    err: !r.ok }),
    { busy: "Running claude plugin…",
      then: async () => {
        UREG = null;
        await userRegistryLoad();
        // the tree changed under the inventory below, and settings.json with it
        await refresh();
        renderPlugins(true);
      } });
}

async function userMarketAdd() {
  const r = await modal({
    title: "Add a marketplace",
    text: "Runs `claude plugin marketplace add … --scope user`, which clones "
        + "the source and records it for every project on this machine. Add "
        + "sources you trust: a plugin can ship hooks, which run commands.",
    fields: [{ id: "s", label: "Source", mono: true, placeholder: "owner/repo",
      hint: "A GitHub owner/repo, a git URL, or a path to a local directory" }],
    ok: "Add",
  });
  if (!r || !r.s) return;
  userRegistryRun("user-marketplace-add", { source: r.s }, "Marketplace added");
}

async function userMarketRemove(name) {
  if (await mconfirm("Remove " + name + "?",
      "Stops it being a place to install from. Plugins you already installed "
      + "from it stay on disk, but stop resolving to a source.", "Remove"))
    userRegistryRun("user-marketplace-remove", { name }, "Marketplace removed");
}

async function userPluginRemove(id) {
  if (await mconfirm("Uninstall " + id + "?",
      "Removes it from your settings.json. A project that enables it in its "
      + "own settings is unaffected.", "Uninstall"))
    userRegistryRun("user-plugin-uninstall", { id }, "Uninstalled");
}

/* What a Split left behind: ordinary items in your config dir that remember
   which plugin they came from. They already show up under Skills/Commands/…,
   but only here do you see them as a group, next to the plugin they answer to. */
function adoptedSection(view) {
  const rows = (PLUGINS.adopted || []).filter((a) =>
    !PQ.trim() || (a.name + " " + a.source).toLowerCase().includes(PQ.trim().toLowerCase()));
  if (!rows.length) return;

  view.append(sectionTitle("Split into your config", rows.length));
  view.append(el("div.section-hint", {
    text: "Yours now — edit them freely. Drift just means you have changed your "
      + "copy, or the plugin moved on; it is only a problem if you meant to stay "
      + "in step." }));

  const box = el("div.list");
  for (const a of rows) box.append(adoptedRow(a));
  view.append(box);
}

function adoptedRow(a) {
  const actions = el("div.li-actions", {});

  const edit = mkbtn("btn-sm", "Edit",
    () => openItemEditor(a.type, a.name, null, a.enabled));
  edit.prepend(icon("pencil"));
  actions.append(edit);

  if (a.source_path) {
    // not btn-ghost: app.css reserves the hover-to-reveal treatment for the
    // overflow "…" alone, since a dimmed button reads as disabled
    const view = mkbtn("btn-sm", "Plugin's copy",
      () => openPath(a.source_path),
      "Open the plugin's version read-only, to see what yours differs from");
    view.prepend(icon("eye"));
    actions.append(view);
  }

  const menu = [{ label: "Copy path", icon: "copy", fn: () => copyText(a.path, "path") }];
  if (!a.missing)
    menu.push({ label: "Re-sync from plugin", icon: "refresh",
      fn: () => adoptedResync(a), danger: true });
  const more = mkbtn("btn-sm btn-icon btn-ghost", "",
    (e) => openMenu(e.currentTarget, menu), "More actions");
  more.append(icon("chevronDown"));
  actions.append(more);

  const badges = [badge(a.type.replace(/s$/, ""), "outline")];
  if (a.missing) {
    const b = badge("source gone", "warning");
    b.title = a.source + " is no longer installed — your copy is now the only one";
    badges.push(b);
  } else if (a.drift) {
    const b = badge("differs", "info");
    b.title = "Your copy and " + a.source + " are no longer identical";
    badges.push(b);
  }
  if (!a.enabled) badges.push(badge("disabled", "secondary"));

  return el("div.list-item", { class: a.enabled ? "" : "off" },
    el("div.li-main", {},
      el("span.li-name", { title: a.path, text: a.name }),
      ...badges),
    el("span.li-desc", { text: "from " + a.source }),
    actions);
}

/* Re-sync overwrites your copy from the plugin. There is no undo — the toast
   Undo pattern used elsewhere can't put back content we never held — so this
   one asks first and says plainly what it will destroy. */
async function adoptedResync(a) {
  const ok = await mconfirm("Re-sync " + a.name + "?",
    "This overwrites your copy with the current version from " + a.source
    + ". Any edits you made to it are lost, and there is no undo.",
    "Overwrite my copy");
  if (!ok) return;
  await act("plugin-resync", { type: a.type, name: a.name },
    a.name + " re-synced from " + a.source, { then: reloadPlugins });
}

function pluginRow(p) {
  const splittable = p.components.some((c) => c.adoptable);
  const agents = p.components.filter((c) => c.kind === "agents");
  const actions = el("div.li-actions", {});

  const body = el("div.detail-panel", { hidden: true });
  if (agents.length || p.state !== "available") {
    const open = mkbtn("btn-sm btn-icon btn-ghost pd-toggle", "", () => togglePluginDetail(p, open, body),
      agents.length ? "Models its agents run on, and the settings it reads"
                    : "The settings this plugin reads");
    open.append(icon("chevronDown"));
    actions.append(open);
    if (POPEN.has(p.id)) { POPEN.delete(p.id); togglePluginDetail(p, open, body); }
  }
  if (splittable) {
    const b = mkbtn("btn-sm btn-primary", "Split…", () => pluginSplit(p),
      "Keep the components you want, drop the rest");
    b.prepend(icon("split"));
    actions.append(b);
  }
  actions.append(mkbtn("btn-sm" + (p.enabled ? " danger" : ""),
    p.enabled ? "Disable" : "Enable", () => pluginToggle(p.id, !p.enabled)));
  /* There is no "turn off one of this plugin's skills" here, and there was:
     it wrote a skillOverrides entry, which the docs say plugin skills ignore,
     under a bare name that could only ever match a skill of your own. Claude
     Code offers whole-plugin enablement and nothing finer, so neither does
     this. See plugins.skill_override_set() for the longer version. */
  const more = mkbtn("btn-sm btn-icon btn-ghost", "", (e) => openMenu(e.currentTarget, [
    { label: "Copy path", icon: "copy", fn: () => copyText(p.path, "path") },
    ...((p.entries || []).length
      ? [{ label: "Move enablement…", icon: "arrowRight",
           fn: () => pluginScopeMove(p) }]
      : []),
  ]), "More actions");
  more.append(icon("chevronDown"));
  actions.append(more);

  const badges = [badge(p.marketplace, "outline")];
  // "not set" is the user store's answer. A plugin a project turns on is set
  // somewhere, so say where before the row calls it unused.
  if (p.state === "available") badges.push(badge("not set", "secondary"));
  for (const e of (p.entries || []).filter((e) => e.scope !== "user")) {
    const b = badge(e.scope + ": " + e.root, "outline");
    b.title = "enabledPlugins in " + e.root + "/.claude/"
      + (e.scope === "project" ? "settings.json — committed"
                               : "settings.local.json — just you")
      + ", currently " + (e.enabled ? "on" : "off") + " there";
    badges.push(b);
  }
  if (p.components.some((c) => c.warn)) {
    const b = badge("plugin-relative paths", "warning");
    b.title = "Some components expand ${CLAUDE_PLUGIN_ROOT}, which stops resolving once split";
    badges.push(b);
  }
  return el("div.list-item", { class: p.enabled ? "" : "off" },
    el("div.li-main", {},
      el("span.li-name", { title: p.path, text: p.name }),
      ...badges),
    el("span.li-desc", {
      text: countLine(p) + (p.description ? " — " + p.description : ""),
    }),
    actions, body);
}

/* Moving where a plugin's enablement is recorded. The plugin's files are in
   the shared cache either way — this moves one line of settings, so nothing
   downloads and nothing is removed from disk. */
async function pluginScopeMove(p) {
  try { if (!PROJECTS) PROJECTS = await api("/api/projects"); }
  catch (e) { toast(e.message, true); return; }
  const projs = PROJECTS.projects || [];
  const label = (e) => (e.scope === "user" ? "your config" : e.root)
    + " · " + e.scope + " · " + (e.enabled ? "on" : "off");
  const dests = [{ value: "user", label: "your config — every project on this machine" }];
  for (const pr of projs) {
    dests.push({ value: "project:" + pr.root,
      label: pr.tilde + " — project, committed with the repo" });
    dests.push({ value: "local:" + pr.root,
      label: pr.tilde + " — local, just you" });
  }
  const r = await modal({
    title: "Move where " + p.name + " is enabled",
    text: "Moves the enabledPlugins entry, keeping its on/off value. The "
        + "plugin's files stay in the shared cache — nothing is downloaded or "
        + "deleted, only the record of who has it switched on.",
    fields: [
      { id: "f", label: "From", type: "select",
        options: (p.entries || []).map((e) => ({
          value: e.scope + ":" + (e.raw_root || ""), label: label(e) })) },
      { id: "t", label: "To", type: "select", options: dests },
    ],
    ok: "Move",
  });
  if (!r || !r.f || !r.t) return;
  const parse = (v) => {
    const i = v.indexOf(":");
    const scope = i < 0 ? v : v.slice(0, i);
    const root = i < 0 ? "" : v.slice(i + 1);
    return root ? { scope, root } : { scope };
  };
  try {
    await api("/api/plugin-scope-move", { id: p.id, from: parse(r.f), to: parse(r.t) });
    toast(p.name + " moved");
    PROJECTS = null;      // a project's settings changed too
    await refresh();      // and possibly your own settings.json
    renderPlugins(true);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------- what a plugin's agents run on

   Claude Code has no per-plugin or per-agent model setting, so there are only
   three honest answers, and this panel shows which one is in force:

     - env.CLAUDE_CODE_SUBAGENT_MODEL, which the schema describes as overriding
       the model subagents use, whatever their own frontmatter says;
     - the agent's own `model:` line — editable once the agent is yours, which
       is what Split makes it;
     - nothing, and it inherits the session's model.

   Plus lever four, which belongs to the plugin rather than to Claude Code: a
   plugin that wants per-agent models ships its own env vars and reads them in
   a hook. Those are in the second section, found by reading the plugin. */

const SUBAGENT_KEY = "env.CLAUDE_CODE_SUBAGENT_MODEL";

/* One control, at the top, for the blunt answer: every subagent on this
   machine, plugin or not. It is the same settings.json key the Settings tab
   edits and writes through the same endpoint — this is a second door, not a
   second source of truth. */
function subagentModelBar() {
  const s = schemaFor(SUBAGENT_KEY);
  const cur = settingsGet(SUBAGENT_KEY);
  const sc = scalarControl({ key: SUBAGENT_KEY, type: "combo", values: s.values || [] },
    cur, "unset · each agent decides");
  const ctrl = el("div.pd-ctrl", {}, sc.node);
  if (sc.aux) ctrl.append(sc.aux);
  ctrl.append(mkbtn("btn-sm btn-primary", "Set", () => trySet(SUBAGENT_KEY, sc.collect)));
  if (cur !== undefined)
    ctrl.append(mkbtn("btn-sm danger", "Clear", () => clearSetting(SUBAGENT_KEY)));
  return el("div.card.subagent-bar", {},
    el("div.pd-name", {},
      el("span.skey", { text: SUBAGENT_KEY }),
      cur !== undefined ? badge("set", "default") : null,
      infoTrigger(SUBAGENT_KEY, () => settingHelpPanel(s))),
    el("div.sdesc", { text: s.desc || "" }),
    ctrl);
}
let PDETAIL = {};   // plugin id -> /api/plugin-detail, fetched once per rescan
// which cards are open: setting a value re-renders the whole list, and having
// the panel you are working in vanish under you is its own bug
let POPEN = new Set();

const schemaFor = (key) => SCHEMA.find((s) => s.key === key) || {};
// the alias/ID list the settings tab offers for this key, minus "inherit",
// which means "no override" there and would read as a model name here
const modelValues = () =>
  (schemaFor(SUBAGENT_KEY).values || []).filter((v) => v !== "inherit");

const subagentModel = () => {
  const v = settingsGet(SUBAGENT_KEY);
  return typeof v === "string" && v.trim() && v.trim() !== "inherit" ? v.trim() : "";
};

/* The model an agent will actually run on, and why — the "why" is the part
   worth showing, since three different files can be the answer. */
function effectiveModel(model) {
  const forced = subagentModel();
  if (forced) return { text: forced, why: "every subagent, from " + SUBAGENT_KEY,
                       variant: "info", forced: true };
  if (model) return { text: model, why: "the agent's own model: line", variant: "default" };
  return { text: "inherits session", why: "no model set anywhere — it runs on the session's model",
           variant: "secondary" };
}

/* Built on first expand, not on render: both panels behind this walk a tree
   server-side, and a list of forty rows must stay instant. `load` runs once —
   a second expand redraws nothing, which is what dataset.built records. */
async function toggleDetail(btn, body, { busy, load, build, onopen }) {
  body.hidden = !body.hidden;
  btn.classList.toggle("is-open", !body.hidden);
  if (onopen) onopen(!body.hidden);
  if (body.hidden || body.dataset.built) return;
  body.dataset.built = "1";
  body.append(el("div.hint", { text: busy }));
  let d;
  try { d = await load(); }
  catch (e) {
    body.innerHTML = "";
    body.append(el("div.hint", { text: e.message }));
    return;
  }
  body.innerHTML = "";
  build(d);
}

const togglePluginDetail = (p, btn, body) => toggleDetail(btn, body, {
  busy: "Reading the plugin…",
  onopen: (open) => (open ? POPEN.add(p.id) : POPEN.delete(p.id)),
  load: async () => (PDETAIL[p.id] ||= await api(
    "/api/plugin-detail?id=" + encodeURIComponent(p.id))),
  build: (d) => {
    pluginAgentsPanel(p, body);
    envPanel(d.env || [], body, "plugin", reloadPlugins);
  },
});

function pluginAgentsPanel(p, body) {
  const agents = p.components.filter((c) => c.kind === "agents");
  if (!agents.length) return;
  body.append(el("div.pd-title", { text: "Agents" }));
  const forced = subagentModel();
  for (const c of agents) {
    // an agent we already split out is our file, so its model: line is ours to
    // set; one still inside the plugin is not, and Split is the way to own it
    const mine = (PLUGINS.adopted || []).find(
      (a) => a.type === "agents" && a.source === p.id + "/agents/" + c.name);
    const eff = effectiveModel(mine ? mine.model : c.model);
    const chip = badge(eff.text, eff.variant);
    chip.title = eff.why;

    const right = el("div.pd-ctrl", {});
    if (forced) {
      right.append(el("span.hint", {
        text: "set by " + SUBAGENT_KEY }));
    } else if (mine) {
      const sc = scalarControl({ key: SUBAGENT_KEY, type: "combo", values: modelValues() },
        mine.model || undefined, "inherits session");
      right.append(sc.node);
      if (sc.aux) right.append(sc.aux);
      right.append(mkbtn("btn-sm btn-primary", "Set",
        () => setItemModel(mine.name, sc.collect())));
      if (mine.model)
        right.append(mkbtn("btn-sm danger", "Clear", () => setItemModel(mine.name, "")));
    } else {
      right.append(mkbtn("btn-sm", "Split to set…", () => pluginSplit(p, c.name),
        "The plugin's own copy is read-only — split this agent into your config first"));
    }
    body.append(el("div.pd-row", {},
      el("div.pd-name", {}, el("span.li-name", { title: c.path, text: c.name }), chip),
      right));
  }
}

/* "Settings this <noun> reads" — the same panel for a plugin and for a skill.
   A skill is a directory, so it can ship a scripts/ package that reads the
   environment exactly as a plugin's code does, with the same problem: the
   names exist only in its source, and the values land in the one settings.json
   `env` either way, because that is the only place Claude Code reads them. */
function envPanel(env, body, noun, redraw) {
  // the two wordings, kept verbatim rather than assembled: a plugin's *code*
  // reads a variable, a skill's *files* do, and the verb follows the noun
  const w = noun === "plugin"
    ? { none: "this plugin's code does not read any environment variable of "
             + "its own.",
        src: "the plugin's own code" }
    : { none: "this skill's files do not read any environment variable of "
             + "their own.",
        src: "the skill's own files" };
  body.append(el("div.pd-title", { text: "Settings this " + noun + " reads" }));
  if (!env.length) {
    body.append(el("div.pd-note", { text: "Nothing found — " + w.none }));
    return;
  }
  body.append(el("div.pd-note", {
    html: "Names found by reading " + w.src + ", not a list it publishes — "
      + "treat them as a lead. They are written to <b>env</b> in settings.json, "
      + "where they apply to every session, not just this " + noun + "." }));
  for (const e of env) {
    const sc = scalarControl(
      { key: e.model ? SUBAGENT_KEY : "env." + e.name, type: "combo",
        values: e.model ? modelValues() : [] },
      e.value || undefined, "unset");
    const right = el("div.pd-ctrl", {}, sc.node);
    if (sc.aux) right.append(sc.aux);
    right.append(mkbtn("btn-sm btn-primary", "Set",
      () => setEnvVar(e.name, sc.collect(), redraw)));
    if (e.value)
      right.append(mkbtn("btn-sm danger", "Clear", () => setEnvVar(e.name, "", redraw)));

    const name = el("div.pd-name", {},
      el("span.skey", { title: "read in " + e.files.join(", "), text: e.name }),
      e.model ? badge("model", "outline") : null,
      e.value ? badge("set", "default") : null);
    body.append(el("div.pd-row", {}, name, right));
    if (e.doc)
      body.append(el("div.pd-doc", { title: e.doc.file, text: e.doc.line }));
  }
}

/* One settings.json env entry. The endpoint has never cared whether the name
   came off a plugin's code or a skill's; only what to redraw afterwards
   differs, and that is the caller's. */
async function setEnvVar(name, value, redraw) {
  await act("plugin-env-set",
    { name, value: value === undefined ? "" : String(value) },
    name + (value ? " set to " + value : " cleared") + " · applies to new sessions",
    { then: redraw });
}

async function setItemModel(name, model) {
  await act("item-model-set",
    { name, model: model === undefined ? "" : String(model) },
    name + (model ? " runs on " + model : " back to inheriting")
      + " · applies to new sessions",
    { then: reloadPlugins });
}

/* Components grouped for the Split checklist. Anything that can't be copied
   out — hooks, a ${CLAUDE_PLUGIN_ROOT} MCP server, a name you already use —
   still shows, greyed, with the reason, so the dialog is the whole picture. */
function splitGroups(p, only, models) {
  const groups = [];
  for (const kind of ["agents", "commands", "skills", "output-styles", "mcp", "hooks"]) {
    const rows = p.components.filter((c) => c.kind === kind).map((c) => {
      const badges = [];
      if (c.warn) {
        const b = badge("plugin-relative", "warning");
        b.title = c.warn;
        badges.push(b);
      }
      if (c.conflict) badges.push(badge("name taken", "destructive"));
      const disabled = !c.adoptable || !!c.conflict;
      // the copy is ours the moment it lands, so its model is settable here —
      // the plugin's own file is never touched either way
      let extra = null;
      if (kind === "agents" && !disabled && !subagentModel()) {
        const sc = scalarControl({ key: SUBAGENT_KEY, type: "combo", values: modelValues() },
          c.model || undefined, "model: inherits");
        models[c.name] = sc;
        extra = sc.aux ? el("span", {}, sc.node, sc.aux) : sc.node;
      }
      return {
        value: kind + "/" + c.name, name: c.name, desc: c.description,
        badges, disabled, extra,
        checked: only ? c.name === only && kind === "agents" : true,
        reason: c.conflict || c.reason || (c.adoptable ? null : "stays with the plugin"),
      };
    });
    if (rows.length) groups.push({ label: KIND_LABEL[kind] + (kind === "hooks" ? "" : "s"), rows });
  }
  return groups;
}

/* `only` pre-ticks a single agent — the entry point from "Split to set…" on the
   agents panel, where you came to change one model, not to take the plugin
   apart. The dialog still shows everything, because splitting turns the plugin
   off and you should see what that costs before agreeing to it. */
async function pluginSplit(p, only) {
  const models = {};
  const groups = splitGroups(p, only, models);
  const keepable = groups.reduce((n, g) => n + g.rows.filter((r) => !r.disabled).length, 0);
  if (!keepable) { toast("Nothing in " + p.name + " can be split out", true); return; }
  const r = await modal({
    title: "Split " + p.name,
    text: "Keep the components you want — they are copied into your config and become "
      + "ordinary items. The rest stay with the plugin"
      + (p.state === "available" ? "." : ", which is turned off.")
      + " Skills can also be turned off individually without splitting."
      + (Object.keys(models).length
         ? " An agent's model is yours to set once the copy is yours." : ""),
    wide: true,
    fields: [{ id: "keep", type: "checklist", groups }],
    ok: "Split",
  });
  if (!r) return;
  const picks = (r.keep || []).map((v) => {
    const i = v.indexOf("/");
    return { kind: v.slice(0, i), name: v.slice(i + 1) };
  });
  if (!picks.length) { toast("Nothing selected — nothing changed"); return; }
  const chosen = {};
  for (const pick of picks) {
    if (pick.kind !== "agents" || !models[pick.name]) continue;
    let v;
    try { v = models[pick.name].collect(); }
    catch (e) { toast("Invalid model for " + pick.name + ": " + e.message, true); return; }
    if (v !== undefined) chosen[pick.name] = String(v);
  }
  await act("plugin-split",
    { id: p.id, picks, disable: p.state !== "available", models: chosen },
    (res) => ({
      text: "Kept " + res.kept + " of " + res.total + " from " + p.name
        + (res.disabled ? " · plugin disabled" : "") + " · applies to new sessions",
      undo: res.disabled
        ? { label: "Re-enable plugin", fn: () => pluginToggle(p.id, true) } : null,
    }),
    { then: reloadPlugins });
}

async function pluginToggle(id, enabled) {
  await act("plugin-toggle", { id, enabled },
    id.split("@")[0] + (enabled ? " enabled" : " disabled")
      + " · applies to new sessions",
    { then: reloadPlugins });
}

/* value is one of Claude Code's four states, or null to remove the entry —
   which is not a fifth state but the absence of one, and reads as "on". The
   wording follows that: clearing is not "set to null". */
async function skillOverride(name, value) {
  const was = (settingsGet("skillOverrides") || {})[name] || null;
  await act("skill-override", { name, value },
    value ? "Skill " + name + " set to " + value + " · applies to new sessions"
          : "Skill " + name + " back on · applies to new sessions",
    { undo: { label: "Undo", fn: () => skillOverride(name, was) } });
}

// ------------------------------------------------------------------- setup

let SETUP = null;

async function renderSetup(reload) {
  const view = document.getElementById("setupview");
  if (!await cached({ view: "setupview", url: "/api/setup", reload, skeleton: 2,
                      get: () => SETUP, set: (v) => { SETUP = v; },
                      alive: () => TAB === "setup" })) return;
  view.innerHTML = "";
  view.append(el("div.view-head", {
    text: "Installable pieces of environment setup. Applying a piece patches your existing setup "
        + "in place — it never replaces your files. Whether a piece is installed is derived by "
        + "looking, not recorded; removing touches only that piece's own artifacts.",
  }));

  const list = el("div.list");
  for (const p of SETUP.pieces) {
    const actions = el("div.li-actions", {},
      mkbtn("btn-sm btn-primary", p.installed ? "Re-apply" : "Apply", () => setupAct("apply", p)));
    if (p.removable && p.installed)
      actions.append(mkbtn("btn-sm danger", "Remove", () => setupAct("remove", p)));
    const row = el("div.list-item", {},
      el("div.li-main", {},
        el("span.li-name", { text: p.label }),
        p.installed ? badge("installed", "success") : badge("not installed", "outline")),
      el("span.li-desc", { text: p.desc + (p.detail ? " — " + p.detail : "") }));
    // A piece may carry notes: the exact writes it makes, one per line, so the
    // list is on screen before the button that performs them is pressed.
    if (p.notes && p.notes.length) {
      // "keys" is the settings-preset shape; a piece that writes files rather
      // than settings keys supplies its own summary instead.
      const fold = el("details.piece-notes", {},
        el("summary", { text: p.notes_label
          || "What it writes (" + p.notes.length + " keys)" }));
      for (const n of p.notes) fold.append(el("div.li-desc", { text: n }));
      row.append(fold);
    }
    if (p.config_fields && p.config_fields.length) row.append(pieceConfigBody(p));
    if (p.id === "local-model") row.append(localModelBody(p));
    row.append(actions);
    list.append(row);
  }
  view.append(list);
}

async function setupAct(action, p) {
  if (action === "remove" &&
      !(await mconfirm("Remove " + p.label + "?",
        "Removes only this piece's own artifacts (" + (p.target || "its files") +
        ") and clears the setting it set. Your own config is left as-is.", "Remove")))
    return;
  try {
    await api("/api/setup-" + action, { id: p.id });
    toast(p.label + (action === "apply" ? " applied" : " removed") + " · applies to new sessions");
    await refresh();
    renderSetup(true);
  } catch (e) { toast(e.message, true); }
}

/* A piece's declared settings, drawn from the state payload alone — the tab
   knows nothing about what the piece does. Explicit Save, not save-on-change:
   these write to disk, and every other write in this app is a button press.
   The local-model piece keeps its own hand-built body above; it has a probe
   and a live test no declaration can express. */
function pieceConfigBody(p) {
  const box = el("div.piececfg");
  const controls = {};
  for (const f of p.config_fields) {
    const sel = el("select");
    for (const o of f.options || []) sel.append(opt(o.value, o.label || o.value));
    if (f.value !== undefined && f.value !== null) sel.value = String(f.value);
    controls[f.id] = () => sel.value;
    box.append(el("div.lmrow", {}, el("span.lmlbl", { text: f.label || f.id }), sel));
    if (f.hint) box.append(el("div.lmrow", {}, el("span.li-desc", { text: f.hint })));
  }
  box.append(el("div.lmrow", {},
    mkbtn("btn-sm btn-primary", "Save", () => pieceConfigSave(p, controls))));
  return box;
}

async function pieceConfigSave(p, controls) {
  const values = {};
  for (const id of Object.keys(controls)) values[id] = controls[id]();
  try {
    await api("/api/setup-config", { id: p.id, values });
    toast(p.label + " saved · applies to new sessions");
    await refresh();
    renderSetup(true);
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------ setup: local-model piece

let LOCALPROBE = null;  // last /api/local-probe result, shown inline
let LOCALTEST = null;   // last /api/local-test result, shown inline

function localModelBody(p) {
  const box = el("div.localcfg");
  const url = el("input.mono", { type: "text", placeholder: "http://localhost:8000",
    value: p.config.base_url || "" });
  const key = el("input.mono", { type: "text", placeholder: "API key (optional)",
    value: p.config.api_key || "" });
  const sel = el("select");
  const models = LOCALPROBE && LOCALPROBE.ok ? LOCALPROBE.models : [];
  // per-model size + the server's live memory ceiling (probe best-effort);
  // 1024-based GB so the numbers match what oMLX itself prints
  const minfo = (LOCALPROBE && LOCALPROBE.info) || {};
  const gb = (b) => (b / (1024 ** 3)).toFixed(1);
  sel.append(opt("", models.length ? "Pick a model…" : "Fetch models first…"));
  for (const m of models) {
    const i = minfo[m];
    sel.append(opt(m, m + (i && i.size ? " · " + gb(i.size) + " GB" : "")
      + (i && i.fits === false ? " — won't fit" : "")));
  }
  // keep a saved model visible even when it's not in (or there is no) fetch
  if (p.config.model && !models.includes(p.config.model))
    sel.append(opt(p.config.model, p.config.model + " (saved)"));
  if (p.config.model) sel.value = p.config.model;
  // the "this model cannot load right now" warning, kept in step with the
  // selection so it is on screen before Save, not after claude-local dies
  const fitrow = el("div.lmrow");
  const fitsync = () => {
    fitrow.replaceChildren();
    const i = minfo[sel.value];
    if (i && i.fits === false)
      fitrow.append(icon("warn"), el("span.li-desc", { text:
        "needs " + gb(i.size) + " GB but the server's memory ceiling is "
        + gb(LOCALPROBE.ceiling) + " GB right now — quit memory-heavy apps, "
        + "or raise it in the oMLX admin page: Settings → Resource Management "
        + "→ Reserve level (aggressive, or custom + a GB value), then restart "
        + "oMLX" }));
  };
  sel.addEventListener("change", fitsync);
  fitsync();

  const save = async (probe) => {
    try {
      await api("/api/local-config",
        { base_url: url.value, model: sel.value, api_key: key.value });
      if (probe) {
        LOCALPROBE = await api("/api/local-probe", {});
        // one model on the server: picking it is the only sensible choice
        if (LOCALPROBE.ok && !sel.value && LOCALPROBE.models.length === 1)
          await api("/api/local-config", { base_url: url.value,
            model: LOCALPROBE.models[0], api_key: key.value });
      } else toast("Local model saved" + (p.installed ? " · wrapper regenerated" : ""));
      renderSetup(true);
    } catch (e) { toast(e.message, true); }
  };

  box.append(el("div.lmrow", {},
    el("span.lmlbl", { text: "server" }), url,
    el("span.lmlbl", { text: "key" }), key,
    mkbtn("btn-sm", "Fetch models", () => save(true),
      "Save the server address, then list its models (GET /v1/models — free)")));
  box.append(el("div.lmrow", {},
    el("span.lmlbl", { text: "model" }), filterSelect(sel),
    mkbtn("btn-sm btn-primary", "Save", () => save(false),
      "Store the choice and, if installed, regenerate claude-local.sh")));
  box.append(fitrow);
  if (LOCALPROBE && !(LOCALPROBE.ok && LOCALPROBE.models.length))
    box.append(el("div.lmrow", {}, icon("warn"),
      el("span.li-desc", { text: (LOCALPROBE.detail || "no models")
        + (LOCALPROBE.ok ? " — download one in the oMLX admin page first"
           : " — start it (omlx start) or install: brew install jundot/omlx/omlx") })));
  if (p.installed)
    box.append(el("div.lmrow", {},
      mkbtn("btn-sm", "Test live…", localTest,
        "One real generation through claude-local — free, but runs on this machine"),
      mkbtn("btn-sm btn-ghost", "Copy run command",
        () => copyText("claude-local", "run command"))));
  if (LOCALTEST) {
    box.append(el("div.lmrow", {},
      icon(LOCALTEST.ok ? "check" : "warn"),
      LOCALTEST.ok ? badge("local model answered", "success")
                   : badge("unexpected answer", "destructive"),
      el("span.li-desc.dmono", { text: LOCALTEST.answer || "(no output)" })));
    if (LOCALTEST.hint)
      box.append(el("div.lmrow", {},
        el("span.li-desc", { text: LOCALTEST.hint.replace(/^ — /, "") })));
  }
  return box;
}

async function localTest() {
  if (!(await mconfirm("Run a live test?",
      "Asks the local model — through claude-local.sh — to echo a fixed phrase. "
      + "Free, but it runs a real generation on this machine; a cold model can "
      + "take a while to load.", "Run test")))
    return;
  const t = toast({ title: "Asking the local model…", variant: "loading", duration: 0 });
  try {
    const r = await api("/api/local-test", {});
    t.close();
    LOCALTEST = r;
    toast(r.ok ? "Local model answered" : "Unexpected answer — details on the card", !r.ok);
    renderSetup();
  } catch (e) { t.close(); toast(e.message, true); }
}

// ---------------------------------------------------------------- projects

let PROJECTS = null;
let PROJTEST = {};  // root -> last live-test result, shown inline on the card
// the restore-into-this-project flow, which takes over the view while it runs:
// {root, tilde, step, archives, rep, picked, open, showSame}
let PRESTORE = null;

// mode -> the live filename inside <project>/.claude/ (projects.py MODES)
const PROJ_FILES = { replace: "system-prompt.md", append: "append-system-prompt.md" };

// the types a project's own .claude/ can hold (core.PROJECT_MANAGED_TYPES)
const PROJ_ITEMS = [["skills", "skill"], ["commands", "command"],
                    ["agents", "agent"], ["output-styles", "output style"]];

// which type sections a card has open, keyed "<root>/<type>". Kept outside
// PROJECTS so a reload after an edit redraws with the same sections open.
const POPENITEMS = new Set();

/* The context every project-scoped row and button is built with. One place
   decides what "this project" means to toggleItem, deleteItem, itemRow and
   the editor, so a new button cannot forget the root and quietly act on your
   own config instead. */
function projCtx(st) {
  return {
    root: st.root, tilde: st.tilde,
    reload: () => renderProjects(true),
    activeStyle: null,
  };
}

/* Types where the docs state precedence across scopes, and which way it goes:

     "When skills share the same name across levels, enterprise overrides
      personal, and personal overrides project."
     — code.claude.com/docs/en/skills, fetched 2026-08-10

   Personal wins, which is the opposite of what most tools do with a project
   directory, and it means a project's copy of a name you also have is dead
   weight — it applies to nobody until yours is gone. The same paragraph says
   .claude/commands/ files "work the same way". Agents and output styles say
   nothing either way, so nothing is claimed about them here. */
const SHADOWED_TYPES = new Set(["skills", "commands"]);

const shadowedBy = (type, name) =>
  SHADOWED_TYPES.has(type)
  && ((DATA.items || {})[type] || []).some((m) => m.name === name && m.enabled);

async function renderProjects(reload) {
  const view = document.getElementById("projectsview");
  if (!await cached({ view: "projectsview", url: "/api/projects", reload,
                      get: () => PROJECTS, set: (v) => { PROJECTS = v; },
                      alive: () => TAB === "projects" })) return;
  view.innerHTML = "";
  if (PRESTORE) { view.append(projRestorePanel()); return; }
  view.append(el("div.view-head", {
    text: "Per-project system prompts. Each project keeps its prompt in its own .claude/ "
        + "directory: system-prompt.md replaces Claude Code's entire default system prompt, "
        + "append-system-prompt.md adds to it. Disable is a rename to .md.off — the whole "
        + "feature is plain files, nothing recorded elsewhere.",
  }));
  const f = PROJECTS.flags;
  if (!f.cli || !f.system_prompt_file) {
    view.append(el("div.alert.alert-warning", {},
      el("span.alert-icon", {}, icon("warn")),
      el("div.alert-body", { text: !f.cli
        ? "claude CLI not found on PATH — the wrappers will not work until it is installed."
        : "Your installed claude doesn't advertise --system-prompt-file; the wrappers would "
        + "fail with it. Update Claude Code to use this feature." })));
  }
  view.append(projMechanismCard());
  view.append(projListCard());
  for (const st of PROJECTS.projects) view.append(projCard(st));
}

function projMechanismCard() {
  const card = el("div.card", { style: { marginBottom: "1.25rem" } });
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "How it applies" }),
      el("div.card-description", {
        text: "Prompt files are data — passed to claude as flag arguments, never executed. "
            + "Two ways a session picks them up:" }))));
  const body = el("div.card-content.flush");
  body.append(el("div.drow", {},
    icon("terminal"),
    el("span.dmsg", { text: "Per project: run ./.claude/claude.sh instead of claude. "
      + "The script ships with the repo, so teammates get it without claude-ui." })));
  const z = PROJECTS.zsh;
  body.append(el("div.drow", {},
    icon("wrench"),
    el("span.dmsg", { text: "Everywhere: the zsh setup piece makes plain `claude` pick up "
      + "registered projects automatically. Only projects listed below are honored." }),
    z.installed ? badge("installed", "success") : badge("not installed", "outline"),
    el("div.dactions", {}, mkbtn("btn-sm", z.installed ? "Manage in Setup" : "Install in Setup",
      () => goTab("setup")))));
  body.append(el("div.drow", {},
    icon("file"),
    el("span.dmsg", { text: "bash: source the same function from ~/.bashrc" }),
    el("span.dmsg.dmono", { text: 'source "' + PROJECTS.zsh_file + '"' }),
    el("div.dactions", {}, mkbtn("btn-sm btn-ghost", "Copy",
      () => copyText('source "' + PROJECTS.zsh_file + '"', "bash line")))));
  card.append(body);
  return card;
}

function projListCard() {
  const card = el("div.card", { style: { marginBottom: "1.25rem" } });
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Registered projects" }),
      el("div.card-description", {
        text: "Recorded in " + PROJECTS.registry + " — also the allowlist the zsh function "
            + "checks, so nothing outside this list ever changes a claude invocation." }))));
  const body = el("div.card-content.flush");
  body.append(el("div.drow", {},
    el("span.dmsg", { text: PROJECTS.projects.length
      ? "Registering only records the path; files are written when you initialise."
      : "No projects yet. Registering only records the path; files are written when you "
      + "initialise a prompt." }),
    el("div.dactions", {}, mkbtn("btn-sm btn-primary", "Add project…", addProject))));
  for (const s of PROJECTS.suggestions) {
    body.append(el("div.drow", {},
      icon("folder"),
      el("span.dmsg.dmono", { text: s }),
      el("span.dmsg", { text: "seen in your session history" }),
      el("div.dactions", {}, mkbtn("btn-sm", "Add",
        () => projPost("project-add", { path: s }, "Added " + s)))));
  }
  card.append(body);
  return card;
}

async function addProject() {
  const r = await modal({
    title: "Add project",
    text: "Absolute path (or ~/…) of a project root. Registering only records the path — "
        + "nothing is written into the project until you initialise a prompt there.",
    fields: [{ id: "p", label: "Path", mono: true, placeholder: "~/src/my-project" }],
    ok: "Add",
  });
  if (!r || !r.p) return;
  projPost("project-add", { path: r.p }, "Project added");
}

function projCard(st) {
  const card = el("div.card", { style: { marginBottom: "1.25rem" } });
  const title = el("div", {},
    el("div.card-title", { text: st.tilde }),
    el("div.card-description", { text: st.mode
      ? (st.mode === "replace" ? "Replaces the entire default system prompt"
                               : "Appends to the default system prompt")
      : "No prompt file yet — initialise to create one." }));
  const badges = el("div", {});
  if (st.missing) badges.append(badge("missing", "destructive"));
  if (st.conflict) badges.append(badge("conflict", "destructive"));
  if (st.mode) {
    badges.append(badge(st.mode, "outline"));
    badges.append(st.enabled ? badge("enabled", "success") : badge("disabled", "outline"));
  }
  card.append(el("div.card-header", {}, title, badges));
  const body = el("div.card-content.flush");

  if (st.conflict) {
    body.append(el("div.drow", {},
      el("span.dmsg", { text: "Both a live and a disabled copy (or both modes) exist — "
        + "claude-ui won't guess which prompt is yours. Open .claude/ and remove one." })));
  }
  if (st.mode && !st.conflict) {
    const name = PROJ_FILES[st.mode] + (st.enabled ? "" : ".off");
    body.append(el("div.drow", {},
      icon("file"),
      el("span.dmsg.dmono", { text: ".claude/" + name }),
      el("div.dactions", {},
        openFileBtn(st.root + "/.claude/" + name, "Edit prompt"),
        mkbtn("btn-sm", st.mode === "replace" ? "Switch to append" : "Switch to replace",
          () => projPost("project-mode",
            { root: st.root, mode: st.mode === "replace" ? "append" : "replace" },
            "Mode switched")))));
    body.append(el("div.drow", {},
      el("span.dmsg", { text: st.enabled
        ? "Enabled — wrapper-launched sessions use this prompt."
        : "Disabled — the file is parked as " + name + "; plain claude behavior." }),
      el("div.dactions", {}, switchToggle("", st.enabled,
        (v) => projPost("project-toggle", { root: st.root, enabled: v },
          v ? "Enabled" : "Disabled")))));
  }
  if (!st.mode && !st.missing) {
    body.append(el("div.drow", {},
      el("span.dmsg", { text: "Initialise writes a starter prompt file and the claude.sh "
        + "wrapper into .claude/ — nothing else in the project is touched." }),
      el("div.dactions", {}, mkbtn("btn-sm btn-primary", "Initialise…", () => projInit(st)))));
  }

  const wbadge = { current: ["current", "success"], stale: ["stale", "outline"],
    foreign: ["not ours", "outline"], "not-executable": ["not executable", "destructive"],
    none: ["none", "outline"] }[st.wrapper];
  const wrow = el("div.drow", {},
    icon("terminal"),
    el("span.dmsg.dmono", { text: ".claude/claude.sh" }),
    badge(wbadge[0], wbadge[1]),
    el("div.dactions", {}));
  const wacts = wrow.querySelector(".dactions");
  const runnable = st.wrapper === "current" || st.wrapper === "stale"
    || st.wrapper === "not-executable";
  if (runnable) {
    const chk = mkbtn("btn-sm", "Check", () => projCheck(st),
      "Free: run the wrapper with --version and show which flag it passes");
    chk.prepend(icon("play"));
    wacts.append(chk);
    if (st.enabled)
      wacts.append(mkbtn("btn-sm", "Test live…", () => projTest(st),
        "Ask claude to quote your prompt file — spends one real claude call"));
  }
  if (st.wrapper !== "none")
    wacts.append(mkbtn("btn-sm btn-ghost", "Copy run command",
      () => copyText("./.claude/claude.sh", "run command")));
  if (st.wrapper !== "current" && !st.missing)
    wacts.append(mkbtn("btn-sm", st.wrapper === "none" ? "Create" : "Regenerate",
      () => projWrapper(st)));
  body.append(wrow);

  const tr = PROJTEST[st.root];
  if (tr) {
    body.append(el("div.drow", {},
      icon(tr.ok ? "check" : "warn"),
      tr.ok ? badge("prompt reached claude", "success")
            : badge("no match", "destructive"),
      el("span.dmsg", { text: tr.ok
        ? "The model quoted a line of your prompt file verbatim."
        : "The model's answer didn't match any line of your prompt file." })));
    body.append(el("div.drow", {},
      el("span.dmsg", { text: "Claude answered:" }),
      el("span.dmsg.dmono", { text: tr.answer || "(no output)" })));
    if (tr.ok)
      body.append(el("div.drow", {},
        el("span.dmsg", { text: "Matches this line of your file:" }),
        el("span.dmsg.dmono", { text: tr.matched_line })));
  }

  // What this project holds of its own, and every way to change it. A skill in
  // .claude/skills/ applies to this project only — the narrower scope beside
  // the personal ~/.claude/skills/ the inventory tabs manage.
  if (!st.missing) {
    body.append(el("div.drow.drow-head", {},
      icon("sparkles"),
      el("span.dmsg", {},
        el("div", { text: "This project's own skills, commands, agents and styles" }),
        el("div.hint", { text: "In .claude/ — this project only, and committable "
          + "with it. Disabling parks a copy in .claude/disabled/." }))));
    for (const [type, one] of PROJ_ITEMS) body.append(projItemsRow(st, type, one));
    body.append(projMcpRow(st));
    body.append(projFilesRow(st));
    body.append(projRegistryRow(st));
  }

  body.append(el("div.drow", {},
    el("span.dmsg", { text: "Removing only unregisters the project — its files stay." }),
    el("div.dactions", {}, mkbtn("btn-sm danger", "Remove", async () => {
      if (await mconfirm("Remove " + st.tilde + "?",
          "Removes the registry entry (and allowlisting) only. Files under " + st.tilde
          + "/.claude/ are left exactly as they are.", "Remove"))
        projPost("project-remove", { root: st.root }, "Project removed");
    }))));
  card.append(body);
  return card;
}

/* -------------------------------------------- a project's own MCP servers --
   .mcp.json sits at the project root, not in .claude/, because that is where
   Claude Code reads it. It is the file a repo ships to whoever clones it, so
   approving a server is a separate answer from having one: the repo asks, and
   this machine decides. Approval goes to .claude/settings.local.json, which
   is personal and normally gitignored — nobody else inherits your trust. */

const MCP_APPROVAL = {
  approved: ["approved", "success"],
  rejected: ["rejected", "destructive"],
  undecided: ["not answered", "outline"],
};

function projMcpRow(st) {
  const m = st.mcp || { servers: [] };
  const local = m.local_servers || [];
  const total = m.servers.length + local.length;
  const key = st.root + "/mcp";
  const open = POPENITEMS.has(key);
  const toggle = mkbtn("btn-sm btn-icon btn-ghost", "", () => {
    if (open) POPENITEMS.delete(key); else POPENITEMS.add(key);
    renderProjects();
  }, total ? (open ? "Collapse" : "Show them") : "Nothing here yet");
  toggle.append(icon("chevronDown"));
  if (!total) toggle.disabled = true;

  const add = mkbtn("btn-sm btn-primary", "Add server…", () => projMcpNew(st));
  add.prepend(icon("plus"));
  const head = el("div.drow", {},
    icon("server"),
    el("span.dmsg", {},
      el("div", { text: "MCP servers · " + (total || "none") }),
      el("div.hint", { text: ".mcp.json — beside .claude/, committed with the repo"
        + (local.length ? " · " + local.length + " local (just you)" : "") })),
    m.error ? badge("unreadable", "destructive") : null,
    m.local_error ? badge("local scope unreadable", "destructive") : null,
    el("div.dactions", {},
      toggle,
      m.exists ? openFileBtn(st.root + "/.mcp.json", "Edit file") : null,
      add));
  if (m.error)
    return el("div", {}, head, el("div.drow", {},
      el("span.dmsg", { text: m.error + " — fix it by hand; claude-ui will not "
        + "write over a file it cannot read." })));
  if (!open || !total) return head;

  const box = el("div.list", { style: { margin: "0 0 .5rem" } });
  for (const s of m.servers) {
    const [label, variant] = MCP_APPROVAL[s.approval] || MCP_APPROVAL.undecided;
    const b = badge(label, variant);
    b.title = s.approval === "undecided"
      ? "Claude Code will ask about this server the first time it is used"
      : "Recorded in this project's settings";
    const acts = el("div.li-actions", {},
      mkbtn("btn-sm", "Edit", () => projMcpEdit(st, s)),
      s.approval !== "approved"
        ? mkbtn("btn-sm", "Approve", () => projMcpApprove(st, s.name, true)) : null,
      s.approval !== "rejected"
        ? mkbtn("btn-sm danger", "Reject", () => projMcpApprove(st, s.name, false)) : null);
    const more = mkbtn("btn-sm btn-icon btn-ghost", "", (e) => openMenu(e.currentTarget, [
      ...(s.approval === "undecided" ? [] : [{ label: "Clear the answer", icon: "power",
        fn: () => projMcpApprove(st, s.name, null) }]),
      { label: "Move to user scope", icon: "arrowRight",
        fn: () => projMcpMoveOut(st, s.name) },
      { label: "Make local — just you", icon: "arrowRight",
        fn: () => projMcpMakeLocal(st, s.name) },
      { label: "Delete…", icon: "trash", danger: true,
        fn: () => projMcpDelete(st, s.name) },
    ]), "More actions");
    more.append(icon("chevronDown"));
    acts.append(more);
    box.append(el("div.list-item", {},
      el("div.li-main", {}, el("span.li-name", { text: s.name }), b),
      el("span.li-desc", { text: mcpSummary(s.config) }),
      acts));
  }
  for (const s of local) {
    const b = badge("local — just you", "outline");
    b.title = "In ~/.claude.json under this project's entry — where "
      + "`claude mcp add` writes by default. Not in the repo.";
    const more = mkbtn("btn-sm btn-icon btn-ghost", "", (e) => openMenu(e.currentTarget, [
      { label: "Move to user scope", icon: "arrowRight",
        fn: () => mcpMove(s.name, { scope: "local", root: st.root }, { scope: "user" }) },
      { label: "Share with the project…", icon: "arrowRight",
        fn: () => projMcpShare(st, s.name) },
    ]), "More actions");
    more.append(icon("chevronDown"));
    box.append(el("div.list-item", {},
      el("div.li-main", {}, el("span.li-name", { text: s.name }), b),
      el("span.li-desc", { text: mcpSummary(s.config) }),
      el("div.li-actions", {}, more)));
  }
  return el("div", {}, head, box);
}

async function projMcpMoveOut(st, name) {
  if (await mconfirm("Move " + name + " to user scope?",
      "Removes it from the repo's .mcp.json — teammates lose it on their next "
      + "pull. Your machine keeps it, in ~/.claude.json, for every project.",
      "Move"))
    mcpMove(name, { scope: "project", root: st.root }, { scope: "user" });
}

async function projMcpMakeLocal(st, name) {
  if (await mconfirm("Make " + name + " local?",
      "Removes it from the repo's .mcp.json — teammates lose it on their next "
      + "pull. It stays yours, in ~/.claude.json, for this project only.",
      "Make local"))
    mcpMove(name, { scope: "project", root: st.root },
            { scope: "local", root: st.root });
}

async function projMcpShare(st, name) {
  if (await mconfirm("Share " + name + " with the project?",
      "Writes it to .mcp.json — committed, so everyone who clones the repo is "
      + "offered it. Each of them still gets asked before it runs; for you it "
      + "is approved already.", "Share"))
    mcpMove(name, { scope: "local", root: st.root },
            { scope: "project", root: st.root });
}

async function projMcpNew(st) {
  const r = await modal({ title: "Add an MCP server to " + st.tilde,
    text: "Written to .mcp.json at the project root — committed, so everyone "
        + "who clones the repo is offered it. Each of them still gets asked "
        + "before it runs.",
    fields: [
      { id: "n", label: "Server name", mono: true },
      { id: "k", label: "Transport", type: "select", options: [
        { value: "stdio", label: "stdio — local command" },
        { value: "http", label: "http/sse — remote URL" }] }],
    ok: "Next" });
  if (!r || !r.n) return;
  projMcpEdit(st, { name: r.n, config: MCP_TEMPLATE[r.k] }, true);
}

async function projMcpEdit(st, server, isNew) {
  const r = await modal({
    title: (isNew ? "New server " : "Edit ") + server.name,
    text: "The server's entry in .mcp.json, as JSON.",
    fields: [{ id: "j", label: "Config", type: "textarea", mono: true, rows: 12,
               value: JSON.stringify(server.config || {}, null, 2) }],
    ok: "Save",
  });
  if (!r) return;
  let config;
  try { config = JSON.parse(r.j); }
  catch (e) { toast("Invalid JSON: " + e.message, true); return; }
  projPost("project-mcp-set", { root: st.root, name: server.name, config },
    server.name + " saved to .mcp.json");
}

async function projMcpDelete(st, name) {
  if (await mconfirm("Delete " + name + "?",
      "Removes it from " + st.tilde + "/.mcp.json. Anyone who has already "
      + "cloned the repo keeps their copy until they pull.", "Delete"))
    projPost("project-mcp-delete", { root: st.root, name }, name + " deleted");
}

const projMcpApprove = (st, name, approved) =>
  projPost("project-mcp-approve", { root: st.root, name, approved },
    approved === null ? name + ": answer cleared"
      : name + (approved ? " approved" : " rejected") + " on this machine");

/* ------------------------------------------------ a project's config files --
   Read-only display of these was already here; the editor has been able to
   open them since resolve_editable learned about project roots. This is the
   button that says so. */

function projFilesRow(st) {
  const files = [
    [".claude/settings.json", "shared with everyone who clones the repo"],
    [".claude/settings.local.json", "yours, normally gitignored"],
    ["CLAUDE.md", "project instructions, read every session"],
  ];
  const acts = el("div.dactions", {});
  for (const [rel, why] of files)
    acts.append(openFileBtn(st.root + "/" + rel, rel.replace(".claude/", ""),
      null, rel + " — " + why));
  const setting = Object.entries(st.output_style_setting)
    .map(([f, v]) => v + " (" + f + ")").join(", ");
  return el("div.drow", {},
    icon("file"),
    el("span.dmsg", {},
      el("div", { text: "Settings and instructions" }),
      el("div.hint", { text: setting ? "outputStyle: " + setting
        : "Opens in the editor; a file that isn't there yet is created on save." })),
    acts);
}

/* ------------------------------------------------ a project's own items --
   One collapsed section per type. Collapsed because a card is a summary and
   a project with twenty commands would bury the prompt controls the tab is
   named for; the count is on the header, so opening it is a choice you make
   knowing what is inside. */

/* Two things a project row can be that a config-dir row cannot: overridden
   off from settings, and shadowed by one of your own. Both are the difference
   between "this file exists" and "this file does anything", which is the only
   question the card is really being asked. */
function projItemBadges(st, type, s) {
  const out = [];
  const ov = (st.skill_overrides || {})[s.name];
  if (ov && ov !== "on") {
    const b = badge(ov === "off" ? "off for you" : ov, "outline");
    b.title = "skillOverrides in this project's settings — the file is untouched";
    out.push(b);
  }
  if (s.enabled && shadowedBy(type, s.name)) {
    const b = badge("shadowed by yours", "warning");
    b.title = "You have a " + type.replace(/s$/, "") + " called " + s.name
      + " too, and personal overrides project — this copy does not apply "
      + "until yours is disabled or renamed";
    out.push(b);
  }
  return out;
}

function projItemsRow(st, type, one) {
  const ctx = projCtx(st);
  const rows = (st.items || {})[type] || [];
  const key = st.root + "/" + type;
  const open = POPENITEMS.has(key);
  // Which style this project's settings select. Read-only at user scope would
  // be wrong here: outputStyle in a project's settings names a project style.
  if (type === "output-styles")
    ctx.activeStyle = st.output_style_setting["settings.local.json"]
      || st.output_style_setting["settings.json"] || "";
  ctx.setActive = (name) => projPost("project-setting-set",
    { root: st.root, key: "outputStyle", value: name },
    "outputStyle set to " + name + " for this project");
  ctx.copyTo = [
    { label: "Copy to your config", icon: "copy",
      fn: () => projItemCopyOut(st, type, rows) },
    { label: "Move to your config", icon: "arrowRight",
      fn: () => projItemMoveOut(st, type, rows) },
  ];
  ctx.extraBadges = (s) => projItemBadges(st, type, s);
  // Claude Code's own per-skill switch, and the only one that leaves a
  // committed skill's file alone — see project_skill_override().
  if (type === "skills")
    ctx.extraMenu = (s) => {
      const off = (st.skill_overrides || {})[s.name] === "off";
      return [{ label: off ? "Turn back on for me" : "Turn off for me",
        icon: "power",
        fn: () => projPost("project-skill-override",
          { root: st.root, name: s.name, value: off ? null : "off" },
          s.name + (off ? " on again" : " off in settings.local.json")) }];
    };

  const off = rows.filter((r) => !r.enabled).length;
  const badges = [];
  if (off) badges.push(badge(off + " disabled", "outline"));

  const toggle = mkbtn("btn-sm btn-icon btn-ghost", "", () => {
    if (open) POPENITEMS.delete(key); else POPENITEMS.add(key);
    renderProjects();
  }, rows.length ? (open ? "Collapse" : "Show them") : "Nothing here yet");
  toggle.append(icon("chevronDown"));
  if (!rows.length) toggle.disabled = true;

  const head = el("div.drow", {},
    icon(TAB_META[type].icon),
    el("span.dmsg", {},
      el("div", { text: TAB_META[type].label + " · "
        + (rows.length ? rows.length : "none") }),
      el("div.hint", { text: ".claude/" + type + "/" })),
    ...badges,
    el("div.dactions", {},
      toggle,
      (() => {
        const b = mkbtn("btn-sm btn-primary", "Add",
          (e) => openMenu(e.currentTarget, projAddMenu(st, type, one)),
          "Put a " + one + " in this project");
        b.prepend(icon("plus"));
        return b;
      })()));
  if (!open || !rows.length) return head;

  const box = el("div.list", { style: { margin: "0 0 .5rem" } });
  for (const s of rows) box.append(itemRow(type, s, s.enabled, ctx));
  return el("div", {}, head, box);
}

function projAddMenu(st, type, one) {
  const out = [
    { label: "New " + one + "…", icon: "plus", fn: () => projItemNew(st, type, one) },
    { label: "Copy from your config…", icon: "copy",
      fn: () => projItemCopyIn(st, type, one) },
    { label: "Move from your config…", icon: "arrowRight",
      fn: () => projItemMoveIn(st, type, one) },
  ];
  // Output styles are not in PROJECT_ITEM_TYPES, so an archive has no copy of
  // one to put here — offering the button would be offering a dead end.
  if (type !== "output-styles")
    out.push({ label: "Restore from backup…", icon: "archive",
               fn: () => projRestoreOpen(st) });
  return out;
}

/* New writes a stub and opens it. Deliberately not a builder: the repo already
   weighed a guided form for these types and chose the terminal over a
   half-guided one. What is different here is that a project has no terminal
   equivalent worth preferring — you would be hand-making directories — so
   this does the two lines of frontmatter and gets out of the way. */
const projStub = (type, name, desc) =>
  type === "commands"
    ? "---\ndescription: " + desc + "\n---\n\n"
    : "---\nname: " + name + "\ndescription: " + desc + "\n---\n\n";

async function projItemNew(st, type, one) {
  const r = await modal({
    title: "New " + one + " in " + st.tilde,
    text: "Writes .claude/" + type + "/ and opens it in the editor. It applies "
        + "to this project only, and commits with it.",
    fields: [
      { id: "n", label: "Name", mono: true,
        hint: type === "commands" ? "Slashes nest it: git/pr becomes /git:pr"
                                  : "Letters, digits, dot, dash, underscore" },
      { id: "d", label: "Description",
        hint: "What it is for. Claude reads this to decide when to use it." },
    ],
    ok: "Create",
  });
  if (!r || !r.n) return;
  try {
    await api("/api/item-create", { type, name: r.n, root: st.root,
      content: projStub(type, r.n, (r.d || "").trim() || "TODO") });
    toast(r.n + " created in " + st.tilde);
    PROJECTS = null;
    openItemEditor(type, r.n, null, true, null, st.root);
  } catch (e) { toast(e.message, true); }
}

async function projItemCopyIn(st, type, one) {
  const mine = ((DATA.items || {})[type] || []).filter((s) => !s.broken);
  if (!mine.length) {
    toast("You have no " + type + " to copy", true);
    return;
  }
  const have = new Set(((st.items || {})[type] || []).map((s) => s.name));
  const r = await modal({
    title: "Copy a " + one + " into " + st.tilde,
    text: "Copies the files, leaving yours where they are. The project's copy "
        + "is then its own — editing it here does not change yours."
        + (SHADOWED_TYPES.has(type)
           ? " Note that personal overrides project: while you still have your "
             + "copy enabled, it is the one that applies, and the project's is "
             + "inert until you disable yours."
           : ""),
    fields: [{ id: "n", label: one[0].toUpperCase() + one.slice(1), type: "select",
      options: mine.map((s) => ({
        value: s.name,
        label: s.name + (have.has(s.name) ? " — already in this project" : "")
          + (s.enabled ? "" : " (disabled)"),
      })) }],
    ok: "Copy",
  });
  if (!r || !r.n) return;
  const src = mine.find((s) => s.name === r.n);
  await projPost("item-copy", { type, name: r.n, to_root: st.root,
    enabled: src ? src.enabled : true }, r.n + " copied into " + st.tilde);
  // The copy landed shadowed. Offer the one action that makes it apply,
  // rather than leaving a badge to explain why nothing happened.
  if (src && src.enabled && SHADOWED_TYPES.has(type)
      && await mconfirm("Disable your own " + r.n + "?",
        "Personal overrides project, so your " + r.n + " is still the one that "
        + "applies everywhere, including here. Disabling yours lets this "
        + "project's copy take effect; it parks in disabled/ and nothing is "
        + "deleted.", "Disable mine"))
    await toggleItem(type, r.n, false);
}

async function projItemCopyOut(st, type, rows) {
  const r = await modal({
    title: "Copy out of " + st.tilde,
    text: "Copies into your own " + (DATA.config_dir || "~/.claude") + "/" + type
        + "/, where every project sees it. The project keeps its copy.",
    fields: [{ id: "n", label: "Which", type: "select",
      options: rows.map((s) => ({ value: s.name,
        label: s.name + (s.enabled ? "" : " (disabled)") })) }],
    ok: "Copy",
  });
  if (!r || !r.n) return;
  const src = rows.find((s) => s.name === r.n);
  try {
    await api("/api/item-copy", { type, name: r.n, from_root: st.root,
      enabled: src ? src.enabled : true });
    toast(r.n + " copied to your config");
    await refresh();          // it is in DATA now, not just PROJECTS
    renderProjects(true);
  } catch (e) { toast(e.message, true); }
}

/* Move is copy's other half: the same modal shapes, but the source goes away,
   so the shadowing story inverts. Copying into a project leaves your copy
   shadowing the new one; moving cannot — there is nothing left to shadow. */
async function projItemMoveIn(st, type, one) {
  const mine = ((DATA.items || {})[type] || []).filter((s) => !s.broken);
  if (!mine.length) {
    toast("You have no " + type + " to move", true);
    return;
  }
  const r = await modal({
    title: "Move a " + one + " into " + st.tilde,
    text: "The files move into the project and your copy is removed — unlike "
        + "Copy, nothing is left behind to keep in sync"
        + (SHADOWED_TYPES.has(type)
           ? ", and nothing of yours is left to shadow the project's copy"
           : "") + ". A symlinked " + one + " moves its contents; the link "
        + "target is untouched.",
    fields: [{ id: "n", label: one[0].toUpperCase() + one.slice(1), type: "select",
      options: mine.map((s) => ({ value: s.name,
        label: s.name + (s.enabled ? "" : " (disabled)") })) }],
    ok: "Move",
  });
  if (!r || !r.n) return;
  const src = mine.find((s) => s.name === r.n);
  if (type === "output-styles" && src
      && styleSettingName(src) === (settingsGet("outputStyle") || "")
      && !await mconfirm("Move your active style?",
        "outputStyle in your settings selects " + r.n + ". After the move the "
        + "setting points at a style you no longer have, and Claude Code falls "
        + "back to the default.", "Move anyway"))
    return;
  try {
    await api("/api/item-move", { type, name: r.n, to_root: st.root,
      enabled: src ? src.enabled : true });
    toast(r.n + " moved into " + st.tilde);
    await refresh();          // it left DATA as well as joining the project
    renderProjects(true);
  } catch (e) { toast(e.message, true); }
}

async function projItemMoveOut(st, type, rows) {
  const r = await modal({
    title: "Move out of " + st.tilde,
    text: "Moves into your own " + (DATA.config_dir || "~/.claude") + "/" + type
        + "/, where every project sees it. The project's copy is removed — if "
        + "it is committed, that shows up as a deletion in the next diff."
        + (SHADOWED_TYPES.has(type)
           ? " Personal overrides project, so the moved copy will shadow any "
             + type.replace(/s$/, "") + " of the same name in other projects."
           : ""),
    fields: [{ id: "n", label: "Which", type: "select",
      options: rows.map((s) => ({ value: s.name,
        label: s.name + (s.enabled ? "" : " (disabled)") })) }],
    ok: "Move",
  });
  if (!r || !r.n) return;
  const src = rows.find((s) => s.name === r.n);
  if (type === "output-styles" && src && (ctxStyle(st) === styleSettingName(src))
      && !await mconfirm("Move this project's active style?",
        "This project's outputStyle setting selects " + r.n + ". After the "
        + "move the setting points at a style the project no longer has.",
        "Move anyway"))
    return;
  try {
    await api("/api/item-move", { type, name: r.n, from_root: st.root,
      enabled: src ? src.enabled : true });
    toast(r.n + " moved to your config");
    await refresh();          // it is in DATA now, not just PROJECTS
    renderProjects(true);
  } catch (e) { toast(e.message, true); }
}

// Which style a project's settings select — the same read projItemsRow makes.
const ctxStyle = (st) => st.output_style_setting["settings.local.json"]
  || st.output_style_setting["settings.json"] || "";

/* ------------------------------------------- marketplaces, for one project --
   The one part of this tab that does not edit a file directly. Adding a
   marketplace clones a repository and installing a plugin resolves a version
   into a cache — Claude Code's decisions, made by Claude Code's own CLI, run
   here in the project's directory with --scope project. What lands in the
   repo is two keys in .claude/settings.json, so a teammate who clones gets
   the same tools.

   The plugin's files do not land in the project. They go to
   ~/.claude/plugins/, shared with every other project on this machine; only
   the decision to use them is the project's. The hint says so, because a
   section that let you believe otherwise would be lying about what committing
   the repo gives away. */

const PREG = {};   // {root: registry_state payload} — fetched on demand

function projRegistryRow(st) {
  const key = st.root + "/registry";
  const open = POPENITEMS.has(key);
  const reg = PREG[st.root];
  const toggle = mkbtn("btn-sm btn-icon btn-ghost", "", () => {
    if (open) { POPENITEMS.delete(key); renderProjects(); }
    else { POPENITEMS.add(key); projRegistryLoad(st); }
  }, open ? "Collapse" : "Load this project's marketplaces (runs claude plugin list)");
  toggle.append(icon("chevronDown"));

  const head = el("div.drow", {},
    icon("plug"),
    el("span.dmsg", {},
      el("div", { text: "Plugin marketplaces" }),
      el("div.hint", { text: "Recorded in .claude/settings.json and committed. "
        + "The plugin's files stay in ~/.claude/plugins/ — only the choice is "
        + "the project's." })),
    el("div.dactions", {}, toggle));
  if (!open) return head;
  if (!reg) return el("div", {}, head,
    el("div.drow", {}, el("span.dmsg", { text: CLI_WAIT })));

  const rows = [];
  if (reg.error)
    rows.push(el("div.drow", {},
      icon("warn"), el("span.dmsg", { text: reg.error })));

  for (const m of reg.marketplaces)
    rows.push(el("div.drow", {},
      icon("download"),
      el("span.dmsg.dmono", { text: m.name || String(m) }),
      el("div.dactions", {}, mkbtn("btn-sm danger", "Remove", () =>
        projRegistryRun(st, "project-marketplace-remove",
          { name: m.name }, "Marketplace removed")))));

  for (const s of reg.suggested)
    if (!reg.marketplaces.some((m) => (m.name || "") === s.source.split("/")[1]))
      rows.push(el("div.drow", {},
        icon("download"),
        el("span.dmsg", {},
          el("div.dmono", { text: s.source }),
          el("div.hint", { text: s.desc })),
        el("div.dactions", {}, mkbtn("btn-sm", "Add", () =>
          projRegistryRun(st, "project-marketplace-add",
            { source: s.source }, "Marketplace added")))));

  rows.push(el("div.drow", {},
    el("span.dmsg", { text: "Any GitHub repo, git URL or local path works too." }),
    el("div.dactions", {}, mkbtn("btn-sm", "Add marketplace…",
      () => projMarketAdd(st)))));

  for (const p of reg.installed)
    rows.push(el("div.drow", {},
      icon("plug"),
      el("span.dmsg", {},
        el("div.dmono", { text: p.id || p.name || "" }),
        el("div.hint", { text: "scope: " + (p.scope || "?")
          + (p.version ? " · v" + p.version : "") })),
      p.scope === "project" ? badge("this project", "success")
                            : badge(p.scope || "?", "outline"),
      el("div.dactions", {}, p.scope === "project"
        ? mkbtn("btn-sm danger", "Uninstall", () => projPluginRemove(st, p))
        : null)));

  for (const p of reg.available)
    rows.push(el("div.drow", {},
      icon("plug"),
      el("span.dmsg", {},
        el("div.dmono", { text: p.pluginId || p.name || "" }),
        el("div.hint", { text: p.description || "" })),
      el("div.dactions", {}, mkbtn("btn-sm btn-primary", "Install", () =>
        projRegistryRun(st, "project-plugin-install",
          { id: p.pluginId }, "Installed for this project")))));

  if (!reg.marketplaces.length && !reg.installed.length)
    rows.push(el("div.drow", {},
      el("span.dmsg", { text: "No marketplaces here yet. Adding one only "
        + "records where to look; nothing runs until you install a plugin." })));

  return el("div", {}, head, ...rows);
}

async function projRegistryLoad(st) {
  renderProjects();               // draw the CLI_WAIT line first
  try { PREG[st.root] = await api("/api/project-registry", { root: st.root }); }
  catch (e) {
    PREG[st.root] = { error: e.message, marketplaces: [], installed: [],
                      available: [], suggested: [] };
  }
  if (TAB === "projects") renderProjects();
}

async function projRegistryRun(st, action, body, msg) {
  const t = toast({ title: "Running claude plugin…", variant: "loading", duration: 0 });
  try {
    const r = await api("/api/" + action, { root: st.root, ...body });
    t.close();
    // the CLI's own last line, not our guess at what it did
    toast(r.ok ? msg + " · " + r.detail : r.detail, !r.ok);
    delete PREG[st.root];
    await projRegistryLoad(st);
  } catch (e) { t.close(); toast(e.message, true); }
}

async function projMarketAdd(st) {
  const r = await modal({
    title: "Add a marketplace to " + st.tilde,
    text: "Runs `claude plugin marketplace add … --scope project`, which "
        + "clones the source and records it in .claude/settings.json. Add "
        + "sources you trust: a plugin can ship hooks, which run commands.",
    fields: [{ id: "s", label: "Source", mono: true,
      placeholder: "owner/repo",
      hint: "A GitHub owner/repo, a git URL, or a path to a local directory" }],
    ok: "Add",
  });
  if (!r || !r.s) return;
  projRegistryRun(st, "project-marketplace-add", { source: r.s }, "Marketplace added");
}

async function projPluginRemove(st, p) {
  const id = p.id || p.name || "";
  if (await mconfirm("Uninstall " + id + "?",
      "Removes it from this project's .claude/settings.json. Other projects "
      + "using it are unaffected.", "Uninstall"))
    projRegistryRun(st, "project-plugin-uninstall", { id }, "Uninstalled");
}

async function projInit(st) {
  const r = await modal({
    title: "Initialise system prompt",
    text: "Writes a starter prompt file plus the claude.sh wrapper into " + st.tilde
        + "/.claude/. Append keeps Claude Code's default prompt; replace discards all of it "
        + "— tone, workflow and safety guidance included — so spell out what you need.",
    fields: [{ id: "m", label: "Mode", type: "select", value: "append",
               options: [
                 { value: "append", label: "Append — add project instructions to the default prompt" },
                 { value: "replace", label: "Replace — swap out the entire default prompt" }] }],
    ok: "Initialise",
  });
  if (!r) return;
  projPost("project-init", { root: st.root, mode: r.m },
    "Initialised — now edit the prompt");
}

async function projWrapper(st) {
  try {
    await api("/api/project-wrapper", { root: st.root });
    toast("Wrapper written");
    renderProjects(true);
  } catch (e) {
    if (/wasn't written by claude-ui/.test(e.message)) {
      if (await mconfirm("Replace your claude.sh?",
          e.message + " Regenerating replaces it with the claude-ui version.", "Replace"))
        projPost("project-wrapper", { root: st.root, force: true }, "Wrapper written");
    } else toast(e.message, true);
  }
}

async function projPost(action, body, msg) {
  try {
    await api("/api/" + action, body);
    if (msg) toast(msg);
    renderProjects(true);
  } catch (e) { toast(e.message, true); }
}

async function projCheck(st) {
  const t = toast({ title: "Checking the wrapper…", variant: "loading", duration: 0 });
  try {
    const r = await api("/api/project-check", { root: st.root });
    t.close();
    if (!r.ok) toast("Wrapper failed: " + (r.stderr || "nonzero exit"), true);
    else if (r.mode === "none")
      toast("Wrapper ran plain claude (no live prompt file) · " + r.version, true);
    else toast("Wrapper passed the " + r.mode + " flag · " + r.version);
  } catch (e) { t.close(); toast(e.message, true); }
}

async function projTest(st) {
  if (!(await mconfirm("Run a live test?",
      "Asks claude — through ./.claude/claude.sh — to quote the first line of your "
      + "prompt file. Spends one real claude call (API or subscription), like any "
      + "claude -p run. Takes up to a minute.", "Run test")))
    return;
  const t = toast({ title: "Asking claude…", variant: "loading", duration: 0 });
  try {
    const r = await api("/api/project-test", { root: st.root });
    t.close();
    PROJTEST[st.root] = r;
    toast(r.ok ? "Prompt reached claude" : "Answer didn't match — details on the card", !r.ok);
    renderProjects();
  } catch (e) { t.close(); toast(e.message, true); }
}

/* -------------------------------------------- restore into one project --
   The Backup tab puts a skill back where it came from: ~/.claude/skills/,
   which every project sees. This puts it in <project>/.claude/skills/
   instead — the scope Claude Code documents beside the personal one, meaning
   this project and nothing else. Same dry run, same rows, same promise that
   nothing is deleted; only the destination differs. */

async function projRestoreOpen(st) {
  const t = toast({ title: "Reading archives…", variant: "loading", duration: 0 });
  try {
    const b = await api("/api/archives");
    t.close();
    PRESTORE = { root: st.root, tilde: st.tilde, step: "archives",
                 dir: b.dir, archives: b.archives || [] };
    renderProjects();
  } catch (e) { t.close(); toast(e.message, true); }
}

async function projRestorePick(name) {
  const t = toast({ title: "Comparing with this project…", variant: "loading", duration: 0 });
  try {
    const rep = await api("/api/project-restore-inspect", { root: PRESTORE.root, name });
    t.close();
    // "already here" is the fact worth leading with, so it goes on the unit's
    // own line rather than into a legend somewhere
    const here = new Set();
    for (const [type] of PROJ_ITEMS)
      for (const n of (rep.present || {})[type] || []) here.add(type + "/" + n);
    for (const e of rep.entries)
      if (here.has(e.unit)) e.unit_desc = (e.unit_desc || "") + " · already in this project";
    // an item the project already has starts unticked: restoring your own
    // config back is not the same decision as importing over something that
    // is already working here
    PRESTORE = { ...PRESTORE, step: "review", rep, showSame: false,
      open: new Set(),
      picked: new Set(rep.entries
        .filter((e) => !here.has(e.unit) && (e.status === "new" || e.status === "differs"))
        .map((e) => e.path)) };
    renderProjects();
  } catch (e) { t.close(); toast(e.message, true); }
}

function projRestorePanel() {
  return PRESTORE.step === "review" ? projRestoreReview() : projRestoreArchives();
}

function projRestoreArchives() {
  const wrap = el("div", {});
  wrap.append(el("div.toolbar", {},
    mkbtn("btn-sm", "← Back to projects", () => { PRESTORE = null; renderProjects(); }),
    el("div.toolbar-end", {}, el("span.hint", { text: "into " + PRESTORE.tilde + "/.claude/" }))));
  const card = el("div.card");
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Restore into " + PRESTORE.tilde }),
      el("div.card-description", {
        text: "Pick an archive. Its skills, commands and agents can land in this project's "
            + "own .claude/, where they apply to this project only — nothing is written "
            + "until you tick files on the next step. Archives live in " + PRESTORE.dir + "." }))));
  const body = el("div.card-content.flush");
  if (!PRESTORE.archives.length) {
    body.append(el("div.drow", {},
      el("span.dmsg", { text: "No archives yet. The Backup tab writes them." }),
      el("div.dactions", {}, mkbtn("btn-sm", "Go to Backup", () => {
        PRESTORE = null; goTab("backup");
      }))));
  }
  for (const a of PRESTORE.archives) {
    const row = el("div.drow", {},
      icon("archive"),
      el("span.dmsg", {},
        el("div.li-name", { text: a.name }),
        el("div.hint", { text: a.error ? a.error
          : [a.created_at, a.note, plural(a.files || 0, "file"), fbytes(a.bytes || 0)]
            .filter(Boolean).join(" · ") })),
      el("div.dactions", {}));
    if (a.error) row.insertBefore(icon("warn"), row.firstChild);
    else row.querySelector(".dactions").append(
      mkbtn("btn-sm btn-primary", "Choose", () => projRestorePick(a.name)));
    body.append(row);
  }
  card.append(body);
  wrap.append(card);
  return wrap;
}

function projRestoreReview() {
  const s = PRESTORE, rep = s.rep, c = rep.counts || {};
  const wrap = el("div", {});
  wrap.append(el("div.toolbar", {},
    mkbtn("btn-sm", "← Back to archives", () => {
      PRESTORE = { ...PRESTORE, step: "archives", rep: null };
      renderProjects();
    }),
    el("div.toolbar-end", {},
      el("span.hint", { text: "restoring into " + rep.claude_dir + "/" }))));

  wrap.append(el("div.stat-grid", { style: { marginBottom: "1rem" } },
    statCard(String(c.new || 0), "new", { accent: true, hint: "not in this project" }),
    statCard(String(c.differs || 0), "differs", { hint: "here but not identical" }),
    statCard(String(c.same || 0), "identical", { hint: "writing them changes nothing" }),
    statCard(String((c.refused || 0) + (c.missing || 0)), "unusable",
      { hint: "listed but unreadable or unsafe" })));

  const card = el("div.card");
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: rep.name }),
      el("div.card-description", {
        text: "Only skills, commands and agents are shown — the rest of an archive has no "
            + "project form. Nothing is deleted: restoring over an item already here writes "
            + "the archived files and leaves any other file in that folder alone." }))));

  const body = el("div.card-content.flush");
  const rows = rep.entries.filter((e) => s.showSame || e.status !== "same");
  const count = el("span.hint", {});
  const sync = () => { count.textContent = s.picked.size + " selected"; };

  card.append(el("div.card-content",
    { style: { display: "flex", gap: ".5rem", alignItems: "center" } },
    count,
    el("span.spring", { style: { flex: "1" } }),
    switchToggle("Show identical", s.showSame,
      (v) => { PRESTORE.showSame = v; renderProjects(); }),
    mkbtn("btn-sm btn-ghost", "All", () => {
      const usable = rows.filter((e) => e.status !== "refused" && e.status !== "missing");
      const on = !usable.every((e) => s.picked.has(e.path));
      for (const e of usable) {
        if (on) s.picked.add(e.path); else s.picked.delete(e.path);
      }
      renderProjects();
    })));

  if (!rep.entries.length)
    body.append(el("div.drow", {},
      el("span.dmsg", { text: "This archive holds no skills, commands or agents." })));
  for (const node of unitRows(rows, s, sync)) body.append(node);
  card.append(body);

  const apply = mkbtn("btn btn-primary", "Restore selected", () => projRestoreApply());
  apply.prepend(icon("upload"));
  card.append(el("div.card-content",
    { style: { display: "flex", justifyContent: "flex-end" } }, apply));
  sync();
  wrap.append(card);
  return wrap;
}

async function projRestoreApply() {
  const s = PRESTORE;
  const paths = [...s.picked];
  if (!paths.length) { toast("Nothing selected", true); return; }
  const over = s.rep.entries.filter((e) => s.picked.has(e.path) && e.status === "differs").length;
  const ok = await mconfirm("Restore " + plural(paths.length, "file") + " into " + s.tilde + "?",
    (over ? plural(over, "file") + " already in this project will be overwritten with the "
          + "backup's version, and that cannot be undone from here. " : "")
    + "Files land in " + s.rep.claude_dir + ", so they apply to this project only — and "
    + "they are ordinary files you can commit. Nothing is deleted, and your personal "
    + "~/.claude is not touched.",
    over ? "Overwrite and restore" : "Restore");
  if (!ok) return;
  try {
    const r = await api("/api/project-restore",
      { root: s.root, name: s.rep.name, paths });
    if (r.failed_count)
      toast(r.count + " restored, " + r.failed_count + " failed: " + r.failed[0].error, true);
    else
      toast(plural(r.count, "file") + " restored into " + s.tilde);
    PRESTORE = null;
    renderProjects(true);
  } catch (e) { toast(e.message, true); }
}

// -------------------------------------------------------------- statusline

let STL = null;
let STL_DRAG = null;

const STL_COLORS = { yellow: "var(--yellow)", blue: "var(--blue)", green: "var(--green)",
  aqua: "var(--aqua)", orange: "var(--orange)", gray: "var(--muted-foreground)",
  purple: "var(--purple)", red: "var(--red)" };
let STL_WIDTH = null;  // preview truncation width in columns (null = full)

const STL_MAX_LINES = 4; // the config format has three line-break fields (br1-3)

// One-click starting layouts; colors and bold overrides are kept as-is.
const STL_PRESETS = {
  minimal: [["model", "dir", "branch", "context"]],
  standard: [["model", "effort", "dir", "branch", "context", "cost", "lines"]],
  "two-line": [["model", "effort", "repo", "branch", "context", "tokens"],
               ["cost", "costtoday", "duration", "lines", "rate5h", "rate7d"]],
};

function stlPreset(name) {
  const layout = STL_PRESETS[name];
  const used = new Set(layout.flat());
  STL.lines = layout.map((l) => [...l]);
  STL.palette = [...stlAvail().keys()]
    .filter((id) => !/^br[123]$/.test(id) && !used.has(id));
  STL.sel = null;
  renderStatusline();
}

function stlAvail() {
  return new Map(((DATA.statusline || {}).available || []).map((f) => [f.id, f]));
}

// The saved config is a flat ordered field list with br1-3 pseudo-fields as
// line breaks; the UI models it as lines of enabled chips plus a palette of
// unused fields, with color overrides kept aside so they survive removal.
function stlInit() {
  const st = DATA.statusline || {};
  const cfg = st.config || st.default || { separator: "  ", fields: [] };
  const avail = stlAvail();
  const lines = [[]];
  const palette = [];
  const colors = {};
  const bold = {};
  const seen = new Set();
  const all = [...(cfg.fields || [])];
  for (const f of st.available || [])
    if (!all.some((x) => x.id === f.id)) all.push({ id: f.id, enabled: false });
  for (const f of all) {
    if (!avail.has(f.id) || seen.has(f.id)) continue;
    seen.add(f.id);
    if (f.color) colors[f.id] = f.color;
    if (f.bold) bold[f.id] = true;
    if (/^br[123]$/.test(f.id)) {
      if (f.enabled && lines.length < STL_MAX_LINES) lines.push([]);
      continue;
    }
    if (f.enabled) lines[lines.length - 1].push(f.id);
    else palette.push(f.id);
  }
  STL = { separator: cfg.separator !== undefined ? cfg.separator : "  ",
    refresh: cfg.refresh || 0, lines, palette, colors, bold, sel: null };
}

// Back to the flat format: enabled fields line by line with br1-3 between
// lines, then the unused fields (disabled) so their order and colors persist.
function stlFields() {
  const fields = [];
  STL.lines.forEach((line, i) => {
    if (i > 0) fields.push({ id: "br" + i, enabled: true });
    for (const id of line) {
      const e = { id, enabled: true };
      if (STL.colors[id]) e.color = STL.colors[id];
      if (STL.bold[id]) e.bold = true;
      fields.push(e);
    }
  });
  for (let i = STL.lines.length; i <= 3; i++) fields.push({ id: "br" + i, enabled: false });
  for (const id of STL.palette) {
    const e = { id, enabled: false };
    if (STL.colors[id]) e.color = STL.colors[id];
    if (STL.bold[id]) e.bold = true;
    fields.push(e);
  }
  return fields;
}

function stlColor(id) {
  const c = STL.colors[id] || (stlAvail().get(id) || {}).color;
  return c && c.startsWith("#") ? c : STL_COLORS[c] || "var(--term-foreground)";
}

function stlPalAdd(id) {
  STL.palette.push(id);
  const order = new Map([...stlAvail().keys()].map((k, i) => [k, i]));
  STL.palette.sort((a, b) => order.get(a) - order.get(b));
}

// Move the dragged chip (from a line or the palette) into line li; fi is the
// insertion index, or null to append.
function stlDrop(li, fi) {
  const d = STL_DRAG;
  STL_DRAG = null;
  if (!d) return;
  let at = fi;
  if (d.src === "line") {
    if (d.line === li && at !== null && d.idx < at) at--;
    STL.lines[d.line].splice(d.idx, 1);
  } else {
    STL.palette = STL.palette.filter((x) => x !== d.id);
  }
  const line = STL.lines[li];
  line.splice(at === null ? line.length : at, 0, d.id);
  renderStatusline();
}

function stlRemove(id) {
  for (const line of STL.lines) {
    const i = line.indexOf(id);
    if (i >= 0) line.splice(i, 1);
  }
  stlPalAdd(id);
  if (STL.sel === id) STL.sel = null;
  renderStatusline();
}

function stlSetColor(id, color) {
  if (color) STL.colors[id] = color;
  else delete STL.colors[id];
  renderStatusline();
}

function stlChip(id, li, fi) {
  const a = stlAvail().get(id);
  const col = stlColor(id);
  const chip = el("span.stlchip", {
    class: STL.sel === id ? "sel" : "",
    draggable: true,
    title: a.desc + " (drag to move, click for colour & bold)",
    style: { color: col, borderColor: col, fontWeight: STL.bold[id] ? "700" : "" },
    onclick: () => { STL.sel = STL.sel === id ? null : id; renderStatusline(); },
  });
  chip.ondragstart = (e) => {
    STL_DRAG = { src: "line", line: li, idx: fi, id };
    e.dataTransfer.setData("text/plain", id);
    e.dataTransfer.effectAllowed = "move";
    chip.classList.add("dragging");
  };
  chip.ondragend = () => { STL_DRAG = null; chip.classList.remove("dragging"); };
  chip.ondragover = (e) => {
    e.preventDefault();
    const r = chip.getBoundingClientRect();
    const left = e.clientX - r.left < r.width / 2;
    chip.classList.toggle("ins-l", left);
    chip.classList.toggle("ins-r", !left);
  };
  chip.ondragleave = () => chip.classList.remove("ins-l", "ins-r");
  chip.ondrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    chip.classList.remove("ins-l", "ins-r");
    const r = chip.getBoundingClientRect();
    stlDrop(li, e.clientX - r.left < r.width / 2 ? fi : fi + 1);
  };
  chip.append(
    el("span", { text: a.label }),
    el("span.smp", { text: a.sample }),
    el("span.x", {
      text: "×", title: "Remove from the statusline",
      onclick: (e) => { e.stopPropagation(); stlRemove(id); },
    }));
  return chip;
}

function stlPalChip(id) {
  const a = stlAvail().get(id);
  const chip = el("span.stlchip.pal", {
    draggable: true,
    title: a.sample + " — " + a.desc + " (click or drag onto a line to add)",
    style: { color: stlColor(id) },
    onclick: () => {
      STL.palette = STL.palette.filter((x) => x !== id);
      STL.lines[STL.lines.length - 1].push(id);
      renderStatusline();
    },
  }, el("span", { text: a.label }));
  chip.ondragstart = (e) => {
    STL_DRAG = { src: "palette", id };
    e.dataTransfer.setData("text/plain", id);
    e.dataTransfer.effectAllowed = "move";
    chip.classList.add("dragging");
  };
  chip.ondragend = () => { STL_DRAG = null; chip.classList.remove("dragging"); };
  return chip;
}

function stlColorPanel() {
  const id = STL.sel;
  const a = stlAvail().get(id);
  const cur = STL.colors[id] || null;
  const box = el("div.stlcolors", {}, el("span.who", { text: "Colour for " + a.label }));
  for (const name of Object.keys(STL_COLORS)) {
    box.append(el("button.btn.swatch", {
      class: cur === name ? "on" : "", title: name, "aria-label": name,
      style: { background: STL_COLORS[name] },
      onclick: () => stlSetColor(id, name),
    }));
  }
  const custom = el("input", {
    type: "color", title: "Custom colour (truecolor terminals)",
    oninput: () => stlSetColor(id, custom.value),
  });
  if (cur && cur.startsWith("#")) custom.value = cur;
  box.append(custom);
  box.append(mkbtn("btn-sm boldbtn" + (STL.bold[id] ? " on" : ""), "Bold", () => {
    if (STL.bold[id]) delete STL.bold[id];
    else STL.bold[id] = true;
    renderStatusline();
  }, "Toggle bold for this field"));
  const def = mkbtn("btn-sm", cur ? "Reset to " + a.color : "Default (" + a.color + ")",
    () => stlSetColor(id, null));
  def.disabled = !cur;
  box.append(def);
  box.append(mkbtn("btn-sm btn-ghost", "Close", () => { STL.sel = null; renderStatusline(); }));
  return box;
}

function renderStatusline() {
  const view = document.getElementById("stlview");
  const st = DATA.statusline || {};
  if (!STL) stlInit();
  view.innerHTML = "";

  view.append(el("div.view-head", {
    html: "Generates <b>" + esc(st.script_path || "") + "</b>, linked into the config dir as "
      + "<b>~/.claude/statusline.sh</b> and referenced from settings.json "
      + (st.applied
        ? '<span class="ok">✓ statusLine is set</span>'
        : '<span class="warn">— not set in settings.json yet; use Save &amp; apply</span>')
      + "<br>No restart needed: Claude Code re-runs the script after every assistant message "
      + "(and picks up settings.json edits on your next interaction); set a refresh interval "
      + "below to also update while the session sits idle.",
  }));

  if (st.script_exists)
    view.append(el("div.toolbar", {},
      openFileBtn(cfgPath("statusline.sh"), "Open statusline.sh", null,
        "Read the generated script — note that Save & apply overwrites it")));

  // ---- terminal preview
  const wsel = el("span.wsel");
  for (const w of [null, 120, 80]) {
    wsel.append(mkbtn("btn-sm btn-ghost" + (STL_WIDTH === w ? " on" : ""),
      w ? w + " col" : "Full",
      () => { STL_WIDTH = w; renderStatusline(); },
      w ? "Truncate the preview at ~" + w + " columns, like a narrow terminal" : "No truncation"));
  }
  const prev = el("div.terminal-body.stlpreview");
  if (STL_WIDTH) {
    prev.classList.add("trunc");
    prev.style.width = "calc(" + STL_WIDTH + "ch + 1.8rem)";
  }
  const raw = STL.separator;
  const sep = raw.trim()
    ? `<span class="psep">${esc(" " + raw.trim() + " ")}</span>` : esc(raw);
  const rendered = STL.lines
    .map((line) => line
      .map((id) => `<span style="color:${stlColor(id)}${STL.bold[id] ? ";font-weight:700" : ""}">`
                 + `${esc(stlAvail().get(id).sample)}</span>`)
      .join(sep))
    .filter((l) => l.length);
  prev.innerHTML = rendered.length
    ? rendered.join("<br>")
    : '<span class="muted">(no fields enabled — add some from the palette)</span>';

  view.append(el("div.terminal", { style: { marginBottom: "1rem" } },
    el("div.terminal-bar", {},
      el("span.dot.r"), el("span.dot.y"), el("span.dot.g"),
      el("span.terminal-title", { text: "preview" }),
      wsel),
    prev));

  // ---- separator + refresh + save
  const sepInput = el("input", {
    type: "text", value: STL.separator, style: { width: "5rem", flex: "none" },
    "aria-label": "Separator",
    oninput: () => { STL.separator = sepInput.value; },
    onchange: renderStatusline,
  });
  const refreshInput = el("input", {
    type: "number", min: 0, max: 3600, value: STL.refresh,
    style: { width: "5rem", flex: "none" }, "aria-label": "Refresh seconds",
    oninput: () => { STL.refresh = Math.max(0, parseInt(refreshInput.value) || 0); },
  });
  const bar = el("div.toolbar", {},
    el("span.muted", {
      style: { fontSize: ".78125rem" }, text: "Separator",
      title: "A visible separator automatically gets one space on each side; leave blank for space-only separation",
    }),
    sepInput);
  for (const ch of ["│", "·", "»"])
    bar.append(mkbtn("btn-sm", ch, () => { STL.separator = ch; renderStatusline(); }, "Separator preset"));
  bar.append(mkbtn("btn-sm", "Space", () => { STL.separator = "  "; renderStatusline(); },
    "Plain spaces, no separator character"));
  bar.append(el("span.separator.separator-v"));
  bar.append(el("span.muted", {
    style: { fontSize: ".78125rem" }, text: "Refresh every",
    title: "Claude Code re-runs the script after each assistant message; a refresh interval also "
         + "re-runs it every N seconds while idle. 0 = only on updates.",
  }), refreshInput, el("span.muted", { style: { fontSize: ".78125rem" }, text: "s" }));
  const saveBtn = mkbtn("btn-primary", "Save & apply", () => stlSave(true));
  saveBtn.prepend(icon("save"));
  bar.append(el("div.toolbar-end", {}, mkbtn("", "Save only", () => stlSave(false)), saveBtn));
  view.append(bar);

  // ---- builder
  const build = el("div.stlbuild");
  const presets = el("div.stlpresets", {}, el("span", { text: "Presets" }));
  for (const name of Object.keys(STL_PRESETS))
    presets.append(mkbtn("btn-sm", name, () => stlPreset(name),
      "Replace the current layout with the " + name + " preset (colours and bold are kept)"));
  build.append(presets);

  if (STL.sel && STL.lines.some((l) => l.includes(STL.sel))) build.append(stlColorPanel());
  else STL.sel = null;

  STL.lines.forEach((line, li) => {
    const lineEl = el("div.stlline", {});
    lineEl.ondragover = (e) => { e.preventDefault(); lineEl.classList.add("drag"); };
    lineEl.ondragleave = () => lineEl.classList.remove("drag");
    lineEl.ondrop = (e) => { e.preventDefault(); stlDrop(li, null); };
    lineEl.append(el("span.lno", { text: String(li + 1), title: "Line " + (li + 1) }));
    line.forEach((id, fi) => lineEl.append(stlChip(id, li, fi)));
    if (!line.length)
      lineEl.append(el("span.stl-placeholder", { text: "Drop fields here" }));
    if (STL.lines.length > 1) {
      const del = mkbtn("btn-sm btn-ghost ldel", "Remove line", () => {
        const dst = li > 0 ? li - 1 : 1;
        STL.lines[dst].push(...STL.lines[li]);
        STL.lines.splice(li, 1);
        renderStatusline();
      }, "Remove this line (its fields move to the line above)");
      lineEl.append(del);
    }
    build.append(lineEl);
  });

  const addLine = mkbtn("btn-sm", "Add line", () => { STL.lines.push([]); renderStatusline(); });
  addLine.prepend(icon("plus"));
  addLine.disabled = STL.lines.length >= STL_MAX_LINES;
  addLine.title = addLine.disabled
    ? "Max " + STL_MAX_LINES + " lines (the config has three line breaks)"
    : "Add a line — narrow terminals truncate long lines instead of wrapping";
  build.append(addLine);

  // ---- field palette
  const side = el("div.stlside", {},
    el("h3", { text: "Available fields" }),
    el("div.hint", { text: "Click or drag onto a line · drag a chip back here to remove" }));
  side.ondragover = (e) => {
    if (STL_DRAG && STL_DRAG.src === "line") { e.preventDefault(); side.classList.add("drag"); }
  };
  side.ondragleave = () => side.classList.remove("drag");
  side.ondrop = (e) => {
    e.preventDefault();
    side.classList.remove("drag");
    const d = STL_DRAG;
    STL_DRAG = null;
    if (!d || d.src !== "line") return;
    stlRemove(d.id);
  };
  if (!STL.palette.length)
    side.append(el("div.muted", { style: { fontSize: ".75rem" }, text: "All fields are in use." }));
  const cats = new Map();
  for (const id of STL.palette) {
    const cat = (stlAvail().get(id) || {}).cat || "other";
    if (!cats.has(cat)) cats.set(cat, []);
    cats.get(cat).push(id);
  }
  for (const [cat, ids] of cats) {
    side.append(el("h4", { text: cat }));
    const chips = el("div.stlchips");
    for (const id of ids) chips.append(stlPalChip(id));
    side.append(chips);
  }

  view.append(el("div.stlgrid", {}, build, side));
}

async function stlSave(apply) {
  try {
    await api("/api/statusline-save", {
      config: { separator: STL.separator, refresh: STL.refresh, fields: stlFields() },
      apply });
    toast(apply
      ? "Statusline saved and statusLine set in settings.json"
      : "Statusline script regenerated — a running Claude Code picks it up on its next update");
    STL = null;
    await refresh();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ insight/costs

let INSIGHT = null;
let DOCTOR = null;

const BUDGET_COLORS = { "CLAUDE.md": "var(--chart-1)", skills: "var(--chart-2)",
  commands: "var(--chart-3)", agents: "var(--chart-4)", "output-styles": "var(--chart-5)",
  plugins: "var(--chart-1)", memory: "var(--chart-3)" };
const USAGE_KIND = { skills: "skill", commands: "command", agents: "agent" };

function tokfmt(n) {
  if (n < 1000) return String(n);
  let s = (n / 1000).toFixed(1);
  if (s.endsWith(".0")) s = s.slice(0, -2);
  return s + "k";
}

function relTime(iso) {
  if (!iso) return "never";
  const d = Date.now() - Date.parse(iso);
  if (!isFinite(d)) return iso;
  const day = 86400000;
  if (d < 3600000) return Math.max(1, Math.round(d / 60000)) + "m ago";
  if (d < day) return Math.round(d / 3600000) + "h ago";
  if (d < 30 * day) return Math.round(d / day) + "d ago";
  return Math.round(d / (30 * day)) + "mo ago";
}

// a titled table wrapped in the shadcn table surface
function dataTable(headers, rows) {
  const t = el("table.table");
  t.append(el("thead", {}, el("tr", {}, ...headers.map((h) => el("th", { text: h })))));
  const body = el("tbody");
  for (const r of rows) {
    const tr = el("tr");
    tr.innerHTML = r;
    body.append(tr);
  }
  t.append(body);
  return el("div.table-wrap", { style: { marginBottom: "1.25rem" } }, t);
}

async function renderInsight(rescan) {
  const view = document.getElementById("insightview");
  if (!await cached({
        view: "insightview", url: "/api/insight" + (rescan ? "?rescan" : ""),
        reload: rescan, skeleton: 5, get: () => INSIGHT,
        set: (v) => { INSIGHT = v; }, alive: () => TAB === "insight",
        note: "Estimating context cost and scanning session transcripts…" })) return;
  const u = INSIGHT.usage;
  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "What actually gets used: skills, commands, agents and Bash prefixes counted "
      + "from the session transcripts in <b>" + esc(u.dir) + "</b>, parsed locally. "
      + "What all of it costs in context lives in the Context tab.",
  }));

  // what the transcripts never mention, from the same inventory the item tabs show
  const used = u.by || {};
  const now = Date.now();
  const unused = [];
  for (const [t, kind] of Object.entries(USAGE_KIND)) {
    for (const s of ((DATA.items || {})[t] || [])) {
      if (!s.enabled || s.broken) continue;
      const rec = (used[kind] || {})[s.name];
      const last = rec && rec.last ? Date.parse(rec.last) : 0;
      if (!last || now - last > 90 * 86400000)
        unused.push({ type: t, name: s.name, last });
    }
  }

  if (u.available) {
    const stats = el("div.stat-grid", { style: { marginBottom: "1.25rem" } },
      statCard(String(u.sessions), "sessions scanned", { accent: true }));
    if (u.sessions) stats.append(statCard(String(unused.length), "unused 90d+"));
    view.append(stats);
  }

  if (u.available && u.sessions) {
    const rows = [];
    for (const [kind, names] of Object.entries(used))
      for (const [name, rec] of Object.entries(names))
        rows.push({ kind, name, count: rec.count, last: rec.last });
    rows.sort((a, b2) => b2.count - a.count);
    view.append(sectionTitle("Most used", rows.length));
    view.append(dataTable(["Name", "Kind", "Uses", "Last used"],
      rows.slice(0, 15).map((r) =>
        `<td class="mono">${esc(r.name)}</td><td class="dim">${esc(r.kind)}</td>`
        + `<td class="num">${r.count}</td><td class="dim">${esc(relTime(r.last))}</td>`)));
    if (unused.length) {
      view.append(sectionTitle("Unused in 90+ days — archive candidates", unused.length));
      view.append(dataTable(["Name", "Type", "Last used"],
        unused.slice(0, 30).map((r) =>
          `<td class="mono">${esc(r.name)}</td><td class="dim">${esc(r.type)}</td>`
          + `<td class="dim">${r.last ? esc(relTime(new Date(r.last).toISOString())) : "never"}</td>`)));
    }
  } else if (!u.available) {
    view.append(emptyState("No transcripts found",
      "Usage analytics appear once Claude Code has recorded sessions on this machine. Looked in "
      + u.dir + ".", "chart"));
  }

  // permission advisor: Bash prefixes approved often -> propose allow rules
  if (u.available && u.sessions && u.bash) {
    const allow = (INSIGHT.allow || []).filter((r) => typeof r === "string");
    const covered = (prefix) => allow.some((r) => {
      if (!r.startsWith("Bash(")) return false;
      const inner = r.slice(5, -1).replace(/:?\*$/, "").trim();
      return inner && (prefix === inner || prefix.startsWith(inner + " "));
    });
    const cand = Object.entries(u.bash)
      .filter(([p, n]) => n >= 5 && !covered(p))
      .sort((a, b2) => b2[1] - a[1])
      .slice(0, 15);
    if (cand.length) {
      view.append(sectionTitle("Permission advisor", cand.length));
      view.append(el("div.view-head", {
        text: "These Bash commands ran repeatedly across your sessions with no allow rule. "
            + "Adding one skips the permission prompt for them.",
      }));
      const t = el("table.table");
      t.append(el("thead", {}, el("tr", {},
        el("th", { text: "Command" }), el("th", { text: "Uses" }),
        el("th", { text: "Proposed rule" }), el("th"))));
      const body = el("tbody");
      for (const [prefix, n] of cand) {
        const rule = "Bash(" + prefix + ":*)";
        const btn = mkbtn("btn-sm btn-primary", "Allow", async () => {
          try {
            await api("/api/settings-set", { key: "permissions.allow", value: [...allow, rule] });
            toast(rule + " added to permissions.allow");
            await refresh();
            INSIGHT = null;
            renderInsight();
          } catch (e) { toast(e.message, true); }
        });
        body.append(el("tr", {},
          el("td.mono", { text: prefix }),
          el("td.num", { text: String(n) }),
          el("td.dim.mono", { text: rule }),
          el("td", { style: { textAlign: "right" } }, btn)));
      }
      t.append(body);
      view.append(el("div.table-wrap", { style: { marginBottom: "1.25rem" } }, t));
    }
  }

  const rb = mkbtn("btn-sm", "Rescan transcripts", () => renderInsight(true),
    "Drop the cache and re-read every transcript"
    + (u.scanned_now ? " (last run read " + u.scanned_now + " new file(s))" : ""));
  rb.prepend(icon("refresh"));
  view.append(el("div.toolbar", {}, rb));
}

// ----------------------------------------------------------------- context

let CONTEXT = null;

function median(nums) {
  if (!nums.length) return 0;
  const s = [...nums].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : Math.round((s[mid - 1] + s[mid]) / 2);
}

/* One scope's stacked bar: CLAUDE.md (+imports), each item listing, memory. */
function ctxBudgetBar(sc) {
  const segs = [["CLAUDE.md", sc.claude_md.reduce((n, m) => n + m.total_tok, 0)],
    ...Object.entries(sc.types).map(([t, v]) => [t, v.listing_tok])];
  if (sc.memory) segs.push(["memory", sc.memory.memory_tok]);
  const bar = el("div.budgetbar");
  const key = el("div.budgetkey");
  for (const [name, tok] of segs) {
    if (!tok) continue;
    const color = BUDGET_COLORS[name] || "var(--muted-foreground)";
    bar.append(el("div", { style: { flex: String(tok), background: color },
      title: name + ": ~" + tokfmt(tok) + " tokens" }));
    key.append(el("span", {},
      el("span.sw", { style: { background: color } }),
      el("span", { text: name + " ~" + tokfmt(tok) })));
  }
  return bar.childElementCount ? [bar, key] : [];
}

function ctxScopeCard(sc) {
  const card = el("div.card", { style: { marginBottom: "1.25rem" } });
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", {
        text: (sc.scope === "user" ? "Every session — " : "Project — ") + sc.tilde }),
      el("div.card-description", {
        text: "~" + tokfmt(sc.est_tok) + " tokens loaded at session start"
          + (sc.scope === "user" ? " in every project"
             : ", on top of the user scope") }))));
  const body = el("div.card-content");
  add(body, ctxBudgetBar(sc));
  const rows = el("div.list", { style: { marginTop: ".5rem" } });

  for (const md of sc.claude_md) {
    if (!md.exists && sc.scope !== "user") continue;
    const chips = md.imports.map((imp) => badge(
      imp.ref + (imp.resolved ? " ~" + tokfmt(imp.tok) : " missing"),
      imp.resolved ? "outline" : "destructive"));
    rows.append(el("div.drow", {},
      icon("file"),
      el("span.dmsg.dmono", { text: md.tilde }),
      md.exists ? badge("~" + tokfmt(md.total_tok), "secondary")
                : badge("not present", "outline"),
      ...chips,
      el("div.dactions", {},
        md.exists ? openFileBtn(md.path, "Edit") : null)));
  }

  if (sc.memory) {
    const mem = sc.memory;
    rows.append(el("div.drow", {},
      icon("book"),
      el("span.dmsg", { text: "auto-memory MEMORY.md — plus "
        + plural(mem.topics.length, "topic file") + " ("
        + tokfmt(mem.topics_chars) + " chars) loaded on demand" }),
      badge("~" + tokfmt(mem.memory_tok), "secondary"),
      el("div.dactions", {}, openFileBtn(mem.dir + "/MEMORY.md", "Edit"))));
  }

  if (sc.mcp.count) {
    const enabled = sc.mcp.servers.filter((s) => s.enabled !== false
      && s.approval !== "rejected");
    rows.append(el("div.drow", {},
      icon("server"),
      el("span.dmsg", { text: plural(sc.mcp.count, "MCP server")
        + (enabled.length !== sc.mcp.count ? " (" + enabled.length + " active)" : "") }),
      el("div.dactions", {}, infoTrigger("MCP context cost", () =>
        el("div", { style: { padding: ".75rem", maxWidth: "22rem" } },
          el("div", { style: { marginBottom: ".5rem" },
            text: "Each connected server injects its tool schemas at session "
              + "start. Their size can't be measured without connecting, so "
              + "no token number is shown — but every enabled server adds "
              + "real context in every session that loads it." }),
          el("div.dim.mono", { style: { fontSize: ".75rem" },
            text: sc.mcp.servers.map((s) => s.name).join(", ") }))))));
  }
  if (rows.childElementCount) body.append(rows);

  const consumers = Object.entries(sc.types)
    .flatMap(([t, v]) => v.items.map((it) => ({ type: t, ...it })))
    .filter((it) => it.listing_tok > 0)
    .sort((a, b) => b.listing_tok - a.listing_tok)
    .slice(0, 8);
  if (consumers.length) {
    body.append(dataTable(["Item", "Type", "~listing", "File"],
      consumers.map((c) =>
        `<td class="mono">${esc(c.name)}</td><td class="dim">${esc(c.type)}</td>`
        + `<td class="num">${tokfmt(c.listing_tok)}</td>`
        + `<td class="num dim">${c.file_chars ? tokfmt(c.file_chars) + " chars" : ""}</td>`)));
  }
  card.append(body);
  return card;
}

function ctxMeasuredTable(m) {
  const t = el("table.table");
  t.append(el("thead", {}, el("tr", {},
    ...["Project", "Sessions", "Starts at", "Peak seen", "Cache reads", "Spend"]
      .map((h) => el("th", { text: h })))));
  const body = el("tbody");
  const shown = m.projects.slice(0, 20);
  for (const p of shown) {
    const sess = m.sessions[p.cwd] || [];
    const tr = el("tr", { style: sess.length ? { cursor: "pointer" } : {},
      title: sess.length ? "Click for recent sessions ("
        + tokfmt(p.base_min) + "–" + tokfmt(p.base_max) + " start range)" : "" });
    add(tr, [
      el("td.mono", {}, el("span", { text: p.tilde }),
        p.registered ? null : badge("unregistered", "outline")),
      el("td.num", { text: String(p.sessions)
        + (p.subagents ? " +" + p.subagents + " sub" : "") }),
      el("td.num", { text: "~" + tokfmt(p.base_med) }),
      el("td.num.dim", { text: tokfmt(p.peak_max) }),
      el("td.num.dim", { text: tokfmt(p.cache_read_tok) }),
      el("td.num", { text: usd(p.cache_spend) })]);
    if (!p.registered) tr.classList.add("dim");
    body.append(tr);
    if (!sess.length) continue;
    let open = [];
    tr.onclick = () => {
      if (open.length) { open.forEach((r) => r.remove()); open = []; return; }
      for (const s of sess) {
        const sr = el("tr.dim", {});
        add(sr, [
          el("td.mono", { text: "  " + s.id }),
          el("td.num", { text: plural(s.msgs, "msg") }),
          el("td.num", { text: "~" + tokfmt(s.baseline) }),
          el("td.num", { text: tokfmt(s.peak) }),
          el("td.dim", { text: s.model.replace(/^claude-/, "") }),
          el("td.dim", { text: relTime(s.last_ts) })]);
        open.push(sr);
      }
      tr.after(...open);
    };
  }
  t.append(body);
  const wrap = el("div.table-wrap", { style: { marginBottom: "1.25rem" } }, t);
  const out = [wrap];
  if (m.projects.length > shown.length)
    out.push(el("div.muted", { style: { marginBottom: "1rem", fontSize: ".8125rem" },
      text: "…and " + (m.projects.length - shown.length)
        + " more projects with smaller cache spend." }));
  return out;
}

async function renderContext(rescan) {
  const view = document.getElementById("contextview");
  // The transcript cache is shared server-side, so a rescan here refreshes
  // what those two tabs are holding. Dropped before the fetch rather than
  // after it: a rescan that fails or that you navigate away from has still
  // invalidated them, and the cost of being wrong is one refetch.
  if (rescan) { INSIGHT = null; COSTS = null; }
  if (!await cached({
        view: "contextview", url: "/api/context" + (rescan ? "?rescan" : ""),
        reload: rescan, skeleton: 5, get: () => CONTEXT,
        set: (v) => { CONTEXT = v; }, alive: () => TAB === "context",
        note: "Sizing your config and reading session transcripts…" })) return;
  const scopes = CONTEXT.scopes, m = CONTEXT.measured, user = scopes[0];
  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "Everything that loads into a session's context, and what your sessions "
      + "measurably started with. Left numbers are chars÷4 <i>estimates</i> of your "
      + "config; measured numbers come from the transcripts in <b>" + esc(m.dir)
      + "</b> — a session's first API call carries the whole fixed context, and "
      + "cache reads re-bill that context on every later call, which is why "
      + "trimming it cuts cost.",
  }));

  const typical = median(m.projects.filter((p) => p.sessions >= 3).map((p) => p.base_med));
  const spend = m.projects.reduce((n, p) => n + p.cache_spend, 0);
  const stats = el("div.stat-grid", { style: { marginBottom: "1.25rem" } },
    statCard(tokfmt(user.est_tok), "estimated: your config, every session"));
  if (m.available && typical)
    stats.prepend(statCard("~" + tokfmt(typical), "measured: typical session start",
      { accent: true, hint: "median across projects with 3+ sessions" }));
  if (m.available && m.sessions_total) {
    stats.append(statCard(usd(spend), "cache reads, all time"),
      statCard(String(m.sessions_total), "transcripts scanned"));
  }
  view.append(stats);

  view.append(sectionTitle("What loads into context", scopes.length));
  for (const sc of scopes) view.append(ctxScopeCard(sc));

  if (m.available && m.projects.length) {
    view.append(sectionTitle("Measured from transcripts", m.projects.length));
    add(view, ctxMeasuredTable(m));
  } else {
    view.append(emptyState("No transcripts found",
      "Measured context sizes appear once Claude Code has recorded sessions "
      + "on this machine. Looked in " + m.dir + ".", "layers"));
  }

  if (CONTEXT.pointers.length) {
    view.append(sectionTitle("Where to cut", CONTEXT.pointers.length));
    const list = el("div.list");
    for (const f of CONTEXT.pointers) {
      list.append(el("div.drow", {},
        f.level === "warn" ? badge("warn", "destructive") : badge("note", "outline"),
        badge(f.area, "secondary"),
        el("span.dmsg", { text: f.msg }),
        el("div.dactions", {}, findingAction(f))));
    }
    view.append(list);
  }

  const rb = mkbtn("btn-sm", "Rescan transcripts", () => renderContext(true),
    "Drop the shared transcript cache and re-read every session "
    + "(also refreshes Insight and Costs)");
  rb.prepend(icon("refresh"));
  view.append(el("div.toolbar", {}, rb));
}

let COSTS = null;

function usd(n) {
  if (n >= 100) return "$" + Math.round(n);
  if (n >= 1) return "$" + n.toFixed(2);
  return "$" + n.toFixed(3);
}

async function renderCosts(rescan) {
  const view = document.getElementById("costsview");
  if (!await cached({
        view: "costsview", url: "/api/costs" + (rescan ? "?rescan" : ""),
        reload: rescan, skeleton: 4, get: () => COSTS,
        set: (v) => { COSTS = v; }, alive: () => TAB === "costs",
        note: "Reading transcripts and pricing usage…" })) return;
  const c = COSTS;
  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "Estimated API-price cost of your Claude Code usage, computed locally from the transcripts "
      + "in <b>" + esc(c.dir) + "</b> (input/output/cache tokens × list prices for the day they were "
      + "used; cache writes at 2× base for the 1-hour TTL and 1.25× for the 5-minute one, cache reads "
      + "at 0.1×; fast-mode requests at 2× and US-pinned inference at 1.1× on top of those, plus $10 "
      + "per 1,000 web searches). Days are your local days. On a Pro/Max subscription this shows what "
      + "the same usage <i>would</i> cost via the API.",
  }));

  if (!c.available || !c.sessions) {
    view.append(emptyState("No transcripts found",
      "Cost data appears once Claude Code has recorded sessions on this machine.", "dollar"));
    return;
  }

  // Transcripts exist but nothing priced: a grid of $0.000 is the symptom, not
  // the story. Say what was measured — where the usage was lost decides which
  // fix applies, so state it rather than guessing at a single cause.
  if (!c.totals.all) {
    let why;
    const zeroed = c.zeroed_models || [];
    const byOv = zeroed.filter((z) => z.override);
    if (byOv.length) {
      const keys = [...new Set(byOv.map((z) => z.override))];
      why = "Every model was priced at $0 by " + (keys.length === 1
        ? "a <code>pricing</code> override in <b>.claude-ui.json</b>: <b>"
        : "<code>pricing</code> overrides in <b>.claude-ui.json</b>: <b>")
        + keys.map(esc).join(", ") + "</b>. Override keys match as substrings, so a "
        + "short one zeroes every model id containing it — "
        + esc(byOv[0].override) + " is being applied to <b>" + esc(byOv[0].model)
        + "</b>. If it was meant only for a local model, make the key that model's "
        + "full id, or remove it.";
    } else if ((c.excluded_models || []).length) {
      why = "Dropped model ids: <b>" + c.excluded_models.map(esc).join(", ") + "</b> ("
        + c.dropped_msgs + " message" + (c.dropped_msgs === 1 ? "" : "s")
        + "). If these are Claude models behind a gateway or proxy alias, price them "
        + "with the <code>pricing</code> key in <b>.claude-ui.json</b>, e.g. "
        + "<code>{\"pricing\": {\"" + esc(c.excluded_models[0])
        + "\": [3, 15]}}</code> — input and output dollars per million tokens.";
    } else if (c.nomodel) {
      why = c.nomodel + " message" + (c.nomodel === 1 ? "" : "s")
        + " carried token usage but no <code>message.model</code> id, so there is nothing "
        + "to price them against. That usually means a proxy or gateway is stripping the "
        + "model from the response before Claude Code records it.";
    } else if (!c.usage_msgs) {
      why = "No message in those transcripts carried a <code>usage</code> block at all, so "
        + "there are no tokens to price.";
    } else {
      why = c.usage_msgs + " usage messages were read and all priced to zero.";
    }
    view.append(el("div.alert.alert-warning", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("warn")),
      el("div.alert-body", {
        html: "Read " + c.sessions + " transcript" + (c.sessions === 1 ? "" : "s")
          + ", but nothing could be priced. " + why
          + " Run the diagnostics below for the full model-id census.",
      })));
    view.append(costsDiagToolbar());
    return;
  }

  view.append(el("div.stat-grid", { style: { marginBottom: "1.25rem" } },
    statCard(usd(c.totals.today), "today", { accent: true }),
    statCard(usd(c.totals.last7), "last 7 days"),
    statCard(usd(c.totals.last30), "last 30 days"),
    statCard(usd(c.totals.month), "month to date"),
    statCard(usd(c.totals.all), "all time"),
    statCard(usd(c.cache_savings), "saved by caching")));

  if (c.days.length) {
    const max = Math.max(...c.days.map((d) => d.cost), 0.0001);
    const chart = el("div.chart");
    for (const d of c.days) {
      chart.append(el("div.cbar", {
        style: { height: Math.max(2, (d.cost / max) * 100) + "%" },
        title: d.day + ": " + usd(d.cost) + "\n"
          + Object.entries(d.by).map(([m, v]) => m + ": " + usd(v)).join("\n"),
      }));
    }
    view.append(chart);
    view.append(el("div.chartkey", {},
      el("span", { text: c.days[0].day }),
      el("span", { text: "daily cost, last " + c.days.length + " active days" }),
      el("span", { text: c.days[c.days.length - 1].day })));
  }

  view.append(sectionTitle("By model", c.by_model.length));
  view.append(dataTable(["Model", "Cost", "Input", "Output", "Cache read", "Msgs"],
    c.by_model.map((m) =>
      `<td class="mono">${esc(m.model)}</td><td class="num">${usd(m.cost)}</td>`
      + `<td class="num dim">${tokfmt(m.in)}</td><td class="num dim">${tokfmt(m.out)}</td>`
      + `<td class="num dim">${tokfmt(m.cacheR)}</td><td class="num dim">${m.msgs}</td>`)));

  if (c.by_project.length > 1) {
    view.append(sectionTitle("By project", c.by_project.length));
    view.append(dataTable(["Project", "Cost", "Assistant msgs"],
      c.by_project.map((p) =>
        `<td class="mono">${esc(p.cwd.replace(/^\/(home|Users)\/[^/]+/, "~"))}</td>`
        + `<td class="num">${usd(p.cost)}</td><td class="num dim">${p.msgs}</td>`)));
  }

  // A Claude id priced at $0 by a broad override is spend going uncounted; the
  // local model it was meant for is not, so only warn about the family ids.
  const zeroClaude = (c.zeroed_models || []).filter(
    (z) => z.override && /claude|anthropic/i.test(z.model));
  if (zeroClaude.length) {
    view.append(el("div.alert.alert-warning", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("warn")),
      el("div.alert-body", { text: "Priced at $0 by a 'pricing' override: "
        + zeroClaude.map((z) => z.model + " (via \"" + z.override + "\")").join(", ")
        + " — that usage is missing from every total here. Override keys match as "
        + "substrings; use the model's full id to narrow one." })));
  }

  if ((c.excluded_models || []).length) {
    view.append(el("div.alert.alert-warning", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("warn")),
      el("div.alert-body", { text: "Not counted: " + c.excluded_models.join(", ")
        + " — " + c.dropped_msgs + " message" + (c.dropped_msgs === 1 ? "" : "s")
        + " dropped as non-Claude models; price them via 'pricing' in .claude-ui.json "
        + "to include them" })));
  }

  if (c.unknown_models.length) {
    view.append(el("div.alert.alert-warning", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("warn")),
      el("div.alert-body", { text: "No list price known for: " + c.unknown_models.join(", ")
        + " — priced at opus-tier; override via 'pricing' in .claude-ui.json" })));
  }

  const rb = mkbtn("btn-sm", "Rescan transcripts", () => renderCosts(true));
  rb.prepend(icon("refresh"));
  view.append(el("div.toolbar", {}, rb, costsDiagBtn()));
  view.append(el("div", { id: "costsdiag" }));
}

// Why this machine's numbers came out the way they did: every model id the
// pricer saw, its verdict, and the counters that say whether usage was lost at
// scan time or at pricing time. Built in rather than shipped as a script,
// because the machine that shows $0 is usually not the one you can log into.
function costsDiagBtn() {
  const b = mkbtn("btn-sm", "Diagnostics", () => renderCostsDiag());
  b.prepend(icon("search"));
  return b;
}

function costsDiagToolbar() {
  return el("div", {}, el("div.toolbar", {}, costsDiagBtn()),
    el("div", { id: "costsdiag" }));
}

async function renderCostsDiag() {
  const box = document.getElementById("costsdiag");
  if (!box) return;
  box.innerHTML = "";
  box.append(el("div.muted", { style: { fontSize: ".8125rem" },
    text: "Taking a census of the model ids in your transcripts…" }));
  let d;
  try { d = await api("/api/costs/diagnose"); }
  catch (e) {
    box.innerHTML = "";
    box.append(errorAlert(e.message));
    return;
  }
  box.innerHTML = "";
  box.append(sectionTitle("Cost diagnostics", d.models.length));

  const mb = (n) => (n < 1024 ? n + " B"
    : n < 1048576 ? (n / 1024).toFixed(1) + " KB"
      : (n / 1048576).toFixed(1) + " MB");
  const facts = [
    ["Transcripts directory", d.dir + (d.available ? "" : " — MISSING")],
    ["Transcripts", d.transcripts + (d.transcripts === 1 ? " file, " : " files, ")
      + mb(d.bytes)
      + (d.oversize ? " (" + d.oversize + " skipped, over " + mb(d.max_transcript)
        + " each)" : "")],
    ["Usage messages", String(d.usage_msgs)],
    ["Usage with no model id", d.nomodel + (d.nomodel ? " — cannot be priced" : "")],
    ["Active days", String(d.days)],
    ["Scan cache", d.cache.path + (d.cache.exists
      ? " (v" + d.cache.version + ", " + mb(d.cache.size) + ", " + d.cache.mtime + ")"
      : " — not written yet")],
  ];
  box.append(dataTable(["Check", "Value"], facts.map(([k, v]) =>
    `<td>${esc(k)}</td><td class="mono">${esc(v)}</td>`)));

  box.append(dataTable(["Model id seen", "Msgs", "Input", "Output", "Days", "Verdict"],
    d.models.length
      ? d.models.map((m) =>
        `<td class="mono">${esc(m.model)}</td><td class="num">${m.msgs}</td>`
        + `<td class="num dim">${tokfmt(m.in)}</td><td class="num dim">${tokfmt(m.out)}</td>`
        + `<td class="num dim">${m.days}</td>`
        + `<td>${esc(m.verdict.toUpperCase())} <span class="dim">— ${esc(m.note)}</span></td>`)
      : [`<td colspan="6" class="dim">No model ids recorded in any transcript.</td>`]));

  box.append(sectionTitle("Pricing overrides", d.overrides.length));
  box.append(dataTable(["Key (matched as a substring)", "Value", "Usable"],
    d.overrides.length
      ? d.overrides.map((o) =>
        `<td class="mono">${esc(o.key)}</td><td class="mono">${esc(JSON.stringify(o.value))}</td>`
        + `<td>${o.ok ? "yes" : "NO — needs [input, output] numbers"}</td>`)
      : [`<td colspan="3" class="dim">None set in .claude-ui.json.</td>`]));

  const txt = ["claude-ui cost diagnostics", ...facts.map(([k, v]) => k + ": " + v),
    "", "model ids seen:",
    ...(d.models.length ? d.models.map((m) =>
      `  ${m.msgs} msgs  ${m.model}  ${m.verdict.toUpperCase()} (${m.note})`)
      : ["  (none)"]),
    "", "pricing overrides:",
    ...(d.overrides.length ? d.overrides.map((o) =>
      `  ${o.key} = ${JSON.stringify(o.value)} ${o.ok ? "ok" : "UNUSABLE"}`)
      : ["  (none)"])].join("\n");
  const cb = mkbtn("btn-sm", "Copy report", () => copyText(txt, "diagnostics"));
  cb.prepend(icon("copy"));
  box.append(el("div.toolbar", {}, cb));
}

// ------------------------------------------------------------------ doctor

let DFILTER = "all";

async function renderDoctor(rerun) {
  const view = document.getElementById("doctorview");
  const cold = !DOCTOR || rerun;
  if (!await cached({ view: "doctorview", url: "/api/doctor", reload: rerun,
                      skeleton: 4, get: () => DOCTOR,
                      set: (v) => { DOCTOR = v; }, alive: () => TAB === "doctor",
                      note: "Running checks…" })) return;
  if (cold) renderTabs();   // the tab's own warning count comes from DOCTOR
  view.innerHTML = "";
  const warns = DOCTOR.warns;
  const infos = DOCTOR.findings.length - warns;

  view.append(el("div.stat-grid", { style: { marginBottom: "1rem" } },
    statCard(String(warns), "warnings", { accent: warns > 0 }),
    statCard(String(infos), "notes"),
    statCard(DOCTOR.ts, "last run")));

  const bar = el("div.toolbar");
  bar.append(segmented(
    [{ key: "all", label: "All" }, { key: "warn", label: "Warnings" },
     { key: "info", label: "Notes" }],
    DFILTER, (k) => { DFILTER = k; renderDoctor(); }));
  const rb = mkbtn("btn-sm", "Run again", () => renderDoctor(true));
  rb.prepend(icon("refresh"));
  bar.append(el("div.toolbar-end", {}, rb));
  view.append(bar);

  const findings = DOCTOR.findings.filter((f) => DFILTER === "all" || f.level === DFILTER);
  if (!findings.length) {
    view.append(emptyState(
      DOCTOR.findings.length ? "Nothing in this filter" : "Nothing to report",
      DOCTOR.findings.length ? "Switch the filter to see the other findings."
        : "No broken symlinks, missing executables, stale backups or item problems were found.",
      "success"));
    return;
  }

  const list = el("div.list");
  for (const f of findings) {
    list.append(el("div.drow", {},
      f.level === "warn" ? badge("warn", "destructive") : badge("note", "outline"),
      badge(f.area, "secondary"),
      el("span.dmsg", { text: f.msg }),
      el("div.dactions", {}, findingAction(f))));
  }
  view.append(list);
}

/* A finding is only worth reading if you can act on it. `target` says where
   the problem lives (doctor.py fills it in); without one we fall back to the
   path embedded in the message, so at least the path is one click away. */
function findingAction(f) {
  const t = f.target;
  if (t && (t.kind === "item" || t.kind === "path")) {
    const b = mkbtn("btn-sm", "Open",
      () => openTarget(t),
      "Open " + (t.kind === "path" ? t.path : t.name) + " in the editor"
        + (t.line ? " at line " + t.line : ""));
    b.prepend(icon("pencil"));
    return b;
  }
  if (t && t.kind === "tab") {
    const b = mkbtn("btn-sm btn-ghost", "Show", () => openTarget(t),
      "Show this in the " + t.tab + " tab");
    b.prepend(icon("chevronRight"));
    return b;
  }
  const m = f.msg.match(/(^|\s)(~?\/[^\s,:]+)/);   // e.g. a broken symlink
  if (m) {
    const b = mkbtn("btn-sm btn-ghost", "Copy path", () => copyText(m[2], "path"));
    b.prepend(icon("copy"));
    return b;
  }
  return null;
}

// --------------------------------------------------------- command palette

let PAL = null;

function palItems() {
  const out = [];
  for (const t of TABS)
    out.push({ kind: "go to", label: TAB_META[t].label, icon: TAB_META[t].icon,
      run: () => goTab(t) });
  // the two segments the tab bar cannot reach directly — the palette was how
  // you got to Plugins and Discover by name, and it still is
  for (const s of SKILL_SEGS.slice(1))
    out.push({ kind: "go to", label: "Skills · " + s.label,
      icon: s.key === "plugins" ? "plug" : "search", run: () => goSeg(s.key) });
  for (const t of ITEM_TABS)
    for (const s of (DATA.items || {})[t] || [])
      out.push({ kind: t.replace(/s$/, ""), label: s.name, icon: TAB_META[t].icon,
        hint: (s.enabled ? "" : "(disabled) ") + (s.description || ""),
        // a broken item has no editor to open, so land on its tab with the
        // filter set to it instead
        run: () => s.broken
          ? (() => { goTab(t); IQ = s.name; render(); })()
          : openItemEditor(t, s.name, null, s.enabled) });
  for (const id of ["CLAUDE.md", "settings.json", "keybindings.json"])
    out.push({ kind: "edit", label: id, icon: "file", run: () => openEditor(id) });
  out.push({ kind: "action", label: "Add MCP server", icon: "plus",
    run: () => { goTab("mcp"); mcpNew(); } });
  for (const t of THEMES)
    out.push({ kind: "theme", label: t.label, icon: "droplet", hint: t.hint,
      run: () => setTheme({ family: t.id }) });
  for (const m of MODES)
    out.push({ kind: "appearance", label: m.label, icon: m.icon, run: () => setTheme({ mode: m.id }) });
  out.push({ kind: "action", label: "Run doctor", icon: "pulse",
    run: () => { goTab("doctor"); renderDoctor(true); } });
  out.push({ kind: "action", label: "Setup pieces", icon: "wrench",
    run: () => { goTab("setup"); renderSetup(true); } });
  out.push({ kind: "action", label: "Rescan usage analytics", icon: "chart",
    run: () => { goTab("insight"); renderInsight(true); } });
  out.push({ kind: "action", label: "Rescan costs", icon: "dollar",
    run: () => { goTab("costs"); renderCosts(true); } });
  out.push({ kind: "action", label: "Copy config directory path", icon: "copy",
    run: () => copyText(DATA.config_dir, "config directory path") });
  return out;
}

function palMatches() {
  const q = PAL.q.trim().toLowerCase();
  if (!q) return PAL.items.slice(0, 12);
  return PAL.items
    .map((it) => ({ it, s: fuzzy(q, it.kind + " " + it.label) }))
    .filter((x) => x.s >= 0)
    .sort((a, b) => b.s - a.s)
    .slice(0, 12)
    .map((x) => x.it);
}

function closePalette() {
  PAL = null;
  const p = document.getElementById("palette");
  p.hidden = true;
  p.className = "";
  p.innerHTML = "";
}

function openPalette() {
  PAL = { q: "", sel: 0, items: palItems(), catalogHits: [] };
  const p = document.getElementById("palette");
  p.hidden = false;
  p.className = "dialog-overlay palette-overlay";
  p.innerHTML = "";

  const inp = el("input.command-input", {
    placeholder: "Jump to anything — items, tabs, themes, actions…",
    "aria-label": "Command palette", spellcheck: false,
  });
  const listEl = el("div.command-list", { role: "listbox" });

  // The catalog group is appended after the local matches, never merged into
  // their fuzzy ranking — it is its own labeled group, per palMatches() below.
  // Every row's run() navigates to Discover with the *typed query* pre-filled;
  // it never opens or installs the specific hit. A command palette is too easy
  // to fire by Enter-muscle-memory to let it clone a repo or run an install.
  const palRows = () => {
    const q = PAL.q.trim();
    const rows = palMatches();
    if (q.length >= 2 && PAL.catalogHits.length)
      for (const h of PAL.catalogHits)
        rows.push({ kind: "discover catalog", label: h.entry.name,
          icon: DISCOVER_ICON[h.entry.kind] || "search",
          hint: h.entry.description || "", run: () => goDiscoverSearch(q) });
    return rows;
  };

  let catTimer = null;
  const catalogSearch = (q) => {
    clearTimeout(catTimer);
    if (q.length < 2) { PAL.catalogHits = []; renderList(); return; }
    catTimer = setTimeout(async () => {
      let hits = [];
      try { hits = (await api("/api/search?q=" + encodeURIComponent(q) + "&limit=8")).hits || []; }
      catch (e) { hits = []; }
      if (!PAL || PAL.q.trim() !== q) return;   // stale answer, or palette closed
      PAL.catalogHits = hits;
      renderList();
    }, 150);
  };

  const renderList = () => {
    const rows = palRows();
    listEl.innerHTML = "";
    if (!rows.length) {
      listEl.append(el("div.command-empty", { text: "No results." }));
      return;
    }
    let lastKind = null;
    rows.forEach((it, i) => {
      if (it.kind !== lastKind) {
        listEl.append(el("div.command-group-label", { text: it.kind }));
        lastKind = it.kind;
      }
      const row = el("div.command-item", {
        role: "option", "aria-selected": String(i === PAL.sel),
        onclick: () => { closePalette(); it.run(); },
      },
        it.icon ? icon(it.icon) : null,
        el("span.ci-label", { text: it.label }),
        it.hint ? el("span.ci-hint", { text: it.hint }) : null);
      listEl.append(row);
      if (i === PAL.sel) setTimeout(() => row.scrollIntoView({ block: "nearest" }));
    });
  };

  inp.oninput = () => {
    PAL.q = inp.value;
    PAL.sel = 0;
    renderList();
    catalogSearch(inp.value.trim());
  };
  inp.onkeydown = (e) => {
    const rows = palRows();
    if (e.key === "ArrowDown") {
      e.preventDefault();
      PAL.sel = Math.min(PAL.sel + 1, rows.length - 1);
      renderList();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      PAL.sel = Math.max(PAL.sel - 1, 0);
      renderList();
    } else if (e.key === "Enter") {
      const it = rows[PAL.sel];
      if (it) { closePalette(); it.run(); }
    } else if (e.key === "Escape") {
      closePalette();
    }
  };

  p.onclick = (e) => { if (e.target === p) closePalette(); };
  p.append(el("div.command", {},
    el("div.command-input-wrap", {}, el("span.command-icon", {}, icon("search")), inp),
    listEl,
    el("div.command-footer", {},
      el("span", {}, el("kbd", { text: "↑↓" }), " navigate"),
      el("span", {}, el("kbd", { text: "↵" }), " open"),
      el("span", {}, el("kbd", { text: "esc" }), " close"))));
  renderList();
  inp.focus();
}

// The editor lives in editor.js (loaded before this file).

// --------------------------------------------------------------- inventory

/* ctx is how a row says which config it belongs to: absent, or `{root, tilde,
   reload}` for one of a project's own. `root` rides into every call and
   `reload` is what redraws afterwards, because the Projects tab holds its
   payload in PROJECTS and would not see a refresh() of /api/state. */
const ctxRoot = (ctx) => (ctx && ctx.root) || undefined;
const ctxReload = (ctx) => (ctx && ctx.reload ? ctx.reload() : refresh());

async function toggleItem(type, name, enabled, ctx) {
  await act("item-toggle", { type, name, enabled, root: ctxRoot(ctx) },
    name + (enabled ? " enabled" : " disabled — moved to disabled/")
      + " · applies to new sessions",
    { undo: { label: "Undo", fn: () => toggleItem(type, name, !enabled, ctx) },
      then: () => ctxReload(ctx) });
}

/* The one action here that takes something away for good.

   Everything else in this app is reversible: disabling parks a file in
   disabled/, editing keeps the bytes it replaced in a diff you were shown, a
   backup can be restored. This cannot, so it asks for the name to be typed and
   the toast carries no Undo — there is nothing held to put back. */
async function deleteItem(type, s, enabled, ctx) {
  // no file count in the wording: the only list the browser has is the editor's
  // (capped, dotfiles excluded), and a number that understates what is about to
  // go is worse than not giving one
  const what = s.symlink
    ? "Removes the link at " + (s.path || s.name) + ". Whatever it points at is "
      + "left exactly as it is."
    : "Permanently removes " + (s.path || s.name)
      + (DIR_TYPES.has(type) ? " and everything inside it." : ".")
      + " This cannot be undone from here.";
  const ok = await mconfirmWord("Delete " + s.name + "?",
    what + (enabled ? "" : " It is already disabled, so nothing in your live config changes."),
    s.name, "Delete");
  if (!ok) return;
  await act("item-delete", { type, name: s.name, enabled, root: ctxRoot(ctx) },
    s.name + " deleted", { then: () => ctxReload(ctx) });
}

function itemBadges(s) {
  const out = [];
  if (s.symlink && !s.broken) out.push(badge("symlink", "outline"));
  if (s.broken) out.push(badge("broken", "destructive"));
  if (s.incomplete && !s.broken) out.push(badge("no SKILL.md", "destructive"));
  if (s.todo) {
    const b = badge("TODO", "warning");
    b.title = "Leftover TODO placeholder inside";
    out.push(b);
  }
  if (s.name_mismatch) {
    const b = badge("name ≠ dir", "warning");
    b.title = "The frontmatter name does not match the folder name";
    out.push(b);
  }
  if (s.long_desc) {
    const b = badge("long desc", "warning");
    b.title = "Description over 1024 characters — it may be truncated";
    out.push(b);
  }
  if (s.source) {
    // an item split out of a plugin is otherwise indistinguishable from one you
    // wrote, and that matters before you edit it or re-sync over it
    const b = badge("from plugin", "outline");
    b.title = "Split out of " + s.source + " — see the Plugins tab";
    out.push(b);
  }
  return out;
}

/* One row for one item, wherever it lives.

   The inventory tabs render your config dir's; a project card renders that
   project's own .claude/. Same badges, same buttons, same wording — a skill
   is a skill, and the only honest difference is which directory it applies
   from, which the card around it already says.

   `ctx` carries that difference: `{root, tilde, reload}` for a project's, and
   nothing at all for yours. It also carries the two things only the inventory
   knows — which output style is active, and how to set it — because at
   project scope those answers come from a different settings file. */
function itemRow(type, s, enabled, ctx) {
  ctx = ctx || {};
  const active = ctx.activeStyle;
  const isActive = active != null && enabled && !s.broken
    && styleSettingName(s) === active;
  const actions = el("div.li-actions");

  // An optional expandable panel under the row, supplied by the view via a
  // ctx flag — the same seam ctx.extraBadges and ctx.extraMenu use. Only the
  // Skills tab sets it today.
  const detail = ctx.detail ? el("div.detail-panel", { hidden: true }) : null;
  if (detail) {
    const open = mkbtn("btn-sm btn-icon btn-ghost pd-toggle", "",
      () => toggleItemDetail(ctx, s, enabled, open, detail),
      "The settings this skill reads");
    open.append(icon("chevronDown"));
    actions.append(open);
  }

  if (active != null && enabled && !s.broken && !isActive) {
    const set = ctx.setActive
      || ((name) => commitSetting("outputStyle", name));
    actions.append(mkbtn("btn-sm", "Set active",
      () => set(styleSettingName(s)),
      "Write outputStyle: " + styleSettingName(s) + " to "
      + (ctx.root ? ".claude/settings.local.json" : "settings.json")
      + " — applies to new sessions"));
  }
  if (!s.broken) {
    const eb = mkbtn("btn-sm", "Edit",
      () => openItemEditor(type, s.name, null, enabled, null, ctxRoot(ctx)));
    eb.prepend(icon("pencil"));
    actions.append(eb);
  }
  actions.append(mkbtn("btn-sm" + (enabled ? " danger" : ""),
    enabled ? "Disable" : "Enable", () => toggleItem(type, s.name, !enabled, ctx)));
  const more = mkbtn("btn-sm btn-icon btn-ghost", "", (e) => openMenu(e.currentTarget, [
    { label: "Copy path", icon: "copy", fn: () => copyText(s.path || s.name, "path") },
    // Broken items have no Edit button (item_read can't follow the dangling
    // link) but the file itself is still openable by path.
    ...(s.broken && s.path
      ? [{ label: "Open by path", icon: "file", fn: () => openPath(s.path) }] : []),
    ...(ctx.copyTo || []),
    ...(ctx.extraMenu ? ctx.extraMenu(s) : []),
    { label: enabled ? "Disable" : "Enable", icon: "power",
      fn: () => toggleItem(type, s.name, !enabled, ctx), danger: enabled },
    { label: "Delete…", icon: "trash", danger: true,
      fn: () => deleteItem(type, s, enabled, ctx) },
  ]), "More actions");
  more.append(icon("chevronDown"));
  actions.append(more);

  let activeBadge = null;
  if (isActive) {
    activeBadge = badge("active", "default");
    activeBadge.title = "outputStyle selects this style";
  }
  return el("div.list-item", { class: enabled ? "" : "off" },
    el("div.li-main", {},
      el("span.li-name", { title: s.path || "", text: s.name }),
      activeBadge,
      ...itemBadges(s),
      ...(ctx.extraBadges ? ctx.extraBadges(s) : [])),
    el("span.li-desc", { text: s.description || "" }),
    actions, detail);
}

const toggleItemDetail = (ctx, s, enabled, btn, body) => toggleDetail(btn, body, {
  busy: "Reading the skill…",
  load: () => api("/api/skill-detail?name=" + encodeURIComponent(s.name)
    + "&enabled=" + (enabled ? "1" : "0")),
  build: (d) => envPanel(d.env || [], body, "skill",
    async () => { await refresh(); render(); }),
});

/* The three remaining plain inventories. `type` is a parameter rather than a
   read of TAB because skills is no longer one of these: it has a tab of its
   own with segments, and a function that decides what to draw by looking at a
   global cannot be reused by it. */
function renderInventory(type) {
  const view = document.getElementById("itemsview");
  const all = (DATA.items || {})[type] || [];
  const q = IQ.toLowerCase();
  const items = all.filter((s) =>
    !q || s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q));
  const on = items.filter((s) => s.enabled);
  const off = items.filter((s) => !s.enabled);

  view.innerHTML = "";
  if (type === "output-styles" && NEWSTYLE) { renderStyleForm(view); return; }
  view.append(el("div.view-head", {
    html: type + " in <b>" + esc(DATA.config_dir) + "/" + type + "</b> — everything real on this machine. "
      + "Disabling moves an item to <b>disabled/" + type + "/</b>; nothing is deleted. "
      + "Changes apply to new sessions.",
  }));

  const inp = el("input", {
    type: "search", id: "iq", placeholder: "Filter " + type + " by name or description…",
    value: IQ,
    oninput: (e) => {
      IQ = inp.value;
      if (e.isComposing) return;
      refilter("iq", () => renderInventory(type));
    },
  });
  // Output styles are the one type you are more likely to want than to have,
  // and the only one this app can compose from scratch — see output-styles.js.
  if (type === "output-styles") view.append(styleDocsCard());

  const end = el("div.toolbar-end", {},
    el("span.hint", {
      text: on.length + " enabled · " + off.length + " disabled"
        + (items.length !== all.length ? " · " + items.length + " of " + all.length + " shown" : "")
        + (type === "output-styles"
           ? " · active: " + (settingsGet("outputStyle") || "default") : "") }));
  if (type === "output-styles") {
    const nb = mkbtn("btn-sm btn-primary", "New output style", openNewStyle);
    nb.prepend(icon("plus"));
    end.append(nb);
  }
  view.append(el("div.toolbar", {}, inp, end));

  if (!all.length) {
    view.append(emptyState("No " + type + " yet",
      "Anything you put in " + DATA.config_dir + "/" + type + " shows up here.",
      TAB_META[type].icon));
    return;
  }

  // Which style outputStyle actually selects, so the list can say so. The
  // file being enabled only makes it selectable; this key is what applies it.
  const activeStyle = type === "output-styles"
    ? (settingsGet("outputStyle") || "default") : null;

  const section = (list, label, enabled) => {
    if (!list.length) return;
    view.append(sectionTitle(label, list.length));
    const box = el("div.list");
    for (const s of list) box.append(itemRow(type, s, enabled, { activeStyle }));
    view.append(box);
  };

  section(on, "Enabled", true);
  section(off, "Disabled", false);

  if (!items.length) {
    view.append(noMatches(IQ));
    // the bridge to Browse works unchanged for these three: goDiscoverSearch
    // routes through the segment now, and they never knew where it went
    view.append(catalogSearchLink(IQ));
  }
}

/* ------------------------------------------------------------------ skills --
   One tab, three segments. Installed is your skills folder and what installed
   plugins bring; Plugins is the plugin inventory those skills come from;
   Browse searches both plus the catalogs. They were three tabs that each
   answered part of "what skills do I have, and what else is there", and could
   not answer each other's part. */

function renderSkills() {
  const view = document.getElementById("skillsview");
  view.innerHTML = "";
  view.append(segmented(SKILL_SEGS, SEG, goSeg));
  // every segment renders into this, so a redraw of one (a rescan, a write)
  // never has to rebuild the bar above it
  view.append(el("div", { id: "skillseg" }));
  if (SEG === "plugins") { renderPlugins(); return; }
  if (SEG === "browse") { renderDiscover(); return; }
  renderInstalled();
}

function renderInstalled() {
  const view = document.getElementById("skillseg");
  const all = (DATA.items || {}).skills || [];
  const q = IQ.toLowerCase();
  const items = all.filter((s) =>
    !q || s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q));
  const on = items.filter((s) => s.enabled);
  const off = items.filter((s) => !s.enabled);

  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "Skills in <b>" + esc(DATA.config_dir) + "/skills</b> — everything real "
      + "on this machine. Disabling moves a skill to <b>disabled/skills/</b>; "
      + "nothing is deleted. Changes apply to new sessions.",
  }));

  const inp = el("input", {
    type: "search", id: "iq", placeholder: "Filter skills by name or description…",
    value: IQ,
    oninput: (e) => {
      IQ = inp.value;
      if (e.isComposing) return;
      refilter("iq", renderInstalled);
    },
  });
  view.append(el("div.toolbar", {}, inp,
    el("div.toolbar-end", {},
      el("span.hint", {
        text: on.length + " enabled · " + off.length + " disabled"
          + (items.length !== all.length
             ? " · " + items.length + " of " + all.length + " shown" : "") }))));

  if (!all.length) {
    view.append(emptyState("No skills yet",
      "Anything you put in " + DATA.config_dir + "/skills shows up here.",
      "sparkles"));
    return;
  }

  // skillOverrides is Claude Code's own switch and does something the file
  // move cannot: it hides a skill from the model while leaving it typable, and
  // it never touches the file. Read from your settings.json only — a project's
  // settings.local.json can turn a skill off too, and this view does not know
  // which project you are in.
  const ov = settingsGet("skillOverrides") || {};
  const ctx = {
    extraBadges: (s) => {
      if (!ov[s.name] || ov[s.name] === "on") return [];
      const b = badge(ov[s.name] === "off" ? "off for you" : ov[s.name], "outline");
      b.title = "skillOverrides in settings.json — the file is untouched";
      return [b];
    },
    extraMenu: (s) => [{
      label: ov[s.name] === "off" ? "Turn back on for me" : "Turn off for me",
      icon: "power",
      fn: () => skillOverride(s.name, ov[s.name] === "off" ? null : "off"),
    }],
    // the panel itself is built on expand; this only says the row has one
    detail: true,
  };

  const section = (list, label, enabled) => {
    if (!list.length) return;
    view.append(sectionTitle(label, list.length));
    const box = el("div.list");
    for (const s of list) box.append(itemRow("skills", s, enabled, ctx));
    view.append(box);
  };

  section(on, "Enabled", true);
  section(off, "Disabled", false);

  if (!items.length) {
    view.append(noMatches(IQ));
    view.append(catalogSearchLink(IQ));
  }
}

/* ------------------------------------------------------------------ backup --
   The reinstall story. Everything else in this app edits config in place; this
   is the one view whose output lives outside the config dir, because the whole
   point is to survive that directory being deleted.

   Restore is never one click: /api/backup-inspect answers new / same / differs
   for every file first, and only the rows you leave ticked are written. That
   report is an inline panel rather than a modal — a full backup is thousands
   of transcripts, and a diff you can expand does not belong in a dialog.

   A group is a coarse tick; inside it the server offers units — one skill, one
   config file, one MCP server, one project's transcripts. BUNITS holds the
   narrowing, and a group with no entry there means all of it. */

let BACKUP = null;      // /api/backup payload
let BPICKS = null;      // Set of ticked group ids; null until first render
let BUNITS = {};        // {groupId: Set(unitId)} — absent means every unit
let BREPORT = null;     // the open dry-run report, or null
let BSHOWSAME = false;  // identical files are collapsed by default
let BFRESH = null;      // snapshot name of an in-flight fresh start, or null
let BOPEN = new Set();  // restore units expanded to show their files

const fbytes = (n) => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + u[i];
};

const fwhen = (iso) => {
  const d = new Date(iso || "");
  return isNaN(d) ? (iso || "") : d.toLocaleString();
};

async function renderBackup(reload) {
  const view = document.getElementById("backupview");
  const cold = !BACKUP || reload;
  if (!await cached({ view: "backupview", url: "/api/backup", reload,
                      get: () => BACKUP, set: (v) => { BACKUP = v; },
                      alive: () => TAB === "backup" })) return;
  // the plan just changed under BUNITS, whose narrowings name units in it
  if (cold) bPruneUnits();
  if (!BPICKS) BPICKS = new Set(BACKUP.plan.filter((g) => g.files).map((g) => g.id));

  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "Copy the parts of your config that took work to build into a zip, so a "
      + "reinstall costs you nothing. Restoring puts them back <b>file by file</b>, "
      + "after showing you what would change. Transcripts are included because your "
      + "cost history is computed from them — without them the Costs tab starts at zero.",
  }));

  view.append(backupDestCard());
  if (BREPORT) { view.append(restorePanel()); return; }
  view.append(backupCreateCard());
  view.append(backupArchivesCard());
  view.append(freshStartCard());
}

function backupDestCard() {
  const card = el("div.card", { style: { marginBottom: "1.25rem" } });
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Where archives are kept" }),
      el("div.card-description", {
        text: "Outside the config directory on purpose — an uninstall that removes "
          + "~/.claude must not take your backups with it." }))));
  const actions = el("div.dactions", {},
    mkbtn("btn-sm", "Change…", changeBackupDir),
    mkbtn("btn-sm btn-ghost", "Copy path", () => copyText(BACKUP.dir, "path")));
  if (!BACKUP.default_dir)
    actions.append(mkbtn("btn-sm btn-ghost", "Reset", async () => {
      try {
        await api("/api/backup-dir", { path: "" });
        toast("Backup directory reset to the default");
        renderBackup(true);
      } catch (e) { toast(e.message, true); }
    }, "Back to the default location"));
  card.append(el("div.card-content.flush", {},
    el("div.drow", {}, icon("folder"),
      el("span.dmsg.dmono", { text: BACKUP.dir }),
      BACKUP.exists ? null : badge("not created yet", "outline"),
      BACKUP.default_dir ? null : badge("custom", "info"),
      actions)));
  return card;
}

async function changeBackupDir() {
  const r = await modal({
    title: "Backup directory",
    text: "Absolute path (or ~/…) where archives are written. Must be outside the "
        + "config directory. Stored machine-locally in .claude-ui.json.",
    fields: [{ id: "p", label: "Path", value: BACKUP.dir, mono: true }],
    ok: "Save",
  });
  if (!r) return;
  try {
    await api("/api/backup-dir", { path: r.p });
    toast("Backup directory updated");
    renderBackup(true);
  } catch (e) { toast(e.message, true); }
}

/* The units of a group that are actually ticked. No entry in BUNITS means all
   of them — the same rule the server applies to a group missing from `units`. */
function bUnits(g) {
  const picked = BUNITS[g.id];
  const all = g.units || [];
  return picked ? all.filter((u) => picked.has(u.id)) : all;
}

const bCustom = (g) => !!BUNITS[g.id] && bUnits(g).length !== (g.units || []).length;

const bTotals = (g) => bUnits(g).reduce(
  (t, u) => ({ files: t.files + u.files, bytes: t.bytes + u.bytes }),
  { files: 0, bytes: 0 });

/* A narrowing must not outlive the units it names: a skill deleted since you
   ticked it would otherwise leave a subset that silently excludes new ones. */
function bPruneUnits() {
  for (const g of BACKUP.plan) {
    if (!BUNITS[g.id]) continue;
    const live = new Set((g.units || []).map((u) => u.id));
    const kept = [...BUNITS[g.id]].filter((id) => live.has(id));
    if (kept.length === live.size) delete BUNITS[g.id];
    else BUNITS[g.id] = new Set(kept);
  }
}

/* Items are the one group big enough to want sections, and their unit ids are
   "<type>/<name>" — so the type is already in hand. Everything else is one
   list under the group's own name. */
function bUnitGroups(g) {
  const row = (u) => ({
    value: u.id, name: u.label,
    desc: [u.desc, plural(u.files, "file"), fbytes(u.bytes)]
      .filter(Boolean).join(" · "),
    checked: !BUNITS[g.id] || BUNITS[g.id].has(u.id),
  });
  if (g.id !== "items") return [{ label: g.label, rows: (g.units || []).map(row) }];
  const byType = new Map();
  for (const u of g.units || []) {
    const t = u.id.split("/")[0];
    if (!byType.has(t)) byType.set(t, []);
    byType.get(t).push(row(u));
  }
  // TAB_META already names each type the way the rest of the app does
  return [...byType].map(([t, rows]) => ({ label: (TAB_META[t] || {}).label || t, rows }));
}

async function chooseUnits(g) {
  const total = (g.units || []).length;
  const r = await modal({
    title: g.label,
    text: "Everything ticked here goes into the archive. Untick what you do not "
      + "need — the other groups are unaffected.",
    wide: true,
    fields: [{ id: "u", type: "checklist", groups: bUnitGroups(g) }],
    ok: "Use these",
  });
  if (!r) return;
  const chosen = r.u || [];
  if (!chosen.length) {
    // ticking nothing in a group and unticking the group are the same request
    delete BUNITS[g.id];
    BPICKS.delete(g.id);
  } else if (chosen.length === total) {
    delete BUNITS[g.id];
  } else {
    BUNITS[g.id] = new Set(chosen);
  }
  renderBackup();
}

function backupCreateCard() {
  const card = el("div.card", { style: { marginBottom: "1.25rem" } });
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Create a backup" }),
      el("div.card-description", {
        text: "Pick what goes in, down to the single skill or server. Nothing is "
          + "moved or changed — this only reads." }))));

  const body = el("div.card-content.tight");
  const totals = el("span.hint");
  const warn = el("div", {});

  const sync = () => {
    const picked = BACKUP.plan.filter((g) => BPICKS.has(g.id)).map(bTotals);
    totals.textContent = plural(picked.reduce((n, t) => n + t.files, 0), "file") + " · "
      + fbytes(picked.reduce((n, t) => n + t.bytes, 0)) + " before compression";
    warn.innerHTML = "";
    if (BACKUP.plan.some((g) => g.secrets && BPICKS.has(g.id) && bTotals(g).files))
      warn.append(el("div.alert.alert-warning", { style: { margin: "0 1.125rem 1rem" } },
        el("span.alert-icon", {}, icon("key")),
        el("div.alert-body", {},
          el("div.alert-title", { text: "This archive will contain credentials" }),
          el("div", { text: "MCP server configs are copied exactly as they are, API keys "
            + "and tokens included — a redacted copy would not restore. Treat the zip "
            + "like the secrets inside it." }))));
  };

  for (const g of BACKUP.plan) {
    const t = bTotals(g);
    const row = el("label.cl-row", { class: g.files ? "" : "off" });
    if (g.files) {
      row.append(el("input", {
        type: "checkbox", checked: BPICKS.has(g.id),
        onchange: (e) => {
          if (e.currentTarget.checked) BPICKS.add(g.id); else BPICKS.delete(g.id);
          sync();
        },
      }));
    } else {
      row.append(el("span.cl-slot"));
    }

    const line = el("div.cl-line", {}, el("span.li-name", { text: g.label }));
    if (g.secrets) line.append(badge("secrets", "warning"));
    if (bCustom(g)) {
      const b = badge("custom", "info");
      b.title = "Only some of this group is selected";
      line.append(b);
    }
    const extra = el("span.cl-extra", {},
      el("span.hint", { style: { whiteSpace: "nowrap" },
        text: !g.files ? "nothing here"
          : t.files + " of " + plural(g.files, "file") + " · " + fbytes(t.bytes) }));
    if ((g.units || []).length > 1) {
      const b = mkbtn("btn-sm btn-ghost", "Choose…", (e) => {
        e.preventDefault();     // the row is a label; don't toggle it too
        chooseUnits(g);
      }, "Pick what goes in from this group");
      b.prepend(icon("filter"));
      extra.append(b);
    }
    line.append(extra);
    row.append(el("div.cl-body", {}, line, el("span.li-desc", { text: g.note })));
    body.append(row);
  }
  sync();
  card.append(body, warn);

  const note = el("input", { type: "text", placeholder: "Optional note — “before wiping the laptop”" });
  const make = mkbtn("btn btn-primary", "Create backup", async () => {
    const picks = [...BPICKS];
    if (!picks.length) { toast("Nothing selected", true); return; }
    const units = {};
    for (const id of picks) if (BUNITS[id]) units[id] = [...BUNITS[id]];
    make.disabled = true;
    make.textContent = "Writing…";
    try {
      const r = await api("/api/backup-create", { picks, units, note: note.value });
      toast(r.name + " — " + plural(r.files, "file") + ", " + fbytes(r.zip_bytes));
      renderBackup(true);
    } catch (e) {
      toast(e.message, true);
      make.disabled = false;
      make.textContent = "Create backup";
    }
  });
  make.prepend(icon("download"));
  card.append(el("div.card-content", { style: { display: "flex", gap: ".5rem", alignItems: "center" } },
    note, totals, el("span.spring", { style: { flex: "1" } }), make));
  return card;
}

function backupArchivesCard() {
  const card = el("div.card");
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Archives" }),
      el("div.card-description", { text: "Restore shows you every file it would touch before it writes anything." }))));
  const body = el("div.card-content.flush");
  if (!BACKUP.archives.length) {
    card.append(el("div.card-content", {},
      emptyState("No backups yet", "Pick what you want above and create one.", "archive")));
    return card;
  }
  const list = el("div.list");
  for (const a of BACKUP.archives) list.append(archiveRow(a));
  body.append(list);
  card.append(body);
  return card;
}

function archiveRow(a) {
  const actions = el("div.li-actions", {});
  if (!a.error) {
    const b = mkbtn("btn-sm btn-primary", "Restore…", () => openRestore(a.name),
      "See what it would change, then pick");
    b.prepend(icon("upload"));
    actions.append(b);
  }
  const more = mkbtn("btn-sm btn-icon btn-ghost", "", (e) => openMenu(e.currentTarget, [
    { label: "Copy path", icon: "copy", fn: () => copyText(a.path, "path") },
    { label: "Delete backup", icon: "trash", danger: true, fn: () => deleteArchive(a) },
  ]), "More actions");
  more.append(icon("chevronDown"));
  actions.append(more);

  const badges = (a.groups || []).map((g) => badge(g, "outline"));
  if (a.contains_secrets) {
    const b = badge("secrets", "warning");
    b.title = "Contains MCP server configs, credentials included";
    badges.push(b);
  }
  if (a.error) badges.push(badge("unreadable", "destructive"));

  const desc = a.error ? a.error
    : fwhen(a.created_at) + " · " + plural(a.files, "file") + " · " + fbytes(a.zip_bytes) + " on disk"
      + (a.bytes ? " (" + fbytes(a.bytes) + " uncompressed)" : "")
      + (a.note ? " · " + a.note : "");

  // an archive is a named thing with badges and actions, the same shape every
  // other list of those uses — not the flat file row it used to borrow
  return el("div.list-item", {},
    el("div.li-main", {},
      el("span.li-name.mono", { title: a.path, text: a.name }), ...badges),
    el("span.li-desc", { text: desc }),
    actions);
}

async function deleteArchive(a) {
  const ok = await mconfirm("Delete " + a.name + "?",
    "The archive file is removed from " + BACKUP.dir + ". Your live config is untouched, "
    + "but this copy of it is gone for good.", "Delete");
  if (!ok) return;
  try {
    await api("/api/backup-delete", { name: a.name });
    toast(a.name + " deleted");
    renderBackup(true);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------ fresh start --
   Reset with a parachute. The server snapshots everything first and refuses
   to delete a single file until that zip is on disk; the way back in is the
   same restore picker every archive uses, opened on the snapshot with nothing
   ticked except the config files. What the reset never touches: your login
   (~/.claude.json keeps everything but mcpServers), and anything in the
   config dir this app does not model — credentials, session state, todos. */

function freshStartCard() {
  const card = el("div.card", {
    style: { marginTop: "1.25rem", borderColor: "var(--destructive)" } });
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Start fresh" }),
      el("div.card-description", {
        text: "Back everything up automatically, wipe the config, then pick "
          + "what comes back — piece by piece." }))));

  const keep = el("input", { type: "checkbox", checked: true });
  const body = el("div.card-content.tight");
  body.append(el("div.drow", {}, icon("info"),
    el("span.dmsg", {},
      el("div", { text: "Deletes: skills, commands, agents, output styles, "
        + "CLAUDE.md, settings, keybindings, statusline, plugins, and the MCP "
        + "servers in ~/.claude.json." }),
      el("div.hint", { text: "Keeps: your login and everything else in "
        + "~/.claude.json, plus files this app does not manage. Plugins you "
        + "restore re-download on the next Claude Code start." }))));
  body.append(el("label.cl-row", {}, keep,
    el("div.cl-body", {},
      el("div.cl-line", {}, el("span.li-name", { text: "Keep cost history (transcripts)" })),
      el("span.li-desc", { text: "Unticked, transcripts go into the snapshot "
        + "and are deleted from disk — the Costs tab starts at zero." }))));
  card.append(body);

  const go = mkbtn("btn btn-destructive", "Reset config…", async () => {
    const ok = await mconfirm("Reset your Claude Code config?",
      "Three steps, in order: a full backup is written first, then the config "
      + "is wiped" + (keep.checked ? "" : " including cost history")
      + ", then you pick what to restore from that backup. If the backup "
      + "cannot be written, nothing is deleted.",
      "Back up and reset");
    if (!ok) return;
    go.disabled = true;
    go.textContent = "Backing up & resetting…";
    try {
      const r = await api("/api/fresh-start", { keep_transcripts: keep.checked });
      BFRESH = r.snapshot;
      if (r.failed && r.failed.length)
        toast(r.failed.length + " path(s) could not be deleted: "
          + r.failed[0].error + " — snapshot is safe at " + r.snapshot_path, true);
      else
        toast("Config reset — snapshot saved as " + r.snapshot);
      await refresh();          // items, settings and MCP all just changed
      await openRestore(r.snapshot, true);
      renderBackup(true);
    } catch (e) {
      toast(e.message, true);
      go.disabled = false;
      go.textContent = "Reset config…";
    }
  });
  go.prepend(icon("refresh"));
  card.append(el("div.card-content", {
    style: { display: "flex", justifyContent: "flex-end" } }, go));
  return card;
}

/* ---------------------------------------------------------------- restore --
   The dry run. Every row already knows its verdict; the ticks decide what gets
   written. `differs` rows start ticked (you asked to restore), `same` rows are
   hidden behind a toggle since writing identical bytes is a no-op. */

async function openRestore(name, fresh) {
  try {
    const rep = await api("/api/backup-inspect?name=" + encodeURIComponent(name));
    // after a reset, start from nothing: only the config files are ticked,
    // because a config that cannot even find its settings is the one thing
    // nobody means by "fresh". A normal restore keeps its old default.
    const want = fresh
      ? (e) => e.group === "config" && e.status !== "refused" && e.status !== "missing"
      : (e) => e.status === "new" || e.status === "differs";
    BREPORT = { ...rep, picked: new Set(rep.entries.filter(want).map((e) => e.path)) };
    BSHOWSAME = false;
    BOPEN = new Set();
    renderBackup();
  } catch (e) { toast(e.message, true); }
}

function restorePanel() {
  const wrap = el("div", {});
  const rep = BREPORT;
  const c = rep.counts || {};

  wrap.append(el("div.toolbar", {},
    mkbtn("btn-sm", BFRESH ? "Skip — keep empty config" : "← Back to archives",
      () => { BREPORT = null; BFRESH = null; renderBackup(); }),
    el("div.toolbar-end", {},
      el("span.hint", {
        text: "restoring into " + rep.config_dir }))));

  if (BFRESH)
    wrap.append(el("div.alert.alert-success", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("success")),
      el("div.alert-body", {},
        el("div.alert-title", { text: "Config reset — snapshot saved as " + BFRESH }),
        el("div", { text: "Tick what you want back; everything else stays gone. "
          + "The snapshot keeps sitting in your archives either way, so nothing "
          + "here is a one-shot decision." }))));

  wrap.append(el("div.stat-grid", { style: { marginBottom: "1rem" } },
    statCard(String(c.new || 0), "new", { accent: true, hint: "not on disk here" }),
    statCard(String(c.differs || 0), "differs", { hint: "on disk but not identical" }),
    statCard(String(c.same || 0), "identical", { hint: "writing them changes nothing" }),
    statCard(String((c.refused || 0) + (c.missing || 0)), "unusable",
      { hint: "listed but unreadable or unsafe" })));

  if (rep.manifest && rep.manifest.config_dir
      && rep.manifest.config_dir !== rep.config_dir_abs)
    wrap.append(el("div.alert.alert-warning", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("warn")),
      el("div.alert-body", {},
        el("div.alert-title", { text: "Made on a different config directory" }),
        el("div", { text: "This archive was taken from " + rep.manifest.config_dir
          + ". Restoring writes into " + rep.config_dir + " instead." }))));

  const card = el("div.card");
  const head = el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: rep.name }),
      el("div.card-description", {
        text: "Tick what to write. Nothing is deleted, and files you untick are left alone." })));
  card.append(head);

  const body = el("div.card-content.flush");
  const rows = rep.entries.filter((e) => BSHOWSAME || e.status !== "same");
  const count = el("span.hint", {});
  const sync = () => { count.textContent = BREPORT.picked.size + " selected"; };

  const setAll = (v) => {
    for (const e of rows) {
      if (e.status === "refused" || e.status === "missing") continue;
      if (v) BREPORT.picked.add(e.path); else BREPORT.picked.delete(e.path);
    }
    renderBackup();
  };

  card.append(el("div.card-content", { style: { display: "flex", gap: ".5rem", alignItems: "center" } },
    count,
    el("span.spring", { style: { flex: "1" } }),
    switchToggle("Show identical", BSHOWSAME, (v) => { BSHOWSAME = v; renderBackup(); }),
    mkbtn("btn-sm btn-ghost", "All", () => {
      const selectable = rows.filter((e) => e.status !== "refused" && e.status !== "missing");
      setAll(!selectable.every((e) => BREPORT.picked.has(e.path)));
    })));

  for (const node of unitRows(rows, { picked: BREPORT.picked, open: BOPEN }, sync))
    body.append(node);
  card.append(body);

  const apply = mkbtn("btn btn-primary", "Restore selected", () => applyRestore());
  apply.prepend(icon("upload"));
  card.append(el("div.card-content", { style: { display: "flex", justifyContent: "flex-end" } }, apply));
  sync();
  wrap.append(card);
  return wrap;
}

const BSTATUS = { new: ["new", "success"], differs: ["differs", "warning"],
  same: ["identical", "secondary"], refused: ["refused", "destructive"],
  missing: ["missing", "destructive"] };

// restoring the plugin list does not bring plugin files back — Claude Code
// re-clones them from the marketplaces the restored list names
const PLUGIN_REFETCH = "plugins re-download on next Claude Code start";

/* Group inspect rows into the things a person would name and render one node
   each. A skill is one tick, not the eleven files inside it — the same rows the
   create pick list showed. Archives from before units were recorded have no
   unit and fall back to one row per file.

   `sess` is {picked, open}: the two Sets a panel keeps for itself, so the
   Backup tab and the Projects tab can render the same rows over different
   selections. */
function unitRows(rows, sess, sync) {
  const units = new Map();
  for (const e of rows) {
    const key = e.group + "\u0000" + (e.unit || e.path);
    if (!units.has(key)) units.set(key, { key, entries: [] });
    units.get(key).entries.push(e);
  }
  return [...units.values()].map((u) => u.entries.length === 1
    ? restoreRow(u.entries[0], sess, sync)
    : unitRestoreRows(u, sess, sync));
}

/* One row for a whole unit — a skill folder, a project's transcripts. The
   checkbox is the unit's verdict on all its usable files (indeterminate when
   they disagree), and the files themselves sit behind an expander. */
function unitRestoreRows(u, sess, sync) {
  const first = u.entries[0];
  const usable = u.entries.filter((e) => e.status !== "refused" && e.status !== "missing");
  const files = el("div", { hidden: !sess.open.has(u.key),
    style: { paddingLeft: "1.75rem" } });

  const row = el("div.drow", {});
  const cb = el("input", { type: "checkbox" });
  const syncBox = () => {
    const on = usable.filter((e) => sess.picked.has(e.path)).length;
    cb.checked = on > 0 && on === usable.length;
    cb.indeterminate = on > 0 && on < usable.length;
    sync();
  };
  cb.onchange = () => {
    for (const e of usable) {
      if (cb.checked) sess.picked.add(e.path);
      else sess.picked.delete(e.path);
    }
    fill();
    syncBox();
  };
  row.append(usable.length ? cb : icon("warn"));

  const bytes = u.entries.reduce((n, e) => n + (e.size || 0), 0);
  row.append(el("span.dmsg", {},
    el("div", {}, el("span.li-name", { text: first.unit_label || first.unit })),
    el("div.hint", { text: [first.unit_desc, plural(u.entries.length, "file"),
        fbytes(bytes), first.group === "plugins" ? PLUGIN_REFETCH : ""]
      .filter(Boolean).join(" · ") })));

  // one badge per verdict present, counted — "3 new · 2 differs" at a glance
  const counts = {};
  for (const e of u.entries) counts[e.status] = (counts[e.status] || 0) + 1;
  for (const [status, n] of Object.entries(counts)) {
    const [label, variant] = BSTATUS[status] || [status, "secondary"];
    row.append(badge(n > 1 ? n + " " + label : label, variant));
  }

  const toggle = mkbtn("btn-sm btn-ghost", "Files", () => {
    files.hidden = !files.hidden;
    if (files.hidden) sess.open.delete(u.key); else sess.open.add(u.key);
  }, "Show the files inside");
  toggle.prepend(icon("chevronDown"));
  row.append(el("div.dactions", {}, toggle));

  const fill = () => {
    files.innerHTML = "";
    for (const e of u.entries) files.append(restoreRow(e, sess, syncBox));
  };
  fill();
  syncBox();
  return el("div", {}, row, files);
}

function restoreRow(e, sess, sync) {
  const [label, variant] = BSTATUS[e.status] || [e.status, "secondary"];
  const usable = e.status !== "refused" && e.status !== "missing";
  const row = el("div.drow", {});
  if (usable) {
    row.append(el("input", {
      type: "checkbox", checked: sess.picked.has(e.path),
      onchange: (ev) => {
        if (ev.currentTarget.checked) sess.picked.add(e.path);
        else sess.picked.delete(e.path);
        sync();
      },
    }));
  } else {
    row.append(icon("warn"));
  }
  row.append(el("span.dmsg", {},
    el("div.dmono", { text: e.target || e.path }),
    el("div.hint", {
      text: (e.error || "") || ([e.unit_desc || e.group, fbytes(e.size),
        e.group === "plugins" ? PLUGIN_REFETCH : ""]
        .filter(Boolean).join(" · ")) })));
  row.append(badge(label, variant));

  const actions = el("div.dactions", {});
  if (e.diff) {
    const pre = el("pre.diffbox", { hidden: true });
    for (const line of e.diff.split("\n"))
      pre.append(el("span", {
        // the ---/+++ file headers lead with the same characters as a removed
        // and an added line, and are neither
        class: /^(---|\+\+\+|@@)/.test(line) ? "hunk"
             : line.startsWith("+") ? "add"
             : line.startsWith("-") ? "del" : "",
        text: line + "\n" }));
    const b = mkbtn("btn-sm btn-ghost", "Diff", () => { pre.hidden = !pre.hidden; },
      "What would change in this file");
    b.prepend(icon("columns"));
    actions.append(b);
    row.append(actions);
    return el("div", {}, row, pre);
  }
  row.append(actions);
  return row;
}

async function applyRestore() {
  const paths = [...BREPORT.picked];
  if (!paths.length) { toast("Nothing selected", true); return; }
  const rows = BREPORT.entries.filter((e) => BREPORT.picked.has(e.path));
  const over = rows.filter((e) => e.status === "differs").length;
  const mcp = rows.some((e) => e.group === "mcp");
  const plug = rows.some((e) => e.group === "plugins");
  const ok = await mconfirm("Restore " + plural(paths.length, "file") + "?",
    (over ? plural(over, "file") + " on disk will be overwritten with the backup's version, and "
          + "that cannot be undone from here. " : "")
    + (mcp ? "MCP servers are merged into ~/.claude.json one at a time; the rest of that "
           + "file is left alone. " : "")
    + (plug ? "The plugin list only names your plugins — Claude Code downloads "
            + "them again the next time it starts. " : "")
    + "Nothing is deleted.",
    over ? "Overwrite and restore" : "Restore");
  if (!ok) return;
  try {
    const r = await api("/api/backup-restore", { name: BREPORT.name, paths });
    if (r.failed_count)
      toast(r.count + " restored, " + r.failed_count + " failed: " + r.failed[0].error, true);
    else
      toast(plural(r.count, "file") + " restored"
        + (BFRESH ? " — fresh start complete" : "")
        + (plug ? " · " + PLUGIN_REFETCH : ""));
    BREPORT = null;
    BFRESH = null;
    await refresh();          // items, settings and MCP all just changed
    renderBackup(true);
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- discover
/* Search over everything Claude Code can load, whether or not you own it —
   your items, installed plugins, on-disk marketplaces, and (once you opt in)
   Anthropic's official and community catalogs. All of that is answered by
   GET /api/search, which never reaches the network: the index is built from
   files already on this machine, and the two remote catalogs are static
   documents downloaded once and cached to disk.

   skills.sh search is the one exception, and it is kept visibly separate for
   that reason — different state (DSH), its own section, its own consent, and
   a button you have to press. Nothing you type reaches it by typing alone. */

let DQ = "";
let DHITS = null;   // null = not searched yet this tab-open; [] = no hits
let DCATALOG = null;
let DSEQ = 0;        // monotonic guard: a slow response cannot overwrite a newer one
let DTIMER = null;

/* skills.sh results are live third-party hits, not part of the local index —
   kept in their own state rather than merged into DHITS so "local unless you
   pressed the button" stays true by construction, not by convention.
   DSH: null = never searched this tab-open, [] = searched, no hits.
   DSHQ: the query those results are for, so the section can say so.
   DSHSEQ: same monotonic guard DSEQ is, for the remote round-trip. */
let DSH = null;
let DSHQ = "";
let DSHSEQ = 0;
let DSHBUSY = false;

/* Jump to Browse with `q` pre-filled and searched — the one navigation every
   catalog bridge uses: the palette's catalog group, and the Plugins segment's
   and inventory tabs' "search the whole catalog" empty-state links. Keeps its
   name because those callers know it by it.

   Routes through goTab() for its unsaved-changes guard; if that guard sends
   the user nowhere (they declined to discard an edit), the segment will not
   actually be Browse afterward, and DQ is left alone rather than primed for a
   navigation that did not happen. */
function goDiscoverSearch(q) {
  goSeg("browse");
  if (!onSeg("browse")) return;
  DQ = q;
  DHITS = null;
  DSH = null;   // results for the old query — never carried onto a new one
  DSHQ = "";
  render();
}

// The empty-state bridge itself: "Search the whole catalog for “q” →",
// wired to goDiscoverSearch. Shared by renderPlugins()'s and
// renderInventory()'s noMatches() branches.
function catalogSearchLink(q) {
  return el("div", {},
    mkbtn("btn-link btn-sm", "Search the whole catalog for “" + q + "” →",
      () => goDiscoverSearch(q)));
}

const DISCOVER_GROUPS = [
  { key: "yours", header: "In your config" },
  { key: "installed", header: "Installed on this machine" },
  { key: "ondisk", header: "In your marketplaces, not installed" },
  { key: "official", header: "Anthropic catalog", badge: "official" },
  { key: "community", header: "Anthropic community catalog", badge: "community",
    hint: "Passed Anthropic's automated validation and safety screening. "
      + "Screened is not endorsed." },
];

const DISCOVER_ICON = { skill: "sparkles", command: "terminal", agent: "bot",
  "output-style": "droplet", mcp: "server", plugin: "plug", marketplace: "download" };
const DISCOVER_TAB = { skill: "skills", command: "commands", agent: "agents",
  "output-style": "output-styles" };

// One card per remote group (official/community) with no recorded consent —
// or consent explicitly withdrawn. `ok` here means exactly one thing: "you
// may download this static public document from this URL." Still nothing to
// word carefully around for query leaks: these are downloads, not searches —
// what you type is never part of the request either way.
//
// Built on the same .card-header/.card-title/.card-description shape every
// other card in the app uses (see the hooks card in renderSettings) so the
// header's padding and border match, and the .drow rows below it sit at the
// card's normal inset rather than flush against its border.
function discoverConsentCard() {
  if (!DCATALOG || DCATALOG.error) return null;
  const consent = DCATALOG.discover_consent || {};
  const pending = DISCOVER_GROUPS.filter((g) => g.badge && !(consent[g.key] || {}).ok);
  if (!pending.length) return null;
  const card = el("div.card", { style: { marginBottom: "1.25rem" } });
  card.append(el("div.card-header", {},
    el("div", {},
      el("div.card-title", { text: "Anthropic's plugin catalogs" }),
      el("div.card-description", {
        text: "Turning one of these on downloads Anthropic's public plugin "
          + "list from GitHub — a static JSON file, cached here and searched "
          + "offline afterwards. What you type is not part of the request." }))));
  for (const g of pending) {
    card.append(el("div.drow", {},
      el("span.dmsg", {},
        el("div", {}, el("span.dmono", { text: g.header }), badge(g.badge, "outline")),
        g.hint ? el("div.hint", { text: g.hint }) : null),
      el("div.dactions", {}, mkbtn("btn-sm btn-primary", "Enable",
        () => discoverEnable(g.key)))));
  }
  return card;
}

/* Download one remote catalog. `grant` records consent first — the only
   difference between enabling a source and refreshing one already enabled,
   which is why they were the same twenty lines twice. */
async function catalogFetch(source, grant) {
  const [busy, done, failed] = grant
    ? ["Fetching", "Fetched · ", "fetch failed"]
    : ["Refreshing", "Refreshed · ", "refresh failed"];
  const t = toast({ title: busy + " the " + source + " catalog…",
                    variant: "loading", duration: 0 });
  try {
    if (grant) await api("/api/discover-consent", { source, ok: true });
    const res = await api("/api/catalog-refresh", { sources: [source] });
    t.close();
    const r = (res.results || {})[source];
    toast(r && r.ok ? done + r.detail : ((r || {}).detail || failed),
         !(r && r.ok));
    await renderDiscover(true);
  } catch (err) { t.close(); toast(err.message, true); }
}

const discoverEnable = (source) => catalogFetch(source, true);
const discoverRefresh = (source) => catalogFetch(source, false);

async function renderDiscover(reload) {
  if (!onSeg("browse")) return;
  const view = document.getElementById("skillseg");
  // the one cached() caller whose failure is payload rather than an alert: the
  // consent card has to render even when the index could not be read, because
  // an unfetched catalog is exactly when you need it
  if (!await cached({ view: "skillseg", url: "/api/catalog", reload,
                      skeleton: 0, errorAsPayload: true,
                      get: () => DCATALOG, set: (v) => { DCATALOG = v; },
                      alive: () => onSeg("browse") })) return;

  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "Search everything Claude Code can load on this machine — your own "
      + "items, installed plugins, and marketplaces you have registered — "
      + "whether or not you own it yet. <b>Typing searches this machine "
      + "only.</b> What you type leaves this machine only when you press "
      + "Search skills.sh.",
  }));

  if (DCATALOG.error) view.append(errorAlert(DCATALOG.error));

  const consentCard = discoverConsentCard();
  if (consentCard) view.append(consentCard);

  const inp = el("input", {
    type: "search", id: "dq", placeholder: "Search skills, commands, agents, plugins, MCP servers…",
    value: DQ,
    oninput: (e) => {
      DQ = inp.value;
      if (e.isComposing) return;
      discoverSearch();   // local index only — never the remote one
    },
    // Enter is one of the two explicit triggers for the remote search (the
    // button is the other). The isComposing/229 guard is not cosmetic here
    // the way it is on oninput: the Enter that commits a Japanese or Chinese
    // IME composition would otherwise fire a mid-word query at a third
    // party. Same guard, much higher stakes.
    onkeydown: (e) => {
      if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      DQ = inp.value;
      skillsShSearch();
    },
  });
  const counts = DCATALOG.counts || {};
  const countLine = Object.entries(counts).map(([k, n]) => n + " " + k).join(" · ");
  view.append(el("div.toolbar", {}, inp,
    mkbtn("btn-sm", "Search skills.sh", () => { DQ = inp.value; skillsShSearch(); },
      "Sends what you type to skills.sh, a third party. Nothing is sent until "
      + "you press this."),
    el("div.toolbar-end", {}, el("span.hint", { text: countLine }))));

  const box = el("div", { id: "discoverresults" });
  view.append(box);
  discoverRenderResults(box);
  if (DHITS === null) discoverSearch();
}

function discoverSearch() {
  clearTimeout(DTIMER);
  DTIMER = setTimeout(async () => {
    const seq = ++DSEQ;
    let hits;
    try {
      const r = await api("/api/search?q=" + encodeURIComponent(DQ) + "&limit=60");
      hits = r.hits || [];
    } catch (e) {
      if (seq !== DSEQ || !onSeg("browse")) return;
      DHITS = [];
      toast(e.message, true);
      const box = document.getElementById("discoverresults");
      if (box) discoverRenderResults(box);
      return;
    }
    if (seq !== DSEQ || !onSeg("browse")) return;
    DHITS = hits;
    const box = document.getElementById("discoverresults");
    if (box) discoverRenderResults(box);
  }, 120);
}

function discoverRenderResults(box) {
  box.innerHTML = "";
  if (DHITS === null) { box.append(skeletonList()); box.append(skillsShSection()); return; }
  // "nothing on this machine matches" is exactly when skills.sh is worth
  // offering, so the section renders after the empty state rather than being
  // skipped along with the groups.
  if (!DHITS.length) { box.append(noMatches(DQ)); box.append(skillsShSection()); return; }
  for (const g of DISCOVER_GROUPS) {
    const hits = DHITS.filter((h) => h.entry.group === g.key);
    if (!hits.length) continue;
    const title = sectionTitle(g.header, hits.length);
    if (g.badge) title.append(badge(g.badge, "outline"));
    box.append(title);
    if (g.hint) box.append(el("div.hint", { text: g.hint }));
    if (g.badge) {
      const fetchedAt = (DCATALOG.discover_fetched_at || {})[g.key];
      box.append(el("div.hint", {},
        el("span", { text: (fetchedAt ? "fetched " + relTime(fetchedAt) : "not fetched yet") + " · " }),
        mkbtn("btn-link btn-sm", "Refresh", () => discoverRefresh(g.key))));
    }
    const list = el("div.list");
    for (const h of hits) list.append(discoverRow(h));
    box.append(list);
  }
  box.append(skillsShSection());
}

function discoverRow(h) {
  const e = h.entry;
  const row = el("div.drow", { title: (h.why || []).join(", ") },
    icon(DISCOVER_ICON[e.kind] || "plug"),
    el("span.dmsg", {},
      el("div", {},
        el("span.dmono", { text: e.name }),
        e.parent ? el("span.hint", { text: " from " + e.parent }) : null),
      el("div.hint", { text: e.description || "" })),
    e.marketplace ? badge(e.marketplace, "outline") : null);

  const actions = el("div.dactions");
  if (e.group === "yours" && DISCOVER_TAB[e.kind]) {
    actions.append(mkbtn("btn-sm", "Open", () =>
      openItemEditor(DISCOVER_TAB[e.kind], e.name, null, e.state === "enabled")));
  } else if (e.group === "installed" && e.kind === "plugin") {
    actions.append(mkbtn("btn-sm", "Show in Plugins", () => { PQ = e.name; goSeg("plugins"); }));
  } else if (e.kind === "plugin" && e.installable && !e.blocked) {
    // Reached from "ondisk" and from the two remote catalogs alike — the
    // check is on the entry's own fields, not on which group it landed in.
    actions.append(mkbtn("btn-sm btn-primary", "Install", () => discoverInstall(e)));
  } else if (e.kind === "plugin" && e.blocked) {
    actions.append(badge("blocked by policy", "outline"));
  }
  // Audit is additive, not exclusive with the actions above — a skill from a
  // marketplace can be both "installed" (Show in Plugins) and auditable.
  if (e.kind === "skill" && e.marketplace)
    actions.append(mkbtn("btn-sm", "Audit", () => skillAudit(e.id)));
  row.append(actions);
  return row;
}

// ----------------------------------------------------------- skills.sh search
//
// The public directory of agent skills, most of which are not on this machine
// and so cannot appear in the local index at all. This is the one search in
// the app that reaches the network, and everything about how it is presented
// exists to keep that obvious:
//
//   - its own section, below the local groups, never merged into them;
//   - its own state (DSH), never DHITS;
//   - fired only by the Search skills.sh button or Enter, never by the
//     as-you-type debounce that drives the local search;
//   - its own consent flag (query_ok), asked for in its own words.
//
// Nothing here installs anything. skills.sh skills are not plugins — the CLI
// path is `npx skills add`, which is arbitrary npm execution — so a result
// offers Audit, a copyable install command, and a link. This app never runs
// it for you.

function skillsShConsentState() {
  return (DCATALOG && DCATALOG.discover_consent
          && DCATALOG.discover_consent.skills_sh) || {};
}

// Two consents stack, and each modal describes only its own grant: a user who
// clicked Audit earlier granted "ask about this one named skill", which is
// not the same as "send me what you type". Granting search implies audit, so
// this one sets both.
async function skillsShSearchConsent() {
  const ok = await modal({
    title: "Search skills.sh?",
    text: "This sends what you type to skills.sh, a third party, every time "
      + "you press Search. Nothing is sent while you type, and your local "
      + "search stays on this machine either way. Nothing found here is "
      + "installed or run — you get an audit, a link, and a command you can "
      + "copy. You can turn this back off in the skills.sh section.",
    ok: "Turn on skills.sh search",
  });
  if (!ok) return false;
  await api("/api/discover-consent", { source: "skills_sh", ok: true, query_ok: true });
  if (DCATALOG && DCATALOG.discover_consent)
    DCATALOG.discover_consent.skills_sh = { ok: true, query_ok: true };
  return true;
}

async function skillsShOff() {
  try {
    // ok stays true: withdrawing search does not withdraw the per-skill
    // audit consent the user may have granted separately. The reverse is not
    // true — remote.py forces query_ok off whenever ok goes off.
    await api("/api/discover-consent", { source: "skills_sh", ok: true, query_ok: false });
    if (DCATALOG && DCATALOG.discover_consent)
      DCATALOG.discover_consent.skills_sh = { ok: true, query_ok: false };
    DSH = null;
    DSHQ = "";
    toast("skills.sh search off — nothing you type leaves this machine");
    const box = document.getElementById("discoverresults");
    if (box) discoverRenderResults(box);
  } catch (e) { toast(e.message, true); }
}

async function skillsShSearch() {
  const q = (DQ || "").trim();
  if (q.length < 2) { toast("Type at least 2 characters to search skills.sh", true); return; }
  if (!skillsShConsentState().query_ok) {
    const granted = await skillsShSearchConsent();
    if (!granted) return;
  }
  const seq = ++DSHSEQ;
  DSHBUSY = true;
  DSHQ = q;
  const box0 = document.getElementById("discoverresults");
  if (box0) discoverRenderResults(box0);
  try {
    const r = await api("/api/skills-search", { q, limit: 25 });
    if (seq !== DSHSEQ || !onSeg("browse")) return;
    DSH = r.skills || [];
  } catch (e) {
    if (seq !== DSHSEQ || !onSeg("browse")) return;
    DSH = [];
    toast(e.message, true);
  } finally {
    if (seq === DSHSEQ) DSHBUSY = false;
  }
  if (!onSeg("browse")) return;
  const box = document.getElementById("discoverresults");
  if (box) discoverRenderResults(box);
}

function skillsShSection() {
  const frag = document.createDocumentFragment();
  const on = !!skillsShConsentState().query_ok;
  const title = sectionTitle("skills.sh", DSH ? DSH.length : null);
  title.append(badge(on ? "on" : "off", "outline"));
  frag.append(title);

  frag.append(el("div.hint", {
    text: on
      ? "The public skills directory — searched only when you press Search "
        + "skills.sh, which sends what you type to a third party."
      : "The public skills directory, including skills you don't have. Off: "
        + "nothing you type leaves this machine until you turn it on." }));

  const controls = el("div.hint", {},
    mkbtn("btn-link btn-sm", DSHBUSY ? "Searching…" : "Search skills.sh",
      () => skillsShSearch()));
  if (on) {
    controls.append(el("span", { text: " · " }));
    controls.append(mkbtn("btn-link btn-sm", "Turn off", () => skillsShOff()));
  }
  frag.append(controls);

  if (DSHBUSY) { frag.append(skeletonList()); return frag; }
  if (DSH === null) return frag;
  frag.append(el("div.hint", {
    text: DSH.length ? "Results for “" + DSHQ + "”"
                     : "No skills.sh results for “" + DSHQ + "”" }));
  if (!DSH.length) return frag;
  const list = el("div.list");
  for (const s of DSH) list.append(skillsShRow(s));
  frag.append(list);
  return frag;
}

// Every field here is third-party text, so it goes in via `text:` only —
// never `html:` — the same rule the rest of this view follows. The endpoint
// returns no description, so a row is name / source / installs and nothing
// more; there is no summary to show and none is invented.
function skillsShRow(s) {
  const meta = s.source + (s.installs != null
    ? " · " + s.installs.toLocaleString() + " installs" : "");
  const row = el("div.drow", {},
    icon("sparkles"),
    el("span.dmsg", {},
      el("div", {}, el("span.dmono", { text: s.name })),
      el("div.hint", { text: meta })));

  const actions = el("div.dactions", {},
    mkbtn("btn-sm", "Audit", () => skillAudit(s.id)),
    mkbtn("btn-sm", "Copy install",
      () => copyText("npx skills add " + s.source, "install command"),
      "Copies `npx skills add " + s.source + "` — this app never runs it"));
  // The URL was assembled server-side from the sanitized id, but it still
  // passes safeHref before becoming an href; a value that fails simply loses
  // its button rather than rendering an unclickable or unsafe one.
  const href = s.url ? safeHref(s.url) : null;
  if (href)
    actions.append(el("a.btn.btn-sm",
      { href, target: "_blank", rel: "noreferrer", text: "Open ↗" }));
  row.append(actions);
  return row;
}

// ------------------------------------------------------------ skills.sh audit
//
// skills.sh's audit endpoint: a documented, public, multi-provider security
// verdict lookup for one named skill the user is already looking at — never
// a search, never free-text. The weaker of skills_sh's two consent flags
// (see remote.py's consent_set_skills_sh()): `ok` means "you may ask
// skills.sh about this one named skill". Nothing the user typed is part of
// it — the "query" is the skill's own name, chosen by clicking Audit on a
// row already in view, whether that row came from the local index or from a
// skills.sh search the user already consented to.

async function skillAuditConsent() {
  const ok = await modal({
    title: "Ask skills.sh for a security scan?",
    text: "This sends the skill's name to skills.sh, a third party, so it "
      + "can return a security verdict (risk level, provider findings from "
      + "services like Socket/Snyk). Nothing you typed is sent, and this "
      + "does not turn on skills.sh search.",
    ok: "Send it",
  });
  if (!ok) return false;
  await api("/api/discover-consent", { source: "skills_sh", ok: true });
  // query_ok is preserved, not reset: the server leaves it alone when the
  // request omits it, so mirroring `false` here would wrongly re-prompt for
  // search consent the user may already have granted.
  if (DCATALOG && DCATALOG.discover_consent)
    DCATALOG.discover_consent.skills_sh = {
      ok: true, query_ok: !!skillsShConsentState().query_ok };
  return true;
}

// Rendered generically/defensively — the sanitized response's exact fields
// are not a schema this codebase pins down (skills.sh is third-party, and
// remote.py's sanitizer only guarantees types, not field names), so this
// just formats whatever keys came back rather than assuming e.g. a fixed
// "risk"/"providers" shape.
function auditVerdictText(data) {
  if (!data || typeof data !== "object" || !Object.keys(data).length)
    return "skills.sh returned no data for this skill.";
  return Object.entries(data).map(([k, v]) =>
    k + ": " + (typeof v === "object" && v !== null ? JSON.stringify(v) : String(v))
  ).join("\n");
}

async function skillAudit(id) {
  const consent = (DCATALOG && DCATALOG.discover_consent && DCATALOG.discover_consent.skills_sh) || {};
  if (!consent.ok) {
    const granted = await skillAuditConsent();
    if (!granted) return;
  }

  const t = toast({ title: "Asking skills.sh for a security scan…", variant: "loading", duration: 0 });
  try {
    const res = await api("/api/skill-audit", { id });
    t.close();
    await modal({ title: "skills.sh audit", text: auditVerdictText(res.data), ok: "Close" });
  } catch (err) { t.close(); toast(err.message, true); }
}

// Xk / Yk phrasing for the install-confirm dialog's token-cost line, or null
// when the entry carries neither number (most "ondisk" entries: the cache
// does not report token cost, only the catalog phases after this one will).
function tokensLine(tokens) {
  const always = tokens && tokens.always_on;
  const invoke = tokens && tokens.on_invoke;
  if (always == null && invoke == null) return null;
  const fmt = (n) => (n / 1000).toFixed(1) + "k";
  if (always != null && invoke != null)
    return "Adds ~" + fmt(always) + " always-on tokens to every session, "
      + fmt(invoke) + " when invoked.";
  if (always != null)
    return "Adds ~" + fmt(always) + " always-on tokens to every session.";
  return "Adds ~" + fmt(invoke) + " tokens when invoked.";
}

async function discoverInstall(e) {
  const src = e.source || {};
  const lines = ["Runs `claude plugin install " + e.id + " --scope user`."];
  if (src.kind || src.path)
    lines.push("Source: " + (src.kind || "?") + (src.path ? " " + src.path : "") + ".");
  // safeHref() mirrors editor.js's markdown-link discipline: the URL is shown
  // only once it passes the same http(s)-with-a-real-host check, even though
  // catalog.py already validated it server-side — belt and suspenders. modal()
  // renders this paragraph as plain text (never markup), so an unsafe value
  // could never become a clickable/executable link either way; the check just
  // decides whether it is worth showing at all.
  if (src.url && safeHref(src.url)) lines.push("URL: " + src.url);
  if (src.ref) lines.push("Ref: " + src.ref + ".");
  if (src.sha) lines.push("Commit: " + src.sha + ".");
  if (e.hooks) lines.push("This plugin ships hooks. Hooks run shell commands on "
    + "Claude Code lifecycle events — before you see anything, and every session.");
  if (e.pinned === false) lines.push("This entry is not pinned to a commit. You get "
    + "whatever that branch holds today, and whatever it holds tomorrow.");
  const tl = tokensLine(e.tokens);
  if (tl) lines.push(tl);

  const r = await modal({ title: "Install " + e.name, text: lines.join(" "), ok: "Install" });
  if (!r) return;

  // scope is always "user" for now — a project-scope install picker is a
  // follow-up; every Discover install lands in every project on this machine,
  // same as the existing user-plugin-install flow on the Plugins tab.
  await act("catalog-install", { id: e.id, scope: "user" },
    (res) => ({ text: res.ok ? "Installed · " + res.detail : res.detail,
                err: !res.ok }),
    { busy: "Running claude plugin…",
      then: async () => { await refresh(); discoverSearch(); } });
}

// ------------------------------------------------------------------ render

function render() {
  closeDropdown();
  renderHeader();
  renderTabs();
  const views = { settings: "settingsview", mcp: "mcpview", statusline: "stlview",
    projects: "projectsview", setup: "setupview", insight: "insightview",
    context: "contextview", costs: "costsview", doctor: "doctorview",
    skills: "skillsview", backup: "backupview" };
  const isEditor = !!EDITING;
  document.getElementById("editorview").hidden = !isEditor;
  document.getElementById("itemsview").hidden = isEditor || !INVENTORY_TABS.includes(TAB);
  if (isEditor) {
    for (const v of Object.values(views)) document.getElementById(v).hidden = true;
    renderEditor();
    return;
  }
  for (const [t, v] of Object.entries(views))
    document.getElementById(v).hidden = TAB !== t;
  if (TAB === "skills") { renderSkills(); return; }
  if (INVENTORY_TABS.includes(TAB)) { renderInventory(TAB); return; }
  if (TAB === "settings") { renderSettings(); return; }
  if (TAB === "mcp") { renderMcp(); return; }
  if (TAB === "statusline") { renderStatusline(); return; }
  if (TAB === "projects") { renderProjects(); return; }
  if (TAB === "setup") { renderSetup(); return; }
  if (TAB === "insight") { renderInsight(); return; }
  if (TAB === "context") { renderContext(); return; }
  if (TAB === "costs") { renderCosts(); return; }
  if (TAB === "doctor") { renderDoctor(); return; }
  if (TAB === "backup") { renderBackup(); return; }
}

async function refresh() {
  DATA = await api("/api/state");
  if (!TABS.includes(TAB)) TAB = "skills";
  render();
}

// -------------------------------------------------------------------- wire

document.getElementById("themebtn").append(icon("contrast"));
document.getElementById("themebtn").onclick = (e) => openThemeMenu(e.currentTarget);
document.getElementById("palettebtn").querySelector(".sb-icon").append(icon("search"));
document.getElementById("palettebtn").onclick = openPalette;
document.getElementById("cfgchip").onclick = (e) => openCfgMenu(e.currentTarget);

addEventListener("hashchange", () => {
  // No lock needed: if the hash already describes what's on screen, we put it
  // there ourselves and there is nothing to do.
  if (location.hash.slice(1) === currentHash()) return;
  routeFromHash();
});

addEventListener("beforeunload", (e) => {
  if (EDITING && EDITING.dirty) { e.preventDefault(); e.returnValue = ""; }
});

// Keyboard: Ctrl/Cmd+K palette, "/" focuses the view filter, Escape closes the
// editor or an open menu, 1-9 switch tabs.
document.addEventListener("keydown", (e) => {
  if (!document.getElementById("modal").hidden) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (PAL) closePalette();
    else openPalette();
    return;
  }
  // Ctrl/Cmd-S saves the open editor — checked before the input-focus bailout
  // below, since the cursor is in the textarea exactly when you press it
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && EDITING) {
    e.preventDefault();
    saveFile();
    return;
  }
  if (PAL) return;  // the palette input handles its own keys
  const tag = e.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.key === "Escape") {
    closeDropdown();
    if (EDITING) closeEditor();
  } else if (e.key === "/") {
    // whichever view is showing owns the only visible filter box
    const f = document.querySelector(".toolbar input[type=search]");
    if (f) { e.preventDefault(); f.focus(); f.select(); }
  } else if (e.key === "?") {
    openPalette();
  } else if (e.key >= "1" && e.key <= "9" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const t = TABS[+e.key - 1];
    if (t) goTab(t);
  }
});

// Load the inventory first, then honour whatever the hash asked for — a deep
// link into a file needs DATA.items to know whether that item is enabled.
refresh().then(() => {
  if (location.hash.slice(1) !== currentHash()) routeFromHash();
});
