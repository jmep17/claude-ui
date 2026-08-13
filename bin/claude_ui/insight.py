"""Transcript analytics: usage counters, Bash prefixes, cost stats.

The context-budget estimate that used to live here moved to context.py, which
sizes every scope rather than just the config dir; this module keeps the
transcript machinery both tabs read.
"""

from pathlib import Path
import calendar
import datetime
import json
import re
import time

from .core import config_dir, read_cfg, tilde


USAGE_CACHE = Path.home() / ".cache" / "claude-ui-usage.json"

# Bump whenever the shape or meaning of the cached per-file data changes, so
# stale entries are re-scanned instead of being mixed with the current format.
CACHE_V = 7

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
    try:
        # The regex only checks shape, so "2026-13-99" reaches this point and
        # timegm raises. One malformed line must not take down the whole scan.
        epoch = calendar.timegm((y, mo, d, h, mi, s, 0, 1, 0))
    except ValueError:
        return ""
    off = m.group(7)
    if off and off not in ("Z", "z"):
        digits = off[1:].replace(":", "")
        delta = int(digits[:2]) * 3600 + int(digits[2:]) * 60
        epoch += delta if off[0] == "-" else -delta
    try:
        return time.strftime("%Y-%m-%d", time.localtime(epoch))
    except (ValueError, OSError, OverflowError):
        return ""

DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

def _is_day(key):
    """True for a real local-date bucket, false for the "unknown" catch-all.

    Day buckets are compared as strings, and "unknown" sorts after every ISO date
    ('u' > '2'), so an untimestamped message would land in *every* dated window
    while still failing the == test for today. Windows filter on this instead.
    """
    return bool(DAY_RE.fullmatch(key or ""))

def _tz_fingerprint():
    """Cached day buckets are local dates, so a machine timezone change makes
    them wrong — fold the zone into the cache validity check."""
    return "|".join(time.tzname) + "|" + str(time.timezone)

# Token counts are accumulated per (day, model, rate) into one row of this shape.
ROW_ZERO = [0, 0, 0, 0, 0, 0, 0]
R_IN, R_OUT, R_CW5M, R_CW1H, R_CR, R_WEB, R_MSGS = range(7)

# Index of the first counter in a cached per-message entry, which is laid out as
# [day, model, rate, in, out, cacheW5m, cacheW1h, cacheR, webSearches].
R_FIRST_COUNT = 3

def _msg_key(entry, msg, path, lineno):
    """Identity of the API response an entry belongs to, for de-duplication.

    One assistant message spans several transcript lines — one per content block
    — and every one of those lines repeats the whole message's `usage`. Summing
    lines therefore multiplies a message's tokens by its block count (~2.4x in
    practice). The same message can also appear in more than one file when a
    session is forked. Keyed on message id + request id, matching ccusage, so
    the two agree; entries too old to carry an id fall back to their uuid, and
    then to their position in the file, which counts them once each rather than
    dropping them. Both fallbacks have to be stable across runs: these keys are
    written to the on-disk cache and de-duplicated against globally.
    """
    mid = msg.get("id")
    if mid:
        return str(mid) + "\t" + str(entry.get("requestId") or "")
    uid = entry.get("uuid")
    if uid:
        return "uuid\t" + str(uid)
    return "line\t" + str(path) + "\t" + str(lineno)

# Premiums that scale every token category of a single message. Fast mode bills at
# 2x base ($10/$50 against Opus 5's $5/$25), but only on the models that actually
# run it — Opus 4.6 accepts speed=fast, runs at standard speed and bills standard,
# and Opus 4.7 rejects it outright. Pinning inference to the US costs 1.1x. They
# stack with each other and sit on top of the cache multipliers below.
FAST_MODELS = ("opus-5", "opus-4-8")

def _rate_multiplier(model, usage):
    """Per-message multiplier on token pricing (1.0 for an ordinary request)."""
    m = (model or "").lower()
    mult = 1.0
    if usage.get("speed") == "fast" and any(s in m for s in FAST_MODELS):
        mult *= 2
    if usage.get("inference_geo") == "us":
        mult *= 1.1
    return mult

# A day can mix rates — fast mode is toggled mid-session — so rows are bucketed by
# model *and* multiplier and split apart again when priced. The composite key never
# leaves this module.
def _rate_key(model, mult):
    return model + "\x1f" + format(mult, ".4f")

def _split_rate_key(key):
    model, _, mult = key.partition("\x1f")
    try:
        return model, float(mult)
    except ValueError:
        return model, 1.0

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
    """One transcript -> {counts, msgs, bash, cwd, sess, nomodel}."""
    counts = {}   # "kind\tname" -> [count, last_iso_ts]
    msgs = {}     # dedup key -> [local day, model, in, out, cacheW5m, cacheW1h, cacheR]
    bash = {}     # prefix -> count
    # Usage this scan had to throw away because the message carried no model id.
    # Nothing downstream can price or even name it, so count it here or the
    # Costs tab has no way to say why its total is short.
    nomodel = 0
    cwd = ""
    first_ts = ""
    last_ts = ""

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
            for lineno, line in enumerate(f, 1):
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
                if isinstance(usage, dict) and not msg.get("model"):
                    nomodel += 1
                if isinstance(usage, dict) and msg.get("model"):
                    # cache_creation splits the write total by TTL, which matters
                    # because a 1-hour write costs 2x base and a 5-minute one
                    # 1.25x. Transcripts predating the split had only 5m writes.
                    cw = int(usage.get("cache_creation_input_tokens") or 0)
                    cc = usage.get("cache_creation")
                    w1h = int((cc.get("ephemeral_1h_input_tokens") or 0)
                              if isinstance(cc, dict) else 0)
                    w1h = max(0, min(w1h, cw))
                    # Web search is billed per search on top of the token rates;
                    # web fetch, the other server_tool_use counter, is free.
                    stu = usage.get("server_tool_use")
                    web = int((stu.get("web_search_requests") or 0)
                              if isinstance(stu, dict) else 0)
                    entry = [
                        _local_day(ts) or "unknown", msg["model"],
                        _rate_multiplier(msg["model"], usage),
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                        cw - w1h, w1h,
                        int(usage.get("cache_read_input_tokens") or 0), web]
                    if not first_ts:
                        first_ts = ts
                    last_ts = max(last_ts, ts)
                    key = _msg_key(d, msg, path, lineno)
                    prev = msgs.get(key)
                    if prev is None:
                        msgs[key] = entry
                    else:
                        # The lines of one message are written as it streams, so an
                        # early content block can carry a partial usage and a later
                        # one the authoritative total (seen on ~9% of messages, and
                        # the whole of a 3% output-token shortfall against ccusage).
                        # Take the largest of each counter — order-independent,
                        # unlike last-wins — while keeping the first line's day and
                        # rate, whose timestamps drift by milliseconds.
                        for i in range(R_FIRST_COUNT, len(entry)):
                            if entry[i] > prev[i]:
                                prev[i] = entry[i]
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
        return {"counts": {}, "msgs": {}, "bash": {}, "cwd": "", "sess": None,
                "nomodel": 0}
    # Per-session summary for the Context tab. `first` is the first
    # usage-bearing message's [input, cache writes, cache reads] — read from
    # msgs after the streaming max-merge above, so a partial early line does
    # not understate it. Their sum approximates the context present at the
    # session's first turn; the running max of cache reads its peak.
    sess = None
    if msgs:
        f = msgs[next(iter(msgs))]   # insertion order: first usage message
        c = R_FIRST_COUNT
        sess = {"first": [f[c + R_IN], f[c + R_CW5M] + f[c + R_CW1H],
                          f[c + R_CR]],
                "max_cr": max(e[c + R_CR] for e in msgs.values()),
                "first_ts": first_ts, "last_ts": last_ts, "model": f[1]}
    return {"counts": counts, "msgs": msgs, "bash": bash, "cwd": cwd,
            "sess": sess, "nomodel": nomodel}

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
    nbytes = 0
    oversize = 0
    pdir = projects_dir()
    if pdir.is_dir():
        for p in pdir.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            nbytes += st.st_size
            if st.st_size > MAX_TRANSCRIPT:
                oversize += 1
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
    session_rows = []
    seen_msgs = set()
    nomodel = 0
    for key in sorted(files):   # sorted so a cross-file duplicate resolves the same way every run
        data = files[key].get("data") or {}
        # Not deduped: an entry with no model id has no dedup key worth trusting.
        # It's a diagnostic count, not an input to any total.
        nomodel += data.get("nomodel") or 0
        session_rows.append({"path": key, "cwd": data.get("cwd") or "",
                             "sess": data.get("sess"),
                             "msgs": len(data.get("msgs") or {})})
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
        for mkey, e in (data.get("msgs") or {}).items():
            if mkey in seen_msgs:   # same message already counted from another file
                continue
            seen_msgs.add(mkey)
            day, rkey = e[0], _rate_key(e[1], e[2])
            for agg in (days.setdefault(day, {}), pdays.setdefault(day, {})):
                s = agg.setdefault(rkey, ROW_ZERO[:])
                for i, v in enumerate(e[3:]):
                    s[i] += v
                s[R_MSGS] += 1
        for prefix, n in (data.get("bash") or {}).items():
            bash[prefix] = bash.get(prefix, 0) + n
    return {"sessions": len(files), "scanned_now": scanned, "by": by,
            "days": days, "bash": bash, "projects": projects,
            "session_rows": session_rows, "usage_msgs": len(seen_msgs),
            "nomodel": nomodel, "bytes": nbytes, "oversize": oversize,
            "dir": tilde(pdir), "available": pdir.is_dir()}

def cost_diagnostics(rescan=False):
    """Why the Costs tab shows what it shows, on this machine.

    A census of every model id the pricer actually sees, with the verdict it
    reaches on each, plus the counts that say whether a shortfall happened at
    scan time (no model id on the message) or at pricing time (an id nothing
    recognises). Read-only, and derived from the same cached scan the tab uses,
    so it reports what the tab really did rather than a second opinion.
    """
    st = transcript_stats(rescan)
    overrides = read_cfg().get("pricing")
    today = datetime.date.today().isoformat()
    census = {}
    for day, rows in st["days"].items():
        for rkey, row in rows.items():
            model, _ = _split_rate_key(rkey)
            c = census.setdefault(model or "(no model id)",
                                  {"model": model or "(no model id)", "msgs": 0,
                                   "in": 0, "out": 0, "days": set()})
            c["msgs"] += row[R_MSGS]
            c["in"] += row[R_IN]
            c["out"] += row[R_OUT]
            c["days"].add(day)
    models = []
    for name, c in census.items():
        if _excluded(name, overrides):
            verdict, note = "dropped", ("known placeholder, never billed"
                                        if name == "<synthetic>"
                                        else "not a Claude id and no 'pricing' "
                                             "override matches it")
        else:
            pin, pout, known = model_price(name, today, overrides)
            hit = _override_match(overrides, name)
            src = (f" from your 'pricing' override \"{hit[0]}\"" if hit
                   else " from the built-in price table")
            if not pin and not pout:
                # Priced, but at nothing — the one verdict that looks like
                # success and reads like a bug on the tab.
                verdict = "zero-priced"
                note = (f"$0/$0 per Mtok{src} — this model's usage counts as free"
                        if hit else "$0/$0 per Mtok — no rate to charge")
            else:
                verdict = "priced" if known else "estimated"
                note = (f"${pin}/${pout} per Mtok{src}" if known
                        else f"no list price — guessed at ${pin}/${pout} per Mtok")
        models.append({**c, "days": len(c["days"]), "verdict": verdict,
                       "note": note})
    models.sort(key=lambda m: -m["msgs"])
    ov = []
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            ov.append({"key": str(k),
                       "ok": _override_price({k: v}, str(k)) is not None,
                       "value": v if isinstance(v, (list, str, int, float))
                                else str(v)})
    cache = {"path": tilde(USAGE_CACHE), "exists": USAGE_CACHE.is_file(),
             "version": CACHE_V}
    try:
        cache["size"] = USAGE_CACHE.stat().st_size
        cache["mtime"] = time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(USAGE_CACHE.stat().st_mtime))
    except OSError:
        cache["size"], cache["mtime"] = 0, ""
    return {"dir": st["dir"], "available": st["available"],
            "transcripts": st["sessions"], "bytes": st.get("bytes", 0),
            "oversize": st.get("oversize", 0),
            "usage_msgs": st.get("usage_msgs", 0),
            "nomodel": st.get("nomodel", 0),
            "days": len(st["days"]), "models": models, "overrides": ov,
            "cache": cache, "max_transcript": MAX_TRANSCRIPT}

def usage_stats(rescan=False):
    # The insight tab wants the item/bash counters, not the cost aggregates — and
    # `days`/`projects` are keyed by the internal rate key, so don't ship them.
    # `session_rows` belongs to the Context tab, which has its own endpoint.
    st = transcript_stats(rescan)
    return {k: v for k, v in st.items()
            if k not in ("days", "projects", "session_rows")}

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

def _override_match(overrides, model):
    """(key, input, output) for the `pricing` override that wins, else None.

    One place decides what counts as a usable override, so `_excluded` can't opt a
    model into the totals that `model_price` then declines to price — a malformed
    entry like {"llama": 3} used to do exactly that, admitting a local model and
    then billing it at the opus-tier guess.

    Keys match as substrings, so more than one can apply. The most specific wins:
    an exact id first, then the longest key — not whichever the JSON happened to
    list first, which made {"opus": [0, 0], "claude-opus-5": [5, 25]} depend on
    file order. The winning key is returned because a $0 total is usually one
    broad override, and the tab can only say so if it knows which.
    """
    if not isinstance(overrides, dict):
        return None
    m = (model or "").lower()
    best = None
    for sub, v in overrides.items():
        k = str(sub).lower()
        if k not in m or not (isinstance(v, (list, tuple)) and len(v) == 2):
            continue
        try:
            pin, pout = float(v[0]), float(v[1])
        except (TypeError, ValueError):
            continue
        rank = (k == m, len(k))
        if best is None or rank > best[0]:
            best = (rank, (str(sub), pin, pout))
    return best[1] if best else None

def _override_price(overrides, model):
    """(input, output) from a matching `pricing` override, else None."""
    hit = _override_match(overrides, model)
    return (hit[1], hit[2]) if hit else None

def model_price(model, day, overrides=None):
    """(input, output, known) $/Mtok for a model on a given local day."""
    if overrides is None:
        overrides = read_cfg().get("pricing")
    ov = _override_price(overrides, model)
    if ov:
        return ov[0], ov[1], True
    m = (model or "").lower()
    for entry in PRICING:
        sub, pin, pout = entry[0], entry[1], entry[2]
        until = entry[3] if len(entry) > 3 else None
        # ISO dates compare correctly as strings; an unparseable day sorts past
        # every window, so it prices at the current rate rather than an old one.
        if sub in m and (until is None or day <= until):
            return pin, pout, True
    return 5, 25, False  # unknown model: opus-tier guess, flagged in the UI

# Cache writes are billed above the base input rate (2x for the 1-hour TTL, 1.25x
# for the 5-minute one) and cache reads at 0.1x. `mult` carries the per-message
# premiums (fast mode, US-pinned inference), which scale every token category and
# stack on top of the cache multipliers. Web search is a flat per-search charge
# outside the token rates.
WEB_SEARCH_USD = 0.01   # $10 per 1,000 searches

def _row_cost(row, pin, pout, mult=1.0):
    return ((row[R_IN] * pin + row[R_OUT] * pout
             + row[R_CW5M] * pin * 1.25 + row[R_CW1H] * pin * 2
             + row[R_CR] * pin * 0.1) * mult / 1e6
            + row[R_WEB] * WEB_SEARCH_USD)

# The dashboard prices the Anthropic family only. Anything else served through
# the same CLI — local models via ollama/proxies, the "<synthetic>" placeholder —
# is dropped entirely (no row, no tokens, no total). A `pricing` override opts a
# non-Claude id back in, since setting a price signals intent to count it.
# "anthropic" counts as family too (Bedrock-style `anthropic.claude-…` already
# matched, but a bare `anthropic.something` did not): an unrecognised id there
# prices at the opus-tier guess and lands in `unknown_models`, which is a visible,
# overridable estimate rather than a silent zero. Whatever is dropped is reported
# back through `excluded_models`, so a $0 screen can say why.
def _excluded(model, overrides=None):
    m = (model or "").lower()
    if "claude" in m or "anthropic" in m:
        return False
    if overrides is None:
        overrides = read_cfg().get("pricing")
    return _override_price(overrides, model) is None

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
    month = d.replace(day=1).isoformat()
    per_day = []
    by_model = {}
    totals = {"today": 0, "last7": 0, "last30": 0, "month": 0, "all": 0}
    cache_savings = 0.0
    unknown = set()
    # What pricing threw away, so the UI can name it instead of showing a grid of
    # $0.000 with no explanation. Collected in the days loop only — the by_project
    # loop below re-walks the same rows and would double-count.
    excluded = set()
    dropped_msgs = 0
    # A `pricing` override of [0, 0] prices real usage at nothing. That is the
    # point for a local model, but the keys match as substrings, so a short one
    # ("opus") silently zeroes every Claude id it appears in. Record model -> the
    # override key responsible, so a $0 screen can name the cause.
    zeroed = {}
    # Read once: the overrides are consulted for every (day, model, rate) row, and
    # re-reading mid-computation could price two days off different config.
    overrides = read_cfg().get("pricing")
    for day in sorted(st["days"]):
        dated = _is_day(day)
        drow = {"day": day, "cost": 0, "by": {}}
        for rkey, row in st["days"][day].items():
            model, mult = _split_rate_key(rkey)
            if _excluded(model, overrides):
                # "<synthetic>" marks CLI-generated messages that were never
                # billed, so reporting it as a pricing gap would cry wolf on
                # every machine. Everything else is a real dropped id.
                if model != "<synthetic>":
                    excluded.add(model or "(no model id)")
                    dropped_msgs += row[R_MSGS]
                continue
            pin, pout, known = model_price(model, day, overrides)
            if not known:
                unknown.add(model)
            if not pin and not pout:
                hit = _override_match(overrides, model)
                zeroed[model] = hit[0] if hit else ""
            c = _row_cost(row, pin, pout, mult)
            drow["cost"] += c
            # One model can appear at more than one rate on the same day, so
            # accumulate rather than assign.
            drow["by"][model] = round(drow["by"].get(model, 0) + c, 4)
            m = by_model.setdefault(model, {"cost": 0, "in": 0, "out": 0,
                                            "cacheR": 0, "cacheW": 0, "msgs": 0})
            m["cost"] += c
            m["in"] += row[R_IN]
            m["out"] += row[R_OUT]
            m["cacheW"] += row[R_CW5M] + row[R_CW1H]
            m["cacheR"] += row[R_CR]
            m["msgs"] += row[R_MSGS]
            cache_savings += row[R_CR] * pin * 0.9 * mult / 1e6
            totals["all"] += c
            # Dated windows take real days only, and are bounded at both ends: the
            # "unknown" bucket sorts past every ISO date, and a clock-skewed future
            # day would otherwise land in all three. Both still count toward the
            # all-time total — the tokens were genuinely spent.
            if not dated or day > today:
                continue
            if day == today:
                totals["today"] += c
            if day >= d7:
                totals["last7"] += c
            if day >= d30:
                totals["last30"] += c
            if day >= month:
                totals["month"] += c
        drow["cost"] = round(drow["cost"], 4)
        if dated:
            per_day.append(drow)
    by_project = []
    for cwd, drows in st["projects"].items():
        c = 0.0
        msgs = 0
        for day, mrows in drows.items():
            for rkey, row in mrows.items():
                model, mult = _split_rate_key(rkey)
                if _excluded(model, overrides):
                    continue
                c += _row_cost(row, *model_price(model, day, overrides)[:2],
                               mult=mult)
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
            "excluded_models": sorted(excluded), "dropped_msgs": dropped_msgs,
            # Measured, not inferred: the zero-state alert states which of these
            # is actually true rather than guessing at a cause.
            "usage_msgs": st.get("usage_msgs", 0), "nomodel": st.get("nomodel", 0),
            "zeroed_models": [{"model": m, "override": k}
                              for m, k in sorted(zeroed.items())],
            "sessions": st["sessions"], "dir": st["dir"],
            "available": st["available"]}
