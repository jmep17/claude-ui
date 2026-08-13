"""Local search index over everything already on disk: your own items,
installed plugins, on-disk marketplaces, and Claude Code's own plugin catalog
cache (~/.claude/plugins/plugin-catalog-cache.json).

Zero network, zero subprocess — this is a hard invariant, not a preference.
Phase 3 adds the two remote groups this module deliberately skips ("official"
and "community"); Phase 2 adds the install actions resolve_install() below is
a stub for.

Everything here is read-only. A search is built from four corpora:

  yours      scan_items() — your own skills/commands/agents/output-styles
  installed  plugins_state() — plugins actually on disk under plugins/
  ondisk     marketplace.json entries not yet fetched to disk, plus the
             marketplace registrations themselves
  ondisk     (also) plugin-catalog-cache.json entries whose marketplace is
             one of the above — see _cache_entries() for the dedupe and the
             judgment call on cache entries whose marketplace is *not*
             registered locally

`yours` is never cached (see _CACHE below); the rest is memoized per
_signature().
"""

from pathlib import Path
from urllib.parse import urlsplit
import json
import math
import re

from .core import config_dir, discover_cache_path, tilde
from .items import scan_items
from . import plugins
from .settings import settings_state


# ---------------------------------------------------------------- entry shape

_ENTRY_FIELDS = (
    "id", "kind", "group", "name", "parent", "description", "author",
    "category", "tags", "marketplace", "installs", "counts", "hooks",
    "tokens", "source", "pinned", "homepage", "state", "installable",
    "blocked", "path",
)

def _entry(**kw):
    """Every Entry has every field, even when a corpus has nothing to say
    about it — callers below pass what they know and this fills the rest."""
    e = {f: kw.get(f) for f in _ENTRY_FIELDS}
    if e["tags"] is None:
        e["tags"] = []
    if e["tokens"] is None:
        e["tokens"] = {"always_on": None, "on_invoke": None}
    if e["hooks"] is None:
        e["hooks"] = False
    if e["installable"] is None:
        e["installable"] = False
    if e["blocked"] is None:
        e["blocked"] = False
    if e["counts"] is None:
        e["counts"] = {}
    return e


_KIND_SINGULAR = {"skills": "skill", "commands": "command", "agents": "agent",
                  "output-styles": "output-style"}
# Plugin component kinds that map onto an Entry `kind`; "hooks" components
# (plugins._plugin_hooks) are deliberately excluded — they aren't independently
# searchable, they just flip the parent plugin's `hooks` bool.
_COMPONENT_KIND = {"agents": "agent", "commands": "command", "skills": "skill",
                   "output-styles": "output-style", "mcp": "mcp"}

TIER = {"yours": 60, "installed": 60, "ondisk": 40, "official": 20, "community": 0}

# Kept in sync with remote.DISCOVER_SOURCES by hand rather than imported: this
# module is the one remote.py imports (for the sanitization helpers below), so
# the reverse import would be a cycle.
_DISCOVER_SOURCES = ("official", "community")


# ------------------------------------------------------------- sanitization
#
# Third-party-influenced data: Claude Code writes the cache file, but the
# marketplace content inside it is not Anthropic-authored for community
# entries, so every string pulled from it is treated as attacker-controlled —
# the same posture schema.py takes with the live-fetched settings schema.

# C0, DEL, C1, zero-width space, and the bidi control characters that let a
# name render right-to-left — the oldest filename-spoofing trick, and a
# catalog name is exactly as attacker-controlled as a filename.
_STRIP = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f​‪-‮⁦-⁩]"
)

def clean_str(s, limit):
    """A trusted-enough string, control/bidi characters stripped and length
    capped — or None when there is nothing usable. Truncates, never raises."""
    if not isinstance(s, str):
        return None
    s = _STRIP.sub("", s).strip()
    return s[:limit] if s else None

_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")

def clean_sha(s):
    """A 64-hex sha, or None — anything else is dropped, not sanitized."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    return s if _SHA_RE.match(s) else None

def safe_url(u):
    """http(s) URL with a real host and no control/space characters, capped
    at 300 chars — or None. Mirrors schema.py's _URL_RE precedent: refuse and
    say nothing rather than pass through a javascript:/data: URL."""
    if not isinstance(u, str) or not u or len(u) > 300:
        return None
    if any(ord(c) < 0x21 for c in u):
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return u


# --------------------------------------------------------- plugin-catalog-cache
#
# Claude Code's own cache, undocumented format, never ours to write. Read
# defensively: any shape surprise degrades to "treat as absent", never a
# crash, and — the single most important invariant of this module — an absent
# or invalid cache must never take the other three corpora down with it.

CACHE_FILE = "plugin-catalog-cache.json"
MAX_CACHE_SIZE = 8 * 1024 * 1024
MIN_CACHE_ENTRIES = 20
MIN_NAME_FRACTION = 0.9

_PID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*$")

def _cache_path():
    return plugins.plugins_root() / CACHE_FILE

def _read_cache_file(path):
    """(plugins: {pid: raw entry}, fetched_at, reason). `reason` is None on
    success; a non-None reason always pairs with an empty plugins dict — the
    caller does not need to check both."""
    try:
        st = path.stat()
    except OSError:
        return {}, "", "no cache file"
    if st.st_size > MAX_CACHE_SIZE:
        return {}, "", f"cache file is over {MAX_CACHE_SIZE} bytes"
    try:
        doc = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return {}, "", f"unreadable: {e}"
    if not isinstance(doc, dict):
        return {}, "", "top level is not a JSON object"
    if doc.get("version") != 1:
        return {}, "", f"unexpected version: {doc.get('version')!r}"
    catalog = doc.get("catalog")
    raw_plugins = catalog.get("plugins") if isinstance(catalog, dict) else None
    if not isinstance(raw_plugins, dict) or len(raw_plugins) < MIN_CACHE_ENTRIES:
        return {}, "", f"catalog.plugins has fewer than {MIN_CACHE_ENTRIES} entries"
    # keys that don't match the id pattern are dropped silently and do not
    # count against the 90% name-completeness floor below
    valid = {k: v for k, v in raw_plugins.items()
             if _PID_RE.match(k) and isinstance(v, dict)}
    if not valid:
        return {}, "", "no plugin keys matched the id pattern"
    named = sum(1 for v in valid.values() if isinstance(
        (v.get("marketplace_entry") or {}).get("name")
        if isinstance(v.get("marketplace_entry"), dict) else None, str))
    if named < len(valid) * MIN_NAME_FRACTION:
        return {}, "", "fewer than 90% of entries carry a marketplace_entry.name"
    fetched_at = doc.get("fetchedAt") if isinstance(doc.get("fetchedAt"), str) else ""
    return valid, fetched_at, None

def _norm_source(raw, fallback_sha):
    """marketplace_entry.source, normalized into the Entry `source` shape.

    Two shapes seen in the cache: a dict ({source, url, path, ref, sha}) for
    most entries, a bare relative-path string (e.g. './plugins/agent-sdk-dev')
    for others — Claude Code's own inconsistency, not ours to fix. Returns
    (source dict, pinned bool)."""
    if isinstance(raw, dict):
        sha = clean_sha(raw.get("sha")) or clean_sha(fallback_sha)
        source = {
            "kind": clean_str(raw.get("source"), 40),
            "url": safe_url(raw.get("url")) if isinstance(raw.get("url"), str) else None,
            "path": clean_str(raw.get("path"), 300),
            "ref": clean_str(raw.get("ref"), 100),
            "sha": sha,
        }
        return source, bool(sha)
    if isinstance(raw, str):
        sha = clean_sha(fallback_sha)
        source = {"kind": "path", "url": None,
                  "path": clean_str(raw, 300), "ref": None, "sha": sha}
        return source, True
    return {"kind": None, "url": None, "path": None, "ref": None, "sha": None}, False

def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None

def _cache_entries(settings_data, known_ids, known_markets):
    """Entries from the cache file, deduped against what the other corpora
    already found. Returns (entries, fetched_at, reason)."""
    raw_plugins, fetched_at, reason = _read_cache_file(_cache_path())
    out = []
    for pid, pe in raw_plugins.items():
        if pid in known_ids:
            continue  # already on disk — plugins_state()/marketplace scan has it
        _, mname = pid.split("@", 1)
        if mname not in known_markets:
            # This plugin's marketplace isn't registered on this machine at
            # all, so "ondisk" (which means "a local marketplace points at
            # this") is the wrong label for it. It also isn't "official" or
            # "community" — those are Phase 3's remote catalogs, fetched live
            # from Anthropic, and this is neither: it's local data (Claude
            # Code's own cache file) about a marketplace we've never seen.
            # Entry.group has no honest slot for that, so — per the plan's
            # explicit permission to punt here — these are left out of the
            # index for this phase rather than mislabeled.
            continue
        me = pe.get("marketplace_entry")
        me = me if isinstance(me, dict) else {}
        name = clean_str(me.get("name"), 80) or pid.split("@", 1)[0]
        desc = clean_str(me.get("description"), 300)
        author = me.get("author")
        if isinstance(author, dict):
            author = author.get("name")
        author = clean_str(author, 60) if isinstance(author, str) else None
        category = clean_str(me.get("category"), 40)
        tags = [t for t in (clean_str(t, 30) for t in (me.get("tags") or [])
                            if isinstance(t, str)) if t][:20]
        homepage = (safe_url(me.get("homepage"))
                   if isinstance(me.get("homepage"), str) else None)
        fallback_sha = pe.get("sha") or pe.get("source_sha")
        source, pinned = _norm_source(me.get("source"), fallback_sha)
        blocked = is_blocked(mname, source, settings_data)
        skills = me.get("skills")
        skills = skills if isinstance(skills, list) else []
        out.append(_entry(
            id=pid, kind="plugin", group="ondisk", name=name, parent=None,
            description=desc, author=author, category=category, tags=tags,
            marketplace=mname, installs=_int_or_none(me.get("installs")),
            counts={"skills": len(skills)} if skills else {}, hooks=False,
            tokens={"always_on": None, "on_invoke": None},
            source=source, pinned=pinned, homepage=homepage,
            state="available", installable=not blocked, blocked=blocked,
            path=None,
        ))
        for s in skills:
            sname = s.get("name") if isinstance(s, dict) \
                else s if isinstance(s, str) else None
            sname = clean_str(sname, 80)
            if not sname:
                continue
            # component entries have no description in the cache — they
            # render/match on name only, with the "from <plugin>" relationship
            # carried by `parent`
            out.append(_entry(
                id=f"{pid}/skills/{sname}", kind="skill", group="ondisk",
                name=sname, parent=pid, marketplace=mname, state="available",
                installable=False, blocked=blocked,
            ))
    return out, fetched_at, reason


# ------------------------------------------------------------------ policy
#
# blockedMarketplaces and strictKnownMarketplaces are documented settings.json
# keys (settings.py MANAGED_KEYS, around line 594) that nothing enforces yet —
# this is the first enforcement point.

def _owner_repo(source):
    """'owner/repo' out of a github.com source URL, or None."""
    url = None
    if isinstance(source, dict):
        url = source.get("url")
    elif isinstance(source, str):
        url = source
    if not isinstance(url, str) or not url:
        return None
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s.]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None

def _match_names(marketplace_name, source):
    names = set()
    if marketplace_name:
        names.add(str(marketplace_name).lower())
    repo = _owner_repo(source)
    if repo:
        o, r = repo.split("/", 1)
        names.add(f"{o}/{r}".lower())
        names.add(f"{r}/{o}".lower())  # "both directions" per the plan
    return names

def is_blocked(marketplace_name, source, settings):
    """Whether policy blocks this marketplace: a denylist entry wins outright;
    a *non-empty* allowlist blocks everything not on it; empty/absent blocks
    nothing. Matches marketplace name and 'owner/repo' form of the source,
    case-insensitive, both directions."""
    settings = settings or {}
    blocked = {str(x).lower() for x in (settings.get("blockedMarketplaces") or [])
              if isinstance(x, str)}
    strict = {str(x).lower() for x in (settings.get("strictKnownMarketplaces") or [])
             if isinstance(x, str)}
    names = _match_names(marketplace_name, source)
    if names & blocked:
        return True
    if strict and not (names & strict):
        return True
    return False


# --------------------------------------------------------------- corpora

def _yours_entries():
    """Your own items — never cached, recomputed on every call. Cheap (already
    done on every /api/state), and the whole point is that a skill created 30
    seconds ago is immediately findable."""
    out = []
    for type_, singular in _KIND_SINGULAR.items():
        for it in scan_items(type_):
            out.append(_entry(
                id=f"yours:{type_}:{it['name']}", kind=singular, group="yours",
                name=it["name"],
                description=clean_str(it.get("description"), 300),
                state="enabled" if it.get("enabled") else "disabled",
                path=it.get("path"),
            ))
    return out

def _installed_entries():
    """Plugins plugins_state() finds on disk, plus their components — never
    blocked: policy exists to gate installing something new, not something
    you already have."""
    st = plugins.plugins_state()
    out = []
    for p in st["plugins"]:
        pid = p["id"]
        counts = p.get("counts") or {}
        out.append(_entry(
            id=pid, kind="plugin", group="installed", name=p["name"],
            description=clean_str(p.get("description"), 300),
            marketplace=p.get("marketplace"), counts=counts,
            hooks=bool(counts.get("hooks")),
            source={"kind": "local", "url": None, "path": p.get("path"),
                   "ref": None, "sha": None},
            state=p.get("state"), installable=False, path=p.get("path"),
        ))
        for c in p.get("components") or []:
            kind = _COMPONENT_KIND.get(c.get("kind"))
            if kind is None:
                continue
            out.append(_entry(
                id=f"{pid}/{c['kind']}/{c['name']}", kind=kind,
                group="installed", name=c["name"], parent=pid,
                description=clean_str(c.get("description"), 300),
                marketplace=p.get("marketplace"), state=p.get("state"),
                installable=False, path=c.get("path"),
            ))
    return out, st.get("error")

def _ondisk_entries(settings_data):
    """Marketplaces registered on this machine, plus the catalogue entries
    they list that are *not* already on disk (a git source not yet fetched —
    plugins_state()/plugins._plugin_dirs() only reports what's physically
    present, so a catalogue entry with no local directory is exactly the set
    this group is for)."""
    out = []
    markets, _err = plugins._marketplaces()
    for mname, mroot in markets:
        out.append(_entry(
            id=f"marketplace:{mname}", kind="marketplace", group="ondisk",
            name=mname, marketplace=mname,
            source={"kind": "marketplace", "url": None, "path": tilde(mroot),
                   "ref": None, "sha": None},
            state="available", path=tilde(mroot),
        ))
        for pname, (desc, pdir) in plugins._catalogue(mroot).items():
            if pdir is not None:
                continue  # on disk — plugins_state() already covers it
            source = {"kind": "marketplace", "url": None, "path": None,
                      "ref": None, "sha": None}
            blocked = is_blocked(mname, source, settings_data)
            out.append(_entry(
                id=f"{pname}@{mname}", kind="plugin", group="ondisk",
                name=pname, description=clean_str(desc, 300),
                marketplace=mname, source=source, state="available",
                installable=not blocked, blocked=blocked,
            ))
    return out


# ---------------------------------------------------------------------- discover
#
# remote.py's own fetch already sanitizes and trims before it writes these
# files, but they are re-validated here anyway, at the same defensiveness
# level as the on-disk marketplace corpus above: a TOCTOU window or a
# hand-edited file means "this is our own file" is not quite proof enough to
# skip the checks.

def _read_discover_file(path):
    """(raw entries list, fetched_at) from a remote.py-written cache file.
    Any shape surprise degrades to "no entries", never a crash — same
    invariant as _read_cache_file() above."""
    try:
        path.stat()
    except OSError:
        return [], None
    try:
        doc = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, ValueError):
        return [], None
    if not isinstance(doc, dict):
        return [], None
    fetched_at = doc.get("fetched_at") if isinstance(doc.get("fetched_at"), str) else None
    raw = doc.get("entries")
    return (raw if isinstance(raw, list) else []), fetched_at

def _discover_entries(group, settings_data):
    """Entries from <config_dir>/claude-ui-discover/<group>.json, or ([], None)
    when it doesn't exist yet (no consent given, or never refreshed)."""
    raw_entries, fetched_at = _read_discover_file(discover_cache_path(group))
    out = []
    for re_ in raw_entries:
        if not isinstance(re_, dict):
            continue
        name = clean_str(re_.get("name"), 80)
        if not name:
            continue
        author = re_.get("author")
        author = clean_str(author, 60) if isinstance(author, str) else None
        homepage = safe_url(re_.get("homepage")) if isinstance(re_.get("homepage"), str) else None
        tags = [t for t in (clean_str(t, 30) for t in (re_.get("tags") or [])
                            if isinstance(t, str)) if t][:20]
        raw_source = re_.get("source") if isinstance(re_.get("source"), dict) else {}
        source = {
            "kind": clean_str(raw_source.get("kind"), 40),
            "url": safe_url(raw_source.get("url")) if isinstance(raw_source.get("url"), str) else None,
            "path": clean_str(raw_source.get("path"), 300),
            "ref": clean_str(raw_source.get("ref"), 100),
            "sha": clean_sha(raw_source.get("sha")),
        }
        blocked = is_blocked(None, source, settings_data)
        pid = f"{name}@{group}"
        out.append(_entry(
            id=pid, kind="plugin", group=group, name=name,
            description=clean_str(re_.get("description"), 300),
            author=author, category=clean_str(re_.get("category"), 40),
            tags=tags, installs=_int_or_none(re_.get("installs")),
            source=source, state="available", installable=not blocked,
            blocked=blocked, homepage=homepage,
        ))
        skills = re_.get("skills")
        for s in (skills if isinstance(skills, list) else []):
            sname = clean_str(s, 80) if isinstance(s, str) else None
            if not sname:
                continue
            out.append(_entry(
                id=f"{pid}/skills/{sname}", kind="skill", group=group,
                name=sname, parent=pid, state="available",
                installable=False, blocked=blocked,
            ))
    return out, fetched_at


# --------------------------------------------------------------------- caching
#
# Rebound wholesale, never mutated in place — see schema.py ~line 252: a
# ThreadingHTTPServer request thread can be reading this while another
# rebuilds it, and a single-reference reassignment is what stays safe there.

INDEX_VERSION = 1
_CACHE = None  # (signature, {"entries", "fetched_at", "cache_reason"}) or None

def _stat_sig(p):
    try:
        st = Path(p).stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None

def _signature():
    cdir = config_dir()
    markets, _ = plugins._marketplaces()
    market_sig = tuple(sorted((n, _stat_sig(r)) for n, r in markets))
    discover_sig = tuple(_stat_sig(discover_cache_path(s)) for s in _DISCOVER_SOURCES)
    return (INDEX_VERSION, str(cdir), _stat_sig(_cache_path()),
            _stat_sig(cdir / "settings.json"), market_sig, discover_sig)

def _build_static_index():
    settings_data = settings_state()["data"]
    installed, _serr = _installed_entries()
    ondisk = _ondisk_entries(settings_data)
    known_ids = {e["id"] for e in installed + ondisk if e["kind"] == "plugin"}
    known_markets = {n for n, _ in plugins._marketplaces()[0]}
    cache_entries, fetched_at, cache_reason = _cache_entries(
        settings_data, known_ids, known_markets)
    discover_entries = []
    discover_fetched_at = {}
    for src in _DISCOVER_SOURCES:
        ents, f_at = _discover_entries(src, settings_data)
        discover_entries += ents
        discover_fetched_at[src] = f_at
    return {"entries": installed + ondisk + cache_entries + discover_entries,
            "fetched_at": fetched_at, "cache_reason": cache_reason,
            "discover_fetched_at": discover_fetched_at}

def _static_index():
    global _CACHE
    sig = _signature()
    cache = _CACHE
    if cache is None or cache[0] != sig:
        cache = (sig, _build_static_index())
        _CACHE = cache
    return cache[1]

def build_index():
    """The full index: the memoized static portion plus fresh `yours` entries."""
    static = _static_index()
    return {"entries": static["entries"] + _yours_entries(),
            "fetched_at": static["fetched_at"],
            "cache_reason": static["cache_reason"],
            "discover_fetched_at": static["discover_fetched_at"]}

def catalog_state():
    """/api/catalog payload: counts per group, cache freshness, policy as
    configured. No search — just index metadata."""
    idx = build_index()
    counts = {}
    for e in idx["entries"]:
        counts[e["group"]] = counts.get(e["group"], 0) + 1
    settings_data = settings_state()["data"]
    return {
        "counts": counts,
        "fetched_at": idx["fetched_at"],
        "cache_reason": idx["cache_reason"],
        "discover_fetched_at": idx["discover_fetched_at"],
        "policy": {
            "blocked_marketplaces": list(settings_data.get("blockedMarketplaces") or []),
            "strict_known_marketplaces":
                list(settings_data.get("strictKnownMarketplaces") or []),
        },
    }


# ------------------------------------------------------------------- scoring

W_NAME_EXACT, W_NAME_PREFIX, W_NAME_WORD, W_NAME_SUB, W_NAME_FUZZY = 1000, 600, 400, 300, 120
W_COMP_EXACT, W_COMP_SUB = 350, 180
W_TAGCAT_EXACT, W_TAGCAT_SUB = 260, 120
W_AUTHOR = 200
W_DESC_WORD, W_DESC_SUB = 90, 60
W_MARKET_SUB = 40

BROWSE_PER_GROUP = 20  # a reasonable cap so no one group crowds out the rest
MAX_LIMIT = 100

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")

def _words(text):
    return [w for w in _WORD_SPLIT.split(text) if w]

def _fuzzy_hit(q, s):
    """Subsequence match, ported from static/ui.js's fuzzy() (~line 707): every
    occurrence of the query's first character is tried as an anchor, word-start
    continuations score higher, and the best run wins. Used here only to decide
    whether the term matches at all — the weight it contributes is the flat
    W_NAME_FUZZY tier, like every other field; the magnitude this algorithm
    computes is not otherwise used (the length tiebreak below plays that role
    instead, once per entry rather than once per fuzzy hit)."""
    if not q:
        return False
    a = s.find(q[0])
    while a >= 0:
        i, run, ok = a, False, True
        for ch in q:
            j = s.find(ch, i)
            if j < 0:
                ok = False
                break
            i = j + 1
            run = True
        if ok:
            return True
        a = s.find(q[0], a + 1)
    return False

def _term_score(term, entry, id_to_name):
    """(weight, why-tag) for this term's best-matching field on this entry, or
    None if the term matches nothing — AND semantics: one None kills the entry."""
    scores = []
    name = (entry.get("name") or "").lower()
    if name == term:
        scores.append((W_NAME_EXACT, "name_exact"))
    elif name.startswith(term):
        scores.append((W_NAME_PREFIX, "name_prefix"))

    if len(term) < 2:  # floor: a 1-char term scores only exact/prefix-on-name
        return max(scores, key=lambda x: x[0]) if scores else None

    if term in _words(name):
        scores.append((W_NAME_WORD, "name_word"))
    if term in name:
        scores.append((W_NAME_SUB, "name_sub"))
    if _fuzzy_hit(term, name):  # fuzzy: name only, never description — see module docstring
        scores.append((W_NAME_FUZZY, "name_fuzzy"))

    comp = (id_to_name.get(entry.get("parent") or "") or "").lower()
    if comp:
        if comp == term:
            scores.append((W_COMP_EXACT, "comp_exact"))
        elif term in comp:
            scores.append((W_COMP_SUB, "comp_sub"))

    tagcat = [t.lower() for t in ([entry.get("category")] + list(entry.get("tags") or []))
             if t]
    if term in tagcat:
        scores.append((W_TAGCAT_EXACT, "tagcat_exact"))
    elif any(term in t for t in tagcat):
        scores.append((W_TAGCAT_SUB, "tagcat_sub"))

    author = (entry.get("author") or "").lower()
    if author and term in author:
        scores.append((W_AUTHOR, "author"))

    market = (entry.get("marketplace") or "").lower()
    if market and term in market:
        scores.append((W_MARKET_SUB, "market_sub"))

    if len(term) >= 3:  # floor: under 3 chars never matches descriptions
        desc = (entry.get("description") or "").lower()
        if desc:
            if term in _words(desc):
                scores.append((W_DESC_WORD, "desc_word"))
            elif term in desc:
                scores.append((W_DESC_SUB, "desc_sub"))

    return max(scores, key=lambda x: x[0]) if scores else None

def _score_entry(terms, entry, id_to_name):
    total, why = 0, []
    for term in terms:
        r = _term_score(term, entry, id_to_name)
        if r is None:
            return None
        w, tag = r
        total += w
        why.append(tag)
    total += TIER.get(entry["group"], 0)
    installs = entry.get("installs") or 0
    total += min(30, round(math.log10(1 + installs) * 6))
    total -= len(entry.get("name") or "") / 100
    return total, why

def _browse(entries, limit):
    """Empty-query listing: best tier/popularity first, capped per group so no
    one source crowds out the rest of the page."""
    by_group = {}
    out = []
    ranked = sorted(entries, key=lambda e: (
        -TIER.get(e["group"], 0), -(e.get("installs") or 0), e["name"]))
    for e in ranked:
        g = e["group"]
        if by_group.get(g, 0) >= BROWSE_PER_GROUP:
            continue
        by_group[g] = by_group.get(g, 0) + 1
        out.append({"entry": e, "score": None, "why": []})
        if len(out) >= limit:
            break
    return out

def search(query, limit=20, root=None):
    """Ranked hits for `query` over the local index. Every whitespace-split
    term must hit something on an entry (AND); a term's score is the max over
    the fields it matched, not the sum. Empty query is browse mode (see
    _browse()).

    `root`: reserved for scoping results to entries under a path prefix (would
    apply to scan_items()-sourced "yours" entries, which carry a real `path`).
    Nothing in this phase needs it, so it is accepted and ignored — kept as a
    parameter so the endpoint signature does not have to change to add it
    later.
    """
    limit = max(1, min(int(limit) if limit else 20, MAX_LIMIT))
    idx = build_index()
    entries = idx["entries"]
    terms = (query or "").strip().lower().split()
    if not terms:
        return _browse(entries, limit)
    id_to_name = {e["id"]: e["name"] for e in entries}
    hits = []
    for e in entries:
        r = _score_entry(terms, e, id_to_name)
        if r is None:
            continue
        score, why = r
        hits.append((score, e, why))
    hits.sort(key=lambda t: (-t[0], t[1]["name"]))
    return [{"entry": e, "score": score, "why": why} for score, e, why in hits[:limit]]


# --------------------------------------------------------------------- install

def get_entry(pid):
    """The index's own Entry for `pid`, or None. Read-only lookup, no policy
    checks (unlike resolve_install() below) — for callers like
    /api/skill-audit that need to resolve an id to its indexed fields
    without asking "is this installable", only "does this exist and what do
    we know about it". Mirrors resolve_install()'s security property: the
    request's copy of an id is used only to look it up here, then discarded —
    every field the caller acts on afterward comes from this function's
    return value, never from the request body directly.
    """
    idx = build_index()
    by_id = {e["id"]: e for e in idx["entries"]}
    return by_id.get(pid)


def resolve_install(pid, scope=None):
    """The index's own stored key for `pid`, or a ValueError explaining the
    refusal — the gate in front of server.py's catalog-install action, which
    passes this return value (never the raw request string) to the `claude
    plugin install` subprocess. Resolving here means an unknown id, a
    non-installable component, or a blocked marketplace is refused before
    anything shells out.

    `scope` is accepted for the same forward-compatibility reason `root` is on
    search(): the caller decides which store an install targets, and this
    function does not act on it — it only answers "is this installable at
    all".
    """
    idx = build_index()
    by_id = {e["id"]: e for e in idx["entries"]}
    e = by_id.get(pid)
    if e is None:
        raise ValueError(f"{pid}: not in the index")
    if e.get("blocked"):
        raise ValueError(f"{pid}: blocked by policy (blockedMarketplaces or "
                         "strictKnownMarketplaces)")
    if not e.get("installable"):
        raise ValueError(f"{pid}: not installable on its own"
                         if e.get("kind") != "plugin" else
                         f"{pid}: already installed or not available to install")
    return e["id"]
