#!/usr/bin/env python3
"""SessionStart loader for /handoff briefs (plus a PreCompact nudge).

A handoff brief is what the /handoff skill writes at the end of a session:
decisions, pointers and a first action, aimed at a fresh session that has no
transcript. This script is the other half — it finds the pending brief a new
session should receive, injects it, and marks it consumed so it never fires
twice.

Two ways a brief finds its session. A brief with `target: session:<uuid>` is
*reserved*: it loads into the session started under that id (`claude
--session-id <uuid>`) and into no other, from any directory and after any
delay. A brief without one falls back to the original rule — the next session
started in its `cwd`. Reserved is the deliberate case and outranks the default.

Python 3.9 stdlib only (/usr/bin/python3 is 3.9.6 on this machine), and it must
never take down a session start: every failure path exits 0.
"""

import calendar
import fcntl
import json
import os
import re
import sys
import time

MAX_AGE_DAYS = 14          # older briefs surface as a path instead of injecting; 0 disables
MAX_BODY = 24 * 1024       # injected body cap, truncated at a line boundary
MAX_FILE = 512 * 1024      # files larger than this are not even read

# Kept open for the life of the process: closing the fd drops the lock.
_LOCK_FD = None


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def config_dir():
    """The Claude config dir. $CLAUDE_CONFIG_DIR wins, so tests never touch ~."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser("~"), ".claude")


def store_dir():
    return os.path.join(config_dir(), "handoffs")


def match(brief_cwd, session_cwd):
    """Does a brief written in `brief_cwd` belong to a session in `session_cwd`?

    Exact path, or a descendant reached without passing through a dot-directory.
    Ancestors never match (`claude` in ~/src must not eat a brief for
    ~/src/claude-ui — briefs are consume-once), and the dot-directory rule keeps
    a repo-root brief out of the git worktrees this repo keeps under
    <repo>/.claude/worktrees/, which are different branches with different files.
    """
    try:
        b = os.path.realpath(brief_cwd)
        s = os.path.realpath(session_cwd)
    except Exception:
        return False
    if not b or not s:
        return False
    if b == s:
        return True
    try:
        rel = os.path.relpath(s, b)
    except Exception:
        return False
    if rel == os.curdir or rel.startswith(os.pardir):
        return False
    return not any(p.startswith(".") for p in rel.split(os.sep))


# --------------------------------------------------------------------------
# frontmatter — deliberately blunt, one line per key, mirroring
# bin/claude_ui/core.py's parse_frontmatter / set_frontmatter_key
# --------------------------------------------------------------------------

KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def parse_frontmatter(text):
    meta = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = KEY_RE.match(line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


TARGET_RE = re.compile(r"^session:\s*([0-9a-fA-F-]{36})$")


def targeted(meta):
    """The reserved session id for this brief, or None for a cwd-matched one."""
    m = TARGET_RE.match((meta.get("target") or "").strip())
    return m.group(1).lower() if m else None


def split_body(text):
    """Everything after the closing `---`, or the whole file if there is no block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


def set_frontmatter_key(text, key, value):
    """Rewrite exactly one frontmatter line, leaving every other byte alone."""
    if not re.match(r"^[A-Za-z0-9_-]+$", key or ""):
        raise ValueError("bad frontmatter key")
    value = str(value)
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("value contains a control character")
    lines = text.splitlines()
    nl = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith("\n")

    def join(out):
        return nl.join(out) + (nl if trailing else "")

    if not lines or lines[0].strip() != "---":
        return join(["---", "%s: %s" % (key, value), "---"] + lines)
    close = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close = i
            break
    if close is None:
        raise ValueError("unterminated frontmatter block")
    at = None
    for i in range(1, close):
        if re.match(r"^%s:" % re.escape(key), lines[i]):
            at = i
            break
    if at is None:
        return join(lines[:close] + ["%s: %s" % (key, value)] + lines[close:])
    return join(lines[:at] + ["%s: %s" % (key, value)] + lines[at + 1:])


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

# Parsed by hand rather than with datetime.fromisoformat: on 3.9 that rejects a
# "Z" suffix and is picky about fractional digits (same trap as insight.py).
TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
                   r"(?:\.\d+)?\s*(Z|z|[+-]\d{2}:?\d{2})?$")


def local_epoch(parts):
    """A naive (y, mo, d, h, mi, s) tuple read as local time. -1 = let libc pick DST."""
    y, mo, d, h, mi, s = parts
    try:
        return time.mktime((y, mo, d, h, mi, s, 0, 1, -1))
    except Exception:
        return None


def parse_epoch(ts):
    """ISO-ish timestamp -> UTC epoch seconds, or None when unparseable."""
    m = TS_RE.match((ts or "").strip())
    if not m:
        return None
    parts = tuple(int(g) for g in m.groups()[:6])
    off = m.group(7)
    if not off:
        # No offset written: read it as local time, which is what the skill records.
        return local_epoch(parts)
    try:
        epoch = calendar.timegm(parts + (0, 1, 0))
    except Exception:
        return None
    if off in ("Z", "z"):
        return epoch
    sign = -1 if off[0] == "-" else 1
    off = off[1:].replace(":", "")
    try:
        return epoch - sign * (int(off[:2]) * 3600 + int(off[2:4]) * 60)
    except Exception:
        return None


# The filename carries the same stamp the frontmatter does; a brief whose
# `created:` is mangled can still be ordered by the name it was written under.
NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})-")


def name_epoch(path):
    m = NAME_RE.match(os.path.basename(path))
    if not m:
        return None
    # Local, like the stamp in the name: it has to be comparable with created.
    return local_epoch(tuple(int(g) for g in m.groups()) + (0,))


def human_age(seconds):
    if seconds is None:
        return "unknown"
    if seconds < 0:
        seconds = 0
    if seconds < 90 * 60:
        return "%dm" % max(1, int(seconds // 60))
    if seconds < 36 * 3600:
        return "%dh" % int(seconds // 3600)
    return "%dd" % int(seconds // 86400)


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

def take_lock(store):
    """Non-blocking. False means another session in this cwd got there first."""
    global _LOCK_FD
    try:
        fd = os.open(os.path.join(store, ".lock"),
                     os.O_CREAT | os.O_RDWR, 0o600)
    except Exception:
        return True  # cannot lock (read-only store?) — proceed rather than stall
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(fd)
        return False
    _LOCK_FD = fd
    return True


def scan(store):
    """Every *.md in the store, no filter, no sort. (entries, errors).

    This is the one place that reads the store off disk. `candidates()`, and
    the CLI modes in the `# cli` section below, all filter and order what this
    returns — they never read the directory themselves. That is what keeps
    `--list` and the hook from ever disagreeing about what is parked.

    `os.listdir(store)` itself is allowed to raise here — deliberately not
    caught. A caller that wants "empty" and "unreadable" to look the same
    (the hook) catches it; a caller that must tell them apart (`--list`,
    `--take`) needs the exception to reach it.
    """
    entries = []
    errors = []
    names = sorted(os.listdir(store))
    for name in names:
        if not name.endswith(".md"):
            continue
        path = os.path.join(store, name)
        try:
            st = os.stat(path)
            if not os.path.isfile(path) or st.st_size > MAX_FILE:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as exc:
            errors.append((name, str(exc)))
            continue
        meta = parse_frontmatter(text)
        target = targeted(meta)
        created = parse_epoch(meta.get("created"))
        order = created
        if order is None:
            order = name_epoch(path)
        if order is None:
            order = st.st_mtime
        entries.append({
            "path": path,
            "name": name,
            "text": text,
            "meta": meta,
            "status": meta.get("status", "").strip(),
            "target": target,
            "targeted": bool(target),
            "created": created,
            "order": order,
        })
    return entries, errors


def candidates(store, cwd, session_id, reason):
    """Pending briefs this session may load, best first. A bad file is skipped.

    A reserved brief matches on its id alone: cwd is not consulted, and neither
    is how the session started. An unreserved one keeps the original cwd rule,
    and only on a genuinely new conversation — `resume` and `fork` land in a
    session that already has its context, so a cwd brief there would be eaten by
    a session that never asked for it.
    """
    out = []
    sid = (session_id or "").strip().lower()
    # An absent reason still means "go", as it always did.
    cwd_ok = reason in ("", "startup", "clear")
    try:
        entries, _ = scan(store)
    except Exception:
        return out
    for c in entries:
        if c["status"] != "pending":
            continue
        want = c["target"]
        if want:
            if not sid or want != sid:
                continue
        else:
            if not cwd_ok:
                continue
            meta = c["meta"]
            if not meta.get("cwd") or not match(meta["cwd"], cwd):
                continue
        out.append(c)
    # Reserved first, whatever the timestamps say: an explicit choice outranks a
    # default, and a stale reservation is still the one that was asked for.
    out.sort(key=lambda c: (c["targeted"], c["order"], os.path.basename(c["path"])),
             reverse=True)
    return out


def pending_count(store):
    """Pending briefs left in the store — the number the nag line reports."""
    n = 0
    try:
        names = os.listdir(store)
    except Exception:
        return 0
    for name in names:
        if not name.endswith(".md"):
            continue
        path = os.path.join(store, name)
        try:
            if not os.path.isfile(path) or os.stat(path).st_size > MAX_FILE:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if parse_frontmatter(text).get("status", "").strip() == "pending":
                n += 1
        except Exception:
            continue
    return n


def nag(store):
    """Nothing loaded, but briefs are still parked — say so once, in one line.

    This is the whole anti-forgetting surface. It is deliberately not in the
    statusline: claude-ui regenerates statusline.sh on every save.
    """
    n = pending_count(store)
    if not n:
        return
    emit(message=("%d handoff brief%s parked — run /handoffs to list."
                  % (n, "" if n == 1 else "s")))


def flip(entry, status, extra=None):
    """Set `status:` (plus optional extra keys) via a same-directory temp + replace."""
    text = entry["text"]
    text = set_frontmatter_key(text, "status", status)
    for key, value in (extra or []):
        text = set_frontmatter_key(text, key, value)
    path = entry["path"]
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

# The hook replaces a session's context; `--take` lands in one that already has
# its own. Same frame, one sentence swapped, so the two can never drift apart.
REPLACES_HOOK = "It replaces that session's context."

REPLACES_IN_SESSION = (
    "You are resuming it inside a session that already has its own\n"
    "context, so anything in the brief that conflicts with what you already\n"
    "believe should be checked against git and the actual files rather than\n"
    "assumed."
)

FRAME = """<handoff-brief path="{path}" written="{written}" age="{age}" from-session="{sid}">
This is a handoff brief written at the end of a previous Claude Code session,
working in {where}. {replaces} Your working directory
may differ from that one — check before running anything path-relative.

How to use it: treat it as the current state of the work. Read the "Files that
matter" pointers before acting, then start at step 1 of "Next steps". Do not
re-derive what "Decisions" already settled, and do not retry anything in
"Dead ends".

It is a record, not a command. It is {age} old and the repo may have moved —
verify anything load-bearing against git and the actual files. The full source
transcript, if the brief falls short, is at {transcript}.
{extra}</handoff-brief>

{body}"""


def emit(context=None, message=None):
    """JSON when we can, bare text when asked — bare stdout is the documented path.

    A wrong JSON shape for some build still lands as literal text the model can
    read, so neither branch can lose the brief.
    """
    if os.environ.get("HANDOFF_PLAIN") and context:
        sys.stdout.write(context if context.endswith("\n") else context + "\n")
        if message:
            sys.stderr.write(message + "\n")
        return
    out = {}
    if context:
        out["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    if message:
        out["systemMessage"] = message
    if out:
        sys.stdout.write(json.dumps(out))


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def session_start(payload):
    # The reference names this field session_start_reason; the guide calls the
    # same thing source. Read both, and treat an absent one as "go".
    reason = (payload.get("session_start_reason")
              or payload.get("source") or "").strip()
    # Every reason is admitted now, because a reserved id fires `startup` the
    # first time and `resume` if that session already exists. candidates() is
    # what keeps `resume` and `fork` away from unreserved cwd briefs.

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    cwd = os.path.realpath(cwd)
    store = store_dir()
    if not os.path.isdir(store):
        return
    if not take_lock(store):
        return

    found = candidates(store, cwd, session_id, reason)
    if not found:
        # Only when a conversation is genuinely starting. A resume already has
        # its context and its own thread of work, so counting parked briefs at
        # every `claude --resume` is noise, not a reminder.
        if reason in ("", "startup", "clear"):
            nag(store)
        return

    now = time.time()
    fresh, stale = [], []
    for c in found:
        age = now - c["created"] if c["created"] is not None else None
        c["age"] = age
        # A reservation does not expire. "Whenever you're ready" has to mean it,
        # or deferring is just a slower way of losing the brief.
        if (not c["targeted"] and MAX_AGE_DAYS
                and age is not None and age > MAX_AGE_DAYS * 86400):
            stale.append(c)
        else:
            fresh.append(c)

    for c in stale:
        try:
            flip(c, "expired")
        except Exception:
            pass

    if not fresh:
        newest = stale[0]
        emit(message=("Handoff brief for this directory is older than %d days "
                      "— not loaded. Read it yourself if it still matters: %s"
                      % (MAX_AGE_DAYS, newest["path"])))
        return

    top = fresh[0]
    # Superseding only makes sense between peers: two briefs for one directory,
    # where injecting the newer really does obsolete the older. A reservation is
    # not a peer of anything. So a reserved brief supersedes nothing (the cwd
    # briefs here are still owed to whoever starts plainly in this directory),
    # and nothing reserved is ever superseded — it is owed to its own session.
    others = [] if top["targeted"] else [c for c in fresh[1:] if not c["targeted"]]
    meta = top["meta"]
    body = split_body(top["text"]).rstrip()
    if len(body) > MAX_BODY:
        cut = body[:MAX_BODY]
        nl = cut.rfind("\n")
        body = (cut[:nl] if nl > 0 else cut).rstrip()
        body += "\n\n[truncated — full brief: %s]" % top["path"]

    age = human_age(top["age"])
    extra = ""
    if others:
        extra = ("\n[superseded: %d older pending brief%s for this directory "
                 "%s marked superseded]\n"
                 % (len(others), "" if len(others) == 1 else "s",
                    "was" if len(others) == 1 else "were"))

    emit(
        context=FRAME.format(
            path=top["path"],
            written=meta.get("created", "unknown"),
            age=age,
            where=(meta.get("cwd") or "an unrecorded directory"),
            replaces=REPLACES_HOOK,
            sid=(payload.get("session_id") or "unknown"),
            transcript=(meta.get("transcript") or "unknown"),
            extra=extra,
            body=body,
        ),
        message=("Handoff loaded: %s (written %s ago). Source: %s"
                 % (meta.get("title") or os.path.basename(top["path"]),
                    age, top["path"])),
    )

    # Print first, consume second. If the status write fails the brief has
    # already been injected and will re-inject next start: at-least-once is
    # deliberate, because a duplicated brief is an annoyance and a lost one is
    # the failure this whole thing exists to prevent.
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        flip(top, "consumed", [
            ("consumed", stamp),
            ("consumed_by", payload.get("session_id") or "unknown"),
        ])
    except Exception:
        pass
    for c in others:
        try:
            flip(c, "superseded")
        except Exception:
            pass


def precompact(payload):
    sys.stdout.write(json.dumps({
        "systemMessage": (
            "Auto-compact fired — context was full. /handoff instead writes a "
            "structured brief (decisions, next steps, dead ends) for a fresh "
            "session."
        )
    }))


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

USAGE = """usage:
  handoff_load.py                 SessionStart hook (reads a JSON payload on stdin)
  handoff_load.py --precompact    PreCompact hook
  handoff_load.py --list          list pending handoff briefs
  handoff_load.py --take <what>   print one brief and mark it consumed
"""


def open_store():
    """(store, entries, errors) — or an int exit code the caller should return.

    The two CLI modes start the same way: resolve the store, scan it, and tell
    "empty" apart from "unreadable" — the distinction the hook itself does not
    need, because it is allowed to treat both as "nothing to do".
    """
    store = store_dir()
    try:
        entries, errors = scan(store)
    except (FileNotFoundError, NotADirectoryError):
        sys.stdout.write("No handoff briefs parked.\n")
        return 0
    except Exception as exc:
        sys.stderr.write("cannot read the handoff store at %s: %s\n" % (store, exc))
        return 2
    return store, entries, errors


def cmd_list(argv):
    """Print every pending brief, newest first. Read-only: no lock, no flip."""
    result = open_store()
    if isinstance(result, int):
        return result
    _store, entries, errors = result

    pending = [c for c in entries if c["status"] == "pending"]
    if not pending:
        sys.stdout.write("No handoff briefs parked.\n")
    else:
        # Newest first, full stop — unlike candidates(), a list is not choosing
        # one, so a reservation gets no special rank here.
        pending.sort(key=lambda c: (c["order"], c["name"]), reverse=True)
        now = time.time()
        n = len(pending)
        lines = ["%d handoff brief%s parked." % (n, "" if n == 1 else "s"), ""]
        for i, c in enumerate(pending, 1):
            meta = c["meta"]
            title = meta.get("title") or c["name"]
            age_seconds = (now - c["created"]) if c["created"] is not None else None
            cwd = meta.get("cwd") or "an unrecorded directory"
            lines.append("%d. %s" % (i, title))
            lines.append("   parked %s ago · %s · %s"
                         % (human_age(age_seconds), cwd, c["name"]))
            if c["target"]:
                lines.append("   claude --session-id %s" % c["target"])
            else:
                third = "loads when you next run claude in that directory"
                if (MAX_AGE_DAYS and c["created"] is not None
                        and age_seconds > MAX_AGE_DAYS * 86400):
                    third += " (expires on next start there)"
                lines.append("   %s" % third)
            lines.append("")
        lines.append("Resume one here with /handoffs <number>.")
        sys.stdout.write("\n".join(lines) + "\n")

    if errors:
        names = ", ".join(name for name, _ in errors)
        sys.stderr.write("warning: %d file(s) in the store could not be read: %s\n"
                         % (len(errors), names))
    return 0


def resolve_selector(entries, pending, selector):
    """(matches, digit_error) against scan() results — never against the disk.

    `digit_error`, when set, is the exact out-of-range message to report; an
    empty `matches` with no `digit_error` means the generic "no match" case.
    """
    name_matches = [c for c in entries if c["name"] == selector]
    if name_matches:
        return name_matches, None
    if selector.isdigit():
        n = int(selector)
        if 1 <= n <= len(pending):
            return [pending[n - 1]], None
        if not pending:
            return [], "nothing is parked."
        return [], "there is no brief %d — %d parked." % (n, len(pending))
    want = selector.strip().lower()
    return [c for c in entries if c["target"] == want], None


def cmd_take(argv):
    """Print one pending brief here and mark it consumed. Loud on every failure."""
    if not argv:
        sys.stderr.write(USAGE)
        return 2
    selector = argv[0]
    # Resolution only ever looks selector up against scan() results — never by
    # joining it onto a path — so nothing outside the store is reachable.
    if not selector or "/" in selector or ".." in selector:
        sys.stderr.write('refusing selector "%s": give a brief name, a session '
                         'id, or a list number\n' % selector)
        return 2

    store = store_dir()
    # The lock comes before resolution so this cannot race a session starting.
    if not take_lock(store):
        sys.stderr.write("another session is loading a brief right now — "
                         "try again in a moment\n")
        return 2

    try:
        entries, _errors = scan(store)
    except (FileNotFoundError, NotADirectoryError):
        # Deliberately not open_store()'s exit-0 "nothing parked" shape: a take
        # that took nothing must not exit 0.
        sys.stderr.write('nothing matches "%s". Run /handoffs to see what is '
                         'parked.\n' % selector)
        return 2

    pending = [c for c in entries if c["status"] == "pending"]
    pending.sort(key=lambda c: (c["order"], c["name"]), reverse=True)

    matches, digit_error = resolve_selector(entries, pending, selector)
    if digit_error:
        sys.stderr.write(digit_error + "\n")
        return 2
    if not matches:
        sys.stderr.write('nothing matches "%s". Run /handoffs to see what is '
                         'parked.\n' % selector)
        return 2
    if len(matches) > 1:
        names = ", ".join(c["name"] for c in matches)
        sys.stderr.write('"%s" matches %d briefs: %s. Name one exactly.\n'
                         % (selector, len(matches), names))
        return 2

    entry = matches[0]
    meta = entry["meta"]
    if entry["status"] != "pending":
        msg = "%s is already %s" % (entry["name"], entry["status"])
        if meta.get("consumed") and meta.get("consumed_by"):
            msg += " — consumed %s by %s" % (meta["consumed"], meta["consumed_by"])
        sys.stderr.write(msg + "\n")
        return 2

    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or "in-session"
    age = human_age(time.time() - entry["created"] if entry["created"] is not None else None)

    # Same truncation the hook applies at line ~468 — copied, not shared, because
    # factoring it out would mean a third edit inside session_start().
    body = split_body(entry["text"]).rstrip()
    if len(body) > MAX_BODY:
        cut = body[:MAX_BODY]
        nl = cut.rfind("\n")
        body = (cut[:nl] if nl > 0 else cut).rstrip()
        body += "\n\n[truncated — full brief: %s]" % entry["path"]

    # Print first, consume second — the hook's comment at line ~496 explains why
    # and applies here unchanged: at-least-once is deliberate.
    sys.stdout.write(FRAME.format(
        path=entry["path"],
        written=meta.get("created", "unknown"),
        age=age,
        where=(meta.get("cwd") or "an unrecorded directory"),
        replaces=REPLACES_IN_SESSION,
        sid=sid,
        transcript=(meta.get("transcript") or "unknown"),
        extra="",
        body=body,
    ))
    sys.stdout.write("\n")
    sys.stderr.write("Handoff loaded: %s (written %s ago). Source: %s\n"
                     % (meta.get("title") or entry["name"], age, entry["path"]))

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        flip(entry, "consumed", [("consumed", stamp), ("consumed_by", sid)])
    except Exception as exc:
        sys.stderr.write("warning: the brief was loaded but could not be marked "
                         "consumed (%s) — it will load again\n" % exc)

    if entry["target"]:
        sys.stderr.write("the reservation for claude --session-id %s is now "
                         "spent\n" % entry["target"])

    return 0


def cli(argv):
    """The interactive modes. Unlike the hook, these are allowed to fail loudly."""
    try:
        if argv[0] in ("--help", "-h"):
            sys.stdout.write(USAGE)
            return 0
        if argv[0] == "--list":
            return cmd_list(argv[1:])
        return cmd_take(argv[1:])
    except Exception as exc:
        if os.environ.get("HANDOFF_DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)
        sys.stderr.write("handoff_load.py: %s\n" % exc)
        return 2


def main():
    argv = sys.argv[1:]
    # argv before stdin: a CLI run from a terminal must not block on read().
    if argv and argv[0] in ("--list", "--take", "--help", "-h"):
        return cli(argv)
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    try:
        if "--precompact" in argv:
            precompact(payload)
        else:
            session_start(payload)
    except Exception:
        if os.environ.get("HANDOFF_DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
