/* ===========================================================================
   app.js — data, routing, and the views.

   Component primitives (el/icon/toast/modal/openMenu/filterSelect/…) come from
   ui.js, which is loaded first. Nothing in here builds a toast, dialog, menu
   or combobox by hand, and nothing hard-codes a colour: every surface is a
   class from components.css over a token from theme.css.
   =========================================================================== */

let DATA = { items: {}, config_files: [], config_dir: "", settings: {}, mcp: {}, statusline: {} };

const ITEM_TABS = ["skills", "commands", "agents", "output-styles"];
// the types core.ITEM_TYPES calls kind "dir": a folder of files, not one file
const DIR_TYPES = new Set(["skills"]);
const TABS = [...ITEM_TABS, "mcp", "statusline", "projects", "setup", "settings", "insight", "costs", "doctor", "plugins", "backup"];

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
  "costs": { icon: "dollar", label: "Costs" },
  "doctor": { icon: "pulse", label: "Doctor" },
  "plugins": { icon: "plug", label: "Plugins" },
  "backup": { icon: "archive", label: "Backup" },
};

let TAB = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "skills";
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

function goTab(t) {
  if (!TABS.includes(t) || (EDITING && !confirmDiscard())) return;
  EDITING = null;   // leaving for a tab closes the editor
  closeNewStyle();  // …and discards a half-filled new-style form
  TAB = t;
  location.hash = t;
  render();
}

/* ---------------------------------------------------------------- routing --
   The hash carries the open file, not just the tab: #skills/pdf/SKILL.md and
   #file/<path>, both with an optional ?line=. That is what makes "click a
   doctor finding" survive a back button and a reload. */

const currentHash = () => {
  if (!EDITING) return TAB;
  const enc = encodeURIComponent;
  return EDITING.item
    ? EDITING.type + "/" + enc(EDITING.name) + "/" + enc(EDITING.file || "")
    : "file/" + enc(EDITING.abs || EDITING.path);
};

function edSyncHash() {
  const want = currentHash();
  if (location.hash.slice(1) !== want) location.hash = want;
}

function routeFromHash() {
  const [head, query] = location.hash.slice(1).split("?");
  const segs = head.split("/").filter(Boolean).map((s) => {
    try { return decodeURIComponent(s); } catch (e) { return s; }
  });
  const line = +new URLSearchParams(query || "").get("line") || 0;
  const locate = line ? { line } : null;

  if (segs[0] === "file" && segs[1]) return openPath(segs[1], locate);
  if (ITEM_TABS.includes(segs[0]) && segs.length >= 2) {
    const it = ((DATA.items || {})[segs[0]] || []).find((i) => i.name === segs[1]);
    return openItemEditor(segs[0], segs[1], segs[2] || null,
      it ? it.enabled : true, locate);
  }
  if (EDITING) {
    if (!confirmDiscard()) { edSyncHash(); return; }
    EDITING = null;
  }
  TAB = TABS.includes(segs[0]) ? segs[0] : "skills";
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
  if (ITEM_TABS.includes(t))
    return String(((DATA.items || {})[t] || []).filter((i) => i.enabled).length);
  if (t === "settings") return String(Object.keys((DATA.settings || {}).data || {}).length);
  if (t === "mcp") return String(((DATA.mcp || {}).servers || []).filter((s) => s.enabled).length);
  if (t === "doctor" && DOCTOR && DOCTOR.warns) return String(DOCTOR.warns);
  if (t === "plugins" && PLUGINS)
    return String(PLUGINS.plugins.filter((p) => p.enabled).length);
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
      b.append(el("span.tab-count", { class: t === "doctor" ? "warn" : "", text: count }));
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

// ----------------------------------------------------------------- plugins

let PLUGINS = null;
let PQ = "";

const KIND_LABEL = { agents: "agent", commands: "command", skills: "skill",
  "output-styles": "output style", mcp: "MCP server", hooks: "hooks" };

const pluralKind = (k, n) =>
  n + " " + KIND_LABEL[k] + (n === 1 || k === "hooks" ? "" : "s");

const countLine = (p) =>
  Object.entries(p.counts).map(([k, n]) => pluralKind(k, n)).join(" · ");

async function renderPlugins(reload) {
  const view = document.getElementById("pluginsview");
  if (!PLUGINS || reload) {
    if (!PLUGINS) { view.innerHTML = ""; view.append(skeletonList(3)); }
    PDETAIL = {};
    try { PLUGINS = await api("/api/plugins"); }
    catch (e) {
      view.innerHTML = "";
      view.append(el("div.alert.alert-destructive", {},
        el("span.alert-icon", {}, icon("error")), el("div.alert-body", { text: e.message })));
      return;
    }
    if (TAB !== "plugins") return;
  }
  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "Plugins on disk under <b>" + esc(PLUGINS.root) + "</b>, as set in <b>settings.json</b>. "
      + "A plugin is enabled as a whole — <b>Split</b> copies the parts you want into your own "
      + "config and turns the plugin off, so they survive the next plugin update.",
  }));

  view.append(subagentModelBar());

  if (PLUGINS.error) {
    view.append(el("div.alert.alert-destructive", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("error")),
      el("div.alert-body", {},
        el("div.alert-title", { text: "Could not read the plugin config" }),
        el("div", { text: PLUGINS.error }))));
  }

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
    return;
  }

  const section = (state, label, hint) => {
    const rows = shown.filter((p) => p.state === state);
    if (!rows.length) return;
    view.append(sectionTitle(label, rows.length));
    if (hint) view.append(el("div.view-head", { text: hint, style: { marginTop: "-.35rem" } }));
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

/* What a Split left behind: ordinary items in your config dir that remember
   which plugin they came from. They already show up under Skills/Commands/…,
   but only here do you see them as a group, next to the plugin they answer to. */
function adoptedSection(view) {
  const rows = (PLUGINS.adopted || []).filter((a) =>
    !PQ.trim() || (a.name + " " + a.source).toLowerCase().includes(PQ.trim().toLowerCase()));
  if (!rows.length) return;

  view.append(sectionTitle("Split into your config", rows.length));
  view.append(el("div.view-head", { style: { marginTop: "-.35rem" },
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
  try {
    await api("/api/plugin-resync", { type: a.type, name: a.name });
    toast(a.name + " re-synced from " + a.source);
    await refresh();
    renderPlugins(true);
  } catch (e) { toast(e.message, true); }
}

function pluginRow(p) {
  const splittable = p.components.some((c) => c.adoptable);
  const agents = p.components.filter((c) => c.kind === "agents");
  const actions = el("div.li-actions", {});

  const body = el("div.plugin-detail", { hidden: true });
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
  const more = mkbtn("btn-sm btn-icon btn-ghost", "", (e) => openMenu(e.currentTarget, [
    { label: "Copy path", icon: "copy", fn: () => copyText(p.path, "path") },
    ...p.components.filter((c) => c.kind === "skills").map((c) => ({
      label: "Turn off skill “" + c.name + "”", icon: "power",
      fn: () => skillOverride(c.name, "off"),
    })),
  ]), "More actions");
  more.append(icon("chevronDown"));
  actions.append(more);

  const badges = [badge(p.marketplace, "outline")];
  if (p.state === "available") badges.push(badge("not set", "secondary"));
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

async function togglePluginDetail(p, btn, body) {
  body.hidden = !body.hidden;
  btn.classList.toggle("is-open", !body.hidden);
  body.hidden ? POPEN.delete(p.id) : POPEN.add(p.id);
  if (body.hidden || body.dataset.built) return;
  body.dataset.built = "1";
  body.append(el("div.hint", { text: "Reading the plugin…" }));
  try {
    if (!PDETAIL[p.id]) PDETAIL[p.id] = await api("/api/plugin-detail?id=" + encodeURIComponent(p.id));
  } catch (e) {
    body.innerHTML = "";
    body.append(el("div.hint", { text: e.message }));
    return;
  }
  body.innerHTML = "";
  pluginAgentsPanel(p, body);
  pluginEnvPanel(p, PDETAIL[p.id].env || [], body);
}

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

function pluginEnvPanel(p, env, body) {
  body.append(el("div.pd-title", { text: "Settings this plugin reads" }));
  if (!env.length) {
    body.append(el("div.pd-note", {
      text: "Nothing found — this plugin's code does not read any environment "
        + "variable of its own." }));
    return;
  }
  body.append(el("div.pd-note", {
    html: "Names found by reading the plugin's own code, not a list it publishes — "
      + "treat them as a lead. They are written to <b>env</b> in settings.json, "
      + "where they apply to every session, not just this plugin." }));
  for (const e of env) {
    const sc = scalarControl(
      { key: e.model ? SUBAGENT_KEY : "env." + e.name, type: "combo",
        values: e.model ? modelValues() : [] },
      e.value || undefined, "unset");
    const right = el("div.pd-ctrl", {}, sc.node);
    if (sc.aux) right.append(sc.aux);
    right.append(mkbtn("btn-sm btn-primary", "Set",
      () => setPluginEnv(e.name, sc.collect())));
    if (e.value) right.append(mkbtn("btn-sm danger", "Clear", () => setPluginEnv(e.name, "")));

    const name = el("div.pd-name", {},
      el("span.skey", { title: "read in " + e.files.join(", "), text: e.name }),
      e.model ? badge("model", "outline") : null,
      e.value ? badge("set", "default") : null);
    body.append(el("div.pd-row", {}, name, right));
    if (e.doc)
      body.append(el("div.pd-doc", { title: e.doc.file, text: e.doc.line }));
  }
}

async function setPluginEnv(name, value) {
  try {
    await api("/api/plugin-env-set", { name, value: value === undefined ? "" : String(value) });
    toast(name + (value ? " set to " + value : " cleared") + " · applies to new sessions");
    await refresh();
    renderPlugins(true);
  } catch (e) { toast(e.message, true); }
}

async function setItemModel(name, model) {
  try {
    await api("/api/item-model-set", { name, model: model === undefined ? "" : String(model) });
    toast(name + (model ? " runs on " + model : " back to inheriting")
      + " · applies to new sessions");
    await refresh();
    renderPlugins(true);
  } catch (e) { toast(e.message, true); }
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
  try {
    const res = await api("/api/plugin-split", {
      id: p.id, picks, disable: p.state !== "available", models: chosen });
    toast("Kept " + res.kept + " of " + res.total + " from " + p.name
      + (res.disabled ? " · plugin disabled" : "") + " · applies to new sessions",
      false, res.disabled
        ? { label: "Re-enable plugin", fn: () => pluginToggle(p.id, true) }
        : null);
    await refresh();
    renderPlugins(true);
  } catch (e) { toast(e.message, true); }
}

async function pluginToggle(id, enabled) {
  try {
    await api("/api/plugin-toggle", { id, enabled });
    toast(id.split("@")[0] + (enabled ? " enabled" : " disabled") + " · applies to new sessions");
    await refresh();
    renderPlugins(true);
  } catch (e) { toast(e.message, true); }
}

async function skillOverride(name, value) {
  try {
    await api("/api/skill-override", { name, value });
    toast("Skill " + name + " set to " + value + " · applies to new sessions",
      false, { label: "Undo", fn: () => skillOverride(name, null) });
    await refresh();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------- setup

let SETUP = null;

async function renderSetup(reload) {
  const view = document.getElementById("setupview");
  if (!SETUP || reload) {
    if (!SETUP) {
      view.innerHTML = "";
      view.append(skeletonList(2));
    }
    try { SETUP = await api("/api/setup"); }
    catch (e) {
      view.innerHTML = "";
      view.append(el("div.alert.alert-destructive", {},
        el("span.alert-icon", {}, icon("error")), el("div.alert-body", { text: e.message })));
      return;
    }
    if (TAB !== "setup") return;
  }
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
      const fold = el("details.piece-notes", {},
        el("summary", { text: "What it writes (" + p.notes.length + " keys)" }));
      for (const n of p.notes) fold.append(el("div.li-desc", { text: n }));
      row.append(fold);
    }
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

// ---------------------------------------------------------------- projects

let PROJECTS = null;

// mode -> the live filename inside <project>/.claude/ (projects.py MODES)
const PROJ_FILES = { replace: "system-prompt.md", append: "append-system-prompt.md" };

async function renderProjects(reload) {
  const view = document.getElementById("projectsview");
  if (!PROJECTS || reload) {
    if (!PROJECTS) {
      view.innerHTML = "";
      view.append(skeletonList(3));
    }
    try { PROJECTS = await api("/api/projects"); }
    catch (e) {
      view.innerHTML = "";
      view.append(el("div.alert.alert-destructive", {},
        el("span.alert-icon", {}, icon("error")), el("div.alert-body", { text: e.message })));
      return;
    }
    if (TAB !== "projects") return;
  }
  view.innerHTML = "";
  view.append(el("div.view-head", {
    text: "Per-project system prompts. Each project keeps its prompt in its own .claude/ "
        + "directory: system-prompt.md replaces Claude Code's entire default system prompt, "
        + "append-system-prompt.md adds to it. Disable is a rename to .md.off — the whole "
        + "feature is plain files, nothing recorded elsewhere.",
  }));
  const f = PROJECTS.flags;
  if (!f.cli || !f.system_prompt_file) {
    view.append(el("div.alert.alert-warning", {},
      el("span.alert-icon", {}, icon("warning")),
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
  if (st.wrapper !== "none")
    wacts.append(mkbtn("btn-sm btn-ghost", "Copy run command",
      () => copyText("./.claude/claude.sh", "run command")));
  if (st.wrapper !== "current" && !st.missing)
    wacts.append(mkbtn("btn-sm", st.wrapper === "none" ? "Create" : "Regenerate",
      () => projWrapper(st)));
  body.append(wrow);

  const styles = st.output_styles.length ? st.output_styles.join(", ") : null;
  const setting = Object.entries(st.output_style_setting)
    .map(([f2, v]) => v + " (" + f2 + ")").join(", ");
  if (styles || setting) {
    body.append(el("div.drow", {},
      icon("droplet"),
      el("span.dmsg", { text: "Output styles in this project: "
        + (styles || "none") + (setting ? " · outputStyle: " + setting : "") })));
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
  commands: "var(--chart-3)", agents: "var(--chart-4)", "output-styles": "var(--chart-5)" };
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
  if (!INSIGHT || rescan) {
    view.innerHTML = "";
    view.append(el("div.muted", { style: { marginBottom: ".75rem", fontSize: ".8125rem" },
      text: "Estimating context cost and scanning session transcripts…" }), skeletonList(5));
    try { INSIGHT = await api("/api/insight" + (rescan ? "?rescan" : "")); }
    catch (e) {
      view.innerHTML = "";
      view.append(el("div.alert.alert-destructive", {},
        el("span.alert-icon", {}, icon("error")), el("div.alert-body", { text: e.message })));
      return;
    }
    if (TAB !== "insight") return;
  }
  const b = INSIGHT.budget, u = INSIGHT.usage;
  view.innerHTML = "";
  view.append(el("div.view-head", {
    html: "What your config costs at the start of <i>every</i> session (chars÷4 estimate — CLAUDE.md "
      + "is injected wholesale, each active item contributes its name + description), and what "
      + "actually gets used (session transcripts in <b>" + esc(u.dir) + "</b>, parsed locally).",
  }));

  const used = u.by || {};
  const now = Date.now();
  const unused = [];
  for (const [t, kind] of Object.entries(USAGE_KIND)) {
    for (const s of ((b.types[t] || {}).items) || []) {
      const rec = (used[kind] || {})[s.name];
      const last = rec && rec.last ? Date.parse(rec.last) : 0;
      if (!last || now - last > 90 * 86400000)
        unused.push({ type: t, name: s.name, last });
    }
  }

  const stats = el("div.stat-grid", { style: { marginBottom: "1.25rem" } },
    statCard(tokfmt(b.total), "tokens every session", { accent: true }),
    statCard(tokfmt(b.claude_md), "CLAUDE.md"),
    statCard(tokfmt((b.types.skills || {}).tokens || 0), "skill descriptions"));
  if (u.available) stats.append(statCard(String(u.sessions), "sessions scanned"));
  if (u.available && u.sessions) stats.append(statCard(String(unused.length), "unused 90d+"));
  view.append(stats);
  view.append(el("div.toolbar", {},
    openFileBtn(cfgPath("CLAUDE.md"), "Edit CLAUDE.md", null,
      "The single biggest fixed cost above — open it")));

  const segs = [["CLAUDE.md", b.claude_md],
    ...Object.entries(b.types).map(([t, v]) => [t, v.tokens])];
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
  view.append(bar, key);

  const consumers = Object.entries(b.types)
    .flatMap(([t, v]) => v.items.map((it) => ({ type: t, ...it })))
    .sort((a, b2) => b2.tokens - a.tokens)
    .slice(0, 12);
  view.append(sectionTitle("Top context consumers"));
  view.append(dataTable(["Item", "Type", "~tokens"],
    consumers.map((c) =>
      `<td class="mono">${esc(c.name)}</td><td class="dim">${esc(c.type)}</td>`
      + `<td class="num">${tokfmt(c.tokens)}</td>`)));

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

let COSTS = null;

function usd(n) {
  if (n >= 100) return "$" + Math.round(n);
  if (n >= 1) return "$" + n.toFixed(2);
  return "$" + n.toFixed(3);
}

async function renderCosts(rescan) {
  const view = document.getElementById("costsview");
  if (!COSTS || rescan) {
    view.innerHTML = "";
    view.append(el("div.muted", { style: { marginBottom: ".75rem", fontSize: ".8125rem" },
      text: "Reading transcripts and pricing usage…" }), skeletonList(4));
    try { COSTS = await api("/api/costs" + (rescan ? "?rescan" : "")); }
    catch (e) {
      view.innerHTML = "";
      view.append(el("div.alert.alert-destructive", {},
        el("span.alert-icon", {}, icon("error")), el("div.alert-body", { text: e.message })));
      return;
    }
    if (TAB !== "costs") return;
  }
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

  if (c.unknown_models.length) {
    view.append(el("div.alert.alert-warning", { style: { marginBottom: "1rem" } },
      el("span.alert-icon", {}, icon("warn")),
      el("div.alert-body", { text: "No list price known for: " + c.unknown_models.join(", ")
        + " — priced at opus-tier; override via 'pricing' in .claude-ui.json" })));
  }

  const rb = mkbtn("btn-sm", "Rescan transcripts", () => renderCosts(true));
  rb.prepend(icon("refresh"));
  view.append(el("div.toolbar", {}, rb));
}

// ------------------------------------------------------------------ doctor

let DFILTER = "all";

async function renderDoctor(rerun) {
  const view = document.getElementById("doctorview");
  if (!DOCTOR || rerun) {
    view.innerHTML = "";
    view.append(el("div.muted", { style: { marginBottom: ".75rem", fontSize: ".8125rem" },
      text: "Running checks…" }), skeletonList(4));
    try { DOCTOR = await api("/api/doctor"); }
    catch (e) {
      view.innerHTML = "";
      view.append(el("div.alert.alert-destructive", {},
        el("span.alert-icon", {}, icon("error")), el("div.alert-body", { text: e.message })));
      return;
    }
    if (TAB !== "doctor") return;
    renderTabs();
  }
  view.innerHTML = "";
  const warns = DOCTOR.warns;
  const infos = DOCTOR.findings.length - warns;

  view.append(el("div.stat-grid", { style: { marginBottom: "1rem" } },
    statCard(String(warns), "warnings", { accent: warns > 0 }),
    statCard(String(infos), "notes"),
    statCard(DOCTOR.ts, "last run")));

  const bar = el("div.toolbar");
  const seg = el("div.row-flex", { style: { gap: ".25rem" } });
  for (const [k, label] of [["all", "All"], ["warn", "Warnings"], ["info", "Notes"]])
    seg.append(mkbtn("btn-sm" + (DFILTER === k ? " on" : ""), label,
      () => { DFILTER = k; renderDoctor(); }));
  bar.append(seg);
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
  for (const t of ITEM_TABS)
    for (const s of (DATA.items || {})[t] || [])
      out.push({ kind: t.replace(/s$/, ""), label: s.name, icon: TAB_META[t].icon,
        hint: (s.enabled ? "" : "(disabled) ") + (s.description || ""),
        run: () => s.broken
          ? (() => { TAB = t; location.hash = t; IQ = s.name; render(); })()
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
  PAL = { q: "", sel: 0, items: palItems() };
  const p = document.getElementById("palette");
  p.hidden = false;
  p.className = "dialog-overlay palette-overlay";
  p.innerHTML = "";

  const inp = el("input.command-input", {
    placeholder: "Jump to anything — items, tabs, themes, actions…",
    "aria-label": "Command palette", spellcheck: false,
  });
  const listEl = el("div.command-list", { role: "listbox" });

  const renderList = () => {
    const list = palMatches();
    listEl.innerHTML = "";
    if (!list.length) {
      listEl.append(el("div.command-empty", { text: "No results." }));
      return;
    }
    let lastKind = null;
    list.forEach((it, i) => {
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

  inp.oninput = () => { PAL.q = inp.value; PAL.sel = 0; renderList(); };
  inp.onkeydown = (e) => {
    const list = palMatches();
    if (e.key === "ArrowDown") {
      e.preventDefault();
      PAL.sel = Math.min(PAL.sel + 1, list.length - 1);
      renderList();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      PAL.sel = Math.max(PAL.sel - 1, 0);
      renderList();
    } else if (e.key === "Enter") {
      const it = list[PAL.sel];
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

async function toggleItem(type, name, enabled) {
  try {
    await api("/api/item-toggle", { type, name, enabled });
    toast(name + (enabled ? " enabled" : " disabled — moved to disabled/") + " · applies to new sessions",
      false, { label: "Undo", fn: () => toggleItem(type, name, !enabled) });
    await refresh();
  } catch (e) { toast(e.message, true); }
}

/* The one action here that takes something away for good.

   Everything else in this app is reversible: disabling parks a file in
   disabled/, editing keeps the bytes it replaced in a diff you were shown, a
   backup can be restored. This cannot, so it asks for the name to be typed and
   the toast carries no Undo — there is nothing held to put back. */
async function deleteItem(type, s, enabled) {
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
  try {
    await api("/api/item-delete", { type, name: s.name, enabled });
    toast(s.name + " deleted");
    await refresh();
  } catch (e) { toast(e.message, true); }
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

function renderInventory() {
  const view = document.getElementById("itemsview");
  const all = (DATA.items || {})[TAB] || [];
  const q = IQ.toLowerCase();
  const items = all.filter((s) =>
    !q || s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q));
  const on = items.filter((s) => s.enabled);
  const off = items.filter((s) => !s.enabled);

  view.innerHTML = "";
  if (TAB === "output-styles" && NEWSTYLE) { renderStyleForm(view); return; }
  view.append(el("div.view-head", {
    html: TAB + " in <b>" + esc(DATA.config_dir) + "/" + TAB + "</b> — everything real on this machine. "
      + "Disabling moves an item to <b>disabled/" + TAB + "/</b>; nothing is deleted. "
      + "Changes apply to new sessions.",
  }));

  const inp = el("input", {
    type: "search", id: "iq", placeholder: "Filter " + TAB + " by name or description…",
    value: IQ,
    oninput: (e) => {
      IQ = inp.value;
      if (e.isComposing) return;
      refilter("iq", renderInventory);
    },
  });
  // Output styles are the one type you are more likely to want than to have,
  // and the only one this app can compose from scratch — see output-styles.js.
  if (TAB === "output-styles") view.append(styleDocsCard());

  const end = el("div.toolbar-end", {},
    el("span.hint", {
      text: on.length + " enabled · " + off.length + " disabled"
        + (items.length !== all.length ? " · " + items.length + " of " + all.length + " shown" : "")
        + (TAB === "output-styles"
           ? " · active: " + (settingsGet("outputStyle") || "default") : "") }));
  if (TAB === "output-styles") {
    const nb = mkbtn("btn-sm btn-primary", "New output style", openNewStyle);
    nb.prepend(icon("plus"));
    end.append(nb);
  }
  view.append(el("div.toolbar", {}, inp, end));

  if (!all.length) {
    view.append(emptyState("No " + TAB + " yet",
      "Anything you put in " + DATA.config_dir + "/" + TAB + " shows up here.",
      TAB_META[TAB].icon));
    return;
  }

  // Which style outputStyle actually selects, so the list can say so. The
  // file being enabled only makes it selectable; this key is what applies it.
  const activeStyle = TAB === "output-styles"
    ? (settingsGet("outputStyle") || "default") : null;

  const section = (list, label, enabled) => {
    if (!list.length) return;
    view.append(sectionTitle(label, list.length));
    const box = el("div.list");
    for (const s of list) {
      const isActive = activeStyle !== null && enabled && !s.broken
        && styleSettingName(s) === activeStyle;
      const actions = el("div.li-actions");
      if (activeStyle !== null && enabled && !s.broken && !isActive) {
        actions.append(mkbtn("btn-sm", "Set active",
          () => commitSetting("outputStyle", styleSettingName(s)),
          "Write outputStyle: " + styleSettingName(s)
          + " to settings.json — applies to new sessions"));
      }
      if (!s.broken) {
        const eb = mkbtn("btn-sm", "Edit", () => openItemEditor(TAB, s.name, null, enabled));
        eb.prepend(icon("pencil"));
        actions.append(eb);
      }
      actions.append(mkbtn("btn-sm" + (enabled ? " danger" : ""),
        enabled ? "Disable" : "Enable", () => toggleItem(TAB, s.name, !enabled)));
      const more = mkbtn("btn-sm btn-icon btn-ghost", "", (e) => openMenu(e.currentTarget, [
        { label: "Copy path", icon: "copy", fn: () => copyText(s.path || s.name, "path") },
        // Broken items have no Edit button (item_read can't follow the dangling
        // link) but the file itself is still openable by path.
        ...(s.broken && s.path
          ? [{ label: "Open by path", icon: "file", fn: () => openPath(s.path) }] : []),
        { label: enabled ? "Disable" : "Enable", icon: "power",
          fn: () => toggleItem(TAB, s.name, !enabled), danger: enabled },
        { label: "Delete…", icon: "trash", danger: true,
          fn: () => deleteItem(TAB, s, enabled) },
      ]), "More actions");
      more.append(icon("chevronDown"));
      actions.append(more);

      let activeBadge = null;
      if (isActive) {
        activeBadge = badge("active", "default");
        activeBadge.title = "outputStyle in settings.json selects this style";
      }
      box.append(el("div.list-item", { class: enabled ? "" : "off" },
        el("div.li-main", {},
          el("span.li-name", { title: s.path || "", text: s.name }),
          activeBadge,
          ...itemBadges(s)),
        el("span.li-desc", { text: s.description || "" }),
        actions));
    }
    view.append(box);
  };

  section(on, "Enabled", true);
  section(off, "Disabled", false);

  if (!items.length)
    view.append(noMatches(IQ));
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
  if (!BACKUP || reload) {
    if (!BACKUP) { view.innerHTML = ""; view.append(skeletonList(3)); }
    try { BACKUP = await api("/api/backup"); }
    catch (e) {
      view.innerHTML = "";
      view.append(el("div.alert.alert-destructive", {},
        el("span.alert-icon", {}, icon("error")), el("div.alert-body", { text: e.message })));
      return;
    }
    if (TAB !== "backup") return;
    bPruneUnits();
  }
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

  // a skill is one tick, not the eleven files inside it — the same unit rows
  // the create pick list showed. Archives from before units were recorded
  // have no unit on their rows and fall back to one row per file.
  const units = new Map();
  for (const e of rows) {
    const key = e.group + " " + (e.unit || e.path);
    if (!units.has(key)) units.set(key, { key, entries: [] });
    units.get(key).entries.push(e);
  }
  for (const u of units.values()) {
    if (u.entries.length === 1) body.append(restoreRow(u.entries[0], sync));
    else body.append(unitRestoreRows(u, sync));
  }
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

/* One row for a whole unit — a skill folder, a project's transcripts. The
   checkbox is the unit's verdict on all its usable files (indeterminate when
   they disagree), and the files themselves sit behind an expander. */
function unitRestoreRows(u, sync) {
  const first = u.entries[0];
  const usable = u.entries.filter((e) => e.status !== "refused" && e.status !== "missing");
  const files = el("div", { hidden: !BOPEN.has(u.key),
    style: { paddingLeft: "1.75rem" } });

  const row = el("div.drow", {});
  const cb = el("input", { type: "checkbox" });
  const syncBox = () => {
    const on = usable.filter((e) => BREPORT.picked.has(e.path)).length;
    cb.checked = on > 0 && on === usable.length;
    cb.indeterminate = on > 0 && on < usable.length;
    sync();
  };
  cb.onchange = () => {
    for (const e of usable) {
      if (cb.checked) BREPORT.picked.add(e.path);
      else BREPORT.picked.delete(e.path);
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
    if (files.hidden) BOPEN.delete(u.key); else BOPEN.add(u.key);
  }, "Show the files inside");
  toggle.prepend(icon("chevronDown"));
  row.append(el("div.dactions", {}, toggle));

  const fill = () => {
    files.innerHTML = "";
    for (const e of u.entries) files.append(restoreRow(e, syncBox));
  };
  fill();
  syncBox();
  return el("div", {}, row, files);
}

function restoreRow(e, sync) {
  const [label, variant] = BSTATUS[e.status] || [e.status, "secondary"];
  const usable = e.status !== "refused" && e.status !== "missing";
  const row = el("div.drow", {});
  if (usable) {
    row.append(el("input", {
      type: "checkbox", checked: BREPORT.picked.has(e.path),
      onchange: (ev) => {
        if (ev.currentTarget.checked) BREPORT.picked.add(e.path);
        else BREPORT.picked.delete(e.path);
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

// ------------------------------------------------------------------ render

function render() {
  closeDropdown();
  renderHeader();
  renderTabs();
  const views = { settings: "settingsview", mcp: "mcpview", statusline: "stlview",
    projects: "projectsview", setup: "setupview", insight: "insightview", costs: "costsview",
    doctor: "doctorview", plugins: "pluginsview", backup: "backupview" };
  const isEditor = !!EDITING;
  document.getElementById("editorview").hidden = !isEditor;
  document.getElementById("itemsview").hidden = isEditor || !ITEM_TABS.includes(TAB);
  if (isEditor) {
    for (const v of Object.values(views)) document.getElementById(v).hidden = true;
    renderEditor();
    return;
  }
  for (const [t, v] of Object.entries(views))
    document.getElementById(v).hidden = TAB !== t;
  if (ITEM_TABS.includes(TAB)) { renderInventory(); return; }
  if (TAB === "settings") { renderSettings(); return; }
  if (TAB === "mcp") { renderMcp(); return; }
  if (TAB === "statusline") { renderStatusline(); return; }
  if (TAB === "projects") { renderProjects(); return; }
  if (TAB === "setup") { renderSetup(); return; }
  if (TAB === "insight") { renderInsight(); return; }
  if (TAB === "costs") { renderCosts(); return; }
  if (TAB === "doctor") { renderDoctor(); return; }
  if (TAB === "plugins") { renderPlugins(); return; }
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
