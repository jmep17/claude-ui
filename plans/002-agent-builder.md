# Plan 002: Build an agent from a guided form, with the reference docs beside it

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 49ae8ba..HEAD -- bin/claude_ui/static/ bin/claude_ui/server.py bin/claude_ui/items.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED (new UI surface; no automated browser tests exist in this repo,
  so verification is manual and the checks below must be run by hand)
- **Depends on**: `plans/001-item-create-backend.md` — this plan calls
  `POST /api/item-create`, which 001 adds. Do not start until 001 is DONE.
- **Category**: dx
- **Planned at**: commit `49ae8ba`, 2026-08-06

## Why this matters

An agent file is fifteen lines of YAML, and almost every field is a thing you
have to look up: which model aliases exist, which tools a subagent can be given,
what `permissionMode` accepts, that `skills:` preloads a skill's full text while
`tools: Skill` merely permits discovery. Claude Code dropped its own interactive
agent wizard in v2.1.198 — `/agents` now prints a note telling you to ask Claude
or edit the directory yourself. So the guided path does not exist anywhere.

This plan adds it to the Agents tab: a **New agent** view with a form on the
left, the official field reference and worked examples on the right, and a live
preview of the exact file that will be written. The skills picker is the part
that earns its keep — it lists the skills actually on this machine, greys out the
ones Claude Code refuses to preload, and writes them into `skills:` for you.

## Current state

### Where the UI lives

The frontend is plain files under `bin/claude_ui/static/`, no build step, loaded
in a fixed order by `bin/claude_ui/static/index.html:83-85`:

```html
<script src="/ui.js"></script>
<script src="/editor.js"></script>
<script src="/app.js"></script>
```

Files are served from an explicit allowlist in `bin/claude_ui/server.py:36-37`:

```python
_STATIC_NAMES = ("theme.css", "components.css", "app.css", "ui.js", "editor.js",
                 "app.js")
```

Adding a new static file means adding its name there **and** a `<script>` tag in
`index.html`. There is no other routing to change.

### The Agents tab today

`agents` is one of four item tabs (`bin/claude_ui/static/app.js:12`):

```js
const ITEM_TABS = ["skills", "commands", "agents", "output-styles"];
```

All four render through one function, `renderInventory()`
(`bin/claude_ui/static/app.js:2541-2619`). Its toolbar is built at lines
2557-2570:

```js
  const inp = el("input", {
    type: "search", id: "iq", placeholder: "Filter " + TAB + " by name or description…",
    value: IQ,
    oninput: (e) => {
      IQ = inp.value;
      if (e.isComposing) return;
      refilter("iq", renderInventory);
    },
  });
  view.append(el("div.toolbar", {}, inp,
    el("div.toolbar-end", {},
      el("span.muted", { style: { fontSize: ".72rem" },
        text: on.length + " enabled · " + off.length + " disabled"
          + (items.length !== all.length ? " · " + items.length + " of " + all.length + " shown" : "") }))));
```

The top-level dispatcher is `render()` (`bin/claude_ui/static/app.js:3013-3040`):

```js
function render() {
  closeDropdown();
  renderHeader();
  renderTabs();
  const views = { settings: "settingsview", mcp: "mcpview", statusline: "stlview",
    setup: "setupview", insight: "insightview", costs: "costsview", doctor: "doctorview",
    plugins: "pluginsview", backup: "backupview" };
  const isEditor = !!EDITING;
  document.getElementById("editorview").hidden = !isEditor;
  document.getElementById("itemsview").hidden = isEditor || !ITEM_TABS.includes(TAB);
  ...
  if (ITEM_TABS.includes(TAB)) { renderInventory(); return; }
```

`refresh()` reloads `/api/state` into `DATA` and re-renders
(`bin/claude_ui/static/app.js:3042-3046`).

### The data you already have

`DATA.items.skills` is an array of skill rows from `scan_items("skills")`. After
plan 001 each row has: `name`, `enabled`, `description`, `path`, `broken`,
`incomplete`, `source`, `no_model_invoke`. `DATA.mcp.servers` is an array of
`{name, enabled, config}`. Neither needs a new fetch.

### The primitives you must reuse

All from `bin/claude_ui/static/ui.js`. **Nothing in this app builds a dialog,
menu, toast, combobox or colour by hand** — the file header at
`bin/claude_ui/static/app.js:1-8` says so explicitly, and it is the single most
important convention in the repo.

- `el(spec, props, ...kids)` (`ui.js:25`) — `el("div.card")`,
  `el("button.btn", {onclick, text})`. `spec` is `tag.class.class`.
- `icon(name)` (`ui.js:117`) — from the inlined set in `ICONS` (`ui.js:58`).
  Confirm a name exists in `ICONS` before using it.
- `badge(text, variant)` (`ui.js:569`) — variants `secondary` (default),
  `outline`, `warning`, `destructive`, `success`.
- `sectionTitle(text, count)` (`ui.js:577`).
- `emptyState(title, hint, iconName)` (`ui.js:560`).
- `toast(msg, isErr, action)` (`ui.js:398`).
- `modal({title, text, fields, ok, cancel, danger, wide})` (`ui.js:446`) —
  resolves to a `{fieldId: value}` object, or `null` on cancel.
- `checklist({groups, hint})` (`ui.js:604`) — **this is the component the skills
  and tools pickers are built from.** Its contract, from the docstring at
  `ui.js:597-603`:

  > Groups are `{label, rows: [{value, name, desc, badges, disabled, reason,
  > extra}]}`; rows that can't be picked render greyed with their reason in
  > place of a checkbox. `extra` is an optional node placed at the end of the
  > row.

  The node exposes a read-only `.value` getter returning the array of checked
  values (`ui.js:646-648`). Rows default to checked; pass `checked: false` to
  start unchecked.
- `filterSelect(sel)` (`ui.js:745`) — wraps a populated `<select>` in a
  type-to-filter combobox once it has more than `FSEL_MIN` (6) options. Below
  that the native control is kept. `modal()` already applies this to its
  `select` fields (`ui.js:496`); when you build a `<select>` outside a modal and
  it has more than 6 options, wrap it yourself.
- `md2html(src)` (`bin/claude_ui/static/editor.js:520`) — the markdown renderer
  behind the editor's preview pane. Output goes into a `.mdprev` container,
  which is already styled (`app.css:931-976`) including tables and fenced code
  with language labels.

### CSS you already have

- `.card` / `.card-header` / `.card-title` / `.card-description` /
  `.card-content` / `.card-footer` (`components.css:383-410`)
- `.code-pane` (`components.css:1041`) — the monospace block used for the assist
  output at `editor.js:948`
- `.mdprev` (`app.css:931`) — rendered-markdown container
- `.view-head` (`app.css:109`) — the one-paragraph explainer at the top of every
  view
- `.toolbar` / `.toolbar-end`, `.list` / `.list-item` / `.li-main` /
  `.li-name` / `.li-desc` / `.li-actions`
- `.mrow` + `.mrow > label` + `.field-hint` (`app.css:987-990`) — the labelled
  form row used inside modals. Reuse it for the builder's form rows.
- `.ed-body-split { grid-template-columns: 1fr 1fr; }` with a
  `@media (max-width: 900px)` collapse to one column (`app.css:715-717`). Your
  two-column layout must collapse the same way at the same breakpoint.

### The authoritative agent file format

From <https://code.claude.com/docs/en/sub-agents>, fetched 2026-08-06. **Only
`name` and `description` are required.** The full field list:

| Field | Notes |
|---|---|
| `name` | lowercase letters and hyphens; must not contain `:` — Claude Code refuses to load such a file |
| `description` | when Claude should delegate to this agent |
| `tools` | comma-separated allowlist; omit to inherit everything |
| `disallowedTools` | comma-separated denylist; applied before `tools` |
| `model` | `sonnet`, `opus`, `haiku`, `fable`, a full model ID, or `inherit` (the default) |
| `permissionMode` | `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`, `manual` |
| `maxTurns` | integer |
| `skills` | YAML list; preloads each skill's **full content** at startup |
| `mcpServers` | YAML list of already-configured server names, or inline definitions |
| `hooks` | lifecycle hooks scoped to this agent |
| `memory` | `user`, `project`, or `local` |
| `background` | `true` to always run in the background |
| `effort` | `low`, `medium`, `high`, `xhigh`, `max` |
| `isolation` | `worktree` |
| `color` | `red`, `blue`, `green`, `yellow`, `purple`, `orange`, `pink`, `cyan` |
| `initialPrompt` | auto-submitted first turn when run as the main session agent |

The canonical example, quoted verbatim from the docs:

```markdown
---
name: code-reviewer
description: Reviews code for quality and best practices
tools: Read, Glob, Grep
model: sonnet
---

You are a code reviewer. When invoked, analyze the code and provide
specific, actionable feedback on quality, security, and best practices.
```

And the `skills` example, verbatim:

```markdown
---
name: api-developer
description: Implement API endpoints following team conventions
skills:
  - api-conventions
  - error-handling-patterns
---

Implement API endpoints. Follow the conventions and patterns from the preloaded skills.
```

Three facts from the docs that the UI must state, because they are the ones
people get wrong:

1. `skills:` **preloads full skill content**. It does not control access — an
   agent can still invoke unlisted skills through the `Skill` tool. To stop that,
   omit `Skill` from `tools` or put it in `disallowedTools`.
2. A skill with `disable-model-invocation: true` **cannot be preloaded**, because
   preloading draws from the same pool Claude can invoke. A listed skill that is
   missing or disabled is skipped with a debug-log warning and no visible error.
3. Subagents run in the background by default as of v2.1.198, and a background
   subagent keeps only these built-in tools regardless of its `tools` list:
   `Read`, `Grep`, `Glob`, `Bash`, `PowerShell`, `Edit`, `Write`, `NotebookEdit`,
   `WebFetch`, `WebSearch`, `TodoWrite`, `Skill`, `ToolSearch`, `EnterWorktree`,
   `ExitWorktree`, `Monitor`, `TaskStop`, `SendMessage`, `Artifact` (plus every
   MCP tool).

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests | `python3 -m unittest discover tests` | `OK`, exit 0 |
| Syntax check (JS) | `node --check bin/claude_ui/static/agents.js` | exit 0, no output |
| Syntax check (Python) | `python3 -m compileall -q bin/claude_ui` | exit 0, no output |
| Run the app | `bin/claude-ui --no-open --port 7455` | prints `claude-ui: http://127.0.0.1:7455` |

There is no JS linter, bundler, or frontend test runner in this repo. Do not add
one. `node --check` is a syntax check only and is the only JS gate available; if
`node` is absent on the machine, load the page and confirm the browser console is
clean instead, and say so in your report.

## Scope

**In scope**:

- `bin/claude_ui/static/agents.js` (create)
- `bin/claude_ui/static/index.html` (modify — one `<script>` tag)
- `bin/claude_ui/server.py` (modify — one entry in `_STATIC_NAMES`)
- `bin/claude_ui/static/app.js` (modify — the three small hooks in step 6)
- `bin/claude_ui/static/app.css` (modify — append the new layout classes)

**Out of scope** (do NOT touch):

- `bin/claude_ui/static/theme.css` — no new colours. Every surface uses an
  existing token.
- `bin/claude_ui/static/components.css` — no new component primitives. If you
  believe you need one, that is a STOP condition.
- `bin/claude_ui/static/ui.js` — reuse `checklist`, `modal`, `filterSelect` as
  they are. Do not extend them.
- `bin/claude_ui/static/editor.js` — the editor-side frontmatter form and docs
  panel are plan 003.
- `bin/claude_ui/items.py`, `doctor.py` — plans 001 and 004.
- Creating skills, commands or output styles from the UI. The **New** button
  goes on the Agents tab only. The backend from 001 is generic, but the guided
  form is agent-shaped and a half-guided form for the other three types is worse
  than none.

## Git workflow

- Branch: `advisor/002-agent-builder`
- Commit messages: imperative, sentence case, describing the user-visible change.
  Examples from `git log`: `Back up the config you would otherwise rebuild by
  hand`, `Show and set the model a plugin's agents run on`. Suggested:
  `Build an agent from a form, with the field reference beside it`.
- Do NOT push or open a PR.

## Steps

### Step 1: Register the new static file

1. `bin/claude_ui/server.py:36-37` — add `"agents.js"` to `_STATIC_NAMES`,
   before `"app.js"`:

```python
_STATIC_NAMES = ("theme.css", "components.css", "app.css", "ui.js", "editor.js",
                 "agents.js", "app.js")
```

2. `bin/claude_ui/static/index.html:84` — add the script tag between
   `editor.js` and `app.js`:

```html
<script src="/editor.js"></script>
<script src="/agents.js"></script>
<script src="/app.js"></script>
```

Order matters: `agents.js` calls `md2html` from `editor.js` and reads `DATA` and
`api` from `app.js`. Only the latter two are `let`/`function` at app.js top
level; because `agents.js` only reads them from inside functions that run after
load, the order above is correct. Do not move `agents.js` after `app.js`.

3. Create `bin/claude_ui/static/agents.js` with just a file header comment for
   now, in the house voice (see `app.js:1-8` and `editor.js`'s section banners
   for the style).

**Verify**: start the server (`bin/claude-ui --no-open --port 7455`), then
`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7455/agents.js` → `200`.
Stop the server afterwards.

### Step 2: Write the YAML emitter and the file composer

In `agents.js`, add the two functions that turn form state into the exact file
text. **This is the only place in the app that generates agent YAML** — the
preview and the saved file must be the same string.

```js
/* YAML is only as forgiving as the parser reading it, and the parser reading
   these files is Claude Code's, not ours. A description containing a colon —
   "Reviews code: quality, security" — is a parse error unquoted, so anything
   that could be read as structure gets double-quoted and escaped. Plain values
   are left plain, because a file you can hand-edit afterwards is the point. */
function yamlScalar(v) {
  const s = String(v == null ? "" : v);
  if (s === "") return '""';
  if (/^[\w][\w .,'()/+-]*$/.test(s) && !/^(true|false|null|yes|no|on|off)$/i.test(s)
      && !/^-?\d/.test(s))
    return s;
  return '"' + s.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
}
```

Then the composer. It takes a plain state object and returns the whole file:

```js
/* Emit only what the user actually set. Claude Code defaults every optional
   field sensibly (model is `inherit`, tools inherit the full pool), and a file
   full of `permissionMode: default` teaches the reader nothing and drifts the
   moment a default changes. */
function agentFileText(a) {
  const fm = [];
  const put = (k, v) => { if (v !== "" && v != null) fm.push(k + ": " + yamlScalar(v)); };
  const putList = (k, arr) => {
    if (arr && arr.length) fm.push(k + ":\n" + arr.map((x) => "  - " + yamlScalar(x)).join("\n"));
  };
  put("name", a.name);
  put("description", a.description);
  if (a.tools && a.tools.length) put("tools", a.tools.join(", "));
  put("model", a.model);
  putList("skills", a.skills);
  putList("mcpServers", a.mcpServers);
  put("permissionMode", a.permissionMode);
  put("color", a.color);
  return "---\n" + fm.join("\n") + "\n---\n\n" + (a.prompt || "").trim() + "\n";
}
```

Field order is deliberate and matches the docs' own example: identity, then
capability, then presentation. Keep it.

**Verify**: `node --check bin/claude_ui/static/agents.js` → exit 0. Then run
this one-liner and confirm the output exactly matches:

```sh
node -e '
const fs=require("fs");eval(fs.readFileSync("bin/claude_ui/static/agents.js","utf8"));
process.stdout.write(agentFileText({name:"code-reviewer",description:"Reviews code: quality and best practices",tools:["Read","Glob","Grep"],model:"sonnet",skills:["api-conventions"],prompt:"You are a code reviewer."}));'
```

Expected, byte for byte:

```
---
name: code-reviewer
description: "Reviews code: quality and best practices"
tools: Read, Glob, Grep
model: sonnet
skills:
  - api-conventions
---

You are a code reviewer.
```

If `node` is unavailable, paste the two functions into the browser console on
the running app and check the same output there.

### Step 3: Build the reference-docs sidebar

Still in `agents.js`. Add a module-level constant holding the reference content
as markdown strings, rendered through `md2html`:

```js
/* The docs, inlined. Not fetched: this app is offline-first by design (the
   settings schema is vendored for the same reason), and the one thing you need
   while writing an agent is the one thing you can't go and look up mid-form. */
const AGENT_DOCS = [
  { id: "fields", title: "Every field", body: `...markdown table...` },
  { id: "skills", title: "Preloading skills", body: `...` },
  { id: "tools", title: "Tools and permissions", body: `...` },
  { id: "examples", title: "Worked examples", body: `...` },
];
```

Content requirements — write these from the "The authoritative agent file
format" section above, which is the fetched source of truth. Do not invent
fields, values, or behaviour beyond what is recorded there.

- **fields**: the full markdown table of all 16 fields, with `name` and
  `description` marked required and everything else optional.
- **skills**: the three facts numbered in "Current state" — preload injects full
  content and is not an access control; `disable-model-invocation` skills can't
  be preloaded; a missing or disabled skill is silently skipped. Include the
  `api-developer` example verbatim.
- **tools**: that omitting `tools` inherits everything; that `disallowedTools`
  is applied first and a tool in both is removed; and the background-subagent
  tool list, with the note that the same file resolves to different tools in the
  foreground and the background.
- **examples**: the `code-reviewer` example verbatim, plus two more built from
  the same documented fields — a read-only researcher
  (`tools: Read, Grep, Glob, Bash`) and a skills-preloading implementer. Each
  example gets a **Use this** button that loads it into the form (wired in step
  5).

Render it as a card with a section list:

```js
function agentDocsPanel(onExample) {
  const box = el("div.card.agent-docs");
  box.append(el("div.card-header", {},
    el("div.card-title", { text: "How to write an agent" }),
    el("div.card-description", { text: "From the Claude Code subagent docs. Nothing here is fetched — it ships with the app." })));
  const content = el("div.card-content.tight");
  for (const s of AGENT_DOCS) {
    const d = el("details.agent-doc", { open: s.id === "fields" });
    d.append(el("summary", { text: s.title }));
    const md = el("div.mdprev");
    md.innerHTML = md2html(s.body);
    d.append(md);
    content.append(d);
  }
  box.append(content);
  return box;
}
```

Wire the **Use this** buttons after `innerHTML` is set, by querying for a marker
you put in the markdown — the simplest reliable approach is to render each
example's button as a separate `el("button.btn.btn-sm", …)` appended *after* its
`.mdprev` block rather than inside the markdown. Do that; do not parse the
rendered HTML.

**Verify**: manual. Load the app, open the Agents tab, click **New agent**
(built in step 4) and confirm: four collapsible sections, the first open, the
tables rendered as tables, fenced blocks showing a `markdown`/`yaml` language
label, and no console errors.

### Step 4: Build the form and the live preview

Add module state and the view function:

```js
let ANEW = null;   // the open builder's state, or null
```

`ANEW` shape: `{name, description, model, permissionMode, color, tools: [],
skills: [], mcpServers: [], prompt}`.

`renderAgentNew(host)` appends into the `#itemsview` element and builds, in a
two-column grid (form left, `agentDocsPanel()` right):

**Form rows** — each is a `.mrow` with a `<label>` and a `.field-hint`, matching
`app.css:987-990`:

1. `name` — text input. On input, slugify into the shape the docs require:
   lowercase, spaces and underscores to hyphens, strip anything outside
   `[a-z0-9-]`, collapse repeated hyphens. Show the resulting filename under it:
   `<config_dir>/agents/<name>.md`. If the name collides with an existing agent
   in `DATA.items.agents` (either enabled or disabled), show a destructive-badge
   warning inline and disable the Create button — the backend refuses this
   anyway (plan 001), but finding out after typing a prompt is a bad trade.
2. `description` — textarea, 2 rows. Hint: "When should Claude delegate to this
   agent? This is the only thing Claude reads when deciding." Show a live
   character count; there is no documented hard limit for agents, so do not
   invent one — just show the count.
3. `model` — `<select>`: `(inherit — default)` with value `""`, then `sonnet`,
   `opus`, `haiku`, `fable`, `inherit`. Five real options plus the blank is at
   the `FSEL_MIN` boundary; leave it as a native select.
4. `tools` — a `checklist` in one group per category, rendered inline in the
   form (not in a modal). Categories and members, taken from the documented
   background-safe list plus the tools named elsewhere in the docs:
   - *Read-only*: `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`
   - *Write*: `Edit`, `Write`, `NotebookEdit`
   - *Execute*: `Bash`, `PowerShell`, `Monitor`
   - *Agent*: `Skill`, `ToolSearch`, `Agent`, `TodoWrite`, `SendMessage`,
     `TaskStop`, `EnterWorktree`, `ExitWorktree`, `Artifact`
   Every row starts **unchecked**, and the hint above the checklist reads: "Leave
   everything unticked to inherit the full tool pool — that is the default and
   usually the right answer. Tick tools only to restrict this agent." Below it,
   show a live line: when nothing is ticked, "tools: omitted — inherits
   everything"; otherwise the emitted `tools:` line.
5. `skills` — the picker. Built from `DATA.items.skills` as a `checklist` with
   these groups, all rows starting unchecked:
   - **"Your skills"** — enabled, not broken, not incomplete, and
     `no_model_invoke` false. `name` is the skill name, `desc` its description,
     and add a `badge("from plugin", "outline")` when `source` is set.
   - **"Can't be preloaded"** — everything else, each row `disabled: true` with a
     specific `reason`: `"disabled — move it back to skills/ first"` when
     `!enabled`; `"disable-model-invocation: true — only you can run it"` when
     `no_model_invoke`; `"broken symlink"` / `"no SKILL.md"` for the other two.
     Omit this group entirely when it has no rows.
   Above the checklist, the hint: "Preloading injects the skill's **full text**
   into the agent at startup — real context cost. It is not an access control:
   the agent can still invoke any other skill through the Skill tool." If
   `DATA.items.skills` is empty, render `emptyState("No skills on this machine",
   "Anything in " + DATA.config_dir + "/skills shows up here.", "sparkles")`
   in place of the checklist.
6. `mcpServers` — a `checklist` over `DATA.mcp.servers`, enabled ones selectable
   and disabled ones greyed with reason `"disabled in ~/.claude.json"`. Omit the
   whole row when there are no servers.
7. `permissionMode` — `<select>`, blank (`""`) plus the seven documented values.
   Wrap it in `filterSelect` since it exceeds `FSEL_MIN`.
8. `color` — `<select>`, blank plus the eight documented colours.
9. `prompt` — textarea, 12 rows, monospace (`class: "mono"`). Label: "System
   prompt". Hint: "This becomes the agent's entire system prompt. It does not
   inherit Claude Code's."

Every control writes straight into `ANEW` on `input`/`change` and then calls a
single `agentNewSync()` that re-renders only the preview and the Create button's
disabled state. **Do not re-render the whole view on every keystroke** — the
textarea would lose its caret. Follow the pattern the statusline view uses:
targeted node updates, full re-render only on structural change.

**Preview** — a `.code-pane` below the form (or in the right column under the
docs on wide screens; below the form is fine and simpler), holding
`agentFileText(ANEW)` verbatim, with a heading `The file that will be written`
and the resolved path beneath it.

**Footer** — a `.toolbar` with a primary **Create agent** button, a **Cancel**
button that sets `ANEW = null` and calls `render()`, and nothing else.

**Verify**: manual, in the running app. Type a name with spaces and capitals and
confirm it slugifies. Tick two skills and confirm the preview grows a `skills:`
block with two `  - ` entries. Untick everything under tools and confirm the
`tools:` line vanishes from the preview.

### Step 5: Wire create, and the example buttons

```js
async function agentNewCreate() {
  const a = ANEW;
  if (!a.name || !a.description.trim()) {
    toast("An agent needs a name and a description", true);
    return;
  }
  try {
    const r = await api("/api/item-create", {
      type: "agents", name: a.name, content: agentFileText(a), enabled: true,
    });
    ANEW = null;
    await refresh();
    toast(a.name + " created · applies to new sessions");
    openItemEditor("agents", a.name, null, true);
  } catch (e) { toast(e.message, true); }
}
```

Opening the editor on success is deliberate: the form gets you a correct file,
and the prompt is the part you will keep working on. `openItemEditor` is defined
in `editor.js:63` and is global.

The **Use this** buttons from step 3 set `ANEW` to the example's field values —
keeping any name and description the user has already typed — and re-render.

**Verify**: manual. Create an agent named `plan-002-smoke`, confirm the toast,
confirm the editor opens on it, then check the file on disk:
`cat "$(python3 -c "import sys;sys.path.insert(0,'bin');from claude_ui.core import config_dir;print(config_dir())")/agents/plan-002-smoke.md"`
→ matches the preview exactly. Delete it by hand afterwards.

### Step 6: Hook it into the Agents tab

Three edits in `bin/claude_ui/static/app.js`:

1. In `renderInventory()`, immediately after `view.innerHTML = "";`
   (line 2550), hand off when the builder is open:

```js
  if (TAB === "agents" && ANEW) { renderAgentNew(view); return; }
```

2. In the same function's toolbar (lines 2566-2570), add a **New agent** button
   into the `.toolbar-end`, only on the agents tab:

```js
      TAB === "agents" ? mkbtn("btn-sm btn-primary", "New agent", () => {
        ANEW = agentNewBlank();
        renderInventory();
      }) : null,
```

`mkbtn(cls, label, onclick, title)` is at `app.js:291`. `el()` ignores `null`
children, so the conditional is safe.

3. In `goTab()` (`app.js:45-51`), discard an open builder when the user leaves
   the tab, so it does not reappear later out of context:

```js
  ANEW = null;
```

Add it beside the existing `EDITING = null;` line.

Do **not** add hash routing for the builder. `#agents` returning you to the
inventory is correct: a half-filled form is not a place you should be able to
deep-link into or reload back onto.

**Verify**: manual. Open the app, press `3` to reach Agents, confirm the **New
agent** button appears there and on no other item tab. Open the builder, switch
to Skills and back, confirm the builder is gone.

### Step 7: Add the layout CSS

Append to the end of `bin/claude_ui/static/app.css`, under a section banner
comment matching the file's existing style:

```css
/* ------------------------------------------------------------ agent builder */

.agent-new { display: grid; gap: 0.875rem; grid-template-columns: 1fr 22rem; align-items: start; }
@media (max-width: 900px) { .agent-new { grid-template-columns: 1fr; } }

.agent-docs { position: sticky; top: 0.75rem; max-height: calc(100vh - 6rem); overflow-y: auto; }
.agent-doc { border-bottom: 1px solid var(--border); padding: 0.5rem 0; }
.agent-doc:last-child { border-bottom: 0; }
.agent-doc > summary { cursor: pointer; font-size: 0.8125rem; font-weight: 600; padding: 0.25rem 0; }
.agent-doc[open] > summary { margin-bottom: 0.25rem; }
```

Use only existing tokens (`--border`, `--muted-foreground`, `--card`, …). No
literal colours anywhere — `grep -n "#[0-9a-fA-F]\{3,6\}" bin/claude_ui/static/app.css`
must return no more matches than it did before your change.

The 900px breakpoint is not arbitrary: it is the same one `.ed-body-split` and
`ED_NARROW()` (`editor.js:1009`) already use. Do not pick a different one.

**Verify**: manual. Resize the window below 900px and confirm the docs panel
moves below the form rather than shrinking into an unreadable column.

### Step 8: Full check

**Verify**:
- `python3 -m unittest discover tests` → `OK` (nothing here should change the
  Python tests; if one fails, that is a STOP condition)
- `node --check bin/claude_ui/static/agents.js` → exit 0
- `python3 -m compileall -q bin/claude_ui` → exit 0
- Load the app, open every tab in turn, and confirm the browser console is
  clean.

## Test plan

This repo has no frontend test runner and this plan does not add one — adding a
JS toolchain to a deliberately zero-dependency, no-build-step app is a much
larger decision than this feature warrants.

Verification is therefore:

- The `node -e` output check in step 2, which covers the one piece of pure logic
  worth testing (`yamlScalar` + `agentFileText`) including the colon-in-
  description case that silently produces an unloadable file.
- The manual checks listed in each step. Run all of them and report the result
  of each.
- `python3 -m unittest discover tests` must stay green.

If you want more automated coverage, the honest follow-up is to move
`agentFileText` into Python and test it there — but that splits YAML generation
across two languages, which step 2 explicitly rejects. Do not do it here.

## Done criteria

ALL must hold:

- [ ] `node --check bin/claude_ui/static/agents.js` exits 0
- [ ] `python3 -m compileall -q bin/claude_ui` exits 0
- [ ] `python3 -m unittest discover tests` exits 0
- [ ] The step 2 `node -e` output matches the expected block byte for byte
- [ ] `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<port>/agents.js` returns `200`
- [ ] A **New agent** button appears on the Agents tab and on no other tab
- [ ] Creating an agent writes a file whose content equals the preview exactly
- [ ] The skills picker lists every skill on the machine, with unpickable ones
      greyed and carrying a specific reason
- [ ] The docs panel renders four sections and moves below the form under 900px
- [ ] `grep -rn "mcp__\|CLAUDE_CODE" bin/claude_ui/static/agents.js` finds no
      hard-coded machine-specific values
- [ ] `git status --porcelain` lists only the five in-scope files and `plans/`
- [ ] `plans/README.md` status row for 002 updated

## STOP conditions

Stop and report back (do not improvise) if:

- `POST /api/item-create` does not exist — plan 001 has not landed. Do not
  implement the backend yourself.
- `checklist()` in `ui.js` no longer exposes a `.value` array getter, or no
  longer supports `disabled` + `reason` rows. The skills and tools pickers rest
  entirely on that contract, and rebuilding it inline is out of scope.
- `md2html` is not defined globally from `editor.js`.
- You conclude you need a new component in `components.css` or a change to
  `ui.js`. Report what you needed and why.
- `DATA.items.skills` rows have no `no_model_invoke` key — plan 001 step 1 was
  skipped.
- A Python test that passed before your change fails after it.

## Maintenance notes

- **The field list will drift.** `AGENT_DOCS` and the tools checklist are a
  snapshot of <https://code.claude.com/docs/en/sub-agents> as of 2026-08-06.
  This is the same tradeoff the settings tab already makes with its vendored
  JSON Schema (see the README's "Settings help" section), but with no sync
  script, because there is no machine-readable schema for agent frontmatter to
  sync from. When a reviewer sees a new field in the docs, it is a hand edit
  here. Say so in a comment at the top of `AGENT_DOCS`.
- A reviewer should scrutinise `yamlScalar` hardest. Everything else in this
  plan is layout; that function is the one place where a bug produces a file
  Claude Code silently refuses to load.
- Deliberately deferred: `hooks`, `maxTurns`, `memory`, `effort`, `isolation`,
  `background`, `initialPrompt` and `disallowedTools` are documented in the
  sidebar but have no form control. They are rare, and the file the form
  produces is a plain markdown file you can hand-edit — which is exactly what
  the editor is for. If they get controls later, they belong in an "Advanced"
  `<details>` block, not in the main flow.
- The builder is one-shot: there is no draft persistence and no hash route. If
  someone asks for "resume where I left off", that is a real feature request,
  not a bug in this one.
