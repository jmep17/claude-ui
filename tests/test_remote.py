"""remote.py: guarded fetch of the two Anthropic plugin catalogs.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from
the repo root, or just `python3 tests/test_remote.py`.

remote._opener.open is replaced throughout, mirroring how test_registry.py
replaces subprocess.run: nothing here should ever reach a real network.
fetch_source() calls a module-private opener (not urllib.request.urlopen)
precisely so this module's redirect guarantees never leak into schema.py's or
settings.py's own urlopen() calls — see remote.py's _SameHostHTTPSRedirect
comment — so that private opener's .open() is what needs patching here, not
the urllib.request module function. What needs pinning is that only a URL out
of remote._SOURCES is ever opened, that an unknown/unconsented source is
refused before any network I/O, that an oversized or malformed response never
touches the disk cache, and that the redirect handler refuses a cross-host or
non-https target.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import catalog, core, items, plugins, remote, settings  # noqa: E402


_CFG_USERS = (core, items, settings, plugins, catalog)


def valid_doc(n=3):
    return {"plugins": [
        {"name": f"pkg{i}", "description": f"plugin {i}", "author": "acme",
         "category": "tools", "tags": ["a", "b"], "installs": 10,
         "homepage": "https://example.com/pkg", "skills": [{"name": "helper"}],
         "source": {"source": "github", "url": "https://github.com/acme/pkg",
                    "ref": "main"}}
        for i in range(n)
    ]}


class _FakeResponse:
    """A minimal context-manager stand-in for urlopen()'s return value."""

    def __init__(self, data, headers=None):
        self._buf = io.BytesIO(data)
        self.headers = headers or {}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = __import__("pathlib").Path(self.tmpdir.name)
        self._saved = [(m, m.config_dir) for m in _CFG_USERS]
        for m, _ in self._saved:
            m.config_dir = lambda t=self.tmp: t
        catalog._CACHE = None

        self._cfg_file = core.CONFIG_FILE
        core.CONFIG_FILE = self.tmp / ".claude-ui.json"

        self._opener_open = remote._opener.open
        self.calls = []
        self.response = _FakeResponse(json.dumps(valid_doc()).encode())

        def fake_open(req, timeout=None):
            self.calls.append({"url": req.full_url, "timeout": timeout,
                               "headers": dict(req.header_items())})
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

        remote._opener.open = fake_open
        self._last_ids = remote._last_search_ids
        remote._last_search_ids = frozenset()

    def tearDown(self):
        remote._opener.open = self._opener_open
        remote._last_search_ids = self._last_ids
        core.CONFIG_FILE = self._cfg_file
        for m, fn in self._saved:
            m.config_dir = fn
        catalog._CACHE = None
        self.tmpdir.cleanup()

    def consent(self, source, ok=True):
        remote.consent_set(source, ok, at="2026-01-01T00:00:00+00:00")

    def cache_path(self, source):
        return core.discover_cache_path(source)


class TestSourcesFrozen(unittest.TestCase):
    def test_urls_are_https(self):
        for name, cfg in remote._SOURCES.items():
            self.assertTrue(cfg["url"].startswith("https://"), name)

    def test_no_url_parameter_anywhere(self):
        """The hard invariant: fetch_source's only input is a dict key."""
        import inspect
        sig = inspect.signature(remote.fetch_source)
        self.assertEqual(list(sig.parameters), ["name"])


class TestFetchSource(Base):
    def test_unknown_source_raises_before_any_open(self):
        with self.assertRaises(ValueError):
            remote.fetch_source("nope")
        self.assertEqual(self.calls, [])

    def test_extra_url_kwarg_has_no_effect(self):
        """There is no code path that accepts a URL — calling with anything
        other than a known key raises before urlopen, whatever else is passed."""
        with self.assertRaises(TypeError):
            remote.fetch_source("official", url="https://evil.example.com")
        self.assertEqual(self.calls, [])

    def test_no_consent_refuses_before_any_open(self):
        with self.assertRaises(ValueError) as ctx:
            remote.fetch_source("official")
        self.assertIn("consent", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_consented_fetch_opens_exact_registered_url(self):
        self.consent("official")
        remote.fetch_source("official")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["url"], remote._SOURCES["official"]["url"])

    def test_community_source_opens_its_own_url(self):
        self.consent("community")
        remote.fetch_source("community")
        self.assertEqual(self.calls[0]["url"], remote._SOURCES["community"]["url"])

    def test_every_open_call_has_a_timeout(self):
        self.consent("official")
        remote.fetch_source("official")
        self.assertEqual(self.calls[0]["timeout"], remote._SOURCES["official"]["timeout"])

    def test_success_writes_trimmed_cache_file(self):
        self.consent("official")
        result = remote.fetch_source("official")
        self.assertTrue(result["ok"])
        path = self.cache_path("official")
        self.assertTrue(path.is_file())
        doc = json.loads(path.read_text())
        self.assertEqual(len(doc["entries"]), 3)
        self.assertEqual(doc["entries"][0]["name"], "pkg0")
        # the raw fetched document is never kept — only the trimmed subset
        self.assertNotIn("plugins", doc)

    def test_oversized_response_refused_and_no_cache_written(self):
        self.consent("official")
        cap = remote._SOURCES["official"]["cap"]
        big_doc = valid_doc(1)
        # pad well past the cap with a long description
        big_doc["plugins"][0]["description"] = "x" * (cap + 10)
        self.response = _FakeResponse(json.dumps(big_doc).encode())
        with self.assertRaises(ValueError):
            remote.fetch_source("official")
        self.assertFalse(self.cache_path("official").exists())

    def test_oversized_response_does_not_blank_a_good_previous_cache(self):
        self.consent("official")
        remote.fetch_source("official")  # good cache written
        path = self.cache_path("official")
        before = path.read_text()

        cap = remote._SOURCES["official"]["cap"]
        big_doc = valid_doc(1)
        big_doc["plugins"][0]["description"] = "x" * (cap + 10)
        self.response = _FakeResponse(json.dumps(big_doc).encode())
        with self.assertRaises(ValueError):
            remote.fetch_source("official")
        self.assertEqual(path.read_text(), before)

    def test_unreadable_json_refused_and_no_cache_written(self):
        self.consent("official")
        self.response = _FakeResponse(b"{not json")
        with self.assertRaises(ValueError):
            remote.fetch_source("official")
        self.assertFalse(self.cache_path("official").exists())

    def test_failed_validation_refused_and_no_cache_written(self):
        self.consent("official")
        self.response = _FakeResponse(json.dumps({"plugins": []}).encode())
        with self.assertRaises(ValueError):
            remote.fetch_source("official")
        self.assertFalse(self.cache_path("official").exists())

    def test_failed_validation_leaves_previous_good_cache_untouched(self):
        self.consent("official")
        remote.fetch_source("official")
        path = self.cache_path("official")
        before = path.read_text()

        self.response = _FakeResponse(json.dumps({"plugins": []}).encode())
        with self.assertRaises(ValueError):
            remote.fetch_source("official")
        self.assertEqual(path.read_text(), before)

    def test_network_error_raises_valueerror(self):
        self.consent("official")
        self.response = urllib.error.URLError("offline")
        with self.assertRaises(ValueError):
            remote.fetch_source("official")

    def test_traversal_attempt_in_entry_name_stays_under_discover_dir(self):
        """The write path is only ever <config_dir>/claude-ui-discover/<source
        key>.json — never built from document content — so an adversarial
        entry name in the upstream doc cannot influence it."""
        self.consent("official")
        doc = valid_doc(1)
        doc["plugins"][0]["name"] = "../../../etc/evil"
        self.response = _FakeResponse(json.dumps(doc).encode())
        remote.fetch_source("official")
        path = self.cache_path("official").resolve()
        discover_dir = (self.tmp / "claude-ui-discover").resolve()
        self.assertEqual(path.parent, discover_dir)
        self.assertEqual(path.name, "official.json")


class TestRedirectHandler(unittest.TestCase):
    def _req(self, url="https://raw.githubusercontent.com/x/y.json"):
        return urllib.request.Request(url)

    def test_cross_host_redirect_refused(self):
        handler = remote._SameHostHTTPSRedirect()
        with self.assertRaises(ValueError):
            handler.redirect_request(
                self._req(), None, 302, "Found", {},
                "https://evil.example.com/marketplace.json")

    def test_downgrade_to_http_refused(self):
        handler = remote._SameHostHTTPSRedirect()
        with self.assertRaises(ValueError):
            handler.redirect_request(
                self._req(), None, 302, "Found", {},
                "http://raw.githubusercontent.com/x/y.json")

    def test_same_host_https_redirect_allowed(self):
        handler = remote._SameHostHTTPSRedirect()
        new = handler.redirect_request(
            self._req(), None, 302, "Found", {},
            "https://raw.githubusercontent.com/x/z.json")
        self.assertIsInstance(new, urllib.request.Request)


class TestConsent(Base):
    def test_defaults_to_false_when_nothing_recorded(self):
        c = remote.consent_get()
        self.assertEqual(c["official"], {"ok": False, "at": None})
        self.assertEqual(c["community"], {"ok": False, "at": None})

    def test_round_trip(self):
        remote.consent_set("official", True, at="2026-02-01T00:00:00+00:00")
        c = remote.consent_get()
        self.assertEqual(c["official"], {"ok": True, "at": "2026-02-01T00:00:00+00:00"})
        self.assertEqual(c["community"], {"ok": False, "at": None})

    def test_withdrawing_consent(self):
        remote.consent_set("official", True, at="2026-02-01T00:00:00+00:00")
        remote.consent_set("official", False, at="2026-02-02T00:00:00+00:00")
        c = remote.consent_get()
        self.assertFalse(c["official"]["ok"])

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            remote.consent_set("skills_sh", True)

    def test_injectable_timestamp_used_when_given(self):
        remote.consent_set("community", True, at="2020-01-01T00:00:00+00:00")
        self.assertEqual(remote.consent_get()["community"]["at"],
                         "2020-01-01T00:00:00+00:00")

    def test_bare_now_not_used_when_at_given(self):
        real_now = remote._now_iso
        remote._now_iso = lambda: (_ for _ in ()).throw(AssertionError("should not be called"))
        try:
            remote.consent_set("official", True, at="2026-03-01T00:00:00+00:00")
        finally:
            remote._now_iso = real_now


class TestFetchRefusedWithoutConsent(Base):
    def test_fetch_source_itself_refuses_no_server_route_involved(self):
        # explicit: not via server.py, calling remote.fetch_source directly
        with self.assertRaises(ValueError):
            remote.fetch_source("community")
        self.assertEqual(self.calls, [])


class TestCatalogPicksUpDiscoverCache(Base):
    def setUp(self):
        super().setUp()
        self.plugin = self.tmp / "plugins" / "marketplaces" / "mkt" / "plugins" / "demo"
        (self.plugin / ".claude-plugin").mkdir(parents=True)
        (self.plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "demo", "description": "a demo plugin"}))
        (self.tmp / "plugins").mkdir(exist_ok=True)
        (self.tmp / "plugins" / "known_marketplaces.json").write_text(json.dumps({"mkt": {}}))

    def test_index_has_no_official_entries_before_fetch(self):
        catalog._CACHE = None
        idx = catalog.build_index()
        self.assertFalse([e for e in idx["entries"] if e["group"] == "official"])

    def test_index_picks_up_entries_after_fetch_without_restart(self):
        self.consent("official")
        remote.fetch_source("official")
        catalog._CACHE = None  # simulate a fresh in-process read, like a new request
        idx = catalog.build_index()
        official = [e for e in idx["entries"]
                   if e["group"] == "official" and e["kind"] == "plugin"]
        self.assertEqual(len(official), 3)
        self.assertEqual(official[0]["installable"], True)
        self.assertIsNotNone(idx["discover_fetched_at"]["official"])

    def test_signature_invalidates_stale_in_process_cache(self):
        """The whole point of extending _signature(): no server restart needed."""
        idx1 = catalog.build_index()
        self.assertFalse([e for e in idx1["entries"] if e["group"] == "official"])
        self.consent("official")
        remote.fetch_source("official")
        idx2 = catalog.build_index()  # no manual catalog._CACHE = None this time
        self.assertTrue([e for e in idx2["entries"] if e["group"] == "official"])

    def test_catalog_state_reports_per_source_freshness(self):
        self.consent("community")
        remote.fetch_source("community")
        catalog._CACHE = None
        state = catalog.catalog_state()
        self.assertIsNotNone(state["discover_fetched_at"]["community"])
        self.assertIsNone(state["discover_fetched_at"]["official"])
        self.assertEqual(state["counts"].get("community"), 6)  # 3 plugins + 3 skills


class TestAuditSkill(Base):
    """audit_skill(): skills.sh's per-skill audit lookup. Separate consent
    flag from official/community, no URL parameter, response never cached to
    disk (unlike fetch_source())."""

    def audit_response(self, doc):
        self.response = _FakeResponse(json.dumps(doc).encode())

    def test_refuses_without_consent_zero_network_calls(self):
        with self.assertRaises(ValueError) as ctx:
            remote.audit_skill("acme", "helper")
        self.assertIn("consent", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_official_or_community_consent_does_not_satisfy_skills_sh(self):
        self.consent("official")
        self.consent("community")
        with self.assertRaises(ValueError):
            remote.audit_skill("acme", "helper")
        self.assertEqual(self.calls, [])

    def consent_skills_sh(self, ok=True):
        remote.consent_set_skills_sh(ok, at="2026-01-01T00:00:00+00:00")

    def test_consented_lookup_opens_the_expected_url(self):
        self.consent_skills_sh()
        self.audit_response({"risk": "low"})
        remote.audit_skill("acme", "helper")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["url"],
                         "https://skills.sh/api/v1/skills/audit/acme/helper")

    def test_every_open_call_has_a_timeout(self):
        self.consent_skills_sh()
        self.audit_response({"risk": "low"})
        remote.audit_skill("acme", "helper")
        self.assertEqual(self.calls[0]["timeout"], remote._AUDIT_TIMEOUT)

    def test_url_always_starts_with_prefix_for_adversarial_segments(self):
        self.consent_skills_sh()
        adversarial = [
            ("../../etc/passwd", "helper"),
            ("acme", "../../../secret"),
            ("a/b/c", "d/e/f"),
            ("..", ".."),
            ("%2e%2e", "%2e%2e"),
            ("has space", "also space"),
            ("ünïcödé", "スキル"),
        ]
        for source, skill in adversarial:
            self.calls.clear()
            self.audit_response({"risk": "low"})  # fresh response each call — BytesIO is single-read
            remote.audit_skill(source, skill)
            self.assertEqual(len(self.calls), 1)
            self.assertTrue(self.calls[0]["url"].startswith(remote._AUDIT_PREFIX),
                            self.calls[0]["url"])

    def test_oversized_response_refused(self):
        self.consent_skills_sh()
        self.response = _FakeResponse(b'{"risk": "' + b"x" * (remote._AUDIT_CAP + 10) + b'"}')
        with self.assertRaises(ValueError):
            remote.audit_skill("acme", "helper")

    def test_unreadable_json_refused(self):
        self.consent_skills_sh()
        self.response = _FakeResponse(b"{not json")
        with self.assertRaises(ValueError):
            remote.audit_skill("acme", "helper")

    def test_non_object_top_level_refused(self):
        self.consent_skills_sh()
        self.audit_response(["not", "an", "object"])
        with self.assertRaises(ValueError):
            remote.audit_skill("acme", "helper")

    def test_network_error_raises_valueerror(self):
        self.consent_skills_sh()
        self.response = urllib.error.URLError("offline")
        with self.assertRaises(ValueError):
            remote.audit_skill("acme", "helper")

    def test_result_is_not_written_to_disk(self):
        """Deliberately not cached — a security verdict should reflect
        current state each time, not go stale silently."""
        self.consent_skills_sh()
        self.audit_response({"risk": "low"})
        remote.audit_skill("acme", "helper")
        discover_dir = self.tmp / "claude-ui-discover"
        if discover_dir.is_dir():
            self.assertEqual(list(discover_dir.iterdir()), [])

    def test_sanitizes_and_returns_plain_fields(self):
        self.consent_skills_sh()
        self.audit_response({"risk": "low", "score": 42, "flagged": False,
                             "providers": ["socket", "snyk"]})
        result = remote.audit_skill("acme", "helper")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["risk"], "low")
        self.assertEqual(result["data"]["score"], 42)
        self.assertEqual(result["data"]["flagged"], False)
        self.assertEqual(result["data"]["providers"], ["socket", "snyk"])

    def test_oversized_string_field_truncated_not_crashed(self):
        self.consent_skills_sh()
        self.audit_response({"summary": "x" * (remote._AUDIT_STR_LIMIT + 500)})
        result = remote.audit_skill("acme", "helper")
        self.assertEqual(len(result["data"]["summary"]), remote._AUDIT_STR_LIMIT)

    def test_deeply_nested_response_flattened_not_crashed(self):
        self.consent_skills_sh()
        nested = {"a": {"b": {"c": {"d": {"e": "too deep"}}}}}
        self.audit_response(nested)
        result = remote.audit_skill("acme", "helper")
        # Should not raise, and should not carry the value past _AUDIT_MAX_DEPTH.
        self.assertIsInstance(result["data"], dict)

    def test_oversized_list_field_capped_not_crashed(self):
        self.consent_skills_sh()
        self.audit_response({"tags": [f"t{i}" for i in range(remote._AUDIT_LIST_LIMIT + 50)]})
        result = remote.audit_skill("acme", "helper")
        self.assertLessEqual(len(result["data"]["tags"]), remote._AUDIT_LIST_LIMIT)

    def test_malformed_field_types_dropped_not_forwarded(self):
        self.consent_skills_sh()
        # None is not JSON-serializable-in as anything but null; simulate an
        # unrecognized/exotic value by post-loading a dict with a null field.
        self.audit_response({"risk": "low", "weird": None})
        result = remote.audit_skill("acme", "helper")
        self.assertNotIn("weird", result["data"])


    def test_categories_at_depth_three_survive_sanitizing(self):
        """The real response nests doc -> audits[] -> {provider} ->
        categories[]; at _AUDIT_MAX_DEPTH = 3 that array was silently
        dropped, which is the bug the limit of 4 fixes."""
        self.consent_skills_sh()
        self.audit_response({"audits": [
            {"provider": "socket", "risk": "low",
             "categories": ["network", "filesystem"]}]})
        result = remote.audit_skill("acme", "helper")
        self.assertEqual(result["data"]["audits"][0]["categories"],
                         ["network", "filesystem"])


class TestSearchSkills(Base):
    """search_skills(): the one call that sends what the user typed to a
    third party. Gated on the stronger query_ok flag, never disk-cached, and
    the URL is always assembled here from a fixed prefix."""

    def search_response(self, doc):
        self.response = _FakeResponse(json.dumps(doc).encode())

    def hit(self, i=0):
        return {"id": f"acme/pkg/skill{i}", "skillId": f"skill{i}",
                "name": f"skill{i}", "installs": 100 + i, "source": "acme/pkg"}

    def allow_search(self):
        remote.consent_set_skills_sh(True, True, at="2026-01-01T00:00:00+00:00")

    def test_refuses_without_consent_zero_network_calls(self):
        with self.assertRaises(ValueError):
            remote.search_skills("react")
        self.assertEqual(self.calls, [])

    def test_audit_consent_alone_does_not_enable_search(self):
        """ok is the weaker flag — it buys a per-skill audit, not the right
        to send what the user types."""
        remote.consent_set_skills_sh(True, at="2026-01-01T00:00:00+00:00")
        self.assertFalse(remote.consent_get()["skills_sh"]["query_ok"])
        with self.assertRaises(ValueError):
            remote.search_skills("react")
        self.assertEqual(self.calls, [])

    def test_official_and_community_consent_do_not_enable_search(self):
        self.consent("official")
        self.consent("community")
        with self.assertRaises(ValueError):
            remote.search_skills("react")
        self.assertEqual(self.calls, [])

    def test_short_query_refused_before_any_open(self):
        self.allow_search()
        for q in ("", " ", "a", "  a  "):
            self.calls.clear()
            with self.assertRaises(ValueError):
                remote.search_skills(q)
            self.assertEqual(self.calls, [])

    def test_consented_search_opens_the_expected_url(self):
        self.allow_search()
        self.search_response({"skills": [self.hit()]})
        remote.search_skills("react", 3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["url"],
                         "https://skills.sh/api/search?q=react&limit=3")

    def test_every_open_call_has_a_timeout(self):
        self.allow_search()
        self.search_response({"skills": []})
        remote.search_skills("react")
        self.assertEqual(self.calls[0]["timeout"], remote._SEARCH_TIMEOUT)

    def test_limit_clamped_into_range(self):
        self.allow_search()
        for given, expected in ((0, 1), (-5, 1), (9999, remote._SEARCH_LIMIT_MAX),
                                (None, remote._SEARCH_LIMIT_MAX),
                                ("nonsense", remote._SEARCH_LIMIT_MAX)):
            self.calls.clear()
            self.search_response({"skills": []})
            remote.search_skills("react", given)
            self.assertIn(f"limit={expected}", self.calls[0]["url"])

    def test_url_always_starts_with_prefix_for_adversarial_queries(self):
        self.allow_search()
        adversarial = [
            "../../etc/passwd",
            "https://evil.example.com/",
            "%2f%2e%2e%2f",
            "react&limit=9999",
            "react#fragment",
            "a b\tc",
            "スキル",
            "?q=x",
            "//evil.example.com",
        ]
        for q in adversarial:
            self.calls.clear()
            self.search_response({"skills": []})
            remote.search_skills(q)
            self.assertEqual(len(self.calls), 1)
            url = self.calls[0]["url"]
            self.assertTrue(url.startswith(remote._SEARCH_PREFIX + "?"), url)
            # the whole query lives in the query string — nothing the user
            # typed can add a path segment or a second host
            self.assertEqual(urllib.parse.urlsplit(url).netloc, "skills.sh")
            self.assertEqual(urllib.parse.urlsplit(url).path, "/api/search")

    def test_query_truncated_to_max(self):
        self.allow_search()
        self.search_response({"skills": []})
        remote.search_skills("x" * (remote._SEARCH_Q_MAX + 200))
        q = urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.calls[0]["url"]).query)["q"][0]
        self.assertEqual(len(q), remote._SEARCH_Q_MAX)

    def test_oversized_response_refused(self):
        self.allow_search()
        self.response = _FakeResponse(
            b'{"skills": [], "pad": "' + b"x" * (remote._SEARCH_CAP + 10) + b'"}')
        with self.assertRaises(ValueError):
            remote.search_skills("react")

    def test_unreadable_json_refused(self):
        self.allow_search()
        self.response = _FakeResponse(b"{not json")
        with self.assertRaises(ValueError):
            remote.search_skills("react")

    def test_non_object_top_level_refused(self):
        self.allow_search()
        self.search_response(["not", "an", "object"])
        with self.assertRaises(ValueError):
            remote.search_skills("react")

    def test_network_error_raises_valueerror(self):
        self.allow_search()
        self.response = urllib.error.URLError("offline")
        with self.assertRaises(ValueError):
            remote.search_skills("react")

    def test_off_host_redirect_raises(self):
        """The shared opener's redirect handler is the boundary — an off-host
        target raises rather than being followed."""
        handler = remote._SameHostHTTPSRedirect()
        req = urllib.request.Request(remote._SEARCH_PREFIX + "?q=react&limit=5")
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {},
                                     "https://evil.example.com/api/search")
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {},
                                     "http://skills.sh/api/search")

    def test_records_sanitized_and_page_url_built_here(self):
        self.allow_search()
        self.search_response({"skills": [self.hit(0)]})
        r = remote.search_skills("react")
        self.assertTrue(r["ok"])
        self.assertEqual(r["query"], "react")
        rec = r["skills"][0]
        self.assertEqual(rec["name"], "skill0")
        self.assertEqual(rec["source"], "acme/pkg")
        self.assertEqual(rec["installs"], 100)
        self.assertEqual(rec["url"], "https://www.skills.sh/acme/pkg/skill0")

    def test_url_from_the_response_body_is_never_used(self):
        self.allow_search()
        bad = dict(self.hit(0), url="javascript:alert(1)",
                   homepage="https://evil.example.com")
        self.search_response({"skills": [bad]})
        rec = remote.search_skills("react")["skills"][0]
        self.assertEqual(rec["url"], "https://www.skills.sh/acme/pkg/skill0")
        self.assertNotIn("homepage", rec)

    def test_malformed_records_dropped(self):
        self.allow_search()
        self.search_response({"skills": [
            "a string", 42, None, [],
            {"id": "a/b/c"},                                  # no name/source
            {"name": "n", "source": "s"},                     # no id
            {"id": "a/b/c", "name": "n"},                     # no source
            {"id": "a/b/c", "name": "n", "source": "s", "installs": True},
            self.hit(1),
        ]})
        recs = remote.search_skills("react")["skills"]
        self.assertEqual(len(recs), 2)
        self.assertIsNone(recs[0]["installs"])   # True is a bool, not a count
        self.assertEqual(recs[1]["name"], "skill1")

    def test_missing_or_wrong_typed_skills_array_yields_no_records(self):
        self.allow_search()
        for doc in ({}, {"skills": None}, {"skills": "nope"}, {"skills": {}}):
            self.search_response(doc)
            self.assertEqual(remote.search_skills("react")["skills"], [])

    def test_record_count_capped(self):
        self.allow_search()
        self.search_response(
            {"skills": [self.hit(i) for i in range(remote._SEARCH_LIMIT_MAX + 40)]})
        recs = remote.search_skills("react")["skills"]
        self.assertLessEqual(len(recs), remote._SEARCH_LIMIT_MAX)

    def test_results_are_not_written_to_disk(self):
        """A cache of remote searches would be a record of what the user
        typed sitting in their config directory."""
        self.allow_search()
        self.search_response({"skills": [self.hit()]})
        remote.search_skills("something private")
        discover_dir = self.tmp / "claude-ui-discover"
        if discover_dir.is_dir():
            self.assertEqual(list(discover_dir.iterdir()), [])
        blob = json.dumps(json.loads(core.CONFIG_FILE.read_text()))
        self.assertNotIn("something private", blob)

    def test_last_search_ids_tracks_the_most_recent_search(self):
        self.allow_search()
        self.search_response({"skills": [self.hit(0), self.hit(1)]})
        remote.search_skills("react")
        self.assertEqual(remote.last_search_ids(),
                         {"acme/pkg/skill0", "acme/pkg/skill1"})
        self.search_response({"skills": [self.hit(2)]})
        remote.search_skills("vue")
        # replaced wholesale, not accumulated
        self.assertEqual(remote.last_search_ids(), {"acme/pkg/skill2"})

    def test_last_search_ids_is_immutable(self):
        self.allow_search()
        self.search_response({"skills": [self.hit(0)]})
        remote.search_skills("react")
        self.assertIsInstance(remote.last_search_ids(), frozenset)


class TestSkillsShConsent(Base):
    def test_defaults_to_false_with_query_ok_flag(self):
        c = remote.consent_get()
        self.assertEqual(c["skills_sh"], {"ok": False, "query_ok": False, "at": None})

    def test_query_ok_round_trip(self):
        remote.consent_set_skills_sh(True, True, at="2026-02-01T00:00:00+00:00")
        self.assertEqual(remote.consent_get()["skills_sh"],
                         {"ok": True, "query_ok": True, "at": "2026-02-01T00:00:00+00:00"})

    def test_query_ok_none_preserves_what_is_on_disk(self):
        """The audit-consent flow sends no query_ok and must not silently
        revoke a search consent granted earlier."""
        remote.consent_set_skills_sh(True, True, at="2026-02-01T00:00:00+00:00")
        remote.consent_set_skills_sh(True, at="2026-02-02T00:00:00+00:00")
        self.assertTrue(remote.consent_get()["skills_sh"]["query_ok"])

    def test_withdrawing_ok_also_clears_query_ok(self):
        """Withdrawing the weaker consent must not leave the stronger live."""
        remote.consent_set_skills_sh(True, True, at="2026-02-01T00:00:00+00:00")
        remote.consent_set_skills_sh(False, at="2026-02-02T00:00:00+00:00")
        c = remote.consent_get()["skills_sh"]
        self.assertFalse(c["ok"])
        self.assertFalse(c["query_ok"])

    def test_query_ok_cannot_be_granted_without_ok(self):
        remote.consent_set_skills_sh(False, True, at="2026-02-01T00:00:00+00:00")
        self.assertFalse(remote.consent_get()["skills_sh"]["query_ok"])

    def test_withdrawing_query_ok_leaves_audit_consent(self):
        remote.consent_set_skills_sh(True, True, at="2026-02-01T00:00:00+00:00")
        remote.consent_set_skills_sh(True, False, at="2026-02-02T00:00:00+00:00")
        c = remote.consent_get()["skills_sh"]
        self.assertTrue(c["ok"])
        self.assertFalse(c["query_ok"])

    def test_round_trip(self):
        remote.consent_set_skills_sh(True, at="2026-02-01T00:00:00+00:00")
        c = remote.consent_get()
        self.assertEqual(c["skills_sh"],
                         {"ok": True, "query_ok": False, "at": "2026-02-01T00:00:00+00:00"})

    def test_does_not_disturb_official_or_community(self):
        remote.consent_set("official", True, at="2026-01-01T00:00:00+00:00")
        remote.consent_set_skills_sh(True, at="2026-01-02T00:00:00+00:00")
        c = remote.consent_get()
        self.assertTrue(c["official"]["ok"])
        self.assertTrue(c["skills_sh"]["ok"])

    def test_consent_set_still_rejects_skills_sh_as_a_DISCOVER_SOURCES_key(self):
        """consent_set() (official/community's setter) must keep refusing
        skills_sh — it has a different shape and its own setter."""
        with self.assertRaises(ValueError):
            remote.consent_set("skills_sh", True)


if __name__ == "__main__":
    unittest.main()
