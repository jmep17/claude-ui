#!/usr/bin/env python3
"""SessionStart loader for /handoff briefs (plus a PreCompact nudge).

A handoff brief is what the /handoff skill writes at the end of a session:
decisions, pointers and a first action, aimed at a fresh session that has no
transcript. This script is the other half — it finds the pending brief a new
session should receive, injects it, and marks it consumed so it never fires
twice. It is also the writer: `--new` builds and writes the brief file itself
(grouped by repository under the store), and `--facts` prints the git/disk
state a brief's "Changed on disk" section needs, so the model composing a
brief never has to run its own git batch.

Two ways a brief finds its session. A brief with `target: session:<uuid>` is
*reserved*: it loads into the session started under that id (`claude
--session-id <uuid>`) and into no other, from any directory and after any
delay. A brief without one falls back to the original rule — the next session
started in its `cwd`. Reserved is the deliberate case and outranks the default.

Grouping is cosmetic. Briefs live under `<store>/<repo-basename>/` once
written by `--new`, and legacy flat briefs at the store root keep working
forever — `match()`, `candidates()` and delivery never consult a brief's
group. `INDEX.md` files are generated, write-only, and never read back by
this script.

Python 3.9 stdlib only (/usr/bin/python3 is 3.9.6 on this machine), and it must
never take down a session start: every failure path exits 0.
"""

import calendar
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid

MAX_AGE_DAYS = 14          # older briefs surface as a path instead of injecting; 0 disables
MAX_BODY = 24 * 1024       # injected body cap, truncated at a line boundary
MAX_FILE = 512 * 1024      # files larger than this are not even read

INDEX_NAME = "INDEX.md"
WORKTREE_MARKER = "/.claude/worktrees/"
MAX_DIFF_FILES = 20

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
TARGET_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


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


def _is_index(name):
    return name.lower() == INDEX_NAME.lower()


def _walk(store, errors):
    """Yield store-relative *.md paths, one level of grouping deep.

    The root `os.listdir(store)` is uncaught — that is what lets `scan()`'s
    caller tell "empty" from "unreadable" apart (the hook treats both as
    "nothing to do"; `--list`/`--take` need to tell them apart). Everything
    below the root is best-effort: an unreadable group directory becomes an
    `errors` entry, never an exception, and a symlinked "group" directory is
    skipped outright — `isdir` follows symlinks, and a symlinked group could
    otherwise read `.md` files from outside the store.
    """
    names = sorted(os.listdir(store))
    for name in names:
        if _is_index(name):
            continue
        path = os.path.join(store, name)
        if name.endswith(".md"):
            try:
                if os.path.isfile(path):
                    yield name
            except OSError:
                pass
            continue
        if name.startswith("."):
            continue
        try:
            is_link = os.path.islink(path)
            is_dir = os.path.isdir(path)
        except OSError:
            continue
        if is_link or not is_dir:
            continue
        try:
            inner_names = sorted(os.listdir(path))
        except Exception as exc:
            errors.append((name, str(exc)))
            continue
        for inner in inner_names:
            if _is_index(inner) or not inner.endswith(".md"):
                continue
            inner_path = os.path.join(path, inner)
            try:
                if os.path.isfile(inner_path):
                    yield "%s/%s" % (name, inner)
            except OSError:
                pass


def scan(store):
    """Every *.md in the store, one level of grouping, no filter, no sort.

    (entries, errors). This is the one place that reads the store off disk.
    `candidates()`, and the CLI modes in the `# cli` section below, all filter
    and order what this returns — they never read the directory themselves.
    That is what keeps `--list` and the hook from ever disagreeing about what
    is parked.

    `os.listdir(store)` itself is allowed to raise here — deliberately not
    caught (see `_walk`'s docstring).
    """
    entries = []
    errors = []
    for rel in _walk(store, errors):
        path = os.path.join(store, rel)
        base = os.path.basename(rel)
        group = rel.rsplit("/", 1)[0] if "/" in rel else ""
        try:
            st = os.stat(path)
            if not os.path.isfile(path) or st.st_size > MAX_FILE:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as exc:
            errors.append((rel, str(exc)))
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
            "name": rel,
            "base": base,
            "group": group,
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
    out.sort(key=lambda c: (c["targeted"], c["order"], c["base"]),
             reverse=True)
    return out


def pending_count(store):
    """Pending briefs left in the store — the number the nag line reports."""
    try:
        entries, _ = scan(store)
    except Exception:
        return 0
    return sum(1 for c in entries if c["status"] == "pending")


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
    entry["text"] = text


# --------------------------------------------------------------------------
# grouping — which repository a cwd belongs to, and which store directory
# --------------------------------------------------------------------------

def _git(args, cwd, timeout=5):
    """(rc, stdout stripped). stderr is discarded — a failed or timed-out
    command is itself a fact, never retried."""
    try:
        proc = subprocess.run(
            ["git"] + list(args), cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True)
    except Exception:
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def root_from_path(p):
    """The main working tree for `p` — the group key.

    1. `p` a directory: `git rev-parse --git-common-dir` with cwd=p. The
       output is usually relative (`.git`, `../.git`), so it is resolved
       against `p` and then its parent taken — that parent is the main tree
       even when `p` itself is a linked worktree.
    2. Otherwise (or if that failed): truncate at `/.claude/worktrees/` if
       present — the same convention `match()`'s docstring already encodes,
       and what makes `--migrate` work when the worktree is gone.
    3. Otherwise: `p` itself.
    """
    try:
        is_dir = os.path.isdir(p)
    except Exception:
        is_dir = False
    if is_dir:
        rc, out = _git(["rev-parse", "--git-common-dir"], cwd=p)
        if rc == 0 and out:
            try:
                gcd = os.path.realpath(os.path.join(p, out))
                return os.path.dirname(gcd)
            except Exception:
                pass
    idx = p.find(WORKTREE_MARKER)
    if idx != -1:
        return p[:idx]
    return p


def group_name(root):
    """Sanitize a main-tree root into a store directory name."""
    try:
        base = os.path.basename(os.path.realpath(root))
    except Exception:
        base = os.path.basename(root)
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base or "")
    base = base.lstrip(".")
    base = base[:60]
    return base or "root"


def group_dir_for(store, root, entries):
    """The store directory name for `root` — matched by inspection, never
    recorded. A group dir named `base` or `base-*` whose briefs carry a
    `root:` matching this one (compared by realpath) is reused; otherwise the
    first free name of `base`, `<parent>-<base>`, `<base>-<hash8>` is picked.
    Zero extra I/O beyond the `entries` already scanned."""
    base = group_name(root)
    try:
        target_root = os.path.realpath(root)
    except Exception:
        target_root = root

    by_group = {}
    for e in entries:
        g = e.get("group")
        if g:
            by_group.setdefault(g, []).append(e)

    for g, es in by_group.items():
        if g != base and not g.startswith(base + "-"):
            continue
        for e in es:
            r = e["meta"].get("root")
            if not r:
                continue
            try:
                same = os.path.realpath(r) == target_root
            except Exception:
                same = r == root
            if same:
                return g

    existing = set(by_group.keys())
    if base not in existing:
        return base
    parent = os.path.basename(os.path.dirname(target_root.rstrip("/"))) or "parent"
    parent = re.sub(r"[^A-Za-z0-9._-]+", "-", parent).lstrip(".") or "parent"
    alt = "%s-%s" % (parent, base)
    if alt not in existing:
        return alt
    h = hashlib.sha1(target_root.encode("utf-8", "replace")).hexdigest()[:8]
    return "%s-%s" % (base, h)


# --------------------------------------------------------------------------
# indexes — generated, write-only, never read back by this script
# --------------------------------------------------------------------------

def _write_if_changed(path, content):
    """Write only when the content differs byte-for-byte. temp + os.replace
    in the same directory, so a reader never sees a partial file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == content:
                return False
    except Exception:
        pass
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def _index_header(hookpath):
    return ("> generated by handoff_load.py — run `python3 %s --list` "
            "instead of reading this\n\n" % hookpath)


def reindex(store, deadline=None):
    """Build the root index and each group index. Prints nothing, ever.

    Returns (written, total_groups). A store-read failure propagates — the
    `--reindex` CLI mode wants that loud; `session_start`'s call site wraps
    this in its own try/except so a hook never sees it. `deadline`, when
    given, is an epoch time after which remaining group indexes are left
    stale rather than risk a hook timeout.
    """
    hookpath = os.path.abspath(__file__)
    entries, _ = scan(store)

    groups = {}
    flat_total = flat_pending = 0
    for e in entries:
        g = e["group"]
        if not g:
            flat_total += 1
            if e["status"] == "pending":
                flat_pending += 1
            continue
        gg = groups.setdefault(g, {"total": 0, "pending": 0, "root": None, "entries": []})
        gg["total"] += 1
        if e["status"] == "pending":
            gg["pending"] += 1
        if gg["root"] is None and e["meta"].get("root"):
            gg["root"] = e["meta"]["root"]
        gg["entries"].append(e)

    header = _index_header(hookpath)
    written = 0

    root_lines = [header, "# Handoff groups\n\n"]
    if not groups and not flat_total:
        root_lines.append("No groups.\n")
    for name in sorted(groups):
        gg = groups[name]
        root_lines.append("- **%s** — %s — %d total, %d pending\n"
                           % (name, gg["root"] or "unknown", gg["total"], gg["pending"]))
    if flat_total:
        root_lines.append("- **(ungrouped)** — %d total, %d pending\n"
                           % (flat_total, flat_pending))
    if _write_if_changed(os.path.join(store, INDEX_NAME), "".join(root_lines)):
        written += 1

    for name, gg in groups.items():
        if deadline is not None and time.time() > deadline:
            break
        gdir = os.path.join(store, name)
        if not os.path.isdir(gdir):
            continue
        lines = [header, "# %s\n\n" % name]
        rows = sorted(gg["entries"], key=lambda e: (e["order"], e["base"]), reverse=True)
        for e in rows:
            meta = e["meta"]
            tgt = (" — target: %s" % e["target"]) if e["target"] else ""
            lines.append("- %s — %s — %s — `%s`%s\n"
                         % (meta.get("created", "unknown"), e["status"],
                            meta.get("title") or e["base"], e["base"], tgt))
        try:
            if _write_if_changed(os.path.join(gdir, INDEX_NAME), "".join(lines)):
                written += 1
        except Exception:
            continue

    return written, len(groups)


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

    flipped = False
    for c in stale:
        try:
            flip(c, "expired")
            flipped = True
        except Exception:
            pass

    if not fresh:
        newest = stale[0]
        emit(message=("Handoff brief for this directory is older than %d days "
                      "— not loaded. Read it yourself if it still matters: %s"
                      % (MAX_AGE_DAYS, newest["path"])))
        if flipped:
            try:
                reindex(store, deadline=time.time() + 2.0)
            except Exception:
                pass
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
        flipped = True
    except Exception:
        pass
    for c in others:
        try:
            flip(c, "superseded")
            flipped = True
        except Exception:
            pass

    if flipped:
        try:
            reindex(store, deadline=time.time() + 2.0)
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
# facts — the git/disk state a brief's "Changed on disk" section needs
# --------------------------------------------------------------------------

def _transcript_path(cwd):
    """Prefer the transcript named for this exact session, fall back to the
    newest .jsonl in this cwd's project directory, then "unknown"."""
    cfg = config_dir()
    slug = re.sub(r"[/.]", "-", cwd)
    proj_dir = os.path.join(cfg, "projects", slug)
    sess = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sess:
        p = os.path.join(proj_dir, "%s.jsonl" % sess)
        if os.path.isfile(p):
            return p
    try:
        names = [n for n in os.listdir(proj_dir) if n.endswith(".jsonl")]
    except Exception:
        return "unknown"
    if not names:
        return "unknown"
    names.sort(key=lambda n: os.stat(os.path.join(proj_dir, n)).st_mtime, reverse=True)
    return os.path.join(proj_dir, names[0])


def _disk_bullets(cwd):
    """The '## Changed on disk' section as a list of lines, or None outside a
    git repo. Shared by `--facts` and `--new` (which splices this verbatim in
    place of the placeholder heading the model wrote), so the two can never
    disagree."""
    rc, _ = _git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0:
        return None

    lines = ["## Changed on disk"]

    rc_b, branch = _git(["branch", "--show-current"], cwd)
    branch = branch if (rc_b == 0 and branch) else None
    if branch:
        rc_a, ahead_out = _git(["rev-list", "--count", "@{u}..HEAD"], cwd)
        rc_be, behind_out = _git(["rev-list", "--count", "HEAD..@{u}"], cwd)
        if rc_a == 0 and rc_be == 0:
            ahead = ahead_out.strip() or "0"
            behind = behind_out.strip() or "0"
            pushed = "pushed" if behind == "0" else "unpushed"
            lines.append("- Branch: `%s` — %s ahead, %s" % (branch, ahead, pushed))
        else:
            lines.append("- Branch: `%s` — no upstream" % branch)
    else:
        lines.append("- Branch: detached HEAD")

    rc_l, log_out = _git(["log", "--oneline", "-15"], cwd)
    commits = log_out.splitlines() if rc_l == 0 and log_out else []
    if commits:
        lines.append("- Commits this session (last 15):")
        for c in commits:
            lines.append("  `%s`" % c)
    else:
        lines.append("- Commits: none")

    rc_s, status_out = _git(["status", "--porcelain=v1"], cwd)
    paths = status_out.splitlines() if rc_s == 0 and status_out else []
    if paths:
        lines.append("- Uncommitted:")
        for p in paths:
            lines.append("  `%s`" % p)
    else:
        lines.append("- Uncommitted: clean")

    rc_st, stash_out = _git(["stash", "list"], cwd)
    stashes = stash_out.splitlines() if rc_st == 0 and stash_out else []
    lines.append("- Stashes: %s" % ("; ".join(stashes) if stashes else "none"))

    return lines


def cmd_facts(argv):
    """CLI-only, never on the hook path. An information superset of the old
    ten-command Gather batch, compressed rather than truncated."""
    cwd = os.getcwd()
    i = 0
    while i < len(argv):
        if argv[i] == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]
            i += 2
        else:
            sys.stderr.write("unknown argument: %s\n" % argv[i])
            return 2
    cwd = os.path.realpath(cwd)

    bullets = _disk_bullets(cwd)
    if bullets is None:
        sys.stdout.write("Not a git repository: %s\n" % cwd)
        return 0

    lines = list(bullets)

    rc_d, diff_out = _git(["diff", "--stat", "HEAD"], cwd)
    diff_lines = diff_out.splitlines() if rc_d == 0 and diff_out else []
    if diff_lines:
        lines.append("")
        lines.append("### git diff --stat")
        for d in diff_lines[:MAX_DIFF_FILES]:
            lines.append(d)
        if len(diff_lines) > MAX_DIFF_FILES:
            lines.append("... +%d more" % (len(diff_lines) - MAX_DIFF_FILES))

    lines.append("")
    lines.append("Transcript: %s" % _transcript_path(cwd))

    sys.stdout.write("\n".join(lines) + "\n")
    return 0


# --------------------------------------------------------------------------
# --new — the script writes the brief
# --------------------------------------------------------------------------

SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(title):
    s = SLUG_RE.sub("-", (title or "").lower()).strip("-")
    if len(s) > 40:
        s = s[:40]
        s = s.rsplit("-", 1)[0] if "-" in s else s
    return s or "brief"


def _first_step(body):
    in_section = False
    for line in body.splitlines():
        if line.strip() == "## Next steps":
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            m = re.match(r"^\s*1\.\s+(.*)$", line)
            if m:
                return m.group(1).strip()
    return None


def _splice_section(text, heading, section_lines):
    """Replace the body under `heading` (up to the next '## ' heading) with
    `section_lines`. If `heading` is not present, `text` is returned unchanged."""
    lines = text.splitlines()
    out = []
    i = 0
    replaced = False
    while i < len(lines):
        if not replaced and lines[i].strip() == heading:
            out.extend(section_lines)
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            replaced = True
            continue
        out.append(lines[i])
        i += 1
    nl = "\n"
    trailing = text.endswith("\n")
    return nl.join(out) + (nl if trailing else "")


def _build_frontmatter(title, target_uuid, cwd, repo, root, branch, created, transcript):
    """Build the frontmatter block key by key through set_frontmatter_key, so
    a bad title (control characters) raises the same ValueError it always
    would — never a hand-formatted line that skips validation."""
    text = "---\n---\n"
    text = set_frontmatter_key(text, "title", title)
    if target_uuid:
        text = set_frontmatter_key(text, "target", "session:%s" % target_uuid)
    text = set_frontmatter_key(text, "cwd", cwd)
    if repo:
        text = set_frontmatter_key(text, "repo", repo)
    if root:
        text = set_frontmatter_key(text, "root", root)
    text = set_frontmatter_key(text, "branch", branch)
    text = set_frontmatter_key(text, "created", created)
    text = set_frontmatter_key(text, "transcript", transcript)
    text = set_frontmatter_key(text, "status", "pending")
    return text


def cmd_new(argv):
    title = None
    body_file = None
    target_mode = "auto"     # "auto" -> generate; "" -> --no-target; else literal
    cwd = os.getcwd()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--title" and i + 1 < len(argv):
            title = argv[i + 1]; i += 2
        elif a == "--body-file" and i + 1 < len(argv):
            body_file = argv[i + 1]; i += 2
        elif a == "--target" and i + 1 < len(argv):
            target_mode = argv[i + 1]; i += 2
        elif a == "--no-target":
            target_mode = ""; i += 1
        elif a == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]; i += 2
        else:
            sys.stderr.write("unknown argument: %s\n" % a)
            return 2

    if not title or not title.strip():
        sys.stderr.write("--title is required\n")
        return 2

    if not body_file:
        if sys.stdin.isatty():
            sys.stderr.write("--body-file is required (a path, or - for stdin)\n")
            return 2
        body_file = "-"

    if body_file == "-":
        try:
            body = sys.stdin.read()
        except Exception:
            body = ""
    else:
        try:
            with open(body_file, "r", encoding="utf-8") as fh:
                body = fh.read()
        except Exception as exc:
            sys.stderr.write("could not read --body-file: %s\n" % exc)
            return 2

    if not body.strip():
        sys.stderr.write("the body is empty\n")
        return 2

    if target_mode == "auto":
        target_uuid = str(uuid.uuid4())
    elif target_mode == "":
        target_uuid = None
    else:
        if not TARGET_UUID_RE.match(target_mode.strip()):
            sys.stderr.write("--target must be a uuid\n")
            return 2
        target_uuid = target_mode.strip().lower()

    cwd = os.path.realpath(cwd)
    store = store_dir()

    try:
        os.makedirs(store, mode=0o700, exist_ok=True)
    except Exception as exc:
        sys.stderr.write("could not create the store: %s\n" % exc)
        return 2

    if not take_lock(store):
        sys.stderr.write("another session is loading a brief right now — "
                         "try again in a moment\n")
        return 2

    try:
        entries, _ = scan(store)
    except Exception:
        entries = []

    root = root_from_path(cwd)
    group = group_dir_for(store, root, entries)

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    name_stamp = time.strftime("%Y-%m-%d-%H%M")
    slug = _slugify(title)

    rc_repo, repo = _git(["rev-parse", "--show-toplevel"], cwd)
    repo = repo if (rc_repo == 0 and repo) else None
    rc_br, branch = _git(["branch", "--show-current"], cwd)
    branch_val = branch if (rc_br == 0 and branch) else "detached"
    transcript = _transcript_path(cwd)

    try:
        frontmatter = _build_frontmatter(
            title.strip(), target_uuid, cwd, repo, root, branch_val, stamp, transcript)
    except ValueError as exc:
        sys.stderr.write("bad title: %s\n" % exc)
        return 2

    full_text = frontmatter + body.rstrip("\n") + "\n"
    disk_bullets = _disk_bullets(cwd)
    if disk_bullets and "## Changed on disk" in full_text:
        full_text = _splice_section(full_text, "## Changed on disk", disk_bullets)

    gdir = os.path.join(store, group)
    try:
        os.makedirs(gdir, mode=0o700, exist_ok=True)
    except Exception as exc:
        sys.stderr.write("could not create the group directory: %s\n" % exc)
        return 2

    final_path = None
    for suffix in [""] + ["-%d" % n for n in range(2, 10)]:
        candidate = os.path.join(gdir, "%s-%s%s.md" % (name_stamp, slug, suffix))
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        except Exception as exc:
            sys.stderr.write("could not write the brief: %s\n" % exc)
            return 2
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(full_text)
        except Exception as exc:
            try:
                os.unlink(candidate)
            except Exception:
                pass
            sys.stderr.write("could not write the brief: %s\n" % exc)
            return 2
        final_path = candidate
        break
    if final_path is None:
        sys.stderr.write("could not find a free filename after 9 attempts\n")
        return 2

    try:
        reindex(store)
    except Exception:
        pass

    first_step = _first_step(body) or "see Next steps in the brief"

    sys.stdout.write("Handoff written: %s\n" % title.strip())
    sys.stdout.write("  %s\n\n" % final_path)
    if target_uuid:
        sys.stdout.write("Parked for a session you choose. Continue it any "
                         "time, from anywhere:\n")
        sys.stdout.write("  claude --session-id %s\n\n" % target_uuid)
    else:
        sys.stdout.write("Loads when you next run claude in %s.\n\n" % cwd)
    sys.stdout.write("First step: %s\n" % first_step)
    return 0


# --------------------------------------------------------------------------
# --migrate — flat briefs at the store root only
# --------------------------------------------------------------------------

def _migrate_root_for(meta):
    for key in ("root", "repo", "cwd"):
        v = meta.get(key)
        if v:
            r = root_from_path(v)
            if r:
                return r
    return None


def cmd_migrate(argv):
    apply_ = False
    for a in argv:
        if a == "--apply":
            apply_ = True
        else:
            sys.stderr.write("unknown argument: %s\n" % a)
            return 2

    store = store_dir()
    try:
        entries, errors = scan(store)
    except Exception as exc:
        sys.stderr.write("cannot read the handoff store at %s: %s\n" % (store, exc))
        return 2

    flat = [e for e in entries if not e["group"]]
    if not flat:
        sys.stdout.write("nothing to migrate.\n")
        return 0

    if apply_ and not take_lock(store):
        sys.stderr.write("another session is loading a brief right now — "
                         "try again in a moment\n")
        return 2

    lines = []
    moved = 0
    for e in sorted(flat, key=lambda e: e["base"]):
        root = _migrate_root_for(e["meta"])
        if not root:
            lines.append("%s (stays: no repo/cwd recorded)" % e["base"])
            continue
        group = group_dir_for(store, root, entries)
        dest_dir = os.path.join(store, group)
        if not apply_:
            lines.append("%s -> %s/%s" % (e["base"], group, e["base"]))
            continue
        try:
            os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        except Exception as exc:
            lines.append("%s: could not create %s (%s)" % (e["base"], group, exc))
            continue
        final = os.path.join(dest_dir, e["base"])
        if os.path.exists(final):
            stem = e["base"][:-3] if e["base"].endswith(".md") else e["base"]
            for n in range(2, 10):
                cand = os.path.join(dest_dir, "%s-%d.md" % (stem, n))
                if not os.path.exists(cand):
                    final = cand
                    break
            else:
                lines.append("%s: no free name in %s" % (e["base"], group))
                continue
        try:
            text = set_frontmatter_key(e["text"], "root", root)
        except ValueError:
            text = e["text"]
        try:
            tmp = "%s.tmp.%d" % (e["path"], os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, e["path"])
            os.replace(e["path"], final)
        except Exception as exc:
            lines.append("%s: move failed (%s)" % (e["base"], exc))
            continue
        moved += 1
        lines.append("%s -> %s/%s" % (e["base"], group, os.path.basename(final)))
        e["group"] = group
        e["meta"]["root"] = root

    for name, exc in errors:
        lines.append("warning: %s could not be read (%s)" % (name, exc))

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.write("\n%d flat brief%s, %d %s.\n" % (
        len(flat), "" if len(flat) == 1 else "s",
        moved, "moved" if apply_ else "would move"))

    if apply_ and moved:
        try:
            reindex(store)
        except Exception:
            pass
    return 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

USAGE = """usage:
  handoff_load.py                 SessionStart hook (reads a JSON payload on stdin)
  handoff_load.py --precompact    PreCompact hook
  handoff_load.py --list          list pending handoff briefs
  handoff_load.py --take <what>   print one brief and mark it consumed
  handoff_load.py --new --title <text> --body-file <path>|- [--target <uuid>|--no-target] [--cwd <path>]
                                   write a new handoff brief
  handoff_load.py --facts [--cwd <path>]
                                   print the git/disk state a brief needs
  handoff_load.py --reindex       rebuild the store's INDEX.md files
  handoff_load.py --groups        list groups and their brief counts
  handoff_load.py --migrate [--apply]
                                   move flat root briefs into their repo's group
"""

CLI_FLAGS = ("--help", "-h", "--list", "--take", "--new", "--facts",
             "--reindex", "--groups", "--migrate")


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
        pending.sort(key=lambda c: (c["order"], c["base"]), reverse=True)
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


def cmd_groups(argv):
    """Read-only, no lock: one line per group plus (ungrouped) for flat briefs."""
    result = open_store()
    if isinstance(result, int):
        return result
    _store, entries, errors = result

    groups = {}
    ungrouped = 0
    for e in entries:
        g = e["group"]
        if not g:
            ungrouped += 1
            continue
        gg = groups.setdefault(g, [0, 0])
        gg[0] += 1
        if e["status"] == "pending":
            gg[1] += 1

    if not groups and not ungrouped:
        sys.stdout.write("no groups.\n")
    else:
        lines = []
        for name in sorted(groups):
            total, pending = groups[name]
            lines.append("%s — %d total, %d pending" % (name, total, pending))
        if ungrouped:
            lines.append("(ungrouped) — %d total" % ungrouped)
        sys.stdout.write("\n".join(lines) + "\n")

    if errors:
        names = ", ".join(name for name, _ in errors)
        sys.stderr.write("warning: %d entries could not be read: %s\n"
                         % (len(errors), names))
    return 0


def cmd_reindex(argv):
    """Loud, meaningful exit code — the interactive counterpart of the
    best-effort reindex the hook and `--take` do on the side."""
    store = store_dir()
    if not take_lock(store):
        sys.stderr.write("another session is loading a brief right now — "
                         "try again in a moment\n")
        return 2
    try:
        written, total = reindex(store)
    except Exception as exc:
        sys.stderr.write("could not reindex: %s\n" % exc)
        return 2
    sys.stdout.write("reindexed: %d/%d group index(es) updated\n" % (written, total))
    return 0


def resolve_selector(entries, pending, selector):
    """(matches, digit_error) against scan() results — never against the disk.

    Four ordered tiers: exact store-relative name, list digit, exact
    basename, target uuid. Tiered, not unioned, so a bare number can never be
    read as a basename.

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
    base_matches = [c for c in entries if c["base"] == selector]
    if base_matches:
        return base_matches, None
    want = selector.strip().lower()
    return [c for c in entries if c["target"] == want], None


def _bad_selector(selector):
    """Refuse anything that could reach outside the store. Resolution still
    never joins the selector onto a path — this only rejects the shapes that
    look like an attempt to."""
    if not selector:
        return True
    if ".." in selector:
        return True
    if selector[0] in "/\\":
        return True
    if selector.count("/") > 1:
        return True
    for part in selector.replace("\\", "/").split("/"):
        if part.startswith("."):
            return True
    return False


def cmd_take(argv):
    """Print one pending brief here and mark it consumed. Loud on every failure."""
    if not argv:
        sys.stderr.write(USAGE)
        return 2
    selector = argv[0]
    if _bad_selector(selector):
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
    pending.sort(key=lambda c: (c["order"], c["base"]), reverse=True)

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
    else:
        try:
            reindex(store)
        except Exception as exc2:
            sys.stderr.write("warning: could not update the index (%s)\n" % exc2)

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
        if argv[0] == "--take":
            return cmd_take(argv[1:])
        if argv[0] == "--new":
            return cmd_new(argv[1:])
        if argv[0] == "--facts":
            return cmd_facts(argv[1:])
        if argv[0] == "--reindex":
            return cmd_reindex(argv[1:])
        if argv[0] == "--groups":
            return cmd_groups(argv[1:])
        if argv[0] == "--migrate":
            return cmd_migrate(argv[1:])
        if argv[0].startswith("--"):
            sys.stderr.write(USAGE)
            return 2
        return cmd_take(argv)
    except Exception as exc:
        if os.environ.get("HANDOFF_DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)
        sys.stderr.write("handoff_load.py: %s\n" % exc)
        return 2


def main():
    argv = sys.argv[1:]
    # argv before stdin: a CLI run from a terminal must not block on read().
    if argv and argv[0] in CLI_FLAGS:
        return cli(argv)
    if argv and argv[0].startswith("--") and argv[0] != "--precompact":
        sys.stderr.write(USAGE)
        return 2
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
