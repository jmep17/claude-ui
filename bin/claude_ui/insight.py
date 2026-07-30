"""Transcript analytics: context budget, usage, Bash prefixes, cost stats."""

from pathlib import Path
import calendar
import datetime
import json
import re
import time

from .core import ITEM_TYPES, config_dir, read_cfg, tilde
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

# Bump whenever the shape or meaning of the cached per-file data changes, so
# stale entries are re-scanned instead of being mixed with the current format.
CACHE_V = 3

def projects_dir():
    """Transcripts live under the resolved config dir, not always ~/.claude."""
    return config_dir() / "projects"

CMD_RE = re.compile(r"<command-name>/?([A-Za-z0-9:_.\/-]+)</command-name>")

# Claude Code writes UTC timestamps ("2026-07-30T11:25:27.932Z"). Parsed by hand
# rather than with fromisoformat: that only accepts a "Z" suffix on 3.11+ and is
# picky about how many fractional-second digits it gets.
TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
                   r"(?:\.\d+)?\s*(Z|z|[+-]\d{2}:?\d{2})?$")

def _local_day(ts):
    """ISO timestamp -> local 'YYYY-MM-DD' ('' when unparseable).

    A day has to mean the user's local day: that's what "today" means to them,
    and what the statusline's cost fields report (it passes the local zone to
    ccusage), so the two features agree on where a day starts.
    """
    m = TS_RE.match((ts or "").strip())
    if not m:
        return ""
    y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
    epoch = calendar.timegm((y, mo, d, h, mi, s, 0, 1, 0))
    off = m.group(7)
    if off and off not in ("Z", "z"):
        digits = off[1:].replace(":", "")
        delta = int(digits[:2]) * 3600 + int(digits[2:]) * 60
        epoch += delta if off[0] == "-" else -delta
    return time.strftime("%Y-%m-%d", time.localtime(epoch))

def _tz_fingerprint():
    """Cached day buckets are local dates, so a machine timezone change makes
    them wrong — fold the zone into the cache validity check."""
    return "|".join(time.tzname) + "|" + str(time.timezone)

# Token counts are accumulated per (day, model) into one row of this shape.
ROW_ZERO = [0, 0, 0, 0, 0, 0]
R_IN, R_OUT, R_CW5M, R_CW1H, R_CR, R_MSGS = range(6)

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
    """One transcript -> {counts, days, bash, cwd}."""
    counts = {}   # "kind\tname" -> [count, last_iso_ts]
    days = {}     # local "YYYY-MM-DD" -> {model: ROW}
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
                    day = _local_day(ts) or "unknown"
                    # cache_creation splits the write total by TTL, which matters
                    # because a 1-hour write costs 2x base and a 5-minute one
                    # 1.25x. Transcripts predating the split had only 5m writes.
                    cw = int(usage.get("cache_creation_input_tokens") or 0)
                    cc = usage.get("cache_creation")
                    w1h = int((cc.get("ephemeral_1h_input_tokens") or 0)
                              if isinstance(cc, dict) else 0)
                    w1h = max(0, min(w1h, cw))
                    row = [int(usage.get("input_tokens") or 0),
                           int(usage.get("output_tokens") or 0),
                           cw - w1h, w1h,
                           int(usage.get("cache_read_input_tokens") or 0), 1]
                    slot = days.setdefault(day, {}).setdefault(msg["model"], ROW_ZERO[:])
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
        return {"counts": {}, "days": {}, "bash": {}, "cwd": ""}
    return {"counts": counts, "days": days, "bash": bash, "cwd": cwd}

def transcript_stats(rescan=False):
    """Aggregate usage/cost/bash data across all transcripts, incrementally
    cached by (mtime, size) per file so only new sessions are re-read."""
    cache = {}
    if not rescan and USAGE_CACHE.is_file():
        try:
            cache = json.loads(USAGE_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}
    tz = _tz_fingerprint()
    if cache.get("v") != CACHE_V or cache.get("tz") != tz:
        cache = {}
    files = cache.get("files") or {}
    seen = set()
    scanned = 0
    pdir = projects_dir()
    if pdir.is_dir():
        for p in pdir.rglob("*.jsonl"):
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
            USAGE_CACHE.write_text(
                json.dumps({"v": CACHE_V, "tz": tz, "files": files}))
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
        # A transcript belongs to one project, so its day rows feed both the
        # global daily series and that project's own — no second aggregate to
        # keep in sync, and per-project cost can be priced per day too.
        cwd = data.get("cwd") or "(unknown)"
        pdays = projects.setdefault(cwd, {})
        for day, mrows in (data.get("days") or {}).items():
            dslot = days.setdefault(day, {})
            pslot = pdays.setdefault(day, {})
            for model, row in mrows.items():
                for agg in (dslot, pslot):
                    s = agg.setdefault(model, ROW_ZERO[:])
                    for i, v in enumerate(row):
                        s[i] += v
        for prefix, n in (data.get("bash") or {}).items():
            bash[prefix] = bash.get(prefix, 0) + n
    return {"sessions": len(files), "scanned_now": scanned, "by": by,
            "days": days, "bash": bash, "projects": projects,
            "dir": tilde(pdir), "available": pdir.is_dir()}

def usage_stats(rescan=False):
    return transcript_stats(rescan)

# USD per million tokens, (model-id substring, input, output[, last day the rate
# applied]). First match wins, so put narrower substrings first. A dated entry
# falls through to the next match once it expires, which is how introductory
# rates work — Sonnet 5 launched at $2/$10 and lists at $3/$15 from Sep 2026.
# Checked against published pricing on 2026-07-30.
PRICING = [
    ("fable", 10, 50), ("mythos", 10, 50),
    ("opus-4-1", 15, 75), ("opus-4-0", 15, 75), ("3-opus", 15, 75),
    ("opus", 5, 25),
    ("3-5-haiku", 0.8, 4), ("3-haiku", 0.25, 1.25),
    ("haiku", 1, 5),
    ("sonnet-5", 2, 10, "2026-08-31"),
    ("sonnet", 3, 15),
]

def model_price(model, day):
    """(input, output, known) $/Mtok for a model on a given local day."""
    m = (model or "").lower()
    overrides = read_cfg().get("pricing")
    if isinstance(overrides, dict):
        for sub, v in overrides.items():
            if (isinstance(v, list) and len(v) == 2 and sub.lower() in m):
                return float(v[0]), float(v[1]), True
    for entry in PRICING:
        sub, pin, pout = entry[0], entry[1], entry[2]
        until = entry[3] if len(entry) > 3 else None
        # ISO dates compare correctly as strings; an unparseable day sorts past
        # every window, so it prices at the current rate rather than an old one.
        if sub in m and (until is None or day <= until):
            return pin, pout, True
    return 5, 25, False  # unknown model: opus-tier guess, flagged in the UI

# Cache writes are billed above the base input rate (2x for the 1-hour TTL, 1.25x
# for the 5-minute one) and cache reads at 0.1x.
def _row_cost(row, pin, pout):
    return (row[R_IN] * pin + row[R_OUT] * pout
            + row[R_CW5M] * pin * 1.25 + row[R_CW1H] * pin * 2
            + row[R_CR] * pin * 0.1) / 1e6

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
    # Local days, matching how the rows were bucketed and what the statusline
    # reports. Date arithmetic rather than subtracting seconds, so a DST switch
    # inside the window can't shift the boundary onto the wrong day. The windows
    # count today plus the N-1 before it, so "last 7 days" really is 7.
    d = datetime.date.today()
    today = d.isoformat()
    d7 = (d - datetime.timedelta(days=6)).isoformat()
    d30 = (d - datetime.timedelta(days=29)).isoformat()
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
            pin, pout, known = model_price(model, day)
            if not known:
                unknown.add(model)
            c = _row_cost(row, pin, pout)
            drow["cost"] += c
            drow["by"][model] = round(c, 4)
            m = by_model.setdefault(model, {"cost": 0, "in": 0, "out": 0,
                                            "cacheR": 0, "cacheW": 0, "msgs": 0})
            m["cost"] += c
            m["in"] += row[R_IN]
            m["out"] += row[R_OUT]
            m["cacheW"] += row[R_CW5M] + row[R_CW1H]
            m["cacheR"] += row[R_CR]
            m["msgs"] += row[R_MSGS]
            cache_savings += row[R_CR] * pin * 0.9 / 1e6
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
    for cwd, drows in st["projects"].items():
        c = 0.0
        msgs = 0
        for day, mrows in drows.items():
            for model, row in mrows.items():
                if _excluded(model):
                    continue
                c += _row_cost(row, *model_price(model, day)[:2])
                msgs += row[R_MSGS]
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
