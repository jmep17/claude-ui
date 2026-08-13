"""Installed plugins, their components, and splitting one into your own items.

Claude Code enables a plugin as a unit — there is no per-component switch for
agents, commands or output-styles. Splitting copies the components you want out
of the plugin and into the config dir, where they become ordinary items, then
turns the plugin off.

Copying (rather than masking components in place) is not a preference: a
marketplace is a tarball extract keyed by .gcs-sha, with no git history, so an
update replaces the tree wholesale and anything we removed inside it would come
silently back.
"""

from pathlib import Path
import json
import os
import re
import shutil

from . import schema
from .core import (NAME_RE, SOURCE_KEY, _read_json_object, atomic_write,
                   config_dir, disabled_dir, is_reserved_skill_dir, item_rel,
                   parse_frontmatter, project_claude_dir, project_root,
                   project_roots, set_frontmatter_key, tilde)
from .items import resolve_archived, resolve_item, skill_dirs, skill_facts
from .mcp import mcp_machine_set, validate_mcp_config
from .projects import project_setting_set
from .settings import ENV_READONLY, settings_set, settings_state


# Component kinds that map onto a config-dir item type, and so can be split out.
PLUGIN_TYPES = ("agents", "commands", "skills", "output-styles")

# Copy guards, mirroring items._skill_files() and items.MAX_EDIT.
MAX_FILES = 200
MAX_BYTES = 2 * 1024 * 1024
MAX_TREE = 8 * 1024 * 1024

# A component whose text expands this only works while its plugin is enabled,
# which splitting ends. Substring check, no parsing — we only need to warn.
PLUGIN_ROOT_VARS = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DIR")

HOOKS_REASON = ("hooks run from ${CLAUDE_PLUGIN_ROOT}, which stops resolving "
                "once the plugin is off")
MCP_ROOT_REASON = ("this server's command expands ${CLAUDE_PLUGIN_ROOT}, which "
                   "stops resolving once the plugin is off")


def plugins_root():
    """Where plugin trees live. A function, so tests can redirect the config."""
    return config_dir() / "plugins"

def _references_plugin_root(text):
    return any(v in text for v in PLUGIN_ROOT_VARS)

def _marketplaces():
    """[(name, Path)] from known_marketplaces.json, else a plain listing."""
    root = plugins_root()
    data, err = _read_json_object(root / "known_marketplaces.json")
    out = []
    for name, entry in data.items():
        loc = entry.get("installLocation") if isinstance(entry, dict) else None
        out.append((name, Path(loc).expanduser() if loc
                    else root / "marketplaces" / name))
    if not out:
        mdir = root / "marketplaces"
        if mdir.is_dir():
            out = [(d.name, d) for d in sorted(mdir.iterdir()) if d.is_dir()]
    return out, err

def _catalogue(mroot):
    """name -> (description, dir or None) for the plugins a marketplace ships.

    Only entries whose `source` is a relative path are on disk here; the rest
    of the catalogue points at git repos that are not fetched until installed.
    That path is the marketplace's own word on where its plugin lives, and it
    is not always a `plugins/` subdirectory — `"source": "./"` means the
    marketplace root *is* the plugin, a layout a directory scan cannot infer.
    """
    data, _ = _read_json_object(mroot / ".claude-plugin" / "marketplace.json")
    entries = data.get("plugins")
    out = {}
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            out[e["name"]] = (e.get("description") or "",
                              _catalogue_dir(mroot, e.get("source")))
    return out

def _catalogue_dir(mroot, source):
    """A catalogue entry's `source` as a directory inside the marketplace, or
    None when it is a git source, an absolute path, or points outside."""
    if not isinstance(source, str) or not source or Path(source).is_absolute():
        return None
    try:
        d = (mroot / source).resolve()
        root = mroot.resolve()
    except OSError:
        return None
    if d != root and root not in d.parents:
        return None
    return d if _is_plugin_dir(d) else None

def _manifest(pdir):
    """(name, description) from .claude-plugin/plugin.json, which many plugins
    (every *-lsp in the official marketplace) simply do not ship."""
    data, _ = _read_json_object(pdir / ".claude-plugin" / "plugin.json")
    return data.get("name"), data.get("description") or ""

def _is_plugin_dir(d):
    return d.is_dir() and not d.name.startswith(".") and (
        (d / ".claude-plugin").is_dir() or (d / ".mcp.json").is_file()
        or any((d / k).is_dir() for k in PLUGIN_TYPES) or (d / "hooks").is_dir())

def _marketplace_plugins(mname, mroot):
    """Catalogue entries first — they are what Claude Code loads — then any
    plugin directory the scan turns up that the catalogue did not claim."""
    cat = _catalogue(mroot)
    out, seen = [], set()
    for pname, (desc, pdir) in sorted(cat.items()):
        if pdir is None:
            continue
        out.append((pname, mname, pdir, desc or _manifest(pdir)[1]))
        seen |= {pname, pdir}
    for sub in ("plugins", "external_plugins"):
        d = mroot / sub
        if not d.is_dir():
            continue
        for pdir in sorted(d.iterdir()):
            if not _is_plugin_dir(pdir):
                continue
            if pdir.name in seen or pdir.resolve() in seen:
                continue
            desc, _ = cat.get(pdir.name, ("", None))
            out.append((pdir.name, mname, pdir, desc or _manifest(pdir)[1]))
    return out

def _repo_plugins():
    """Plugins installed straight from a git source live under plugins/repos/,
    nested by owner and repo, sometimes with the plugin one level deeper."""
    root = plugins_root() / "repos"
    out = []
    if not root.is_dir():
        return out
    for owner in sorted(root.iterdir()):
        if not owner.is_dir() or owner.name.startswith("."):
            continue
        for repo in sorted(owner.iterdir()):
            if not repo.is_dir() or repo.name.startswith("."):
                continue
            cands = [repo] if _is_plugin_dir(repo) else [
                d for d in sorted(repo.iterdir()) if _is_plugin_dir(d)]
            for pdir in cands[:MAX_FILES]:
                name, desc = _manifest(pdir)
                out.append((name or pdir.name, owner.name, pdir, desc))
    return out

def _plugin_dirs():
    """[(name, marketplace, Path, description)] for every plugin on disk."""
    markets, err = _marketplaces()
    out = []
    for mname, mroot in markets:
        out += _marketplace_plugins(mname, mroot)
    out += _repo_plugins()
    return out, err

def _md_components(pdir, kind):
    root = pdir / kind
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or not p.is_file():
            continue
        text = p.read_text(errors="replace")
        meta = parse_frontmatter(text)
        out.append({
            "kind": kind, "name": str(rel)[:-3],
            "description": meta.get("description", ""),
            # an agent's own model: line, the only per-agent model Claude Code
            # reads. Free here — the frontmatter is already parsed.
            "model": meta.get("model", "") if kind == "agents" else "",
            "path": tilde(p), "adoptable": True, "reason": None,
            "warn": ("references ${CLAUDE_PLUGIN_ROOT}; those paths will not "
                     "resolve once split") if _references_plugin_root(text) else None,
        })
    return out

def _skill_components(pdir):
    """A plugin's skills, read by items.skill_facts() and decorated here.

    The facts (description, symlink, broken, incomplete, todo, long_desc,
    chars, mtime) come from the one shared reader, so a plugin's skill and one
    of yours carry the same keys and the frontend needs a single badge
    function. What stays here is the ${CLAUDE_PLUGIN_ROOT} walk: skill_facts()
    deliberately reads only SKILL.md, because a recursive read of every skill
    tree on every /api/state is not a cost the inventory can carry, and only
    splitting needs the whole-tree answer."""
    out = []
    for entry in skill_dirs(pdir / "skills"):
        if not entry.is_dir():   # a bare symlink is not a plugin's to ship
            continue
        smd = entry / "SKILL.md"
        text = smd.read_text(errors="replace") if smd.is_file() else ""
        try:
            body = "".join(p.read_text(errors="replace")
                           for p in entry.rglob("*.md") if p.is_file())
        except OSError:
            body = text
        out.append({
            **skill_facts(entry),   # name, description, path, and the badges
            "kind": "skills",
            "model": "",
            "adoptable": True, "reason": None,
            "warn": ("references ${CLAUDE_PLUGIN_ROOT}; those paths will not "
                     "resolve once split") if _references_plugin_root(body) else None,
        })
    return out

def _plugin_mcp(pdir):
    """A plugin's .mcp.json is a flat {name: config} map, no mcpServers key."""
    data, err = _read_json_object(pdir / ".mcp.json")
    if err:
        return [{"kind": "mcp", "name": ".mcp.json", "description": "",
                 "model": "", "path": tilde(pdir / ".mcp.json"),
                 "adoptable": False,
                 "reason": f"invalid JSON: {err}", "warn": None}]
    out = []
    for name, cfg in data.items():
        blocked = _references_plugin_root(json.dumps(cfg))
        out.append({
            "kind": "mcp", "name": name,
            "description": cfg.get("command") or cfg.get("url") or ""
                           if isinstance(cfg, dict) else "",
            "model": "",
            "path": tilde(pdir / ".mcp.json"),
            "adoptable": not blocked,
            "reason": MCP_ROOT_REASON if blocked else None, "warn": None,
        })
    return out

def _plugin_hooks(pdir):
    hdir = pdir / "hooks"
    if not hdir.is_dir():
        return []
    data, _ = _read_json_object(hdir / "hooks.json")
    events = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    return [{
        "kind": "hooks", "name": "hooks",
        "description": ", ".join(sorted(events)) if events else "",
        "model": "", "path": tilde(hdir), "adoptable": False,
        "reason": HOOKS_REASON, "warn": None,
    }]

def _components(pdir):
    out = []
    for kind in PLUGIN_TYPES:
        out += _skill_components(pdir) if kind == "skills" else _md_components(pdir, kind)
    out += _plugin_mcp(pdir)
    out += _plugin_hooks(pdir)
    return out

def enabled_plugins():
    """(mapping, error) — the settings.json enabledPlugins object."""
    st = settings_state()
    if st["error"]:
        return {}, st["error"]
    m = st["data"].get("enabledPlugins", {})
    return (m if isinstance(m, dict) else {}), None

def _dest(kind, name, enabled=True):
    base = config_dir() if enabled else disabled_dir()
    rel = item_rel(name)
    if kind == "skills":
        if len(rel.parts) != 1:
            raise ValueError("bad name")
        return base / kind / rel
    return base / kind / rel.with_suffix(".md")

def _conflict(kind, name):
    """Why this component cannot be split out under this name, or None.

    The disabled side counts: items.set_enabled refuses to move when both sides
    exist, so landing on top of a disabled twin would manufacture that state.
    The archive counts for the same reason — items.skill_archive_set refuses to
    restore onto an occupied name — and a reserved directory name is not a name
    an item may answer to at all. A plugin may legally ship a skill called
    `archived`; it simply cannot be split out under that name.
    """
    if kind not in PLUGIN_TYPES:
        return None
    if kind == "skills" and is_reserved_skill_dir(name):
        return (f"{name} is a reserved directory name in your skills folder — "
                "split it under a different name")
    try:
        live, off = _dest(kind, name), _dest(kind, name, enabled=False)
    except ValueError:
        return "name is not usable as an item name"
    noun = kind[:-1]
    article = "an" if noun[0] in "aeiou" else "a"
    if live.exists() or live.is_symlink():
        return f"you already have {article} {noun} called {name}"
    if off.exists() or off.is_symlink():
        return f"a disabled {noun} called {name} is parked in disabled/"
    if kind == "skills":
        try:
            arch = resolve_archived(name)
        except ValueError:
            return "name is not usable as an item name"
        if arch.exists() or arch.is_symlink():
            return f"an archived skill called {name} is in skills/archived/"
    return None

def plugins_state():
    """Every plugin on this machine, its components, and whether it is on."""
    root = plugins_root()
    enabled, serr = enabled_plugins()
    entries = _plugin_scope_entries()
    try:
        found, merr = _plugin_dirs()
    except OSError as e:
        return {"plugins": [], "marketplaces": [], "root": tilde(root),
                "error": str(e)}
    out = []
    for name, market, pdir, desc in found:
        pid = f"{name}@{market}"
        try:
            comps = _components(pdir)
        except OSError as e:
            comps, desc = [], f"(unreadable: {e})"
        for c in comps:
            c["conflict"] = _conflict(c["kind"], c["name"]) if c["adoptable"] else None
        out.append({
            "id": pid, "name": name, "marketplace": market,
            "description": desc, "path": tilde(pdir),
            # state and enabled are the user store's answer, unchanged:
            # doctor and insight read them, and the tab's sections are
            # about what applies to you everywhere
            "state": "enabled" if enabled.get(pid) is True
                     else "disabled" if pid in enabled else "available",
            "enabled": enabled.get(pid) is True,
            "entries": entries.get(pid, []),
            "components": comps,
            "counts": _counts(comps),
        })
    out.sort(key=lambda p: (p["state"] != "enabled", p["id"]))
    return {"plugins": out, "marketplaces": [m for m, _ in _marketplaces()[0]],
            "root": tilde(root), "error": serr or merr}

def _counts(comps):
    counts = {}
    for c in comps:
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
    return counts

def _plugin(pid):
    for name, market, pdir, desc in _plugin_dirs()[0]:
        if f"{name}@{market}" == pid:
            return name, market, pdir, desc
    raise ValueError(f"{pid}: unknown plugin")

# ---- the env vars a plugin reads
#
# Claude Code has no per-plugin or per-agent model setting, so a plugin that
# wants one ships its own: caveman's SessionStart hook rewrites each cavecrew
# agent's `model:` line from CAVECREW_REVIEWER_MODEL and friends. Those names
# exist only in the plugin's source and, if you are lucky, a README table
# inside one of its skills. Reading them off disk is the only way anyone is
# going to find them — and once found they are ordinary settings.json `env`
# entries, which this app already writes.
#
# A guess, and labelled as one in the UI: a name matched here is a name the
# plugin's code mentions, not a documented contract.

ENV_SCAN_CODE_EXTS = frozenset({".js", ".mjs", ".cjs", ".ts", ".py", ".sh",
                                ".bash", ".zsh", ".json", ".toml"})
ENV_SCAN_EXTS = ENV_SCAN_CODE_EXTS | {".md"}
# node_modules and friends are not this plugin's code; tests and evals mention
# env vars nobody is meant to set.
ENV_SCAN_SKIP = frozenset({"node_modules", "dist", "build", "vendor",
                           "__pycache__", "coverage", "fixtures", "tests",
                           "test", "evals", "benchmarks", "examples"})
ENV_SCAN_MAX_FILES = 400
ENV_SCAN_MAX_BYTES = 256 * 1024  # per file; a bundle is not worth reading

# Reads we can see directly.
_ENV_PATTERNS = [
    re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)"),
    re.compile(r"""process\.env\[\s*['"]([A-Z_][A-Z0-9_]*)"""),
    re.compile(r"""os\.environ(?:\.get\(|\[)\s*['"]([A-Z_][A-Z0-9_]*)"""),
    re.compile(r"""os\.getenv\(\s*['"]([A-Z_][A-Z0-9_]*)"""),
]
# Shell has no marker distinguishing "read from the environment" from "read the
# variable I set four lines up", so names assigned in the same file are dropped.
_SH_USE = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)\b")
_SH_SET = re.compile(r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+-\w+\s+)?"
                     r"([A-Z_][A-Z0-9_]*)=", re.M)
# Reads we can only infer: caveman's model overrides go through a table of
# {envVar: 'CAVECREW_REVIEWER_MODEL'} literals, which no process.env.X pattern
# can see. In a file that demonstrably works with the environment, a quoted
# SCREAMING_SNAKE literal is worth offering. The underscore is the filter that
# keeps this from matching every quoted constant in the file.
_ENV_AWARE = re.compile(r"process\.env|os\.environ|os\.getenv|getenv\(")
_ENV_LITERAL = re.compile(r"""['"]([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)['"]""")

# Names that are somebody else's: the shell's, the OS's, or Claude Code's own
# (those have dedicated Settings rows — surfacing them per-plugin would imply a
# scope they do not have).
ENV_SCAN_GENERIC = frozenset({
    "PATH", "HOME", "PWD", "OLDPWD", "USER", "LOGNAME", "SHELL", "TERM",
    "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "EDITOR", "VISUAL", "PAGER",
    "NO_COLOR", "FORCE_COLOR", "CI", "DEBUG", "NODE_ENV", "NODE_OPTIONS",
    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "USERPROFILE", "HOSTNAME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "SHLVL", "IFS", "PS1", "RANDOM", "OSTYPE", "BASH_SOURCE",
})

def _env_scan_files(pdir):
    """Text files worth grepping, hard-capped."""
    out = []
    for p in sorted(pdir.rglob("*")):
        rel = p.relative_to(pdir)
        if any(part.startswith(".") or part in ENV_SCAN_SKIP for part in rel.parts):
            continue
        if not p.is_file() or p.is_symlink() or p.suffix not in ENV_SCAN_EXTS:
            continue
        out.append(p)
        if len(out) >= ENV_SCAN_MAX_FILES:
            break
    return out

def _env_names(text, suffix):
    """Env var names one source file reads. Markdown never contributes a name —
    prose is full of `$VARIABLE`-shaped things that nobody can set."""
    names = set()
    for pat in _ENV_PATTERNS:
        names |= set(pat.findall(text))
    if suffix in (".sh", ".bash", ".zsh"):
        names |= set(_SH_USE.findall(text)) - set(_SH_SET.findall(text))
    if _ENV_AWARE.search(text):
        names |= set(_ENV_LITERAL.findall(text))
    return names

def _env_doc(name, texts):
    """The first line of the plugin's own prose that mentions this var — for
    caveman that is the env-var/agent table in skills/cavecrew/README.md.

    Markdown table rows are the common shape and read badly raw, so pipes and
    backticks are flattened into something a row can show as a sentence.
    """
    for rel, text in texts:
        for line in text.splitlines():
            if name not in line or not line.strip():
                continue
            line = line.strip()
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                line = " → ".join(c for c in cells if c)
            return {"line": line.replace("`", "")[:240], "file": rel}
    return None

def env_vars_in(root):
    """[{name, model, files, doc, value}] — env vars the code under `root`
    reads, whatever `root` is: a plugin tree or one skill's directory.

    Kept out of any state() call: those run on every /api/plugins hit and again
    inside insight and doctor, and this walks a whole tree.
    """
    # CLAUDE_PLUGIN_ROOT and friends are handed *to* a plugin by Claude Code
    official = set(schema.env_var_names()) | set(ENV_READONLY) | set(PLUGIN_ROOT_VARS)
    hits, docs = {}, []
    for p in _env_scan_files(root):
        try:
            if p.stat().st_size > ENV_SCAN_MAX_BYTES:
                continue
            text = p.read_text(errors="replace")
        except OSError:
            continue
        rel = str(p.relative_to(root))
        if p.suffix == ".md":
            docs.append((rel, text))
            continue
        for name in _env_names(text, p.suffix):
            if name in official or name in ENV_SCAN_GENERIC or len(name) < 4:
                continue
            hits.setdefault(name, []).append(rel)
    env = settings_state()["data"].get("env")
    env = env if isinstance(env, dict) else {}
    out = []
    for name in sorted(hits):
        files = sorted(set(hits[name]))
        out.append({
            "name": name,
            "model": "MODEL" in name,
            "files": files[:8],
            "doc": _env_doc(name, docs),
            "value": env.get(name, ""),
        })
    # model knobs first — they are what people come here for
    out.sort(key=lambda e: (not e["model"], e["name"]))
    return out


def plugin_env_vars(pid):
    """The env vars one installed plugin reads."""
    _, _, pdir, _ = _plugin(pid)
    return env_vars_in(pdir)

def plugin_env_set(name, value):
    """Set (or clear, value falsy) one settings.json env entry."""
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", name or ""):
        raise ValueError("bad environment variable name")
    settings_set("env." + name, value if value else None)


def skill_env_vars(name, enabled=True):
    """The env vars one personal skill's own files read.

    A skill is a directory, so it can ship a scripts/ package that reads the
    environment the same way a plugin does — and with the same problem: the
    names exist only in its source. Same scanner, same caveat in the UI, and
    the values land in the same settings.json `env`, because that is the only
    place Claude Code reads them from.
    """
    root = resolve_item("skills", name, enabled, None)
    if not root.is_dir():
        raise ValueError(f"no skill named {name}")
    return env_vars_in(root)


def plugin_set_enabled(pid, enabled):
    """Flip a plugin in settings.json's enabledPlugins.

    The whole object is rewritten rather than addressed as a dotted path:
    settings_set splits on '.' and its key regex has no '@', so a plugin id
    like 'wordpress.com@mkt' would nest into the wrong shape.
    """
    if not isinstance(pid, str) or "@" not in pid:
        raise ValueError("bad plugin id")
    cur, err = enabled_plugins()
    if err:
        raise ValueError(f"settings.json: {err} — fix it by hand first")
    settings_set("enabledPlugins", {**cur, pid: bool(enabled)})

# ---- where a plugin's enablement is recorded
#
# Claude Code reads enabledPlugins from three stores: your settings.json
# (user — every project on this machine), a project's .claude/settings.json
# (project — committed, everyone who clones), and its settings.local.json
# (local — you, that project). The plugin's files live in the shared cache
# regardless; the stores hold only the decision, which is why moving between
# them is a settings edit and not a reinstall.

def _plugin_scope_entries():
    """Every enabledPlugins entry across every store this app can see:
    {pid: [{"scope", "root", "raw_root", "enabled"}, …]}.

    Without this a plugin recorded only in a project renders as "available"
    on the Plugins tab — "not in use", which is false. A project whose
    .claude is a symlink is skipped, as everywhere else."""
    out = {}
    def note(pid, scope, root, on):
        if isinstance(on, bool):
            out.setdefault(pid, []).append(
                {"scope": scope, "root": tilde(root) if root else None,
                 "raw_root": str(root) if root else None, "enabled": on})
    user, _ = enabled_plugins()
    for pid, on in user.items():
        note(pid, "user", None, on)
    for root in project_roots():
        try:
            cdir = project_claude_dir(root)
        except ValueError:
            continue
        for fname, scope in (("settings.json", "project"),
                             ("settings.local.json", "local")):
            data, _ = _read_json_object(cdir / fname)
            m = data.get("enabledPlugins")
            for pid, on in (m.items() if isinstance(m, dict) else ()):
                note(pid, scope, root, on)
    return out

def _store(d):
    """One end of a scope move, validated. The root goes through
    project_root, so registration and the symlink gates apply first."""
    scope = (d or {}).get("scope") if isinstance(d, dict) else None
    if scope not in ("user", "project", "local"):
        raise ValueError("scope must be user, project or local")
    root = project_root((d or {}).get("root")) if scope != "user" else None
    return scope, root

def _store_desc(scope, root):
    if scope == "user":
        return "user scope (settings.json)"
    rel = "settings.json" if scope == "project" else "settings.local.json"
    return f"{tilde(root)}/.claude/{rel}"

def _store_read(scope, root):
    if scope == "user":
        m, err = enabled_plugins()
        if err:
            raise ValueError(f"settings.json: {err} — fix it by hand first")
        return m
    path = project_claude_dir(root) / ("settings.json" if scope == "project"
                                       else "settings.local.json")
    data, err = _read_json_object(path)
    if err:
        raise ValueError(f"{tilde(path)}: {err} — fix it by hand first")
    m = data.get("enabledPlugins")
    return m if isinstance(m, dict) else {}

def _store_write(scope, root, pid, val):
    """Set (bool) or remove (None) one entry. An emptied map loses the whole
    key rather than lingering as {} — in a committed settings.json that would
    be a puzzle, and Claude Code reads an absent key the same way."""
    cur = dict(_store_read(scope, root))
    if val is None:
        cur.pop(pid, None)
    else:
        cur[pid] = val
    if scope == "user":
        settings_set("enabledPlugins", cur or None)
    else:
        project_setting_set(root, "enabledPlugins", cur or None,
                            local=(scope == "local"))

def plugin_scope_move(pid, src, dst):
    """Move an enabledPlugins entry between stores, bool and all.

    The recorded value travels verbatim: a plugin disabled at user scope
    arrives disabled at project scope, because the move changes where the
    decision is recorded, not the decision. Destination first, then the
    source entry goes — a failure in between duplicates the record, never
    drops it. A destination that already has an answer for this plugin is a
    conflict to resolve by hand, not to silently overwrite."""
    if not isinstance(pid, str) or "@" not in pid:
        raise ValueError("bad plugin id")
    s_scope, s_root = _store(src)
    d_scope, d_root = _store(dst)
    if (s_scope, s_root) == (d_scope, d_root):
        raise ValueError("source and destination are the same place")
    val = _store_read(s_scope, s_root).get(pid)
    if not isinstance(val, bool):
        raise ValueError(f"{pid}: not recorded at {_store_desc(s_scope, s_root)}")
    if pid in _store_read(d_scope, d_root):
        raise ValueError(f"{pid}: already recorded at "
                         f"{_store_desc(d_scope, d_root)} — resolve by hand")
    _store_write(d_scope, d_root, pid, val)
    _store_write(s_scope, s_root, pid, None)
    return {"id": pid, "enabled": val,
            "from": _store_desc(s_scope, s_root),
            "to": _store_desc(d_scope, d_root)}


def skill_override_set(name, value):
    """Set (or clear, value=None) one skillOverrides entry in your settings.

    Claude Code's own per-skill switch, and it reaches your skills and a
    project's — not a plugin's:

        "Plugin skills are not affected by skillOverrides. Manage those
         through /plugin instead."
        — code.claude.com/docs/en/skills, fetched 2026-08-10

    This lived on the Plugins tab until that was noticed. The key is a bare
    skill name, while a plugin's skill answers to plugin-name:skill-name, so
    an entry written for a plugin's skill did not name it — it named whatever
    skill of your own happened to share the last segment, and turned that off
    instead. Splitting a plugin component creates a copy under exactly that
    name, so the two features aimed at each other.

    Kept here, next to the plugin code that motivated it, because the Plugins
    tab is still where you find out a skill exists; the switch itself now sits
    on the skill's own row.
    """
    if not NAME_RE.match(name or ""):
        raise ValueError("bad skill name")
    if value is not None and value not in ("on", "name-only",
                                           "user-invocable-only", "off"):
        raise ValueError("bad override value")
    st = settings_state()
    if st["error"]:
        raise ValueError(f"settings.json: {st['error']} — fix it by hand first")
    cur = st["data"].get("skillOverrides", {})
    cur = dict(cur) if isinstance(cur, dict) else {}
    if value is None:
        cur.pop(name, None)
    else:
        cur[name] = value
    settings_set("skillOverrides", cur or None)


# ---- splitting

def _plan_copy(src, name):
    """Relative paths to copy out of a skill dir. Raises before anything is
    written, so a refusal always leaves the machine untouched."""
    rels, total = [], 0
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if p.is_symlink():
            raise ValueError(f"{name}: contains a symlink ({rel}) — copy it by hand")
        if not p.is_file():
            continue
        size = p.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(f"{name}: {rel} is over the "
                             f"{MAX_BYTES // (1024 * 1024)}MB file limit — copy it by hand")
        total += size
        rels.append(rel)
    if len(rels) > MAX_FILES:
        raise ValueError(f"{name}: {len(rels)} files, over the {MAX_FILES}-file "
                         "limit — copy it by hand")
    if total > MAX_TREE:
        raise ValueError(f"{name}: {total // (1024 * 1024)}MB, over the "
                         f"{MAX_TREE // (1024 * 1024)}MB limit — copy it by hand")
    return rels

def _stage_path(dst):
    return dst.with_name(f".{dst.name}.claude-ui-tmp")

def _copy_tree(src, dst, rels, source):
    """Build the whole tree in a staging dir, then rename it into place: a
    half-written skill directory is one Claude Code would try to load."""
    stage = _stage_path(dst)
    shutil.rmtree(stage, ignore_errors=True)
    for rel in rels:
        s, d = src / rel, stage / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if rel.name == "SKILL.md":
            d.write_text(_with_source(s.read_text(errors="replace"), source))
        else:
            d.write_bytes(s.read_bytes())  # bytes: skills ship real binaries
        if os.access(s, os.X_OK):
            d.chmod(d.stat().st_mode | 0o111)
    return stage

def _with_source(text, source):
    """Record provenance in the item's own frontmatter, adding a block if the
    file has none."""
    return set_frontmatter_key(text, SOURCE_KEY, source)

def _strip_source(text):
    """Drop the provenance line, so an adopted item is not read as drifted from
    the source it was copied from."""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.startswith(SOURCE_KEY + ":"))

def check_model(model):
    """A model alias or ID, as it will be written to a `model:` line."""
    model = (model or "").strip()
    if not model:
        return ""
    if re.search(r"[\x00-\x1f\x7f]", model) or len(model) > 200:
        raise ValueError(f"{model[:40]}: not a usable model name")
    return model

def plugins_split(pid, picks, disable=True, models=None):
    """Copy the chosen components into the config dir, then turn the plugin off.

    `models` is {agent name: model}, applied to the copy's `model:` line on the
    way out — the plugin's own file is never touched, so choosing a model here
    is the same act as splitting, not a second edit against someone else's tree.

    Everything that can fail is checked first, so the common refusals (a name
    collision, an oversized skill, unparseable settings) change nothing at all.
    Once copying starts a failure leaves the copies that landed and leaves the
    plugin enabled — extra items with the plugin still on beats a disabled
    plugin with pieces missing.
    """
    name, market, pdir, _ = _plugin(pid)
    models = {k: check_model(v) for k, v in (models or {}).items()}
    comps = {(c["kind"], c["name"]): c for c in _components(pdir)}

    # phase 1: validate, write nothing
    plan = []
    for pick in picks or []:
        kind, cname = (pick.get("kind"), pick.get("name")) if isinstance(pick, dict) \
            else (None, None)
        c = comps.get((kind, cname))
        if c is None:
            raise ValueError(f"{pid}: no {kind} component called {cname}")
        if not c["adoptable"]:
            raise ValueError(f"{cname}: {c['reason']}")
        if kind == "mcp":
            plan.append(("mcp", cname, None, None))
            continue
        conflict = _conflict(kind, cname)
        if conflict:
            raise ValueError(f"{cname}: {conflict} — rename or remove yours first")
        src = pdir / kind / (cname if kind == "skills" else cname + ".md")
        source = f"{pid}/{kind}/{cname}"
        plan.append((kind, cname, src,
                     _plan_copy(src, cname) if kind == "skills" else source))
    if not plan:
        raise ValueError("nothing selected")
    if disable:
        _, serr = enabled_plugins()
        if serr:
            raise ValueError(f"settings.json: {serr} — fix it by hand first")
    mcp_cfg, _ = _read_json_object(pdir / ".mcp.json")

    # phase 2: stage
    staged, copied = [], []
    try:
        for kind, cname, src, extra in plan:
            if kind == "mcp":
                continue
            dst = _dest(kind, cname)
            if kind == "skills":
                source = f"{pid}/skills/{cname}"
                staged.append((_copy_tree(src, dst, extra, source), dst))
            else:
                text = _with_source(src.read_text(errors="replace"), extra)
                if kind == "agents" and cname in models:
                    text = set_frontmatter_key(text, "model",
                                               models[cname] or None)
                staged.append((text, dst))

        # phase 3: commit
        for payload, dst in staged:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(payload, Path):
                payload.rename(dst)
            else:
                atomic_write(dst, payload)
            copied.append(dst)
        for kind, cname, _, _ in plan:
            if kind == "mcp":
                cfg = mcp_cfg.get(cname)
                validate_mcp_config(cfg)
                mcp_machine_set(cname, cfg)
                copied.append(Path(cname))
    except OSError as e:
        raise ValueError(f"{pid}: copied {len(copied)} of {len(plan)} — the plugin "
                         f"was left enabled; finish or undo by hand ({e})") from None
    finally:
        for payload, _ in staged:
            if isinstance(payload, Path):
                shutil.rmtree(payload, ignore_errors=True)

    # phase 4: settings
    if disable:
        plugin_set_enabled(pid, False)
    return {"plugin": pid, "kept": len(plan), "total": len(comps),
            "copied": [{"kind": k, "name": n} for k, n, _, _ in plan],
            "disabled": bool(disable)}


# ---- provenance and drift

def _meta_of(path):
    """An item's frontmatter — a skill's lives in its SKILL.md."""
    if path.is_dir():
        path = path / "SKILL.md"
    return parse_frontmatter(path.read_text(errors="replace")) \
        if path.is_file() else {}

def _source_of(path):
    return _meta_of(path).get(SOURCE_KEY)

def _resolve_source(source):
    """A recorded 'plugin@market/kind/name' back to its path, or None."""
    try:
        pid, kind, cname = source.split("/", 2)
        _, _, pdir, _ = _plugin(pid)
    except ValueError:
        return None
    if kind not in PLUGIN_TYPES:
        return None
    return pdir / kind / (cname if kind == "skills" else cname + ".md")

def _differs(ours, theirs):
    if theirs.is_dir():
        rel = lambda root: sorted(str(p.relative_to(root)) for p in root.rglob("*")
                                  if p.is_file()
                                  and not any(x.startswith(".")
                                              for x in p.relative_to(root).parts))
        if rel(ours) != rel(theirs):
            return True
        for r in rel(ours):
            a, b = ours / r, theirs / r
            if r == "SKILL.md":
                if _strip_source(a.read_text(errors="replace")) != \
                        _strip_source(b.read_text(errors="replace")):
                    return True
            elif a.read_bytes() != b.read_bytes():
                return True
        return False
    return _strip_source(ours.read_text(errors="replace")) != \
        _strip_source(theirs.read_text(errors="replace"))

def adopted_items():
    """Config-dir items that record a plugin source, and whether they still
    match it. Drift is neutral: after a split the user is expected to edit."""
    out = []
    for kind in PLUGIN_TYPES:
        for enabled in (True, False):
            root = (config_dir() if enabled else disabled_dir()) / kind
            if not root.is_dir():
                continue
            # archived/ is not an item: it holds them. Without this filter an
            # archived split-out skill renders as an adopted item whose path
            # the tab cannot open.
            entries = (sorted(p for p in root.iterdir()
                              if p.is_dir() and not is_reserved_skill_dir(p.name))
                       if kind == "skills" else sorted(root.rglob("*.md")))
            for p in entries:
                try:
                    meta = _meta_of(p)
                except OSError:
                    continue
                source = meta.get(SOURCE_KEY)
                if not source:
                    continue
                name = p.name if kind == "skills" else str(
                    p.relative_to(root))[:-3]
                theirs = _resolve_source(source)
                missing = theirs is None or not (theirs.exists())
                try:
                    drift = not missing and _differs(p, theirs)
                except OSError:
                    drift = False
                # absolute and readable: the UI opens the plugin's copy
                # read-only to show what "drift" actually means. A skill is a
                # directory, so point at the file, not the folder.
                their_file = "" if missing else str(
                    theirs / "SKILL.md" if kind == "skills" else theirs)
                out.append({"type": kind, "name": name, "source": source,
                            "path": tilde(p), "enabled": enabled,
                            "missing": missing, "drift": drift,
                            # ours to set, unlike the plugin's read-only copy
                            "model": meta.get("model", "") if kind == "agents" else "",
                            "source_path": their_file})
    return out

def plugin_resync(type_, name):
    """Overwrite an adopted item from the plugin it came from."""
    if type_ not in PLUGIN_TYPES:
        raise ValueError("unknown type")
    dst = _dest(type_, name)
    if not (dst.exists() or dst.is_symlink()):
        raise ValueError(f"{name}: not found")
    source = _source_of(dst)
    if not source:
        raise ValueError(f"{name}: has no {SOURCE_KEY} — nothing to re-sync from")
    src = _resolve_source(source)
    if src is None or not src.exists():
        raise ValueError(f"{name}: {source} is no longer installed")
    if type_ == "skills":
        stage = _copy_tree(src, dst, _plan_copy(src, name), source)
        try:
            shutil.rmtree(dst)
            stage.rename(dst)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    else:
        atomic_write(dst, _with_source(src.read_text(errors="replace"), source))
    return {"path": tilde(dst)}
