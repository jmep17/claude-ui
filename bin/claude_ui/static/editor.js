/* ===========================================================================
   editor.js — the config editor.

   A textarea is still the input (it gets native undo, IME, spellcheck and
   accessibility for free); everything else is drawn around and behind it:

     .ed-pane
       <pre class="ed-hl">   tokenized copy of the text, sits underneath
       <textarea class="ed-input">   transparent text, visible caret, on top

   The two share their box metrics exactly — see the --ed-* custom properties
   in app.css — so glyphs line up with their highlighted twins. Line numbers
   are a ::before on each .ed-line inside the <pre>, which means they stay
   aligned through soft wrapping for free, with nothing to measure or sync.

   Loaded after ui.js and before app.js. Everything is global on purpose;
   there is no bundler here.
   =========================================================================== */

let EDITING = null;

// Transient view state for the open file. `view` survives across files.
const ED = {
  view: localStorage.getItem("claude-ui-edview") || "edit",  // edit|split|preview
  ta: null, hl: null, prev: null, strip: null,
  raf: 0, lintTimer: 0, marks: [], locate: null,
};

// Above this we stop tokenizing: rebuilding a highlight layer for a file this
// big on every keystroke costs more than the colour is worth. MAX_EDIT on the
// server is 2 MB, so this is reachable.
const ED_HL_MAX_BYTES = 200 * 1024;
const ED_HL_MAX_LINES = 5000;

const DESC_MAX = 1024;  // mirrors items.py's long_desc threshold

const confirmDiscard = () =>
  !EDITING || !EDITING.dirty
  || confirm("You have unsaved changes in " + EDITING.path + ". Discard them?");

/* ------------------------------------------------------------------ opening
   Every entry point funnels through here so the "unsaved changes" guard, the
   hash, and the jump-to-line behave the same however you arrived. */

function edMode(name) {
  const n = (name || "").toLowerCase();
  if (n.endsWith(".json")) return "json";
  if (n.endsWith(".md") || n.endsWith(".markdown")) return "md";
  if (n.endsWith(".sh") || n.endsWith(".bash") || n.endsWith(".zsh")) return "sh";
  return "text";
}

const edIsMd = () => EDITING && edMode(EDITING.item ? EDITING.file : EDITING.path) === "md";

/* Declining the unsaved-changes prompt has to put the hash back: the open may
   have been driven *by* the hash (a deep link, a back button), and leaving it
   pointing at a file we refused to open makes the URL lie about the screen. */
function edKeep() {
  edSyncHash();
  return false;
}

/* `root` names a registered project when the item is that project's own
   rather than one of yours. It is the only difference: the same endpoint, the
   same payload, so everything downstream — the file tabs, the conflict
   handling, the doctor strip — works on a project's skill without knowing it
   is one. It is kept on EDITING so the save can send it back. */
async function openItemEditor(type, name, file, enabled, locate, root) {
  if (EDITING && !confirmDiscard()) return edKeep();
  try {
    const q = "type=" + encodeURIComponent(type) + "&name=" + encodeURIComponent(name)
      + "&enabled=" + (enabled ? "1" : "0")
      + (file ? "&file=" + encodeURIComponent(file) : "")
      + (root ? "&root=" + encodeURIComponent(root) : "");
    EDITING = { item: true, root: root || null, ...(await api("/api/item?" + q)) };
    ED.locate = locate || null;
    edOpened();
  } catch (e) { toast(e.message, true); }
}

async function openPath(path, locate) {
  if (EDITING && !confirmDiscard()) return edKeep();
  try {
    const r = await api("/api/path?path=" + encodeURIComponent(path));
    // `abs` is what we send back on save; `path` is the ~-shortened label the
    // rest of the UI shows.
    EDITING = { item: false, ...r, abs: r.path, path: r.tilde };
    ED.locate = locate || null;
    edOpened();
  } catch (e) { toast(e.message, true); }
}

// The palette still opens config files by bare name.
const openEditor = (id) => openPath((DATA.config_dir || "~/.claude") + "/" + id);

function edOpened() {
  edSyncHash();
  // The editor replaces the whole view, so it starts at the top rather than
  // inheriting however far down the list you had scrolled to click Open.
  window.scrollTo(0, 0);
  render();
  edLoadFindings();
}

/* openTarget — the one thing a doctor finding (or any "there's a problem in
   this file" affordance) needs to call. */
function openTarget(t) {
  if (!t) return;
  const locate = { line: t.line || 0, find: t.find || "" };
  if (t.kind === "item")
    return openItemEditor(t.type, t.name, t.file || null, t.enabled !== false, locate);
  if (t.kind === "path") return openPath(t.path, locate);
  if (t.kind === "tab") {
    if (EDITING && !confirmDiscard()) return;
    EDITING = null;
    IQ = t.q || "";
    goTab(t.tab);
  }
}

function closeEditor() {
  if (!confirmDiscard()) return;
  EDITING = null;
  edSyncHash();
  refresh();
}

/* --------------------------------------------------------------- selecting
   Placing the caret is the whole point of a clickable finding, so it gets to
   be precise: a line number when the backend knew one, else the first literal
   match, else the first token of that literal (hook commands arrive as raw
   shell strings but sit in the JSON with their quotes escaped). */

function edOffsetOfLine(text, line) {
  let at = 0;
  for (let i = 1; i < line; i++) {
    const nl = text.indexOf("\n", at);
    if (nl < 0) return at;
    at = nl + 1;
  }
  return at;
}

function edResolveLocate(text, locate) {
  if (!locate) return -1;
  if (locate.line > 0) return edOffsetOfLine(text, locate.line);
  const f = locate.find;
  if (!f) return -1;
  let i = text.indexOf(f);
  if (i < 0 && f.includes(" ")) i = text.indexOf(f.split(/\s+/)[0]);
  return i;
}

function edGoto(offset, len) {
  const ta = ED.ta;
  if (!ta || offset < 0) return;
  const v = ta.value;
  const end = v.indexOf("\n", offset);
  // Both focus() and setSelectionRange() scroll the textarea into view, and
  // that scrolls the window too — which throws the page around when all we
  // wanted was to move the caret. Pin the page and put it back.
  const pageY = window.scrollY;
  ta.focus({ preventScroll: true });
  ta.setSelectionRange(offset, len != null ? offset + len : (end < 0 ? v.length : end));
  window.scrollTo(0, pageY);
  // put the target near the middle rather than flush against the top edge
  const line = v.slice(0, offset).split("\n").length;
  const lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.scrollTop = Math.max(0, (line - 1) * lh - ta.clientHeight / 2);
  edSyncScroll();
}

// The textarea is authoritative while it exists; EDITING.content is the
// snapshot everything else (preview, lint, save) reads.
const text0 = () => (ED.ta ? ED.ta.value : (EDITING && EDITING.content) || "");

/* ------------------------------------------------------------------ saving */

async function saveFile() {
  if (!EDITING || EDITING.readonly) return;
  edSync();
  const wasNew = !EDITING.exists;
  try {
    const res = EDITING.item
      ? await api("/api/item-save", {
          type: EDITING.type, name: EDITING.name, file: EDITING.file,
          content: EDITING.content, enabled: EDITING.enabled,
          base: EDITING.mtime, root: EDITING.root })
      : await api("/api/path-save", {
          path: EDITING.abs || EDITING.path, content: EDITING.content,
          base: EDITING.mtime });
    if (EDITING.item && EDITING.files && !EDITING.files.includes(EDITING.file))
      EDITING.files.push(EDITING.file);
    toast(EDITING.path + " saved");
    EDITING.exists = true;
    EDITING.dirty = false;
    EDITING.mtime = res.mtime;          // the new base for the next save
    renderEditor();
    edLoadFindings(true);               // re-run the doctor: did we fix it?
    if (wasNew) refreshQuiet();
  } catch (e) {
    if (/changed on disk/.test(e.message)) edConflict(e.message);
    else toast(e.message, true);
  }
}

/* A 409 means someone else wrote the file while it was open. Offer the only
   two honest choices; never pick for them. */
async function edConflict(msg) {
  const overwrite = await modal({
    title: "This file changed on disk",
    text: msg + " Overwriting drops their version; reloading drops yours, but "
      + "copies it to the clipboard first so nothing is actually lost.",
    ok: "Overwrite theirs", cancel: "Reload from disk", danger: true,
  });
  if (overwrite !== null) {
    EDITING.mtime = null;               // no base => skip the conflict check
    saveFile();
    return;
  }
  const mine = EDITING.content;
  await copyText(mine, "your version to the clipboard");
  EDITING.dirty = false;
  if (EDITING.item)
    await openItemEditor(EDITING.type, EDITING.name, EDITING.file,
                         EDITING.enabled, null, EDITING.root);
  else
    await openPath(EDITING.abs || EDITING.path);
  // The file provably changed, so the cached findings describe a version that
  // no longer exists.
  edLoadFindings(true);
}

async function refreshQuiet() {
  try { DATA = await api("/api/state"); renderTabs(); } catch (e) { /* not fatal */ }
}

function edSync() {
  if (ED.ta && EDITING) EDITING.content = ED.ta.value;
}

/* ================================================================ tokenizer
   Regex per line, deliberately approximate. This is a reading aid, not a
   parser: a wrong colour costs nothing, and every branch stays cheap enough
   to re-run the visible file on a keystroke. */

const T = (cls, s) => '<span class="t-' + cls + '">' + s + "</span>";

function tokJson(line) {
  let out = "", last = 0;
  const re = /"(?:\\.|[^"\\])*"(\s*:)?|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\b(?:true|false|null)\b|[{}[\],:]/g;
  let m;
  while ((m = re.exec(line))) {
    out += esc(line.slice(last, m.index));
    const s = m[0];
    if (s[0] === '"') out += T(m[1] ? "key" : "str", esc(s));
    else if (/^[-\d]/.test(s)) out += T("num", esc(s));
    else if (/^[a-z]/.test(s)) out += T("lit", esc(s));
    else out += T("punc", esc(s));
    last = m.index + s.length;
  }
  return out + esc(line.slice(last));
}

function tokSh(line) {
  const h = line.match(/^(\s*)(#.*)$/);
  if (h) return esc(h[1]) + T("cmt", esc(h[2]));
  let out = "", last = 0;
  const re = /"(?:\\.|[^"\\])*"|'[^']*'|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|#.*$/g;
  let m;
  while ((m = re.exec(line))) {
    out += esc(line.slice(last, m.index));
    const s = m[0];
    out += T(s[0] === "#" ? "cmt" : s[0] === "$" ? "var" : "str", esc(s));
    last = m.index + s.length;
  }
  return out + esc(line.slice(last));
}

/* Markdown inline runs, applied to already-escaped text. Code spans win over
   emphasis (that is CommonMark's rule and also what people expect when they
   write `**` inside backticks). */
function mdInline(escaped) {
  return escaped
    .replace(/`[^`]+`/g, (s) => T("code", s))
    .replace(/(\*\*|__)(?=\S)([^*_]+?)\1/g, (s) => T("bold", s))
    .replace(/(?<![*\w])(\*|_)(?=\S)([^*_]+?)\1(?![*\w])/g, (s) => T("em", s))
    .replace(/\[[^\]]*\]\([^)\s]*\)/g, (s) => T("link", s));
}

function tokMd(line, st) {
  if (st.fence) {
    if (/^\s*```/.test(line)) { st.fence = false; return T("fence", esc(line)); }
    return T("code", esc(line));
  }
  if (st.fm) {
    if (line.trim() === "---") { st.fm = false; return T("fence", esc(line)); }
    const m = line.match(/^([A-Za-z0-9_-]+)(:)(.*)$/);
    return m ? T("key", esc(m[1])) + T("punc", ":") + T("str", esc(m[3]))
             : T("str", esc(line));
  }
  if (/^\s*```/.test(line)) { st.fence = true; return T("fence", esc(line)); }
  let m;
  if ((m = line.match(/^(#{1,6}\s+)(.*)$/)))
    return T("head", esc(m[1]) + mdInline(esc(m[2])));
  if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) return T("fence", esc(line));
  if ((m = line.match(/^(\s*)([-*+]|\d+\.)(\s+)(.*)$/)))
    return esc(m[1]) + T("mark", esc(m[2])) + esc(m[3]) + mdInline(esc(m[4]));
  if ((m = line.match(/^(\s*>+\s?)(.*)$/)))
    return T("mark", esc(m[1])) + T("quote", mdInline(esc(m[2])));
  return mdInline(esc(line));
}

function edTokenize(text, mode) {
  const lines = text.split("\n");
  const opensFm = mode === "md" && (lines[0] || "").trim() === "---";
  const st = { fence: false, fm: opensFm };
  const marks = ED.marks;
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let html;
    if (mode === "json") html = tokJson(line);
    else if (mode === "sh") html = tokSh(line);
    // the opening --- is the fence itself, not a line inside the block
    else if (mode === "md") html = (i === 0 && opensFm)
      ? T("fence", esc(line)) : tokMd(line, st);
    else html = esc(line);
    const mark = marks.find((k) => k.line === i + 1);
    out.push('<div class="ed-line' + (mark ? " ed-mark ed-mark-" + mark.level : "")
      + '" data-n="' + (i + 1) + '">' + (html || "&#8203;") + "</div>");
  }
  return out.join("");
}

/* ================================================================== linting
   Two sources feed one strip. Local lint runs on every idle keystroke and
   needs no server; doctor findings arrive once and describe the file as it
   was on disk. Both are shown, both jump. */

function edFrontmatter(text) {
  const lines = text.split("\n");
  if (!lines.length || lines[0].trim() !== "---") return null;
  const meta = {}, order = [];
  let key = null, buf = [], endLine = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === "---") { endLine = i; break; }
    const m = lines[i].match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (m) {
      if (key && buf.length) meta[key] = buf.join(" ").trim();
      const v = m[2].trim();
      if ([">", "|", ">-", "|-"].includes(v)) { key = m[1]; buf = []; }
      else { meta[m[1]] = v; key = null; buf = []; }
      if (!order.includes(m[1])) order.push(m[1]);
      meta["@line:" + m[1]] = i + 1;
    } else if (key && /^[ \t]/.test(lines[i])) buf.push(lines[i].trim());
  }
  if (key && buf.length) meta[key] = buf.join(" ").trim();
  return endLine < 0 ? null : { meta, order, endLine: endLine + 1 };
}

function jsonErrLine(text, err) {
  let m = /line (\d+)/i.exec(err.message);
  if (m) return +m[1];
  m = /position (\d+)/i.exec(err.message);
  if (m) return text.slice(0, +m[1]).split("\n").length;
  return 1;
}

function edLocalLint() {
  const text = text0();
  const mode = edMode(EDITING.item ? EDITING.file : EDITING.path);
  const out = [];
  if (mode === "json" && text.trim()) {
    try { JSON.parse(text); }
    catch (e) {
      out.push({ level: "warn", source: "lint", line: jsonErrLine(text, e),
        msg: "invalid JSON — " + e.message });
    }
  }
  if (mode === "md") {
    const fm = edFrontmatter(text);
    if (fm) {
      const d = fm.meta.description || "";
      if (d.length > DESC_MAX)
        out.push({ level: "warn", source: "lint", line: fm.meta["@line:description"],
          msg: "description is " + d.length + " characters — over the "
            + DESC_MAX + " limit, so it may be truncated" });
      // Same suppression the doctor uses: an unfinished file (one still
      // carrying a TODO) doesn't need to be told its description is thin.
      if (EDITING.item && EDITING.type === "skills" && d
          && !/use when/i.test(d) && !text.includes("TODO"))
        out.push({ level: "info", source: "lint", line: fm.meta["@line:description"],
          msg: 'description has no "Use when …" trigger — Claude may not know '
            + "when to load this skill" });
      if (EDITING.item && fm.meta.name && fm.meta.name !== EDITING.name)
        out.push({ level: "info", source: "lint", line: fm.meta["@line:name"],
          msg: "frontmatter name is “" + fm.meta.name + "” but the item is “"
            + EDITING.name + "”" });
    }
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++)
      if (lines[i].includes("TODO")) {
        out.push({ level: "info", source: "lint", line: i + 1,
          msg: "leftover TODO placeholder" });
        break;
      }
  }
  return out;
}

/* Doctor findings whose target is this exact file. */
function edDoctorFindings() {
  if (!DOCTOR || !EDITING) return [];
  // The doctor only ever inspects the config dir, and its item targets carry
  // a type and a name — which a project's own skill can match exactly while
  // being a different file. Nothing here belongs to one of those.
  if (EDITING.root) return [];
  return DOCTOR.findings.filter((f) => {
    const t = f.target;
    if (!t) return false;
    if (t.kind === "item")
      return EDITING.item && t.type === EDITING.type && t.name === EDITING.name
        && (!t.file || t.file === EDITING.file);
    if (t.kind === "path")
      return !EDITING.item && (t.path === EDITING.abs || t.path === EDITING.path);
    return false;
  }).map((f) => ({
    level: f.level, source: "doctor", msg: f.msg,
    line: f.target.line || 0, find: f.target.find || "",
  }));
}

/* Fetch the doctor once, lazily, and never block opening a file on it. */
async function edLoadFindings(force) {
  if (DOCTOR && !force) { edRefreshFindings(); return; }
  try {
    DOCTOR = await api("/api/doctor");
    renderTabs();
  } catch (e) { return; }
  if (EDITING) edRefreshFindings();
}

function edFindings() {
  const text = text0();
  return [...edDoctorFindings(), ...edLocalLint()].map((f) => {
    // resolve a `find` hint into a line so the gutter can mark it
    if (!f.line && f.find) {
      const at = edResolveLocate(text, { line: 0, find: f.find });
      if (at >= 0) f.line = text.slice(0, at).split("\n").length;
    }
    return f;
  }).filter((f, i, a) =>
    // The doctor and the local lint spot many of the same things, but word
    // them differently — the doctor prefixes the item name because its list
    // spans every file ("pdf-tools: leftover TODO placeholder" vs "leftover
    // TODO placeholder"). Same line and one message contained in the other
    // means one finding; keep the first, which is always the doctor's.
    a.findIndex((g) => g.line === f.line
      && (g.msg === f.msg || g.msg.endsWith(f.msg) || f.msg.endsWith(g.msg))) === i);
}

/* Recompute findings, repaint the strip and the gutter marks. Cheap enough to
   run on an idle keystroke; never rebuilds the pane. */
function edRefreshFindings() {
  if (!EDITING || !ED.strip) return;
  const finds = edFindings();
  ED.marks = finds.filter((f) => f.line > 0)
    .map((f) => ({ line: f.line, level: f.level }));
  ED.strip.innerHTML = "";
  ED.strip.hidden = !finds.length;
  for (const f of finds) {
    // A finding whose line we couldn't place (the text it named is gone, or
    // it describes the item rather than a spot in it) is still worth showing,
    // but must not look clickable when clicking would do nothing.
    const jumpable = f.line > 0;
    const row = el("button.ed-find", {
      class: "ed-find-" + f.level + (jumpable ? "" : " ed-find-flat"),
      disabled: !jumpable,
      title: jumpable ? "Jump to line " + f.line : "",
      onclick: jumpable ? () => edGoto(edOffsetOfLine(text0(), f.line)) : null,
    }, el("span.ed-find-dot"),
       el("span.ed-find-msg", { text: f.msg }),
       jumpable ? el("span.ed-find-line", { text: ":" + f.line }) : null,
       el("span.badge.badge-outline.ed-find-src", { text: f.source }));
    ED.strip.append(row);
  }
  edPaint();
}

/* ================================================================== preview
   md2html — a small markdown renderer, kept because the alternative is a
   dependency and this file has none. Everything is escaped before any markup
   is added, on every path. */

function mdInlineHtml(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, "<em>[image: $1]</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/(\*\*|__)(?=\S)([\s\S]+?)\1/g, "<b>$2</b>")
    .replace(/(?<![*\w])\*(?=\S)([^*]+?)\*(?![*\w])/g, "<i>$1</i>")
    .replace(/~~(?=\S)([\s\S]+?)~~/g, "<s>$1</s>");
}

/* The frontmatter card. This is the highest-leverage text in a skill file —
   it is the only thing Claude sees before deciding whether to load it — and
   as raw YAML at the top of a textarea it may as well be invisible. */
function mdFrontmatterCard(fm) {
  const d = fm.meta.description || "";
  const over = d.length > DESC_MAX;
  const rows = fm.order.filter((k) => k !== "description").map((k) =>
    '<div class="fm-row"><span class="fm-key">' + esc(k) + "</span>"
    + '<span class="fm-val">' + esc(fm.meta[k] || "") + "</span></div>").join("");
  return '<div class="fm-card">'
    + (fm.meta.name ? '<div class="fm-name">' + esc(fm.meta.name) + "</div>" : "")
    + (d ? '<div class="fm-desc">' + esc(d) + "</div>"
         + '<div class="fm-count' + (over ? " over" : "") + '">'
         + d.length + " / " + DESC_MAX + " characters"
         + (over ? " — over the limit, may be truncated" : "") + "</div>"
       : '<div class="fm-count over">no description — Claude has nothing to '
         + "match against</div>")
    + rows + "</div>";
}

function mdTableRow(line) {
  return line.replace(/^\s*\|?|\|?\s*$/g, "").split("|").map((c) => c.trim());
}

function md2html(src) {
  const lines = String(src == null ? "" : src).split("\n");
  let html = "", i = 0;

  const fm = edFrontmatter(src || "");
  if (fm) { html += mdFrontmatterCard(fm); i = fm.endLine + 1; }

  const stack = [];  // open lists: {tag, indent}
  const closeLists = (toIndent) => {
    while (stack.length && (toIndent == null || stack[stack.length - 1].indent >= toIndent))
      html += "</li></" + stack.pop().tag + ">";
  };

  for (; i < lines.length; i++) {
    const line = lines[i];

    if (/^\s*```/.test(line)) {
      closeLists();
      const lang = line.replace(/^\s*```/, "").trim();
      const body = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) body.push(lines[i]);
      html += '<pre data-lang="' + esc(lang) + '"><code>' + esc(body.join("\n"))
        + "</code></pre>";
      continue;
    }

    // table: a header row followed by a |---|---| separator
    if (line.includes("|") && /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1] || "")
        && lines[i + 1].includes("-")) {
      closeLists();
      const head = mdTableRow(line);
      const align = mdTableRow(lines[i + 1]).map((c) =>
        c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : "left");
      html += "<table><thead><tr>" + head.map((c, n) =>
        '<th style="text-align:' + align[n] + '">' + mdInlineHtml(c) + "</th>").join("")
        + "</tr></thead><tbody>";
      i += 2;
      for (; i < lines.length && lines[i].includes("|"); i++)
        html += "<tr>" + mdTableRow(lines[i]).map((c, n) =>
          '<td style="text-align:' + (align[n] || "left") + '">' + mdInlineHtml(c)
          + "</td>").join("") + "</tr>";
      i--;
      html += "</tbody></table>";
      continue;
    }

    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      closeLists();
      html += "<h" + m[1].length + ">" + mdInlineHtml(m[2]) + "</h" + m[1].length + ">";
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      closeLists();
      html += "<hr>";
    } else if ((m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
      const indent = m[1].replace(/\t/g, "  ").length;
      const tag = /\d/.test(m[2]) ? "ol" : "ul";
      while (stack.length && stack[stack.length - 1].indent > indent)
        html += "</li></" + stack.pop().tag + ">";
      const top = stack[stack.length - 1];
      if (!top || top.indent < indent) {
        html += "<" + tag + "><li>";
        stack.push({ tag, indent });
      } else {
        if (top.tag !== tag) {
          html += "</li></" + stack.pop().tag + "><" + tag + "><li>";
          stack.push({ tag, indent });
        } else html += "</li><li>";
      }
      let body = m[3];
      const task = body.match(/^\[([ xX])\]\s+(.*)$/);
      if (task)
        html += '<input type="checkbox" disabled' + (task[1] === " " ? "" : " checked")
          + "> " + mdInlineHtml(task[2]);
      else html += mdInlineHtml(body);
    } else if ((m = line.match(/^\s*>\s?(.*)$/))) {
      closeLists();
      html += "<blockquote>" + mdInlineHtml(m[1]) + "</blockquote>";
    } else if (!line.trim()) {
      closeLists();
    } else if (stack.length) {
      html += " " + mdInlineHtml(line.trim());   // lazy continuation of a list item
    } else {
      const para = [line];
      for (; i + 1 < lines.length && lines[i + 1].trim()
             && !/^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```)/.test(lines[i + 1]); i++)
        para.push(lines[i + 1]);
      html += "<p>" + para.map(mdInlineHtml).join(" ") + "</p>";
    }
  }
  closeLists();
  return html;
}

/* ================================================================ formatting
   Two different jobs behind one button. JSON gets real reformatting because
   there is a canonical form; markdown gets tidying only — reflowing someone's
   prose is not formatting, it's editing. */

function mdTidy(src) {
  const lines = src.split("\n");
  const out = [];
  let fence = false, blanks = 0;
  for (const line of lines) {
    if (/^\s*```/.test(line)) fence = !fence;
    if (fence || /^\s*```/.test(line)) { out.push(line); blanks = 0; continue; }
    const t = line.replace(/[ \t]+$/, "");
    if (!t.trim()) { if (++blanks > 1) continue; out.push(""); continue; }
    blanks = 0;
    out.push(t.replace(/^(\s*)[*+](\s+)/, "$1-$2"));
  }
  while (out.length && !out[out.length - 1]) out.pop();
  return out.join("\n") + "\n";
}

function edFormat() {
  edSync();
  const mode = edMode(EDITING.item ? EDITING.file : EDITING.path);
  const src = text0();
  let next;
  if (mode === "json") {
    if (!src.trim()) return;
    try { next = JSON.stringify(JSON.parse(src), null, 2) + "\n"; }
    catch (e) {
      const line = jsonErrLine(src, e);
      toast({ title: "Can't format — invalid JSON", variant: "error",
        description: e.message + " (line " + line + ")" });
      edGoto(edOffsetOfLine(src, line));
      edRefreshFindings();
      return;
    }
  } else if (mode === "md") {
    next = mdTidy(src);
  } else return;

  if (next === src) { toast("Already formatted"); return; }
  edReplaceAll(next);
  toast(mode === "json" ? "Reformatted with 2-space indent" : "Tidied whitespace");
}

/* ================================================================== editing
   execCommand is deprecated and still the only way to write into a textarea
   without destroying the browser's native undo stack — which matters more
   here than the deprecation does. setRangeText is the fallback. */

function edInsert(text, selStart, selLen) {
  const ta = ED.ta;
  // A read-only file has no Save button, so letting anything write into the
  // buffer would only manufacture edits with nowhere to go. setRangeText
  // ignores the readOnly attribute, so the guard has to live here.
  if (!ta || (EDITING && EDITING.readonly)) return;
  ta.focus();
  let ok = false;
  try { ok = document.execCommand("insertText", false, text); } catch (e) { ok = false; }
  if (!ok) {
    const s = ta.selectionStart, e = ta.selectionEnd;
    ta.setRangeText(text, s, e, "end");
  }
  if (selStart != null) {
    const base = ta.selectionStart - text.length;
    ta.setSelectionRange(base + selStart, base + selStart + (selLen || 0));
  }
  edChanged();
}

function edReplaceAll(next) {
  const ta = ED.ta;
  if (!ta) { EDITING.content = next; renderEditor(); return; }
  ta.focus();
  ta.setSelectionRange(0, ta.value.length);
  edInsert(next);
  ta.setSelectionRange(0, 0);
}

/* Wrap the selection in a marker, or unwrap it if it is already wrapped. */
function edWrap(before, after, placeholder) {
  const ta = ED.ta;
  if (!ta) return;
  const s = ta.selectionStart, e = ta.selectionEnd;
  const sel = ta.value.slice(s, e);
  after = after == null ? before : after;

  // Leave the bare text selected after unwrapping, so pressing the same key
  // again re-wraps it instead of doing nothing.
  if (sel.startsWith(before) && sel.endsWith(after)
      && sel.length >= before.length + after.length) {
    const bare = sel.slice(before.length, sel.length - after.length);
    edInsert(bare, 0, bare.length);
    return;
  }
  const out = ta.value.slice(s - before.length, s) === before
    && ta.value.slice(e, e + after.length) === after;
  if (out) {  // markers sit just outside the selection — take them off
    ta.setSelectionRange(s - before.length, e + after.length);
    edInsert(sel, 0, sel.length);
    return;
  }
  const body = sel || placeholder || "";
  edInsert(before + body + after, before.length, body.length);
}

/* Apply a line operation to every line the selection touches. If every line
   already has the marker the whole block loses it, so the button toggles
   rather than stacking "- - - item". Blank lines are left alone. */
function edLineOp(test, addFn, removeFn) {
  const ta = ED.ta;
  if (!ta) return;
  const v = ta.value;
  const s = v.lastIndexOf("\n", ta.selectionStart - 1) + 1;
  let e = v.indexOf("\n", ta.selectionEnd);
  if (e < 0) e = v.length;
  const lines = v.slice(s, e).split("\n");
  const body = lines.filter((l) => l.trim());
  const all = body.length > 0 && body.every(test);
  const next = lines
    .map((l, i) => (!l.trim() ? l : all ? removeFn(l) : addFn(l, i)))
    .join("\n");
  if (next === v.slice(s, e)) return;
  ta.setSelectionRange(s, e);
  edInsert(next);
  ta.setSelectionRange(s, s + next.length);
}

const edActions = {
  bold: () => edWrap("**", "**", "bold text"),
  italic: () => edWrap("*", "*", "italic text"),
  code: () => edWrap("`", "`", "code"),
  link: () => {
    const ta = ED.ta;
    const sel = ta ? ta.value.slice(ta.selectionStart, ta.selectionEnd) : "";
    if (sel) edInsert("[" + sel + "](url)", sel.length + 3, 3);
    else edInsert("[text](url)", 1, 4);
  },
  heading: () => edLineOp(
    (l) => /^#{1,6}\s+/.test(l),
    (l) => "## " + l,
    (l) => l.replace(/^#{1,6}\s+/, "")),
  bullet: () => edLineOp(
    (l) => /^\s*[-*+]\s+/.test(l),
    (l) => "- " + l,
    (l) => l.replace(/^(\s*)[-*+]\s+/, "$1")),
  number: () => edLineOp(
    (l) => /^\s*\d+[.)]\s+/.test(l),
    (l, i) => i + 1 + ". " + l,
    (l) => l.replace(/^(\s*)\d+[.)]\s+/, "$1")),
  quote: () => edLineOp(
    (l) => /^\s*>\s?/.test(l),
    (l) => "> " + l,
    (l) => l.replace(/^(\s*)>\s?/, "$1")),
};

/* ==================================================================== paint
   edPaint rebuilds only the highlight layer; the pane and the textarea are
   never re-created while you type. renderEditor() builds the chrome. */

function edPaint() {
  if (!ED.hl || !ED.ta || !EDITING) return;
  const text = ED.ta.value;
  if (ED.plain) { ED.hl.innerHTML = ""; return; }
  ED.hl.innerHTML = edTokenize(text, edMode(EDITING.item ? EDITING.file : EDITING.path));
  edSyncScroll();
}

function edSyncScroll() {
  if (ED.hl && ED.ta) {
    ED.hl.scrollTop = ED.ta.scrollTop;
    ED.hl.scrollLeft = ED.ta.scrollLeft;
  }
}

function edPreview() {
  if (ED.prev && EDITING) ED.prev.innerHTML = md2html(text0());
}

/* One handler for every kind of change, whoever caused it. */
function edChanged() {
  if (!ED.ta || !EDITING) return;
  EDITING.content = ED.ta.value;
  if (!EDITING.dirty) {
    EDITING.dirty = true;
    // Surgical: a full renderEditor() here would rebuild the textarea and
    // eat the keystroke that just set the flag.
    const badge = document.getElementById("ed-dirty");
    if (badge) badge.hidden = false;
  }
  cancelAnimationFrame(ED.raf);
  ED.raf = requestAnimationFrame(edPaint);
  clearTimeout(ED.lintTimer);
  ED.lintTimer = setTimeout(() => { edPreview(); edRefreshFindings(); }, 150);
}

function edSetView(v) {
  edSync();
  ED.view = v;
  localStorage.setItem("claude-ui-edview", v);
  renderEditor();
}

/* ------------------------------------------------------------------ chrome */

function edHeadline(f) {
  const bits = [el("span", { text: "Editing " }), el("b", { text: f.path })];
  if (!f.exists) bits.push(el("span.muted", { text: " (new file — created on save)" }));
  if (f.readonly) bits.push(el("span.badge.badge-warning", { text: "read-only" }));
  if (f.item && !f.enabled) bits.push(el("span.muted", { text: " · this item is disabled" }));
  bits.push(el("span.warn", { id: "ed-dirty", hidden: !f.dirty, text: " · unsaved changes" }));
  if (f.item || /CLAUDE\.md|settings\.json/.test(f.path))
    bits.push(el("span.muted", { text: " · applies to new sessions" }));
  return el("div.view-head", {}, bits);
}

function edToolbar(f, isMd) {
  const bar = el("div.ed-toolbar");
  const editable = !f.readonly;

  if (editable && isMd && ED.view !== "preview") {
    const fmt = el("div.ed-fmtgroup");
    const b = (name, ic, title, key) => {
      const btn = el("button.btn.btn-sm.btn-icon.btn-ghost", {
        title: title + (key ? "  " + key : ""),
        "aria-label": title,
        onclick: () => edActions[name](),
      }, icon(ic));
      return btn;
    };
    fmt.append(
      b("bold", "bold", "Bold", "⌘B"),
      b("italic", "italic", "Italic", "⌘I"),
      b("code", "code", "Inline code"),
      b("link", "link", "Link", "⌘K"),
      el("span.separator.separator-v"),
      b("heading", "heading", "Heading"),
      b("bullet", "list", "Bullet list"),
      b("number", "listOrdered", "Numbered list"),
      b("quote", "quote", "Blockquote"));
    bar.append(fmt);
  }

  const mode = edMode(f.item ? f.file : f.path);
  if (editable && (mode === "json" || mode === "md")) {
    const fb = el("button.btn.btn-sm", {
      title: mode === "json"
        ? "Reformat with 2-space indent"
        : "Tidy trailing whitespace, blank lines and list bullets",
      onclick: edFormat,
    }, icon("wand"), el("span", { text: "Format" }));
    bar.append(fb);
  }

  if (isMd) {
    const seg = el("div.ed-seg", { role: "group", "aria-label": "Editor layout" });
    for (const [k, label, ic] of [["edit", "Edit", "pencil"],
                                  ["split", "Split", "columns"],
                                  ["preview", "Preview", "eye"]]) {
      if (k === "split" && innerWidth < 900) continue;
      seg.append(el("button.btn.btn-sm", {
        class: ED.view === k ? "on" : "",
        "aria-pressed": String(ED.view === k),
        onclick: () => edSetView(k),
      }, icon(ic), el("span", { text: label })));
    }
    bar.append(seg);
  }
  return bar;
}

function renderEditor() {
  const view = document.getElementById("editorview");
  const f = EDITING;
  view.innerHTML = "";
  ED.ta = ED.hl = ED.prev = ED.strip = null;

  view.append(edHeadline(f));

  const shell = el("div.editor-shell");

  if (f.item && f.files && f.files.length > 1) {
    const tabs = el("div.ftabs");
    for (const name of f.files)
      tabs.append(el("button.btn.btn-sm", {
        class: name === f.file ? "on" : "",
        text: name,
        onclick: () => { edSync(); openItemEditor(f.type, f.name, name, f.enabled, null, f.root); },
      }));
    shell.append(tabs);
  }

  const isMd = edMode(f.item ? f.file : f.path) === "md";
  const view3 = isMd ? (innerWidth < 900 && ED.view === "split" ? "edit" : ED.view) : "edit";

  shell.append(edToolbar(f, isMd));

  ED.strip = el("div.ed-findings", { hidden: true });
  shell.append(ED.strip);

  const body = el("div.ed-body", { class: view3 === "split" ? "ed-body-split" : "" });

  if (view3 !== "preview") {
    const text = f.content || "";
    ED.plain = text.length > ED_HL_MAX_BYTES
      || text.split("\n").length > ED_HL_MAX_LINES;

    const pane = el("div.ed-pane", { class: ED.plain ? "ed-plain" : "" });
    ED.hl = el("pre.ed-hl", { "aria-hidden": "true" });
    ED.ta = el("textarea.ed-input", {
      id: "fileeditor", spellcheck: false, value: text,
      readOnly: !!f.readonly,
      "aria-label": "File contents",
      oninput: edChanged,
      onscroll: edSyncScroll,
      onkeydown: edKey,
    });
    pane.append(ED.hl, ED.ta);
    body.append(pane);
    if (ED.plain)
      body.append(el("div.muted.ed-note", {
        text: "Syntax highlighting is off above "
          + ED_HL_MAX_BYTES / 1024 + " KB — this file is "
          + Math.round(text.length / 1024) + " KB." }));
  }

  if (view3 === "preview" || view3 === "split") {
    ED.prev = el("div.mdprev.ed-prev");
    body.append(ED.prev);
  }

  shell.append(body);

  if (f.assist) {
    shell.append(el("div.code-pane.assistout", { text: f.assist.text }));
    const abar = el("div.toolbar", { style: { marginBottom: 0 } });
    if (f.assist.replaces && !f.readonly)   // nothing to apply it to otherwise
      abar.append(mkbtn("btn-sm btn-primary", "Use result", () => {
        edReplaceAll(f.assist.text);
        delete f.assist;
        renderEditor();
      }));
    abar.append(mkbtn("btn-sm", "Dismiss", () => { edSync(); delete f.assist; renderEditor(); }));
    shell.append(abar);
  }

  const bar = el("div.toolbar", { style: { marginBottom: 0 } });
  if (!f.readonly) {
    const save = mkbtn("btn-primary", "Save", saveFile, "Save this file (⌘S)");
    save.prepend(icon("save"));
    bar.append(save);
  }
  const assist = mkbtn("btn-sm", "Assist", edAssist,
    "Ask Claude (via the claude CLI) to improve or review this file");
  assist.prepend(icon("sparkles"));
  bar.append(assist);
  const copy = mkbtn("btn-sm btn-ghost", "Copy path",
    () => copyText(f.abs || f.path, "path"));
  copy.prepend(icon("copy"));
  bar.append(copy);
  bar.append(el("div.toolbar-end", {}, mkbtn("btn-ghost", "Close", closeEditor)));
  shell.append(bar);

  view.append(shell);

  edPaint();
  edPreview();
  edRefreshFindings();

  if (ED.locate) {
    const at = edResolveLocate(text0(), ED.locate);
    ED.locate = null;
    if (at >= 0) requestAnimationFrame(() => edGoto(at));
  }
}

/* Keys handled while the caret is in the textarea. The global handler in
   app.js owns ⌘S; these are the ones that only make sense in a file. */
function edKey(e) {
  const meta = e.ctrlKey || e.metaKey;
  if (meta && !e.altKey) {
    const k = e.key.toLowerCase();
    if (k === "b" && edIsMd()) { e.preventDefault(); edActions.bold(); return; }
    if (k === "i" && edIsMd()) { e.preventDefault(); edActions.italic(); return; }
    if (k === "k" && edIsMd()) { e.preventDefault(); edActions.link(); return; }
  }
  if (e.key === "Tab" && !e.shiftKey && !meta) {
    e.preventDefault();
    edInsert("  ");
  }
}

/* CSS collapses the split grid on its own, but the Edit/Split/Preview control
   is built in JS, so crossing the breakpoint has to rebuild the chrome or the
   Split button lingers on a window too narrow to honour it. */
const ED_NARROW = () => innerWidth < 900;
let edWasNarrow = ED_NARROW();
addEventListener("resize", () => {
  if (ED_NARROW() === edWasNarrow) return;
  edWasNarrow = ED_NARROW();
  if (EDITING) { edSync(); renderEditor(); }
});

/* -------------------------------------------------------------------- assist */

async function edAssist() {
  edSync();
  const r = await modal({ title: "Ask Claude",
    text: "Runs `claude -p` locally with this file (uses your own Claude Code auth; can take a minute).",
    fields: [
      { id: "m", label: "Task", type: "select", options: [
        { value: "improve", label: "Improve — tighten description & triggers, return revised file" },
        { value: "review", label: "Review — list concrete problems, no changes" },
        { value: "custom", label: "Custom instruction…" }] },
      { id: "c", label: "Custom instruction (for custom)",
        placeholder: "e.g. add a 'Use when' trigger list for CI debugging" }],
    ok: "Run" });
  if (!r) return;
  const t = toast({ title: "Asking Claude… this can take a while", variant: "loading", duration: 0 });
  try {
    const res = await api("/api/assist", { mode: r.m, custom: r.c,
      content: EDITING.content, path: EDITING.path });
    t.close();
    EDITING.assist = { text: res.result, replaces: res.replaces };
    renderEditor();
  } catch (e) { t.close(); toast(e.message, true); }
}
