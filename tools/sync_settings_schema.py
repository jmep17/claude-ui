#!/usr/bin/env python3
"""Regenerate bin/claude_ui/data/settings_schema.json from the official schema.

    python3 tools/sync_settings_schema.py            # fetch and write
    python3 tools/sync_settings_schema.py --check    # diff only, exit 1 if stale

The output is the vendored floor the app falls back to offline. Review the diff:
a reworded description is upstream telling you a setting changed meaning, and
that is exactly what this file exists to surface.

Imports the fetch/flatten/validate logic from claude_ui.schema — the app must
never depend on tools/, so the direction is one-way.
"""

import argparse
import datetime
import difflib
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))

from claude_ui import schema  # noqa: E402


def fetch(url):
    """(document, resolved url) — follows the schemastore 301."""
    req = urllib.request.Request(url, headers={"user-agent": "claude-ui"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode(errors="replace")), r.geturl()


def build(url):
    doc, resolved = fetch(url)
    return schema.build(
        doc, source=url, resolved=resolved,
        fetched=datetime.date.today().isoformat())


def check(path, snap):
    """0 when the file already matches, 1 otherwise (diff printed to stderr)."""
    new = schema.serialize(snap)
    old = path.read_text() if path.is_file() else ""
    if old == new:
        print(f"up to date: {path.relative_to(REPO)} "
              f"({len(snap['keys'])} keys)")
        return 0
    # the snapshot's own fetch date always differs; don't call that a change
    diff = [ln for ln in difflib.unified_diff(
        old.splitlines(True), new.splitlines(True),
        fromfile="vendored", tofile="live", n=1)
        if not ln.startswith(("+  \"fetched\"", "-  \"fetched\""))]
    if not any(ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
               for ln in diff):
        print(f"up to date: {path.relative_to(REPO)} (fetch date aside)")
        return 0
    sys.stderr.writelines(diff)
    sys.stderr.write(
        f"\n{path.relative_to(REPO)} is stale — run:\n"
        f"    python3 tools/sync_settings_schema.py\n")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="report drift without writing; exit 1 if stale")
    ap.add_argument("--url", default=schema.SCHEMA_URL)
    ap.add_argument("--out", type=Path, default=schema.OFFICIAL_PATH)
    args = ap.parse_args(argv)

    try:
        snap = build(args.url)
    except (OSError, ValueError) as e:
        sys.stderr.write(f"cannot build snapshot: {e}\n")
        return 2

    if args.check:
        return check(args.out, snap)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(schema.serialize(snap))
    managed = sum(1 for v in snap["keys"].values() if v["managed"])
    docs = sum(1 for v in snap["keys"].values() if v.get("doc"))
    print(f"wrote {args.out.relative_to(REPO)}: {len(snap['keys'])} keys "
          f"({managed} managed, {docs} with a docs URL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
