"""Guarded fetch of Anthropic's two public plugin catalogs, plus the two
skills.sh lookups (per-skill audit, and free-text search) — the only network
calls this app makes on the user's behalf against non-Anthropic-docs URLs.

This module knows nothing about what a plugin or skill is. The catalog half
is a generic fetch-a-pre-registered-document utility with a hardcoded, frozen
allowlist of sources (_SOURCES below). There is deliberately no function
anywhere in this module that accepts a URL from a caller — every request is
built here from a hardcoded prefix plus escaped/encoded arguments, never from
a string a caller hands over. That is the hard security invariant this module
exists to enforce: a bug anywhere else in the codebase that calls into this
module with attacker-influenced input can, at worst, name an unknown source
key (refused before any network I/O) or push text through urlencode() into a
query string — it can never steer the request to a different host.

Consent is enforced here too, not just by server.py choosing to gate its
routes: every fetching function refuses to run when no consent is on record.
A caller bug elsewhere cannot fire an unconsented fetch.

The three things that reach the network, and what each costs the user:

  fetch_source()  — a public, static JSON document (a marketplace.json) from
    GitHub. No query, no user data. `ok` consent for that source means
    exactly "you may download this document from this URL", nothing more.

  audit_skill()   — skills.sh's security verdict for one named skill the user
    clicked Audit on. Sends that skill's name. Gated on skills_sh.ok.

  search_skills() — skills.sh's search endpoint. This one sends *what the
    user typed* to a third party, so it is gated on the separate, stronger
    skills_sh.query_ok flag, and server.py only reaches it from an explicit
    press of a Search button — never from the Discover tab's as-you-type
    local search, which stays entirely offline. Results are never cached to
    disk: a cache file of remote searches would be a record of what the user
    typed sitting in their config directory.
"""

from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlsplit
import json
import urllib.error
import urllib.request

from . import catalog
from .core import atomic_write, discover_cache_path, read_cfg, write_cfg


# ------------------------------------------------------------------ sources

def _validate_marketplace_doc(doc):
    """(ok, reason) — the same "refuse and say why" posture as schema.py's
    validate(): a truncated or reshaped upstream document is refused outright
    rather than partially trusted."""
    if not isinstance(doc, dict):
        return False, "top level is not a JSON object"
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        return False, "no 'plugins' array"
    if not plugins:
        return False, "'plugins' array is empty"
    named = sum(1 for p in plugins
               if isinstance(p, dict) and isinstance(p.get("name"), str) and p.get("name"))
    if named < len(plugins) * 0.9:
        return False, "fewer than 90% of entries carry a name"
    return True, None


# Frozen at import time. Every value here is a literal — nothing computed from
# a request ever reaches this dict, and nothing computed from this dict's
# values (other than the request itself) ever reaches urlopen.
_SOURCES = {
    "official": {
        "url": "https://raw.githubusercontent.com/anthropics/claude-plugins-official/main/.claude-plugin/marketplace.json",
        "cap": 1_000_000,   # real doc is ~169KB, plenty of headroom
        "timeout": 10,
        "validate": _validate_marketplace_doc,
    },
    "community": {
        "url": "https://raw.githubusercontent.com/anthropics/claude-plugins-community/main/.claude-plugin/marketplace.json",
        "cap": 3_000_000,   # real doc is ~1.5MB
        "timeout": 15,
        "validate": _validate_marketplace_doc,
    },
}

for _name, _cfg in _SOURCES.items():
    assert _cfg["url"].startswith("https://"), f"{_name}: source URL must be https"

DISCOVER_SOURCES = tuple(_SOURCES)  # ("official", "community") — consent_get()'s defaults


# -------------------------------------------------------------------- consent
#
# Stored in .claude-ui.json (core.read_cfg/write_cfg — the existing gitignored
# machine-local config file), under a "discover" key:
#   {"discover": {"official": {"ok": true, "at": "..."},
#                 "community": {"ok": false},
#                 "skills_sh": {"ok": true, "query_ok": false, "at": "..."}}}
# skills_sh carries two flags, not one: `ok` (ask about one named skill) and
# the strictly stronger `query_ok` (send what the user types to the search
# endpoint). See consent_set_skills_sh().

def _now_iso():
    """Real clock by default; a function (not a bare call site) so tests can
    monkeypatch it the same way settings.py's _get is monkeypatched, keeping
    consent_set() callers deterministic without threading a timestamp through
    every call site."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def consent_get():
    """The discover-consent block, defaults filled in for official/community
    and skills_sh. A source with nothing recorded reads as
    {"ok": False, "at": None} (skills_sh also carries "query_ok": False —
    see consent_set_skills_sh() for what that second flag means, and
    search_skills() for the only thing that checks it)."""
    discover = read_cfg().get("discover")
    discover = discover if isinstance(discover, dict) else {}
    out = {}
    for src in DISCOVER_SOURCES:
        entry = discover.get(src)
        entry = entry if isinstance(entry, dict) else {}
        out[src] = {"ok": bool(entry.get("ok")),
                    "at": entry.get("at") if isinstance(entry.get("at"), str) else None}
    skills_sh = discover.get("skills_sh")
    skills_sh = skills_sh if isinstance(skills_sh, dict) else {}
    out["skills_sh"] = {"ok": bool(skills_sh.get("ok")),
                        "query_ok": bool(skills_sh.get("query_ok")),
                        "at": skills_sh.get("at") if isinstance(skills_sh.get("at"), str) else None}
    return out


def consent_set(source, ok, at=None):
    """Record consent (or its withdrawal) for one of DISCOVER_SOURCES
    (official/community — NOT skills_sh, which has its own shape and its
    own setter, consent_set_skills_sh(), below). `at` is injectable for
    tests; production callers leave it unset and get the real clock."""
    if source not in DISCOVER_SOURCES:
        raise ValueError(f"{source!r}: not a source this app manages")
    cfg = read_cfg()
    discover = cfg.get("discover")
    discover = dict(discover) if isinstance(discover, dict) else {}
    discover[source] = {"ok": bool(ok), "at": at or _now_iso()}
    cfg["discover"] = discover
    write_cfg(cfg)
    return discover[source]


def consent_set_skills_sh(ok, query_ok=None, at=None):
    """Record consent (or its withdrawal) for the two skills.sh lookups.

    Kept separate from consent_set() rather than folded into DISCOVER_SOURCES:
    that dict/function pair's shape is {"ok", "at"} for a static-document
    fetch; skills_sh's shape is {"ok", "query_ok", "at"} (see the consent-shape
    comment above). The two flags are nested, not parallel:

      ok        — "you may ask skills.sh about this one named skill I clicked
                   Audit on". Checked by audit_skill().
      query_ok  — "you may send what I type to skills.sh's search endpoint".
                   Strictly stronger. Checked by search_skills(), and by
                   nothing else.

    `query_ok=None` (the default) preserves whatever is already on disk, so
    the audit-consent flow — which knows nothing about search — cannot
    silently revoke a search consent granted earlier. Setting `ok=False`
    forces `query_ok=False` regardless of what was passed or stored:
    withdrawing the weaker consent must not leave the stronger one live.
    """
    cfg = read_cfg()
    discover = cfg.get("discover")
    discover = dict(discover) if isinstance(discover, dict) else {}
    existing = discover.get("skills_sh")
    existing = existing if isinstance(existing, dict) else {}
    ok = bool(ok)
    query = bool(existing.get("query_ok")) if query_ok is None else bool(query_ok)
    discover["skills_sh"] = {"ok": ok, "query_ok": query and ok,
                             "at": at or _now_iso()}
    cfg["discover"] = discover
    write_cfg(cfg)
    return discover["skills_sh"]


# ------------------------------------------------------------------- redirect
#
# The actual security boundary for an open redirect on the allowlisted host,
# or a compromised CDN edge: refuse to follow anywhere that is not https on
# the exact same host the request was made to. Built into a module-private
# opener rather than urllib.request.install_opener()'s process-wide default:
# schema.py and settings.py already call urllib.request.urlopen() for their
# own docs fetches, and installing globally would silently change their
# redirect handling too — this module's guarantees should not leak into code
# that never asked for them.

class _SameHostHTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Refuse (raise) unless the redirect target is https:// and shares
        the exact netloc (host[:port]) of the original request. Returning
        None here would make the caller silently receive the redirect
        response's own body instead of erroring — not what we want, so an
        invalid target raises instead of falling through."""
        orig = urlsplit(req.full_url)
        new = urlsplit(newurl)
        if new.scheme != "https" or new.netloc != orig.netloc:
            raise ValueError(
                f"refusing to follow redirect from {req.full_url!r} to "
                f"{newurl!r}: not https on the same host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameHostHTTPSRedirect())


# ---------------------------------------------------------------------- trim
#
# The raw fetched document (up to 1.5MB for community) is never kept on disk —
# only the Entry-subset fields catalog.py's discover groups need. Reuses
# catalog.py's own sanitization helpers rather than reimplementing them: this
# data is exactly as attacker-controlled as the on-disk marketplace corpus
# catalog.py already treats defensively.

def _trim_entries(raw_plugins):
    out = []
    for p in raw_plugins:
        if not isinstance(p, dict):
            continue
        name = catalog.clean_str(p.get("name"), 80)
        if not name:
            continue
        author = p.get("author")
        if isinstance(author, dict):
            author = author.get("name")
        author = catalog.clean_str(author, 60) if isinstance(author, str) else None
        homepage = (catalog.safe_url(p.get("homepage"))
                   if isinstance(p.get("homepage"), str) else None)
        installs = p.get("installs")
        installs = installs if isinstance(installs, int) and not isinstance(installs, bool) else None
        tags = [t for t in (catalog.clean_str(t, 30) for t in (p.get("tags") or [])
                            if isinstance(t, str)) if t][:20]
        source, _pinned = catalog._norm_source(p.get("source"), None)
        skills = p.get("skills") or []
        skill_names = [catalog.clean_str(
            s.get("name") if isinstance(s, dict) else s if isinstance(s, str) else None, 80)
            for s in skills] if isinstance(skills, list) else []
        skill_names = [s for s in skill_names if s]
        out.append({
            "name": name,
            "description": catalog.clean_str(p.get("description"), 300),
            "author": author,
            "category": catalog.clean_str(p.get("category"), 40),
            "tags": tags,
            "homepage": homepage,
            "installs": installs,
            "source": source,
            "skills": skill_names,
        })
    return out


# ----------------------------------------------------------------------- fetch

def fetch_source(name):
    """Fetch, validate, trim and cache one of _SOURCES's documents.

    `name` is the ONLY input — it must be a key of _SOURCES, never a URL.
    Raises ValueError on every failure path (unknown source, no consent,
    network error, oversized response, unreadable JSON, failed shape
    validation) — the same signal shape catalog.resolve_install() already
    uses, so server.py's error handling does not need two conventions.
    Returns a dict on success: {"ok": True, "detail", "count", "fetched_at"}.

    A failure never touches the disk cache: a truncated or malformed upstream
    response must never blank out a previously-good cache file (schema.py's
    "refuse and say why" posture, same rule here) — every raise below happens
    before the atomic_write near the end.
    """
    if name not in _SOURCES:
        raise ValueError(f"{name!r}: not a known discover source")
    consent = consent_get().get(name) or {}
    if not consent.get("ok"):
        raise ValueError(f"{name}: no consent recorded — enable it first")

    cfg = _SOURCES[name]
    req = urllib.request.Request(cfg["url"], headers={"user-agent": "claude-ui"})
    try:
        with _opener.open(req, timeout=cfg["timeout"]) as resp:
            # Read at most cap+1 bytes — never trust content-length, it is the
            # server's claim, not a fact — so an oversized body is refused
            # without materializing more of it than necessary to know that.
            data = resp.read(cfg["cap"] + 1)
    except urllib.error.URLError as e:
        raise ValueError(f"{name}: fetch failed: {e.reason}") from None
    except OSError as e:
        raise ValueError(f"{name}: fetch failed: {e}") from None

    if len(data) > cfg["cap"]:
        raise ValueError(f"{name}: response exceeded {cfg['cap']} bytes — refusing")

    try:
        doc = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"{name}: unreadable JSON: {e}") from None

    ok, reason = cfg["validate"](doc)
    if not ok:
        raise ValueError(f"{name}: {reason}")

    entries = _trim_entries(doc["plugins"])
    fetched_at = _now_iso()
    path = discover_cache_path(name)
    atomic_write(path, json.dumps(
        {"fetched_at": fetched_at, "source": name, "entries": entries},
        indent=2) + "\n")
    return {"ok": True, "detail": f"{len(entries)} entries", "count": len(entries),
            "fetched_at": fetched_at}


# ------------------------------------------------------------------- audit
#
# skills.sh's audit endpoint: a documented, public, multi-provider security
# verdict lookup for one named skill the user already has in view — NOT a
# search. Search lives in search_skills() below, behind its own stronger
# consent flag, because it is the one call that sends what the user typed.
#
# Different shape from _SOURCES on purpose: on-demand, per-skill, a small
# JSON response, not part of the search index. It is deliberately NOT
# disk-cached (unlike fetch_source()'s marketplace documents) — a security
# verdict should reflect current state each time it's asked for, not go
# stale silently in a cache file nothing prompts the user to refresh.

_AUDIT_PREFIX = "https://skills.sh/api/v1/skills/audit"
assert _AUDIT_PREFIX.startswith("https://"), "audit URL prefix must be https"

_AUDIT_CAP = 100_000       # real response is a small JSON verdict blob
_AUDIT_TIMEOUT = 8         # seconds — small lookup, no reason to wait longer
_AUDIT_STR_LIMIT = 2000    # generous cap for a verdict/explanation string
_AUDIT_KEY_LIMIT = 80
# Shallow nesting only — see _sanitize_audit_value. 4, not 3: the real
# response nests doc -> audits[] -> {provider…} -> categories[], and the
# categories array (the most useful part of a verdict) sits at depth 3, so a
# limit of 3 dropped it silently.
_AUDIT_MAX_DEPTH = 4
_AUDIT_LIST_LIMIT = 50     # a list this long from a "verdict" endpoint is not useful, cap it


def _sanitize_audit_value(v, depth):
    """A defensive pass-through for third-party JSON we don't control the
    schema of: skills.sh is not Anthropic, so its response is treated with
    the same "attacker-controlled string" posture catalog.py already takes
    with marketplace content, plus a shape allowlist on top (this JSON's
    *shape* is not pinned to a known schema the way _SOURCES's marketplace
    documents are, so there is no cfg["validate"] to call here — only a
    generic type/depth/size filter).

    Only str/int/float/bool survive as leaves (strings cleaned+truncated via
    catalog.clean_str); list/dict survive up to _AUDIT_MAX_DEPTH, each
    recursively sanitized and capped in size; every other type (null,
    NaN/Infinity already excluded by json.loads, anything exotic) is dropped
    silently rather than raising or being forwarded raw to the client.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return catalog.clean_str(v, _AUDIT_STR_LIMIT)
    if depth >= _AUDIT_MAX_DEPTH:
        return None
    if isinstance(v, list):
        out = []
        for item in v[:_AUDIT_LIST_LIMIT]:
            sv = _sanitize_audit_value(item, depth + 1)
            if sv is not None:
                out.append(sv)
        return out
    if isinstance(v, dict):
        out = {}
        for k, item in v.items():
            if not isinstance(k, str):
                continue
            ck = catalog.clean_str(k, _AUDIT_KEY_LIMIT)
            if not ck:
                continue
            sv = _sanitize_audit_value(item, depth + 1)
            if sv is not None:
                out[ck] = sv
        return out
    return None  # None/other JSON scalars we don't recognize — dropped, not forwarded


def audit_skill(source, skill):
    """Fetch skills.sh's audit verdict for one skill. `source` and `skill`
    are opaque path segments — NOT a URL, never accepted as one — matching
    fetch_source()'s "the only input is a lookup key" posture even though
    here the key has two parts.

    Refuses (raises ValueError, matching fetch_source()'s error-signal
    convention) when:
      - skills_sh.ok consent is not recorded — this is the only flag this
        function checks. skills_sh.query_ok is a *stricter* flag guarding
        search_skills(), i.e. "sending what I type to this company". This
        function's `skill` argument is never free text — it is the name of
        an entry the caller already resolved server-side, either out of the
        LOCAL catalog index or out of this server's own most recent search
        response (see server.py's /api/skill-audit, which resolves an id
        rather than trusting a raw name from the request body) — so
        query_ok does not apply here.
      - the assembled URL, after quote(seg, safe="")'ing both segments
        (percent-encoding everything, including "/"), does not start with
        _AUDIT_PREFIX — belt-and-suspenders against a pathological encoding
        bug letting a crafted source/skill escape the prefix.
      - the response is oversized, unreadable JSON, or not a JSON object.

    Returns {"ok": True, "data": <sanitized dict>} on success — the same
    {"ok": ...} envelope shape fetch_source() returns.
    """
    consent = consent_get().get("skills_sh") or {}
    if not consent.get("ok"):
        raise ValueError("skills_sh: no consent recorded — enable it first")

    url = f"{_AUDIT_PREFIX}/{quote(str(source), safe='')}/{quote(str(skill), safe='')}"
    if not url.startswith(_AUDIT_PREFIX):
        raise ValueError("skills_sh: assembled audit URL escaped its prefix — refusing")

    req = urllib.request.Request(url, headers={"user-agent": "claude-ui"})
    try:
        with _opener.open(req, timeout=_AUDIT_TIMEOUT) as resp:
            data = resp.read(_AUDIT_CAP + 1)
    except urllib.error.URLError as e:
        raise ValueError(f"skills_sh: fetch failed: {e.reason}") from None
    except OSError as e:
        raise ValueError(f"skills_sh: fetch failed: {e}") from None

    if len(data) > _AUDIT_CAP:
        raise ValueError(f"skills_sh: response exceeded {_AUDIT_CAP} bytes — refusing")

    try:
        doc = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"skills_sh: unreadable JSON: {e}") from None

    if not isinstance(doc, dict):
        raise ValueError("skills_sh: unexpected response shape (not a JSON object)")

    return {"ok": True, "data": _sanitize_audit_value(doc, 0)}


# ------------------------------------------------------------------ search
#
# skills.sh's search endpoint — the only call in this codebase that sends
# what the user typed to a third party, and the reason skills_sh consent has
# two flags instead of one.
#
# It is reached only from an explicit press of the Discover tab's "Search
# skills.sh" button (or Enter in that box, IME composition excluded). It is
# deliberately NOT on the as-you-type debounce that drives the local index
# search — that would ship partial keystrokes off-machine, which is the whole
# thing query_ok exists to prevent.
#
# Not disk-cached, unlike fetch_source()'s marketplace documents: a cache of
# remote search results is a log of what the user typed, sitting in their
# config directory. Ephemeral or nothing.
#
# This is the same endpoint the official `skills` npm CLI calls. The
# documented /api/v1/* API is Vercel-OIDC gated and unusable from here.

_SEARCH_PREFIX = "https://skills.sh/api/search"
assert _SEARCH_PREFIX.startswith("https://"), "search URL prefix must be https"

_SEARCH_CAP = 200_000       # a result list, not a document
_SEARCH_TIMEOUT = 8
_SEARCH_LIMIT_MAX = 50
_SEARCH_Q_MAX = 100
_SEARCH_Q_MIN = 2           # shorter than this is refused without a network call

# skills.sh page URLs are built here from the returned id, never taken from
# the response body — the same "no URL from anyone else" rule the rest of
# this module follows.
_SKILL_PAGE_PREFIX = "https://www.skills.sh/"

# The ids of the most recent search's results. server.py's /api/skill-audit
# consults this to decide whether an id it cannot find in the local index is
# nonetheless one this server itself just handed the client — see that route
# for why that is the whole authorization story for auditing a remote hit.
#
# The server is threaded, so this is shared across request threads. It is
# rebound whole (never mutated in place) so a reader always sees a complete,
# self-consistent set. The worst a race can do is refuse an audit for an id a
# newer search has just displaced, and the user clicks again.
_last_search_ids = frozenset()


def last_search_ids():
    """The id set from the most recent successful search_skills() call."""
    return _last_search_ids


def _trim_search_records(skills):
    """The name/source/installs subset the UI renders, sanitized with the
    same helpers catalog.py applies to on-disk marketplace content — this
    response is third-party and exactly as attacker-controlled. A record
    missing any of id/name/source is dropped rather than half-rendered.

    Note what is NOT here: the endpoint returns no description, and no URL
    from the response is ever used — the skill page link is assembled from
    the sanitized id and re-checked with catalog.safe_url().
    """
    out = []
    for s in skills:
        if not isinstance(s, dict):
            continue
        sid = catalog.clean_str(s.get("id"), 220)
        name = catalog.clean_str(s.get("name"), 80)
        source = catalog.clean_str(s.get("source"), 120)
        if not sid or not name or not source:
            continue
        installs = s.get("installs")
        installs = installs if isinstance(installs, int) and not isinstance(installs, bool) else None
        out.append({
            "id": sid,
            "name": name,
            "source": source,
            "installs": installs,
            "url": catalog.safe_url(_SKILL_PAGE_PREFIX + quote(sid, safe="/")),
        })
    return out


def search_skills(q, limit=None):
    """Search skills.sh for `q`. Returns {"ok": True, "query", "skills": [...]}.

    Refuses (raises ValueError, matching fetch_source()'s error-signal
    convention) when:
      - skills_sh.query_ok consent is not recorded. Note `query_ok`, NOT
        `ok`: consenting to a per-skill audit is not consenting to send what
        you type. Checked first, before any I/O.
      - `q` is under _SEARCH_Q_MIN characters after cleaning — refused
        locally, no request made.
      - the assembled URL does not start with _SEARCH_PREFIX. urlencode()
        already escapes everything, so this is belt-and-braces against an
        encoding bug, the same re-check audit_skill() does.
      - the response is oversized, unreadable JSON, or not a JSON object.

    `q` is a string, never a URL; `limit` is clamped into 1.._SEARCH_LIMIT_MAX.
    """
    consent = consent_get().get("skills_sh") or {}
    if not consent.get("query_ok"):
        raise ValueError("skills_sh: search is off — turn on skills.sh search first")

    q = (catalog.clean_str(q, _SEARCH_Q_MAX) or "").strip()
    if len(q) < _SEARCH_Q_MIN:
        raise ValueError(f"skills_sh: type at least {_SEARCH_Q_MIN} characters to search")

    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = _SEARCH_LIMIT_MAX
    n = max(1, min(_SEARCH_LIMIT_MAX, n))

    url = f"{_SEARCH_PREFIX}?{urlencode({'q': q, 'limit': n})}"
    if not url.startswith(_SEARCH_PREFIX):
        raise ValueError("skills_sh: assembled search URL escaped its prefix — refusing")

    req = urllib.request.Request(url, headers={"user-agent": "claude-ui"})
    try:
        with _opener.open(req, timeout=_SEARCH_TIMEOUT) as resp:
            # cap+1, never content-length — the server's claim is not a fact.
            data = resp.read(_SEARCH_CAP + 1)
    except urllib.error.URLError as e:
        raise ValueError(f"skills_sh: search failed: {e.reason}") from None
    except OSError as e:
        raise ValueError(f"skills_sh: search failed: {e}") from None

    if len(data) > _SEARCH_CAP:
        raise ValueError(f"skills_sh: response exceeded {_SEARCH_CAP} bytes — refusing")

    try:
        doc = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"skills_sh: unreadable JSON: {e}") from None

    if not isinstance(doc, dict):
        raise ValueError("skills_sh: unexpected response shape (not a JSON object)")

    skills = doc.get("skills")
    records = _trim_search_records(skills[:_SEARCH_LIMIT_MAX]) if isinstance(skills, list) else []

    global _last_search_ids
    _last_search_ids = frozenset(r["id"] for r in records)
    return {"ok": True, "query": q, "skills": records}
