"""Machine inventory: the items actually in the Claude config dir."""

from pathlib import Path
import json
import os
import shutil

from .core import (CONFIG_FILES, ITEM_TYPES, SOURCE_KEY, atomic_write,
                   atomic_write_bytes, config_dir, disabled_dir, item_rel,
                   parse_frontmatter, project_claude_dir, resolve_editable,
                   set_frontmatter_key, tilde)


MAX_EDIT = 2 * 1024 * 1024

def _todo_line(text):
    """1-based line of the first TODO, or 0. The doctor turns this into a
    click that lands on the placeholder instead of just naming the file."""
    i = text.find("TODO")
    return text.count("\n", 0, i) + 1 if i >= 0 else 0

def item_scope(root):
    """The directory an item op works under: the config dir when no project
    root was named, that project's gated .claude/ when one was.

    Every project-scoped op resolves its scope here, so the registry check is
    not something a new endpoint can forget to make. `None` in, `None` out —
    every function below reads that as "the config dir", which leaves the
    user-scope call sites exactly as they were.

    Called `scope`, not `base`, throughout this module: `base` was already
    taken by the mtime a save started from."""
    return project_claude_dir(root) if root else None

def item_root(type_, enabled=True, scope=None):
    """Directory holding a type's items: live, or the disabled parking area.

    `scope` is the config dir by default, and a registered project's gated
    .claude/ when the Projects tab is managing that project's own copy. The
    parking area keeps its shape either way, so a project's disabled items sit
    in <project>/.claude/disabled/<type>/ — somewhere Claude Code does not
    scan, legible in a plain `ls`, and committed with the repo like everything
    else in there."""
    if scope is None:
        return (config_dir() if enabled else disabled_dir()) / type_
    return (scope if enabled else scope / "disabled") / type_

def resolve_item(type_, name, enabled=True, scope=None):
    if type_ not in ITEM_TYPES:
        raise ValueError("unknown type")
    rel = item_rel(name)
    if ITEM_TYPES[type_]["kind"] == "md":
        rel = rel.with_suffix(".md")
    elif len(rel.parts) != 1:
        raise ValueError("bad name")
    return item_root(type_, enabled, scope) / rel

def _dir_item(entry, enabled):
    skill_md = entry / "SKILL.md"
    broken = entry.is_symlink() and not entry.exists()
    text = "" if broken else (
        skill_md.read_text(errors="replace") if skill_md.is_file() else "")
    meta = parse_frontmatter(text)
    try:
        mtime = entry.stat().st_mtime
    except OSError:
        mtime = 0
    return {
        "name": entry.name, "enabled": enabled,
        "symlink": entry.is_symlink(), "broken": broken,
        "incomplete": not broken and not skill_md.is_file(),
        "description": ("(broken symlink: " + str(entry.readlink()) + ")")
                       if broken else meta.get("description", ""),
        "path": tilde(entry), "mtime": mtime, "chars": len(text),
        "todo": "TODO" in text,
        "todo_line": _todo_line(text),
        # the plugin this was split out of, if any — the frontmatter is already
        # parsed here, so saying where an item came from costs nothing
        "source": meta.get(SOURCE_KEY) or "",
        "name_mismatch": bool(meta.get("name")) and meta["name"] != entry.name,
        "long_desc": len(meta.get("description", "")) > 1024,
        # a skill with disable-model-invocation can only be run by the user, so
        # it can't be preloaded into an agent's `skills:` list either — the
        # picker that offers skills needs to know before it offers one
        "no_model_invoke": str(meta.get("disable-model-invocation", "")
                               ).strip().lower() in ("true", "yes"),
    }

def _scan_dir_type(root, enabled):
    items = []
    if not root.is_dir():
        return items
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir() or entry.is_symlink():
            items.append(_dir_item(entry, enabled))
    return items

def _scan_md_type(root, enabled):
    items = []
    if not root.is_dir():
        return items
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        name = str(rel)[:-3]
        broken = p.is_symlink() and not p.exists()
        text = "" if broken else p.read_text(errors="replace")
        meta = parse_frontmatter(text)
        try:
            mtime = 0 if broken else p.stat().st_mtime
        except OSError:
            mtime = 0
        items.append({
            "name": name, "enabled": enabled,
            "symlink": p.is_symlink(), "broken": broken, "incomplete": False,
            "description": ("(broken symlink: " + str(p.readlink()) + ")")
                           if broken else meta.get("description", ""),
            "path": tilde(p), "mtime": mtime, "chars": len(text),
            "todo": "TODO" in text,
            "todo_line": _todo_line(text),
            "source": meta.get(SOURCE_KEY) or "",
            "model": meta.get("model", ""),
            # Claude Code registers an output style under its frontmatter
            # name when set, not the filename — settings must use this value
            "meta_name": "" if broken else str(meta.get("name", "") or ""),
            "name_mismatch": False,
            "long_desc": len(meta.get("description", "")) > 1024,
        })
    return items

def scan_items(type_, scope=None):
    """Every item of a type in one scope: live first, then disabled. `scope`
    is the config dir by default, a project's .claude/ when given."""
    scan = _scan_dir_type if ITEM_TYPES[type_]["kind"] == "dir" else _scan_md_type
    return (scan(item_root(type_, True, scope), True)
            + scan(item_root(type_, False, scope), False))

def config_files_state():
    """The single config files present in the config dir."""
    out = []
    for name in CONFIG_FILES:
        p = config_dir() / name
        if p.is_file() or p.is_symlink():
            out.append({"name": name, "path": tilde(p),
                        "symlink": p.is_symlink(),
                        "broken": p.is_symlink() and not p.exists()})
    return out

def set_enabled(type_, name, enabled, scope=None):
    """Move an item between the live type dir and disabled/<type>/. `enabled`
    is the desired end state. Returns the item's new location string."""
    if type_ not in ITEM_TYPES:
        raise ValueError("unknown type")
    src = resolve_item(type_, name, not enabled, scope)
    dst = resolve_item(type_, name, enabled, scope)
    if not (src.exists() or src.is_symlink()):
        raise ValueError(f"{name}: not {'enabled' if not enabled else 'disabled'}")
    if dst.exists() or dst.is_symlink():
        raise ValueError(
            f"{name}: already exists on the "
            f"{'enabled' if enabled else 'disabled'} side — resolve by hand")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)  # same filesystem: atomic, content untouched
    # Tidy the empty directories the move left behind, each side with its own
    # floor. The parking area is ours alone, so it may empty out completely —
    # not accreting cruft is the whole reason to prune it. The live side stops
    # at the type dir: commands/ is a directory Claude Code scans, and
    # disabling the last command is not a request to remove it, the rule
    # item_delete() states below.
    #
    # Which end is which follows the direction of travel, and the stops are
    # given rather than defaulted — the default climbs to the config dir,
    # which under a project scope means walking out of .claude/ and into the
    # repo.
    live, parked = (dst, src) if enabled else (src, dst)
    _prune_empty_up(parked.parent, stop=scope or config_dir())
    _prune_empty_up(live.parent, stop=item_root(type_, True, scope))
    return tilde(dst)

def _prune_empty_up(d, stop=None):
    """Remove empty dirs from d up to (not including) `stop`, the config dir by
    default. A caller that must keep a directory Claude Code scans — deleting
    the last command should not take commands/ with it — passes its own."""
    stop = stop or disabled_dir().parent
    while d != stop and d.is_dir() and not d.is_symlink():
        try:
            if any(d.iterdir()):
                break
            parent = d.parent
            d.rmdir()
            d = parent
        except OSError:
            break

def _item_file_rel(f):
    """Validate a within-item relative file path (no traversal, no dotfiles)."""
    rel = Path(*[p for p in f.split("/") if p and p != ".." and not p.startswith(".")])
    if not rel.parts or str(rel) != f:
        raise ValueError("bad file name")
    return rel

def _skill_files(root):
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and p.stat().st_size <= MAX_EDIT
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
    )[:200]

def _stamp(path):
    """Identity of the bytes we just read, so a later save can tell whether
    someone else (a Claude Code session, your $EDITOR) moved them."""
    try:
        st = path.stat()
        return {"mtime": st.st_mtime, "size": st.st_size}
    except OSError:
        return {"mtime": 0, "size": 0}

class Conflict(ValueError):
    """The file changed underneath the editor — surfaced as a 409, not a 400."""

def _check_base(path, base):
    """Refuse a write whose starting point is no longer what's on disk."""
    if base is None:
        return
    now = _stamp(path)["mtime"]
    if now != base:
        # Deliberately just the fact: the caller owns the wording of the
        # choice, and the two run together badly if both editorialise.
        raise Conflict(f"{path.name} changed on disk since you opened it.")

def item_read(type_, name, fname=None, enabled=True, scope=None):
    root = resolve_item(type_, name, enabled, scope)
    if ITEM_TYPES[type_]["kind"] == "md":
        if not root.is_file():
            raise ValueError(f"{name}: not found")
        return {"type": type_, "name": name, "enabled": enabled,
                "files": [root.name], "file": root.name, "exists": True,
                "content": root.read_text(errors="replace"), "path": tilde(root),
                **_stamp(root)}
    if not root.is_dir():  # follows symlinks
        raise ValueError(f"{name}: not found")
    files = _skill_files(root)
    f = fname or ("SKILL.md" if "SKILL.md" in files or not files else files[0])
    target = root / _item_file_rel(f)
    return {"type": type_, "name": name, "enabled": enabled,
            "files": files, "file": f, "exists": target.is_file(),
            "content": target.read_text(errors="replace") if target.is_file() else "",
            "path": tilde(target), **_stamp(target)}

def _reject_bad_json(path, content):
    """A .json file must parse before we overwrite it, so a bad save can't
    corrupt config Claude Code reads. The one gate for every file write."""
    if path.suffix == ".json" and content.strip():
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from None

def item_save(type_, name, fname, content, enabled=True, base=None, scope=None):
    if not isinstance(content, str) or len(content) > MAX_EDIT:
        raise ValueError("bad content")
    root = resolve_item(type_, name, enabled, scope)
    if ITEM_TYPES[type_]["kind"] == "md":
        if not root.is_file():
            raise ValueError(f"{name}: not found")
        _check_base(root, base)
        atomic_write(root, content)
        return {"path": tilde(root), **_stamp(root)}
    if not root.is_dir():
        raise ValueError(f"{name}: not found")
    target = root / _item_file_rel(fname or "SKILL.md")
    _reject_bad_json(target, content)
    _check_base(target, base)
    atomic_write(target, content)
    return {"path": tilde(target), **_stamp(target)}

def item_create(type_, name, content, enabled=True, scope=None):
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
    _need_free(type_, name, scope)
    target = resolve_item(type_, name, enabled, scope)
    if ITEM_TYPES[type_]["kind"] == "dir":
        target = target / "SKILL.md"
    _reject_bad_json(target, content)
    atomic_write(target, content)
    return item_read(type_, name, None, enabled, scope)

def _need_free(type_, name, scope):
    """Refuse a name already taken on either side of disabled/ — see
    item_create() for why both sides count."""
    for side in (True, False):
        p = resolve_item(type_, name, side, scope)
        if p.exists() or p.is_symlink():
            raise ValueError(f"{name}: already exists at {tilde(p)}")

def item_copy(type_, name, from_scope=None, to_scope=None, enabled=True):
    """Copy an item between scopes: your config dir and a project's .claude/.

    The contents, not the link. A skill symlinked in from a checkout copies the
    files it points at, because the copy has to go on working when that
    checkout moves — the mirror of item_delete() below, which unlinks and
    leaves the target alone. Both rules come from the same place: neither may
    reach into a directory we were never invited to write.

    Nothing is removed at the source. Moving an item is a copy and then a
    delete, two decisions the caller makes separately, because one of them
    cannot be undone and the other can.
    """
    if from_scope == to_scope:
        raise ValueError("source and destination are the same place")
    src = resolve_item(type_, name, enabled, from_scope)
    if not (src.is_dir() or src.is_file()):  # follows a symlink to its target
        raise ValueError(f"{name}: not found")
    _need_free(type_, name, to_scope)
    dst = resolve_item(type_, name, enabled, to_scope)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        # temp-then-rename, the shape core._atomic uses: a copytree that dies
        # half way leaves its mess under a dot-name, not a skill with three of
        # its five files that Claude Code would happily load
        tmp = dst.with_name(f".{dst.name}.claude-ui-tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp, symlinks=False)
        tmp.replace(dst)
    else:
        atomic_write_bytes(dst, src.read_bytes())
    return {"path": tilde(dst), "name": name, "type": type_}

def item_delete(type_, name, enabled=True, scope=None):
    """Remove an item for good. The one call in this module that destroys.

    A symlinked item loses the link and nothing else: a skill symlinked in from
    a git checkout lives somewhere we were never invited to write, and rmtree
    following that link would delete the checkout. Path handling is
    resolve_item()'s — the name is validated and confined to the type's own
    directory there, so nothing here can reach outside it.

    Empty parents left behind by a nested command go too, but the prune stops
    at the type root: `commands/` is a directory Claude Code scans, and
    deleting the last command in it is not a request to remove it.
    """
    root = item_root(type_, enabled, scope)
    p = resolve_item(type_, name, enabled, scope)
    if not (p.exists() or p.is_symlink()):
        raise ValueError(f"{name}: not found")
    files = 1
    if p.is_symlink() or p.is_file():
        p.unlink()
    else:
        # os.walk, not rglob: rglob descends into a symlinked subdirectory and
        # would count files rmtree is (rightly) never going to touch
        files = sum(len(names) for _, _, names in os.walk(p))
        shutil.rmtree(p)
    _prune_empty_up(p.parent, stop=root)
    return {"deleted": tilde(p), "files": files}

def item_set_model(name, model, enabled=True, scope=None):
    """Rewrite an agent's `model:` frontmatter line; blank model removes it.

    Only agents: no other item type has a model. Goes through resolve_item, so
    this can never reach outside the config dir into a plugin's own copy.
    """
    p = resolve_item("agents", name, enabled, scope)
    if not p.is_file():
        raise ValueError(f"{name}: not found")
    text = p.read_text(errors="replace")
    out = set_frontmatter_key(text, "model", model.strip() or None
                              if isinstance(model, str) else None)
    if out != text:
        atomic_write(p, out)
    return {"path": tilde(p), "model": (model or "").strip(), **_stamp(p)}

def path_read(raw):
    """Read any file we're willing to open by absolute path — the config dir,
    ~/.claude.json, a registered project's .claude/ subtree, or (read-only)
    an installed plugin."""
    p, readonly = resolve_editable(raw)
    if p.is_file() and p.stat().st_size > MAX_EDIT:
        raise ValueError(f"{p.name}: too large to edit here "
                         f"({p.stat().st_size // 1024} KB)")
    return {"path": str(p), "tilde": tilde(p), "name": p.name,
            "exists": p.is_file(), "readonly": readonly,
            "content": p.read_text(errors="replace") if p.is_file() else "",
            **_stamp(p)}

def path_save(raw, content, base=None):
    p, readonly = resolve_editable(raw)
    if readonly:
        raise ValueError(f"{p.name} is read-only here — it belongs to an "
                         "installed plugin")
    if not isinstance(content, str) or len(content) > MAX_EDIT:
        raise ValueError("bad content")
    _reject_bad_json(p, content)
    _check_base(p, base)
    atomic_write(p, content)
    return {"path": str(p), "tilde": tilde(p), **_stamp(p)}
