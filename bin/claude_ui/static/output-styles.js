/* Output styles: the reference panel, and the form that writes a new one.
   ---------------------------------------------------------------------------
   Everything else on an inventory tab edits a file that already exists. An
   output style is the one item type you are most likely to want and least
   likely to have, because there is no way to make one without writing YAML
   frontmatter from memory — so this file is a create path, and a place to put
   the facts you need while you use it.

   Loaded before app.js (see index.html): renderInventory() calls into here.

   Naming note: `styleFileText` is the only producer of the file's text. The
   preview you approve and the bytes that get written are the same string,
   which is the whole reason there is no server-side template. */

let NEWSTYLE = null;    // create-form state; null means the form is closed
let STYLEDOCS = false;  // reference panel expanded?

const STYLE_DOC = "https://code.claude.com/docs/en/output-styles";

function newStyleState() {
  return {
    step: 1, loading: true, busy: false,
    presets: [], fields: [], doc: STYLE_DOC,
    preset: "",          // preset id, or "" for a blank style
    name: "", description: "", body: "",
    flags: { "keep-coding-instructions": false, "force-for-plugin": false },
    file: "", fileEdited: false,
  };
}

function closeNewStyle() {
  NEWSTYLE = null;
}

/* ------------------------------------------------------------ file text --
   YAML by hand, because the backend has no YAML library either and a style
   file only ever holds four scalars. Quote only when the value would otherwise
   parse as something else: parse_frontmatter keeps quotes verbatim, so a
   needlessly quoted description shows its own quote marks back in the list. */

const YAML_LEAD = "-?:,[]{}#&*!|>'\"%@`";

function yamlScalar(v) {
  const s = String(v);
  if (!s) return "''";
  if (s !== s.trim()) return "'" + s.replace(/'/g, "''") + "'";
  if (YAML_LEAD.includes(s[0])) return "'" + s.replace(/'/g, "''") + "'";
  if (/:\s/.test(s) || /\s#/.test(s) || s.endsWith(":")) {
    return "'" + s.replace(/'/g, "''") + "'";
  }
  return s;
}

function styleFileText(st) {
  const fm = [];
  if (st.name.trim()) fm.push("name: " + yamlScalar(st.name.trim()));
  if (st.description.trim()) {
    fm.push("description: " + yamlScalar(st.description.trim()));
  }
  // a field left at its default behaves the same as a field left out, and the
  // shorter file is the one a human wants to open later
  for (const key of Object.keys(st.flags)) {
    if (st.flags[key]) fm.push(key + ": true");
  }
  const body = st.body.replace(/\s+$/, "");
  return "---\n" + fm.join("\n") + "\n---\n\n" + body + "\n";
}

function styleSlug(s) {
  return String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 60);
}

function styleFileName(st) {
  return st.fileEdited ? st.file : (styleSlug(st.name) || "");
}

/* --------------------------------------------------------- reference panel */

function styleDocPoint(title, ...body) {
  return el("div.stack", { style: { gap: ".15rem" } },
    el("div", { style: { fontWeight: "600", fontSize: ".78rem" }, text: title }),
    el("div.field-hint", { style: { lineHeight: "1.55" } }, ...body));
}

function styleDocsCard() {
  // .plain drops the header's bottom padding so content can follow it snugly.
  // Collapsed, there is no content to follow — the row would sit against the
  // card's bottom edge — so put the padding back and let it sit centred.
  const head = el("div.card-header.plain", {
    style: STYLEDOCS ? null : { paddingBottom: "0.875rem" },
  },
    el("div.row-flex", { style: { gap: ".5rem" } },
      icon("book"),
      el("span.card-title", { text: "How output styles work" })),
    el("div.card-action", {},
      el("a.btn.btn-sm", { href: STYLE_DOC, target: "_blank", rel: "noreferrer" },
        icon("link"), el("span", { text: "Docs" }))));

  const toggle = mkbtn("btn-sm btn-ghost btn-icon", "", () => {
    STYLEDOCS = !STYLEDOCS;
    renderInventory();
  }, STYLEDOCS ? "Collapse" : "Expand");
  toggle.append(icon(STYLEDOCS ? "chevronDown" : "chevronRight"));
  head.firstChild.prepend(toggle);

  const card = el("div.card", { style: { marginBottom: "1rem" } }, head);
  if (!STYLEDOCS) return card;

  card.append(el("div.card-content", { class: "stack", style: { gap: ".75rem" } },
    styleDocPoint("It changes the system prompt, not the conversation",
      "An output style is added to Claude Code's system prompt, so it applies " +
      "to every response, and Claude is reminded to follow it as the " +
      "conversation goes on. That is what makes it stick where a CLAUDE.md " +
      "note or a SessionStart hook drifts."),

    styleDocPoint("Compared with the alternatives",
      "Output style: system prompt, every turn. CLAUDE.md: a user message " +
      "after the system prompt, for project facts. --append-system-prompt: a " +
      "one-off addition for a single run. Agent: a subagent with its own " +
      "system prompt. Skill: task instructions loaded when invoked or relevant."),

    styleDocPoint("Read once, at session start",
      "Creating or editing a style changes nothing in a running session. Run " +
      "/clear or start a new session to pick it up."),

    styleDocPoint("Subagents are not covered",
      "A subagent runs its own system prompt, so your style does not reach it. " +
      "A fork is the exception — it inherits the parent's system prompt."),

    styleDocPoint("keep-coding-instructions is off by default",
      "Off means Claude Code drops its built-in software-engineering " +
      "instructions and runs on your style alone. Turn it on for a style that " +
      "changes how Claude talks while it still writes code."),

    styleDocPoint("Built-in styles",
      "default, Proactive, Explanatory and Learning ship with Claude Code. " +
      "Your own styles appear alongside them."),

    styleDocPoint("Choosing one",
      "/config, then Output style — or set outputStyle in Settings. The " +
      "value must be the style's frontmatter name (or its filename when no " +
      "name is set), exact case: a file adhd.md with name: ADHD is selected " +
      "as ADHD, not adhd. A mismatch silently falls back to default. The " +
      "standalone /output-style command was deprecated in v2.1.73 and removed " +
      "in v2.1.91."),

    styleDocPoint("Where they are read from",
      "~/.claude/output-styles/ for you, .claude/output-styles/ for a project " +
      "(every such directory between the working directory and the repo root; " +
      "closest to the working directory wins a name clash), the managed policy " +
      "directory, and any enabled plugin's output-styles/ directory. This app " +
      "writes the first of those; the Projects tab shows what a registered " +
      "project's .claude/output-styles/ holds.")));
  return card;
}

/* ------------------------------------------------------------------- form */

function styleFieldHelp(f) {
  const head = el("div.pop-head", {}, el("code.skey", { text: f.key }));
  head.append(badge(f.type === "bool" ? "boolean" : "string", "outline"));
  if (f.type === "bool") head.append(badge("default: false", "outline"));
  return el("div.popover", { role: "dialog", "aria-label": f.key },
    head,
    el("div.pop-body", { text: f.help }),
    el("div.pop-foot", {},
      el("a.btn.btn-sm", { href: STYLE_DOC, target: "_blank", rel: "noreferrer" },
        icon("link"), el("span", { text: "Docs · output-styles" }))));
}

function styleTextField(st, f, onInput) {
  const input = el("input", {
    type: "text", value: st[f.key] || "", spellcheck: f.key === "description",
    oninput: () => { st[f.key] = input.value; if (onInput) onInput(); },
  });
  return el("div.formfield", {},
    el("div.row-flex", { style: { gap: ".25rem" } },
      el("span.flabel", { text: f.key }),
      infoTrigger(f.key, () => styleFieldHelp(f))),
    input,
    el("div.field-hint", { text: f.desc }));
}

function styleBoolField(st, f, disabled) {
  const box = el("input", {
    type: "checkbox", checked: !!st.flags[f.key], disabled: !!disabled,
    onchange: () => { st.flags[f.key] = box.checked; },
  });
  const row = el("label.cl-row", { class: disabled ? "off" : "" }, box,
    el("div.stack", { style: { gap: ".15rem" } },
      el("div.row-flex", { style: { gap: ".25rem" } },
        el("span", { style: { fontSize: ".8125rem" }, text: f.key }),
        infoTrigger(f.key, () => styleFieldHelp(f))),
      el("div.field-hint", {
        text: disabled
          ? f.desc + " — not available here: this app writes to your config " +
            "dir, which is not a plugin, so Claude Code would ignore it."
          : f.desc,
      })));
  return row;
}

const STYLE_STEPS = ["Start", "Fields", "Instructions", "Review"];

function styleStepBar(st) {
  const bar = el("div.row-flex", { style: { gap: ".375rem" } });
  STYLE_STEPS.forEach((label, i) => {
    const n = i + 1;
    bar.append(badge((n === st.step ? "" : n + ". ") + label,
                     n === st.step ? "default" : "outline"));
  });
  return bar;
}

function renderStyleForm(view) {
  const st = NEWSTYLE;

  const card = el("div.card");
  card.append(el("div.card-header", {},
    el("div.stack", { style: { gap: ".375rem" } },
      el("span.card-title", { text: "New output style" }),
      styleStepBar(st)),
    el("div.card-action", {},
      mkbtn("btn-sm btn-ghost", "Cancel", () => {
        closeNewStyle();
        renderInventory();
      }))));

  const content = el("div.card-content", { class: "stack", style: { gap: ".75rem" } });
  card.append(content);

  if (st.loading) {
    content.append(el("div.muted", { text: "Loading presets…" }));
    view.append(card);
    return;
  }

  const field = (key) => st.fields.find((f) => f.key === key) || { key, desc: "", help: "" };

  if (st.step === 1) {
    content.append(el("div.card-description", {
      text: "Start from a preset shipped with this app, or from a blank file. " +
            "A preset fills in every field on the next steps; you can change " +
            "any of it before anything is written.",
    }));
    const pick = (id) => {
      st.preset = id;
      const p = st.presets.find((x) => x.id === id);
      st.name = p ? p.name : "";
      st.description = p ? p.description : "";
      st.body = p ? p.body : "";
      st.flags["keep-coding-instructions"] = p ? !!p["keep-coding-instructions"] : false;
      st.flags["force-for-plugin"] = false;
      st.fileEdited = false;
      st.file = "";
      renderInventory();
    };
    const opts = el("div.stack", { style: { gap: ".25rem" } });
    for (const p of st.presets) {
      opts.append(el("label.cl-row", {},
        el("input", { type: "radio", name: "stylepreset", checked: st.preset === p.id,
                      onchange: () => pick(p.id) }),
        el("div.stack", { style: { gap: ".15rem" } },
          el("span", { style: { fontSize: ".8125rem", fontWeight: "600" }, text: p.name }),
          el("div.field-hint", { text: p.description }))));
    }
    opts.append(el("label.cl-row", {},
      el("input", { type: "radio", name: "stylepreset", checked: st.preset === "",
                    onchange: () => pick("") }),
      el("div.stack", { style: { gap: ".15rem" } },
        el("span", { style: { fontSize: ".8125rem", fontWeight: "600" }, text: "Blank style" }),
        el("div.field-hint", { text: "Start with empty fields and write your own instructions." }))));
    content.append(opts);
  }

  if (st.step === 2) {
    const fileLine = el("div.field-hint");
    const showFile = () => {
      const f = styleFileName(st);
      fileLine.textContent = f
        ? "Will be written to " + DATA.config_dir + "/output-styles/" + f + ".md"
        : "Give the style a name, or set the file name on the review step.";
    };
    content.append(
      styleTextField(st, field("name"), showFile),
      styleTextField(st, field("description")),
      fileLine,
      el("div.separator.separator-h"),
      styleBoolField(st, field("keep-coding-instructions")),
      styleBoolField(st, field("force-for-plugin"), true));
    showFile();
  }

  if (st.step === 3) {
    content.append(el("div.card-description", {
      text: "This text is added to Claude Code's system prompt as written. " +
            "Say how you want responses shaped; there is no format to follow.",
    }));
    const ta = el("textarea", {
      class: "mono", rows: 18, value: st.body, spellcheck: false,
      placeholder: "Lead with the next action. Number multi-step work…",
      oninput: () => { st.body = ta.value; },
    });
    content.append(ta);
  }

  if (st.step === 4) {
    const nameInput = el("input", {
      type: "text", value: styleFileName(st), spellcheck: false,
      oninput: () => {
        st.fileEdited = true;
        st.file = styleSlug(nameInput.value);
      },
      onblur: () => { nameInput.value = styleFileName(st); },
    });
    content.append(
      el("div.formfield", {},
        el("span.flabel", { text: "file name" }),
        nameInput,
        el("div.field-hint", {
          text: "Written to " + DATA.config_dir + "/output-styles/<name>.md. With no " +
                "name: field set, this is what the style is called.",
        })),
      el("div.separator.separator-h"),
      el("div.flabel", { text: "file contents" }),
      el("pre.code-pane", { text: styleFileText(st) }),
      el("div.field-hint", {
        text: "Claude Code reads its output style once, at session start — run " +
              "/clear or start a new session before this takes effect.",
      }));
  }

  const foot = el("div.card-footer", {});
  if (st.step > 1) {
    foot.append(mkbtn("btn-sm", "Back", () => { st.step--; renderInventory(); }));
  }
  const spacer = el("div", { style: { marginLeft: "auto" } });
  foot.append(spacer);
  if (st.step < 4) {
    const next = mkbtn("btn-sm btn-primary", "Next", () => {
      if (st.step === 2 && !styleFileName(st)) {
        toast("Give the style a name first — the file name comes from it.", true);
        return;
      }
      st.step++;
      renderInventory();
    });
    foot.append(next);
  } else {
    foot.append(mkbtn("btn-sm btn-primary", st.busy ? "Creating…" : "Create", () => {
      if (!st.busy) createStyle(st);
    }));
  }
  card.append(foot);
  view.append(card);
}

async function createStyle(st) {
  const file = styleFileName(st);
  if (!file) return toast("Give the style a file name first.", true);
  if (!st.body.trim()) {
    return toast("An output style with no instructions would do nothing.", true);
  }
  st.busy = true;
  renderInventory();
  try {
    await api("/api/item-create", {
      type: "output-styles", name: file, content: styleFileText(st), enabled: true,
    });
    closeNewStyle();
    await refresh();
    toast("Created " + file + " — it applies to new sessions, not this one.");
  } catch (e) {
    st.busy = false;
    renderInventory();
    toast(e.message, true);
  }
}

async function openNewStyle() {
  NEWSTYLE = newStyleState();
  renderInventory();
  try {
    const d = await api("/api/output-style-presets");
    if (!NEWSTYLE) return;   // cancelled while the fetch was in flight
    NEWSTYLE.presets = d.presets || [];
    NEWSTYLE.fields = d.fields || [];
    NEWSTYLE.doc = d.doc || STYLE_DOC;
  } catch (e) {
    // a blank style is still worth offering, so degrade instead of bailing
    toast("Could not load presets: " + e.message, true);
  }
  if (!NEWSTYLE) return;
  NEWSTYLE.loading = false;
  renderInventory();
}
