# Plan 003: Edit an agent's frontmatter as a form, with the reference on hand

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 49ae8ba..HEAD -- bin/claude_ui/static/editor.js bin/claude_ui/static/app.css`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED — this plan rewrites the text in the open editor programmatically.
  A bug here corrupts a file the user is editing. The mitigation is that every
  change goes through the existing dirty/save/conflict path and nothing writes
  to disk on its own.
- **Depends on**: `plans/002-agent-builder.md` — reuses `agentDocsPanel()` and
  `yamlScalar()` from `bin/claude_ui/static/agents.js`, which 002 creates. Do not
  start until 002 is DONE.
- **Category**: dx
- **Planned at**: commit `49ae8ba`, 2026-08-06

## Why this matters

Plan 002 gets you a correct agent file the first time. Every time after that you
are back in a raw textarea, hand-editing YAML — which is where `model: sonnet-5`
(not a real alias), a `skills:` entry naming a skill you since disabled, and a
description with an unquoted colon come from. The same dropdowns and pickers that
made creation safe should make editing safe, and the reference that was beside
the form should be beside the file.

This is idea #7 in `docs/IDEAS.md` ("Frontmatter-aware form editing"), scoped to
agents, where the field vocabulary is largest and least memorable.

## Current state

### The editor

`bin/claude_ui/static/editor.js`. `EDITING` (line 20) holds the open file;
`ED` (line 23) holds the live DOM handles and view state. The relevant shape of
`EDITING` for an item: `{item: true, type, name, file, enabled, path, abs,
content, dirty, readonly, files, mtime, size}`.

`renderEditor()` (`editor.js:885-988`) rebuilds the whole editor chrome. Its
structure, in order: `edHeadline(f)`, then a `.editor-shell` containing an
optional `.ftabs` file switcher, `edToolbar(f, isMd)`, the findings strip
`ED.strip`, the `.ed-body` (textarea pane and/or preview pane), an optional
assist output, and the button `.toolbar`.

The layout control lives in `edToolbar` (`editor.js:868-881`):

```js
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
```

`edSetView(v)` (`editor.js:810-815`) syncs the textarea into `EDITING.content`,
stores the choice in `localStorage`, and re-renders.

### Reading and writing frontmatter

`edFrontmatter(text)` (`editor.js:334-353`) already parses the block and returns
`{meta, order, endLine}`, where `meta` also carries `"@line:<key>"` entries
giving each key's 1-based line:

```js
function edFrontmatter(text) {
  const lines = text.split("\n");
  if (!lines.length || lines[0].trim() !== "---") return null;
  const meta = {}, order = [];
  let key = null, buf = [], endLine = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === "---") { endLine = i; break; }
    const m = lines[i].match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    ...
```

There is no JS *writer*. The Python side has one — `core.set_frontmatter_key`
(`bin/claude_ui/core.py:145-187`) — and its docstring states the design rule you
must copy:

> The inverse of parse_frontmatter() above, and deliberately as blunt: it
> rewrites one line and leaves every other byte alone, so a hand-formatted file
> survives a model change with its comments, ordering and folded blocks intact.

### How text gets into the textarea

`edReplaceAll(next)` (`editor.js:684-693`) — read it before step 2. It is the
only supported way to swap the whole buffer, and it is what the **Use result**
button after an assist calls.

`edChanged()` (`editor.js:794-808`) marks dirty, repaints highlighting, and
schedules the preview and lint refresh.

Saving goes through `saveFile()` (`editor.js:173`), which sends the `base` mtime
and handles the 409 conflict path. **Nothing in this plan may write to disk
directly.**

### The backend endpoint you must NOT use

`POST /api/item-model-set` (`server.py:242-245` → `items.item_set_model`) writes
an agent's `model:` line straight to disk. It exists for the Plugins tab, where
there is no editor open. Calling it from the editor would write behind the
editor's back, leave `EDITING.mtime` stale, and produce a spurious 409 on the
next save. Do not call it from this plan.

### Conventions

- No new component primitives; reuse `ui.js`. See `app.js:1-8`.
- No literal colours; every surface uses a `theme.css` token.
- 900px is the app's one layout breakpoint (`app.css:717`, `editor.js:1009`).
- Comments explain why. Match the voice of `editor.js`'s section banners.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Syntax check | `node --check bin/claude_ui/static/editor.js` | exit 0, no output |
| Tests | `python3 -m unittest discover tests` | `OK`, exit 0 |
| Run the app | `bin/claude-ui --no-open --port 7455` | prints the URL |

No JS linter, bundler or frontend test runner exists in this repo. Do not add one.

## Scope

**In scope**:

- `bin/claude_ui/static/editor.js` (modify)
- `bin/claude_ui/static/agents.js` (modify — export the two helpers this plan
  reuses; small additions only)
- `bin/claude_ui/static/app.css` (modify — append layout classes)

**Out of scope** (do NOT touch):

- Any Python file. This plan is entirely client-side; the save path already
  exists and already handles conflicts.
- `bin/claude_ui/static/ui.js`, `components.css`, `theme.css`.
- The other three item types. A commands or output-styles frontmatter form is a
  different (much smaller) field set and is not asked for here.
- The raw-JSON textareas in `mcpEditPanel()` and `jsonForm()` — that is idea #20
  in `docs/IDEAS.md` and a separate piece of work.
- Any change to `saveFile()`, `_check_base`, or the 409 conflict flow.

## Git workflow

- Branch: `advisor/003-agent-frontmatter-form`
- Commit style: imperative, sentence case, user-visible. Example from `git log`:
  `Show and set the model a plugin's agents run on`. Suggested:
  `Edit an agent's frontmatter as a form, not as YAML`.
- Do NOT push or open a PR.

## Steps

### Step 1: Make the two helpers from plan 002 reusable

In `bin/claude_ui/static/agents.js`, confirm `yamlScalar()` and
`agentDocsPanel()` are declared at top level (they are, per plan 002) and add a
short comment above each noting that `editor.js` also calls it, so a future
refactor does not make them local.

Also add, in `agents.js`, the two shared constants the form needs, if plan 002
left them inline inside `renderAgentNew`:

```js
const AGENT_MODELS = ["", "sonnet", "opus", "haiku", "fable", "inherit"];
const AGENT_PERMISSION_MODES = ["", "default", "acceptEdits", "auto", "dontAsk",
                                "bypassPermissions", "plan", "manual"];
const AGENT_COLORS = ["", "red", "blue", "green", "yellow", "purple", "orange",
                      "pink", "cyan"];
```

A blank first entry means "not set — leave the field out". These values come
from <https://code.claude.com/docs/en/sub-agents> as of 2026-08-06; do not add
values not on that list.

**Verify**: `node --check bin/claude_ui/static/agents.js` → exit 0, and
`grep -c "^const AGENT_MODELS" bin/claude_ui/static/agents.js` → `1`.

### Step 2: Write the frontmatter key writer

Add to `editor.js`, immediately after `edFrontmatter` (which ends at line 353):

```js
/* The browser-side twin of core.py's set_frontmatter_key, and blunt in exactly
   the same way: rewrite one key's lines and leave every other byte alone. A
   file someone hand-formatted — with comments, a deliberate key order, a folded
   description — must survive a dropdown change with all of that intact. A key
   that isn't there is appended at the end of the block; `null` removes it. */
function edSetFrontmatterKey(text, key, value) {
  const nl = text.includes("\r\n") ? "\r\n" : "\n";
  const trailing = text.endsWith("\n");
  const lines = text.split(/\r?\n/);
  if (trailing) lines.pop();
  const join = (out) => out.join(nl) + (trailing ? nl : "");
  if (!lines.length || lines[0].trim() !== "---")
    return value == null ? text : join(["---", key + ": " + value, "---", ...lines]);
  let close = -1;
  for (let i = 1; i < lines.length; i++) if (lines[i].trim() === "---") { close = i; break; }
  if (close < 0) return text;          // unterminated block — refuse to guess
  let at = -1;
  for (let i = 1; i < close; i++) if (new RegExp("^" + key + ":").test(lines[i])) { at = i; break; }
  if (at < 0)
    return value == null ? text
      : join([...lines.slice(0, close), key + ": " + value, ...lines.slice(close)]);
  // a folded or list value continues into the indented lines below it
  let end = at + 1;
  while (end < close && /^[ \t-]/.test(lines[end])) end++;
  return join([...lines.slice(0, at),
               ...(value == null ? [] : [key + ": " + value]),
               ...lines.slice(end)]);
}
```

Two differences from the Python original, both deliberate — note them in the
comment:

- the continuation test also accepts a leading `-`, because `skills:` and
  `mcpServers:` are block lists whose items start at column 0 with a dash in the
  common two-space form `  - x` **and** in the flush form `- x`; the Python
  version never had to handle a list value.
- `value` is passed already-encoded. Callers use `yamlScalar()` from
  `agents.js` for scalars and build the multi-line list themselves.

**Verify**: with the app running, paste into the browser console:

```js
edSetFrontmatterKey("---\nname: a\nmodel: opus\n---\nbody\n", "model", "sonnet")
```

→ `"---\nname: a\nmodel: sonnet\n---\nbody\n"`. Then:

```js
edSetFrontmatterKey("---\nname: a\nskills:\n  - x\n  - y\n---\nbody\n", "skills", null)
```

→ `"---\nname: a\n---\nbody\n"`. Both must match exactly.

### Step 3: Build the frontmatter form

Add a new section to `editor.js` (after the chrome section banner) with:

```js
/* Only agents get a form. Their frontmatter has sixteen documented fields with
   fixed vocabularies you have to look up, which is exactly the case a form
   beats a textarea — and exactly the case where a typo (`model: sonnet-5`)
   produces a file Claude Code declines to load, silently. */
const edIsAgent = () => !!(EDITING && EDITING.item && EDITING.type === "agents");

function edAgentForm() { ... }
```

`edAgentForm()` returns a `.card.agent-fm` node, or `null` when
`!edIsAgent()` or `EDITING.readonly` or `edFrontmatter(text0())` returns `null`
(a file with no frontmatter block — offer nothing rather than guess where to put
one).

Controls, laid out as a responsive grid of `.mrow` rows:

| Control | Field | Behaviour |
|---|---|---|
| `<select>` | `model` | options `AGENT_MODELS`; blank removes the key |
| `<select>` | `permissionMode` | options `AGENT_PERMISSION_MODES` |
| `<select>` | `color` | options `AGENT_COLORS` |
| button | `skills` | opens the skills picker (step 4) |
| button | `tools` | opens the tools picker (step 4) |
| read-only line | `description` | shows the current value and its character count, with an **Edit in file** button that calls `edGoto()` on the description's line. Do not put a textarea here — two editors over one string is the bug this app already warns about in `docs/IDEAS.md` idea #25 |

Each select's current value comes from `edFrontmatter(text0()).meta[key]`. On
`change`:

```js
  const next = edSetFrontmatterKey(text0(), key,
    sel.value ? yamlScalar(sel.value) : null);
  if (next !== text0()) { edReplaceAll(next); renderEditor(); }
```

`edReplaceAll` marks the buffer dirty; `renderEditor()` rebuilds the form from
the new text so the controls and the file can never disagree. Nothing is written
to disk — the user still presses Save (or ⌘S), and the existing conflict
detection still applies. State that in the card's `.card-description`: **"Edits
the text above. Nothing is saved until you press Save."**

Place the card in `renderEditor()` between the findings strip and `.ed-body`:

```js
  ED.strip = el("div.ed-findings", { hidden: true });
  shell.append(ED.strip);

  const fm = edAgentForm();
  if (fm) shell.append(fm);
```

Make it collapsible with `<details>` remembering its open state in
`localStorage` under `claude-ui-agentfm`, defaulting to open. Agents are not the
only thing you open the editor for, and a permanently expanded card above every
agent file is a cost paid on every visit.

**Verify**: manual. Open an existing agent, change **Model** to `haiku`, and
confirm the textarea's `model:` line changes, the headline shows
`· unsaved changes`, and the file on disk is unchanged until you press Save.

### Step 4: The skills and tools pickers

Both open a `modal()` with a single `checklist` field, pre-ticked from the
current frontmatter.

Skills picker:

- Groups and disabled reasons: identical to plan 002 step 4 item 5. Build them
  from `DATA.items.skills`.
- Additionally, any name already in the file's `skills:` list that no longer
  matches a skill on this machine gets its own group, **"Listed but not found"**,
  with rows pre-ticked and `badge("missing", "destructive")`. Unticking is how
  you remove it. This is the case that most needs surfacing: the docs say a
  missing or disabled listed skill is skipped with only a debug-log warning, so
  today it fails completely silently.
- On OK, rewrite the key:

```js
  const list = r.skills;                       // checklist .value
  const val = list.length
    ? "\n" + list.map((s) => "  - " + yamlScalar(s)).join("\n")
    : null;
  edReplaceAll(edSetFrontmatterKey(text0(), "skills", val));
  renderEditor();
```

Note the leading `\n`: `edSetFrontmatterKey` writes `key + ": " + value`, so a
block list is passed as a value that starts with a newline. Confirm this
round-trips through `edFrontmatter` before moving on.

Tools picker: same categories as plan 002 step 4 item 4, pre-ticked from the
current comma-separated `tools:` value, written back as
`yamlScalar(list.join(", "))` or `null` when nothing is ticked. The modal's
`text` must say: **"Nothing ticked means the agent inherits every tool — that is
the default."**

**Verify**: manual. On an agent with no `skills:` key, pick two skills and
confirm the block appears correctly indented and that `edFrontmatter(text0())
.meta.skills` parses. Then open the picker again and confirm both are pre-ticked.

### Step 5: Put the reference beside the file

Add a fourth button to the layout segmented control in `edToolbar`
(`editor.js:868-881`), shown only for agents: `["docs", "Reference", "book"]`.
Confirm a suitable icon name exists in `ICONS` (`ui.js:58`) before using it; if
`book` is not there, use one that is rather than adding an icon.

In `renderEditor()`, when `ED.view === "docs"` and `edIsAgent()`, render the
body as a two-column grid — the textarea pane on the left, `agentDocsPanel()`
from `agents.js` on the right — reusing the `.ed-body-split` grid:

```js
  if (view3 === "docs") {
    body.classList.add("ed-body-split");
    body.append(agentDocsPanel());
  }
```

Guard the stored view: `ED.view` is persisted to `localStorage`
(`editor.js:813`), so a user who last had **Reference** open on an agent and then
opens `settings.json` must not land in a broken state. In the `view3` resolution
at `editor.js:907`, treat `docs` as `edit` whenever `!edIsAgent()` or
`innerWidth < 900`:

```js
  const view3 = isMd ? (innerWidth < 900 && (ED.view === "split" || ED.view === "docs")
    ? "edit" : (ED.view === "docs" && !edIsAgent() ? "edit" : ED.view)) : "edit";
```

Read the existing line before replacing it and keep its shape; the resize
handler at `editor.js:1011-1015` already rebuilds the chrome on crossing 900px,
so nothing else is needed.

**Verify**: manual. Open an agent, click **Reference**, confirm the docs render
beside the textarea. Close the editor, open `settings.json`, confirm you get the
plain editor and no console error. Narrow the window below 900px with Reference
active and confirm it falls back to the single-column editor.

### Step 6: Append the CSS

Add to the end of `bin/claude_ui/static/app.css`, under the agent-builder banner
added by plan 002:

```css
.agent-fm > .card-content { display: grid; gap: 0.75rem; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); }
.agent-fm summary { cursor: pointer; font-size: 0.8125rem; font-weight: 600; }
.agent-fm .fm-desc { grid-column: 1 / -1; }
```

Tokens only — no literal colours.

**Verify**: `grep -c "#[0-9a-fA-F]\{3,6\}" bin/claude_ui/static/app.css` returns
the same count as before your change.

### Step 7: Full check

**Verify**:
- `node --check bin/claude_ui/static/editor.js` → exit 0
- `node --check bin/claude_ui/static/agents.js` → exit 0
- `python3 -m unittest discover tests` → `OK`
- Open the app; visit an agent, a skill's SKILL.md, `CLAUDE.md` and
  `settings.json` in turn; console clean in all four.

## Test plan

No frontend test runner exists and this plan does not add one — see plan 002's
test plan for why.

Verification is the console checks in step 2 (which cover the one piece of pure
logic worth testing, the frontmatter writer, including its two hardest cases:
replacing a scalar and removing a block list) plus the manual checks in every
step. Run all of them and report each result. `python3 -m unittest discover
tests` must stay green.

## Done criteria

ALL must hold:

- [ ] `node --check bin/claude_ui/static/editor.js` exits 0
- [ ] `node --check bin/claude_ui/static/agents.js` exits 0
- [ ] `python3 -m unittest discover tests` exits 0
- [ ] Both `edSetFrontmatterKey` console checks in step 2 return the expected
      strings exactly
- [ ] The frontmatter card appears for agents and for no other file type
- [ ] Changing a dropdown updates the textarea, marks the editor dirty, and
      writes nothing to disk until Save is pressed
- [ ] The skills picker pre-ticks the current list and shows listed-but-missing
      skills in their own group
- [ ] **Reference** appears only for agents, and a stored `docs` view degrades
      to `edit` on a non-agent file and below 900px
- [ ] `grep -n "item-model-set" bin/claude_ui/static/editor.js` returns nothing
- [ ] `git status --porcelain` lists only the three in-scope files and `plans/`
- [ ] `plans/README.md` status row for 003 updated

## STOP conditions

Stop and report back (do not improvise) if:

- `agents.js` does not exist, or `agentDocsPanel` / `yamlScalar` are not global —
  plan 002 has not landed or diverged from its spec.
- `edReplaceAll`, `edFrontmatter` or `edSetView` no longer have the shape quoted
  in "Current state".
- The frontmatter writer's console checks do not produce the expected strings
  after two fix attempts. A writer that is *almost* right is worse than none —
  it corrupts files.
- You find you need to call a backend endpoint. This plan touches no Python; if
  a change seems to require one, report what and why.
- Adding the `docs` view breaks the existing Edit/Split/Preview control for
  non-agent files.

## Maintenance notes

- There are now two frontmatter writers in the codebase — `core.set_frontmatter_key`
  (Python, used by the Plugins tab's model setter) and `edSetFrontmatterKey`
  (JS, used by the editor form). That is a real duplication and it is
  deliberate: the Python one writes to disk with no editor open, the JS one
  edits an unsaved buffer, and merging them would mean a round trip per
  dropdown change. If a third caller appears, revisit. Whoever changes one
  should read the other.
- A reviewer should check that no code path in this plan writes to disk. The
  whole safety argument rests on it.
- `AGENT_MODELS`, `AGENT_PERMISSION_MODES` and `AGENT_COLORS` are a snapshot of
  the docs. Same drift caveat as plan 002's `AGENT_DOCS`; they are three
  adjacent constants in one file so they drift together, which is the best that
  can be done without a schema to sync from.
- Deliberately deferred: a form for the remaining eight fields, and forms for
  the other three item types. Both are additive and neither is blocked by
  anything here.
