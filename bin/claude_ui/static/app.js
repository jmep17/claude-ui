let DATA = { items: {}, config_files: [], config_dir: "", settings: {}, mcp: {}, statusline: {} };
const ITEM_TABS = ["skills", "commands", "agents", "output-styles"];
const TABS = [...ITEM_TABS, "mcp", "statusline", "setup", "settings", "insight", "costs", "doctor"];
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

// Theme: JS sets data-theme so a manual choice survives reloads.
(function () {
  let t = null;
  try { t = localStorage.getItem("claude-ui-theme"); } catch (e) {}
  if (t !== "light" && t !== "dark")
    t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = t;
})();

function toggleTheme() {
  const t = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("claude-ui-theme", t); } catch (e) {}
}

// Toasts stack; errors stick around until dismissed (they may contain paths).
// An optional action ({label, fn}) renders as a button — used for undo.
function toast(msg, err, action) {
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = "toastmsg" + (err ? " err" : "");
  const span = document.createElement("span");
  span.textContent = msg;
  el.appendChild(span);
  if (action) {
    const ab = document.createElement("button");
    ab.className = "small";
    ab.textContent = action.label;
    ab.onclick = () => { el.remove(); action.fn(); };
    el.appendChild(ab);
  }
  const x = document.createElement("span");
  x.className = "x";
  x.textContent = "×";
  x.onclick = () => el.remove();
  el.appendChild(x);
  box.appendChild(el);
  while (box.children.length > 5) box.firstChild.remove();
  if (!err) setTimeout(() => el.remove(), action ? 10000 : 3500);
}

// Generic modal replacing prompt()/confirm(): resolves to {field: value} on OK,
// null on cancel/Escape. fields: [{id, label, type: "text"|"select", ...}]
function modal({ title, text, fields = [], ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    const m = document.getElementById("modal");
    m.hidden = false;
    m.innerHTML = "";
    const box = document.createElement("div");
    box.className = "mbox";
    if (title) {
      const h = document.createElement("h3");
      h.textContent = title;
      box.appendChild(h);
    }
    if (text) {
      const p = document.createElement("div");
      p.className = "mtext";
      p.textContent = text;
      box.appendChild(p);
    }
    const inputs = {};
    for (const f of fields) {
      const row = document.createElement("div");
      row.className = "mrow";
      if (f.label) {
        const l = document.createElement("label");
        l.textContent = f.label;
        row.appendChild(l);
      }
      let inp;
      if (f.type === "select") {
        inp = document.createElement("select");
        for (const o of f.options) {
          const op = document.createElement("option");
          op.value = o.value !== undefined ? o.value : o;
          op.textContent = o.label !== undefined ? o.label : o;
          if (op.value === f.value) op.selected = true;
          inp.appendChild(op);
        }
      } else {
        inp = document.createElement("input");
        inp.type = "text";
        inp.value = f.value || "";
        if (f.placeholder) inp.placeholder = f.placeholder;
      }
      inputs[f.id] = inp;
      row.appendChild(inp);
      box.appendChild(row);
    }
    const done = (val) => {
      m.hidden = true;
      m.innerHTML = "";
      document.removeEventListener("keydown", onkey, true);
      resolve(val);
    };
    const submit = () => done(Object.fromEntries(
      Object.entries(inputs).map(([k, i]) => [k, i.value.trim()])));
    const onkey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); done(null); }
      else if (e.key === "Enter" && e.target.tagName !== "SELECT") {
        e.preventDefault();
        submit();
      }
    };
    document.addEventListener("keydown", onkey, true);
    m.onclick = (e) => { if (e.target === m) done(null); };
    const btns = document.createElement("div");
    btns.className = "mbtns";
    const cancel = document.createElement("button");
    cancel.textContent = "cancel";
    cancel.onclick = () => done(null);
    const okb = document.createElement("button");
    okb.textContent = ok;
    okb.className = danger ? "danger" : "primary";
    okb.onclick = submit;
    btns.append(cancel, okb);
    box.appendChild(btns);
    m.appendChild(box);
    const first = Object.values(inputs)[0];
    (first || okb).focus();
    if (first && first.select) first.select();
  });
}

const mconfirm = (title, text, ok) =>
  modal({ title, text, ok: ok || "confirm", danger: true }).then((r) => r !== null);

// Small popup menu anchored to a button (row overflow actions).
function openMenu(anchor, entries) {
  closeMenu();
  const m = document.createElement("div");
  m.className = "menu";
  m.id = "menu";
  for (const e of entries) {
    const b = document.createElement("button");
    b.textContent = e.label;
    if (e.danger) b.className = "danger";
    b.onclick = () => { closeMenu(); e.fn(); };
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  m.style.left = Math.max(8, Math.min(r.right - m.offsetWidth, innerWidth - m.offsetWidth - 8)) + "px";
  m.style.top = Math.max(8, Math.min(r.bottom + 4, innerHeight - m.offsetHeight - 8)) + "px";
  setTimeout(() => document.addEventListener("click", closeMenu, { once: true }));
}

function closeMenu() {
  const m = document.getElementById("menu");
  if (m) m.remove();
}

const esc = (t) => t.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let CFGEDIT = false;

function renderHeader() {
  document.getElementById("cfgprefix").textContent = DATA.config_dir.replace(/\/?$/, "/");
  const row = document.getElementById("cfgrow");
  if (CFGEDIT) {
    row.innerHTML =
      `<span>config dir</span>` +
      `<input type="text" id="cfgdir" value="${esc(DATA.config_dir)}">` +
      `<button class="small" onclick="saveCfgDir()">save</button>` +
      `<button class="small" onclick="CFGEDIT=false;render()">cancel</button>`;
  } else {
    row.innerHTML =
      `<span>managing <b>${esc(DATA.config_dir)}</b>` +
      (DATA.default_dir ? " (the default — Claude Code reads this automatically)" : "") +
      `</span><span style="flex:1"></span>` +
      `<button class="small" onclick="CFGEDIT=true;render()">change…</button>` +
      (DATA.default_dir ? "" : `<button class="small" onclick="resetCfgDir()">reset to default</button>`);
  }
  document.getElementById("cfghint").textContent = DATA.default_dir
    ? "" : "non-default config dir: Claude Code only uses it if CLAUDE_CONFIG_DIR is exported in your shell";
}

async function saveCfgDir() {
  const v = document.getElementById("cfgdir").value.trim();
  try {
    await api("/api/config-dir", { path: v === DATA.config_dir ? "" : v });
    CFGEDIT = false;
    toast("config dir updated");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function resetCfgDir() {
  try { await api("/api/config-dir", { path: "" }); toast("config dir reset to default"); await refresh(); }
  catch (e) { toast(e.message, true); }
}

const allTabs = () => TABS;

function renderTabs() {
  const el = document.getElementById("tabs");
  el.innerHTML = "";
  for (const t of allTabs()) {
    const b = document.createElement("button");
    b.textContent = ITEM_TABS.includes(t)
      ? t + " · " + ((DATA.items || {})[t] || []).filter((i) => i.enabled).length
      : t === "settings"
      ? "settings · " + Object.keys((DATA.settings || {}).data || {}).length
      : t === "mcp"
      ? "mcp · " + ((DATA.mcp || {}).servers || []).length
      : t === "statusline"
      ? "statusline" + ((DATA.statusline || {}).applied ? " ✓" : "")
      : t === "doctor"
      ? "doctor" + (DOCTOR && DOCTOR.warns ? " · " + DOCTOR.warns + "⚠" : "")
      : t;
    b.className = t === TAB ? "on" : "";
    b.onclick = () => { TAB = t; location.hash = t; render(); };
    el.appendChild(b);
  }
}

function settingsGet(key) {
  let node = (DATA.settings || {}).data || {};
  for (const p of key.split(".")) {
    if (node == null || typeof node !== "object") return undefined;
    node = node[p];
  }
  return node;
}

// ---- schema-driven form controls ------------------------------------------
// Every settings control resolves to a `collect()` that returns the value to
// write, `undefined` to clear the key, or throws on invalid input. Selects
// (bool/enum) commit on change; everything else commits on the "set" button.

async function commitSetting(key, value) {
  try {
    await api("/api/settings-set", { key, value });
    toast(value === null ? key + " cleared" : key + " set");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

const clearSetting = (key) => commitSetting(key, null);

function trySet(key, collect) {
  let v;
  try { v = collect(); }
  catch (e) { toast("invalid value: " + e.message, true); return; }
  if (v === undefined) clearSetting(key);
  else commitSetting(key, v);
}

const opt = (v, label) => {
  const o = document.createElement("option");
  o.value = v; o.textContent = label == null ? v : label;
  return o;
};
const mkbtn = (cls, label, onclick) => {
  const b = document.createElement("button");
  b.className = cls; b.textContent = label; b.onclick = onclick;
  return b;
};

let DL_SEQ = 0;
function datalist(values) {
  const dl = document.createElement("datalist");
  dl.id = "dl_" + (++DL_SEQ);
  for (const v of values) dl.appendChild(opt(v));
  return dl;
}

// Live datalist suggestions per setting key; a ":key" suffix targets a kv
// control's key input. Merged with static schema values and the server's
// machine-local DATA.suggest payload by suggestFor().
const itemNames = (t) =>
  ((DATA.items || {})[t] || []).map((it) => it.name).filter(Boolean);
const mcpNames = () => ((DATA.mcp || {}).servers || []).map((sv) => sv.name);
const LIVE_SUGGEST = {
  "outputStyle": () => itemNames("output-styles"),
  "skillOverrides:key": () => itemNames("skills"),
  "mcpServerTimeouts:key": mcpNames,
  "enabledMcpjsonServers": mcpNames,
  "disabledMcpjsonServers": mcpNames,
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
    const sel = document.createElement("select");
    sel.append(opt("", "(unset" + (f.default !== undefined ? ", default: " + f.default : "") + ")"),
      opt("true"), opt("false"));
    if (value === true) sel.value = "true";
    else if (value === false) sel.value = "false";
    return { node: sel, collect: () => sel.value === "" ? undefined : sel.value === "true" };
  }
  if (f.type === "enum") {
    const sel = document.createElement("select");
    sel.appendChild(opt("", "(unset" + (f.default !== undefined ? ", default: " + f.default : "") + ")"));
    // schema values plus any live docs-discovered values for this key
    const vals = suggestFor(f.key, f.values);
    for (const v of vals) sel.appendChild(opt(v));
    // keep an out-of-vocabulary current value visible instead of showing "(unset)"
    if (value !== undefined && value !== null && !vals.includes(String(value)))
      sel.appendChild(opt(String(value), String(value) + " (current)"));
    if (value !== undefined && value !== null) sel.value = String(value);
    return { node: sel, collect: () => sel.value === "" ? undefined : sel.value };
  }
  const inp = document.createElement("input");
  const sugg = suggestFor(f.key, f.values);
  // datalist on type=number is ignored by Safari/Firefox, so suggested
  // numbers use a text input; collect() still validates numerically
  inp.type = f.type === "number" && !sugg.length ? "number" : "text";
  if (f.type === "number") inp.inputMode = "decimal";
  if (value === "") inp.value = '""';
  else if (value !== undefined && value !== null) inp.value = String(value);
  if (ph) inp.placeholder = ph;
  let aux = null;
  if (sugg.length) { aux = datalist(sugg); inp.setAttribute("list", aux.id); }
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
  return { node: inp, aux, collect };
}

// list → one input per entry, with add/remove; optional per-row suggestions.
function listForm(ctrl, s, cur) {
  const box = document.createElement("div");
  box.className = "formrows";
  ctrl.appendChild(box);
  const sugg = suggestFor(s.key, s.item_values);
  const dl = sugg.length ? datalist(sugg) : null;
  if (dl) ctrl.appendChild(dl);
  const addRow = (val) => {
    const r = document.createElement("div");
    r.className = "formrow";
    const inp = document.createElement("input");
    inp.type = "text"; inp.value = val || "";
    if (dl) inp.setAttribute("list", dl.id);
    r.append(inp, mkbtn("small danger", "×", () => r.remove()));
    box.appendChild(r);
  };
  (Array.isArray(cur) ? cur : []).forEach((v) => addRow(String(v)));
  ctrl.appendChild(mkbtn("small", "+ add", () => addRow("")));
  return () => {
    const vals = [...box.querySelectorAll("input")]
      .map((i) => i.value.trim()).filter(Boolean);
    return vals.length ? vals : undefined;
  };
}

// kv → key/value row editor; value control is a dropdown (s.values),
// number input (s.value_type === "number"), or free text.
function mapForm(ctrl, s, cur) {
  const box = document.createElement("div");
  box.className = "formrows";
  ctrl.appendChild(box);
  const ksugg = suggestFor(s.key + ":key", s.key_values);
  const kdl = ksugg.length ? datalist(ksugg) : null;
  if (kdl) ctrl.appendChild(kdl);
  const addRow = (k, v) => {
    const r = document.createElement("div");
    r.className = "formrow";
    const kin = document.createElement("input");
    kin.type = "text"; kin.className = "kk"; kin.placeholder = "key";
    kin.value = k || "";
    if (kdl) kin.setAttribute("list", kdl.id);
    const val = scalarControl(
      s.values ? { type: "enum", values: s.values }
        : s.value_type === "number" ? { type: "number" } : { type: "string" },
      v, "value");
    r.append(kin, val.node);
    if (val.aux) r.appendChild(val.aux);
    r.append(mkbtn("small danger", "×", () => r.remove()));
    box.appendChild(r);
    return () => {
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
  ctrl.appendChild(mkbtn("small", "+ add", () => collectors.push(addRow("", ""))));
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
  const box = document.createElement("div");
  box.className = "formobj";
  ctrl.appendChild(box);
  const obj = cur && typeof cur === "object" && !Array.isArray(cur) ? cur : {};
  const collectors = [];
  for (const f of s.fields) {
    if (f.const !== undefined) continue;
    const line = document.createElement("label");
    line.className = "formfield";
    const lab = document.createElement("span");
    lab.className = "flabel";
    lab.textContent = f.key + (f.desc ? " — " + f.desc : "");
    line.appendChild(lab);
    // dotted path so subfields resolve live/server suggestions by full key
    const sc = scalarControl({ ...f, key: s.key + "." + f.key }, obj[f.key]);
    line.appendChild(sc.node);
    if (sc.aux) line.appendChild(sc.aux);
    box.appendChild(line);
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
  const ta = document.createElement("textarea");
  ta.placeholder = "JSON";
  const text = isSet ? JSON.stringify(cur, null, 2) : "";
  ta.value = text;
  const fit = () => { ta.rows = Math.min(12, Math.max(2, ta.value.split("\n").length)); };
  fit();
  if ((s.templates || []).length) {
    const sel = document.createElement("select");
    const ph = opt("", "insert template…");
    ph.disabled = true;
    sel.append(ph, ...s.templates.map((t, i) => opt(String(i), t.name)));
    sel.value = "";
    sel.onchange = async () => {
      const t = s.templates[Number(sel.value)];
      sel.value = "";
      if (!t) return;
      if (ta.value.trim() && !(await mconfirm("replace " + s.key,
          "replace the editor contents with the '" + t.name + "' template?",
          "replace")))
        return;
      ta.value = JSON.stringify(t.value, null, 2);
      fit();
    };
    ctrl.appendChild(sel);
  }
  ctrl.appendChild(ta);
  return () => {
    const r = ta.value.trim();
    if (!r) return undefined;
    try { return JSON.parse(r); }
    catch (e) { throw new Error("JSON: " + e.message); }
  };
}

function settingRow(s) {
  const cur = settingsGet(s.key);
  const isSet = cur !== undefined;
  const row = document.createElement("div");
  row.className = "srow";

  const meta = document.createElement("div");
  meta.className = "smeta";
  meta.innerHTML =
    `<span class="skey">${esc(s.key)}</span>` +
    (isSet ? '<span class="badge group">set</span>' : "") +
    `<div class="sdesc">${esc(s.desc || "")}</div>`;

  const ctrl = document.createElement("div");
  ctrl.className = "sctrl";
  if (s.type === "object" || s.type === "list" || s.type === "kv" || s.type === "json")
    ctrl.classList.add("wide");

  if (s.type === "bool" || s.type === "enum") {
    // fixed-choice dropdown that commits immediately
    const sc = scalarControl(s, cur);
    sc.node.onchange = () => {
      const v = sc.collect();
      v === undefined ? clearSetting(s.key) : commitSetting(s.key, v);
    };
    ctrl.appendChild(sc.node);
  } else {
    let collect;
    if (s.type === "object") collect = objectForm(ctrl, s, cur);
    else if (s.type === "list") collect = listForm(ctrl, s, cur);
    else if (s.type === "kv") collect = mapForm(ctrl, s, cur);
    else if (s.type === "json") collect = jsonForm(ctrl, s, cur, isSet);
    else {
      const ph = s.default !== undefined ? "default: " + s.default : "(unset)";
      const sc = scalarControl(s, cur, ph);
      ctrl.appendChild(sc.node);
      if (sc.aux) ctrl.appendChild(sc.aux);
      collect = sc.collect;
    }
    ctrl.appendChild(mkbtn("small", "set", () => trySet(s.key, collect)));
    if (isSet) ctrl.appendChild(mkbtn("small danger", "clear", () => clearSetting(s.key)));
  }

  row.append(meta, ctrl);
  return row;
}

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
  const r = await modal({ title: "add hook",
    text: "the command receives the event JSON on stdin; exit code 2 blocks the action (tool events)",
    fields: [
      { id: "e", label: "event", type: "select", options: HOOK_EVENTS },
      { id: "m", label: "matcher — tool name pattern, tool events only (blank = all)",
        placeholder: "e.g. Bash or Edit|Write" },
      { id: "c", label: "command" },
      { id: "t", label: "timeout seconds (optional)" }], ok: "add" });
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
    toast("hook added — applies to new sessions");
  } catch (e) { toast(e.message, true); }
}

async function hookDelete(row) {
  if (!(await mconfirm("delete hook", row.event + ": " + row.command, "delete"))) return;
  const hooks = JSON.parse(JSON.stringify(DATA.settings.data.hooks));
  const m = hooks[row.event][row.mi];
  m.hooks.splice(row.hi, 1);
  if (!m.hooks.length) hooks[row.event].splice(row.mi, 1);
  if (!hooks[row.event].length) delete hooks[row.event];
  try {
    await hooksSave(hooks);
    toast("hook removed");
  } catch (e) { toast(e.message, true); }
}

async function hookFire(row) {
  toast("piping a sample " + row.event + " event into the command…");
  try {
    const r = await api("/api/hook-test", { command: row.command, event: row.event });
    const bits = [row.event + " test: " + r.detail];
    if ((r.stdout || "").trim()) bits.push("stdout: " + r.stdout.trim().slice(0, 300));
    if ((r.stderr || "").trim()) bits.push("stderr: " + r.stderr.trim().slice(0, 300));
    toast(bits.join(" · "), !r.ok);
  } catch (e) { toast(e.message, true); }
}

let SFILTER = { q: "", set: false };

function renderSettings() {
  const el = document.getElementById("settingsview");
  const st = DATA.settings || {};
  el.innerHTML =
    `<div class="sethead">editing <b>${esc(st.path || "")}</b>` +
    (st.exists ? "" : " (file will be created on first set)") +
    " · changes apply to new sessions</div>";
  if (st.error) {
    const b = document.createElement("div");
    b.className = "banner warn";
    b.textContent = "settings.json has invalid JSON — fix the file by hand; form editing is disabled. " + st.error;
    el.appendChild(b);
    return;
  }

  // hooks builder
  const hooksH = document.createElement("h2");
  hooksH.textContent = "hooks";
  el.appendChild(hooksH);
  const rows = hooksList(st.data || {});
  const hbar = document.createElement("div");
  hbar.className = "bar";
  const note = document.createElement("span");
  note.style.cssText = "align-self:center;font-size:.75rem;color:var(--fg2);flex:1;min-width:12rem";
  note.textContent = rows === null
    ? "hooks config has a non-standard shape — edit it as raw JSON in the schema list below"
    : "lifecycle commands: each receives the event JSON on stdin; test fires a sample event";
  hbar.appendChild(note);
  if (rows !== null) {
    const addb = document.createElement("button");
    addb.className = "small primary";
    addb.textContent = "+ add hook";
    addb.onclick = hookAdd;
    hbar.appendChild(addb);
  }
  el.appendChild(hbar);
  for (const row of rows || []) {
    const d = document.createElement("div");
    d.className = "drow";
    d.innerHTML =
      `<span class="badge group">${esc(row.event)}</span>` +
      (row.matcher ? `<span class="badge link">${esc(row.matcher)}</span>` : "") +
      `<span class="dmsg">${esc(row.command)}</span>` +
      (row.timeout ? `<span class="badge ok">${esc(String(row.timeout))}s</span>` : "");
    const tb = document.createElement("button");
    tb.className = "small";
    tb.textContent = "test";
    tb.onclick = () => hookFire(row);
    d.appendChild(tb);
    const db = document.createElement("button");
    db.className = "small danger";
    db.textContent = "delete";
    db.onclick = () => hookDelete(row);
    d.appendChild(db);
    el.appendChild(d);
  }

  // schema-driven settings with filter
  const fbar = document.createElement("div");
  fbar.className = "bar";
  fbar.style.marginTop = "1.2rem";
  const fin = document.createElement("input");
  fin.type = "search";
  fin.id = "setq";
  fin.placeholder = "filter settings…";
  fin.value = SFILTER.q;
  fin.oninput = () => {
    SFILTER.q = fin.value;
    renderSettings();
    const nf = document.getElementById("setq");
    nf.focus();
    nf.setSelectionRange(nf.value.length, nf.value.length);
  };
  fbar.appendChild(fin);
  const ob = document.createElement("button");
  ob.className = "small" + (SFILTER.set ? " primary" : "");
  ob.textContent = "only set";
  ob.title = "show only keys that are set in the file";
  ob.onclick = () => { SFILTER.set = !SFILTER.set; renderSettings(); };
  fbar.appendChild(ob);
  el.appendChild(fbar);

  const q = SFILTER.q.toLowerCase();
  const match = (s) =>
    (!q || s.key.toLowerCase().includes(q) || (s.desc || "").toLowerCase().includes(q)) &&
    (!SFILTER.set || settingsGet(s.key) !== undefined);
  const cats = {};
  for (const s of SCHEMA) if (match(s)) (cats[s.cat] = cats[s.cat] || []).push(s);
  for (const [cat, items] of Object.entries(cats)) {
    const h = document.createElement("h2");
    h.textContent = cat;
    el.appendChild(h);
    for (const s of items) el.appendChild(settingRow(s));
  }
  const covered = new Set(SCHEMA.map((s) => s.key.split(".")[0]));
  const extra = Object.keys(st.data || {})
    .filter((k) => !covered.has(k))
    .map((k) => ({ key: k, type: "json",
      desc: "(not in the documented schema — edited as raw JSON)" }))
    .filter(match);
  if (extra.length) {
    const h = document.createElement("h2");
    h.textContent = "other keys in file";
    el.appendChild(h);
    for (const s of extra) el.appendChild(settingRow(s));
  }
}

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
  const p = document.createElement("div");
  p.className = "mcppanel";
  p.innerHTML =
    `<div class="bar"><input type="text" id="mcpname" placeholder="server name"` +
    ` value="${esc(MCPEDIT.name || "")}" ${MCPEDIT.isNew ? "" : "disabled"}>` +
    (MCPEDIT.enabled === false ? '<span class="badge link">disabled</span>' : "") +
    `<span style="flex:1"></span>` +
    (MCPEDIT.isNew ? "" :
      `<button class="small danger" onclick="mcpDelete()">delete</button>`) +
    `</div>`;
  const ta = document.createElement("textarea");
  ta.id = "mcpjson";
  ta.className = "fedit";
  ta.rows = 12;
  ta.value = MCPEDIT.json;
  p.appendChild(ta);
  const bar = document.createElement("div");
  bar.className = "bar";
  bar.style.marginTop = ".6rem";
  bar.innerHTML =
    `<button class="primary" onclick="mcpSave()">save</button>` +
    `<button onclick="MCPEDIT=null;render()">cancel</button>`;
  p.appendChild(bar);
  return p;
}

async function mcpSave() {
  let config;
  try { config = JSON.parse(document.getElementById("mcpjson").value); }
  catch (e) { toast("invalid JSON: " + e.message, true); return; }
  const name = (document.getElementById("mcpname").value || "").trim();
  const enabled = MCPEDIT.enabled !== false;
  try {
    await api("/api/mcp-save", { name, config, enabled });
    toast(name + " saved" + (enabled ? "" : " (still disabled)") +
      " — applies to new sessions");
    MCPEDIT = null;
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function mcpDelete() {
  const enabled = MCPEDIT.enabled !== false;
  if (!(await mconfirm("delete " + MCPEDIT.name,
    enabled ? "Removes it from " + DATA.mcp.machine_path + "."
      : "Removes it from disabled/mcp-servers.json.", "delete"))) return;
  try {
    await api("/api/mcp-delete", { name: MCPEDIT.name, enabled });
    toast(MCPEDIT.name + " deleted");
    MCPEDIT = null;
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function mcpNew() {
  const r = await modal({ title: "add MCP server", fields: [
    { id: "n", label: "server name" },
    { id: "k", label: "transport", type: "select", options: [
      { value: "stdio", label: "stdio — local command" },
      { value: "http", label: "http/sse — remote URL" }] }], ok: "create" });
  if (!r || !r.n) return;
  MCPEDIT = { name: r.n, isNew: true,
    json: JSON.stringify(MCP_TEMPLATE[r.k], null, 2) };
  render();
}

function renderMcp() {
  const el = document.getElementById("mcpview");
  const st = DATA.mcp || { servers: [] };
  el.innerHTML =
    `<div class="sethead">user-scope MCP servers in <b>${esc(st.machine_path)}</b>` +
    ` — Claude Code's machine store${st.machine_exists ? "" : " (created on first save)"}.` +
    " Changes apply to new sessions.</div>";
  const machineOk = !st.machine_error;
  if (st.machine_error) {
    const b = document.createElement("div");
    b.className = "banner warn";
    b.textContent = "~/.claude.json has invalid JSON — editing disabled; fix the file by hand. " + st.machine_error;
    el.appendChild(b);
  }
  if (machineOk) {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.innerHTML =
      `<span style="flex:1"></span>` +
      `<button class="primary" onclick="mcpNew()">+ add server</button>`;
    el.appendChild(bar);
    if (MCPEDIT) el.appendChild(mcpEditPanel());
  }
  if (!st.servers.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "no MCP servers on this machine";
    el.appendChild(d);
    return;
  }
  for (const s of st.servers) {
    const row = document.createElement("div");
    row.className = "row" + (s.enabled ? "" : " off");
    row.innerHTML =
      `<span class="name">${esc(s.name)}</span>` +
      (s.enabled ? "" : '<span class="badge link">disabled</span>') +
      `<span class="desc">${esc(mcpSummary(s.config))}</span>`;
    const act = document.createElement("span");
    act.className = "actions";
    const btn = (label, fn, cls) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (cls) b.className = cls;
      b.onclick = fn;
      act.appendChild(b);
    };
    btn("test", () => mcpTest(s.name));
    if (machineOk)
      btn("edit", () => {
        MCPEDIT = { name: s.name, isNew: false, enabled: s.enabled,
          json: JSON.stringify(s.config, null, 2) };
        render();
      });
    if (machineOk)
      btn(s.enabled ? "disable" : "enable", () => mcpToggle(s.name, !s.enabled),
        "small" + (s.enabled ? " danger" : ""));
    row.appendChild(act);
    el.appendChild(row);
  }
}

async function mcpToggle(name, enabled) {
  try {
    await api("/api/mcp-toggle", { name, enabled });
    toast(name + (enabled ? " enabled" : " disabled — parked in disabled/mcp-servers.json") +
      " · applies to new sessions");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

let SETUP = null;

async function renderSetup(reload) {
  const el = document.getElementById("setupview");
  if (!SETUP || reload) {
    if (!SETUP) el.innerHTML = '<div class="empty">checking setup pieces…</div>';
    try { SETUP = await api("/api/setup"); }
    catch (e) { el.innerHTML = '<div class="banner warn">' + esc(e.message) + "</div>"; return; }
    if (TAB !== "setup") return;
  }
  el.innerHTML =
    '<div class="sethead">Installable pieces of environment setup. Applying a ' +
    'piece <b>patches your existing setup in place</b> — it never replaces your ' +
    'files. Whether a piece is installed is derived by looking, not recorded; ' +
    'removing touches only that piece’s own artifacts.</div>';
  for (const p of SETUP.pieces) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML =
      `<span class="name">${esc(p.label)}</span>` +
      (p.installed ? '<span class="badge ok">installed</span>'
                   : '<span class="badge link">not installed</span>') +
      `<span class="desc">${esc(p.desc)}${p.detail ? " — " + esc(p.detail) : ""}</span>`;
    const act = document.createElement("span");
    act.className = "actions";
    const btn = (label, fn, cls) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (cls) b.className = cls;
      b.onclick = fn;
      act.appendChild(b);
    };
    btn(p.installed ? "re-apply" : "apply", () => setupAct("apply", p),
        "small primary");
    if (p.removable && p.installed)
      btn("remove", () => setupAct("remove", p), "small danger");
    row.appendChild(act);
    el.appendChild(row);
  }
}

async function setupAct(action, p) {
  if (action === "remove" &&
      !(await mconfirm("remove " + p.label,
        "Removes only this piece's own artifacts (" + (p.target || "its files") +
        ") and clears the setting it set. Your own config is left as-is.", "remove")))
    return;
  try {
    await api("/api/setup-" + action, { id: p.id });
    toast(p.label + (action === "apply" ? " applied" : " removed") +
      " · applies to new sessions");
    await refresh();
    renderSetup(true);
  } catch (e) { toast(e.message, true); }
}

async function mcpTest(name) {
  toast("testing " + name + "…");
  try {
    const r = await api("/api/mcp-test", { name });
    toast(name + ": " + r.detail, !r.ok);
  } catch (e) { toast(e.message, true); }
}

let STL = null;
let STL_DRAG = null;

const STL_COLORS = { yellow: "var(--yellow)", blue: "var(--blue)", green: "var(--green)",
  aqua: "var(--aqua)", orange: "var(--orange)", gray: "var(--fg2)",
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
  return c && c.startsWith("#") ? c : STL_COLORS[c] || "var(--fg)";
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
  const chip = document.createElement("span");
  chip.className = "stlchip" + (STL.sel === id ? " sel" : "");
  chip.style.color = col;
  chip.style.borderColor = col;
  if (STL.bold[id]) chip.style.fontWeight = "bold";
  chip.title = a.desc + " (drag to move, click for colour & bold)";
  chip.draggable = true;
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
  chip.onclick = () => { STL.sel = STL.sel === id ? null : id; renderStatusline(); };
  const label = document.createElement("span");
  label.textContent = a.label;
  chip.appendChild(label);
  const smp = document.createElement("span");
  smp.className = "smp";
  smp.textContent = a.sample;
  chip.appendChild(smp);
  const x = document.createElement("span");
  x.className = "x";
  x.textContent = "×";
  x.title = "remove from the statusline";
  x.onclick = (e) => { e.stopPropagation(); stlRemove(id); };
  chip.appendChild(x);
  return chip;
}

function stlPalChip(id) {
  const a = stlAvail().get(id);
  const chip = document.createElement("span");
  chip.className = "stlchip pal";
  chip.style.color = stlColor(id);
  chip.title = a.sample + " — " + a.desc + " (click or drag onto a line to add)";
  chip.draggable = true;
  chip.ondragstart = (e) => {
    STL_DRAG = { src: "palette", id };
    e.dataTransfer.setData("text/plain", id);
    e.dataTransfer.effectAllowed = "move";
    chip.classList.add("dragging");
  };
  chip.ondragend = () => { STL_DRAG = null; chip.classList.remove("dragging"); };
  chip.onclick = () => {
    STL.palette = STL.palette.filter((x) => x !== id);
    STL.lines[STL.lines.length - 1].push(id);
    renderStatusline();
  };
  const label = document.createElement("span");
  label.textContent = a.label;
  chip.appendChild(label);
  return chip;
}

function stlColorPanel() {
  const id = STL.sel;
  const a = stlAvail().get(id);
  const box = document.createElement("div");
  box.className = "stlcolors";
  const cur = STL.colors[id] || null;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = "colour for " + a.label + ":";
  box.appendChild(who);
  for (const name of Object.keys(STL_COLORS)) {
    const b = document.createElement("button");
    b.className = "swatch" + (cur === name ? " on" : "");
    b.style.background = STL_COLORS[name];
    b.title = name;
    b.onclick = () => stlSetColor(id, name);
    box.appendChild(b);
  }
  const custom = document.createElement("input");
  custom.type = "color";
  custom.title = "custom colour (truecolor terminals)";
  if (cur && cur.startsWith("#")) { custom.value = cur; custom.classList.add("on"); }
  custom.oninput = () => stlSetColor(id, custom.value);
  box.appendChild(custom);
  const bold = document.createElement("button");
  bold.className = "small boldbtn" + (STL.bold[id] ? " on" : "");
  bold.textContent = "bold";
  bold.title = "toggle bold for this field";
  bold.onclick = () => {
    if (STL.bold[id]) delete STL.bold[id];
    else STL.bold[id] = true;
    renderStatusline();
  };
  box.appendChild(bold);
  const def = document.createElement("button");
  def.className = "small";
  def.textContent = cur ? "reset to default (" + a.color + ")" : "default (" + a.color + ")";
  def.disabled = !cur;
  def.onclick = () => stlSetColor(id, null);
  box.appendChild(def);
  const close = document.createElement("button");
  close.className = "small";
  close.textContent = "close";
  close.onclick = () => { STL.sel = null; renderStatusline(); };
  box.appendChild(close);
  return box;
}

function renderStatusline() {
  const el = document.getElementById("stlview");
  const st = DATA.statusline || {};
  if (!STL) stlInit();
  el.innerHTML = "";
  const head = document.createElement("div");
  head.className = "sethead";
  head.innerHTML =
    `generates <b>${esc(st.script_path || "")}</b>, linked into the config dir as ` +
    `<b>~/.claude/statusline.sh</b> and referenced from settings.json ` +
    (st.applied
      ? '<span style="color:var(--green)">✓ statusLine is set</span>'
      : '<span style="color:var(--orange)">— not set in settings.json yet; use save &amp; apply</span>') +
    `<br>no restart needed: Claude Code re-runs the script after every assistant ` +
    `message (and picks up settings.json edits on your next interaction); set a ` +
    `refresh interval below to also update while the session sits idle.`;
  el.appendChild(head);
  const term = document.createElement("div");
  term.className = "stlterm";
  term.innerHTML =
    '<div class="stltermhead"><span class="dot r"></span><span class="dot y"></span>' +
    '<span class="dot g"></span><span class="ttl">preview</span></div>';
  const wsel = document.createElement("span");
  wsel.className = "wsel";
  for (const w of [null, 120, 80]) {
    const b = document.createElement("button");
    b.className = "small" + (STL_WIDTH === w ? " on" : "");
    b.textContent = w ? w + " col" : "full";
    b.title = w ? "truncate the preview at ~" + w + " columns, like a narrow terminal"
      : "no truncation";
    b.onclick = () => { STL_WIDTH = w; renderStatusline(); };
    wsel.appendChild(b);
  }
  term.querySelector(".stltermhead").appendChild(wsel);
  const prev = document.createElement("div");
  prev.className = "stlpreview";
  if (STL_WIDTH) {
    prev.classList.add("trunc");
    prev.style.width = "calc(" + STL_WIDTH + "ch + 1.8rem)";
  }
  const raw = STL.separator;
  const sep = raw.trim()
    ? `<span class="psep">${esc(" " + raw.trim() + " ")}</span>` : esc(raw);
  const rendered = STL.lines
    .map((line) => line
      .map((id) => `<span style="color:${stlColor(id)}${STL.bold[id] ? ";font-weight:bold" : ""}">` +
                   `${esc(stlAvail().get(id).sample)}</span>`)
      .join(sep))
    .filter((l) => l.length);
  prev.innerHTML = rendered.length
    ? rendered.join("<br>")
    : '<span style="color:var(--bg2)">(no fields enabled — add some from the palette)</span>';
  term.appendChild(prev);
  el.appendChild(term);
  const bar = document.createElement("div");
  bar.className = "bar";
  bar.innerHTML =
    `<span style="align-self:center;font-size:.8rem;color:var(--fg2)"` +
    ` title="a visible separator automatically gets one space on each side; leave blank/spaces for space-only separation">separator</span>` +
    `<input type="text" id="stlsep" value="${esc(STL.separator)}" style="width:6rem"` +
    ` oninput="STL.separator=this.value" onchange="renderStatusline()">` +
    ["│", "·", "»"].map((ch) =>
      `<button class="small" title="separator preset"` +
      ` onclick="STL.separator='${ch}';renderStatusline()">${ch}</button>`).join("") +
    `<button class="small" title="plain spaces, no separator character"` +
    ` onclick="STL.separator='  ';renderStatusline()">space</button>` +
    `<span style="align-self:center;font-size:.8rem;color:var(--fg2)"` +
    ` title="Claude Code re-runs the script after each assistant message; a refresh interval also re-runs it every N seconds while idle, so edits show up without any interaction. 0 = only on updates.">` +
    `refresh every</span>` +
    `<input type="number" min="0" max="3600" value="${STL.refresh}" style="width:4.5rem"` +
    ` oninput="STL.refresh=Math.max(0,parseInt(this.value)||0)">` +
    `<span style="align-self:center;font-size:.8rem;color:var(--fg2)">s (0 = on updates only)</span>` +
    `<span style="flex:1"></span>` +
    `<button onclick="stlSave(false)">save</button>` +
    `<button class="primary" onclick="stlSave(true)">save &amp; apply</button>`;
  el.appendChild(bar);
  const grid = document.createElement("div");
  grid.className = "stlgrid";
  const build = document.createElement("div");
  build.className = "stlbuild";
  const presets = document.createElement("div");
  presets.className = "stlpresets";
  presets.append("presets:");
  for (const name of Object.keys(STL_PRESETS)) {
    const b = document.createElement("button");
    b.className = "small";
    b.textContent = name;
    b.title = "replace the current layout with the " + name + " preset " +
      "(colours and bold are kept)";
    b.onclick = () => stlPreset(name);
    presets.appendChild(b);
  }
  build.appendChild(presets);
  if (STL.sel && STL.lines.some((l) => l.includes(STL.sel))) build.appendChild(stlColorPanel());
  else STL.sel = null;
  STL.lines.forEach((line, li) => {
    const lineEl = document.createElement("div");
    lineEl.className = "stlline";
    lineEl.ondragover = (e) => { e.preventDefault(); lineEl.classList.add("drag"); };
    lineEl.ondragleave = () => lineEl.classList.remove("drag");
    lineEl.ondrop = (e) => { e.preventDefault(); stlDrop(li, null); };
    const lno = document.createElement("span");
    lno.className = "lno";
    lno.textContent = String(li + 1);
    lno.title = "line " + (li + 1);
    lineEl.appendChild(lno);
    line.forEach((id, fi) => lineEl.appendChild(stlChip(id, li, fi)));
    if (!line.length) {
      const ph = document.createElement("span");
      ph.className = "empty";
      ph.style.pointerEvents = "none";
      ph.textContent = "drop fields here";
      lineEl.appendChild(ph);
    }
    if (STL.lines.length > 1) {
      const del = document.createElement("button");
      del.className = "small ldel";
      del.textContent = "× line";
      del.title = "remove this line (its fields move to the line above)";
      del.onclick = () => {
        const dst = li > 0 ? li - 1 : 1;
        STL.lines[dst].push(...STL.lines[li]);
        STL.lines.splice(li, 1);
        renderStatusline();
      };
      lineEl.appendChild(del);
    }
    build.appendChild(lineEl);
  });
  const add = document.createElement("button");
  add.className = "small";
  add.textContent = "+ line";
  add.disabled = STL.lines.length >= STL_MAX_LINES;
  add.title = add.disabled
    ? "max " + STL_MAX_LINES + " lines (the config has three line breaks)"
    : "add a line — narrow terminals truncate long lines instead of wrapping";
  add.onclick = () => { STL.lines.push([]); renderStatusline(); };
  build.appendChild(add);
  grid.appendChild(build);
  const side = document.createElement("div");
  side.className = "stlside";
  side.innerHTML =
    `<h3>available fields</h3>` +
    `<div class="hint">click or drag onto a line · drag a chip back here to remove</div>`;
  side.ondragover = (e) => {
    if (STL_DRAG && STL_DRAG.src === "line") { e.preventDefault(); side.classList.add("drag"); }
  };
  side.ondragleave = () => side.classList.remove("drag");
  side.ondrop = (e) => {
    e.preventDefault();
    const d = STL_DRAG;
    STL_DRAG = null;
    if (!d || d.src !== "line") return;
    stlRemove(d.id);
  };
  if (!STL.palette.length) {
    const chips = document.createElement("div");
    chips.className = "stlchips";
    chips.innerHTML = '<span class="empty">all fields in use</span>';
    side.appendChild(chips);
  }
  const cats = new Map();
  for (const id of STL.palette) {
    const cat = (stlAvail().get(id) || {}).cat || "other";
    if (!cats.has(cat)) cats.set(cat, []);
    cats.get(cat).push(id);
  }
  for (const [cat, ids] of cats) {
    const h = document.createElement("h4");
    h.textContent = cat;
    side.appendChild(h);
    const chips = document.createElement("div");
    chips.className = "stlchips";
    for (const id of ids) chips.appendChild(stlPalChip(id));
    side.appendChild(chips);
  }
  grid.appendChild(side);
  el.appendChild(grid);
}

async function stlSave(apply) {
  try {
    await api("/api/statusline-save", {
      config: { separator: STL.separator, refresh: STL.refresh, fields: stlFields() },
      apply });
    toast(apply
      ? "statusline saved and statusLine set in settings.json — make sure statusline.sh and settings.json are linked"
      : "statusline script regenerated — a running Claude Code picks it up on its next update");
    STL = null;
    await refresh();
  } catch (e) { toast(e.message, true); }
}


let INSIGHT = null;
let DOCTOR = null;

const BUDGET_COLORS = { "CLAUDE.md": "var(--yellow)", skills: "var(--aqua)",
  commands: "var(--blue)", agents: "var(--purple)",
  "output-styles": "var(--orange)" };
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

async function renderInsight(rescan) {
  const el = document.getElementById("insightview");
  if (!INSIGHT || rescan) {
    el.innerHTML = '<div class="empty">estimating context cost and scanning session transcripts…</div>';
    try { INSIGHT = await api("/api/insight" + (rescan ? "?rescan" : "")); }
    catch (e) {
      el.innerHTML = '<div class="banner warn">' + esc(e.message) + "</div>";
      return;
    }
    if (TAB !== "insight") return;
  }
  const b = INSIGHT.budget, u = INSIGHT.usage;
  el.innerHTML = "";
  const head = document.createElement("div");
  head.className = "sethead";
  head.innerHTML =
    "what your config costs at the start of <i>every</i> session " +
    "(chars÷4 estimate — CLAUDE.md is injected wholesale, each active item " +
    "contributes its name + description), and what actually gets used " +
    "(session transcripts in <b>" + esc(u.dir) + "</b>, parsed locally)";
  el.appendChild(head);

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

  const tiles = document.createElement("div");
  tiles.className = "tiles";
  const tile = (num, lbl) =>
    `<div class="tile"><div class="tnum">${num}</div><div class="tlbl">${lbl}</div></div>`;
  tiles.innerHTML =
    tile(tokfmt(b.total), "tokens every session") +
    tile(tokfmt(b.claude_md), "CLAUDE.md") +
    tile(tokfmt((b.types.skills || {}).tokens || 0), "skill descriptions") +
    (u.available ? tile(u.sessions, "sessions scanned") : "") +
    (u.available && u.sessions ? tile(unused.length, "unused 90d+") : "");
  el.appendChild(tiles);

  const segs = [["CLAUDE.md", b.claude_md],
    ...Object.entries(b.types).map(([t, v]) => [t, v.tokens])];
  const bar = document.createElement("div");
  bar.className = "budgetbar";
  const key = document.createElement("div");
  key.className = "budgetkey";
  for (const [name, tok] of segs) {
    if (!tok) continue;
    const d = document.createElement("div");
    d.style.flex = String(tok);
    d.style.background = BUDGET_COLORS[name] || "var(--fg2)";
    d.title = name + ": ~" + tokfmt(tok) + " tokens";
    bar.appendChild(d);
    const k = document.createElement("span");
    k.innerHTML = `<span class="sw" style="background:${BUDGET_COLORS[name] || "var(--fg2)"}"></span>` +
      `${esc(name)} ~${tokfmt(tok)}`;
    key.appendChild(k);
  }
  el.appendChild(bar);
  el.appendChild(key);

  const table = (title, headers, rows) => {
    const h = document.createElement("h2");
    h.textContent = title;
    el.appendChild(h);
    const t = document.createElement("table");
    t.className = "itable";
    t.innerHTML = "<tr>" + headers.map((x) => `<th>${x}</th>`).join("") + "</tr>";
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = r;
      t.appendChild(tr);
    }
    el.appendChild(t);
  };

  const consumers = Object.entries(b.types)
    .flatMap(([t, v]) => v.items.map((it) => ({ type: t, ...it })))
    .sort((a, b2) => b2.tokens - a.tokens)
    .slice(0, 12);
  table("top context consumers", ["item", "type", "~tokens"],
    consumers.map((c) =>
      `<td>${esc(c.name)}</td><td class="dim">${esc(c.type)}</td>` +
      `<td class="num">${tokfmt(c.tokens)}</td>`));

  if (u.available && u.sessions) {
    const rows = [];
    for (const [kind, names] of Object.entries(used))
      for (const [name, rec] of Object.entries(names))
        rows.push({ kind, name, count: rec.count, last: rec.last });
    rows.sort((a, b2) => b2.count - a.count);
    table("most used (all transcripts)", ["name", "kind", "uses", "last used"],
      rows.slice(0, 15).map((r) =>
        `<td>${esc(r.name)}</td><td class="dim">${esc(r.kind)}</td>` +
        `<td class="num">${r.count}</td><td class="dim">${esc(relTime(r.last))}</td>`));
    if (unused.length)
      table("unused in 90+ days — archive candidates", ["name", "type", "last used"],
        unused.slice(0, 30).map((r) =>
          `<td>${esc(r.name)}</td><td class="dim">${esc(r.type)}</td>` +
          `<td class="dim">${r.last ? esc(relTime(new Date(r.last).toISOString())) : "never"}</td>`));
  } else if (!u.available) {
    const n = document.createElement("div");
    n.className = "empty";
    n.textContent = "no transcripts found at " + u.dir + " — usage analytics appear once Claude Code has recorded sessions on this machine";
    el.appendChild(n);
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
      .sort((a, b) => b[1] - a[1])
      .slice(0, 15);
    if (cand.length) {
      const h = document.createElement("h2");
      h.textContent = "permission advisor — frequent Bash commands with no allow rule";
      el.appendChild(h);
      const note = document.createElement("div");
      note.className = "sethead";
      note.textContent = "these ran repeatedly across your sessions; an allow rule in settings.json skips the permission prompt for them";
      el.appendChild(note);
      const t = document.createElement("table");
      t.className = "itable";
      t.innerHTML = "<tr><th>command</th><th>uses</th><th>proposed rule</th><th></th></tr>";
      for (const [prefix, n] of cand) {
        const rule = "Bash(" + prefix + ":*)";
        const tr = document.createElement("tr");
        tr.innerHTML =
          `<td>${esc(prefix)}</td><td class="num">${n}</td>` +
          `<td class="dim">${esc(rule)}</td>`;
        const td = document.createElement("td");
        const b = document.createElement("button");
        b.className = "small";
        b.textContent = "allow";
        b.onclick = async () => {
          try {
            await api("/api/settings-set", { key: "permissions.allow",
              value: [...allow, rule] });
            toast(rule + " added to permissions.allow");
            await refresh();
            INSIGHT = null;
            renderInsight();
          } catch (e) { toast(e.message, true); }
        };
        td.appendChild(b);
        tr.appendChild(td);
        t.appendChild(tr);
      }
      el.appendChild(t);
    }
  }

  const bar2 = document.createElement("div");
  bar2.className = "bar";
  const rb = document.createElement("button");
  rb.className = "small";
  rb.textContent = "rescan transcripts";
  rb.title = "drop the cache and re-read every transcript" +
    (u.scanned_now ? " (last run read " + u.scanned_now + " new file(s))" : "");
  rb.onclick = () => renderInsight(true);
  bar2.appendChild(rb);
  el.appendChild(bar2);
}

let COSTS = null;

function usd(n) {
  if (n >= 100) return "$" + Math.round(n);
  if (n >= 1) return "$" + n.toFixed(2);
  return "$" + n.toFixed(3);
}

async function renderCosts(rescan) {
  const el = document.getElementById("costsview");
  if (!COSTS || rescan) {
    el.innerHTML = '<div class="empty">reading transcripts and pricing usage…</div>';
    try { COSTS = await api("/api/costs" + (rescan ? "?rescan" : "")); }
    catch (e) {
      el.innerHTML = '<div class="banner warn">' + esc(e.message) + "</div>";
      return;
    }
    if (TAB !== "costs") return;
  }
  const c = COSTS;
  el.innerHTML = "";
  const head = document.createElement("div");
  head.className = "sethead";
  head.innerHTML =
    "estimated API-price cost of your Claude Code usage, computed locally from " +
    "the transcripts in <b>" + esc(c.dir) + "</b> (input/output/cache tokens × " +
    "list prices; cache writes at 1.25×, cache reads at 0.1×). On a Pro/Max " +
    "subscription this shows what the same usage <i>would</i> cost via the API.";
  el.appendChild(head);
  if (!c.available || !c.sessions) {
    el.innerHTML += '<div class="empty">no transcripts found — cost data appears once Claude Code has recorded sessions on this machine</div>';
    return;
  }
  const tiles = document.createElement("div");
  tiles.className = "tiles";
  const tile = (num, lbl) =>
    `<div class="tile"><div class="tnum">${num}</div><div class="tlbl">${lbl}</div></div>`;
  tiles.innerHTML =
    tile(usd(c.totals.today), "today") +
    tile(usd(c.totals.last7), "last 7 days") +
    tile(usd(c.totals.month), "month to date") +
    tile(usd(c.totals.all), "all time") +
    tile(usd(c.cache_savings), "saved by caching");
  el.appendChild(tiles);

  if (c.days.length) {
    const max = Math.max(...c.days.map((d) => d.cost), 0.0001);
    const chart = document.createElement("div");
    chart.className = "chart";
    for (const d of c.days) {
      const bar = document.createElement("div");
      bar.className = "cbar";
      bar.style.height = Math.max(2, (d.cost / max) * 100) + "%";
      bar.title = d.day + ": " + usd(d.cost) + "\n" +
        Object.entries(d.by).map(([m, v]) => m + ": " + usd(v)).join("\n");
      chart.appendChild(bar);
    }
    el.appendChild(chart);
    const key = document.createElement("div");
    key.className = "chartkey";
    key.innerHTML = `<span>${esc(c.days[0].day)}</span>` +
      `<span>daily cost, last ${c.days.length} active days</span>` +
      `<span>${esc(c.days[c.days.length - 1].day)}</span>`;
    el.appendChild(key);
  }

  const table = (title, headers, rows) => {
    const h = document.createElement("h2");
    h.textContent = title;
    el.appendChild(h);
    const t = document.createElement("table");
    t.className = "itable";
    t.innerHTML = "<tr>" + headers.map((x) => `<th>${x}</th>`).join("") + "</tr>";
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = r;
      t.appendChild(tr);
    }
    el.appendChild(t);
  };

  table("by model", ["model", "cost", "input", "output", "cache read", "msgs"],
    c.by_model.map((m) =>
      `<td>${esc(m.model)}</td><td class="num">${usd(m.cost)}</td>` +
      `<td class="dim">${tokfmt(m.in)}</td><td class="dim">${tokfmt(m.out)}</td>` +
      `<td class="dim">${tokfmt(m.cacheR)}</td><td class="dim">${m.msgs}</td>`));

  if (c.by_project.length > 1)
    table("by project", ["project", "cost", "assistant msgs"],
      c.by_project.map((p) =>
        `<td>${esc(p.cwd.replace(/^\/(home|Users)\/[^\/]+/, "~"))}</td>` +
        `<td class="num">${usd(p.cost)}</td><td class="dim">${p.msgs}</td>`));

  if (c.unknown_models.length) {
    const b = document.createElement("div");
    b.className = "banner warn";
    b.textContent = "no list price known for: " + c.unknown_models.join(", ") +
      " — priced at opus-tier; override via 'pricing' in .claude-ui.json";
    el.appendChild(b);
  }
  const bar = document.createElement("div");
  bar.className = "bar";
  const rb = document.createElement("button");
  rb.className = "small";
  rb.textContent = "rescan transcripts";
  rb.onclick = () => renderCosts(true);
  bar.appendChild(rb);
  el.appendChild(bar);
}

async function renderDoctor(rerun) {
  const el = document.getElementById("doctorview");
  if (!DOCTOR || rerun) {
    el.innerHTML = '<div class="empty">running checks…</div>';
    try { DOCTOR = await api("/api/doctor"); }
    catch (e) {
      el.innerHTML = '<div class="banner warn">' + esc(e.message) + "</div>";
      return;
    }
    if (TAB !== "doctor") return;
    renderTabs();
  }
  el.innerHTML = "";
  const head = document.createElement("div");
  head.className = "sethead";
  const warns = DOCTOR.warns;
  const infos = DOCTOR.findings.length - warns;
  head.innerHTML =
    (DOCTOR.findings.length
      ? `<b>${warns}</b> warning${warns === 1 ? "" : "s"}, ${infos} note${infos === 1 ? "" : "s"}`
      : '<span style="color:var(--green)">✓ nothing to report</span>') +
    ` · checked at ${esc(DOCTOR.ts)}`;
  el.appendChild(head);
  const bar = document.createElement("div");
  bar.className = "bar";
  const rb = document.createElement("button");
  rb.className = "small";
  rb.textContent = "run again";
  rb.onclick = () => renderDoctor(true);
  bar.appendChild(rb);
  el.appendChild(bar);
  for (const f of DOCTOR.findings) {
    const row = document.createElement("div");
    row.className = "drow";
    row.innerHTML =
      `<span class="badge ${f.level === "warn" ? "warn" : "link"}">${f.level}</span>` +
      `<span class="badge group">${esc(f.area)}</span>` +
      `<span class="dmsg">${esc(f.msg)}</span>`;
    el.appendChild(row);
  }
}

// ------------------------------------------------------------- command palette

let PAL = null;

function palItems() {
  const out = [];
  for (const t of allTabs())
    out.push({ kind: "go to", label: t,
      run: () => { TAB = t; location.hash = t; render(); } });
  for (const t of ITEM_TABS)
    for (const s of (DATA.items || {})[t] || [])
      out.push({ kind: t.replace(/s$/, ""), label: s.name,
        hint: (s.enabled ? "" : "(disabled) ") + (s.description || ""),
        run: () => s.broken
          ? (() => { TAB = t; location.hash = t; IQ = s.name; render(); })()
          : openItemEditor(t, s.name, null, s.enabled) });
  for (const id of ["CLAUDE.md", "settings.json", "keybindings.json"])
    out.push({ kind: "edit", label: id, run: () => openEditor(id) });
  out.push({ kind: "action", label: "add mcp server",
    run: () => { TAB = "mcp"; location.hash = TAB; render(); mcpNew(); } });
  out.push({ kind: "action", label: "toggle light/dark theme", run: toggleTheme });
  out.push({ kind: "action", label: "run doctor",
    run: () => { TAB = "doctor"; location.hash = TAB; render(); renderDoctor(true); } });
  out.push({ kind: "action", label: "setup pieces",
    run: () => { TAB = "setup"; location.hash = TAB; render(); renderSetup(true); } });
  out.push({ kind: "action", label: "rescan usage analytics",
    run: () => { TAB = "insight"; location.hash = TAB; render(); renderInsight(true); } });
  return out;
}

function fuzzy(q, s) {
  s = s.toLowerCase();
  let score = 0, i = 0;
  for (const ch of q) {
    const j = s.indexOf(ch, i);
    if (j < 0) return -1;
    score += (j === i ? 3 : 1) + (j === 0 ? 2 : 0);
    i = j + 1;
  }
  return score - s.length / 100;
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
  p.innerHTML = "";
}

function openPalette() {
  PAL = { q: "", sel: 0, items: palItems() };
  const p = document.getElementById("palette");
  p.hidden = false;
  p.innerHTML = "";
  const box = document.createElement("div");
  box.className = "palbox";
  const inp = document.createElement("input");
  inp.placeholder = "jump to anything — items, tabs, actions…";
  const listEl = document.createElement("div");
  listEl.className = "pallist";
  const renderList = () => {
    const list = palMatches();
    listEl.innerHTML = "";
    if (!list.length) {
      listEl.innerHTML = '<div class="palempty">no matches</div>';
      return;
    }
    list.forEach((it, i) => {
      const row = document.createElement("div");
      row.className = "palrow" + (i === PAL.sel ? " sel" : "");
      row.innerHTML =
        `<span class="pk">${esc(it.kind)}</span>` +
        `<span class="pl">${esc(it.label)}</span>` +
        `<span class="ph">${esc(it.hint || "")}</span>`;
      row.onclick = () => { closePalette(); it.run(); };
      listEl.appendChild(row);
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
  box.appendChild(inp);
  box.appendChild(listEl);
  p.onclick = (e) => { if (e.target === p) closePalette(); };
  p.appendChild(box);
  renderList();
  inp.focus();
}

let EDITING = null;

async function openEditor(id) {
  try {
    EDITING = await api("/api/file?id=" + encodeURIComponent(id));
    render();
  } catch (e) { toast(e.message, true); }
}

async function openItemEditor(type, name, file, enabled) {
  try {
    const q = "type=" + encodeURIComponent(type) + "&name=" + encodeURIComponent(name) +
      "&enabled=" + (enabled ? "1" : "0") +
      (file ? "&file=" + encodeURIComponent(file) : "");
    EDITING = { item: true, ...(await api("/api/item?" + q)) };
    render();
  } catch (e) { toast(e.message, true); }
}

// Minimal markdown renderer for the editor preview (headings, lists, code
// fences, inline code/bold/italic/links, blockquotes) — enough to sanity-check
// a SKILL.md without any dependency.
function md2html(src) {
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\*([^*]+)\*/g, "<i>$1</i>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  let html = "";
  let inCode = false, inList = null, para = [];
  const flushPara = () => {
    if (para.length) { html += "<p>" + para.map(inline).join(" ") + "</p>"; para = []; }
  };
  const closeList = () => {
    if (inList) { html += "</" + inList + ">"; inList = null; }
  };
  for (const line of src.split("\n")) {
    if (line.trim().startsWith("```")) {
      flushPara(); closeList();
      html += inCode ? "</code></pre>" : "<pre><code>";
      inCode = !inCode;
      continue;
    }
    if (inCode) { html += esc(line) + "\n"; continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara(); closeList();
      const n = m[1].length;
      html += `<h${n}>${inline(m[2])}</h${n}>`;
    } else if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flushPara(); closeList();
      html += "<hr>";
    } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      flushPara();
      if (inList !== "ul") { closeList(); html += "<ul>"; inList = "ul"; }
      html += "<li>" + inline(m[1]) + "</li>";
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      flushPara();
      if (inList !== "ol") { closeList(); html += "<ol>"; inList = "ol"; }
      html += "<li>" + inline(m[1]) + "</li>";
    } else if ((m = line.match(/^>\s?(.*)$/))) {
      flushPara(); closeList();
      html += "<blockquote>" + inline(m[1]) + "</blockquote>";
    } else if (!line.trim()) {
      flushPara(); closeList();
    } else {
      para.push(line);
    }
  }
  flushPara(); closeList();
  if (inCode) html += "</code></pre>";
  return html;
}

function edSync() {
  const ta = document.getElementById("fileeditor");
  if (ta) EDITING.content = ta.value;
}

async function edAssist() {
  edSync();
  const r = await modal({ title: "✨ ask claude",
    text: "runs `claude -p` locally with this file (uses your own Claude Code auth; can take a minute)",
    fields: [
      { id: "m", label: "task", type: "select", options: [
        { value: "improve", label: "improve — tighten description & triggers, return revised file" },
        { value: "review", label: "review — list concrete problems, no changes" },
        { value: "custom", label: "custom instruction…" }] },
      { id: "c", label: "custom instruction (for custom)",
        placeholder: "e.g. add a 'Use when' trigger list for CI debugging" }],
    ok: "run" });
  if (!r) return;
  toast("asking claude… (this can take a while)");
  try {
    const res = await api("/api/assist", { mode: r.m, custom: r.c,
      content: EDITING.content, path: EDITING.path });
    EDITING.assist = { text: res.result, replaces: res.replaces };
    render();
  } catch (e) { toast(e.message, true); }
}

function renderEditor() {
  const el = document.getElementById("editorview");
  const f = EDITING;
  el.innerHTML =
    `<div class="sethead">editing <b>${esc(f.path)}</b>` +
    (f.exists ? "" : " (new file — created on save)") +
    (f.item && !f.enabled ? " · this item is disabled" : "") +
    (f.id === "CLAUDE.md" || f.id === "settings.json" || f.item ? " · applies to new sessions" : "") +
    `</div>`;
  if (f.item && f.files && f.files.length > 1) {
    const tabs = document.createElement("div");
    tabs.className = "ftabs";
    for (const name of f.files) {
      const b = document.createElement("button");
      b.className = "small" + (name === f.file ? " on" : "");
      b.textContent = name;
      b.onclick = () => { edSync(); openItemEditor(f.type, f.name, name, f.enabled); };
      tabs.appendChild(b);
    }
    el.appendChild(tabs);
  }
  const isMd = (f.item ? f.file : f.path || "").endsWith(".md");
  if (f.preview && isMd) {
    const pv = document.createElement("div");
    pv.className = "mdprev";
    pv.innerHTML = md2html(f.content || "");
    el.appendChild(pv);
  } else {
    const ta = document.createElement("textarea");
    ta.id = "fileeditor";
    ta.rows = 24;
    ta.className = "fedit";
    ta.value = f.content;
    ta.oninput = () => { f.content = ta.value; };
    el.appendChild(ta);
  }
  if (f.assist) {
    const ap = document.createElement("div");
    ap.className = "assistout";
    ap.textContent = f.assist.text;
    el.appendChild(ap);
    const abar = document.createElement("div");
    abar.className = "bar";
    abar.style.marginTop = ".5rem";
    if (f.assist.replaces) {
      const use = document.createElement("button");
      use.className = "small primary";
      use.textContent = "use result";
      use.onclick = () => {
        f.content = f.assist.text;
        delete f.assist;
        render();
      };
      abar.appendChild(use);
    }
    const dis = document.createElement("button");
    dis.className = "small";
    dis.textContent = "dismiss";
    dis.onclick = () => { edSync(); delete f.assist; render(); };
    abar.appendChild(dis);
    el.appendChild(abar);
  }
  const bar = document.createElement("div");
  bar.className = "bar";
  bar.style.marginTop = ".75rem";
  const btn = (label, fn, cls, title) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (cls) b.className = cls;
    if (title) b.title = title;
    b.onclick = fn;
    bar.appendChild(b);
    return b;
  };
  btn("save", saveFile, "primary");
  if (isMd)
    btn(f.preview ? "edit" : "preview", () => {
      edSync();
      f.preview = !f.preview;
      render();
    }, f.preview ? "small on" : "small");
  btn("✨ assist", edAssist, "small", "ask Claude (via the claude CLI) to improve or review this file");
  btn("close", closeEditor);
  el.appendChild(bar);
}

async function saveFile() {
  edSync();
  try {
    if (EDITING.item) {
      await api("/api/item-save", { type: EDITING.type, name: EDITING.name,
        file: EDITING.file, content: EDITING.content, enabled: EDITING.enabled });
      if (EDITING.files && !EDITING.files.includes(EDITING.file))
        EDITING.files.push(EDITING.file);
    } else {
      await api("/api/file-save", { id: EDITING.id, content: EDITING.content });
    }
    toast(EDITING.path + " saved");
    EDITING.exists = true;
  } catch (e) { toast(e.message, true); }
}

function closeEditor() {
  EDITING = null;
  refresh();
}

async function toggleItem(type, name, enabled) {
  try {
    await api("/api/item-toggle", { type, name, enabled });
    toast(name + (enabled ? " enabled" : " disabled — moved to disabled/") +
      " · applies to new sessions");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

function itemBadges(s) {
  return (s.symlink && !s.broken ? '<span class="badge link">symlink</span>' : "") +
    (s.broken ? '<span class="badge warn">broken</span>' : "") +
    (s.incomplete && !s.broken ? '<span class="badge warn">no SKILL.md</span>' : "") +
    (s.todo ? '<span class="badge warn" title="leftover TODO placeholder inside">TODO</span>' : "") +
    (s.name_mismatch ? '<span class="badge warn" title="frontmatter name does not match the folder name">name≠dir</span>' : "") +
    (s.long_desc ? '<span class="badge warn" title="description over 1024 characters — may be truncated">long desc</span>' : "");
}

function renderInventory() {
  const el = document.getElementById("itemsview");
  const all = (DATA.items || {})[TAB] || [];
  const q = IQ.toLowerCase();
  const items = all.filter(
    (s) => !q || s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q));
  const on = items.filter((s) => s.enabled);
  const off = items.filter((s) => !s.enabled);
  el.innerHTML =
    `<div class="sethead">${TAB} in <b>${esc(DATA.config_dir)}/${TAB}</b>` +
    ` — everything real on this machine. Disabling moves an item to ` +
    `<b>disabled/${TAB}/</b>; nothing is deleted. Changes apply to new sessions.</div>`;
  const bar = document.createElement("div");
  bar.className = "bar";
  const inp = document.createElement("input");
  inp.type = "search";
  inp.id = "iq";
  inp.placeholder = "filter " + TAB + "…";
  inp.value = IQ;
  inp.oninput = () => {
    IQ = inp.value;
    renderInventory();
    const n = document.getElementById("iq");
    n.focus();
    n.setSelectionRange(n.value.length, n.value.length);
  };
  bar.appendChild(inp);
  el.appendChild(bar);

  const section = (list, label, enabled) => {
    if (!list.length && !(enabled && !all.length)) return;
    const h = document.createElement("h2");
    h.innerHTML = label + ` <span class="count">· ${list.length}</span>`;
    el.appendChild(h);
    if (!list.length) {
      const d = document.createElement("div");
      d.className = "empty";
      d.textContent = q ? "no matches" : "nothing here";
      el.appendChild(d);
      return;
    }
    for (const s of list) {
      const row = document.createElement("div");
      row.className = "row" + (enabled ? "" : " off");
      row.innerHTML =
        `<span class="name" title="${esc(s.path || "")}">${esc(s.name)}</span>` +
        itemBadges(s) +
        `<span class="desc">${esc(s.description || "")}</span>`;
      const act = document.createElement("span");
      act.className = "actions";
      if (!s.broken) {
        const eb = document.createElement("button");
        eb.textContent = "edit";
        eb.className = "small";
        eb.onclick = () => openItemEditor(TAB, s.name, null, enabled);
        act.appendChild(eb);
      }
      const b = document.createElement("button");
      b.textContent = enabled ? "disable" : "enable";
      b.className = "small" + (enabled ? " danger" : "");
      b.onclick = () => toggleItem(TAB, s.name, !enabled);
      act.appendChild(b);
      row.appendChild(act);
      el.appendChild(row);
    }
  };
  section(on, "enabled", true);
  section(off, "disabled", false);
}

function render() {
  closeMenu();
  renderHeader();
  renderTabs();
  const views = { settings: "settingsview", mcp: "mcpview", statusline: "stlview",
    setup: "setupview", insight: "insightview", costs: "costsview", doctor: "doctorview" };
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
  if (TAB === "setup") { renderSetup(); return; }
  if (TAB === "insight") { renderInsight(); return; }
  if (TAB === "costs") { renderCosts(); return; }
  if (TAB === "doctor") { renderDoctor(); return; }
}

async function refresh() {
  DATA = await api("/api/state");
  if (!TABS.includes(TAB)) TAB = "skills";
  render();
}

document.getElementById("themebtn").addEventListener("click", toggleTheme);

// Keyboard: Ctrl/Cmd+K palette, Escape closes editor/menu, 1-9 switch tabs.
document.addEventListener("keydown", (e) => {
  if (!document.getElementById("modal").hidden) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (PAL) closePalette();
    else openPalette();
    return;
  }
  if (PAL) return;  // the palette input handles its own keys
  const tag = e.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.key === "Escape") {
    closeMenu();
    if (EDITING) closeEditor();
  } else if (e.key >= "1" && e.key <= "9" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const t = allTabs()[+e.key - 1];
    if (t) { TAB = t; location.hash = t; render(); }
  }
});

refresh();
