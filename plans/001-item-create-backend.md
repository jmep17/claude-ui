# Plan 001: Let the backend create a new item file

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 49ae8ba..HEAD -- bin/claude_ui/items.py bin/claude_ui/server.py bin/claude_ui/core.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `49ae8ba`, 2026-08-06

## Why this matters

`claude-ui` can edit and enable/disable items, but it cannot make one. Creating
an agent means dropping to a terminal, writing YAML frontmatter from memory, and
coming back — which breaks the loop the whole app is built around. Claude Code
removed its own interactive agent wizard in v2.1.198 (the `/agents` command now
just prints "ask Claude or edit `.claude/agents/` directly"), so there is no
guided way to author an agent anywhere. Plans 002 and 003 build that guided UI;
this plan is the one API call they both need.

It also adds one field to the skills scan — whether a skill sets
`disable-model-invocation: true` — because plan 002's skill picker must not
offer skills that Claude Code refuses to preload.

## Current state

Files in play:

- `bin/claude_ui/items.py` — the whole item layer: scan, read, save, enable /
  disable. It has no create path at all.
- `bin/claude_ui/server.py` — the HTTP handler; one `elif action == "..."`
  branch per mutating call.
- `bin/claude_ui/core.py` — shared helpers (`ITEM_TYPES`, `item_rel`,
  `atomic_write`, `parse_frontmatter`, `tilde`). **Read only; do not edit.**

`item_read` and `item_save` both refuse a name that isn't already there
(`bin/claude_ui/items.py:188-232`):

```python
def item_read(type_, name, fname=None, enabled=True):
    root = resolve_item(type_, name, enabled)
    if ITEM_TYPES[type_]["kind"] == "md":
        if not root.is_file():
            raise ValueError(f"{name}: not found")
```

`resolve_item` already validates the name and picks the right side of
`disabled/` (`bin/claude_ui/items.py:24-32`):

```python
def resolve_item(type_, name, enabled=True):
    if type_ not in ITEM_TYPES:
        raise ValueError("unknown type")
    rel = item_rel(name)
    if ITEM_TYPES[type_]["kind"] == "md":
        rel = rel.with_suffix(".md")
    elif len(rel.parts) != 1:
        raise ValueError("bad name")
    return item_root(type_, enabled) / rel
```

`ITEM_TYPES` (`bin/claude_ui/core.py:33-38`) says which types are directories:

```python
ITEM_TYPES = {
    "skills": {"kind": "dir"},
    "commands": {"kind": "md"},
    "agents": {"kind": "md"},
    "output-styles": {"kind": "md"},
}
```

`set_enabled` shows the collision rule this repo already uses — a name that
exists on *either* side is a conflict, because re-enabling would fail
(`bin/claude_ui/items.py:128-131`):

```python
    if dst.exists() or dst.is_symlink():
        raise ValueError(
            f"{name}: already exists on the "
            f"{'enabled' if enabled else 'disabled'} side — resolve by hand")
```

The skills scan builds each row in `_dir_item` (`bin/claude_ui/items.py:34-58`),
which already has the parsed frontmatter in hand:

```python
    meta = parse_frontmatter(text)
    ...
    return {
        "name": entry.name, "enabled": enabled,
        ...
        "long_desc": len(meta.get("description", "")) > 1024,
    }
```

Server branches look like this (`bin/claude_ui/server.py:202-206`):

```python
            elif action == "item-save":
                self.send(200, {"ok": True, **item_save(
                    req.get("type", ""), req.get("name", ""), req.get("file"),
                    req.get("content", ""), bool(req.get("enabled", True)),
                    req.get("base"))})
```

Conventions to match:

- **Backend is Python 3 standard library only.** No pip, no imports outside
  `json`, `pathlib`, `re`, `os`, `shutil`, `subprocess`, `time`, `zipfile` etc.
- Every write goes through `core.atomic_write` — temp file plus rename, never a
  direct `open(...).write`.
- Errors are plain `ValueError` with a lowercase message that names the item;
  `server.py` turns them into a 400.
- Comments explain *why*, not what. Look at `items.py:139-141` and
  `items.py:166-168` for the house voice, and match it.
- Tests are stdlib `unittest`, one file per area, run with
  `python3 -m unittest discover tests`.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests | `python3 -m unittest discover tests` | `OK`, exit 0 |
| One test file | `python3 tests/test_items.py` | `OK`, exit 0 |
| Syntax check | `python3 -m compileall -q bin/claude_ui` | exit 0, no output |

There is no linter, formatter, typechecker or build step in this repo. Do not
add one.

## Scope

**In scope** (the only files you may modify or create):

- `bin/claude_ui/items.py` (modify)
- `bin/claude_ui/server.py` (modify)
- `tests/test_items.py` (create)

**Out of scope** (do NOT touch, even though they look related):

- `bin/claude_ui/core.py` — everything you need is already exported from it.
- `bin/claude_ui/static/*` — the UI is plans 002 and 003. This plan ships an API
  with no caller, deliberately.
- `bin/claude_ui/doctor.py` — agent lint checks are plan 004.
- Rename, duplicate and delete for items. They belong with create conceptually,
  but each has its own hazards (a rename inside `disabled/`, a delete that must
  never unlink) and none of them is needed by plans 002–004. Do not add them.

## Git workflow

- Branch: `advisor/001-item-create-backend`
- One commit for the whole plan is fine.
- Commit messages here are imperative, sentence case, and describe the user-
  visible change, not the code. Real examples from `git log`:
  `Back up the config you would otherwise rebuild by hand`,
  `Show and set the model a plugin's agents run on`. Suggested message:
  `Create a new item from the app instead of the terminal`.
- Do NOT push or open a PR.

## Steps

### Step 1: Report whether a skill can be preloaded

In `bin/claude_ui/items.py`, inside `_dir_item`, add one key to the returned
dict, right after `"long_desc"`:

```python
        # a skill with disable-model-invocation can only be run by the user, so
        # it can't be preloaded into an agent's `skills:` list either — the
        # picker that offers skills needs to know before it offers one
        "no_model_invoke": str(meta.get("disable-model-invocation", "")
                               ).strip().lower() in ("true", "yes"),
```

Do not add this key to `_scan_md_type` — only skills have the field.

**Verify**: `python3 -c "import sys; sys.path.insert(0, 'bin'); from claude_ui import items; print('no_model_invoke' in items.scan_items('skills')[0] if items.scan_items('skills') else 'no skills on this machine — fine')"`
→ prints `True`, or the "no skills" message. Either is a pass.

### Step 2: Add `item_create` to items.py

Add this function immediately after `item_save` (which ends at line 232 today).
Keep it there so read / save / create sit together.

```python
def item_create(type_, name, content, enabled=True):
    """Write a brand-new item and hand back the same shape item_read returns.

    A name that exists on *either* side of disabled/ is a conflict, not just one
    that exists on the side we're writing to: set_enabled() refuses to move an
    item onto an occupied name, so creating a twin of a disabled item builds a
    trap you only spring later. The content arrives fully formed — the caller
    that composed the frontmatter is the same one showing you a preview of it,
    and two places generating YAML is one place too many.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("nothing to write")
    if len(content) > MAX_EDIT:
        raise ValueError("bad content")
    for side in (True, False):
        p = resolve_item(type_, name, side)
        if p.exists() or p.is_symlink():
            raise ValueError(f"{name}: already exists at {tilde(p)}")
    target = resolve_item(type_, name, enabled)
    if ITEM_TYPES[type_]["kind"] == "dir":
        target = target / "SKILL.md"
    _reject_bad_json(target, content)
    atomic_write(target, content)
    return item_read(type_, name, None, enabled)
```

`atomic_write` already does `path.parent.mkdir(parents=True, exist_ok=True)`
(`bin/claude_ui/core.py:209`), so a nested name like `git/pr` and a skill's own
directory both come into existence on their own. Do not add a `mkdir` call.

**Verify**: `python3 -m compileall -q bin/claude_ui` → exit 0, no output.

### Step 3: Expose it as `POST /api/item-create`

In `bin/claude_ui/server.py`:

1. Add `item_create` to the `from .items import (...)` list at line 16, keeping
   the names alphabetical: `Conflict, config_files_state, item_create,
   item_read, item_save, ...`
2. Add a branch immediately **before** the existing `elif action == "item-save":`
   branch:

```python
            elif action == "item-create":
                self.send(200, {"ok": True, **item_create(
                    req.get("type", ""), req.get("name", ""),
                    req.get("content", ""),
                    bool(req.get("enabled", True)))})
```

No other change to `server.py`. The `ValueError` handler at the bottom of
`do_POST` already turns a name collision into a 400 with the message.

**Verify**: `python3 -m compileall -q bin/claude_ui` → exit 0, and
`grep -c "item-create" bin/claude_ui/server.py` → `1`.

### Step 4: Write tests/test_items.py

Create `tests/test_items.py`. Model its structure on `tests/test_plugins.py`
(read that file first — the `sys.path` insert, the `config_dir` monkey-patch
across every module that reads it, and the `tempfile.TemporaryDirectory` in
`setUp` are all load-bearing and must be copied).

The patching idiom you must reuse, from `tests/test_plugins.py:39-43`:

```python
        self._saved = [(m, m.config_dir) for m in (plugins, settings, core)]
        for m, _ in self._saved:
            m.config_dir = lambda t=self.tmp: t
```

For this file, patch `items` and `core` (not `plugins`/`settings`), and restore
in `tearDown` the same way `test_plugins.py` does.

Cases to cover, one test method each:

1. `test_creates_agent` — `item_create("agents", "reviewer", "---\nname: reviewer\n---\nbody\n")`
   creates `<tmp>/agents/reviewer.md` with exactly that text, and the return
   value has `name == "reviewer"`, `exists is True`, and `content` equal to what
   was passed.
2. `test_creates_skill_as_directory` — `item_create("skills", "pdf", "...")`
   creates `<tmp>/skills/pdf/SKILL.md`, and `<tmp>/skills/pdf` is a directory.
3. `test_creates_nested_command` — `item_create("commands", "git/pr", "...")`
   creates `<tmp>/commands/git/pr.md`.
4. `test_refuses_existing` — create once, create again with the same name →
   `ValueError`, and the file on disk still holds the *first* content.
5. `test_refuses_name_taken_on_disabled_side` — write
   `<tmp>/disabled/agents/reviewer.md` by hand, then
   `item_create("agents", "reviewer", ...)` → `ValueError`. This is the case the
   naive one-sided check gets wrong; it must be present.
6. `test_refuses_bad_name` — `item_create("agents", "../escape", "x")` →
   `ValueError`, and nothing is written outside `<tmp>`.
7. `test_refuses_empty_content` — `item_create("agents", "a", "   ")` →
   `ValueError`.
8. `test_no_model_invoke_flag` — write a skill at
   `<tmp>/skills/private/SKILL.md` whose frontmatter has
   `disable-model-invocation: true`, and another without it; assert
   `scan_items("skills")` reports `no_model_invoke` `True` and `False`
   respectively.

End the file with the same runner footer the other test files use
(`if __name__ == "__main__": unittest.main()`).

**Verify**: `python3 tests/test_items.py` → `OK`, 8 tests.

### Step 5: Full suite

**Verify**: `python3 -m unittest discover tests` → `OK`, exit 0. The count must
be 8 higher than before your change and no previously-passing test may fail.

## Test plan

- New file `tests/test_items.py`, 8 tests, listed in step 4.
- Structural pattern: `tests/test_plugins.py` — same `sys.path` bootstrap, same
  `config_dir` patching, same temp-dir lifecycle.
- Do not add network access to any test. `CLAUDE_UI_NET_TESTS` gates the one
  existing networked check and nothing here needs it.

## Done criteria

ALL must hold:

- [ ] `python3 -m unittest discover tests` exits 0
- [ ] `python3 tests/test_items.py` reports 8 tests, all passing
- [ ] `python3 -m compileall -q bin/claude_ui` exits 0 with no output
- [ ] `grep -n "def item_create" bin/claude_ui/items.py` returns exactly one match
- [ ] `grep -n "no_model_invoke" bin/claude_ui/items.py` returns exactly one match
- [ ] `git status --porcelain` lists only `bin/claude_ui/items.py`,
      `bin/claude_ui/server.py`, `tests/test_items.py` and `plans/`
- [ ] `plans/README.md` status row for 001 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The excerpts in "Current state" don't match the live files.
- `resolve_item` no longer validates names, or `ITEM_TYPES` has gained or lost a
  type — the create path's safety rests entirely on those two.
- `atomic_write` no longer creates parent directories; creating a skill would
  then fail and the fix is not obviously a one-liner.
- A test fails twice after a reasonable fix attempt.
- You find yourself wanting to touch `core.py`, `doctor.py` or anything under
  `static/` to make this work.

## Maintenance notes

- `item_create` deliberately takes finished content rather than a template name.
  Plans 002 and 003 compose the YAML in the browser so the live preview and the
  saved file are the same string. If a second producer of item content ever
  appears (a CLI, a plugin import), that is the moment to move templating into
  Python — not before.
- The two-sided name check is the subtle part. A reviewer should confirm it, and
  confirm the test for it exists: a one-sided check passes casual testing and
  fails the first time someone re-enables the twin.
- `no_model_invoke` is read straight from frontmatter text, so a skill whose
  value is written as `True` or `"true"` in quotes will read as `true`/`"true"`
  — the check lowercases and matches `true`/`yes`. A quoted `"true"` will *not*
  match. That is acceptable: the picker degrades to offering a skill Claude Code
  will skip, and logs nothing worse than a debug warning. Do not build a YAML
  parser for it.
