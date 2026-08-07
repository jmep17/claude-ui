"""Shared constants, machine-local config, and path/frontmatter helpers."""

from pathlib import Path
import json
import os
import re
import secrets



# this file lives at <repo>/bin/claude_ui/core.py
REPO = Path(__file__).resolve().parents[2]

CONFIG_FILE = REPO / ".claude-ui.json"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

def item_rel(name):
    """Validate a possibly-nested item name ('git/pr') into a relative Path."""
    parts = [p for p in (name or "").split("/") if p]
    if not parts or any(not NAME_RE.match(p) for p in parts):
        raise ValueError("bad name")
    return Path(*parts)

# Written into an adopted item's frontmatter. Claude Code ignores unknown keys;
# the x- prefix marks it as ours. Keeping the fact in the file rather than a
# sidecar manifest means it survives the user moving or committing the file.
# Lives here because both plugins.py (which writes it) and items.py (which
# reports it on every scan) need it, and neither imports the other.
SOURCE_KEY = "x-claude-ui-source"

# the four item-type directories inside the Claude config dir
ITEM_TYPES = {
    "skills": {"kind": "dir"},
    "commands": {"kind": "md"},
    "agents": {"kind": "md"},
    "output-styles": {"kind": "md"},
}

CONFIG_FILES = ("CLAUDE.md", "settings.json", "keybindings.json")

MCP_FILE = "mcp-servers.json"

CLAUDE_JSON = Path.home() / ".claude.json"  # user-scope mcpServers live here

# Per-run token: POSTs must echo it back, so a random webpage doing
# cross-origin/DNS-rebinding requests against 127.0.0.1 can't mutate config.
TOKEN = secrets.token_hex(16)

def read_cfg():
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def write_cfg(cfg):
    if not cfg:
        CONFIG_FILE.unlink(missing_ok=True)
    else:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")

def config_dir():
    p = read_cfg().get("config_dir")
    if p:
        return Path(p).expanduser()
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"

def set_config_dir(path):
    cfg = read_cfg()
    if not path:
        cfg.pop("config_dir", None)
    else:
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise ValueError("config dir must be an absolute path (or start with ~)")
        cfg["config_dir"] = str(p)
    write_cfg(cfg)

def disabled_dir():
    """Parked home for disabled things — outside every dir Claude Code scans."""
    return config_dir() / "disabled"

def plugins_dir():
    """Where Claude Code installs plugins: readable here, never written.

    The same path as plugins.plugins_root(); duplicated rather than imported
    because plugins.py imports this module, and resolve_editable() below has to
    know about plugins to mark them read-only."""
    return config_dir() / "plugins"

def _within(path, root):
    return path == root or root in path.parents

# One absolute project root per line; the Projects tab appends to it and the
# generated fish function greps it as an allowlist. Lives here (not in
# projects.py) because resolve_editable() below needs it — the same layering
# note as plugins_dir() above.
PROJECTS_REGISTRY = "claude-ui-projects.txt"

def project_roots():
    """Registered project roots, exactly as recorded (absolute, resolved at
    registration). Read fresh on every call: it is one small file, and
    resolve_editable must see a just-registered project without a restart.
    Validation happens at registration (projects.py); a root that has since
    vanished simply matches nothing here."""
    try:
        text = (config_dir() / PROJECTS_REGISTRY).read_text()
    except OSError:
        return []
    return [Path(s) for s in (l.strip() for l in text.splitlines())
            if s and not s.startswith("#")]

def resolve_editable(raw):
    """Expand a user-supplied path and decide whether we'll open it at all.

    Editable: the config dir, ~/.claude.json, and the .claude/ subtree of each
    registered project root. Installed plugins are readable only. Returns
    (resolved Path, readonly bool); raises ValueError otherwise. The check
    runs against the *resolved* path, so a symlink sitting in the config dir
    is judged by where it points, not by where it lives — and a project whose
    .claude is itself a symlink is refused outright, so a checkout cannot
    point us at an arbitrary directory."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("no path given")
    p = Path(raw.strip()).expanduser()
    if not p.is_absolute():
        raise ValueError("path must be absolute (or start with ~)")
    # strict=False so a not-yet-created file still resolves to a real target
    p = p.resolve()
    if _within(p, plugins_dir().resolve()):
        return p, True  # installed plugins are someone else's copy — look only
    if _within(p, config_dir().resolve()) or p == CLAUDE_JSON.resolve():
        return p, False
    for root in project_roots():
        croot = root.resolve() / ".claude"
        if croot.resolve() != croot:
            continue
        if _within(p, croot):
            return p, False
    raise ValueError("path is outside the config dir and every registered project")

def tilde(p):
    return str(p).replace(str(Path.home()), "~", 1)

def parse_frontmatter(text):
    meta = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta
    key = None
    buf = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            if key and buf:
                meta[key] = " ".join(buf).strip()
            val = m.group(2).strip()
            if val in (">", "|", ">-", "|-"):
                key, buf = m.group(1), []
            else:
                meta[m.group(1)] = val
                key, buf = None, []
        elif key and line.startswith((" ", "\t")):
            buf.append(line.strip())
    if key and buf:
        meta[key] = " ".join(buf).strip()
    return meta

def set_frontmatter_key(text, key, value):
    """Return `text` with `key: value` set in its frontmatter; None removes it.

    The inverse of parse_frontmatter() above, and deliberately as blunt: it
    rewrites one line and leaves every other byte alone, so a hand-formatted
    file survives a model change with its comments, ordering and folded blocks
    intact. A key that is not there is appended to the end of the block; a file
    with no block at all gets one, unless the value is None (nothing to remove).
    """
    if not re.match(r"^[A-Za-z0-9_-]+$", key or ""):
        raise ValueError("bad frontmatter key")
    if value is not None:
        value = str(value)
        if re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError("value contains a control character")
    lines = text.splitlines()
    # CRLF in, CRLF out — a Windows-authored agent should not come back mixed
    nl = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith("\n")

    def join(out):
        return nl.join(out) + (nl if trailing else "")

    if not lines or lines[0].strip() != "---":
        if value is None:
            return text
        return join(["---", f"{key}: {value}", "---"] + lines)
    close = next((i for i in range(1, len(lines))
                  if lines[i].strip() == "---"), None)
    if close is None:
        raise ValueError("unterminated frontmatter block")
    at = next((i for i in range(1, close)
               if re.match(rf"^{re.escape(key)}:", lines[i])), None)
    if at is None:
        return text if value is None else join(
            lines[:close] + [f"{key}: {value}"] + lines[close:])
    # a folded value ('key: >') continues into the indented lines below it,
    # which belong to this key and go with it
    end = at + 1
    while end < close and lines[end].startswith((" ", "\t")):
        end += 1
    return join(lines[:at] + ([] if value is None else [f"{key}: {value}"])
                + lines[end:])

def _read_json_object(path):
    """(data, error) — data is {} on missing file; error set on bad JSON."""
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return {}, str(e)
    return (data, None) if isinstance(data, dict) else ({}, "top level is not a JSON object")

def atomic_write(path, content, mode=None):
    """Write text via temp file + rename so readers never see partial content."""
    _atomic(path, mode, lambda tmp: tmp.write_text(content))

def atomic_write_bytes(path, data, mode=None):
    """The bytes form of atomic_write, for content that isn't text (a skill's
    image, a compiled helper) or that carries a mode worth keeping."""
    _atomic(path, mode, lambda tmp: tmp.write_bytes(data))

def _atomic(path, mode, put):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.claude-ui-tmp")
    put(tmp)
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)
