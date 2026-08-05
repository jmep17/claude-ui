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
"""

from pathlib import Path
import datetime
import difflib
import hashlib
import json
import os
import re
import zipfile

from .core import (CLAUDE_JSON, CONFIG_FILES, ITEM_TYPES, _read_json_object,
                   _within, atomic_write_bytes, config_dir, disabled_dir,
                   read_cfg, tilde, write_cfg)
from .insight import MAX_TRANSCRIPT, projects_dir
from .mcp import mcp_machine_set, mcp_state
from .plugins import plugins_root
from .statusline import statusline_paths


FORMAT = 1

# Skip anything bigger than this. Matches insight.MAX_TRANSCRIPT, the cap the
# cost scanner already applies, so the two agree on what a transcript is.
MAX_FILE = MAX_TRANSCRIPT

# Diffs are for reading, not for archiving: past these the inspect report says
# only whether the bytes differ.
DIFF_MAX_BYTES = 256 * 1024
DIFF_MAX_LINES = 400

ARCHIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.zip$")

# Where a synthesized (not copied) member lands in the archive.
MCP_MEMBER = "mcp/mcpServers.json"
FILES_PREFIX = "files/"


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


def _entry(path, group="", src=None, data=None, mode=None, size=None):
    return {"path": path, "group": group, "src": src, "data": data,
            "mode": mode, "size": size}

def _file_entries(group, pairs):
    """Config-dir files -> archive members under files/."""
    out = []
    cfg = config_dir()
    for p in pairs:
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
                          mode=st.st_mode & 0o777, size=st.st_size))
    return out

def _walk_entries(group, root, cap=MAX_FILE):
    try:
        base = root.relative_to(config_dir()).as_posix()
    except ValueError:
        return []
    return [_entry(FILES_PREFIX + (base + "/" + rel if base else rel),
                   group=group, src=p, mode=st.st_mode & 0o777, size=st.st_size)
            for rel, p, st in _walk(root, cap)]


# -------------------------------------------------------------------- groups

def _g_items():
    out = []
    for t in ITEM_TYPES:
        out += _walk_entries("items", config_dir() / t)
        out += _walk_entries("items", disabled_dir() / t)
    return out

def _g_config():
    return _file_entries("config", [config_dir() / n for n in CONFIG_FILES])

def _g_statusline():
    return _file_entries("statusline", list(statusline_paths()))

def _g_mcp():
    """The mcpServers map from ~/.claude.json, synthesized — never that whole
    file, which also holds your project history and OAuth account."""
    out = []
    data, err = _read_json_object(CLAUDE_JSON)
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and servers:
        blob = (json.dumps({"mcpServers": servers}, indent=2) + "\n").encode()
        out.append(_entry(MCP_MEMBER, group="mcp", data=blob, size=len(blob)))
    out += _file_entries("mcp", [disabled_dir() / "mcp-servers.json"])
    return out

def _g_plugins():
    """Which plugins you had and where they came from — not their trees.

    An installed plugin is a tarball extract that reinstalls from its
    marketplace; copying megabytes of someone else's code into every archive
    buys nothing a `claude plugin install` doesn't. What is not recoverable is
    the list, so that is what goes in. (`enabledPlugins` lives in settings.json
    and rides along with the config group.)
    """
    root = plugins_root()
    paths = [root / "config.json", root / "known_marketplaces.json"]
    mdir = root / "marketplaces"
    if mdir.is_dir():
        try:
            paths += [d / ".claude-plugin" / "marketplace.json"
                      for d in sorted(mdir.iterdir()) if d.is_dir()]
        except OSError:
            pass
    return _file_entries("plugins", paths)

def _g_transcripts():
    return _walk_entries("transcripts", projects_dir(), MAX_TRANSCRIPT)


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


def _collect(picks):
    picks = [p for p in GROUP_IDS if p in set(picks or [])]
    if not picks:
        raise ValueError("nothing selected")
    entries, seen = [], set()
    for g in GROUPS:
        if g["id"] not in picks:
            continue
        for e in g["collect"]():
            if e["path"] in seen:   # a file two groups both claim
                continue
            seen.add(e["path"])
            entries.append(e)
    return entries

def backup_plan():
    """What each group would put in an archive, for the pick list."""
    out = []
    for g in GROUPS:
        try:
            entries = g["collect"]()
        except OSError as e:
            out.append({"id": g["id"], "label": g["label"], "files": 0,
                        "bytes": 0, "note": g["note"], "secrets": bool(g.get("secrets")),
                        "error": str(e)})
            continue
        out.append({"id": g["id"], "label": g["label"],
                    "files": len(entries),
                    "bytes": sum(e["size"] or 0 for e in entries),
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

def backup_create(picks, note=""):
    entries = _collect(picks)
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
                    "mode": e["mode"], "sha256": _sha(data)})
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

def _target(member):
    """Archive member -> the absolute file it restores to.

    Every path is checked twice: for shape (no absolute, no ``..``, a known
    prefix) and for where it lands after resolution, which is what actually
    stops a crafted archive escaping. resolve_editable() is not used here: its
    plugins carve-out is an editor rule, and this needs plugins/config.json to
    be writable while still refusing anything outside the config dir.
    """
    if not isinstance(member, str) or member == MCP_MEMBER:
        raise ValueError("bad member")
    if not member.startswith(FILES_PREFIX):
        raise ValueError(f"{member}: not a restorable path")
    rel = member[len(FILES_PREFIX):]
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p == ".." for p in parts) or rel.startswith("/"):
        raise ValueError(f"{member}: unsafe path")
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

def _live_mcp_bytes():
    data, _ = _read_json_object(CLAUDE_JSON)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return None
    return (json.dumps({"mcpServers": servers}, indent=2) + "\n").encode()

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
                   "size": int(e.get("size") or 0)}
            if member not in names:
                rows.append({**row, "status": "missing",
                             "error": "listed in the manifest but not in the zip"})
                continue
            if member == MCP_MEMBER:
                row["target"] = tilde(CLAUDE_JSON) + " → mcpServers"
                live = _live_mcp_bytes()
            else:
                try:
                    target = _target(member)
                except ValueError as err:
                    rows.append({**row, "status": "refused", "error": str(err)})
                    continue
                row["target"] = tilde(target)
                live = target.read_bytes() if target.is_file() else None
            if live is None:
                row["status"] = "new"
            elif _sha(live) == e.get("sha256"):
                row["status"] = "same"
            else:
                row["status"] = "differs"
                new = z.read(member)
                if (len(live) <= DIFF_MAX_BYTES and len(new) <= DIFF_MAX_BYTES
                        and _is_text(live) and _is_text(new)):
                    row["diff"] = _diff(live, new, row["target"])
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

def backup_restore(name, paths):
    """Write back exactly the members in `paths`. Never deletes anything."""
    p = archive_path(name)
    want = [x for x in (paths or []) if isinstance(x, str)]
    if not want:
        raise ValueError("nothing selected")
    written, failed = [], []
    with zipfile.ZipFile(p) as z:
        m = _manifest_of(z)
        modes = {e.get("path"): e.get("mode") for e in m["entries"]}
        known = set(modes)
        for member in want:
            if member not in known:
                failed.append({"path": member, "error": "not in this archive"})
                continue
            try:
                data = z.read(member)
                if member == MCP_MEMBER:
                    n = _restore_mcp(data)
                    written.append({"path": member,
                                    "target": tilde(CLAUDE_JSON),
                                    "detail": f"{n} server(s) merged"})
                else:
                    target = _target(member)
                    atomic_write_bytes(target, data, modes.get(member))
                    written.append({"path": member, "target": tilde(target)})
            except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
                failed.append({"path": member, "error": str(e)})
    return {"written": written, "failed": failed,
            "count": len(written), "failed_count": len(failed)}

def backup_delete(name):
    archive_path(name).unlink()
    return {"deleted": name}
