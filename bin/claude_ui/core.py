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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.claude-ui-tmp")
    tmp.write_text(content)
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)
