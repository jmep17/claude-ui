"""The handoff system as a setup piece: the hook, two skills, the settings wiring.

The handoff system was built by hand in the live config dir — a
SessionStart/PreCompact hook (handoff_load.py) that delivers parked briefs,
a writer skill (/handoff), a lister-resumer (/handoffs), four+one hook blocks
in settings.json, and a permissions.additionalDirectories entry that lets a
project-scoped session read the store. This piece vendors those files under
data/handoff/ and installs them, so a second machine is a checkout and one
Apply — the caveman contract, minus the upstream: this repo IS the upstream.

That last point sets the ownership rules. The hook script is the piece's
artifact the way statusline.sh is statusline's: Apply overwrites it whenever
it differs from the vendored copy, so fixes land by editing data/handoff/ and
re-applying, never by editing the installed file. The skills are ordinary
items once written and a user's edit wins — except that Apply will *adopt* an
installed copy whose only difference from the payload is the missing
x-claude-ui-preset stamp. That one narrow case is what turns a pre-piece hand
install into a piece-owned one without ever clobbering a real edit.

In settings.json the rule is caveman's: touch only what still says what we
wrote. For hooks that is a command string naming our script — either tilde or
absolute spelling, with or without --precompact; for
permissions.additionalDirectories it is the store path in either spelling.
"""

from pathlib import Path
import json

from .core import atomic_write, config_dir, parse_frontmatter, set_frontmatter_key, tilde
from .items import item_create, resolve_item
from .settings import settings_set, settings_state

PRESET_KEY = "x-claude-ui-preset"
PRESET_REF = "handoff@claude-ui"

DATA_DIR = Path(__file__).resolve().parent / "data" / "handoff"

SKILLS = ("handoff", "handoffs")
SESSION_MATCHERS = ("startup", "clear", "resume", "fork")
TIMEOUT = 10


def handoff_paths():
    """(installed hook script, brief store)."""
    cfg = config_dir()
    return cfg / "hooks" / "handoff_load.py", cfg / "handoffs"


# ---------------------------------------------------------------------------
# payloads
# ---------------------------------------------------------------------------

def _render(text):
    """Fill the vendored placeholders with this machine's paths. Two spellings
    on purpose: prose and commands read better with ~, while allowed-tools
    needs the exact absolute string the skill's own commands use."""
    hookp, storep = handoff_paths()
    return (text
            .replace("__HOOK__", str(hookp))
            .replace("__STORE__", str(storep))
            .replace("__STORE_TILDE__", tilde(storep)))


def _hook_payload():
    """The vendored hook, byte-identical — it resolves its own store through
    $CLAUDE_CONFIG_DIR, so there is nothing to render."""
    try:
        text = (DATA_DIR / "handoff_load.py").read_text()
    except OSError as e:
        raise ValueError(f"vendored handoff_load.py is unreadable ({e})")
    if not text.strip():
        raise ValueError("vendored handoff_load.py is empty")
    return text


def _payload(name):
    """A skill's SKILL.md, rendered and stamped."""
    try:
        text = (DATA_DIR / "skills" / name / "SKILL.md").read_text()
    except OSError as e:
        raise ValueError(f"vendored {name} SKILL.md is unreadable ({e})")
    if not text.strip():
        raise ValueError(f"vendored {name} SKILL.md is empty")
    return set_frontmatter_key(_render(text), PRESET_KEY, PRESET_REF)


# ---------------------------------------------------------------------------
# settings.json — hooks and the store permission
# ---------------------------------------------------------------------------

def _commands():
    """Every spelling of our hook commands, so ownership survives a hand-edit
    that expanded or collapsed the home directory — statusline's test."""
    hookp = handoff_paths()[0]
    out = []
    for s in (tilde(hookp), str(hookp)):
        out += [f"python3 {s}", f"python3 {s} --precompact"]
    return tuple(out)


def _store_spellings():
    storep = handoff_paths()[1]
    return (tilde(storep), str(storep))


def _session_blocks():
    cmd = f"python3 {handoff_paths()[0]}"
    return [{"matcher": m,
             "hooks": [{"type": "command", "command": cmd, "timeout": TIMEOUT}]}
            for m in SESSION_MATCHERS]


def _precompact_block():
    cmd = f"python3 {handoff_paths()[0]} --precompact"
    return {"matcher": "auto",
            "hooks": [{"type": "command", "command": cmd, "timeout": TIMEOUT}]}


def _hooks_of(data):
    # Same shape guard caveman makes; duplicated so the pieces stay independent.
    hooks = data.get("hooks")
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise ValueError("settings.json: hooks is not a JSON object — "
                         "fix it by hand first")
    return json.loads(json.dumps(hooks))       # deep copy; it is plain JSON


def _dirs_of(data):
    """permissions.additionalDirectories as a list, [] when absent, loud when
    either level is the wrong shape."""
    perms = data.get("permissions")
    if perms is None:
        return []
    if not isinstance(perms, dict):
        raise ValueError("settings.json: permissions is not a JSON object — "
                         "fix it by hand first")
    dirs = perms.get("additionalDirectories")
    if dirs is None:
        return []
    if not isinstance(dirs, list):
        raise ValueError("settings.json: permissions.additionalDirectories is "
                         "not a list — fix it by hand first")
    return list(dirs)


def _strip_ours(hooks):
    """(hooks without our entries, whether any was there).

    Caveman's rule over two events: an entry of ours leaves its block, a block
    goes only when it held nothing else, and an event key goes only when no
    block is left under it. A user who parked another command in one of our
    blocks keeps it."""
    cmds = _commands()
    out = dict(hooks)
    found = False
    for event in ("SessionStart", "PreCompact"):
        blocks = out.get(event)
        if not isinstance(blocks, list):
            continue
        kept = []
        for b in blocks:
            if not isinstance(b, dict) or not isinstance(b.get("hooks"), list):
                kept.append(b)
                continue
            inner = [h for h in b["hooks"]
                     if not (isinstance(h, dict) and h.get("command") in cmds)]
            if len(inner) != len(b["hooks"]):
                found = True
            if inner:
                kept.append({**b, "hooks": inner})
        if kept:
            out[event] = kept
        else:
            out.pop(event, None)
    return out, found


def _wired(hooks):
    """True when every block Apply would add is already there — right event,
    right matcher, either spelling. Position and spelling are deliberately not
    part of the test: a wired setup that a hand-edit moved or re-spelled is
    still wired, and Apply must not churn it back."""
    hookp = handoff_paths()[0]
    starts = {f"python3 {s}" for s in (tilde(hookp), str(hookp))}
    pres = {f"{c} --precompact" for c in starts}

    def matchers(event, cmds):
        found = set()
        blocks = hooks.get(event)
        if isinstance(blocks, list):
            for b in blocks:
                if not isinstance(b, dict) or not isinstance(b.get("hooks"), list):
                    continue
                if any(isinstance(h, dict) and h.get("command") in cmds
                       for h in b["hooks"]):
                    found.add(b.get("matcher", ""))
        return found

    return (set(SESSION_MATCHERS) <= matchers("SessionStart", starts)
            and "auto" in matchers("PreCompact", pres))


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------

def _skill_dir(name):
    return config_dir() / "skills" / name


def _skill_status(name):
    """(present, note) for one skill."""
    live = _skill_dir(name) / "SKILL.md"
    if live.is_file():
        try:
            text = live.read_text()
            payload = _payload(name)
        except (OSError, ValueError):
            return True, "installed, but could not be compared"
        if text == payload:
            return True, "installed"
        if set_frontmatter_key(text, PRESET_KEY, PRESET_REF) == payload:
            return True, "installed, unstamped — Apply adopts it"
        if parse_frontmatter(text).get(PRESET_KEY) == PRESET_REF:
            return True, "installed, older revision — Apply updates it"
        return True, "installed, and edited since — Apply leaves it alone"
    parked = resolve_item("skills", name, False, None)
    if parked.exists() or parked.is_symlink():
        return False, f"disabled, parked at {tilde(parked)}"
    return False, "not installed"


def _install_skill(name, payload):
    """Write the skill, adopt an unstamped identical copy, respect an edit."""
    live = _skill_dir(name) / "SKILL.md"
    if live.is_file():
        try:
            text = live.read_text()
        except OSError:
            return                       # unreadable is not ours to fix
        if text == payload:
            return
        if set_frontmatter_key(text, PRESET_KEY, PRESET_REF) == payload:
            atomic_write(live, payload)  # same skill, minus only our stamp
            return
        if parse_frontmatter(text).get(PRESET_KEY) == PRESET_REF:
            atomic_write(live, payload)  # stamped: this repo owns the revision
            return
        return                           # foreign, or a genuine edit
    parked = resolve_item("skills", name, False, None)
    if parked.exists() or parked.is_symlink():
        raise ValueError(f"a {name} skill is disabled at {tilde(parked)} — "
                         "enable or delete it on the Skills tab first")
    item_create("skills", name, payload)


# ---------------------------------------------------------------------------
# the piece
# ---------------------------------------------------------------------------

def handoff_state():
    hookp, storep = handoff_paths()
    st = settings_state()
    status = {n: _skill_status(n) for n in SKILLS}
    have_skills = all(ok for ok, _ in status.values())

    hook_ok, hook_note = False, "not installed"
    try:
        vendored = _hook_payload()
    except ValueError as e:
        vendored, hook_note = None, str(e)
    if hookp.is_file():
        try:
            current = hookp.read_text()
        except OSError:
            current = None
        if vendored is not None and current == vendored:
            hook_ok, hook_note = True, "installed"
        else:
            hook_note = "differs from the vendored copy — Apply overwrites it"

    wired = dir_ok = False
    err = st["error"]
    if not err:
        try:
            hooks = _hooks_of(st["data"])
            wired = _wired(hooks)
            dir_ok = any(d in _store_spellings() for d in _dirs_of(st["data"]))
        except ValueError as e:
            err = str(e)

    installed = bool(have_skills and hook_ok and wired and dir_ok)

    if err:
        detail = f"settings.json is unreadable ({err}) — fix it by hand first"
    elif installed:
        detail = (f"on — {tilde(hookp)} delivers parked briefs at session "
                  "start; /handoff parks one, /handoffs lists and resumes")
    elif not (any(ok for ok, _ in status.values()) or hookp.is_file()
              or wired or dir_ok):
        detail = ("installs the handoff hook, two skills, five hook blocks "
                  "and one permission; no plugin, no network")
    else:
        missing = [w for w, ok in (
            *((f"the {n} skill", ok) for n, (ok, _) in status.items()),
            ("the hook script", hook_ok),
            ("the settings.json hooks", wired),
            ("the store permission", dir_ok)) if not ok]
        detail = "half installed — Apply adds " + ", ".join(missing)

    return {
        "id": "handoff",
        "label": "Handoff briefs",
        "desc": ("Install the handoff system, vendored in this repo: /handoff "
                 "ends a session by parking a structured brief, a "
                 "SessionStart/PreCompact hook delivers it to the session it "
                 "was reserved for, and /handoffs lists what is parked and "
                 "resumes one in place. The script writes the brief itself "
                 "and groups it by repository. Apply wires five hook blocks "
                 f"and one permissions entry for {tilde(storep)} so a "
                 "project-scoped session can read the store. Remove unhooks; "
                 "parked briefs are never touched"),
        "installed": installed,
        "detail": detail,
        "target": tilde(hookp),
        "removable": True,
        "notes_label": "What it writes (one script, two skills, two settings.json keys)",
        "notes": [
            f"{tilde(hookp)} — the hook and its "
            "--list/--take/--new/--facts/--reindex/--groups/--migrate CLI — "
            f"{hook_note}",
            f"{tilde(_skill_dir('handoff'))}/ — /handoff, writes a brief — "
            f"{status['handoff'][1]}",
            f"{tilde(_skill_dir('handoffs'))}/ — /handoffs, lists and resumes — "
            f"{status['handoffs'][1]}",
            f"{tilde(storep)}/ — the store; created on Apply, one directory per "
            "repo (every worktree folds into one), INDEX.md regenerated by the "
            "hook; never deleted",
            "settings.json hooks — four SessionStart blocks (startup, clear, "
            "resume, fork) and one PreCompact(auto); blocks already there are "
            "left byte-identical",
            "settings.json permissions.additionalDirectories — one entry, so "
            "a session scoped to any project can still read the store",
        ],
    }


def handoff_apply():
    """Install everything. Every write is guarded on already being right, so a
    re-apply touches nothing — the caveman contract."""
    hookp, storep = handoff_paths()
    hook_src = _hook_payload()                       # loud before writing
    payloads = {n: _payload(n) for n in SKILLS}      # ditto

    st = settings_state()
    if st["error"]:
        raise ValueError(f"settings.json has invalid JSON — fix it by hand "
                         f"first ({st['error']})")
    hooks = _hooks_of(st["data"])          # loud if hooks is the wrong shape
    dirs = _dirs_of(st["data"])            # loud if permissions is

    for name in SKILLS:
        _install_skill(name, payloads[name])

    try:
        current = hookp.read_text()
    except OSError:
        current = None
    if current != hook_src:
        atomic_write(hookp, hook_src)

    if not storep.is_dir():
        storep.mkdir(parents=True, exist_ok=True)

    if not _wired(hooks):
        stripped, _ = _strip_ours(hooks)
        stripped.setdefault("SessionStart", []).extend(_session_blocks())
        stripped.setdefault("PreCompact", []).append(_precompact_block())
        settings_set("hooks", stripped)

    if not any(d in _store_spellings() for d in dirs):
        settings_set("permissions.additionalDirectories",
                     dirs + [tilde(storep)])


def handoff_remove():
    """Unregister the hooks, drop the store permission, delete the script.

    The skills stay: once written they are ordinary items, and the Skills tab
    is where items are deleted — /handoffs will report the missing script
    until then, loudly, which is that skill's own contract. The store and
    every brief in it is user data this piece never deletes."""
    hookp = handoff_paths()[0]
    hookp.unlink(missing_ok=True)
    st = settings_state()
    if st["error"]:
        return                              # nothing safe to do; state says so
    try:
        hooks = _hooks_of(st["data"])
        dirs = _dirs_of(st["data"])
    except ValueError:
        return
    stripped, found = _strip_ours(hooks)
    if found:
        settings_set("hooks", stripped or None)
    kept = [d for d in dirs if d not in _store_spellings()]
    if kept != dirs:
        settings_set("permissions.additionalDirectories", kept or None)
