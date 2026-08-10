"""Marketplaces and plugins for one project, through Claude Code's own CLI.

Everywhere else this app reads and writes config files directly, because the
filesystem is the state and a file is legible without us. Marketplaces are the
one place that would not work: adding one clones a repository, installing a
plugin resolves a version and unpacks it into a cache, and both decisions are
Claude Code's to make and to revise. Reimplementing them here would mean
owning a downloader, a trust model and a cache layout that belong to something
else and change without us — so this module shells out and reports what came
back, verbatim.

`--scope project` is what makes it worth doing at all: it records the
marketplace and the plugin in <project>/.claude/settings.json, which is
committed, so a teammate cloning the repo gets the same tools. The plugin's
files still live in ~/.claude/plugins/ — only the decision is project-scoped,
and any UI built on this has to say so.

Nothing here executes repo content: `claude` is resolved on PATH, arguments go
as separate argv entries to a subprocess with no shell, and every call runs
against a root that came out of the registry.
"""

import json
import subprocess

from .core import project_root, tilde


# Long enough for a clone of a real marketplace over a slow link, short enough
# that a wedged call gives the browser an answer instead of a spinner.
TIMEOUT = 120

# Offered in the UI as starting points. Both are Anthropic's own: the first is
# curated and is the one Claude Code registers by itself on a first
# interactive launch, the second is third-party plugins after review.
SUGGESTED = (
    {"source": "anthropics/claude-plugins-official",
     "label": "Anthropic official",
     "desc": "Curated by Anthropic. Claude Code registers this one itself on "
             "first launch, so it is usually already there."},
    {"source": "anthropics/claude-plugins-community",
     "label": "Anthropic community",
     "desc": "Third-party plugins, reviewed before listing."},
)

def _arg(value, what):
    """One CLI argument out of a request.

    Nothing is shell-interpolated — these go straight into argv — so the check
    is not about quoting. It is about a value that starts with a dash, which
    argv cannot distinguish from an option and which would let a typed name
    turn `install <name>` into `install --some-flag`."""
    s = str(value or "").strip()
    if not s:
        raise ValueError(f"no {what} given")
    if s.startswith("-"):
        raise ValueError(f"{what} cannot start with a dash")
    return s

def _run(root, *args):
    """`claude plugin …` in the project's directory.

    The cwd is the whole point: --scope project means "the project I am
    standing in", so a call made from anywhere else would quietly write to the
    wrong settings.json. stdin is closed rather than inherited — these run
    behind an HTTP request with no terminal, and a subcommand that decided to
    prompt should fail visibly rather than hang holding the connection."""
    p = project_root(root)
    try:
        r = subprocess.run(["claude", "plugin", *args], cwd=str(p),
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise ValueError("claude not found on PATH — install Claude Code "
                         "to manage marketplaces here") from None
    except subprocess.TimeoutExpired:
        raise ValueError(f"claude plugin {args[0]} took longer than "
                         f"{TIMEOUT}s — run it in a terminal to see why") from None
    out, err = r.stdout.strip(), r.stderr.strip()
    return {"ok": r.returncode == 0, "cmd": "claude plugin " + " ".join(args),
            "stdout": out, "stderr": err,
            # the CLI's own words, because it knows why it refused and we do not
            "detail": (out or err or f"exit {r.returncode}").splitlines()[-1]}

def _json_run(root, *args):
    """A --json call, decoded. A non-zero exit or unparseable output is state
    the card shows rather than an exception: the registry section failing must
    not cost the project its skills and MCP servers too."""
    r = _run(root, *args)
    if not r["ok"]:
        return None, r["detail"]
    try:
        return json.loads(r["stdout"] or "null"), None
    except json.JSONDecodeError:
        return None, f"{r['cmd']}: unreadable output"

def registry_state(root):
    """What this project can install from, and what it has enabled.

    Two calls, because the CLI answers two questions: which marketplaces are
    reachable from here, and which plugins are installed or available. The
    scope on each installed plugin is the CLI's, not ours — a plugin enabled
    for you personally shows up here too, and pretending otherwise would make
    the card claim the project owns something it does not."""
    markets, merr = _json_run(root, "marketplace", "list", "--json")
    plugins, perr = _json_run(root, "list", "--json", "--available")
    plugins = plugins if isinstance(plugins, dict) else {}
    installed = [p for p in (plugins.get("installed") or []) if isinstance(p, dict)]
    return {
        "root": tilde(project_root(root)),
        "error": merr or perr,
        "marketplaces": markets if isinstance(markets, list) else [],
        "installed": installed,
        "available": [p for p in (plugins.get("available") or [])
                      if isinstance(p, dict)],
        "suggested": [dict(s) for s in SUGGESTED],
    }

def marketplace_add(root, source):
    return _run(root, "marketplace", "add", _arg(source, "marketplace source"),
                "--scope", "project")

def marketplace_remove(root, name):
    return _run(root, "marketplace", "remove", _arg(name, "marketplace name"),
                "--scope", "project")

def plugin_install(root, plugin_id):
    return _run(root, "install", _arg(plugin_id, "plugin"), "--scope", "project")

def plugin_uninstall(root, plugin_id):
    # -y answers the prune confirmation the CLI asks for when stdout is not a
    # terminal, which it never is here
    return _run(root, "uninstall", _arg(plugin_id, "plugin"),
                "--scope", "project", "-y")
