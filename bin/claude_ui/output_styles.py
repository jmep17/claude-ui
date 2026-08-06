"""Output-style frontmatter fields, and the presets shipped with the app.

There is no official JSON Schema for output-style frontmatter — schemastore
publishes one for settings.json and nothing else — so unlike settings.py, this
list has no upstream to merge facts in over the top of. It is hand-written from
https://code.claude.com/docs/en/output-styles and is the only description of
these four fields the app has. When that page changes, this file is what needs
editing, and tests/test_output_styles.py is what will notice.

Everything here is read by three callers that must not disagree: the create form
renders FIELDS, item creation runs validate() over what the form produced, and
the preset test runs validate(strict=True) over every file in data/presets/.
"""

from pathlib import Path

from .core import parse_frontmatter


PRESET_DIR = Path(__file__).resolve().parent / "data" / "presets" / "output-styles"

DOC = "output-styles"

# The four fields Claude Code reads. `type` is the control the form renders,
# matching the vocabulary settings.py uses: "text" or "bool".
FIELDS = [
    {"key": "name", "type": "text", "default": "",
     "desc": "Shown in the /config picker; defaults to the filename",
     "help": "The style's display name. Leave it blank and Claude Code uses the "
             "filename instead, so a file named adhd.md becomes the style "
             "\"adhd\". Setting it lets the file name and the display name "
             "differ."},
    {"key": "description", "type": "text", "default": "",
     "desc": "One line, shown under the name in the picker",
     "help": "A single line describing what the style does. It appears beside "
             "the name in /config so you can tell two styles apart without "
             "opening them."},
    {"key": "keep-coding-instructions", "type": "bool", "default": False,
     "desc": "Keep Claude Code's built-in software-engineering instructions",
     "help": "Off by default, and off means Claude Code drops its built-in "
             "software-engineering instructions — how it scopes changes, "
             "writes comments and verifies work — and runs on your style "
             "alone. Turn it on when you are changing how Claude talks but "
             "still want it coding the same way. Leave it off only for a style "
             "that is not about software at all, like a writing assistant."},
    {"key": "force-for-plugin", "type": "bool", "default": False,
     "desc": "Plugin styles only — auto-apply whenever the plugin is enabled",
     "help": "Only does anything in a style shipped inside a plugin. It makes "
             "the style apply automatically whenever that plugin is enabled, "
             "overriding whatever outputStyle the user set. If several enabled "
             "plugins set it, the first one loaded wins. A style in your own "
             "config dir is not a plugin, so this field is ignored there."},
]

FIELD_KEYS = {f["key"] for f in FIELDS}

_BOOL_FIELDS = {f["key"] for f in FIELDS if f["type"] == "bool"}

# parse_frontmatter hands back raw strings, so a bool arrives as whatever the
# file said. This is the same lowercase test items.py uses on
# disable-model-invocation; a quoted "true" reads as literal `"true"` and is
# rejected, which is the honest answer — Claude Code would not read it as true
# either. Do not add a YAML parser: the backend is stdlib only.
_TRUE = ("true", "yes")
_FALSE = ("false", "no", "")


def validate(meta, strict=False):
    """Error messages for a parsed frontmatter dict; empty list means valid.

    strict=True also rejects keys Claude Code does not read, and is what the
    bundled presets are held to — a typo in a preset we ship is a bug in this
    repo. User input is checked loosely instead, because Claude Code tolerates
    extra frontmatter and this app must never be the stricter of the two.
    """
    errs = []
    for key in _BOOL_FIELDS:
        if key not in meta:
            continue
        val = str(meta[key]).strip().lower()
        if val not in _TRUE and val not in _FALSE:
            errs.append(f"{key}: expected true or false, got {meta[key]!r}")
    if strict:
        for key in sorted(set(meta) - FIELD_KEYS):
            errs.append(f"{key}: not a field Claude Code reads")
    return errs


def is_true(meta, key):
    """Whether a bool frontmatter field is on, by Claude Code's reading."""
    return str(meta.get(key, "")).strip().lower() in _TRUE


def presets():
    """The output styles shipped with the app, as create-form starting points.

    A preset that fails strict validation is skipped rather than raised on: a
    bad file we ship must cost the user a missing choice, not a dead page. The
    test suite is where that failure is meant to surface, loudly.
    """
    out = []
    if not PRESET_DIR.is_dir():
        return out
    for p in sorted(PRESET_DIR.glob("*.md")):
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        meta = parse_frontmatter(text)
        if validate(meta, strict=True):
            continue
        out.append({
            "id": p.stem,
            "name": meta.get("name") or p.stem,
            "description": meta.get("description", ""),
            "keep-coding-instructions": is_true(meta, "keep-coding-instructions"),
            "body": text.split("---\n", 2)[-1].lstrip("\n"),
            "content": text,
        })
    return sorted(out, key=lambda s: s["name"].lower())
