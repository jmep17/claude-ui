"""Machine inventory: the items actually in the Claude config dir."""

from pathlib import Path
import json

from .core import (CONFIG_FILES, ITEM_TYPES, SOURCE_KEY, atomic_write,
                   config_dir, disabled_dir, item_rel, parse_frontmatter,
                   resolve_editable, set_frontmatter_key, tilde)


MAX_EDIT = 2 * 1024 * 1024

def _todo_line(text):
    """1-based line of the first TODO, or 0. The doctor turns this into a
    click that lands on the placeholder instead of just naming the file."""
    i = text.find("TODO")
    return text.count("\n", 0, i) + 1 if i >= 0 else 0

def item_root(type_, enabled=True):
    """Directory holding a type's items: live, or the disabled parking area."""
    base = config_dir() if enabled else disabled_dir()
    return base / type_

def resolve_item(type_, name, enabled=True):
    if type_ not in ITEM_TYPES:
        raise ValueError("unknown type")
    rel = item_rel(name)
    if ITEM_TYPES[type_]["kind"] == "md":
        rel = rel.with_suffix(".md")
    elif len(rel.parts) != 1:
        raise ValueError("bad name")
    return item_root(type_, enabled) / rel

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
        "path": tilde(entry), "mtime": mtime,
        "todo": "TODO" in text,
        "todo_line": _todo_line(text),
        # the plugin this was split out of, if any — the frontmatter is already
        # parsed here, so saying where an item came from costs nothing
        "source": meta.get(SOURCE_KEY) or "",
        "name_mismatch": bool(meta.get("name")) and meta["name"] != entry.name,
        "long_desc": len(meta.get("description", "")) > 1024,
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
            "path": tilde(p), "mtime": mtime,
            "todo": "TODO" in text,
            "todo_line": _todo_line(text),
            "source": meta.get(SOURCE_KEY) or "",
            "model": meta.get("model", ""),
            "name_mismatch": False,
            "long_desc": len(meta.get("description", "")) > 1024,
        })
    return items

def scan_items(type_):
    """Every item of a type on this machine: live first, then disabled."""
    scan = _scan_dir_type if ITEM_TYPES[type_]["kind"] == "dir" else _scan_md_type
    return (scan(item_root(type_, True), True)
            + scan(item_root(type_, False), False))

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

def set_enabled(type_, name, enabled):
    """Move an item between the live type dir and disabled/<type>/. `enabled`
    is the desired end state. Returns the item's new location string."""
    if type_ not in ITEM_TYPES:
        raise ValueError("unknown type")
    src = resolve_item(type_, name, enabled=not enabled)
    dst = resolve_item(type_, name, enabled=enabled)
    if not (src.exists() or src.is_symlink()):
        raise ValueError(f"{name}: not {'enabled' if not enabled else 'disabled'}")
    if dst.exists() or dst.is_symlink():
        raise ValueError(
            f"{name}: already exists on the "
            f"{'enabled' if enabled else 'disabled'} side — resolve by hand")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)  # same filesystem: atomic, content untouched
    # tidy now-empty dirs in the disabled area so it doesn't accrete cruft
    disabled_side = src if not enabled else dst
    _prune_empty_up(disabled_side.parent)
    return tilde(dst)

def _prune_empty_up(d):
    """Remove empty dirs from d up to (not including) the config dir."""
    stop = disabled_dir().parent
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

def item_read(type_, name, fname=None, enabled=True):
    root = resolve_item(type_, name, enabled)
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

def item_save(type_, name, fname, content, enabled=True, base=None):
    if not isinstance(content, str) or len(content) > MAX_EDIT:
        raise ValueError("bad content")
    root = resolve_item(type_, name, enabled)
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

def item_set_model(name, model, enabled=True):
    """Rewrite an agent's `model:` frontmatter line; blank model removes it.

    Only agents: no other item type has a model. Goes through resolve_item, so
    this can never reach outside the config dir into a plugin's own copy.
    """
    p = resolve_item("agents", name, enabled)
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
    ~/.claude.json, or (read-only) an installed plugin."""
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
