/* ===========================================================================
   ui.js — the component layer's behaviour half.

   components.css supplies the shadcn look; this file supplies the pieces that
   need JS: the theme controller, an icon set, toasts, dialogs (with a real
   focus trap), dropdown menus, and a few DOM helpers. app.js consumes these
   and never builds a toast/dialog/menu by hand.

   Loaded before app.js. Everything is attached to the global scope on purpose
   — there is no bundler here, and inline onclick= handlers in generated markup
   have to be able to see these names.
   =========================================================================== */

/* ------------------------------------------------------------- DOM helpers */

const esc = (t) =>
  String(t == null ? "" : t).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* el("div.card", {onclick}, child, "text", …) — the tag accepts a css-ish
   shorthand ("button.btn.btn-sm") so markup reads close to the class names in
   components.css. Attributes go in the props object; anything that is not a
   known DOM property is set with setAttribute, so aria- and data- attributes
   just work. */
function el(spec, props, ...kids) {
  const [tag, ...classes] = String(spec).split(".");
  const node = document.createElement(tag || "div");
  if (classes.length) node.className = classes.join(" ");
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class" || k === "className") node.className += (node.className ? " " : "") + v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    // Property first whenever the node has one, strings included: a <textarea>
    // has no value *attribute*, so setAttribute("value") silently does nothing
    // and the editor comes up empty. Keys with no matching property (for,
    // aria-*, data-*) fall through to setAttribute, which is what they want.
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  add(node, kids);
  return node;
}

function add(parent, kids) {
  for (const k of kids.flat(4)) {
    if (k == null || k === false) continue;
    parent.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return parent;
}

/* ------------------------------------------------------------------ icons --
   A trimmed lucide set (the icon family shadcn/ui ships with), inlined as
   paths so nothing is fetched. icon("search") returns a fresh <svg>. */

const ICONS = {
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8M12 17v4"/>',
  contrast: '<circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 0 0 0 20Z" fill="currentColor" stroke="none"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  warn: '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/><path d="M12 9v4M12 17h.01"/>',
  error: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  success: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
  loader: '<path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  settings: '<path d="M20 7h-9M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
  terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
  server: '<rect width="20" height="8" x="2" y="2" rx="2"/><rect width="20" height="8" x="2" y="14" rx="2"/><path d="M6 6h.01M6 18h.01"/>',
  wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76Z"/>',
  chart: '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/>',
  dollar: '<path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  pulse: '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/><path d="M3.22 12H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"/>',
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M16 13H8M16 17H8M10 9H8"/>',
  sparkles: '<path d="M9.9 2.6 8 7l-4.4 1.9L8 10.8 9.9 15l1.9-4.2L16 8.9 11.8 7Z"/><path d="M18 3v4M20 5h-4M17.8 15.2 17 17l-1.8.8 1.8.8.8 1.8.8-1.8 1.8-.8-1.8-.8Z"/>',
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2M20 14h2M15 13v2M9 13v2"/>',
  droplet: '<path d="M12 2.7 6.7 8a7.5 7.5 0 1 0 10.6 0Z"/>',
  panel: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 15h18"/>',
  plus: '<path d="M5 12h14M12 5v14"/>',
  trash: '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  pencil: '<path d="M21.2 2.8a2.7 2.7 0 0 0-3.8 0l-11 11L5 19l5.2-1.4 11-11a2.7 2.7 0 0 0 0-3.8Z"/><path d="m15 5 4 4"/>',
  play: '<path d="M6 3.5v17l13.5-8.5Z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  link: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  power: '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  save: '<path d="M15.2 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8.8Z"/><path d="M17 21v-7H7v7M7 3v5h8"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8Z"/>',
  copy: '<rect width="13" height="13" x="8" y="8" rx="2"/><path d="M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2"/>',
  plug: '<path d="M12 22v-5M9 8V2M15 8V2"/><path d="M18 8v4a6 6 0 0 1-12 0V8Z"/>',
  split: '<path d="M16 3h5v5"/><path d="M8 3H3v5"/><path d="M21 3 3 21"/><path d="m15 15 6 6v-5"/>',
};

function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

/* ------------------------------------------------------------------ theme --
   Two orthogonal axes: family (palette) and mode (light/dark/system). Both
   live on <html> so the CSS in theme.css can select on them, and both persist
   in localStorage. index.html applies the stored choice before first paint;
   this module only handles changes afterwards. */

const THEMES = [
  { id: "clay", label: "Clay", hint: "warm ivory & terracotta", swatch: ["#d0754e", "#f7f2e8", "#2b2724"] },
  { id: "slate", label: "Slate", hint: "shadcn's neutral default", swatch: ["#1e293b", "#f8fafc", "#0f172a"] },
  { id: "gruvbox", label: "Gruvbox", hint: "the original palette", swatch: ["#fabd2f", "#fbf1c7", "#282828"] },
  { id: "nord", label: "Nord", hint: "cool arctic blues", swatch: ["#88c0d0", "#eceff4", "#2e3440"] },
];
const MODES = [
  { id: "light", label: "Light", icon: "sun" },
  { id: "dark", label: "Dark", icon: "moon" },
  { id: "system", label: "System", icon: "monitor" },
];

const THEME_KEYS = { family: "claude-ui-family", mode: "claude-ui-mode" };

const lsGet = (k, d) => {
  try { return localStorage.getItem(k) || d; } catch (e) { return d; }
};
const lsSet = (k, v) => {
  try { localStorage.setItem(k, v); } catch (e) { /* private mode; ignore */ }
};

const themeState = () => ({
  family: THEMES.some((t) => t.id === lsGet(THEME_KEYS.family)) ? lsGet(THEME_KEYS.family) : "clay",
  mode: MODES.some((m) => m.id === lsGet(THEME_KEYS.mode)) ? lsGet(THEME_KEYS.mode) : "system",
});

const resolveMode = (mode) =>
  mode !== "system" ? mode
    : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

function applyTheme(animate) {
  const { family, mode } = themeState();
  const root = document.documentElement;
  if (animate) {
    root.classList.add("theming");
    clearTimeout(applyTheme._t);
    applyTheme._t = setTimeout(() => root.classList.remove("theming"), 260);
  }
  root.dataset.family = family;
  root.dataset.mode = resolveMode(mode);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    meta.content = getComputedStyle(root).getPropertyValue("--background").trim() || "#282828";
  }
}

function setTheme({ family, mode }) {
  if (family) lsSet(THEME_KEYS.family, family);
  if (mode) lsSet(THEME_KEYS.mode, mode);
  applyTheme(true);
}

/* cycles light → dark → system; kept because the command palette exposes it */
function cycleThemeMode() {
  const order = ["light", "dark", "system"];
  const next = order[(order.indexOf(themeState().mode) + 1) % order.length];
  setTheme({ mode: next });
  toast("theme: " + next + (next === "system" ? " (" + resolveMode("system") + ")" : ""));
}

/* the theme dropdown: mode segmented control on top, palettes below */
function openThemeMenu(anchor) {
  const st = themeState();
  const menu = el("div.dropdown-menu", { role: "menu", style: { minWidth: "13.5rem" } });

  menu.append(el("div.dropdown-label", { text: "Appearance" }));
  for (const m of MODES) {
    menu.append(el("button.btn.dropdown-item", {
      role: "menuitemradio",
      "aria-checked": String(st.mode === m.id),
      onclick: () => { closeDropdown(); setTheme({ mode: m.id }); },
    },
      icon(m.icon),
      m.label,
      st.mode === m.id ? el("span.dropdown-shortcut", {}, icon("check", "check")) : null));
  }

  menu.append(el("div.dropdown-separator"));
  menu.append(el("div.dropdown-label", { text: "Palette" }));
  for (const t of THEMES) {
    menu.append(el("button.btn.dropdown-item", {
      role: "menuitemradio",
      "aria-checked": String(st.family === t.id),
      title: t.hint,
      onclick: () => { closeDropdown(); setTheme({ family: t.id }); },
    },
      el("span.swatch-trio", {},
        ...t.swatch.map((c) => el("i", { style: { background: c } }))),
      t.label,
      st.family === t.id ? el("span.dropdown-shortcut", {}, icon("check", "check")) : null));
  }
  openDropdown(anchor, menu);
}

/* --------------------------------------------------------------- dropdown --
   One open at a time, positioned under its anchor and flipped when it would
   leave the viewport. Escape and any outside click close it. */

let DROPDOWN = null;

function closeDropdown() {
  if (!DROPDOWN) return;
  DROPDOWN.node.remove();
  removeEventListener("keydown", DROPDOWN.onkey, true);
  removeEventListener("mousedown", DROPDOWN.onout, true);
  removeEventListener("resize", closeDropdown);
  scrollingElement()?.removeEventListener("scroll", closeDropdown);
  const a = DROPDOWN.anchor;
  DROPDOWN = null;
  if (a && a.isConnected) a.setAttribute("aria-expanded", "false");
}

const scrollingElement = () => document.scrollingElement || document.documentElement;

function openDropdown(anchor, node) {
  const reopening = DROPDOWN && DROPDOWN.anchor === anchor;
  closeDropdown();
  if (reopening) return;

  document.body.appendChild(node);
  const r = anchor.getBoundingClientRect();
  const w = node.offsetWidth;
  const h = node.offsetHeight;
  node.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + "px";
  node.style.top = (r.bottom + 4 + h > innerHeight - 8 && r.top - 4 - h > 8
    ? r.top - 4 - h
    : Math.min(r.bottom + 4, innerHeight - h - 8)) + "px";

  const onkey = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); closeDropdown(); anchor.focus(); return; }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Tab") return;
    const items = [...node.querySelectorAll("button:not(:disabled)")];
    if (!items.length) return;
    e.preventDefault();
    const i = items.indexOf(document.activeElement);
    const d = e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey) ? -1 : 1;
    items[(i + d + items.length + (i < 0 ? (d > 0 ? 0 : 1) : 0)) % items.length].focus();
  };
  const onout = (e) => { if (!node.contains(e.target) && e.target !== anchor) closeDropdown(); };

  addEventListener("keydown", onkey, true);
  addEventListener("mousedown", onout, true);
  addEventListener("resize", closeDropdown);
  scrollingElement()?.addEventListener("scroll", closeDropdown, { passive: true });
  anchor.setAttribute("aria-expanded", "true");
  DROPDOWN = { node, anchor, onkey, onout };
}

/* a plain action menu, the shape the row overflow buttons want */
function openMenu(anchor, entries) {
  const menu = el("div.dropdown-menu", { role: "menu" });
  for (const e of entries) {
    if (e.separator) { menu.append(el("div.dropdown-separator")); continue; }
    menu.append(el("button.btn.dropdown-item", {
      role: "menuitem",
      class: e.danger ? "danger" : "",
      onclick: () => { closeDropdown(); e.fn(); },
    }, e.icon ? icon(e.icon) : null, e.label));
  }
  openDropdown(anchor, menu);
}

/* ----------------------------------------------------------------- toasts --
   Sonner-shaped: top-right stack, newest at the top, auto-dismiss unless it's
   an error (errors carry paths worth reading, so they wait to be closed).

   toast(msg)                       – success/neutral, auto-dismisses
   toast(msg, true)                 – error, sticky
   toast(msg, false, {label, fn})   – with an action button (undo, retry…)
   toast({title, description, variant, duration, action})  – full form */

function toast(msg, isErr, action) {
  const o = typeof msg === "object" && msg !== null && !(msg instanceof Node)
    ? msg
    : { title: msg, variant: isErr ? "error" : "success", action };
  const variant = o.variant || "success";
  const sticky = o.duration === 0 || (o.duration == null && variant === "error");
  const ms = o.duration != null ? o.duration : (o.action ? 10000 : 4000);

  const box = document.getElementById("toaster");
  const node = el("div.toast", { class: "toast-" + variant, role: variant === "error" ? "alert" : "status" });

  const close = () => {
    node.classList.add("leaving");
    node.addEventListener("animationend", () => node.remove(), { once: true });
    setTimeout(() => node.remove(), 400);
  };

  const iconName = { success: "success", error: "error", warning: "warn", info: "info", loading: "loader" }[variant];
  node.append(el("span.toast-icon", {}, icon(iconName || "info")));

  const body = el("div.toast-body", {}, el("div", { text: o.title }));
  if (o.description) body.append(el("div.muted", { text: o.description, style: { marginTop: "2px", fontSize: ".75rem" } }));
  if (o.action) {
    body.append(el("div.toast-actions", {},
      el("button.btn.btn-sm.btn-secondary", {
        text: o.action.label,
        onclick: () => { close(); o.action.fn(); },
      })));
  }
  node.append(body);
  node.append(el("button.toast-close", {
    text: "×", title: "dismiss", "aria-label": "dismiss notification", onclick: close,
  }));

  box.prepend(node);
  while (box.children.length > 6) box.lastChild.remove();
  if (!sticky) setTimeout(close, ms);
  return { close, node };
}

/* ---------------------------------------------------------------- dialogs --
   modal() replaces prompt()/confirm(): resolves to {field: value} on OK and
   null on cancel/Escape/backdrop. Focus is trapped inside and restored to
   whatever was focused before, which the old implementation did not do.

   fields: [{id, label, hint, type: "text"|"select"|"textarea", value,
             placeholder, options, mono}] */

function modal({ title, text, fields = [], ok = "OK", cancel = "Cancel", danger = false, wide = false }) {
  return new Promise((resolve) => {
    const host = document.getElementById("modal");
    const restore = document.activeElement;
    host.hidden = false;
    host.innerHTML = "";
    host.className = "dialog-overlay";

    const content = el("div.dialog-content", {
      role: "dialog",
      "aria-modal": "true",
      style: wide ? { maxWidth: "44rem" } : null,
    });

    if (title || text) {
      const head = el("div.dialog-header");
      if (title) head.append(el("h2.dialog-title", { text: title, id: "dialog-title" }));
      if (text) head.append(el("p.dialog-description", { text }));
      content.append(head);
      if (title) content.setAttribute("aria-labelledby", "dialog-title");
    }

    const inputs = {};
    if (fields.length) {
      const body = el("div.dialog-body");
      for (const f of fields) {
        const row = el("div.mrow");
        let inp;
        if (f.type === "select") {
          inp = el("select");
          for (const o of f.options) {
            const value = o.value !== undefined ? o.value : o;
            inp.append(el("option", {
              value, text: o.label !== undefined ? o.label : o, selected: value === f.value,
            }));
          }
        } else if (f.type === "textarea") {
          inp = el("textarea", { value: f.value || "", rows: f.rows || 5 });
        } else if (f.type === "checklist") {
          inp = checklist(f);
        } else {
          inp = el("input", { type: "text", value: f.value || "", placeholder: f.placeholder || "" });
          if (f.mono) inp.className = "mono";
        }
        if (f.label) {
          const id = "mf_" + f.id;
          inp.id = id;
          row.append(el("label", { for: id, text: f.label }));
        }
        inputs[f.id] = inp;
        row.append(inp.tagName === "SELECT" ? filterSelect(inp) : inp);
        if (f.hint) row.append(el("div.field-hint", { text: f.hint }));
        body.append(row);
      }
      content.append(body);
    }

    const done = (val) => {
      host.hidden = true;
      host.innerHTML = "";
      document.removeEventListener("keydown", onkey, true);
      if (restore && restore.isConnected && restore.focus) restore.focus();
      resolve(val);
    };
    const submit = () => done(Object.fromEntries(Object.entries(inputs).map(
      ([k, i]) => [k, Array.isArray(i.value) ? i.value : String(i.value).trim()])));

    const focusables = () => [...content.querySelectorAll(
      'button:not(:disabled), input:not([hidden]):not(:disabled), select:not([hidden]), textarea, [tabindex]:not([tabindex="-1"])')]
      .filter((n) => n.offsetParent !== null);

    const onkey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); done(null); return; }
      if (e.key === "Tab") {
        const f = focusables();
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        return;
      }
      // Enter submits, except where it belongs to a dropdown or a textarea
      if (e.key === "Enter" && e.target.tagName !== "SELECT"
          && e.target.tagName !== "TEXTAREA" && !e.target.closest(".fsel")) {
        e.preventDefault();
        submit();
      }
    };
    document.addEventListener("keydown", onkey, true);
    host.onclick = (e) => { if (e.target === host) done(null); };

    const okBtn = el("button.btn", {
      class: danger ? "btn-destructive" : "btn-primary", text: ok, onclick: submit,
    });
    content.append(el("div.dialog-footer", {},
      el("button.btn", { text: cancel, onclick: () => done(null) }),
      okBtn));

    host.append(content);
    const first = Object.values(inputs)[0];
    ((first && (first.fselTrigger || first)) || okBtn).focus();
    if (first && first.select) first.select();
  });
}

const mconfirm = (title, text, ok) =>
  modal({ title, text, ok: ok || "Confirm", danger: true }).then((r) => r !== null);

/* --------------------------------------------------------- loading states */

const skeletonList = (rows = 4) =>
  el("div.list", {},
    ...Array.from({ length: rows }, () => el("div.skeleton.skeleton-row", { style: { margin: 0, borderRadius: 0 } })));

function emptyState(title, hint, iconName) {
  return el("div.empty-state", {},
    el("div.es-icon", {}, icon(iconName || "folder")),
    el("div.es-title", { text: title }),
    hint ? el("div.es-hint", { text: hint }) : null);
}

/* --------------------------------------------------------------- fragments */

const badge = (text, variant) => el("span.badge", { class: "badge-" + (variant || "secondary"), text });

const statCard = (value, label, opts = {}) =>
  el("div.stat", { class: opts.accent ? "stat-accent" : "", title: opts.title || "" },
    el("div.stat-label", { text: label }),
    el("div.stat-value", { text: value }),
    opts.hint ? el("div.stat-hint", { text: opts.hint }) : null);

function sectionTitle(text, count) {
  return el("h2.section-title", {},
    el("span", { text }),
    count != null ? el("span.count", { text: String(count) }) : null,
    el("span.rule"));
}

/* a small labelled toggle built on .switch, used for filter flags */
function switchToggle(label, checked, onchange, title) {
  const sw = el("button.switch", {
    type: "button", role: "switch", "aria-checked": String(!!checked),
    onclick: () => {
      const now = sw.getAttribute("aria-checked") !== "true";
      sw.setAttribute("aria-checked", String(now));
      onchange(now);
    },
  });
  return el("label.switch-row", { title: title || "" }, sw, el("span", { text: label }));
}

/* ---------------------------------------------------------------- checklist --
   A grouped multi-select field for modal(). Groups are
   {label, rows: [{value, name, desc, badges, disabled, reason}]}; rows that
   can't be picked render greyed with their reason in place of a checkbox.
   Exposes .value as the array of checked values, which modal()'s submit()
   passes through untouched. */
function checklist({ groups = [], hint }) {
  const node = el("div.checklist");
  const count = el("span.cl-count");
  const boxes = [];

  const sync = () => {
    const on = boxes.filter((b) => b.checked).length;
    count.textContent = on + " of " + boxes.length + " kept";
  };
  const setAll = (v) => { for (const b of boxes) b.checked = v; sync(); };

  node.append(el("div.cl-head", {},
    count,
    el("div.cl-head-actions", {},
      el("button.btn.btn-sm.btn-ghost", { type: "button", text: "All", onclick: () => setAll(true) }),
      el("button.btn.btn-sm.btn-ghost", { type: "button", text: "None", onclick: () => setAll(false) }))));

  for (const g of groups) {
    if (!g.rows || !g.rows.length) continue;
    node.append(el("div.cl-group", {}, sectionTitle(g.label, g.rows.length)));
    for (const r of g.rows) {
      const row = el("label.cl-row", { class: r.disabled ? "off" : "", title: r.reason || "" });
      if (r.disabled) {
        row.append(el("span.cl-slot"));
      } else {
        const box = el("input", { type: "checkbox", value: r.value, checked: r.checked !== false });
        box.onchange = sync;
        boxes.push(box);
        row.append(box);
      }
      row.append(el("div.cl-body", {},
        el("div.cl-line", {},
          el("span.li-name", { text: r.name }),
          ...(r.badges || [])),
        el("span.li-desc", { text: r.reason || r.desc || "" })));
      node.append(row);
    }
  }
  if (hint) node.append(el("div.field-hint", { text: hint }));
  sync();

  Object.defineProperty(node, "value", {
    get: () => boxes.filter((b) => b.checked).map((b) => b.value),
  });
  node.fselTrigger = boxes[0] || null;
  return node;
}

/* ---------------------------------------------------------------- combobox --
   shadcn's Combobox/Select, minus Radix. Long value lists — hook events,
   docs-discovered enums, the 300-odd env var names — are unusable in a native
   dropdown or a datalist, so anything past FSEL_MIN options gets a popup list
   narrowed as you type with the same fuzzy() the command palette uses.

   Two flavours share filterPopup(). filterSelect() takes a populated <select>
   and returns the node to lay out; the select stays in the DOM as the value
   holder, so callers keep reading .value, listening for change, and assigning
   .value programmatically. filterInput() does the same for a free-text input
   with suggestions, standing in for its <datalist> — typed text still wins,
   the list is only a shortcut. Shorter lists keep the native control. */

const FSEL_MIN = 6;

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

/* The popup itself: items are {value, text}, value() reports what is currently
   set (marked in the list), own adds a filter box for owners that have nowhere
   else to type. Returns a controller the owner drives from its key handling. */
function filterPopup(wrap, { items, value, own, onPick }) {
  const list = el("div.fsellist");
  const pop = el("div.fselpop", { role: "listbox" });
  let hits = items;
  // a select always keeps a row highlighted; an input starts with none, so a
  // plain Enter commits what was typed instead of the first suggestion
  let cur = own ? Math.max(0, items.findIndex((o) => o.value === value())) : -1;

  const draw = () => {
    list.innerHTML = "";
    if (!hits.length) {
      list.append(el("div.fselempty", { text: "No matches" }));
      return;
    }
    hits.forEach((o, i) => {
      const row = el("div.fselrow", {
        role: "option",
        "aria-selected": String(o.value === value()),
        class: (i === cur ? "sel " : "") + (o.value === value() ? "on" : ""),
        text: o.text,
        onmousedown: (e) => { e.preventDefault(); onPick(o); },
      });
      list.append(row);
      if (i === cur) setTimeout(() => row.scrollIntoView({ block: "nearest" }));
    });
  };

  const ctl = {
    filter(s) {
      s = s.trim().toLowerCase();
      hits = s ? items.filter((o) => fuzzy(s, o.text + " " + o.value) >= 0) : items;
      cur = own ? 0 : -1;
      draw();
    },
    move(d) {
      cur = Math.max(own ? 0 : -1, Math.min(cur + d, hits.length - 1));
      draw();
    },
    take() {
      const o = hits[cur];
      if (o) onPick(o);
      return !!o;
    },
    close: () => pop.remove(),
  };

  if (own) {
    const q = el("input.fselq", { type: "text", placeholder: "Filter…", oninput: () => ctl.filter(q.value) });
    pop.append(q);
    setTimeout(() => q.focus());
  }
  draw();
  pop.append(list);
  wrap.append(pop);
  // hang the popup off the right edge instead when it would run off-screen
  if (pop.getBoundingClientRect().right > innerWidth - 8) {
    pop.style.left = "auto";
    pop.style.right = "0";
  }
  return ctl;
}

function filterSelect(sel) {
  if (sel.options.length <= FSEL_MIN) return sel;
  const wrap = el("span.fsel");
  sel.hidden = true;
  sel.tabIndex = -1;
  const label = el("span.fsellbl");
  const trig = el("button.btn.fseltrig", {
    type: "button", "aria-haspopup": "listbox", "aria-expanded": "false",
  }, label, el("span.fselcar", {}, icon("chevronDown")));
  wrap.append(trig, sel);
  sel.fselTrigger = trig;  // modal() focuses this instead of the hidden select

  // the current option's text, refreshed after every change (handlers may reset
  // the value themselves, as the template picker does)
  const sync = () => {
    const o = sel.selectedOptions[0];
    label.textContent = o ? o.textContent : "";
  };
  sync();

  let pop = null;

  const close = () => {
    if (!pop) return;
    pop.close();
    pop = null;
    removeEventListener("keydown", onkey, true);
    removeEventListener("mousedown", onout, true);
    trig.setAttribute("aria-expanded", "false");
    sync();
  };

  const pick = (o) => {
    sel.value = o.value;
    close();
    trig.focus();
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    sync();
  };

  // capture on window so the popup's keys win over the modal's and the app's
  // own document-level capture handlers while it is open
  const onkey = (e) => {
    if (!["ArrowDown", "ArrowUp", "Enter", "Escape", "Tab"].includes(e.key)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "ArrowDown") pop.move(1);
    else if (e.key === "ArrowUp") pop.move(-1);
    else if (e.key === "Enter") pop.take();
    else { close(); trig.focus(); }
  };
  const onout = (e) => { if (!wrap.contains(e.target)) close(); };

  const open = () => {
    // options can change between renders (live docs suggestions), so read them
    // fresh; disabled entries are labels ("insert template…"), not choices
    const items = [...sel.options].filter((o) => !o.disabled)
      .map((o) => ({ value: o.value, text: o.textContent }));
    pop = filterPopup(wrap, { items, value: () => sel.value, own: true, onPick: pick });
    trig.setAttribute("aria-expanded", "true");
    addEventListener("keydown", onkey, true);
    addEventListener("mousedown", onout, true);
  };

  trig.onclick = () => { if (pop) { close(); trig.focus(); } else open(); };
  return wrap;
}

function filterInput(inp, values) {
  if (values.length <= FSEL_MIN) return null;  // caller keeps the datalist
  const wrap = el("span.fsel.fcombo", { class: inp.className });
  inp.className = "";  // sizing rules move to the wrapper
  const trig = el("button.btn.btn-sm.fcaret", {
    type: "button",
    tabIndex: -1,  // Tab belongs to the input; the caret is mouse-only
    title: "Browse suggestions",
    "aria-label": "Browse suggestions",
  }, icon("chevronDown"));
  wrap.append(inp, trig);
  const items = values.map((v) => ({ value: String(v), text: String(v) }));

  let pop = null, quiet = false;

  const close = () => {
    if (!pop) return;
    pop.close();
    pop = null;
    removeEventListener("keydown", onkey, true);
    removeEventListener("mousedown", onout, true);
  };

  const pick = (o) => {
    inp.value = o.value;
    close();
    inp.focus();
    // notify like a typed edit would, without the input event reopening the list
    quiet = true;
    inp.dispatchEvent(new Event("input", { bubbles: true }));
    inp.dispatchEvent(new Event("change", { bubbles: true }));
    quiet = false;
  };

  const onkey = (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      pop.move(e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "Enter") {
      // only a row the user arrowed onto wins; otherwise the typed text stands
      // and the key carries on to whatever else handles it (modals submit)
      if (pop.take()) { e.preventDefault(); e.stopPropagation(); }
      else close();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
      inp.focus();
    } else if (e.key === "Tab") {
      close();
    }
  };
  const onout = (e) => { if (!wrap.contains(e.target)) close(); };

  // q is what to narrow by: the typed text while typing, nothing when the caret
  // is used to browse the whole list
  const open = (q) => {
    if (!pop) {
      pop = filterPopup(wrap, { items, value: () => inp.value, own: false, onPick: pick });
      addEventListener("keydown", onkey, true);
      addEventListener("mousedown", onout, true);
    }
    pop.filter(q);
  };

  inp.oninput = () => { if (!quiet) open(inp.value); };
  inp.onkeydown = (e) => {
    if (!pop && e.key === "ArrowDown") { e.preventDefault(); open(inp.value); }
  };
  trig.onclick = () => { if (pop) close(); else { inp.focus(); open(""); } };
  return wrap;
}

// A filter box whose keystrokes re-render the whole view around it: run the
// re-render, then put the caret back where it was instead of yanking it to
// end-of-string, which makes editing mid-word impossible. The selection has to
// be read before render() — the old input node is detached afterwards.
function refilter(id, render) {
  const src = document.getElementById(id);
  const [a, b, d] = [src.selectionStart, src.selectionEnd, src.selectionDirection];
  render();
  const nf = document.getElementById(id);
  if (!nf) return;
  nf.focus();
  // keep the end and direction too, or shift-arrow selections collapse
  try { nf.setSelectionRange(a, b, d); } catch { /* older engines */ }
}

applyTheme(false);
addEventListener("storage", () => applyTheme(true));
matchMedia("(prefers-color-scheme: dark)")
  .addEventListener("change", () => { if (themeState().mode === "system") applyTheme(true); });
