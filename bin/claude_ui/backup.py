"""Backup and restore a selected slice of the machine config as a zip.

The job this does is "I am about to reinstall Claude Code": take the parts of
the config that took effort to build — skills, MCP servers, statusline, and the
transcripts your cost history is computed from — write them somewhere an
uninstall will not reach, and put them back afterwards without silently
clobbering anything that has since changed.

Three deliberate choices:

- **Archives live outside the config dir.** ``~/.claude/backups`` is Claude
  Code's own (``.claude.json.backup.<epoch>``), is pruned by
  ``cleanupPeriodDays``, and is inside the directory an uninstall removes.
  ``~/.cache`` is where derived state goes and gets purged. So the default is
  ``$XDG_DATA_HOME/claude-ui/backups``, overridable in .claude-ui.json.

- **Secrets go in verbatim.** MCP server configs routinely carry API keys, and
  a redacted copy would not restore. The manifest records `contains_secrets`
  and the UI warns; the archive is a file you own, on your own disk.

- **Restore is opt-in per file.** inspect() answers new / same / differs with a
  diff before anything is written, and restore() only touches the paths it is
  given. Nothing here ever deletes.

Within a group, the pickable thing is a **unit**: one skill, one config file,
one MCP server, one project's transcripts. Units are what a person would name,
not what the filesystem happens to hold — a skill is one tick, not the eleven
files inside it — and every entry carries the unit it belongs to, so both the
pick list and the filter fall out of the same walk.
"""

from pathlib import Path
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import zipfile

from .core import (CLAUDE_JSON, CONFIG_FILES, ITEM_TYPES, MCP_FILE, NAME_RE,
                   PROJECT_ITEM_TYPES, _read_json_object, _within, atomic_write,
                   atomic_write_bytes, config_dir, disabled_dir,
                   is_reserved_skill_dir, project_claude_dir, project_items,
                   read_cfg, tilde, write_cfg)
from .insight import MAX_TRANSCRIPT, projects_dir
from .items import (resolve_archived, resolve_item, scan_archived_skills,
                    scan_items)
from .mcp import mcp_machine_set, mcp_state
from .plugins import plugins_root
from .statusline import statusline_paths


# 2: MCP servers became one archive member each (see _g_mcp). Format 1 archives
# still read — their single whole-map member is handled everywhere alongside.
FORMAT = 2

# Skip anything bigger than this. Matches insight.MAX_TRANSCRIPT, the cap the
# cost scanner already applies, so the two agree on what a transcript is.
MAX_FILE = MAX_TRANSCRIPT

# Diffs are for reading, not for archiving: past these the inspect report says
# only whether the bytes differ.
DIFF_MAX_BYTES = 256 * 1024
DIFF_MAX_LINES = 400

ARCHIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.zip$")

# Where synthesized (not copied) members land in the archive. MCP_MEMBER is the
# format-1 shape — every server in one blob — and is still read, never written.
MCP_MEMBER = "mcp/mcpServers.json"
MCP_PREFIX = "mcp/servers/"
FILES_PREFIX = "files/"

# What a single item type is called in the pick list, one at a time.
ITEM_LABEL = {"skills": "skill", "commands": "command", "agents": "agent",
              "output-styles": "output style"}


# --------------------------------------------------------------- destination

def default_backup_dir():
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "claude-ui" / "backups"

def backup_dir():
    """Where archives are written. Mirrors core.config_dir(): an explicit
    setting in .claude-ui.json wins, otherwise the platform default."""
    p = read_cfg().get("backup_dir")
    return Path(p).expanduser() if p else default_backup_dir()

def set_backup_dir(path):
    cfg = read_cfg()
    if not path:
        cfg.pop("backup_dir", None)
    else:
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise ValueError("backup dir must be an absolute path (or start with ~)")
        if _within(p.resolve(strict=False), config_dir().resolve(strict=False)):
            # The whole point is to survive the config dir being deleted.
            raise ValueError("backup dir must be outside the config dir")
        cfg["backup_dir"] = str(p)
    write_cfg(cfg)


# ------------------------------------------------------------------- walking

def _walk(root, cap=MAX_FILE):
    """(relative-to-root Path, stat) for every file under `root`.

    Follows symlinks — a skill symlinked into the config dir from a git
    checkout is still yours, and a backup wants its bytes, not a dangling link.
    Directory loops are cut by remembering resolved directories. Dotfiles are
    skipped, the same rule items.py scans by.
    """
    out, seen = [], set()

    def rec(d, rel):
        try:
            real = d.resolve()
        except OSError:
            return
        if real in seen:
            return
        seen.add(real)
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for p in entries:
            if p.name.startswith("."):
                continue
            child = rel + "/" + p.name if rel else p.name
            if p.is_dir():
                rec(p, child)
            elif p.is_file():
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append((child, p, st))

    if root.is_dir():
        rec(root, "")
    return [(rel, p, st) for rel, p, st in out if st.st_size <= cap]


def _entry(path, group="", src=None, data=None, mode=None, size=None, **unit):
    """One archive member. `unit` is the pickable thing it belongs to: without
    one it falls back to the group, which is the old all-or-nothing behaviour."""
    return {"path": path, "group": group, "src": src, "data": data,
            "mode": mode, "size": size,
            "unit": unit.get("unit") or group,
            "unit_label": unit.get("unit_label") or "",
            "unit_desc": unit.get("unit_desc") or ""}

def _file_entries(group, paths, unit_of=None):
    """Config-dir files -> archive members under files/.

    `unit_of` maps a source path to its unit fields; without it every file in
    the group shares one unit.
    """
    out = []
    cfg = config_dir()
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_file() or st.st_size > MAX_FILE:
            continue
        try:
            rel = p.relative_to(cfg)
        except ValueError:
            continue
        out.append(_entry(FILES_PREFIX + rel.as_posix(), group=group, src=p,
                          mode=st.st_mode & 0o777, size=st.st_size,
                          **(unit_of(p) if unit_of else {})))
    return out

def _walk_entries(group, root, cap=MAX_FILE, unit_of=None):
    try:
        base = root.relative_to(config_dir()).as_posix()
    except ValueError:
        return []
    return [_entry(FILES_PREFIX + (base + "/" + rel if base else rel),
                   group=group, src=p, mode=st.st_mode & 0o777, size=st.st_size,
                   **(unit_of(p) if unit_of else {}))
            for rel, p, st in _walk(root, cap)]


# -------------------------------------------------------------------- groups

def _g_items():
    """One unit per item, so an archive can hold two skills instead of all of
    them. scan_items() already knows every item's name and which side of
    disabled/ it is parked on — the same list the inventory renders from.

    Then the type directories are walked anyway, and anything no item claimed
    goes into a catch-all unit. An item scan is a view of those directories,
    not an inventory of them: a note beside a command, a helper script the
    scan does not model as an item, are still files a backup must not drop.
    """
    out, claimed = [], set()
    for t in ITEM_TYPES:
        for it in scan_items(t):
            try:
                p = resolve_item(t, it["name"], it["enabled"])
            except ValueError:
                continue
            u = {"unit": t + "/" + it["name"], "unit_label": it["name"],
                 "unit_desc": ITEM_LABEL.get(t, t)
                              + ("" if it["enabled"] else " · disabled")}
            if ITEM_TYPES[t]["kind"] == "dir":
                out += _walk_entries("items", p, unit_of=lambda _p, u=u: u)
            else:
                out += _file_entries("items", [p], unit_of=lambda _p, u=u: u)
    # Archived skills are items too, and would otherwise land in the catch-all
    # below — a unit called "Other files" is not somewhere you can find one
    # skill to restore. scan_items() cannot see them by design (the archive is
    # excluded at items._scan_dir_type), so they are asked for separately.
    for a in scan_archived_skills():
        try:
            p = resolve_archived(a["name"])
        except ValueError:
            continue
        u = {"unit": "skills/archived/" + a["name"], "unit_label": a["name"],
             "unit_desc": ITEM_LABEL.get("skills", "skills") + " · archived"}
        out += _walk_entries("items", p, unit_of=lambda _p, u=u: u)
    claimed = {e["path"] for e in out}

    rest = {"unit": "other", "unit_label": "Other files",
            "unit_desc": "files in the item directories that are not items"}
    for t in ITEM_TYPES:
        for root in (config_dir() / t, disabled_dir() / t):
            out += [e for e in _walk_entries("items", root,
                                             unit_of=lambda _p: rest)
                    if e["path"] not in claimed]
    return out

def _g_config():
    return _file_entries("config", [config_dir() / n for n in CONFIG_FILES],
                         unit_of=lambda p: {"unit": p.name, "unit_label": p.name})

def _g_statusline():
    return _file_entries("statusline", list(statusline_paths()),
                         unit_of=lambda p: {"unit": p.name, "unit_label": p.name})

def _g_mcp():
    """One member per server, synthesized from the mcpServers map in
    ~/.claude.json — never that whole file, which also holds your project
    history and OAuth account.

    A member each rather than one blob so a single server can be archived, and
    so restore can offer it as its own row: the map form (format 1) was
    all-or-nothing on the way back in too.
    """
    out = []
    data, err = _read_json_object(CLAUDE_JSON)
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        for name, cfg in sorted(servers.items()):
            # a name we cannot make a member path from is one mcp_machine_set
            # would refuse to write back anyway
            if not NAME_RE.match(name or ""):
                continue
            blob = _mcp_blob({name: cfg})
            out.append(_entry(MCP_PREFIX + name + ".json", group="mcp",
                              data=blob, size=len(blob), unit=name,
                              unit_label=name, unit_desc=_mcp_desc(cfg)))
    out += _file_entries("mcp", [disabled_dir() / MCP_FILE],
                         unit_of=lambda p: {"unit": "disabled",
                                            "unit_label": "disabled servers",
                                            "unit_desc": tilde(p)})
    return out

def _mcp_blob(servers):
    return (json.dumps({"mcpServers": servers}, indent=2) + "\n").encode()

def _mcp_desc(cfg):
    if not isinstance(cfg, dict):
        return ""
    return str(cfg.get("command") or cfg.get("url") or "")[:120]

def _g_plugins():
    """Which plugins you had and where they came from — not their trees.

    An installed plugin is a tarball extract that reinstalls from its
    marketplace; copying megabytes of someone else's code into every archive
    buys nothing a `claude plugin install` doesn't. What is not recoverable is
    the list, so that is what goes in. (`enabledPlugins` lives in settings.json
    and rides along with the config group.)
    """
    root = plugins_root()
    out = _file_entries("plugins", [root / "config.json",
                                    root / "known_marketplaces.json"],
                        unit_of=lambda p: {
                            "unit": "list", "unit_label": "Plugin list",
                            "unit_desc": "which plugins are installed and enabled"})
    mdir = root / "marketplaces"
    if mdir.is_dir():
        try:
            dirs = sorted(d for d in mdir.iterdir() if d.is_dir())
        except OSError:
            dirs = []
        for d in dirs:
            out += _file_entries(
                "plugins", [d / ".claude-plugin" / "marketplace.json"],
                unit_of=lambda _p, n=d.name: {
                    "unit": "marketplace:" + n, "unit_label": n,
                    "unit_desc": "marketplace metadata"})
    return out

def _g_transcripts():
    """One unit per project directory. The names are Claude Code's own encoding
    of the working directory, shown as they are on disk rather than decoded —
    the encoding is lossy for paths containing dashes, and a wrong guess about
    which project you are ticking is worse than an ugly one."""
    root = projects_dir()

    def unit_of(p):
        try:
            rel = p.relative_to(root)
        except ValueError:
            return {}
        if len(rel.parts) < 2:
            return {"unit": "transcripts", "unit_label": "loose transcripts"}
        return {"unit": rel.parts[0], "unit_label": rel.parts[0]}

    return _walk_entries("transcripts", root, MAX_TRANSCRIPT, unit_of=unit_of)


GROUPS = [
    {"id": "items", "label": "Skills, commands, agents, output styles",
     "note": "enabled and disabled, including every file inside a skill",
     "collect": _g_items},
    {"id": "config", "label": "CLAUDE.md, settings.json, keybindings.json",
     "note": "your memory file, all settings, and key bindings",
     "collect": _g_config},
    {"id": "statusline", "label": "Statusline",
     "note": "statusline.json and the generated statusline.sh",
     "collect": _g_statusline},
    {"id": "mcp", "label": "MCP servers", "secrets": True,
     "note": "server configs from ~/.claude.json — these usually contain API keys",
     "collect": _g_mcp},
    {"id": "plugins", "label": "Plugin list and marketplaces",
     "note": "which plugins and marketplaces you had, not their installed files",
     "collect": _g_plugins},
    {"id": "transcripts", "label": "Transcripts (cost history)",
     "note": "projects/**.jsonl — restoring these is what makes costs accurate again",
     "collect": _g_transcripts},
]

GROUP_IDS = [g["id"] for g in GROUPS]


def _collect(picks, units=None):
    """Entries for the ticked groups, narrowed to the ticked units.

    `units` is {group_id: [unit_id, ...]}. A group missing from it takes
    everything, so a caller that only knows about groups keeps working.
    """
    picks = [p for p in GROUP_IDS if p in set(picks or [])]
    if not picks:
        raise ValueError("nothing selected")
    units = units if isinstance(units, dict) else {}
    entries, seen = [], set()
    for g in GROUPS:
        if g["id"] not in picks:
            continue
        want = units.get(g["id"])
        want = set(want) if isinstance(want, (list, set, tuple)) else None
        for e in g["collect"]():
            if want is not None and e["unit"] not in want:
                continue
            if e["path"] in seen:   # a file two groups both claim
                continue
            seen.add(e["path"])
            entries.append(e)
    return entries

def _units_of(entries):
    """Roll entries up into the rows the pick list ticks. A unit that turned
    out to hold nothing is left out: it is a row you could tick to no effect."""
    out = {}
    for e in entries:
        u = out.setdefault(e["unit"], {
            "id": e["unit"], "label": e["unit_label"] or e["unit"],
            "desc": e["unit_desc"], "files": 0, "bytes": 0})
        u["files"] += 1
        u["bytes"] += e["size"] or 0
    return sorted(out.values(), key=lambda u: u["label"].lower())

def backup_plan():
    """What each group would put in an archive, and the units inside it."""
    out = []
    for g in GROUPS:
        try:
            entries = g["collect"]()
        except OSError as e:
            out.append({"id": g["id"], "label": g["label"], "files": 0,
                        "bytes": 0, "note": g["note"], "secrets": bool(g.get("secrets")),
                        "units": [], "error": str(e)})
            continue
        out.append({"id": g["id"], "label": g["label"],
                    "files": len(entries),
                    "bytes": sum(e["size"] or 0 for e in entries),
                    "units": _units_of(entries),
                    "note": g["note"], "secrets": bool(g.get("secrets"))})
    skipped = _oversized_transcripts()
    for row in out:
        if row["id"] == "transcripts" and skipped:
            row["skipped"] = skipped
            row["note"] += f" ({skipped} over {MAX_TRANSCRIPT // (1024 * 1024)} MB skipped)"
    return out

def _oversized_transcripts():
    n = 0
    pdir = projects_dir()
    if pdir.is_dir():
        for p in pdir.rglob("*.jsonl"):
            try:
                if p.stat().st_size > MAX_TRANSCRIPT:
                    n += 1
            except OSError:
                pass
    return n


# -------------------------------------------------------------------- create

def _sha(data):
    return hashlib.sha256(data).hexdigest()

def _read(entry):
    if entry["data"] is not None:
        return entry["data"]
    return entry["src"].read_bytes()

def backup_create(picks, note="", units=None):
    entries = _collect(picks, units)
    dest = backup_dir()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"claude-config-{stamp}.zip"
    n = 2
    while (dest / name).exists():
        name = f"claude-config-{stamp}-{n}.zip"
        n += 1
    path = dest / name
    tmp = dest / ("." + name + ".claude-ui-tmp")
    manifest = {
        "format": FORMAT,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "config_dir": str(config_dir()),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "note": str(note or "")[:500],
        # what the archive actually holds, not what was ticked: a group that
        # found nothing must not show up as a badge on a row that lacks it
        "groups": [g["id"] for g in GROUPS
                   if any(e["group"] == g["id"] for e in entries)],
        "contains_secrets": any(
            g.get("secrets") for g in GROUPS
            if any(e["group"] == g["id"] for e in entries)),
        "entries": [],
    }
    written = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for e in entries:
                try:
                    data = _read(e)
                except OSError:
                    continue    # vanished mid-run; the manifest just won't list it
                manifest["entries"].append({
                    "path": e["path"], "group": e["group"], "size": len(data),
                    "mode": e["mode"], "sha256": _sha(data),
                    # the unit rides along so restore can offer "the pdf skill"
                    # as one tick, the same row the create pick list showed
                    "unit": e["unit"], "unit_label": e["unit_label"],
                    "unit_desc": e["unit_desc"]})
                z.writestr(e["path"], data)
                written += len(data)
            z.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"name": name, "path": tilde(path), "dir": tilde(dest),
            "files": len(manifest["entries"]), "bytes": written,
            "zip_bytes": path.stat().st_size,
            "contains_secrets": manifest["contains_secrets"]}


# ---------------------------------------------------------------------- read

def archive_path(name):
    """Resolve an archive name inside the backup dir, or refuse."""
    if not isinstance(name, str) or not ARCHIVE_RE.match(name):
        raise ValueError("bad archive name")
    dest = backup_dir()
    p = dest / name
    if p.parent.resolve(strict=False) != dest.resolve(strict=False):
        raise ValueError("bad archive name")
    if not p.is_file():
        raise ValueError(f"{name}: not found")
    return p

def _manifest_of(z):
    try:
        data = json.loads(z.read("manifest.json"))
    except (KeyError, json.JSONDecodeError, OSError) as e:
        raise ValueError(f"not a claude-ui backup ({e})") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("not a claude-ui backup (bad manifest)")
    if data.get("format", 0) > FORMAT:
        raise ValueError(f"made by a newer claude-ui (format {data.get('format')})")
    return data

def backup_list():
    dest = backup_dir()
    out = []
    if dest.is_dir():
        for p in sorted(dest.glob("*.zip"), reverse=True):
            try:
                size = p.stat().st_size
            except OSError:
                continue
            row = {"name": p.name, "path": tilde(p), "zip_bytes": size}
            try:
                with zipfile.ZipFile(p) as z:
                    m = _manifest_of(z)
                row.update({
                    "created_at": m.get("created_at", ""),
                    "note": m.get("note", ""),
                    "groups": m.get("groups", []),
                    "config_dir": m.get("config_dir", ""),
                    "host": m.get("host", ""),
                    "files": len(m["entries"]),
                    "bytes": sum(int(e.get("size") or 0) for e in m["entries"]),
                    "contains_secrets": bool(m.get("contains_secrets")),
                })
            except (ValueError, OSError, zipfile.BadZipFile) as e:
                row["error"] = str(e)
            out.append(row)
    return {"dir": tilde(dest), "exists": dest.is_dir(),
            "default_dir": "backup_dir" not in read_cfg(),
            "archives": out}


# ------------------------------------------------------------------- restore

def _member_parts(member):
    """The shape half of the two checks below: a members's path segments, or a
    refusal. Nothing here looks at the filesystem — this only decides whether
    the string is the sort of thing a restore may consider at all."""
    if not isinstance(member, str) or _is_mcp(member):
        raise ValueError("bad member")
    if not member.startswith(FILES_PREFIX):
        raise ValueError(f"{member}: not a restorable path")
    rel = member[len(FILES_PREFIX):]
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p == ".." for p in parts) or rel.startswith("/"):
        raise ValueError(f"{member}: unsafe path")
    return parts

def _target(member):
    """Archive member -> the absolute file it restores to.

    Every path is checked twice: for shape (no absolute, no ``..``, a known
    prefix) and for where it lands after resolution, which is what actually
    stops a crafted archive escaping. resolve_editable() is not used here: its
    plugins carve-out is an editor rule, and this needs plugins/config.json to
    be writable while still refusing anything outside the config dir.
    """
    parts = _member_parts(member)
    cfg = config_dir()
    p = cfg.joinpath(*parts)
    # strict=False: the file usually does not exist yet on a fresh machine
    if not _within(p.resolve(strict=False), cfg.resolve(strict=False)):
        raise ValueError(f"{member}: escapes the config dir")
    return p

def _is_text(data):
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode()
    except UnicodeDecodeError:
        return False
    return True

def _diff(old, new, path):
    lines = list(difflib.unified_diff(
        old.decode(errors="replace").splitlines(),
        new.decode(errors="replace").splitlines(),
        fromfile=f"{path} (on disk)", tofile=f"{path} (in backup)", lineterm="", n=2))
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"… {len(lines) - DIFF_MAX_LINES} more lines"]
    return "\n".join(lines)

def _is_mcp(member):
    return member == MCP_MEMBER or member.startswith(MCP_PREFIX)

def _mcp_name(member):
    """The server a per-server member holds, validated on the way out of the
    archive exactly as it was on the way in — a crafted zip does not get to
    name the key it merges into ~/.claude.json."""
    name = member[len(MCP_PREFIX):]
    if not name.endswith(".json") or not NAME_RE.match(name[:-5]):
        raise ValueError(f"{member}: bad server name")
    return name[:-5]

def _live_mcp_bytes(only=None):
    """The live servers in the shape the archive holds them, for comparison.
    `only` narrows to one server, so a per-server member is judged against its
    own counterpart rather than the whole map."""
    data, _ = _read_json_object(CLAUDE_JSON)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    if only is not None:
        servers = {k: v for k, v in servers.items() if k == only}
    return _mcp_blob(servers) if servers else None

def _verdict(z, e, row, live):
    """Fill in new / same / differs (+ a diff) for one entry against the bytes
    already on disk. Shared so a restore into a project judges a file by
    exactly the rule a restore into the config dir does."""
    if live is None:
        row["status"] = "new"
    elif _sha(live) == e.get("sha256"):
        row["status"] = "same"
    else:
        row["status"] = "differs"
        new = z.read(row["path"])
        if (len(live) <= DIFF_MAX_BYTES and len(new) <= DIFF_MAX_BYTES
                and _is_text(live) and _is_text(new)):
            row["diff"] = _diff(live, new, row["target"])
    return row

def backup_inspect(name):
    """The dry run: what restoring this archive would do to each file."""
    p = archive_path(name)
    rows = []
    with zipfile.ZipFile(p) as z:
        m = _manifest_of(z)
        names = set(z.namelist())
        for e in m["entries"]:
            member = e.get("path") or ""
            row = {"path": member, "group": e.get("group", ""),
                   "size": int(e.get("size") or 0),
                   # empty on archives from before units were recorded — the
                   # UI then falls back to one row per file
                   "unit": e.get("unit") or "",
                   "unit_label": e.get("unit_label") or "",
                   "unit_desc": e.get("unit_desc") or ""}
            if member not in names:
                rows.append({**row, "status": "missing",
                             "error": "listed in the manifest but not in the zip"})
                continue
            if _is_mcp(member):
                if member == MCP_MEMBER:        # format 1: every server at once
                    row["target"] = tilde(CLAUDE_JSON) + " → mcpServers"
                    live = _live_mcp_bytes()
                else:
                    try:
                        sname = _mcp_name(member)
                    except ValueError as err:
                        rows.append({**row, "status": "refused", "error": str(err)})
                        continue
                    row["target"] = tilde(CLAUDE_JSON) + " → mcpServers." + sname
                    live = _live_mcp_bytes(sname)
            else:
                try:
                    target = _target(member)
                except ValueError as err:
                    rows.append({**row, "status": "refused", "error": str(err)})
                    continue
                row["target"] = tilde(target)
                live = target.read_bytes() if target.is_file() else None
            _verdict(z, e, row, live)
            rows.append(row)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"name": name, "manifest": {k: v for k, v in m.items() if k != "entries"},
            "entries": rows, "counts": counts,
            # both forms: tilde() is for reading, and the manifest records an
            # absolute path, so comparing the two needs the absolute one
            "config_dir": tilde(config_dir()),
            "config_dir_abs": str(config_dir())}

def _restore_mcp(blob):
    """Merge the archived servers into ~/.claude.json one at a time, so the
    rest of that file — project history, the account you are logged in as —
    is never rewritten from a backup."""
    data = json.loads(blob.decode())
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError("mcpServers is not an object")
    for sname, cfg in servers.items():
        mcp_machine_set(sname, cfg, enabled=True)
    return len(servers)

def _restore_members(z, want, known, write):
    """The write loop both restores share: only members the manifest names,
    one `write` call each, and a failure is collected rather than raised — one
    unwritable file must not abandon the rest of the selection."""
    written, failed = [], []
    for member in want:
        if member not in known:
            failed.append({"path": member, "error": "not in this archive"})
            continue
        try:
            written.append(write(member, z.read(member)))
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
            failed.append({"path": member, "error": str(e)})
    return {"written": written, "failed": failed,
            "count": len(written), "failed_count": len(failed)}

def _wanted(paths):
    want = [x for x in (paths or []) if isinstance(x, str)]
    if not want:
        raise ValueError("nothing selected")
    return want

def backup_restore(name, paths):
    """Write back exactly the members in `paths`. Never deletes anything."""
    p = archive_path(name)
    want = _wanted(paths)
    with zipfile.ZipFile(p) as z:
        m = _manifest_of(z)
        modes = {e.get("path"): e.get("mode") for e in m["entries"]}

        def write(member, data):
            if _is_mcp(member):
                if member != MCP_MEMBER:
                    _mcp_name(member)       # refuse a member we could not write
                n = _restore_mcp(data)
                return {"path": member, "target": tilde(CLAUDE_JSON),
                        "detail": f"{n} server(s) merged"}
            target = _target(member)
            atomic_write_bytes(target, data, modes.get(member))
            return {"path": member, "target": tilde(target)}

        return _restore_members(z, want, set(modes), write)

def backup_delete(name):
    archive_path(name).unlink()
    return {"deleted": name}


# --------------------------------------------- restore into one project only

# Claude Code reads a skill from three places, and they are different scopes:
# ~/.claude/skills/<name>/ applies to all your projects, <project>/.claude/
# skills/<name>/ to that project only. Commands and agents follow the same
# split. An archive holds the personal copies; this puts one back as the
# narrower thing instead — the reason to restore into a project at all.

def _project_member(member):
    """Archive member -> (unit id, path segments under <project>/.claude/).

    Two rules live here. Only the three item types a project directory can
    hold are restorable — a settings.json or a transcript has no project form.
    And a leading ``disabled/`` is dropped: that is this app's own parking
    area inside the config dir, and a project has no such place, so a skill
    archived while disabled comes back as an ordinary project skill.

    The unit is derived from the path, never read from the manifest's unit
    field. A hand-built archive gets to say whatever it likes about its
    entries; what it cannot do is make ``files/settings.json`` end in a
    skills directory by labelling it one.
    """
    parts = _member_parts(member)
    if parts[0] == "disabled":
        parts = parts[1:]
    if len(parts) < 2 or parts[0] not in PROJECT_ITEM_TYPES:
        raise ValueError(f"{member}: not a project skill, command or agent")
    # The archive is a user-scope idea, and unlike disabled/ it cannot simply
    # be dropped: a project has no archive area, so restoring skills/archived/
    # <name>/ into one would either resurrect a skill you archived or create a
    # directory Claude Code scans for a project that never asked for it.
    if parts[0] == "skills" and is_reserved_skill_dir(parts[1]):
        raise ValueError(f"{member}: archived skills have no project form — "
                         "restore it to your config dir and move it there")
    type_, rest = parts[0], parts[1:]
    if ITEM_TYPES[type_]["kind"] == "dir":
        # a skill is a directory of files: the item is the directory, and
        # everything below it rides along under the same unit
        if len(rest) < 2 or not NAME_RE.match(rest[0]):
            raise ValueError(f"{member}: not a file inside a skill")
        name = rest[0]
    else:
        # a command or agent is one file, and may be nested: commands/git/pr.md
        # is the item git/pr, exactly as items.item_rel() reads it
        if not rest[-1].endswith(".md"):
            raise ValueError(f"{member}: not a {type_[:-1]} file")
        rel = rest[:-1] + [rest[-1][:-3]]
        if not all(NAME_RE.match(s) for s in rel):
            raise ValueError(f"{member}: bad item name")
        name = "/".join(rel)
    return type_ + "/" + name, parts

def _project_target(member, cdir):
    """Where `member` lands inside a project's .claude/, checked the same two
    ways _target() checks its own: for shape, then for where it actually
    resolves to. The caller must have obtained `cdir` from
    core.project_claude_dir(), which is what rules out a symlinked .claude —
    the containment test below cannot see that on its own."""
    unit, parts = _project_member(member)
    p = cdir.joinpath(*parts)
    if not _within(p.resolve(strict=False), cdir.resolve(strict=False)):
        raise ValueError(f"{member}: escapes {tilde(cdir)}")
    return unit, p

def _blocked_by_file(target, cdir):
    """The one failure worth catching before the write rather than during it:
    a plain file sitting where a directory has to go. atomic_write_bytes would
    raise FileExistsError from mkdir; saying so in the dry run turns a
    confusing per-file failure into a verdict you can see coming."""
    for parent in target.parents:
        if not _within(parent, cdir):   # stops at .claude itself, which counts
            break
        if parent.exists() and not parent.is_dir():
            return f"{tilde(parent)} is a file, not a directory"
    return ""

def project_restore_inspect(root, name):
    """The dry run for restoring into one project: every skill, command and
    agent the archive holds, judged against what is in <root>/.claude/ now.

    Row shape matches backup_inspect's, so the same UI renders both.
    """
    cdir = project_claude_dir(root)          # registry + symlink gate, first
    p = archive_path(name)
    rows = []
    with zipfile.ZipFile(p) as z:
        m = _manifest_of(z)
        names = set(z.namelist())
        for e in m["entries"]:
            member = e.get("path") or ""
            try:
                unit, _ = _project_member(member)
            except ValueError:
                continue    # not an error to show: most of an archive is not this
            row = {"path": member, "group": e.get("group", ""),
                   "size": int(e.get("size") or 0), "unit": unit,
                   # the manifest's labels are for reading only; the unit above
                   # is the one anything is decided by
                   "unit_label": e.get("unit_label") or unit.split("/", 1)[1],
                   "unit_desc": e.get("unit_desc") or unit.split("/", 1)[0]}
            if member not in names:
                rows.append({**row, "status": "missing",
                             "error": "listed in the manifest but not in the zip"})
                continue
            try:
                _, target = _project_target(member, cdir)
            except ValueError as err:
                rows.append({**row, "status": "refused", "error": str(err)})
                continue
            row["target"] = tilde(target)
            blocked = _blocked_by_file(target, cdir)
            if blocked:
                rows.append({**row, "status": "refused", "error": blocked})
                continue
            live = target.read_bytes() if target.is_file() else None
            _verdict(z, e, row, live)
            rows.append(row)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"name": name, "root": str(cdir.parent), "tilde": tilde(cdir.parent),
            "claude_dir": tilde(cdir), "entries": rows, "counts": counts,
            "manifest": {k: v for k, v in m.items() if k != "entries"},
            # which item names are already here, so the panel can say that a
            # tick lands on top of something rather than beside it
            "present": {t: project_items(cdir, t) for t in PROJECT_ITEM_TYPES}}

def project_restore(root, name, paths):
    """Write the listed members into <root>/.claude/. Never deletes: an item
    already there is merged into, so a file the archive does not carry stays."""
    cdir = project_claude_dir(root)
    p = archive_path(name)
    want = _wanted(paths)
    with zipfile.ZipFile(p) as z:
        m = _manifest_of(z)
        modes = {e.get("path"): e.get("mode") for e in m["entries"]}

        def write(member, data):
            _, target = _project_target(member, cdir)
            atomic_write_bytes(target, data, modes.get(member))
            return {"path": member, "target": tilde(target)}

        return _restore_members(z, want, set(modes), write)


# --------------------------------------------------------------- fresh start

def _reset_targets(keep_transcripts):
    """(dirs, files) the reset deletes — the same things the groups archive.

    Deliberately a list of names, not a directory wipe: the config dir also
    holds things this app does not model — .credentials.json, todos/,
    shell-snapshots/ — and a reset that logs you out or eats Claude Code's own
    state is a bug, not a feature.
    """
    cfg = config_dir()
    dirs = [cfg / t for t in ITEM_TYPES] + [disabled_dir(), plugins_root()]
    files = [cfg / n for n in CONFIG_FILES] + list(statusline_paths())
    if not keep_transcripts:
        dirs.append(projects_dir())
    return dirs, files

def reset_config(keep_transcripts=True):
    """Delete the modelled slice of the config dir and the mcpServers key.

    Never creates a backup — the caller does that first (see fresh_start).
    Collects per-path failures rather than stopping at the first one: a
    half-reset with a precise list of what refused beats an exception that
    leaves the user guessing how far it got.
    """
    cfg = config_dir()
    rcfg = cfg.resolve(strict=False)
    if rcfg == Path(rcfg.anchor) or rcfg == Path.home().resolve():
        raise ValueError(f"refusing to reset {tilde(cfg)} — not a config dir")
    dirs, files = _reset_targets(keep_transcripts)
    deleted, failed = [], []

    def fail(path, err):
        failed.append({"path": tilde(Path(path)), "error": str(err)})

    for d in dirs:
        if not (d.is_symlink() or d.exists()):
            continue
        if d.is_symlink() or d.is_file():
            # a symlinked item dir is a pointer into someone's checkout:
            # remove the pointer, never the target — which is also why this
            # runs before the containment check, which judges the target
            try:
                d.unlink()
                deleted.append(tilde(d))
            except OSError as e:
                fail(d, e)
            continue
        if not _within(d.resolve(strict=False), rcfg):
            fail(d, "outside the config dir")
            continue
        errs = []
        shutil.rmtree(d, onerror=lambda _f, p, ei: errs.append((p, ei[1])))
        if errs:
            for p, e in errs:
                fail(p, e)
        else:
            deleted.append(tilde(d))
    for f in files:
        if not (f.is_symlink() or f.exists()):
            continue
        if not f.is_symlink() and not _within(f.resolve(strict=False), rcfg):
            fail(f, "outside the config dir")
            continue
        try:
            f.unlink()
            deleted.append(tilde(f))
        except OSError as e:
            fail(f, e)

    # ~/.claude.json: pop mcpServers and nothing else — the rest of that file
    # is your login and project history, which a reset must survive
    mcp_cleared = 0
    data, err = _read_json_object(CLAUDE_JSON)
    if err:
        fail(CLAUDE_JSON, f"mcpServers not cleared: {err}")
    elif "mcpServers" in data:
        servers = data.pop("mcpServers")
        try:
            atomic_write(CLAUDE_JSON, json.dumps(data, indent=2) + "\n")
            mcp_cleared = len(servers) if isinstance(servers, dict) else 0
            deleted.append(tilde(CLAUDE_JSON) + " → mcpServers")
        except OSError as e:
            fail(CLAUDE_JSON, e)
    return {"deleted": deleted, "failed": failed, "mcp_cleared": mcp_cleared}

def fresh_start(keep_transcripts=True):
    """Snapshot everything, then reset — in that order, and only that order.

    The snapshot is the whole safety story: if it cannot be written, nothing
    is touched. Transcripts go into it only when they are about to be deleted;
    when they stay on disk, archiving them too would double the disk they use
    for no recovery value.
    """
    picks = (GROUP_IDS if not keep_transcripts
             else [g for g in GROUP_IDS if g != "transcripts"])
    snap = backup_create(
        picks, note="Fresh Start snapshot — taken automatically before reset")
    result = reset_config(keep_transcripts=keep_transcripts)
    return {"snapshot": snap["name"], "snapshot_path": snap["path"], **result}
