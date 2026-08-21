"""Which tools sessions actually call, and the switch that turns one off.

Every tool Claude Code loads ships its schema with every request, so a tool
you never use is pure context cost. The off switch is Claude Code's own:
`permissions.deny` in settings.json. A bare tool name there — "WebSearch",
no `(...)` argument filter — removes the whole tool from the session
(the settings equivalent of `--disallowedTools`), unlike a `Bash(git:*)`-style
rule, which only gates individual calls. This module joins three things the
app already has: the per-tool histogram insight.py counts from the
transcripts, the deny list, and the MCP inventory — because for MCP servers
the same question has a bigger answer: disabling the whole server (the MCP
tab's toggle) removes every schema it injects.
"""

import re

from .mcp import mcp_state
from .settings import settings_set, settings_state


# The built-in roster, hand-curated: the official docs list the tools but not
# machine-readably, so this is a floor, not a census — anything else the
# transcripts mention gets a row of its own with no blurb. `core` marks the
# tools the whole app-shaped workflow stands on; the UI still offers the
# switch, behind a confirm.
BUILTIN_TOOLS = [
    ("Bash", True, "runs shell commands"),
    ("Read", True, "reads files"),
    ("Edit", True, "edits files in place"),
    ("Write", True, "creates and overwrites files"),
    ("Glob", True, "finds files by name pattern"),
    ("Grep", True, "searches file contents"),
    ("Task", False, "spawns subagents — your agents stop working without it"),
    ("Skill", False, "invokes skills — skills stop loading without it"),
    ("SlashCommand", False, "lets the model run your /commands mid-task"),
    ("TodoWrite", False, "keeps the visible task checklist"),
    ("WebSearch", False, "searches the web (billed per search)"),
    ("WebFetch", False, "fetches and reads a URL"),
    ("NotebookEdit", False, "edits Jupyter notebook cells"),
    ("AskUserQuestion", False, "asks multiple-choice questions mid-task"),
    ("ExitPlanMode", False, "ends plan mode — plan mode needs it"),
    ("BashOutput", False, "reads background shell output"),
    ("KillShell", False, "kills a background shell"),
]

# Tool names as they appear in tool_use blocks: built-ins are CamelCase words,
# MCP tools mcp__server__tool. No parens, so an argument-filter rule can never
# pass as a name; bounded, so a hostile transcript can't grow the deny list.
TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")

MCP_PREFIX = "mcp__"


def _bare_denies(data):
    """Deny entries that name a whole tool (no argument filter)."""
    deny = (data.get("permissions") or {}).get("deny")
    if not isinstance(deny, list):
        return set()
    return {r.strip() for r in deny
            if isinstance(r, str) and r.strip() and "(" not in r}


def tools_report(by_tool):
    """The Insight tab's tools section: built-ins and MCP servers, each with
    measured uses and its current on/off state. `by_tool` is insight.py's
    "tool" histogram — {name: {count, last}} — so the one transcript scan
    both tabs share is not repeated here."""
    by_tool = by_tool or {}
    st = settings_state()
    denied = _bare_denies(st["data"])
    builtin = []
    for name, core, blurb in BUILTIN_TOOLS:
        rec = by_tool.get(name) or {}
        builtin.append({"name": name, "core": core, "blurb": blurb,
                        "count": rec.get("count", 0),
                        "last": rec.get("last", ""),
                        "denied": name in denied})
    listed = {r["name"] for r in builtin}
    for name, rec in by_tool.items():
        if name in listed or name.startswith(MCP_PREFIX):
            continue
        builtin.append({"name": name, "core": False, "blurb": "",
                        "count": rec["count"], "last": rec["last"],
                        "denied": name in denied})
    builtin.sort(key=lambda r: (-r["count"], r["name"]))

    # mcp__server__tool -> per-server usage, joined against the inventory.
    # A server the transcripts mention but ~/.claude.json doesn't know is
    # kept (scope "other"): it is a project-scope or since-removed server,
    # and hiding it would misreport the histogram.
    per = {}
    for name, rec in by_tool.items():
        if not name.startswith(MCP_PREFIX):
            continue
        server, _, tool = name[len(MCP_PREFIX):].partition("__")
        s = per.setdefault(server or "?", {"count": 0, "last": "", "tools": {}})
        s["count"] += rec["count"]
        s["last"] = max(s["last"], rec["last"])
        s["tools"][tool or "?"] = s["tools"].get(tool or "?", 0) + rec["count"]
    mcp = []
    for row in mcp_state()["servers"]:
        u = per.pop(row["name"], None) or {"count": 0, "last": "", "tools": {}}
        mcp.append({"name": row["name"], "scope": "user",
                    "enabled": row["enabled"], "count": u["count"],
                    "last": u["last"],
                    "tools": sorted(u["tools"].items(),
                                    key=lambda kv: (-kv[1], kv[0]))})
    for name, u in per.items():
        mcp.append({"name": name, "scope": "other", "enabled": None,
                    "count": u["count"], "last": u["last"],
                    "tools": sorted(u["tools"].items(),
                                    key=lambda kv: (-kv[1], kv[0]))})
    mcp.sort(key=lambda s: (s["scope"] != "user", -s["count"], s["name"]))
    return {"builtin": builtin, "mcp": mcp, "deny": sorted(denied),
            "settings_error": st["error"]}


def tool_set_enabled(name, enabled):
    """Add (enabled=False) or remove (enabled=True) a bare `permissions.deny`
    entry for one tool. Argument-filter rules — anything with a `(` — are
    someone's deliberate policy and are never touched here."""
    name = (name or "").strip()
    if not TOOL_NAME_RE.match(name):
        raise ValueError("bad tool name")
    st = settings_state()
    if st["error"]:
        raise ValueError(f"settings.json: {st['error']} — fix it by hand first")
    deny = (st["data"].get("permissions") or {}).get("deny", [])
    if not isinstance(deny, list):
        raise ValueError("settings.json: permissions.deny is not a list — "
                         "fix it by hand first")
    if enabled:
        new = [r for r in deny
               if not (isinstance(r, str) and r.strip() == name)]
        if len(new) == len(deny):
            return  # not denied: nothing to remove, don't churn the file
    else:
        if name in _bare_denies(st["data"]):
            return  # already off
        new = deny + [name]
    # an emptied list deletes the key — settings_set prunes an empty
    # `permissions` parent too, leaving the file as if never touched
    settings_set("permissions.deny", new or None)
