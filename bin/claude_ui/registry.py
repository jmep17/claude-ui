"""Marketplaces and plugins, through Claude Code's own CLI.

Everywhere else this app reads and writes config files directly, because the
filesystem is the state and a file is legible without us. Marketplaces are the
one place that would not work: adding one clones a repository, installing a
plugin resolves a version and unpacks it into a cache, and both decisions are
Claude Code's to make and to revise. Reimplementing them here would mean
owning a downloader, a trust model and a cache layout that belong to something
else and change without us — so this module shells out and reports what came
back, verbatim.

Two scopes, two entry points. `--scope project` records the marketplace and
the plugin in <project>/.claude/settings.json, which is committed, so a
teammate cloning the repo gets the same tools; those calls run in the
project's own directory. `--scope user` records them in <config>/settings.json
for every project on this machine; those calls run from the home directory,
which always exists and is nobody's project. Either way the plugin's files
live in the shared plugins cache — only the decision is scoped, and any UI
built on this has to say so.

Nothing here executes repo content: `claude` is resolved on PATH, arguments go
as separate argv entries to a subprocess with no shell, and every
project-scoped call runs against a root that came out of the registry.
"""

import json
import os
import pathlib
import subprocess

from .core import config_dir, project_root, tilde


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

def _run_at(cwd, *args):
    """`claude plugin …` in a chosen directory.

    stdin is closed rather than inherited — these run behind an HTTP request
    with no terminal, and a subcommand that decided to prompt should fail
    visibly rather than hang holding the connection. CLAUDE_CONFIG_DIR goes in
    the environment explicitly: this app's config dir can be redirected by
    .claude-ui.json, which the CLI has never heard of, and without the env the
    two would quietly disagree about where settings live."""
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir())}
    try:
        r = subprocess.run(["claude", "plugin", *args], cwd=str(cwd),
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=TIMEOUT, env=env)
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

def _run(root, *args):
    """A project-scoped call, from the project's own directory.

    The cwd is the whole point: --scope project means "the project I am
    standing in", so a call made from anywhere else would quietly write to the
    wrong settings.json."""
    return _run_at(project_root(root), *args)

def _user_run(*args):
    """A user-scoped call, from home. --scope user lands in the same
    settings.json wherever it runs; home is used because it is always there
    and cannot be mistaken for a project."""
    return _run_at(pathlib.Path.home(), *args)

def _decode(r):
    """A --json answer, decoded. A non-zero exit or unparseable output is
    state the card shows rather than an exception: the registry section
    failing must not cost the page its skills and MCP servers too."""
    if not r["ok"]:
        return None, r["detail"]
    try:
        return json.loads(r["stdout"] or "null"), None
    except json.JSONDecodeError:
        return None, f"{r['cmd']}: unreadable output"

def _json_run(root, *args):
    return _decode(_run(root, *args))

def _user_json(*args):
    return _decode(_user_run(*args))

def _state(json_run):
    """What a scope can install from, and what it has enabled.

    Two calls, because the CLI answers two questions: which marketplaces are
    reachable, and which plugins are installed or available. The scope on each
    installed plugin is the CLI's, not ours — the caller's card badges it
    rather than filtering it, because pretending a plugin enabled elsewhere
    does not exist would make the list lie."""
    markets, merr = json_run("marketplace", "list", "--json")
    plugins, perr = json_run("list", "--json", "--available")
    plugins = plugins if isinstance(plugins, dict) else {}
    return {
        "error": merr or perr,
        "marketplaces": markets if isinstance(markets, list) else [],
        "installed": [p for p in (plugins.get("installed") or [])
                      if isinstance(p, dict)],
        "available": [p for p in (plugins.get("available") or [])
                      if isinstance(p, dict)],
        "suggested": [dict(s) for s in SUGGESTED],
    }

def registry_state(root):
    return {"root": tilde(project_root(root)),
            **_state(lambda *a: _json_run(root, *a))}

def user_registry_state():
    return _state(_user_json)

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

# `--scope user` is the CLI's default, but it goes on argv anyway: an implicit
# scope is one CLI default-change away from writing somewhere else.

def user_marketplace_add(source):
    return _user_run("marketplace", "add", _arg(source, "marketplace source"),
                     "--scope", "user")

def user_marketplace_remove(name):
    return _user_run("marketplace", "remove", _arg(name, "marketplace name"),
                     "--scope", "user")

def user_plugin_install(plugin_id):
    return _user_run("install", _arg(plugin_id, "plugin"), "--scope", "user")

def user_plugin_uninstall(plugin_id):
    return _user_run("uninstall", _arg(plugin_id, "plugin"),
                     "--scope", "user", "-y")
