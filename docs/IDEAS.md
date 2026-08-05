# Ideas backlog

A ranked list of things that would make claude-ui better, written against the
code that exists. Everything here respects the project's constraints:

- **Python 3 standard library only** on the backend — no pip, nothing to install.
- **No build step** on the frontend — plain files in `bin/claude_ui/static/`.
- **The filesystem is the state** — the app edits your live config in place and
  never owns, links, tracks, or syncs anything.

Ordered by value ÷ effort. Effort is S (an afternoon), M (a day or two), L (more).

---

## Done

Shipped in the design-system pass; listed so the backlog stays honest.

| Idea | Notes |
| :--- | :--- |
| Undo on destructive toggles | `toggleItem()` now passes a 10s **Undo** action to `toast()`; the affordance existed but no call site used it. |
| Unsaved-changes guard | `EDITING.dirty` blocks tab switches, editor close, and unload. |
| ⌘S / Ctrl-S saves the editor | Handled before the input-focus bailout in the global key handler. |
| Docs deep-link per setting | `SETTING_DOCS` maps key prefixes to the right docs page; the link appears on row hover. |
| Serve `static/` from a list | `_STATIC_NAMES` in `server.py` — adding a frontend file no longer needs a routing edit. |
| 53 more settings keys | See the settings coverage note at the bottom. |
| Plugins tab, with Split | Was idea 13. `plugins.py` inventories `<config>/plugins/`, the tab splits a plugin into your own items, `doctor` and `insight_budget()` now both see plugin components. |

---

## High value

### 1. Lost-update protection and external-change detection — S

`file_save` / `item_save` / `settings_set` / `mcp_machine_set` all call
`atomic_write()` unconditionally. If a Claude Code session or your editor
changed the file after `/api/file` returned it, your save silently destroys
theirs.

Return `mtime` + `size` from `file_read()` / `item_read()` / `settings_state()`,
accept an optional `base` in the POST bodies, and reject the write with a 409
and a diff when the file moved underneath. Pair it with a lightweight poll of
`/api/state` that flags "changed on disk" in the header.

The app's core promise is that the filesystem is the state — but it currently
treats its own in-memory `DATA` snapshot as authoritative for the length of an
edit.

### 2. Item lifecycle: create, duplicate, rename, delete — M

`items.py` can only edit what already exists — `item_read`/`item_save` both
raise `not found`, and `server.py` exposes no create/delete/rename action. So
authoring a new skill means dropping to a terminal, which breaks the loop the
whole UI is built around.

Add `item_create(type_, name, template)` writing frontmatter stubs, `item_copy`,
`item_rename` (a rename inside `disabled/` too), and a delete that moves to
`disabled/` first rather than unlinking. Templates per type — a skill with a
`Use when …` trigger already filled in is a teaching moment.

### 3. Doctor check pack — S

`doctor()` has a clean `add(level, area, msg)` shape to extend, and it is the
best value-per-line file in the repo: pure inspection, no writes, no UI work.
Worth adding:

- `~/.claude.json` over ~5 MB (classic `projects` bloat — and this app rewrites
  that whole file on every MCP save)
- `statusline.sh` present but not `0o755`, or missing its shebang
- `permissions.allow`/`ask`/`deny` entries that can never match — unknown tool
  name, unbalanced parens, a `Bash(...)` rule with no `*` that will only ever
  match one exact string
- Two enabled skills whose descriptions are near-duplicates (Claude can't pick)
- Hooks whose command is a relative path (they run with an unpredictable cwd)
- MCP servers referencing env vars that are unset in `settings.json.env`
- `env` keys that are not documented environment variables (typos are silent)
- A `disabled/` item shadowed by a plugin of the same name

### 4. Permission rule tester — M

Permission rules are the config's sharpest edge — a wrong `deny` silently blocks
work, a broad `allow` silently grants it — and they are write-only today.

A panel under `permissions`: type a candidate tool call (`Bash(git push origin
main)`, `Read(~/.ssh/id_rsa)`, `mcp__github__create_pr`) and see which of
deny → ask → allow matches first, the exact rule string that won, and the
decision. Plus the reverse: "show me every call this rule would match".

### 5. Full-text search across every item — S

`IQ` matches only name and description, per tab. Add `/api/search?q=` walking
`scan_items()` for all four types plus `CONFIG_FILES`, returning
`{type, name, file, line, snippet}` hits. Wire it into the ⌘K palette so the
existing `fuzzy()` ranking covers file contents, not just names.

"Which of my skills mentions ripgrep?" currently has one answer: `rg ~/.claude`.

### 6. Tool and MCP usage analytics — S

`_scan_transcript()` already inspects every `tool_use` block but records only
three names: `Skill`, `Task`, `Bash`. Add a per-tool histogram and a split of
`mcp__<server>__<tool>` into (server, tool) pairs in the same loop; bump
`CACHE_V`. A dozen lines, and it answers the question the app most wants to
answer: **which MCP servers are you paying context for and never using?**

### 7. Frontmatter-aware form editing — M

`parse_frontmatter()` exists and the doctor already lints frontmatter quality,
but the editor is a raw textarea. For item `.md` files, render a small form
above it: `name`, `description` with a live counter against the 1024-char limit
and a "Use when …" trigger hint, `allowed-tools`, `model`. The description is
the single highest-leverage string in the whole config — it decides whether a
skill ever loads — and today it's buried in YAML.

### 8. MCP inspector — M

`mcp_test()` only checks that a command resolves on `PATH` or that a URL answers
`HEAD`. Do the real handshake: spawn the stdio server, write JSON-RPC
`initialize` → `notifications/initialized` → `tools/list` over stdin
(newline-delimited JSON, stdlib `json`, no framing library), and show the tool
list with its estimated token cost.

That turns "the binary exists" into "here are the 47 tools this server injects
into every session, costing ~6k tokens" — a number invisible everywhere else.

### 9. Sessions browser — M

`transcript_stats()` already walks every transcript with a per-file cache.
Extend the cached record with first/last timestamp, message count, and the first
user prompt as a title. The costs tab tells you that you spent $40 last Tuesday
and gives you no way to find out on what.

### 10. Dry-run diffs before every write — M

`difflib` is stdlib. Honor a `dry` flag on the mutating POST handlers, returning
a unified diff instead of writing. `statusline-save` regenerates a 450-line
script and `mcp-save` rewrites the entirety of `~/.claude.json` — a file that
also holds your project history and OAuth account — both with no preview.

### 11. Doctor quick-fixes — M

Every finding is report-only, which trains people to ignore the list. Give
findings an optional `{action, args}` and add `POST /api/doctor-fix` with a
small allowlist: delete a leftover `*.bak*`, unlink a broken symlink, clear a
`statusLine`/hook key pointing at a missing executable, `chmod +x` a
non-executable statusline. Every one of those is unambiguous.

### 12. Statusline preview that runs the real script — S

The preview is a hand-rolled HTML reimplementation of what `STATUSLINE_SCRIPT`
does — the script's `paint()`, separator dimming, and per-field empty
suppression are all reimplemented in JS, and the two will drift. Pipe a
realistic payload through the actual generated script and render its stdout.
It turns the preview from a drawing into proof.

### 13. Rename-on-split, and marketplace management — M

The Plugins tab shipped (see Done). Two pieces were deliberately left out.

**Rename on split.** A component whose name you already use renders greyed with
a `name taken` badge and cannot be ticked; the only way through is to rename
your own item first. An inline rename input per conflicting row would fix that,
but it breaks the one-decision-one-confirm shape the dialog was built around, so
it wants its own thought.

**Marketplaces.** `plugins_state()` returns the marketplace list and the schema
has `extraKnownMarketplaces`, but nothing adds, updates or removes one — that is
still `claude plugin marketplace` territory. Worth doing only alongside install,
which means shelling out to the CLI: the first thing in this app that would.

---

## Medium value

### 14. Hooks builder v2 — M
Edit (not delete-and-retype), reorder within a matcher group (order is
execution order), soft-disable, and a recipe picker. Hooks are the most powerful
and least approachable part of the config.

### 15. Costs drill-down — M
`cost_stats()` computes per-project per-day rows and throws most away
(`by_project[:12]`, `days[-30:]`). Add a per-session table, a month-end
projection from the current burn rate, and CSV export. The number it computes is
more careful than most tools' — the de-dup, the cache-TTL multipliers, the
fast-mode and US-inference premiums — and it's presented as five tiles.

### 16. Deep-link hash routing — S
`location.hash` carries only the tab. Extend to `#skills/pdf/SKILL.md`,
`#settings?q=permission`, `#costs/2026-07-30`, restore state on load, and make
the back button work between editor and inventory.

### 17. Undo journal with a history view — M
Have `atomic_write()` snapshot the pre-write content into
`~/.cache/claude-ui/history/` (a cache, deliberately outside the config dir —
the filesystem stays the state, this is only a safety net), keep ~20 versions
per file, and add a restore path. Extends the 10-second toast undo into
something that survives a reload.

### 18. Config git panel — M
If `<config>/.git` exists, show `git status --porcelain`, `git diff`, and
`git log --oneline -20`. The git binary is the user's own tool, not a Python
dependency — `settings.py` already shells out to `git config`. A large fraction
of people who'd run this app keep `~/.claude` in git, and the app currently
dirties tracked files with no visibility.

### 19. CLAUDE.md / memory browser — M
`insight_budget()` counts CLAUDE.md as one number. Resolve `@path` imports
recursively to show the real assembled token cost, list the auto-memory
directory, and flag imports that don't resolve.

### 20. Syntax highlighting + JSON format/lint — M
The standard no-library technique: a `<pre>` behind a transparent-text textarea
with synchronized scroll, plus a regex tokenizer for JSON, YAML frontmatter and
Markdown. Add a format button and turn `Expecting ',' delimiter: line 12` into a
cursor jump.

### 21. Reference map across items — M
Parse each item body for skill names, `/command` invocations, `subagent_type`
values and `@file` imports, then show what refers to what and which references
are broken. A command that invokes a skill you later disabled just quietly does
less.

### 22. Grow the setup-pieces registry — M
`setup.py`'s `PIECES` dict is documented as the extension point and holds
exactly one entry. Candidates following the same derived-state contract: a
session-start git-identity hook, a stop-hook desktop notifier, a safe-defaults
permissions preset, shell completions.

### 23. Import/export a config bundle — L
`zipfile` is stdlib. Export a selected subset with a manifest; import via a
per-file dry-run diff (new / would-overwrite / identical). The two real jobs are
"set up my new laptop" and "share these three skills with a colleague".

### 24. Keybindings editor UI — L
`keybindings.json` is in `CONFIG_FILES` but only reachable as a raw textarea.
Keybindings have exactly the properties that reward a UI: a chord you'd rather
press than spell, a fixed vocabulary of action names nobody remembers, and
conflicts invisible in a text file.

---

## Settings coverage

Claude Code publishes a **machine-readable JSON Schema** for `settings.json` at
`https://json.schemastore.org/claude-code-settings.json` (301 → `www.schemastore.org`,
~230 KB, draft-07). The docs name it as the `$schema` value in their own example
settings file, so it is the same thing the CLI is validated against.

It carries a `description`, `type`, `enum` and `default` for every documented
key, and most descriptions embed the exact anchored docs URL for that key. As of
the current snapshot: 141 real top-level properties, 590 dotted keys when you
recurse `properties`, **100% of them described**, 340 `env.*` vars, 40 `sandbox.*`
sub-keys, 31 `hooks.*` events. `additionalProperties` is `true`.

The schema now drives the settings tab:

- `tools/sync_settings_schema.py` fetches it and writes
  `bin/claude_ui/data/settings_schema.json`, committed. Review the diff — a
  reworded description is upstream telling you a setting changed meaning, which
  is the whole reason to vendor it rather than fetch blind.
- `schema.py` overlays a background re-fetch at server start (`_live`) and merges
  the result over the hand-written entries. The vendored file is the **floor**:
  live may add or replace, never delete, so a bad upstream commit degrades to
  stale rather than empty. `validate()` guards both paths.
- `SETTINGS_RAW` in `settings.py` stays hand-written and is never regenerated.
  It supplies the two things the official schema has no concept of — which
  *control* to draw, and which category to file the key under — plus a one-line
  `desc` short enough to sit under the key name. Everything else comes from the
  merge. `tests/test_settings.py::TestMerge` asserts the curation survives.
- The long descriptions are served lazily from `/api/schema-help` (~58 KB) rather
  than inlined, and shown in a popover with the type, default, allowed values and
  a **Docs** link that lands on the key's own anchor.

What the first sync corrected: `permissions.defaultMode` was missing `delegate`;
`workflowSizeGuideline`'s default is `unrestricted`, not `medium`; `fastMode`,
`fastModePerSessionOptIn` and `prefersReducedMotion` default to `false` and
`includeCoAuthoredBy` to `true`, none of which were recorded;
`permissions.skipDangerousModePermissionPrompt` is not a real key (it is
top-level — doctor now warns if an older version wrote it nested); the hooks
builder knew 9 of 31 events; and `ENV_VARS` had drifted in both directions.

**Managed and enterprise keys are now included**, in their own collapsed
`managed & enterprise` group marked as no-ops in user scope, so the tab can
answer "what is `allowManagedHooksOnly`" without pretending you would set it
here. Placement comes from the explicit `MANAGED_KEYS` list, *not* from the
`(Managed settings)` description prefix — that prose convention also tags
`disableAgentView` and `sshConfigs`, which are ordinary user-facing rows. The
flag badges; the list places.

**Still excluded on purpose:** the six `~/.claude.json` global-config keys —
`autoConnectIde`, `autoInstallIdeExtension`, `diffTool`, `externalEditorContext`,
`permissionExplainerEnabled`, `teammateDefaultModel`. The official schema lists
all six, but the Global config settings table in `settings.md` says outright that
`settings.json` silently ignores them, and the docs win. `permissionExplainerEnabled`
and `externalEditorContext` are the easy mistake here: they read as ordinary user
preferences. Doctor warns when one turns up in `settings.json`.

**Not listed in the official schema: 22 keys** — `thinkingBudgetTokens`,
`interfaceLanguage`, `strikethrough`, `interactiveEditingEnabled`,
`showHiddenFiles`, `keyBindings`, `maxCompactMessages`, `sessionHistorySize`,
`mcpServerTimeouts`, `warningOnSandboxEscape`, `invalidSSLWarning`, `proxy`,
`restartOnConfigChange`, `telemetryEnabled`, `workspaceInitScript`,
`skipFirstRunQuestions`, `llmConnectionTimeout`, `llmRequestTimeout`,
`gitAttributionName`, `gitAttributionEmail`, `switchModelsOnFlag`,
`remote.defaultEnvironmentId`. They are badged `unverified` in the UI and the set
is frozen in a test. Because `additionalProperties` is `true`, absence is not
disproof — the badge and the doctor message both say "not listed", never "not
real". Worth checking against the binary before removing any.

---

## Next, on the schema

### 25. Break `sandbox` out of its JSON blob — M
The highest-value remaining raw-JSON row: security configuration, 40 sub-keys
three levels deep, each with a type, an enum and a description in the snapshot.
Needs **no backend work** — `settings_set` already writes dotted paths and prunes
empty parents, and `SETTINGS_KEY_RE` already permits dots, so
`sandbox.credentials.allowPlaintextInject` writes correctly today.

Generate the rows from the snapshot with a small hand-curated control-type
override table; hand-writing 40 literals would re-create exactly the rot the
schema sync just ended. Costs ~10 KB on the inlined payload. One hazard: if the
whole-object `sandbox` row stays as an escape hatch, two editors point at one
subtree and last write wins — label it, or hide it behind the group.

### 26. Env var descriptions in the `env` key picker — M
The snapshot has a description for all 340, and the `env` editor currently
suggests bare names. Two costs, which is why it isn't done: ~74 KB on the wire,
useful only inside the combobox, so it needs its own lazy endpoint; and it
changes `filterPopup`'s item contract from `{value, text}` to `{value, text, hint}`
plus a two-line row — a shared primitive behind `filterSelect`, `filterInput`,
every long enum, the hook event picker and the model pickers. Worth doing
deliberately, not as a rider on something else.
