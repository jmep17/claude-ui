"""Transcript analytics: context budget, usage, Bash prefixes, cost stats."""

from pathlib import Path
import json
import re
import time

from .core import ITEM_TYPES, config_dir, read_cfg
from .items import scan_items


def _tok(s):
    return (len(s) + 3) // 4 if s else 0

def insight_budget():
    claude_md = config_dir() / "CLAUDE.md"
    md_tok = _tok(claude_md.read_text(errors="replace")) if claude_md.is_file() else 0
    per_type = {}
    for t in ITEM_TYPES:
        items = scan_items(t)
        rows = [{"name": it["name"],
                 "tokens": _tok(it["name"]) + _tok(it.get("description", ""))}
                for it in items if it["enabled"] and not it.get("broken")]
        rows.sort(key=lambda r: -r["tokens"])
        per_type[t] = {"tokens": sum(r["tokens"] for r in rows), "items": rows}
    return {"claude_md": md_tok, "types": per_type,
            "total": md_tok + sum(v["tokens"] for v in per_type.values())}

USAGE_CACHE = Path.home() / ".cache" / "claude-ui-usage.json"

CACHE_V = 2

PROJECTS_DIR = Path.home() / ".claude" / "projects"

CMD_RE = re.compile(r"<command-name>/?([A-Za-z0-9:_.\/-]+)</command-name>")

MAX_TRANSCRIPT = 64 * 1024 * 1024

# commands whose first sub-word is part of the identity (git status vs git push)
BASH_MULTI = {"git", "npm", "npx", "yarn", "pnpm", "cargo", "docker", "kubectl",
              "python", "python3", "pip", "pip3", "uv", "make", "go", "bundle",
              "gh", "node", "poetry", "brew", "apt", "apt-get", "gcloud", "aws"}

def _bash_prefix(cmd):
    """'git diff --stat | head' -> 'git diff'; None if unclassifiable."""
    seg = re.split(r"[|;&\n]", (cmd or "").strip(), 1)[0].strip()
    toks = seg.split()
    while toks and (toks[0] in ("env", "sudo", "command")
                    or ("=" in toks[0] and not toks[0].startswith(("/", ".")))):
        toks.pop(0)
    if not toks:
        return None
    head = toks[0].rsplit("/", 1)[-1]
    if not re.match(r"^[A-Za-z0-9._-]+$", head):
        return None
    if head in BASH_MULTI and len(toks) > 1 and re.match(r"^[A-Za-z0-9._-]+$", toks[1]):
        return head + " " + toks[1]
    return head

def _scan_transcript(path):
    """One transcript -> {counts, days, models, bash, cwd}."""
    counts = {}   # "kind\tname" -> [count, last_iso_ts]
    days = {}     # "YYYY-MM-DD" -> {model: [in, out, cacheW, cacheR, msgs]}
    models = {}   # model -> [in, out, cacheW, cacheR, msgs]  (for per-project cost)
    bash = {}     # prefix -> count
    cwd = ""

    def bump(kind, name, ts):
        k = kind + "\t" + name
        c = counts.get(k)
        if c:
            c[0] += 1
            c[1] = max(c[1], ts)
        else:
            counts[k] = [1, ts]

    def texts(content):
        if isinstance(content, str):
            yield content
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "text":
                yield b.get("text") or ""

    try:
        with open(path, errors="replace") as f:
            for line in f:
                if ('"usage"' not in line and "command-name" not in line
                        and '"Skill"' not in line and '"Task"' not in line
                        and '"Bash"' not in line and '"cwd"' not in line):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = d.get("timestamp") or ""
                if not cwd and isinstance(d.get("cwd"), str):
                    cwd = d["cwd"]
                msg = d.get("message") or {}
                usage = msg.get("usage")
                if isinstance(usage, dict) and msg.get("model"):
                    day = ts[:10] or "unknown"
                    row = [int(usage.get("input_tokens") or 0),
                           int(usage.get("output_tokens") or 0),
                           int(usage.get("cache_creation_input_tokens") or 0),
                           int(usage.get("cache_read_input_tokens") or 0), 1]
                    for agg in (days.setdefault(day, {}), models):
                        slot = agg.setdefault(msg["model"], [0, 0, 0, 0, 0])
                        for i, v in enumerate(row):
                            slot[i] += v
                content = msg.get("content")
                for text in texts(content):
                    for m in CMD_RE.finditer(text):
                        bump("command", m.group(1).replace(":", "/"), ts)
                for b in content if isinstance(content, list) else []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    inp = b.get("input") or {}
                    if b.get("name") == "Skill" and inp.get("skill"):
                        bump("skill", str(inp["skill"]), ts)
                    elif b.get("name") == "Task" and inp.get("subagent_type"):
                        bump("agent", str(inp["subagent_type"]), ts)
                    elif b.get("name") == "Bash" and inp.get("command"):
                        p = _bash_prefix(str(inp["command"]))
                        if p:
                            bash[p] = bash.get(p, 0) + 1
    except OSError:
        return {"counts": {}, "days": {}, "models": {}, "bash": {}, "cwd": ""}
    return {"counts": counts, "days": days, "models": models,
            "bash": bash, "cwd": cwd}

def transcript_stats(rescan=False):
    """Aggregate usage/cost/bash data across all transcripts, incrementally
    cached by (mtime, size) per file so only new sessions are re-read."""
    cache = {}
    if not rescan and USAGE_CACHE.is_file():
        try:
            cache = json.loads(USAGE_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}
    if cache.get("v") != CACHE_V:
        cache = {}
    files = cache.get("files") or {}
    seen = set()
    scanned = 0
    if PROJECTS_DIR.is_dir():
        for p in PROJECTS_DIR.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > MAX_TRANSCRIPT:
                continue
            key = str(p)
            seen.add(key)
            sig = [int(st.st_mtime), st.st_size]
            if files.get(key, {}).get("sig") != sig:
                files[key] = {"sig": sig, "data": _scan_transcript(p)}
                scanned += 1
    for key in list(files):
        if key not in seen:
            del files[key]
    if scanned or set(files) != seen:
        try:
            USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            USAGE_CACHE.write_text(json.dumps({"v": CACHE_V, "files": files}))
        except OSError:
            pass
    by = {}
    days = {}
    bash = {}
    projects = {}
    for f in files.values():
        data = f.get("data") or {}
        for k, (n, ts) in (data.get("counts") or {}).items():
            kind, _, name = k.partition("\t")
            slot = by.setdefault(kind, {}).setdefault(name, {"count": 0, "last": ""})
            slot["count"] += n
            slot["last"] = max(slot["last"], ts)
        for day, mrows in (data.get("days") or {}).items():
            dslot = days.setdefault(day, {})
            for model, row in mrows.items():
                s = dslot.setdefault(model, [0, 0, 0, 0, 0])
                for i, v in enumerate(row):
                    s[i] += v
        for prefix, n in (data.get("bash") or {}).items():
            bash[prefix] = bash.get(prefix, 0) + n
        cwd = data.get("cwd") or "(unknown)"
        pslot = projects.setdefault(cwd, {})
        for model, row in (data.get("models") or {}).items():
            s = pslot.setdefault(model, [0, 0, 0, 0, 0])
            for i, v in enumerate(row):
                s[i] += v
    return {"sessions": len(files), "scanned_now": scanned, "by": by,
            "days": days, "bash": bash, "projects": projects,
            "dir": str(PROJECTS_DIR).replace(str(Path.home()), "~", 1),
            "available": PROJECTS_DIR.is_dir()}

def usage_stats(rescan=False):
    return transcript_stats(rescan)

PRICING = [
    ("fable", 10, 50), ("mythos", 10, 50),
    ("opus-4-1", 15, 75), ("opus-4-0", 15, 75), ("3-opus", 15, 75),
    ("opus", 5, 25),
    ("3-5-haiku", 0.8, 4), ("3-haiku", 0.25, 1.25),
    ("haiku", 1, 5),
    ("sonnet", 3, 15),
]

def model_price(model):
    m = (model or "").lower()
    overrides = read_cfg().get("pricing")
    if isinstance(overrides, dict):
        for sub, v in overrides.items():
            if (isinstance(v, list) and len(v) == 2 and sub.lower() in m):
                return float(v[0]), float(v[1]), True
    for sub, pin, pout in PRICING:
        if sub in m:
            return pin, pout, True
    return 5, 25, False  # unknown model: opus-tier guess, flagged in the UI

def _row_cost(row, pin, pout):
    i, o, cw, cr = row[0], row[1], row[2], row[3]
    return (i * pin + o * pout + cw * pin * 1.25 + cr * pin * 0.1) / 1e6

# The dashboard prices Claude models only. Anything else served through the same
# CLI — local models via ollama/proxies, the "<synthetic>" placeholder — is
# dropped entirely (no row, no tokens, no total). A `pricing` override opts a
# non-Claude id back in, since setting a price signals intent to count it.
def _excluded(model):
    m = (model or "").lower()
    if "claude" in m:
        return False
    overrides = read_cfg().get("pricing")
    if isinstance(overrides, dict):
        return not any(str(sub).lower() in m for sub in overrides)
    return True

def cost_stats(rescan=False):
    st = transcript_stats(rescan)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    d7 = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))
    d30 = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
    month = today[:8] + "01"
    per_day = []
    by_model = {}
    totals = {"today": 0, "last7": 0, "last30": 0, "month": 0, "all": 0}
    cache_savings = 0.0
    unknown = set()
    for day in sorted(st["days"]):
        drow = {"day": day, "cost": 0, "by": {}}
        for model, row in st["days"][day].items():
            if _excluded(model):
                continue
            pin, pout, known = model_price(model)
            if not known:
                unknown.add(model)
            c = _row_cost(row, pin, pout)
            drow["cost"] += c
            drow["by"][model] = round(c, 4)
            m = by_model.setdefault(model, {"cost": 0, "in": 0, "out": 0,
                                            "cacheR": 0, "cacheW": 0, "msgs": 0})
            m["cost"] += c
            m["in"] += row[0]
            m["out"] += row[1]
            m["cacheW"] += row[2]
            m["cacheR"] += row[3]
            m["msgs"] += row[4]
            cache_savings += row[3] * pin * 0.9 / 1e6
            totals["all"] += c
            if day == today:
                totals["today"] += c
            if day >= d7:
                totals["last7"] += c
            if day >= d30:
                totals["last30"] += c
            if day >= month:
                totals["month"] += c
        drow["cost"] = round(drow["cost"], 4)
        per_day.append(drow)
    by_project = []
    for cwd, mrows in st["projects"].items():
        mrows = {m: row for m, row in mrows.items() if not _excluded(m)}
        c = sum(_row_cost(row, *model_price(m)[:2]) for m, row in mrows.items())
        msgs = sum(row[4] for row in mrows.values())
        if msgs:
            by_project.append({"cwd": cwd, "cost": round(c, 4), "msgs": msgs})
    by_project.sort(key=lambda p: -p["cost"])
    return {"days": per_day[-30:], "totals": {k: round(v, 2) for k, v in totals.items()},
            "by_model": [{"model": m, **{k: (round(v, 2) if k == "cost" else v)
                                         for k, v in d.items()}}
                         for m, d in sorted(by_model.items(),
                                            key=lambda kv: -kv[1]["cost"])],
            "by_project": by_project[:12],
            "cache_savings": round(cache_savings, 2),
            "unknown_models": sorted(unknown),
            "sessions": st["sessions"], "dir": st["dir"],
            "available": st["available"]}
