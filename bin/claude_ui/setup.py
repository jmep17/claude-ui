"""Setup pieces: one-shot, idempotent, in-place patches to the user's
environment. A piece patches (drop-in / key-merge), never replaces a file it
doesn't own, and can be removed. Install state is always derived by looking at
the target — nothing is recorded. Re-applying is safe and yields the same
result; removing touches only the piece's own artifacts.

The registry below is the extension point: each entry supplies a `state`
callable (returns the piece's current status) plus `apply` and `remove`. New
pieces (fish/tmux/ghostty drop-ins) slot in as more entries once their payload
exists in this checkout."""

from .statusline import statusline_apply, statusline_remove, statusline_state


def _statusline_state():
    st = statusline_state()
    installed = bool(st["script_exists"] and st["applied"])
    if installed:
        detail = f"script at {st['script_path']}, settings.json points at it"
    elif st["script_exists"]:
        detail = "script exists but settings.json isn't pointing at it"
    else:
        detail = "not installed"
    return {"id": "statusline", "label": "Claude Code statusline",
            "desc": "Generate the statusline script into the config dir and set "
                    "the one statusLine key in settings.json. Choose the fields "
                    "in the statusline tab; nothing else in settings is touched.",
            "installed": installed, "detail": detail,
            "target": st["script_path"], "removable": True}


PIECES = {
    "statusline": {"state": _statusline_state,
                   "apply": lambda: statusline_apply(),
                   "remove": statusline_remove},
}

def setup_state():
    return {"pieces": [PIECES[p]["state"]() for p in PIECES]}

def setup_apply(pid):
    if pid not in PIECES:
        raise ValueError("unknown setup piece")
    PIECES[pid]["apply"]()

def setup_remove(pid):
    if pid not in PIECES:
        raise ValueError("unknown setup piece")
    PIECES[pid]["remove"]()
