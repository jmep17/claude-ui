"""Guarded fetch of Anthropic's two public plugin catalogs — the only network
calls this app makes on the user's behalf against non-Anthropic-docs URLs.

This module knows nothing about what a plugin or skill is. It is a generic
fetch-a-pre-registered-document utility with a hardcoded, frozen allowlist of
sources (_SOURCES below). There is deliberately no function anywhere in this
module that accepts a URL from a caller — every fetchable thing is a key into
that frozen dict, never a string built at runtime. That is the hard security
invariant this module exists to enforce: a bug anywhere else in the codebase
that calls fetch_source() with attacker-influenced input can, at worst, name
an unknown key (refused before any network I/O) — it can never steer the
request to a different host.

Consent is enforced here too, not just by server.py choosing to gate its
route: fetch_source() itself refuses to run when no consent is on record for
that source. A caller bug elsewhere cannot fire an unconsented fetch.

Everything fetched is a public, static JSON document (a marketplace.json) —
no query, no user data, ever leaves this machine. `ok` consent means exactly
"you may download this document from this URL", nothing more.
"""

from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
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
#                 "community": {"ok": false}}}
# skills_sh is Phase 4's key; this phase never writes it, and passes an
# existing value through untouched if the file already has one.

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
    see audit_skill()'s docstring for what that flag means and why it is
    not checked by anything in this phase)."""
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


def consent_set_skills_sh(ok, at=None):
    """Record consent (or its withdrawal) for the skills.sh audit lookup.

    Kept separate from consent_set() rather than folded into DISCOVER_SOURCES:
    that dict/function pair's shape is {"ok", "at"} for a static-document
    fetch; skills_sh's shape is {"ok", "query_ok", "at"} (see the module
    docstring's consent-shape comment near _SOURCES). Only `ok` is settable
    here — `query_ok` would gate the skills.sh *search* endpoint, which is
    out of scope forever per the plan, so nothing in this codebase ever
    writes it; it is preserved as-is (defaulting to False) if some future
    caller ever sets it directly in the config file by hand.
    """
    cfg = read_cfg()
    discover = cfg.get("discover")
    discover = dict(discover) if isinstance(discover, dict) else {}
    existing = discover.get("skills_sh")
    existing = existing if isinstance(existing, dict) else {}
    discover["skills_sh"] = {"ok": bool(ok), "query_ok": bool(existing.get("query_ok")),
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
# search. skills.sh's *search* endpoint is undocumented and privacy-leaking
# (it would send whatever the user is typing to a third party) and is out of
# scope forever; nothing in this module builds a URL for it.
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
_AUDIT_MAX_DEPTH = 3       # shallow nesting only — see _sanitize_audit_value
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
        function checks. skills_sh.query_ok is a *stricter* flag reserved
        for skills.sh's free-text search endpoint (which this codebase never
        calls): it guards "sending what I type to this company" as the user
        types it. This function's `skill` argument is never free text — it
        is the name of an entry the caller already resolved out of the LOCAL
        catalog index (see server.py's /api/skill-audit, which resolves an
        id server-side rather than trusting a raw name from the request
        body) — so query_ok does not apply here.
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
