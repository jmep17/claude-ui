"""Statusline builder: field palette, generated script, save/apply."""

import json
import re

from .core import _read_json_object, atomic_write, config_dir, tilde
from .settings import settings_set, settings_state


# Fields available to the generated statusline script; sample/color feed the
# UI preview. Input JSON schema: https://code.claude.com/docs/en/statusline
STATUSLINE_FIELDS = [
    {"id": "model", "label": "model", "sample": "Fable 5", "color": "yellow",
     "desc": "model display name"},
    {"id": "modelid", "label": "model id", "sample": "claude-fable-5", "color": "gray",
     "desc": "full model identifier"},
    {"id": "dir", "label": "directory", "sample": "workspace", "color": "blue",
     "desc": "basename of the current dir"},
    {"id": "project", "label": "project", "sample": "workspace", "color": "blue",
     "desc": "basename of the project dir"},
    {"id": "addeddirs", "label": "added dirs", "sample": "+2 dirs", "color": "blue",
     "desc": "directories added via /add-dir"},
    {"id": "repo", "label": "repo", "sample": "jmep17/workspace", "color": "blue",
     "desc": "owner/name of the git remote"},
    {"id": "branch", "label": "git branch", "sample": "main*", "color": "green",
     "desc": "current branch, * when dirty"},
    {"id": "worktree", "label": "worktree", "sample": "wt:my-feature", "color": "green",
     "desc": "git worktree name (when in one)"},
    {"id": "context", "label": "context left", "sample": "92%", "color": "aqua",
     "desc": "context window remaining"},
    {"id": "ctxused", "label": "context used", "sample": "8% used", "color": "aqua",
     "desc": "context window used"},
    {"id": "ctxsize", "label": "context size", "sample": "200k ctx", "color": "aqua",
     "desc": "max context window size"},
    {"id": "tokens", "label": "tokens", "sample": "15.5k↑ 1.2k↓", "color": "aqua",
     "desc": "tokens in the context window (in/out)"},
    {"id": "lastcall", "label": "last call", "sample": "8.5k→1.2k ⚡2k", "color": "aqua",
     "desc": "last API call tokens (⚡ = cache read)"},
    {"id": "cachehit", "label": "cache hit", "sample": "cache 87%", "color": "aqua",
     "desc": "share of last call's input read from cache"},
    {"id": "cachetok", "label": "cache tokens", "sample": "⚡42k +5.2k", "color": "aqua",
     "desc": "last call cache reads (⚡) and writes (+)"},
    {"id": "cost", "label": "cost", "sample": "$0.42", "color": "orange",
     "desc": "session cost in USD"},
    {"id": "costhour", "label": "burn rate", "sample": "$1.80/h", "color": "orange",
     "desc": "session cost per hour of wall clock"},
    {"id": "costtoday", "label": "cost today", "sample": "$4.20 today", "color": "orange",
     "desc": "all sessions today — via ccusage, cached 5 min, ~ when stale"},
    {"id": "costweek", "label": "cost 7 days", "sample": "$28.10 wk", "color": "orange",
     "desc": "last 7 days across sessions — via ccusage, cached 5 min, ~ when stale"},
    {"id": "costmonth", "label": "cost this month", "sample": "$132 mo", "color": "orange",
     "desc": "calendar month across sessions — via ccusage, cached 5 min, ~ when stale"},
    {"id": "duration", "label": "duration", "sample": "24m", "color": "gray",
     "desc": "wall-clock session duration"},
    {"id": "apitime", "label": "api time", "sample": "api 2.3s", "color": "gray",
     "desc": "time spent waiting on the API"},
    {"id": "lines", "label": "lines +/-", "sample": "+120/-45", "color": "orange",
     "desc": "lines added/removed this session"},
    {"id": "effort", "label": "effort", "sample": "high", "color": "purple",
     "desc": "reasoning effort level"},
    {"id": "thinking", "label": "thinking", "sample": "think", "color": "purple",
     "desc": "shown when extended thinking is on"},
    {"id": "fastmode", "label": "fast mode", "sample": "fast", "color": "yellow",
     "desc": "shown when fast mode is on"},
    {"id": "vim", "label": "vim mode", "sample": "NORMAL", "color": "green",
     "desc": "vim mode (when enabled)"},
    {"id": "rate5h", "label": "5h rate limit", "sample": "5h 24%", "color": "red",
     "desc": "five-hour rate limit used (subscribers)"},
    {"id": "rate7d", "label": "7d rate limit", "sample": "7d 41%", "color": "red",
     "desc": "seven-day rate limit used (subscribers)"},
    {"id": "reset5h", "label": "5h reset", "sample": "5h↺18:00", "color": "red",
     "desc": "when the 5-hour rate window resets"},
    {"id": "reset7d", "label": "7d reset", "sample": "7d↺Mon 09:00", "color": "red",
     "desc": "when the 7-day rate window resets"},
    {"id": "session", "label": "session name", "sample": "my-session", "color": "yellow",
     "desc": "session name (after /rename)"},
    {"id": "sessionid", "label": "session id", "sample": "abc123de", "color": "gray",
     "desc": "session id (first 8 chars)"},
    {"id": "agent", "label": "agent", "sample": "security-reviewer", "color": "purple",
     "desc": "agent name (--agent sessions)"},
    {"id": "pr", "label": "pull request", "sample": "#1234 pending", "color": "green",
     "desc": "open PR number and review state"},
    {"id": "style", "label": "output style", "sample": "default", "color": "gray",
     "desc": "active output style name"},
    {"id": "version", "label": "version", "sample": "v2.1.90", "color": "gray",
     "desc": "Claude Code version"},
    {"id": "br1", "label": "line break", "sample": "↵", "color": "gray",
     "desc": "fields after this start a new line — narrow terminals truncate long lines"},
    {"id": "br2", "label": "line break", "sample": "↵", "color": "gray",
     "desc": "a second line break for three-line layouts"},
    {"id": "br3", "label": "line break", "sample": "↵", "color": "gray",
     "desc": "a third line break for four-line layouts"},
]

# palette grouping in the UI
_STL_CATS = {
    "model": ("model", "modelid", "effort", "thinking", "fastmode"),
    "workspace": ("dir", "project", "addeddirs"),
    "git": ("repo", "branch", "worktree", "pr"),
    "context": ("context", "ctxused", "ctxsize", "tokens", "lastcall",
                "cachehit", "cachetok"),
    "cost & time": ("cost", "costhour", "costtoday", "costweek", "costmonth",
                    "duration", "apitime", "lines"),
    "limits": ("rate5h", "rate7d", "reset5h", "reset7d"),
    "session": ("session", "sessionid", "agent", "vim", "style", "version"),
    "layout": ("br1", "br2", "br3"),
}

for _f in STATUSLINE_FIELDS:
    _f["cat"] = next((c for c, ids in _STL_CATS.items() if _f["id"] in ids), "other")

STATUSLINE_DEFAULT = {
    "separator": "  ",
    "fields": [{"id": f["id"], "enabled": f["id"] in ("model", "dir", "branch", "context")}
               for f in STATUSLINE_FIELDS],
}

STATUSLINE_SCRIPT = '''#!/usr/bin/env python3
# Generated by claude-ui (statusline tab) — regenerate there, or edit freely
# (the UI overwrites this file on every save).
# Input schema: https://code.claude.com/docs/en/statusline
import datetime, json, os, shutil, subprocess, sys, time

CONFIG = json.loads(__CONFIG__)

C = {"yellow": "38;5;214", "blue": "38;5;109", "green": "38;5;142",
     "aqua": "38;5;108", "orange": "38;5;208", "gray": "38;5;245",
     "purple": "38;5;175", "red": "38;5;167"}

def paint(name, s, bold=False):
    if not s:
        return ""
    if name and name.startswith("#") and len(name) == 7:
        try:
            code = "38;2;" + ";".join(str(int(name[i:i + 2], 16)) for i in (1, 3, 5))
        except ValueError:
            code = "0"
    else:
        code = C.get(name, "0")
    if bold:
        code = "1;" + code
    return "\\033[" + code + "m" + s + "\\033[0m"

# Historical costs come from ccusage (sums the ~/.claude/projects transcripts;
# https://ccusage.com). It can take seconds, so results live in a cache file
# refreshed by a detached background run — the status line itself never waits.
CACHE = os.path.expanduser("~/.cache/claude-statusline-costs.json")

def local_tz():
    # ccusage groups days in its own default timezone; ours is date.today()'s.
    # Passing the local IANA zone keeps both on the same day boundaries.
    tz = os.environ.get("TZ", "").lstrip(":")
    if "/" in tz:
        return tz
    path = os.path.realpath("/etc/localtime")
    _, _, zone = path.partition("zoneinfo/")
    return zone

def ccusage_daily(argv):
    out = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    return json.loads(out.stdout).get("daily") or []

def claude_day_cost(row):
    # ccusage's totalCost sums every model it saw; a local model served through
    # the same CLI (ollama/proxy) lands in modelBreakdowns under its own id and
    # would inflate the number. Count Claude models only. Older ccusage builds
    # omit the breakdown — fall back to totalCost there.
    mb = row.get("modelBreakdowns")
    if isinstance(mb, list) and mb:
        return sum(float(b.get("cost") or 0) for b in mb
                   if "claude" in (b.get("modelName") or "").lower())
    return float(row.get("totalCost") or 0)

def refresh_costs():
    today = datetime.date.today()
    month_start = today.replace(day=1)
    week_start = today - datetime.timedelta(days=6)
    exe = shutil.which("ccusage")
    # @latest so npx/bunx don't serve a stale cached copy — an old pricing
    # table silently prices new models at $0.
    cmds = [[exe]] if exe else []
    if shutil.which("npx"):
        cmds.append(["npx", "-y", "ccusage@latest"])
    if shutil.which("bunx"):
        cmds.append(["bunx", "ccusage@latest"])
    data = {"ts": time.time()}
    if cmds:
        since = min(month_start, week_start).strftime("%Y%m%d")
        tz = local_tz()
        rows = err = None
        # Any runner can be broken independently (stale install, npm auth,
        # missing --timezone support) — keep trying before giving up.
        for cmd in cmds:
            args = cmd + ["daily", "--json", "--since", since]
            for a in ([args + ["--timezone", tz]] if tz else []) + [args]:
                try:
                    rows = ccusage_daily(a)
                    break
                except Exception as e:
                    err = e
            if rows is not None:
                break
        if rows is None:
            raise err
        t = w = m = 0.0
        for r in rows:
            try:
                # "date" up to ccusage v17, renamed to "period" in later releases
                day = datetime.date.fromisoformat(r.get("period") or r.get("date") or "")
                c = claude_day_cost(r)
            except (ValueError, TypeError):
                continue
            if day >= month_start:
                m += c
            if day >= week_start:
                w += c
            if day == today:
                t += c
        data.update(today=t, week=w, month=m)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(CACHE + ".tmp", CACHE)

if "--refresh-costs" in sys.argv:
    try:
        refresh_costs()
    except Exception:
        pass
    sys.exit(0)

_CC = None

def _costs():
    global _CC
    if _CC is not None:
        return _CC
    try:
        with open(CACHE) as f:
            _CC = json.load(f)
    except Exception:
        _CC = {}
    if time.time() - _CC.get("ts", 0) > 300:
        lock = CACHE + ".lock"
        try:
            stale = time.time() - os.path.getmtime(lock) > 120
        except OSError:
            stale = True
        if stale:
            try:
                os.makedirs(os.path.dirname(lock), exist_ok=True)
                with open(lock, "w"):
                    pass
                subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--refresh-costs"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception:
                pass
    return _CC

d = json.load(sys.stdin)
cost = d.get("cost") or {}
cw = d.get("context_window") or {}
ws = d.get("workspace") or {}
cwd = ws.get("current_dir") or d.get("cwd") or "."

def kfmt(n):
    return format(n / 1000, ".1f").rstrip("0").rstrip(".") + "k" if n >= 1000 else str(n)

def f_model():
    return (d.get("model") or {}).get("display_name", "")

def f_modelid():
    return (d.get("model") or {}).get("id", "")

def f_dir():
    return os.path.basename(cwd)

def f_project():
    return os.path.basename(ws.get("project_dir") or "")

def f_addeddirs():
    n = len(ws.get("added_dirs") or [])
    return "+" + str(n) + (" dir" if n == 1 else " dirs") if n else ""

def f_repo():
    if not in_repo():
        return ""
    r = ws.get("repo") or {}
    return r["owner"] + "/" + r["name"] if r.get("owner") and r.get("name") else ""

def f_worktree():
    wt = (d.get("worktree") or {}).get("name") or ws.get("git_worktree") or ""
    return "wt:" + wt if wt else ""

def _git(*a):
    return subprocess.run(["git", "-C", cwd, *a],
                          capture_output=True, text=True, timeout=1)

_IN_REPO = None

def in_repo():
    global _IN_REPO
    if _IN_REPO is None:
        try:
            _IN_REPO = _git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
        except Exception:
            _IN_REPO = False
    return _IN_REPO

def f_branch():
    if not in_repo():
        return ""
    try:
        b = _git("branch", "--show-current").stdout.strip()
        if b and _git("status", "--porcelain").stdout.strip():
            b += "*"
        return b
    except Exception:
        return ""

def f_context():
    rp = cw.get("remaining_percentage")
    if isinstance(rp, (int, float)):
        return str(round(rp)) + "%"
    if d.get("exceeds_200k_tokens"):
        return "200k+"
    return ""

def f_ctxused():
    up = cw.get("used_percentage")
    return str(round(up)) + "% used" if isinstance(up, (int, float)) else ""

def f_ctxsize():
    s = cw.get("context_window_size")
    return kfmt(int(s)) + " ctx" if isinstance(s, (int, float)) and s else ""

def f_tokens():
    ti, to = cw.get("total_input_tokens"), cw.get("total_output_tokens")
    if not isinstance(ti, (int, float)) and not isinstance(to, (int, float)):
        return ""
    return kfmt(int(ti or 0)) + "\\u2191 " + kfmt(int(to or 0)) + "\\u2193"

def f_lastcall():
    cu = cw.get("current_usage") or {}
    i, o = cu.get("input_tokens"), cu.get("output_tokens")
    if i is None and o is None:
        return ""
    s = kfmt(int(i or 0)) + "\\u2192" + kfmt(int(o or 0))
    cr = cu.get("cache_read_input_tokens")
    if cr:
        s += " \\u26a1" + kfmt(int(cr))
    return s

def f_cachehit():
    cu = cw.get("current_usage") or {}
    total = sum(int(cu.get(k) or 0) for k in
                ("input_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens"))
    if not total:
        return ""
    cr = int(cu.get("cache_read_input_tokens") or 0)
    return "cache " + str(round(100 * cr / total)) + "%"

def f_cachetok():
    cu = cw.get("current_usage") or {}
    cr = int(cu.get("cache_read_input_tokens") or 0)
    cc = int(cu.get("cache_creation_input_tokens") or 0)
    if not cr and not cc:
        return ""
    return " ".join((["\\u26a1" + kfmt(cr)] if cr else [])
                    + (["+" + kfmt(cc)] if cc else []))

def _claude_session():
    # This session's own model. A non-Claude id (local model via ollama/proxy)
    # is priced wrong by the harness, so we don't surface its cost.
    return "claude" in f_modelid().lower()

def f_cost():
    if not _claude_session():
        return "$0.00"
    c = cost.get("total_cost_usd")
    return "$" + format(c, ".2f") if isinstance(c, (int, float)) else ""

def f_costhour():
    c, ms = cost.get("total_cost_usd"), cost.get("total_duration_ms")
    # under a minute of wall clock the extrapolation is meaningless
    if not _claude_session() or not isinstance(c, (int, float)) \\
            or not isinstance(ms, (int, float)) or ms < 60000:
        return ""
    return "$" + format(c * 3600000 / ms, ".2f") + "/h"

def _fmt_cost(v, tag):
    if not isinstance(v, (int, float)):
        return ""
    s = "$" + (format(v, ".2f") if v < 100 else str(round(v))) + " " + tag
    # Refresh failures are silent (the cache just stops updating), so flag
    # numbers well past the 5-minute refresh window instead of passing them
    # off as current.
    if time.time() - _costs().get("ts", 0) > 1800:
        s = "~" + s
    return s

def f_costtoday():
    return _fmt_cost(_costs().get("today"), "today")

def f_costweek():
    return _fmt_cost(_costs().get("week"), "wk")

def f_costmonth():
    return _fmt_cost(_costs().get("month"), "mo")

def f_duration():
    ms = cost.get("total_duration_ms")
    if not isinstance(ms, (int, float)):
        return ""
    m = int(ms // 60000)
    return (str(m // 60) + "h" + format(m % 60, "02d") + "m") if m >= 60 else str(m) + "m"

def f_apitime():
    ms = cost.get("total_api_duration_ms")
    if not isinstance(ms, (int, float)):
        return ""
    if ms >= 60000:
        m = int(ms // 60000)
        return "api " + str(m) + "m" + format(int(ms // 1000) % 60, "02d") + "s"
    return "api " + format(ms / 1000, ".1f").rstrip("0").rstrip(".") + "s"

def f_lines():
    a, r = cost.get("total_lines_added"), cost.get("total_lines_removed")
    if a is None and r is None:
        return ""
    return "+" + str(a or 0) + "/-" + str(r or 0)

def f_effort():
    return (d.get("effort") or {}).get("level", "")

def f_thinking():
    return "think" if (d.get("thinking") or {}).get("enabled") else ""

def f_fastmode():
    return "fast" if d.get("fast_mode") else ""

def f_vim():
    return (d.get("vim") or {}).get("mode", "")

def _rate(win, tag):
    up = ((d.get("rate_limits") or {}).get(win) or {}).get("used_percentage")
    return tag + " " + str(round(up)) + "%" if isinstance(up, (int, float)) else ""

def f_rate5h():
    return _rate("five_hour", "5h")

def f_rate7d():
    return _rate("seven_day", "7d")

def _reset(win, tag):
    ts = ((d.get("rate_limits") or {}).get(win) or {}).get("resets_at")
    if not isinstance(ts, (int, float)):
        return ""
    t = datetime.datetime.fromtimestamp(ts)
    fmt = "%H:%M" if 0 <= ts - time.time() < 86400 else "%a %H:%M"
    return tag + "\\u21ba" + t.strftime(fmt)

def f_reset5h():
    return _reset("five_hour", "5h")

def f_reset7d():
    return _reset("seven_day", "7d")

def f_session():
    return d.get("session_name", "")

def f_sessionid():
    return (d.get("session_id") or "")[:8]

def f_agent():
    return (d.get("agent") or {}).get("name", "")

def f_pr():
    pr = d.get("pr") or {}
    if not pr.get("number"):
        return ""
    return ("#" + str(pr["number"]) + " " + (pr.get("review_state") or "")).strip()

def f_style():
    return (d.get("output_style") or {}).get("name", "")

def f_version():
    v = d.get("version", "")
    return "v" + v if v else ""

FIELDS = {"model": (f_model, "yellow"), "modelid": (f_modelid, "gray"),
          "dir": (f_dir, "blue"), "project": (f_project, "blue"),
          "addeddirs": (f_addeddirs, "blue"), "repo": (f_repo, "blue"),
          "branch": (f_branch, "green"), "worktree": (f_worktree, "green"),
          "context": (f_context, "aqua"), "ctxused": (f_ctxused, "aqua"),
          "ctxsize": (f_ctxsize, "aqua"), "tokens": (f_tokens, "aqua"),
          "lastcall": (f_lastcall, "aqua"), "cachehit": (f_cachehit, "aqua"),
          "cachetok": (f_cachetok, "aqua"), "cost": (f_cost, "orange"),
          "costhour": (f_costhour, "orange"),
          "costtoday": (f_costtoday, "orange"), "costweek": (f_costweek, "orange"),
          "costmonth": (f_costmonth, "orange"),
          "duration": (f_duration, "gray"), "apitime": (f_apitime, "gray"),
          "lines": (f_lines, "orange"),
          "effort": (f_effort, "purple"), "thinking": (f_thinking, "purple"),
          "fastmode": (f_fastmode, "yellow"),
          "vim": (f_vim, "green"), "rate5h": (f_rate5h, "red"),
          "rate7d": (f_rate7d, "red"), "reset5h": (f_reset5h, "red"),
          "reset7d": (f_reset7d, "red"), "session": (f_session, "yellow"),
          "sessionid": (f_sessionid, "gray"), "agent": (f_agent, "purple"),
          "pr": (f_pr, "green"), "style": (f_style, "gray"),
          "version": (f_version, "gray")}

lines = [[]]
for f in CONFIG.get("fields", []):
    if not f.get("enabled"):
        continue
    fid = f.get("id") or ""
    if fid in ("br1", "br2", "br3"):
        lines.append([])
        continue
    fn = FIELDS.get(fid)
    if fn:
        v = fn[0]()
        if v:
            lines[-1].append(paint(f.get("color") or fn[1], v, f.get("bold")))
# A visible separator always gets one space of breathing room on each side
# and is dimmed so the fields stand out; whitespace-only is used as-is.
sep = CONFIG.get("separator", "  ")
if sep.strip():
    sep = paint("gray", " " + sep.strip() + " ")
print("\\n".join(sep.join(l) for l in lines if l))
'''

def statusline_paths():
    """(config json, script) — both live directly in the Claude config dir."""
    cfg = config_dir()
    return cfg / "statusline.json", cfg / "statusline.sh"

def statusline_state():
    cfgp, scriptp = statusline_paths()
    cfg, err = _read_json_object(cfgp)
    sdata = settings_state()["data"]
    sl = sdata.get("statusLine") if isinstance(sdata, dict) else None
    applied = (isinstance(sl, dict)
               and sl.get("command") in (tilde(scriptp), str(scriptp)))
    return {"config": cfg if cfg else None, "error": err,
            "script_exists": scriptp.is_file(),
            "script_path": tilde(scriptp),
            "available": STATUSLINE_FIELDS,
            "default": STATUSLINE_DEFAULT,
            "applied": applied,
            "current_statusline": sl}

STATUSLINE_COLOR_NAMES = {"yellow", "blue", "green", "aqua", "orange",
                          "gray", "purple", "red"}

def statusline_save(config, apply):
    if not isinstance(config, dict) or not isinstance(config.get("fields"), list):
        raise ValueError("bad statusline config")
    known = {f["id"] for f in STATUSLINE_FIELDS}
    fields = []
    for f in config["fields"]:
        if not isinstance(f, dict) or f.get("id") not in known:
            raise ValueError(f"unknown statusline field: {f}")
        entry = {"id": f["id"], "enabled": bool(f.get("enabled"))}
        color = f.get("color")
        if color:
            if not isinstance(color, str) or not (
                    color in STATUSLINE_COLOR_NAMES
                    or re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
                raise ValueError(f"bad statusline color: {color}")
            entry["color"] = color
        if f.get("bold"):
            entry["bold"] = True
        fields.append(entry)
    sep = config.get("separator", "  ")
    if not isinstance(sep, str) or len(sep) > 16:
        raise ValueError("bad separator")
    refresh = config.get("refresh", 0)
    if not isinstance(refresh, int) or not 0 <= refresh <= 3600:
        raise ValueError("bad refresh interval")
    clean = {"separator": sep, "refresh": refresh, "fields": fields}
    cfgp, scriptp = statusline_paths()
    atomic_write(cfgp, json.dumps(clean, indent=2) + "\n")
    atomic_write(scriptp,
                 STATUSLINE_SCRIPT.replace("__CONFIG__", json.dumps(json.dumps(clean))),
                 mode=0o755)
    if apply:
        sl = {"type": "command", "command": tilde(scriptp)}
        if refresh:
            sl["refreshInterval"] = refresh
        settings_set("statusLine", sl)

def statusline_apply(config=None):
    """Install the statusline as a setup piece: generate the script and point
    settings at it. Uses the saved config, else the default field selection."""
    if config is None:
        config, _ = _read_json_object(statusline_paths()[0])
        if not config:
            config = STATUSLINE_DEFAULT
    statusline_save(config, apply=True)

def statusline_remove():
    """Uninstall the statusline piece: drop the generated script and clear the
    settings key. The saved config json is left so a re-apply restores fields."""
    cfgp, scriptp = statusline_paths()
    scriptp.unlink(missing_ok=True)
    sl = settings_state()["data"].get("statusLine")
    if isinstance(sl, dict) and sl.get("command") in (tilde(scriptp), str(scriptp)):
        settings_set("statusLine", None)
