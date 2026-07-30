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
CACHE_V = 5

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

def _msg_key(entry, msg):
    """Identity of the API response an entry belongs to, for de-duplication.

    One assistant message spans several transcript lines — one per content block
    — and every one of those lines repeats the whole message's `usage`. Summing
    lines therefore multiplies a message's tokens by its block count (~2.4x in
    practice). The same message can also appear in more than one file when a
    session is forked. Keyed on message id + request id, matching ccusage, so
    the two agree; entries too old to carry an id fall back to the line's own
    uuid, which counts them once each rather than dropping them.
    """
    mid = msg.get("id")
    if mid:
        return str(mid) + "\t" + str(entry.get("requestId") or "")
    return "uuid\t" + str(entry.get("uuid") or id(entry))

MAX_TRANSCRIPT = 64 * 1024 * 1024

# Sources a billed token can be blamed on. Tool results get one source each,
# named "tool:<Name>"; these three cover everything else.
SRC_SYS = "system prompt + tools"
SRC_PROMPT = "your prompts"
SRC_OUT = "model output"

# How many individual tool results each transcript contributes to the
# biggest-offenders table. Small, because this rides in the per-file cache.
BIG_PER_FILE = 8

# Fields worth putting in a tool's label, most identifying first.
LABEL_KEYS = ("file_path", "notebook_path", "command", "pattern", "url",
              "skill", "subagent_type", "query", "description")

def _blob(x):
    """Text of a tool_result payload: a string, or a list of content blocks."""
    if isinstance(x, str):
        return x
    if x is None:
        return ""
    try:
        return json.dumps(x, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(x)

def _tool_label(name, inp):
    """'Read' + {"file_path": "/a/b.py"} -> 'Read /a/b.py'.

    Bash collapses to its prefix, so every `git status` groups under one label
    instead of splitting on the flags it happened to carry.
    """
    if isinstance(inp, dict):
        for k in LABEL_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                if k == "command":
                    # `cd somewhere && real-command` is about the real command,
                    # so don't let every one of them label itself "cd".
                    v = re.sub(r"^(?:\s*cd\s+[^&;|]*&&\s*)+", "", v)
                    v = _bash_prefix(v) or v
                return (str(name) + " " + str(v).strip())[:90]
    return str(name)

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

def _blocks(content):
    """Content blocks of a message, whose `content` may be a bare string."""
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []

def _pend_user(content, pending, tools, big):
    """Queue a user turn's content as input the next request will pay for."""
    for b in _blocks(content):
        if b.get("type") == "tool_result":
            label = tools.get(b.get("tool_use_id")) or "(unmatched tool result)"
            n = _tok(_blob(b.get("content")))
            pending.append(("tool:" + label.split(" ", 1)[0], n))
            e = big.setdefault(label, [0, 0])
            e[0] += n
            e[1] += 1
        elif b.get("type") == "text":
            pending.append((SRC_PROMPT, _tok(b.get("text") or "")))

def _pend_assistant(content, pending, tools):
    """Queue an assistant block, and remember tool_use ids so the tool_result
    coming back can be attributed to the tool that asked for it."""
    for b in _blocks(content):
        t = b.get("type")
        if t == "tool_use":
            if b.get("id"):
                tools[b["id"]] = _tool_label(b.get("name"), b.get("input"))
            pending.append((SRC_OUT, _tok(_blob(b.get("input")))))
        elif t in ("text", "thinking"):
            pending.append((SRC_OUT, _tok(b.get(t) or "")))

def _blame_request(blame, day, model, usage, cw5m, cw1h, pending, run, state):
    """Split one request's billed tokens across the sources that caused them.

    Fresh input (uncached input + both cache writes) is exactly the content
    appended since the last request, so `pending`'s char shares divide it. Cache
    reads re-read the prefix already in context, so they divide by what that
    prefix is made of (`run`). Output is its own source. Every token lands
    somewhere, so the per-source costs sum to the same total as the per-day ones.
    """
    fresh = [(R_IN, int(usage.get("input_tokens") or 0)),
             (R_CW5M, cw5m), (R_CW1H, cw1h)]
    new = sum(n for _, n in fresh)
    est = sum(c for _, c in pending)

    cr = int(usage.get("cache_read_input_tokens") or 0)
    if cr:
        total = sum(run.values())
        if total:
            for src, v in run.items():
                blame(day, model, src, R_CR, cr * v / total)
        else:
            blame(day, model, SRC_SYS, R_CR, cr)

    # The system prompt and tool definitions are never written to the transcript,
    # so on a session's first request they are the whole unexplained residual —
    # which makes that residual a measurement of them rather than a guess.
    shares = []
    rest = 1.0
    if state["first"] and new > est and new:
        rest = est / new
        shares.append((SRC_SYS, 1.0 - rest))
    state["first"] = False
    if est:
        for src, c in pending:
            shares.append((src, rest * c / est))
    elif rest:
        shares.append((SRC_SYS, rest))

    for src, w in shares:
        for slot, n in fresh:
            blame(day, model, src, slot, n * w)
        run[src] = run.get(src, 0) + new * w
    blame(day, model, SRC_OUT, R_OUT, int(usage.get("output_tokens") or 0))

def _scan_transcript(path):
    """One transcript -> {counts, msgs, bash, cwd, attr, big, meta}."""
    counts = {}   # "kind\tname" -> [count, last_iso_ts]
    msgs = {}     # dedup key -> [local day, model, in, out, cacheW5m, cacheW1h, cacheR]
    bash = {}     # prefix -> count
    cwd = ""
    attr = {}     # "day\tmodel\tsource" -> row, every billed token blamed on one source
    big = {}      # tool label -> [tokens, results]
    meta = {"sid": "", "slug": "", "sidechain": False}

    # Attribution state. `pending` is the content appended since the last priced
    # request — that content is exactly what the next request pays fresh input
    # for, so its char shares divide those tokens. `run` is what the cached
    # prefix is made of, which is what a cache read re-reads.
    pending = []  # (source, chars)
    run = {}      # source -> tokens already in the cached prefix
    tools = {}    # tool_use_id -> label
    state = {"first": True}

    def blame(day, model, src, slot, n):
        row = attr.setdefault(day + "\t" + model + "\t" + src, [0.0] * 6)
        row[slot] += n

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
                        and '"Bash"' not in line and '"cwd"' not in line
                        and '"tool_result"' not in line):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = d.get("timestamp") or ""
                if not cwd and isinstance(d.get("cwd"), str):
                    cwd = d["cwd"]
                if not meta["sid"] and isinstance(d.get("sessionId"), str):
                    meta["sid"] = d["sessionId"]
                if not meta["slug"] and isinstance(d.get("slug"), str):
                    meta["slug"] = d["slug"]
                if d.get("isSidechain"):
                    meta["sidechain"] = True
                msg = d.get("message") or {}
                usage = msg.get("usage")
                if msg.get("role") == "user":
                    _pend_user(msg.get("content"), pending, tools, big)
                if isinstance(usage, dict) and msg.get("model"):
                    # cache_creation splits the write total by TTL, which matters
                    # because a 1-hour write costs 2x base and a 5-minute one
                    # 1.25x. Transcripts predating the split had only 5m writes.
                    cw = int(usage.get("cache_creation_input_tokens") or 0)
                    cc = usage.get("cache_creation")
                    w1h = int((cc.get("ephemeral_1h_input_tokens") or 0)
                              if isinstance(cc, dict) else 0)
                    w1h = max(0, min(w1h, cw))
                    # setdefault, not assignment: repeated lines for one message
                    # carry identical usage but drift by milliseconds, so keeping
                    # the first fixes the day bucket regardless of read order.
                    mk = _msg_key(d, msg)
                    priced = mk not in msgs
                    msgs.setdefault(mk, [
                        _local_day(ts) or "unknown", msg["model"],
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                        cw - w1h, w1h,
                        int(usage.get("cache_read_input_tokens") or 0)])
                    # Only the message's first line pays: the rest are the same
                    # response written out one content block at a time.
                    if priced:
                        _blame_request(blame, msgs[mk][0], msg["model"], usage,
                                       cw - w1h, w1h, pending, run, state)
                        pending = []
                content = msg.get("content")
                if msg.get("role") == "assistant":
                    # Whatever the model just produced is re-sent as input on the
                    # next request, so it joins `pending` — one block per line.
                    _pend_assistant(content, pending, tools)
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
        return {"counts": {}, "msgs": {}, "bash": {}, "cwd": "",
                "attr": {}, "big": {}, "meta": meta}
    top = sorted(big.items(), key=lambda kv: -kv[1][0])[:BIG_PER_FILE]
    return {"counts": counts, "msgs": msgs, "bash": bash, "cwd": cwd,
            "attr": {k: [round(v, 2) for v in row] for k, row in attr.items()},
            "big": dict(top), "meta": meta}

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
    attr = {}
    big = {}
    sessions = {}
    agents = {"main": {}, "sub": {}}
    seen_msgs = set()

    def acc(store, k, e):
        """Add one message's five token counts (and itself) to a row."""
        r = store.setdefault(k, ROW_ZERO[:])
        for i, v in enumerate(e[2:]):
            r[i] += v
        r[R_MSGS] += 1

    for key in sorted(files):   # sorted so a cross-file duplicate resolves the same way every run
        data = files[key].get("data") or {}
        for k, (n, ts) in (data.get("counts") or {}).items():
            kind, _, name = k.partition("\t")
            slot = by.setdefault(kind, {}).setdefault(name, {"count": 0, "last": ""})
            slot["count"] += n
            slot["last"] = max(slot["last"], ts)
        # A transcript belongs to one project, so each message feeds both the
        # global daily series and that project's own — one pass, no second
        # aggregate to keep in sync, and per-project cost priced per day too.
        cwd = data.get("cwd") or "(unknown)"
        pdays = projects.setdefault(cwd, {})
        meta = data.get("meta") or {}
        sub = bool(meta.get("sidechain"))
        # Subagent transcripts carry their parent's sessionId, so a session rolls
        # up its sidechains — which is what you want to see, with the sidechain
        # share broken out separately.
        sess = sessions.setdefault(meta.get("sid") or key, {
            "slug": meta.get("slug") or "", "cwd": "", "msgs": 0,
            "first": "", "last": "", "rows": {}, "sub_rows": {}})
        if data.get("cwd") and (not sess["cwd"] or not sub):
            sess["cwd"] = data["cwd"]
        mkeys = data.get("msgs") or {}
        # Counted before the loop below adds to seen_msgs: the share of this
        # file's messages it is the first to contribute.
        scale = (sum(k not in seen_msgs for k in mkeys) / len(mkeys)) if mkeys else 0.0
        for mkey, e in mkeys.items():
            if mkey in seen_msgs:   # same message already counted from another file
                continue
            seen_msgs.add(mkey)
            day, model = e[0], e[1]
            dm = day + "\t" + model
            acc(days.setdefault(day, {}), model, e)
            acc(pdays.setdefault(day, {}), model, e)
            acc(sess["sub_rows"] if sub else sess["rows"], dm, e)
            acc(agents["sub" if sub else "main"], dm, e)
            sess["msgs"] += 1
            sess["first"] = min(sess["first"] or day, day)
            sess["last"] = max(sess["last"], day)
        # Attribution is computed per file and cannot see the cross-file dedup
        # above, so a forked session would count its shared prefix twice. Scaling
        # each file's contribution by `scale` keeps the attribution total equal
        # to the priced total.
        if scale:
            for k, row in (data.get("attr") or {}).items():
                r = attr.setdefault(k, [0.0] * 6)
                for i, v in enumerate(row):
                    r[i] += v * scale
            for label, (n, c) in (data.get("big") or {}).items():
                e = big.setdefault(label, [0.0, 0])
                e[0] += n * scale
                e[1] += c
        for prefix, n in (data.get("bash") or {}).items():
            bash[prefix] = bash.get(prefix, 0) + n
    return {"sessions": len(sessions), "files": len(files),
            "scanned_now": scanned, "by": by, "days": days, "bash": bash,
            "projects": projects, "attr": attr, "big": big,
            "session_rows": sessions, "agents": agents,
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

# The five billed token types, as (label, row slot, multiplier on the model's
# input rate — None meaning the output rate instead). Ordered most to least
# expensive per token, which is also the order the UI shows them in.
TOKEN_TYPES = (("output", R_OUT, None), ("cache write 1h", R_CW1H, 2.0),
               ("cache write 5m", R_CW5M, 1.25), ("fresh input", R_IN, 1.0),
               ("cache read", R_CR, 0.1))

def _slot_cost(row, slot, mult, pin, pout):
    return row[slot] * (pout if mult is None else pin * mult) / 1e6

def _price_rows(rows):
    """Flat {"day\tmodel": row} -> (cost, tokens, msgs), skipping what we can't price."""
    cost = 0.0
    tokens = msgs = 0
    for k, row in rows.items():
        day, _, model = k.partition("\t")
        if _excluded(model):
            continue
        cost += _row_cost(row, *model_price(model, day)[:2])
        tokens += int(sum(row[:R_MSGS]))
        msgs += int(row[R_MSGS])
    return cost, tokens, msgs

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
    tcost = {t: 0.0 for t, _, _ in TOKEN_TYPES}
    ttok = {t: 0 for t, _, _ in TOKEN_TYPES}
    for day in sorted(st["days"]):
        drow = {"day": day, "cost": 0, "by": {}, "ct": {}}
        for model, row in st["days"][day].items():
            if _excluded(model):
                continue
            pin, pout, known = model_price(model, day)
            if not known:
                unknown.add(model)
            c = _row_cost(row, pin, pout)
            drow["cost"] += c
            drow["by"][model] = round(c, 4)
            # The same cost split by token type, globally and for this day's bar.
            for t, slot, mult in TOKEN_TYPES:
                tc = _slot_cost(row, slot, mult, pin, pout)
                tcost[t] += tc
                ttok[t] += int(row[slot])
                drow["ct"][t] = round(drow["ct"].get(t, 0) + tc, 4)
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

    # What put the tokens there. Same rows, same pricing, sliced by cause instead
    # of by model, so this sums to totals["all"] as well.
    src = {}
    for k, row in st["attr"].items():
        day, rest = k.split("\t", 1)
        model, _, source = rest.partition("\t")
        if _excluded(model):
            continue
        pin, pout = model_price(model, day)[:2]
        s = src.setdefault(source, {"cost": 0.0, "fresh": 0, "reread": 0, "out": 0})
        s["cost"] += _row_cost(row, pin, pout)
        s["fresh"] += int(row[R_IN] + row[R_CW5M] + row[R_CW1H])
        s["reread"] += int(row[R_CR])
        s["out"] += int(row[R_OUT])
    by_source = sorted(({"source": k, **v} for k, v in src.items()),
                       key=lambda s: -s["cost"])

    by_session = []
    for sid, s in st["session_rows"].items():
        cost, tokens, msgs = _price_rows(s["rows"])
        sub_cost, sub_tokens, sub_msgs = _price_rows(s["sub_rows"])
        if not (msgs or sub_msgs):
            continue
        by_session.append({"sid": sid, "slug": s["slug"], "cwd": s["cwd"],
                           "first": s["first"], "last": s["last"],
                           "cost": round(cost + sub_cost, 4),
                           "sub_cost": round(sub_cost, 4),
                           "tokens": tokens + sub_tokens, "msgs": msgs + sub_msgs})
    by_session.sort(key=lambda s: -s["cost"])

    agents = {}
    for which in ("main", "sub"):
        cost, tokens, msgs = _price_rows(st["agents"][which])
        agents[which] = {"cost": round(cost, 4), "tokens": tokens, "msgs": msgs}

    big_items = sorted(({"label": k, "tokens": int(n), "count": c}
                        for k, (n, c) in st["big"].items()),
                       key=lambda b: -b["tokens"])
    return {"days": per_day[-30:], "totals": {k: round(v, 2) for k, v in totals.items()},
            "by_type": [{"type": t, "tokens": ttok[t], "cost": round(tcost[t], 4)}
                        for t, _, _ in TOKEN_TYPES],
            "by_source": by_source, "by_session": by_session[:12],
            "agents": agents, "big_items": big_items[:15],
            "by_model": [{"model": m, **{k: (round(v, 2) if k == "cost" else v)
                                         for k, v in d.items()}}
                         for m, d in sorted(by_model.items(),
                                            key=lambda kv: -kv[1]["cost"])],
            "by_project": by_project[:12],
            "cache_savings": round(cache_savings, 2),
            "unknown_models": sorted(unknown),
            "sessions": st["sessions"], "dir": st["dir"],
            "available": st["available"]}
