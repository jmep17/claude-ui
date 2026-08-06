# Plan 005: Offer all 31 hook events in the builder, not the 9 it still knows

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 49ae8ba..HEAD -- bin/claude_ui/settings.py bin/claude_ui/server.py bin/claude_ui/static/app.js docs/IDEAS.md`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `49ae8ba`, 2026-08-06

## Why this matters

There are two constants named `HOOK_EVENTS` in this repo. The Python one is
schema-driven, holds all 31 documented lifecycle events, and has a test pinning
it. The JavaScript one is a hardcoded list of 9, and it is the one the **Add
hook** dropdown actually renders. So 22 documented events — including
`SubagentStart`, `PermissionRequest`, `PostToolUseFailure`, `TaskCompleted`,
`ConfigChange`, `WorktreeCreate` — cannot be selected from the UI at all.

Worse, the two disagree in a way that is invisible until you hit it:
`hook_test()` validates a fired event against the Python list of 31
(`bin/claude_ui/settings.py:812`), so the backend accepts events the picker
will not offer.

`docs/IDEAS.md` records this as already fixed. Under "What the first sync
corrected" it lists *"the hooks builder knew 9 of 31 events"* among the things
the schema sync put right. It did put the Python side right. The picker was
never rewired, so the documentation is now wrong about the state of the code.

This plan is the tail end of work that already shipped, not new work: the
correct list already exists, already comes from the vendored schema, and already
has a test. It just never reaches the browser.

## Current state

### The verification that found this

Run against commit `49ae8ba` on 2026-08-06 — the vendored settings schema is
current and every documented count in the README still holds:

```
$ python3 tools/sync_settings_schema.py --check
up to date: bin/claude_ui/data/settings_schema.json (fetch date aside)

$ CLAUDE_UI_NET_TESTS=1 python3 -m unittest discover tests
Ran 265 tests in 1.359s
OK
```

| README claim | Actual |
|---|---|
| 141 real top-level properties | 141 |
| 590 dotted keys | 590 |
| 100% of them described | 590/590 |
| 340 `env.*` vars | 340 |
| 40 `sandbox.*` sub-keys | 40 |
| 31 `hooks.*` events | 31 |
| 6 global-config keys excluded on purpose | exactly those 6 |
| 22 keys not listed in the official schema | exactly those 22 |
| all docs deep-links on `code.claude.com` | 388/388 |

The one thing that does not hold: **the hooks builder offers 9 of those 31.**

### The correct list, in Python

`bin/claude_ui/settings.py:784-791`:

```python
# The nine events the hooks builder used to know, kept as the head of the list
# so the common ones stay at the top of the event picker. The rest — 22 more —
# come from the official schema's hooks.* properties.
HOOK_EVENTS_COMMON = ["SessionStart", "UserPromptSubmit", "PreToolUse",
                      "PostToolUse", "Notification", "Stop", "SubagentStop",
                      "PreCompact", "SessionEnd"]

HOOK_EVENTS = HOOK_EVENTS_COMMON + [e for e in schema.hook_events()
                                    if e not in HOOK_EVENTS_COMMON]
```

The comment even says the picker is the reason for the ordering. It is fed by
`schema.hook_events()` (`bin/claude_ui/schema.py:304-307`):

```python
def hook_events():
    """Documented lifecycle hook event names, from the snapshot's hooks.* keys."""
    return sorted(k[6:] for k in official()
                  if k.startswith("hooks.") and "." not in k[6:])
```

and pinned by `tests/test_settings.py:307-308`:

```python
    def test_hook_events_cover_the_documented_set(self):
        self.assertTrue(set(schema.hook_events()) <= set(settings.HOOK_EVENTS))
```

Its only consumer today is the validator in `hook_test`
(`bin/claude_ui/settings.py:812`):

```python
    if event not in HOOK_EVENTS:
```

### The stale list, in JavaScript

`bin/claude_ui/static/app.js:640-642`:

```js
const HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse",
  "PostToolUse", "Notification", "Stop", "SubagentStop", "PreCompact",
  "SessionEnd"];
```

Used once, at `bin/claude_ui/static/app.js:672`:

```js
      { id: "e", label: "Event", type: "select", options: HOOK_EVENTS },
```

### The 22 missing events

`ConfigChange`, `CwdChanged`, `DirectoryAdded`, `Elicitation`,
`ElicitationResult`, `FileChanged`, `InstructionsLoaded`, `MessageDisplay`,
`PermissionDenied`, `PermissionRequest`, `PostCompact`, `PostToolBatch`,
`PostToolUseFailure`, `Setup`, `StopFailure`, `SubagentStart`, `TaskCompleted`,
`TaskCreated`, `TeammateIdle`, `UserPromptExpansion`, `WorktreeCreate`,
`WorktreeRemove`.

### How server data reaches the page

`GET /api/state` (`bin/claude_ui/server.py:106-117`) returns the object the
frontend keeps in `DATA`:

```python
        elif self.path == "/api/state":
            self.send(200, {
                "items": {t: scan_items(t) for t in ITEM_TYPES},
                "config_files": config_files_state(),
                "settings": settings_state(),
                "suggest": suggest_state(),
                "mcp": mcp_state(),
                "statusline": statusline_state(),
                "config_dir": tilde(config_dir()),
                "default_dir": "config_dir" not in read_cfg()
                               and not os.environ.get("CLAUDE_CONFIG_DIR"),
            })
```

There is a convention here you must follow. The page's inlined schema is
rendered by **calling** `settings_schema()` per request rather than reading a
module constant, and `bin/claude_ui/server.py:87-89` says why:

```python
            # a call, not the module constant: a live schema fetch that landed
            # after start-up is picked up on the next page render
            self.send(200, page.replace("__SCHEMA__", json.dumps(settings_schema()))
```

`schema.start_schema_fetch()` (`server.py:296`) refreshes the snapshot in a
background thread after the server is already serving, so anything computed once
at import time is frozen at whatever the vendored file said. `HOOK_EVENTS` is a
module-level constant computed at import. **Turning it into a function is part
of this fix, not an optional tidy-up** — otherwise a live fetch that adds a
32nd event still would not reach the picker.

### Conventions

- Python 3 standard library only; no build step on the frontend.
- Comments explain why. Match the voice of `settings.py:784-786` and
  `server.py:87-88`.
- Tests are stdlib `unittest`: `python3 -m unittest discover tests`.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests | `python3 -m unittest discover tests` | `OK`, exit 0 |
| Tests incl. network | `CLAUDE_UI_NET_TESTS=1 python3 -m unittest discover tests` | `OK`, exit 0 |
| Schema freshness | `python3 tools/sync_settings_schema.py --check` | `up to date: …`, exit 0 |
| Syntax (Python) | `python3 -m compileall -q bin/claude_ui` | exit 0, no output |
| Syntax (JS) | `node --check bin/claude_ui/static/app.js` | exit 0, no output |
| Run the app | `bin/claude-ui --no-open --port 7455` | prints the URL |

## Scope

**In scope**:

- `bin/claude_ui/settings.py` (modify — `HOOK_EVENTS` becomes a function)
- `bin/claude_ui/server.py` (modify — one key in the `/api/state` payload)
- `bin/claude_ui/static/app.js` (modify — read the list from `DATA`)
- `tests/test_settings.py` (modify — one new test)
- `docs/IDEAS.md` (modify — correct the claim that this was already fixed)

**Out of scope** (do NOT touch):

- `bin/claude_ui/schema.py` — `hook_events()` is already correct and already
  tested. Do not change it.
- `bin/claude_ui/data/settings_schema.json` — verified current on 2026-08-06.
  Do not regenerate it as part of this plan; that is a separate, reviewable
  commit by design (see the README's "Settings help" section: *"Read the diff
  before committing"*).
- The hooks builder's other gaps — editing an existing hook, reordering within
  a matcher group, soft-disable, a recipe picker. That is idea #14 in
  `docs/IDEAS.md` ("Hooks builder v2") and is a much larger piece of work. This
  plan only fixes which events the picker offers.
- `hook_sample()` — it returns a generic payload for events it has no special
  case for, which is correct behaviour for the 22 newly-reachable events. Do
  not add 22 sample payloads.
- The 22 `unverified` settings keys and the 6 excluded global-config keys. Both
  sets were verified correct on 2026-08-06 and are frozen by existing tests.

## Git workflow

- Branch: `advisor/005-hook-events-drift`
- Commit style: imperative, sentence case, user-visible. Examples from
  `git log`: `Drive settings help and coverage from the official JSON Schema`,
  `Show and set the model a plugin's agents run on`. Suggested:
  `Offer every documented hook event, not the nine hard-coded ones`.
- Do NOT push or open a PR.

## Steps

### Step 1: Make the Python list a function

In `bin/claude_ui/settings.py`, replace the module constant at lines 790-791
with a function, and keep `HOOK_EVENTS_COMMON` exactly as it is:

```python
def hook_events():
    """Every documented lifecycle event, common ones first.

    A function rather than a constant because start_schema_fetch() refreshes the
    snapshot in the background after the server is already serving — the same
    reason server.py calls settings_schema() per request instead of reading a
    module-level value.
    """
    return HOOK_EVENTS_COMMON + [e for e in schema.hook_events()
                                 if e not in HOOK_EVENTS_COMMON]
```

Update the one existing consumer at line 812 from `if event not in HOOK_EVENTS:`
to `if event not in hook_events():`.

Then `grep -n "HOOK_EVENTS" bin/claude_ui/settings.py` must show only
`HOOK_EVENTS_COMMON` — if a bare `HOOK_EVENTS` remains anywhere in Python, you
have missed a caller.

**Verify**: `python3 -m compileall -q bin/claude_ui` → exit 0, and
`python3 -m unittest discover tests` → note which tests fail; `test_settings.py`
references `settings.HOOK_EVENTS` at line 308 and you fix that in step 4. A
failure there and nowhere else is expected at this point.

### Step 2: Send it to the page

In `bin/claude_ui/server.py`:

1. Add `hook_events` to the `from .settings import (...)` list at line 24,
   keeping it alphabetical.
2. Add one key to the `/api/state` payload, after `"suggest": suggest_state(),`:

```python
                # a call, not a constant, for the same reason the inlined schema
                # is: a live schema fetch that lands after start-up must reach
                # the hooks picker without a restart
                "hook_events": hook_events(),
```

**Verify**: start the app on port 7455, then
`curl -s http://127.0.0.1:7455/api/state | python3 -c "import json,sys; e=json.load(sys.stdin)['hook_events']; print(len(e)); print(e[:9])"`
→ `31`, followed by the nine common events in their original order. Stop the
server afterwards.

### Step 3: Read it in the browser

In `bin/claude_ui/static/app.js`, delete the hardcoded constant at lines 640-642
and replace it with an accessor that falls back to the common nine if
`/api/state` has not landed yet:

```js
/* The event list comes from the server, which builds it from the official
   settings schema — 31 events, not the nine this file used to hard-code. The
   fallback is only for the window before the first /api/state resolves. */
const hookEvents = () => (DATA.hook_events && DATA.hook_events.length)
  ? DATA.hook_events
  : ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
     "Notification", "Stop", "SubagentStop", "PreCompact", "SessionEnd"];
```

Update the one use at line 672 to `options: hookEvents()`.

`modal()` already wraps a `<select>` in `filterSelect()` (`ui.js:496`), and
`filterSelect` switches to the type-to-filter combobox above `FSEL_MIN` (6)
options (`ui.js:666, 745`). So a 31-item list becomes searchable automatically —
do not add scrolling, grouping or a second control.

**Verify**: `node --check bin/claude_ui/static/app.js` → exit 0. Then manually:
open the app, go to Settings, click **Add hook**, and confirm the Event field is
a filterable combobox, that typing `subagent` narrows to `SubagentStop` and
`SubagentStart`, and that `SessionStart` is still the first entry.

### Step 4: Fix and extend the tests

In `tests/test_settings.py`, update the existing test at lines 307-308 to call
the function:

```python
    def test_hook_events_cover_the_documented_set(self):
        self.assertTrue(set(schema.hook_events()) <= set(settings.hook_events()))
```

Add one new test beside it, asserting the two properties that actually broke:

```python
    def test_hook_events_lead_with_the_common_ones(self):
        events = settings.hook_events()
        self.assertEqual(events[:len(settings.HOOK_EVENTS_COMMON)],
                         settings.HOOK_EVENTS_COMMON)
        self.assertEqual(len(events), len(set(events)))
        # the regression this guards: the picker offered nine of these
        self.assertGreater(len(events), len(settings.HOOK_EVENTS_COMMON))
        self.assertIn("SubagentStart", events)
```

**Verify**: `python3 -m unittest discover tests` → `OK`, one more test than
before, and no failures.

### Step 5: Correct the documentation

`docs/IDEAS.md` lists this under "What the first sync corrected" in the
**Settings coverage** section, in the sentence beginning *"…and the hooks
builder knew 9 of 31 events; and `ENV_VARS` had drifted in both directions."*

That claim was true of the Python constant and false of the picker. Reword it so
the record is accurate — something in the file's existing voice, e.g. that the
sync gave `settings.py` all 31 events while the browser kept its own hard-coded
nine until this change. Do not restructure the section; change the claim and
nothing else.

While you are in the file, check the **Medium value → 14. Hooks builder v2**
entry: it should no longer imply the event list is a gap, since it will not be
one. Adjust only if it says so; leave it alone otherwise.

**Verify**: `grep -n "9 of 31" docs/IDEAS.md` → returns either nothing or a line
whose surrounding sentence is now accurate about both sides.

### Step 6: Confirm nothing else drifted

**Verify**, all four:

- `python3 tools/sync_settings_schema.py --check` → `up to date: …`, exit 0
- `CLAUDE_UI_NET_TESTS=1 python3 -m unittest discover tests` → `OK`
- `python3 -m compileall -q bin/claude_ui` → exit 0
- `node --check bin/claude_ui/static/app.js` → exit 0

## Test plan

- Modified: `tests/test_settings.py::test_hook_events_cover_the_documented_set`
  — call the function instead of the removed constant.
- New: `tests/test_settings.py::test_hook_events_lead_with_the_common_ones` —
  asserts the common nine lead the list, that there are no duplicates, that the
  list is strictly longer than the common nine (the actual regression), and that
  a specific schema-derived event is present.
- Structural pattern: the surrounding tests in `tests/test_settings.py`.
- The frontend change has no automated coverage — this repo has no frontend test
  runner, by design. Verify it manually per step 3 and report the result.

## Done criteria

ALL must hold:

- [ ] `python3 -m unittest discover tests` exits 0, with one more test than
      before
- [ ] `CLAUDE_UI_NET_TESTS=1 python3 -m unittest discover tests` exits 0
- [ ] `python3 tools/sync_settings_schema.py --check` exits 0
- [ ] `python3 -m compileall -q bin/claude_ui` exits 0
- [ ] `node --check bin/claude_ui/static/app.js` exits 0
- [ ] `grep -n "HOOK_EVENTS" bin/claude_ui/settings.py` shows only
      `HOOK_EVENTS_COMMON`
- [ ] `grep -n "const HOOK_EVENTS" bin/claude_ui/static/app.js` returns nothing
- [ ] `/api/state` returns a `hook_events` array of length 31 whose first nine
      are the common events in order
- [ ] The **Add hook** dialog's Event field is a filterable combobox offering all
      31 events, `SessionStart` first
- [ ] `docs/IDEAS.md` no longer claims the picker already knew all 31
- [ ] `git status --porcelain` lists only the five in-scope files and `plans/`
- [ ] `plans/README.md` status row for 005 updated

## STOP conditions

Stop and report back (do not improvise) if:

- `python3 tools/sync_settings_schema.py --check` exits non-zero **before** you
  change anything. That means the vendored schema went stale between this plan
  being written and you running it, and the counts in "Current state" no longer
  describe reality. Report the diff; do not run the sync yourself as part of this
  plan — the README requires that diff be read and committed on its own.
- `schema.hook_events()` returns fewer than 31 events.
- Any test other than `test_hook_events_cover_the_documented_set` fails after
  step 1.
- Making the picker filterable appears to need a change to `ui.js`,
  `filterSelect` or `FSEL_MIN`. It should need none; report what you found.
- You find a third copy of the event list anywhere in the repo.

## Maintenance notes

- The general shape of this bug — a constant duplicated across the Python/JS
  boundary, one side wired to the schema and the other frozen — is worth
  grepping for once. `MODELS`, `HOOK_EVENTS` and the permission-mode lists are
  the candidates. A reviewer should ask whether any other picker in `app.js`
  hard-codes a vocabulary the backend already derives.
- Plans 002–004 in this directory introduce exactly this pattern deliberately
  for **agent** frontmatter, because agent frontmatter has no published schema
  to derive from. The difference matters: `settings.json` has a schema and any
  hard-coded copy of it is a bug; agent fields have none and a hand-maintained
  snapshot with a drift note is the best available. Do not "fix" the agent
  constants by pointing them at a schema that does not exist.
- After this lands, `hook_events()` has two callers — `hook_test`'s validator
  and `/api/state`. If a third appears, that is fine; it is a pure function over
  the snapshot.
