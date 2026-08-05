"""The official Claude Code settings JSON Schema: snapshot, live overlay, merge.

Claude Code publishes a machine-readable JSON Schema for settings.json — the
docs name it as the `$schema` value in their own example file. It carries a
description, type, enum and default for every documented key, and most
descriptions embed the exact anchored docs URL for that key. That makes it the
right source for tooltips and for knowing which keys exist at all.

Three layers, so regenerating can never clobber hand curation:

    data/settings_schema.json   generated snapshot — description, type, enum,
                                default, doc URL, managed flag
            ↓ overlaid by
    _live                       background re-fetch of the same URL at start
            ↓ merged under
    SETTINGS_SCHEMA             hand-written — control type, category, aka,
                                fields, templates

merge() applies official facts *over* the hand-written list at boot. The list
itself is never rewritten, which is what guarantees the curated control types
and categories survive a regeneration.

Invariant: the vendored snapshot is the floor. A live fetch may add or replace
entries, never delete them — so a bad upstream commit degrades to stale, not to
empty.

Packaging note: data/settings_schema.json is read relative to this file. The app
runs from a checkout (bin/claude-ui does the sys.path insert), so there is no
package_data to declare; anyone packaging this needs to add it. vendored()
returns {} on any read failure, so a missing file degrades to the hand-written
schema rather than crashing.
"""

from pathlib import Path
import functools
import json
import re
import threading
import urllib.request


SCHEMA_URL = "https://json.schemastore.org/claude-code-settings.json"
OFFICIAL_PATH = Path(__file__).resolve().parent / "data" / "settings_schema.json"

# The category holding keys that only do something in managed/enterprise scope.
MANAGED_CAT = "managed & enterprise"

# Keys the official schema lists but Claude Code reads from ~/.claude.json, not
# settings.json — see the "Global config settings" table in
# https://code.claude.com/docs/en/settings#global-config-settings, which states
# outright that settings.json silently ignores them. The schema over-includes;
# the docs win. Four of the six descriptions even link to that section.
# permissionExplainerEnabled and externalEditorContext are the easy mistake
# here: they read as ordinary user preferences.
GLOBAL_CONFIG_KEYS = frozenset({
    "autoConnectIde", "autoInstallIdeExtension", "diffTool",
    "externalEditorContext", "permissionExplainerEnabled",
    "teammateDefaultModel",
})

# Where to read more about a key, for the ~13 keys whose official description
# carries no URL of its own. Most live on the settings reference; the handful
# with a page of their own get sent there instead. This used to be SETTING_DOCS
# in static/app.js; it lives here now so a test can assert every key resolves.
DOC_BASE = "https://code.claude.com/docs/en/"
DOC_FALLBACKS = [
    (re.compile(r"^hooks|^disableAllHooks"), "hooks"),
    (re.compile(r"^statusLine"), "statusline"),
    (re.compile(r"^sandbox|^autoMode|^warningOnSandboxEscape"), "sandboxing"),
    (re.compile(r"^permissions"), "iam"),
    (re.compile(r"Mcp|^mcpServer"), "mcp"),
    (re.compile(r"^plugin"), "plugins"),
    (re.compile(r"^outputStyle"), "output-styles"),
    (re.compile(r"^autoMemory|^claudeMdExcludes|^autoCompact"), "memory"),
    (re.compile(r"^env$"), "settings#environment-variables"),
    # the env.* alternative is anchored and uppercase-only so it can't collide;
    # adding /i to `Model$` would start matching unrelated future keys
    (re.compile(r"^model$|^fallbackModel|Model$|^env\.[A-Z_]*MODEL$"), "model-config"),
    (re.compile(r"^keyBindings|^editorMode"), "terminal-config"),
    (re.compile(r"^fileCheckpointing"), "checkpointing"),
]

# The official schema has no machine-readable managed marker — it is a prose
# convention. Most managed keys open with "(Managed settings…)" or "(Admin…)";
# these five say it in softer words. Note the convention is imprecise in the
# other direction too: it also tags disableAgentView and sshConfigs, which the
# app ships as ordinary user-facing rows. So this flag may *badge* a key, but it
# must never decide which category a key lands in — see MANAGED_EXTRA in
# settings.py, which is an explicit list.
MANAGED_SOFT = frozenset({
    "allowedMcpServers", "deniedMcpServers", "forceLoginGatewayUrl",
    "policyHelper", "wslInheritsWindowsSettings",
})

_MANAGED_RE = re.compile(r"^\((Managed|Admin)")
_URL_RE = re.compile(r"https://(?:code|docs)\.claude\.com/docs/\S+")


# ------------------------------------------------------------------ building
# Used by both the sync tool (tools/sync_settings_schema.py, which imports from
# here) and the live fetch below, so a bad document is refused on both paths.

def validate(doc):
    """Raise ValueError unless `doc` is a plausible Claude Code settings schema.

    The guard against schemastore serving a truncated or broken upstream commit:
    without it, a bad fetch would silently blank every tooltip.
    """
    if not isinstance(doc, dict):
        raise ValueError("schema is not a JSON object")
    if "draft-07" not in str(doc.get("$schema", "")):
        raise ValueError(f"unexpected $schema: {doc.get('$schema')!r}")
    props = doc.get("properties")
    if not isinstance(props, dict):
        raise ValueError("schema has no properties object")
    if len(props) < 120:
        raise ValueError(f"only {len(props)} top-level properties — looks truncated")
    flat = flatten(doc)
    described = sum(1 for v in flat.values() if v.get("description"))
    if described < len(flat) * 0.95:
        raise ValueError(f"only {described}/{len(flat)} keys carry a description")
    return doc


def branches(node):
    """A node and its one-level anyOf/oneOf/allOf branches.

    Only four keys in the schema use composition (theme,
    strictPluginOnlyCustomization, policyHelper.refreshIntervalMs,
    forceLoginOrgUUID), and none nest it, so one level is enough.
    """
    out = [node]
    for kw in ("anyOf", "oneOf", "allOf"):
        for b in node.get(kw) or []:
            if isinstance(b, dict):
                out.append(b)
    return out


def pick_type(node):
    """The JSON type, unioned across branches. str, list, or None."""
    seen = []
    for b in branches(node):
        t = b.get("type")
        for one in (t if isinstance(t, list) else [t] if t else []):
            if one not in seen:
                seen.append(one)
    if not seen:
        return None
    return seen[0] if len(seen) == 1 else seen


def pick_enum(node):
    """Union of `enum` across branches, order preserved, first occurrence wins."""
    out = []
    for b in branches(node):
        for v in b.get("enum") or []:
            if v not in out:
                out.append(v)
    return out


def doc_url(desc):
    """The docs URL a description points at — anchored one preferred.

    Descriptions cite between zero and three URLs. `permissions` cites three, of
    which only the settings one is anchored; that anchor is the useful link.
    """
    urls = [u.rstrip(".,;)") for u in _URL_RE.findall(desc or "")]
    if not urls:
        return None
    anchored = [u for u in urls if "#" in u]
    return (anchored or urls)[-1]


def is_managed(key, desc):
    return bool(_MANAGED_RE.match(desc or "")) or key in MANAGED_SOFT


def flatten(doc):
    """{dotted key: entry} for every property, recursing `properties` only.

    No $ref appears inside a `properties` subtree in this schema (only inside
    `items`), so this never needs to resolve one.
    """
    out = {}

    def walk(node, prefix=""):
        for k, v in (node.get("properties") or {}).items():
            if not isinstance(v, dict):
                continue
            key = prefix + k
            if key == "$schema":
                continue
            entry = {"description": (v.get("description") or "").strip()}
            t = pick_type(v)
            if t is not None:
                entry["type"] = t
            vals = pick_enum(v)
            if vals:
                entry["enum"] = vals
            ex = [e for b in branches(v) for e in (b.get("examples") or [])]
            if ex:
                entry["examples"] = list(dict.fromkeys(
                    e for e in ex if isinstance(e, (str, int, float, bool))))
            for b in branches(v):
                if "default" in b:
                    entry["default"] = b["default"]
                    break
            url = doc_url(entry["description"])
            if url:
                entry["doc"] = url
            entry["managed"] = is_managed(key, entry["description"])
            out[key] = entry
            if v.get("properties"):
                walk(v, key + ".")

    walk(doc)
    return out


def build(doc, source=SCHEMA_URL, resolved=None, fetched=None):
    """The snapshot dict written to data/settings_schema.json."""
    validate(doc)
    return {
        "source": source,
        "resolved": resolved or source,
        "schema_id": doc.get("$id", ""),
        "fetched": fetched or "",
        "keys": flatten(doc),
    }


def serialize(snapshot):
    """Canonical on-disk form. Sorted and indented so a diff is reviewable —
    noticing when upstream reworded a description is the point of vendoring."""
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ------------------------------------------------------- snapshot + overlay

@functools.lru_cache(maxsize=1)
def vendored():
    """The committed snapshot. {} on any failure — never fatal."""
    try:
        data = json.loads(OFFICIAL_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Rebound wholesale by _fetch_official, never mutated in place: ThreadingHTTPServer
# means a request thread can be reading this while the fetch thread replaces it.
_live = {}
_generation = 0


def generation():
    """Bumped once per successful live fetch. Callers memoize against it."""
    return _generation


def official():
    """Vendored keys, overlaid by anything the live fetch found.

    The vendored data is the floor: live may add or replace an entry, never
    remove one.
    """
    base = vendored().get("keys") or {}
    live = _live  # single read — the fetch thread may rebind mid-call
    if not live:
        return base
    return {**base, **live}


def _get(url):
    req = urllib.request.Request(url, headers={"user-agent": "claude-ui"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode(errors="replace")


def _fetch_official():
    global _live, _generation
    try:
        doc = json.loads(_get(SCHEMA_URL))
        validate(doc)
        found = flatten(doc)
    except (OSError, ValueError):
        return
    if found:
        _live = found          # single rebind, built off to the side
        _generation += 1


def start_schema_fetch():
    threading.Thread(target=_fetch_official, daemon=True).start()


def env_var_names():
    """Documented env var names, from the snapshot's env.* keys."""
    return {k[4:] for k in official() if k.startswith("env.") and "." not in k[4:]}


def hook_events():
    """Documented lifecycle hook event names, from the snapshot's hooks.* keys."""
    return sorted(k[6:] for k in official()
                  if k.startswith("hooks.") and "." not in k[6:])


def known_top_level():
    """Top-level keys the official schema documents."""
    return {k.split(".")[0] for k in official()}


# ----------------------------------------------------------------- merging

def resolve_doc(key, desc=""):
    """Always a URL: the official anchored one, else the official page, else the
    fallback table, else the settings reference."""
    url = doc_url(desc)
    if url:
        return url
    for rx, page in DOC_FALLBACKS:
        if rx.search(key):
            return DOC_BASE + page
    return DOC_BASE + "settings"


def _short(desc, limit=160):
    """First sentence of an official description, for keys with no curated one."""
    text = " ".join((desc or "").split())
    # the "(Managed settings only)" opener is redundant on a row already filed
    # under the managed group, and it costs a third of the line
    text = re.sub(r"^\((?:Managed|Admin)[^)]*\)\s*", "", text)
    # cut at the first sentence end that isn't inside a URL or an abbreviation
    m = re.search(r"(?<![A-Z])\.\s+(?=[A-Z(])", text)
    if m and m.start() <= limit:
        return text[:m.start() + 1]
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip(",;:") + "…"


def merge(entries):
    """Hand-written entries with official facts applied over them.

    Hand curation wins wherever the two describe the same thing in different
    vocabularies (control type, category) or wherever a human wrote something
    better (the short desc). Official wins on facts it is authoritative about
    (allowed values, defaults, docs URL). Neither ever overwrites the source
    list — this runs at boot, over a copy.
    """
    off = official()
    out = []
    for raw in entries:
        s = dict(raw)
        o = off.get(s["key"])

        if o:
            # allowed values: hand order first, official enum and examples added
            extra = list(o.get("enum") or []) + list(o.get("examples") or [])
            if extra:
                vals = list(s.get("values") or [])
                vals += [v for v in extra if v not in vals]
                s["values"] = vals
            # defaults: official is authoritative. `None` is a real default for
            # some keys, so test membership, not truthiness.
            if "default" in o:
                s["default"] = o["default"]
            if not s.get("desc"):
                s["desc"] = _short(o.get("description"))
            s["doc"] = resolve_doc(s["key"], o.get("description"))
            if o.get("managed"):
                s["managed"] = True
        else:
            s["doc"] = resolve_doc(s["key"])
            # Not listed in the official schema. additionalProperties is true,
            # so absence is not disproof — the key may be real and documented
            # elsewhere. Badge it, don't hide it.
            s["unverified"] = True
        out.append(s)
    return out


def help_payload(keys):
    """Long-form help for /api/schema-help, restricted to `keys`.

    Only the prose lives here — everything a row needs to *render* is already
    inlined into the page. Keeping the two apart is what keeps the eager payload
    small while the 340 env descriptions stay reachable.
    """
    off = official()
    out = {}
    for key in keys:
        o = off.get(key)
        if not o or not o.get("description"):
            continue
        item = {"description": o["description"]}
        for f in ("type", "enum", "doc"):
            if o.get(f):
                item[f] = o[f]
        if "default" in o:
            item["default"] = o["default"]
        if o.get("managed"):
            item["managed"] = True
        out[key] = item
    return out
