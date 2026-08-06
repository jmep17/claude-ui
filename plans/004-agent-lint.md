# Plan 004: Catch the agent frontmatter mistakes that fail silently

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 49ae8ba..HEAD -- bin/claude_ui/items.py bin/claude_ui/doctor.py bin/claude_ui/static/editor.js`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW — report-only. `doctor()` never writes, and the editor lint only
  draws a strip.
- **Depends on**: none strictly. Best run after `plans/003-agent-frontmatter-form.md`,
  because the editor lint and the form then reinforce each other; but nothing
  here imports from 002 or 003.
- **Category**: correctness
- **Planned at**: commit `49ae8ba`, 2026-08-06

## Why this matters

Four agent frontmatter mistakes produce no error you will ever see:

1. A `name:` containing `:` — Claude Code **refuses to load the file** and logs
   to the debug log only.
2. A `model:` that is not a real alias or model ID — silently ignored, the agent
   runs on the inherited model.
3. A `skills:` entry naming a skill that is missing, disabled, or sets
   `disable-model-invocation: true` — **skipped with a debug-log warning**. Your
   agent runs without the domain knowledge you thought you gave it.
4. A `tools:` list where no entry resolves to a real tool — the agent fails to
   launch, and the failure names the entries but happens at delegation time,
   long after you wrote the file.

Every one of these is checkable from the file. `doctor()` is the best
value-per-line file in the repo (pure inspection, `add(level, area, msg,
target)`, no writes, no UI work), and the editor already has a findings strip
that puts a warning on the line it refers to. This plan spends both.

## Current state

### The scan does not see list values

`core.parse_frontmatter` (`bin/claude_ui/core.py:119-143`) handles scalars and
folded blocks but **not block lists**. Given:

```yaml
skills:
  - api-conventions
```

the key regex `^([A-Za-z0-9_-]+):\s*(.*)$` matches with an empty value, which is
not one of `">", "|", ">-", "|-"`, so it stores `meta["skills"] = ""` and sets
`key = None` — and the `  - api-conventions` line is then dropped. So
`parse_frontmatter` alone cannot answer "which skills does this agent preload".
You will add a small list reader rather than changing `parse_frontmatter`, whose
scalar behaviour many callers depend on.

### The agent rows

`_scan_md_type` (`bin/claude_ui/items.py:71-100`) builds every non-skill item
row and already reads `model`:

```python
        items.append({
            "name": name, "enabled": enabled,
            ...
            "source": meta.get(SOURCE_KEY) or "",
            "model": meta.get("model", ""),
            "name_mismatch": False,
            "long_desc": len(meta.get("description", "")) > 1024,
        })
```

### The doctor's item loop

`bin/claude_ui/doctor.py:159-192`:

```python
    for t in ITEM_TYPES:
        items = scan_items(t)
        live = {it["name"] for it in items if it["enabled"]}
        for it in items:
            where = "" if it["enabled"] else " (disabled)"
            at_tab = {"kind": "tab", "tab": t, "q": it["name"]}
            main = _main_file(t)
            ...
            if it.get("long_desc"):
                add("info", t, f"{it['name']}{where}: description over 1024 chars",
                    _at_item(it, t, file=main, find="description:"))
```

`_at_item(it, type_, **rest)` (`doctor.py:53-55`) builds a target the UI turns
into an **Open** button; `find="description:"` makes the editor jump to that
text. Levels are `"warn"` and `"info"` only.

### The editor's live lint

`edLocalLint()` (`bin/claude_ui/static/editor.js:363-403`) returns
`{level, source: "lint", line, msg}` objects. It already reads the parsed
frontmatter and its per-key line numbers:

```js
  if (mode === "md") {
    const fm = edFrontmatter(text);
    if (fm) {
      const d = fm.meta.description || "";
      if (d.length > DESC_MAX)
        out.push({ level: "warn", source: "lint", line: fm.meta["@line:description"],
          msg: "description is " + d.length + " characters — over the "
            + DESC_MAX + " limit, so it may be truncated" });
```

Note the suppression convention immediately below it (`editor.js:382-388`): a
file still carrying a `TODO` is unfinished and is not nagged about description
quality. Apply the same suppression to the new checks.

`edFindings()` (`editor.js:433-450`) de-duplicates doctor and lint findings that
land on the same line when one message contains the other. Word your two sets so
that de-duplication works — the doctor prefixes the item name, the lint does not.

### The documented vocabularies

From <https://code.claude.com/docs/en/sub-agents>, fetched 2026-08-06.

Valid frontmatter keys: `name`, `description`, `tools`, `disallowedTools`,
`model`, `permissionMode`, `maxTurns`, `skills`, `mcpServers`, `hooks`, `memory`,
`background`, `effort`, `isolation`, `color`, `initialPrompt`.

`model`: `sonnet`, `opus`, `haiku`, `fable`, `inherit`, or a full model ID.
`permissionMode`: `default`, `acceptEdits`, `auto`, `dontAsk`,
`bypassPermissions`, `plan`, `manual`. `memory`: `user`, `project`, `local`.
`effort`: `low`, `medium`, `high`, `xhigh`, `max`. `isolation`: `worktree`.
`color`: `red`, `blue`, `green`, `yellow`, `purple`, `orange`, `pink`, `cyan`.

Built-in tool names (the background-safe set, which is the documented list):
`Read`, `Grep`, `Glob`, `Bash`, `PowerShell`, `Edit`, `Write`, `NotebookEdit`,
`WebFetch`, `WebSearch`, `TodoWrite`, `Skill`, `ToolSearch`, `EnterWorktree`,
`ExitWorktree`, `Monitor`, `TaskStop`, `SendMessage`, `Artifact`. Plus `Agent`,
which the docs name separately.

### Conventions

- Python 3 standard library only. No pip.
- `doctor()` never writes. Keep it that way.
- Comments explain why. Match `doctor.py:124-127` for the voice used when a
  check has to hedge.
- Tests are stdlib `unittest`, run with `python3 -m unittest discover tests`.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests | `python3 -m unittest discover tests` | `OK`, exit 0 |
| One file | `python3 tests/test_doctor.py` | `OK`, exit 0 |
| Syntax (Python) | `python3 -m compileall -q bin/claude_ui` | exit 0, no output |
| Syntax (JS) | `node --check bin/claude_ui/static/editor.js` | exit 0, no output |

## Scope

**In scope**:

- `bin/claude_ui/items.py` (modify — the list reader and three new scan fields)
- `bin/claude_ui/doctor.py` (modify — the new checks)
- `bin/claude_ui/static/editor.js` (modify — the matching live lint)
- `tests/test_doctor.py` (create)

**Out of scope** (do NOT touch):

- `bin/claude_ui/core.py` — `parse_frontmatter`'s scalar behaviour is depended on
  by the plugins tab, the backup manifest and the skills scan. Do not change it.
- Any fix action. These findings are report-only; `POST /api/doctor-fix` is idea
  #11 in `docs/IDEAS.md` and is separate work.
- MCP server name validation in `mcpServers:` — the field also accepts inline
  definitions, so "not a configured server" is not an error.
- Validating a full model ID beyond a shape check. Model IDs change; asserting a
  list of them here would rot within weeks.

## Git workflow

- Branch: `advisor/004-agent-lint`
- Commit style: imperative, sentence case, user-visible. Suggested:
  `Warn about the agent frontmatter mistakes Claude Code swallows`.
- Do NOT push or open a PR.

## Steps

### Step 1: Read block lists out of frontmatter

In `bin/claude_ui/items.py`, add near the top (after `_todo_line`):

```python
def _fm_list(text, key):
    """The items of a block list in frontmatter — `skills:` and friends.

    parse_frontmatter() deliberately only understands scalars and folded blocks,
    and it is depended on in that shape by the plugin, backup and skill paths.
    Rather than teach it lists and risk all of them, this reads the one shape
    that matters: a bare key followed by indented `- ` entries.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    out, seen = [], False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if seen:
            s = line.strip()
            if s.startswith("- "):
                out.append(s[2:].strip().strip("\"'"))
                continue
            if line[:1] in (" ", "\t") and not s:
                continue
            break
        if line.strip() == key + ":":
            seen = True
    return out
```

Also handle the inline form `skills: [a, b]`: if `meta.get(key)` starts with `[`
and ends with `]`, split on commas and strip quotes. Add that as a two-line
fallback inside the caller in step 2, not inside `_fm_list`.

**Verify**: `python3 -c "
import sys; sys.path.insert(0,'bin')
from claude_ui.items import _fm_list
t='---\nname: a\nskills:\n  - x\n  - \'y\'\nmodel: opus\n---\nbody\n'
print(_fm_list(t,'skills'))"` → `['x', 'y']`

### Step 2: Add the agent fields to the scan

In `_scan_md_type` (`bin/claude_ui/items.py:71-100`), add three keys to the
appended dict:

```python
            # agent-only, and cheap: the text and the frontmatter are already
            # in hand here, and the doctor would otherwise re-read every file
            "fm_keys": [k for k in meta if not k.startswith("@")],
            "skills_list": _fm_list(text, "skills"),
            "tools_list": [s.strip() for s in (meta.get("tools") or "").split(",")
                           if s.strip()],
```

These are harmless on commands and output styles (empty lists, their own key
names) and the doctor will only look at them for `agents`.

Apply the inline-list fallback here: if `_fm_list` returned nothing and
`meta.get("skills", "").startswith("[")`, parse the bracketed form.

**Verify**: `python3 -m unittest discover tests` → `OK` (nothing existing asserts
the exact row shape; if something does, that is a STOP condition).

### Step 3: Add the doctor checks

In `bin/claude_ui/doctor.py`, add module-level constants above `doctor()`, with
a comment naming their source and their twin:

```python
# The documented agent frontmatter vocabulary, from
# https://code.claude.com/docs/en/sub-agents (2026-08-06). Duplicated in
# static/agents.js, which draws the pickers from the same lists; there is no
# machine-readable schema for agent frontmatter to sync from, unlike settings.
AGENT_FIELDS = {...}
AGENT_MODEL_ALIASES = {"sonnet", "opus", "haiku", "fable", "inherit"}
AGENT_TOOLS = {...}
```

Then, inside the existing `for t in ITEM_TYPES:` loop's inner `for it in items:`
body, after the `long_desc` check, add a guarded block:

```python
            if t == "agents" and not it.get("broken") and not it.get("todo"):
                ...
```

The `todo` suppression matches the convention already used for the skills
description check at `doctor.py:187-192`: an unfinished file gets left alone.

Checks to add, in this order:

1. `":" in it["name"]` → **warn**: `f"{it['name']}: the name contains ':', which
   is reserved for plugin-scoped agents — Claude Code will not load this file"`,
   target `_at_item(it, t, file=main, find="name:")`.
2. no `description` → **warn**: `"...: no description — description is required,
   and it is the only thing Claude reads when choosing an agent"`, target
   `_at_item(it, t, file=main)`.
3. `it["model"]` set, not in `AGENT_MODEL_ALIASES`, and not matching
   `^claude-[a-z0-9.\-]+$` → **warn**: `f"...: model '{model}' is not a known
   alias or model ID — Claude Code ignores it and uses the inherited model"`,
   target `find="model:"`.
4. for each name in `it["skills_list"]` that does not resolve to an enabled,
   non-broken, non-`no_model_invoke` skill → **warn**, with the reason spelled
   out per case (`"is disabled"`, `"is not installed"`, `"sets
   disable-model-invocation"`), ending `"— Claude Code skips it and warns only
   in the debug log"`. Target `find="- " + name`. Build the skill lookup once,
   outside the item loop, from `scan_items("skills")`.
5. for each entry in `it["tools_list"]` not in `AGENT_TOOLS` and not starting
   `mcp__` → **info**: `f"...: tools lists '{name}', which is not a tool name we
   know of"`. Level is **info**, not warn, and the wording says "we know of" —
   the tool set is not published as a schema and MCP tools are open-ended, so
   absence is not proof, exactly as `doctor.py:124-127` already hedges for
   settings keys.
6. unknown frontmatter key (in `fm_keys`, not in `AGENT_FIELDS`, not
   `SOURCE_KEY`) → **info**: `f"...: frontmatter key '{k}' is not a documented
   agent field"`, target `find=k + ":"`.

Do not add any other check. In particular do not warn about an agent with no
`tools:` — omitting it is the documented default.

**Verify**: `python3 -c "
import sys; sys.path.insert(0,'bin')
from claude_ui.doctor import doctor
print(doctor()['warns'], 'warnings')"` → runs without error against your real
config.

### Step 4: Mirror the checks in the editor lint

In `edLocalLint()` (`bin/claude_ui/static/editor.js:363-403`), inside the
existing `if (mode === "md")` / `if (fm)` block, add the same six checks guarded
by `EDITING.item && EDITING.type === "agents"` and by the existing
`!text.includes("TODO")` suppression.

Use `fm.meta["@line:<key>"]` for the line number, and for a bad `skills:` entry
find the line by scanning for `"- " + name` after `fm.meta["@line:skills"]`.
Read `DATA.items.skills` for the skill lookup — it is already loaded.

Keep the messages **shorter than the doctor's and contained in them** where they
overlap, so `edFindings()`'s de-duplication (`editor.js:442-449`) collapses the
pair. For example: doctor says `"reviewer: model 'sonnet-5' is not a known alias
or model ID — ..."`, lint says `"model 'sonnet-5' is not a known alias or model
ID — ..."`.

Define the vocabulary constants once in `editor.js` near `DESC_MAX`
(`editor.js:35`), or reuse `AGENT_MODELS` etc. from `agents.js` if plan 003 has
landed — check with `typeof AGENT_MODELS !== "undefined"` rather than assuming.

**Verify**: `node --check bin/claude_ui/static/editor.js` → exit 0. Then
manually: open an agent, change `model:` to `sonnet-5`, and confirm a warning
appears in the findings strip within a second and clicking it jumps to that line.

### Step 5: Write tests/test_doctor.py

Create `tests/test_doctor.py`. Copy the bootstrap and `config_dir` patching
idiom from `tests/test_plugins.py:20-45` (read it first); patch `doctor`,
`items`, `core`, `settings`, `mcp`, `plugins`, `statusline` and `schema` — any
module `doctor.py` reaches `config_dir()` through. If a module resists patching,
that is a STOP condition, not a reason to skip the test.

One test method per check, each writing one agent file into the temp config dir
and asserting on the messages `doctor()` returns:

1. `test_colon_in_name_warns`
2. `test_missing_description_warns`
3. `test_unknown_model_warns` — and a companion assertion that
   `model: claude-opus-5` and `model: inherit` produce **no** finding
4. `test_missing_preloaded_skill_warns` — three sub-cases: not installed,
   disabled, `disable-model-invocation: true`
5. `test_unknown_tool_is_info_only` — asserts the level is `"info"`, and that
   `mcp__github__create_pr` produces no finding
6. `test_unknown_frontmatter_key_is_info`
7. `test_todo_file_is_left_alone` — an agent with every one of the above problems
   **and** a `TODO` produces none of these findings
8. `test_valid_agent_is_clean` — a correct agent produces no `agents`-area
   findings at all

Assert on substrings of `f["msg"]`, not on exact strings — the wording will be
tuned and a test that pins it is a test that gets deleted.

**Verify**: `python3 tests/test_doctor.py` → `OK`, 8 tests.

### Step 6: Full check

**Verify**:
- `python3 -m unittest discover tests` → `OK`, 8 more tests than before
- `python3 -m compileall -q bin/claude_ui` → exit 0
- `node --check bin/claude_ui/static/editor.js` → exit 0

## Test plan

- New file `tests/test_doctor.py`, 8 tests, listed in step 5.
- Structural pattern: `tests/test_plugins.py`.
- The editor lint has no automated coverage (no frontend test runner exists —
  see plan 002). Its correctness rests on being a mirror of the Python checks,
  which are tested; verify it manually per step 4 and report the result.

## Done criteria

ALL must hold:

- [ ] `python3 -m unittest discover tests` exits 0
- [ ] `python3 tests/test_doctor.py` reports 8 tests, all passing
- [ ] `python3 -m compileall -q bin/claude_ui` exits 0
- [ ] `node --check bin/claude_ui/static/editor.js` exits 0
- [ ] `doctor()` runs against the real config dir without raising
- [ ] Every new doctor finding carries a `target` (verify:
      `python3 -c "..."` printing any `agents`-area finding without one → empty)
- [ ] `grep -n "atomic_write\|\.write_text\|\.unlink" bin/claude_ui/doctor.py`
      returns nothing — the doctor still never writes
- [ ] A `TODO`-carrying agent produces none of the new findings
- [ ] `git status --porcelain` lists only the four in-scope files and `plans/`
- [ ] `plans/README.md` status row for 004 updated

## STOP conditions

Stop and report back (do not improvise) if:

- `parse_frontmatter` has changed to understand lists — then `_fm_list` is
  redundant and the plan needs rethinking rather than adding a second reader.
- An existing test asserts the exact shape of a `_scan_md_type` row and your new
  keys break it.
- `doctor()` raises on the real config dir.
- You cannot patch `config_dir` for a module `doctor.py` depends on.
- Any new check fires on an agent you believe is correct. A noisy doctor is
  worse than a quiet one — the tab's value is that every row is real.

## Maintenance notes

- `AGENT_FIELDS` / `AGENT_TOOLS` in `doctor.py` and their twins in `agents.js`
  are a hand-maintained snapshot. Unlike `settings.json`, agent frontmatter has
  no published JSON Schema, so there is no `tools/sync_*.py` to write. When the
  docs gain a field, both copies change. A reviewer should check both were
  updated.
- Check 5's level is `info` and its wording is `"not a tool name we know of"` on
  purpose. If it is ever promoted to `warn`, the tool list must first become
  something better than a hand-copied snapshot.
- Deliberately deferred: quick-fixes for any of these findings (idea #11), and
  a `permissionMode` / `memory` / `effort` / `color` value check. The last four
  are the same shape as check 3 and are a cheap follow-up if the enum lists here
  prove stable.
