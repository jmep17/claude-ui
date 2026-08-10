"""The Context tab: what loads into every session, and what it measurably cost.

Two halves, deliberately different in kind. The *inventory* half walks the
config dir and every registered project and estimates what Claude Code loads
at session start — CLAUDE.md files (with their @-imports resolved), the item
listings, MCP server configs, auto-memory. Estimates are chars/4 and say so;
there is no tokenizer here and never will be (a recorded rejected finding —
the split is a relative weight, and a real tokenizer would improve nothing
that is reported). The *measured* half reads what the API actually billed,
via insight.py's transcript cache: each session's first usage-bearing message
approximates the context present before anyone typed, and its running max of
cache reads the peak. The two halves meet in `pointers`, a short list of
if-statements naming the cheapest things to shrink.
"""

from pathlib import Path
import json
import re
import statistics

from .core import (ITEM_TYPES, config_dir, project_roots, read_cfg, tilde)
from .insight import (R_CR, _excluded, _split_rate_key, model_price,
                      projects_dir, transcript_stats)
from .items import scan_items
from .mcp import mcp_state
from .plugins import PLUGIN_TYPES, plugins_state
from .projects import project_mcp_state


def _tok(s):
    return (len(s) + 3) // 4 if s else 0

# ------------------------------------------------------------- @-imports

# An import is a whitespace-delimited @path token — line start or after
# whitespace, so scoped npm names in prose ("run @foo/bar") still match, which
# is faithful: Claude Code applies the same blunt rule and would try them too.
IMPORT_RE = re.compile(r"(?:^|(?<=\s))@([^\s@]+)")
IMPORT_DEPTH = 5   # Claude Code documents a max of 5 import hops

def _import_refs(text):
    """@-references in reading order, fenced code blocks skipped."""
    out = []
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        for m in IMPORT_RE.finditer(line):
            out.append(m.group(1).rstrip(".,;:!?)\"'"))
    return [r for r in out if r]

def _imports(path, text, seen, depth=0):
    """Flattened import rows for one file's text, recursion included.

    `seen` holds resolved absolute paths already counted (starting with the
    root file itself), so a file imported twice — or a cycle — is counted
    once. A reference that doesn't resolve to a file still gets a row with
    resolved=False: Claude Code would fail the same way, and an import that
    silently vanishes is exactly what this tab exists to surface.
    """
    if depth >= IMPORT_DEPTH:
        return []
    rows = []
    for ref in _import_refs(text):
        p = Path(ref).expanduser()
        if not p.is_absolute():
            p = path.parent / p
        try:
            p = p.resolve()
        except OSError:
            pass
        if str(p) in seen:
            continue
        seen.add(str(p))
        if p.is_file():
            try:
                sub = p.read_text(errors="replace")
            except OSError:
                sub = ""
            rows.append({"ref": "@" + ref, "path": str(p), "resolved": True,
                         "chars": len(sub), "tok": _tok(sub)})
            rows += _imports(p, sub, seen, depth + 1)
        else:
            rows.append({"ref": "@" + ref, "path": str(p), "resolved": False,
                         "chars": 0, "tok": 0})
    return rows

def _md_entry(path):
    """One CLAUDE.md-shaped row: the file plus everything it pulls in."""
    exists = path.is_file()
    text = ""
    if exists:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            text = ""
    imports = _imports(path, text, {str(path)}) if text else []
    tok = _tok(text)
    return {"name": path.name, "path": str(path), "tilde": tilde(path),
            "exists": exists, "chars": len(text), "tok": tok,
            "imports": imports,
            "total_tok": tok + sum(i["tok"] for i in imports)}

# ------------------------------------------------------------- inventory

def _listing_rows(items):
    """What each item contributes to the startup listing: name + description.
    Disabled and broken items ride along at zero so the UI can show them."""
    rows = []
    for it in items:
        counted = it["enabled"] and not it.get("broken")
        rows.append({
            "name": it["name"], "enabled": it["enabled"],
            "path": it.get("path", ""),
            "listing_tok": (_tok(it["name"]) + _tok(it.get("description", "")))
                           if counted else 0,
            "desc_chars": len(it.get("description", "")),
            "file_chars": it.get("chars", 0),
            "long_desc": bool(it.get("long_desc"))})
    rows.sort(key=lambda r: -r["listing_tok"])
    return rows

def _type_block(items):
    rows = _listing_rows(items)
    return {"listing_tok": sum(r["listing_tok"] for r in rows), "items": rows}

def _config_chars(cfg):
    try:
        return len(json.dumps(cfg))
    except (TypeError, ValueError):
        return 0

def _user_scope():
    types = {t: _type_block(scan_items(t)) for t in ITEM_TYPES}
    # enabled plugins load their components alongside yours — same fold the
    # old Insight budget made, kept so plugin weight stays visible
    comps = [{"name": f"{p['name']}:{c['name']}", "enabled": True,
              "description": c.get("description", "")}
             for p in plugins_state()["plugins"] if p["enabled"]
             for c in p["components"] if c["kind"] in PLUGIN_TYPES]
    types["plugins"] = _type_block(comps)
    st = mcp_state()
    servers = [{"name": s["name"], "enabled": s["enabled"],
                "transport": ("stdio" if (s["config"] or {}).get("command")
                              else str((s["config"] or {}).get("type") or "http")),
                "config_chars": _config_chars(s["config"])}
               for s in st["servers"]]
    md = [_md_entry(config_dir() / "CLAUDE.md")]
    return {"scope": "user", "root": None, "tilde": tilde(config_dir()),
            "claude_md": md, "types": types,
            "mcp": {"count": len(servers), "servers": servers},
            "memory": None,
            "est_tok": sum(m["total_tok"] for m in md)
                       + sum(b["listing_tok"] for b in types.values())}

def _memory_block(root, slug_map):
    """The auto-memory dir for a project: MEMORY.md is loaded every session,
    topic files only on demand — so only MEMORY.md joins the estimate."""
    slug = slug_map.get(str(root)) or re.sub(r"[^A-Za-z0-9]", "-", str(root))
    mdir = projects_dir() / slug / "memory"
    if not mdir.is_dir():
        return None
    index = mdir / "MEMORY.md"
    try:
        text = index.read_text(errors="replace") if index.is_file() else ""
    except OSError:
        text = ""
    topics = []
    for p in sorted(mdir.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            topics.append({"name": p.name, "chars": p.stat().st_size})
        except OSError:
            continue
    return {"dir": str(mdir), "tilde": tilde(mdir),
            "memory_chars": len(text), "memory_tok": _tok(text),
            "topics": topics,
            "topics_chars": sum(t["chars"] for t in topics)}

def _project_scope(root, slug_map):
    cdir = root / ".claude"
    types = {}
    for t in ITEM_TYPES:
        try:
            types[t] = _type_block(scan_items(t, scope=cdir))
        except OSError:
            types[t] = _type_block([])
    try:
        pm = project_mcp_state(str(root))
        servers = [{"name": s["name"], "approval": s["approval"],
                    "config_chars": _config_chars(s["config"])}
                   for s in pm["servers"]]
    except (ValueError, OSError):
        servers = []
    md = [_md_entry(root / n) for n in ("CLAUDE.md", "CLAUDE.local.md")]
    memory = _memory_block(root, slug_map)
    return {"scope": "project", "root": str(root), "tilde": tilde(root),
            "missing": not root.is_dir(),
            "claude_md": md, "types": types,
            "mcp": {"count": len(servers), "servers": servers},
            "memory": memory,
            "est_tok": sum(m["total_tok"] for m in md)
                       + sum(b["listing_tok"] for b in types.values())
                       + (memory["memory_tok"] if memory else 0)}

# ------------------------------------------------------------- measured

SESSION_ROWS_PER_PROJECT = 15

def _is_subagent(path):
    return "subagents" in Path(path).parts

def _slug_map(session_rows):
    """cwd -> transcript slug, from the transcripts themselves — ground truth
    over guessing, since the slug is a lossy flattening of the path."""
    pdir = projects_dir()
    out = {}
    for r in session_rows:
        if not r["cwd"] or _is_subagent(r["path"]):
            continue
        try:
            rel = Path(r["path"]).relative_to(pdir)
        except ValueError:
            continue
        if len(rel.parts) >= 2:
            out.setdefault(r["cwd"], rel.parts[0])
    return out

def _cache_read_cost(pdays, overrides):
    """(cache-read tokens, USD) for one project's day rows — the read side
    only, because that is the recurring price of context and the number this
    tab exists to attribute."""
    toks = 0
    usd = 0.0
    for day, rates in pdays.items():
        for rkey, row in rates.items():
            model, mult = _split_rate_key(rkey)
            if _excluded(model, overrides):
                continue
            # the raw day key: "unknown" sorts past every dated rate window,
            # which prices it at the current rate — same rule as cost_stats
            pin, _, _ = model_price(model, day, overrides)
            toks += row[R_CR]
            usd += row[R_CR] * pin * 0.1 * mult / 1e6
    return toks, usd

def _measured(st, roots):
    overrides = read_cfg().get("pricing")
    by_cwd = {}
    for r in st.get("session_rows") or []:
        cwd = r["cwd"] or "(unknown)"
        slot = by_cwd.setdefault(cwd, {"main": [], "subagents": 0})
        if _is_subagent(r["path"]):
            slot["subagents"] += 1
        elif r["sess"]:
            slot["main"].append(r)
    projects = []
    sessions = {}
    for cwd, slot in by_cwd.items():
        main = slot["main"]
        if not main and not slot["subagents"]:
            continue
        bases = [sum(r["sess"]["first"]) for r in main]
        peaks = [r["sess"]["max_cr"] for r in main]
        toks, usd = _cache_read_cost(st["projects"].get(cwd, {}), overrides)
        p = Path(cwd)
        projects.append({
            "cwd": cwd, "tilde": tilde(cwd),
            "registered": any(r == p or r in p.parents for r in roots),
            "sessions": len(main), "subagents": slot["subagents"],
            "base_med": int(statistics.median(bases)) if bases else 0,
            "base_min": min(bases) if bases else 0,
            "base_max": max(bases) if bases else 0,
            "peak_max": max(peaks) if peaks else 0,
            "cache_read_tok": toks, "cache_spend": usd,
            "last_ts": max((r["sess"]["last_ts"] for r in main), default="")})
        main.sort(key=lambda r: r["sess"]["last_ts"], reverse=True)
        sessions[cwd] = [
            {"id": Path(r["path"]).stem[:8],
             "first_ts": r["sess"]["first_ts"], "last_ts": r["sess"]["last_ts"],
             "msgs": r["msgs"], "model": r["sess"]["model"],
             "baseline": sum(r["sess"]["first"]), "peak": r["sess"]["max_cr"]}
            for r in main[:SESSION_ROWS_PER_PROJECT]]
    projects.sort(key=lambda p: -p["cache_spend"])
    return {"available": st["available"], "dir": st["dir"],
            "sessions_total": st["sessions"], "scanned_now": st["scanned_now"],
            "projects": projects, "sessions": sessions}

# ------------------------------------------------------------- pointers

# Deliberately a flat list of if-statements over the two halves, not a rules
# engine: each threshold is one line to read and one line to argue with.
MD_WARN_TOK = 4000        # ~16k chars of CLAUDE.md, paid at every session start
MCP_INFO_COUNT = 3        # user-scope servers load their schemas everywhere
MEMORY_INFO_TOK = 2000    # MEMORY.md rides along every session in that project
BASE_OVER_FLOOR_TOK = 8000  # baseline excess over the leanest project's median
PEAK_INFO_TOK = 150000    # compaction territory
PEAK_PROJECT_CAP = 5      # peak-growth notes: top spenders only
LONG_DESC_CAP = 5         # don't drown the list in description nags

def _pointers(scopes, measured):
    finds = []

    def add(level, area, msg, target=None):
        f = {"level": level, "area": area, "msg": msg}
        if target:
            f["target"] = target
        finds.append(f)

    for sc in scopes:
        where = "user scope" if sc["scope"] == "user" else sc["tilde"]
        for md in sc["claude_md"]:
            if md["total_tok"] > MD_WARN_TOK:
                add("warn", "claude-md",
                    f"{md['tilde']} is ~{md['total_tok'] // 1000}k tokens, "
                    "loaded at the start of every session here",
                    {"kind": "path", "path": md["path"]})
            for imp in md["imports"]:
                if not imp["resolved"]:
                    add("warn", "claude-md",
                        f"{md['tilde']} imports {imp['ref']}, which doesn't "
                        "resolve to a file — it loads nothing",
                        {"kind": "path", "path": md["path"]})
        long_rows = [(t, r) for t, b in sc["types"].items()
                     for r in b["items"] if r["enabled"] and r["long_desc"]]
        for t, r in long_rows[:LONG_DESC_CAP]:
            add("info", t,
                f"{r['name']} has a {r['desc_chars']}-char description; "
                "every session pays for it in the listing "
                f"({where})",
                {"kind": "path", "path": str(Path(r["path"]).expanduser())}
                if r.get("path") else None)
        if len(long_rows) > LONG_DESC_CAP:
            add("info", "items",
                f"…and {len(long_rows) - LONG_DESC_CAP} more over-length "
                f"descriptions in {where}")
        if sc["scope"] == "user":
            enabled = sum(1 for s in sc["mcp"]["servers"] if s.get("enabled"))
            if enabled > MCP_INFO_COUNT:
                add("info", "mcp",
                    f"{enabled} user-scope MCP servers are enabled — every "
                    "session in every project loads their tool schemas",
                    {"kind": "tab", "tab": "mcp"})
        mem = sc.get("memory")
        if mem and mem["memory_tok"] > MEMORY_INFO_TOK:
            add("info", "memory",
                f"MEMORY.md for {sc['tilde']} is ~{mem['memory_tok'] // 1000}k "
                "tokens and rides along in every session there",
                {"kind": "path", "path": str(Path(mem["dir"]) / "MEMORY.md")})

    # Every session everywhere starts with the harness's own ~25-30k system
    # prompt, which no config change removes — so an absolute threshold would
    # warn about every project and say nothing. Compare each project against
    # the leanest one instead: the excess is the part this tab can shrink.
    med_rows = [p for p in measured["projects"] if p["sessions"] >= 3]
    floor = min((p["base_med"] for p in med_rows), default=0)
    for p in med_rows:
        over = p["base_med"] - floor
        if over > BASE_OVER_FLOOR_TOK:
            add("warn", "sessions",
                f"sessions in {p['tilde']} start at ~{p['base_med'] // 1000}k "
                f"tokens (median) — ~{over // 1000}k more than your leanest "
                "project")
    for p in measured["projects"][:PEAK_PROJECT_CAP]:
        grown = sum(1 for s in measured["sessions"].get(p["cwd"], [])
                    if s["peak"] > PEAK_INFO_TOK)
        if grown:
            add("info", "sessions",
                f"{grown} recent session{'s' if grown != 1 else ''} in "
                f"{p['tilde']} grew past ~{PEAK_INFO_TOK // 1000}k tokens — "
                "long sessions multiply cache-read cost")
    order = {"warn": 0, "info": 1}
    finds.sort(key=lambda f: order.get(f["level"], 2))
    return finds

# ------------------------------------------------------------- entry point

def context_state(rescan=False):
    st = transcript_stats(rescan)
    slug_map = _slug_map(st.get("session_rows") or [])
    roots = project_roots()
    scopes = [_user_scope()] + [_project_scope(r, slug_map) for r in roots]
    measured = _measured(st, roots)
    return {"scopes": scopes, "measured": measured,
            "pointers": _pointers(scopes, measured)}
