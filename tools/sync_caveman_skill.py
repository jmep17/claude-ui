#!/usr/bin/env python3
"""Regenerate bin/claude_ui/data/presets/skills/* from upstream caveman.

    python3 tools/sync_caveman_skill.py            # fetch and write
    python3 tools/sync_caveman_skill.py --check    # diff only, exit 1 if stale
    python3 tools/sync_caveman_skill.py --ref v2.1.0        # move the pin
    python3 tools/sync_caveman_skill.py --skill caveman     # just the one

Covers every file of every skill the piece ships — `caveman` is one markdown
file, `caveman-compress` is a tree with a scripts/ package — and the list comes
from what is on disk, so adding a file to the vendored tree brings it under this
check without touching this script.

The vendored files are what the caveman setup piece installs, and they are kept
byte-identical to the upstream tag so this comparison can be a plain diff. The
`x-claude-ui-preset` stamp naming the ref is added at install time by
caveman._payload(), never here.

The pin lives in caveman.REF, so moving it is a one-line edit there followed by
a run of this script. Review the diff: this file is a *prompt*, and a reworded
rule changes how every session behaves. That is exactly what vendoring it is
for — upstream cannot quietly change the way the assistant talks on your
machine.

Imports the pin and the destination from claude_ui.caveman — the app must never
depend on tools/, so the direction is one-way.
"""

import argparse
import difflib
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))

from claude_ui import caveman, core  # noqa: E402

RAW = "https://raw.githubusercontent.com/JuliusBrussee/caveman/{ref}/skills/{rel}"


def fetch(url):
    req = urllib.request.Request(url, headers={"user-agent": "claude-ui"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode(errors="replace")


def vendored_files(name):
    """Every file we ship for a skill, as paths relative to skills/<name>/.

    Driven by what is on disk rather than a hard-coded list: adding a file to
    the vendored tree is enough to bring it under this check. A file upstream
    adds that we do not ship is not drift — the piece installs what it installs.
    """
    root = caveman.PRESET_DIR / name
    return sorted(p.relative_to(root).as_posix()
                  for p in root.rglob("*") if p.is_file())


def validate(name, rel, text, ref):
    """Refuse a payload the piece could not install — a 404 body, a redirect
    page, or a SKILL.md whose frontmatter no longer says what we install."""
    if not text.strip():
        raise ValueError(f"upstream returned an empty {rel}")
    if rel != "SKILL.md":
        return text
    meta = core.parse_frontmatter(text)
    if meta.get("name") != name:
        raise ValueError(f"upstream {ref} {rel} has no `name: {name}` "
                         "frontmatter — the skill may have moved")
    if not meta.get("description", "").strip():
        raise ValueError(f"upstream {ref} {name} has no description")
    if "CLAUDE_PLUGIN_ROOT" in text:
        raise ValueError(f"upstream {ref} {name} now references "
                         "CLAUDE_PLUGIN_ROOT — the skill needs its plugin")
    return text


def one(name, rel, ref, check_only):
    """(exit code, changed) for a single vendored file."""
    path = caveman.PRESET_DIR / name / rel
    text = validate(name, rel, fetch(RAW.format(ref=ref, rel=f"{name}/{rel}")), ref)
    old = path.read_text() if path.is_file() else ""
    if old == text:
        return 0, False
    if check_only:
        sys.stderr.writelines(difflib.unified_diff(
            old.splitlines(True), text.splitlines(True),
            fromfile=f"vendored {name}/{rel}",
            tofile=f"upstream {ref} {name}/{rel}", n=2))
        return 1, True
    core.atomic_write(path, text)
    print(f"wrote {path.relative_to(REPO)} ({len(text.encode())} bytes)")
    return 0, True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="report drift without writing; exit 1 if stale")
    ap.add_argument("--ref", default=caveman.REF,
                    help=f"upstream tag to compare against (default {caveman.REF})")
    ap.add_argument("--skill", action="append", choices=list(caveman.ALL_SKILLS),
                    help="limit to one skill (repeatable; default all)")
    args = ap.parse_args(argv)

    stale = 0
    seen = 0
    for name in (args.skill or caveman.ALL_SKILLS):
        for rel in vendored_files(name):
            seen += 1
            try:
                code, _ = one(name, rel, args.ref, args.check)
            except (OSError, urllib.error.URLError, ValueError) as e:
                sys.stderr.write(f"cannot fetch {name}/{rel} at {args.ref}: {e}\n")
                return 2
            stale += code

    if args.check:
        if stale:
            sys.stderr.write(f"\n{stale} of {seen} vendored files are stale — "
                             "run:\n    python3 tools/sync_caveman_skill.py\n")
            return 1
        print(f"up to date: {seen} vendored files at {args.ref}")
        return 0
    print(f"checked {seen} files at {args.ref}")
    if args.ref != caveman.REF:
        print(f"note: caveman.REF still says {caveman.REF} — update it to "
              f"{args.ref} so the install stamp matches", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
