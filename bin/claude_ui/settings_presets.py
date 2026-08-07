"""Settings presets: curated batches of settings.json values, shipped as data
files and applied as one atomic write through settings_set_many(). Each preset
is a setup piece (see setup.py): install state is derived by comparing every
key against the preset value, apply is idempotent, and remove clears a key
only while it still holds the value we wrote — an edit made since is the
user's, and stays (the statusline rule).

The token-saver preset is the researched answer to "cut API token spend
without gutting the tool". What it deliberately does NOT touch is as
load-bearing as what it sets:

  - permissionExplainerEnabled — settings.json silently ignores it; it is a
    ~/.claude.json global-config key (schema.GLOBAL_CONFIG_KEYS), and the
    doctor warns on exactly this mistake. test_setup.py pins the whole class
    out of every preset.
  - DISABLE_PROMPT_CACHING (and per-family variants) — reads like a saver, is
    a cost multiplier: cache reads are ~10x cheaper than fresh input.
  - MAX_THINKING_TOKENS=0 / CLAUDE_CODE_DISABLE_THINKING — kills reasoning,
    and adaptive-reasoning models ignore the budget anyway; effortLevel is
    the honest lever.
  - ENABLE_PROMPT_CACHING_1H — cache writes bill higher; only wins for
    long-idle sessions, wrong as a default.
  - autoCompactEnabled: false — skipping compaction grows per-turn input to
    the context ceiling; the default is already the cheap side.
  - CLAUDE_CODE_MAX_OUTPUT_TOKENS / MAX_MCP_OUTPUT_TOKENS caps — truncation
    causes retries and rework, which costs more than it saves.
  - includeGitInstructions: false — loses git/PR workflow competence for a
    one-time context saving.
  - CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS — Explore/Plan subagents keep
    file dumps out of the main context; they are token-efficient, not waste.
  - DISABLE_COST_WARNINGS — hides the signal this preset exists to manage.
  - CLAUDE_CODE_SIMPLE / SAFE_MODE — gut the tool.
  - outputStyle — a preference, never overridden by a preset."""

import json
from pathlib import Path

from .settings import SETTINGS_KEY_RE, settings_set_many, settings_state

PRESET_DIR = Path(__file__).resolve().parent / "data" / "presets" / "settings"

# Label and one-line description per shipped preset; the keys, values and
# per-key rationale live in the data file so the next preset is a JSON file
# plus one row here plus one PIECES entry in setup.py.
PRESETS = {
    "token-saver": {
        "label": "Token saver",
        "desc": "Cheaper defaults for pay-per-token API use: Sonnet main "
                "model, Haiku subagents, medium effort, smaller workflows, "
                "and fewer model-generated extras. Only these keys are "
                "written; Remove clears just the ones still at preset values.",
    },
}

_MISSING = object()


def preset_entries(pid):
    """The preset's (key, value, why) rows, loudly validated.

    Loud because this runs on apply, where a broken shipped file must be a
    400, not a silent partial write. preset_state() catches the error and
    shows it instead — the output-styles split: runtime degrades to a visible
    broken row, the test suite is where shipping a bad file fails the build."""
    path = PRESET_DIR / f"{pid}.json"
    try:
        entries = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"preset {pid}: unreadable ({e})")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"preset {pid}: not a non-empty list")
    seen = set()
    for e in entries:
        if not isinstance(e, dict) or "value" not in e:
            raise ValueError(f"preset {pid}: entry without key/value/why")
        key, why = e.get("key"), e.get("why")
        if not (isinstance(key, str) and SETTINGS_KEY_RE.match(key)):
            raise ValueError(f"preset {pid}: bad key {key!r}")
        if not (isinstance(why, str) and why.strip()):
            raise ValueError(f"preset {pid}: {key} has no why")
        if key in seen:
            raise ValueError(f"preset {pid}: duplicate key {key}")
        seen.add(key)
    return entries


def _current(data, key):
    """Dotted-path lookup returning _MISSING when absent, so a stored false
    or null is correctly distinguished from an unset key."""
    node = data
    for p in key.split("."):
        if not isinstance(node, dict) or p not in node:
            return _MISSING
        node = node[p]
    return node


def preset_state(pid):
    """The setup-piece state dict, derived entirely by inspection."""
    meta = PRESETS[pid]
    st = {"id": pid, "label": meta["label"], "desc": meta["desc"],
          "installed": False, "detail": "", "target": None, "removable": True,
          "notes": []}
    try:
        entries = preset_entries(pid)
    except ValueError as e:
        st["detail"] = str(e)
        return st
    st["notes"] = [f'{e["key"]} = {json.dumps(e["value"])} — {e["why"]}'
                   for e in entries]
    settings = settings_state()
    st["target"] = settings["path"]
    if settings["error"]:
        st["detail"] = f"settings.json unreadable: {settings['error']}"
        return st
    n = len(entries)
    matched = sum(1 for e in entries
                  if _current(settings["data"], e["key"]) == e["value"])
    if matched == n:
        st["installed"] = True
        st["detail"] = f"all {n} keys at preset values"
    elif matched:
        st["detail"] = f"{matched} of {n} keys at preset values — Apply sets the rest"
    else:
        st["detail"] = f"writes {n} settings.json keys, none currently at preset values"
    return st


def preset_apply(pid):
    settings_set_many((e["key"], e["value"]) for e in preset_entries(pid))


def preset_remove(pid):
    """Clear only the keys still holding the preset's value. A key the user
    has since changed is theirs now; pruning in settings_set_many drops an
    env dict the removal emptied, while one holding user keys survives."""
    data = settings_state()["data"]
    settings_set_many((e["key"], None) for e in preset_entries(pid)
                      if _current(data, e["key"]) == e["value"])
