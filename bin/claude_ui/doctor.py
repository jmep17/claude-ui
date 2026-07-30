"""Report-only health checks over the live machine config."""

from pathlib import Path
import json
import os
import shutil
import time

from .core import ITEM_TYPES, _read_json_object, config_dir, tilde
from .items import scan_items
from .mcp import mcp_state
from .settings import SETTINGS_SCHEMA, settings_state
from .statusline import STATUSLINE_SCRIPT, statusline_paths


def _first_cmd_word(cmd):
    return (cmd or "").strip().split()[0] if (cmd or "").strip() else ""

def _cmd_missing(cmd):
    """True if a hook/statusline command's executable clearly doesn't exist."""
    word = os.path.expanduser(_first_cmd_word(cmd))
    if not word or any(c in word for c in "$`("):  # shell expr — can't judge
        return False
    if word.startswith(("/", ".")) or os.sep in word:
        return not os.path.exists(word)
    return shutil.which(word) is None

def doctor():
    finds = []

    def add(level, area, msg):
        finds.append({"level": level, "area": area, "msg": msg})

    cfg = config_dir()

    if cfg.is_dir():
        for p in sorted(cfg.glob("*.bak*")):
            add("info", "config", f"{tilde(p)} — leftover backup; delete once "
                                  "you're sure")
        for p in sorted(cfg.iterdir()):
            if p.is_symlink() and not p.exists():
                add("warn", "config", f"{tilde(p)} — broken symlink "
                                      f"(points at {p.readlink()})")

    # ~/.claude.json / settings.json that don't parse
    st = mcp_state()
    if st["machine_error"]:
        add("warn", "mcp", f"{st['machine_path']}: {st['machine_error']}")
    sstate = settings_state()
    if sstate["error"]:
        add("warn", "settings", f"{sstate['path']}: {sstate['error']}")

    # settings.json: hooks / statusLine pointing at missing executables
    sdata = sstate["data"]
    hooks = sdata.get("hooks")
    if isinstance(hooks, dict):
        for event, matchers in hooks.items():
            for m in matchers if isinstance(matchers, list) else []:
                for h in (m.get("hooks") or []) if isinstance(m, dict) else []:
                    cmd = h.get("command") if isinstance(h, dict) else None
                    if cmd and _cmd_missing(cmd):
                        add("warn", "settings",
                            f"hooks.{event}: command not found: {_first_cmd_word(cmd)}")
    sl = sdata.get("statusLine")
    if isinstance(sl, dict) and sl.get("command") and _cmd_missing(sl["command"]):
        add("warn", "settings",
            f"statusLine.command not found: {_first_cmd_word(sl['command'])}")

    # settings keys outside the documented schema
    known = {s["key"].split(".")[0] for s in SETTINGS_SCHEMA}
    for k in sdata if isinstance(sdata, dict) else {}:
        if k not in known:
            add("info", "settings", f"settings.json key not in the documented "
                                    f"schema: {k}")

    # MCP: stdio commands that don't resolve on this machine
    for s in st["servers"]:
        cmd = (s["config"] or {}).get("command")
        if cmd and _cmd_missing(cmd):
            add("warn", "mcp", f"{s['name']}: command not found: {cmd}")

    # statusline drift: script on disk differs from what the saved config
    # would generate (hand edits get overwritten on the next UI save)
    cfgp, scriptp = statusline_paths()
    if cfgp.is_file() and scriptp.is_file():
        saved, err = _read_json_object(cfgp)
        if not err and saved:
            expected = STATUSLINE_SCRIPT.replace(
                "__CONFIG__", json.dumps(json.dumps(saved)))
            if scriptp.read_text(errors="replace") != expected:
                add("warn", "statusline",
                    f"{tilde(scriptp)} differs from the saved statusline "
                    "config — hand edits are lost on the next UI save")

    # item quality
    for t in ITEM_TYPES:
        items = scan_items(t)
        live = {it["name"] for it in items if it["enabled"]}
        for it in items:
            where = "" if it["enabled"] else " (disabled)"
            if not it["enabled"] and it["name"] in live:
                add("warn", t, f"{it['name']}: exists both enabled and disabled "
                               "— re-enabling it would fail; resolve by hand")
            if it.get("broken"):
                add("warn", t, f"{it['name']}{where}: broken symlink")
            if it.get("incomplete"):
                add("warn", t, f"{it['name']}{where}: missing SKILL.md")
            if it.get("todo"):
                add("info", t, f"{it['name']}{where}: leftover TODO placeholder")
            if it.get("name_mismatch"):
                add("info", t, f"{it['name']}{where}: frontmatter name doesn't "
                               "match the folder name")
            if it.get("long_desc"):
                add("info", t, f"{it['name']}{where}: description over 1024 chars")
            if (t == "skills" and it["enabled"] and it.get("description")
                    and "use when" not in it["description"].lower()
                    and not it.get("todo") and not it.get("broken")):
                add("info", t, f"{it['name']}: description has no \"Use when …\" "
                               "trigger — Claude may not know when to load it")

    # plugin skills sharing a name with config skills (one shadows the other)
    plugins_dir = Path.home() / ".claude" / "plugins"
    if plugins_dir.is_dir():
        ours = {i["name"] for i in scan_items("skills") if i["enabled"]}
        try:
            for smd in list(plugins_dir.glob("*/*/skills/*/SKILL.md"))[:500] + \
                       list(plugins_dir.glob("*/*/*/skills/*/SKILL.md"))[:500]:
                pname = smd.parent.name
                if pname in ours:
                    add("info", "plugins",
                        f"installed plugin skill '{pname}' shares a name with "
                        f"your skill ({smd.parent}) — one may shadow the other")
        except OSError:
            pass

    order = {"warn": 0, "info": 1}
    finds.sort(key=lambda f: (order[f["level"]], f["area"]))
    return {"findings": finds,
            "warns": sum(1 for f in finds if f["level"] == "warn"),
            "ts": time.strftime("%H:%M:%S")}
