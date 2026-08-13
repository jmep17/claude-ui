"""catalog.py: the Discover search index over local corpora only.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_catalog.py`.

Same config_dir-patching pattern as test_editor.py: every module that did
`from .core import config_dir` holds its own binding, so redirecting the
config dir means rebinding all of them, catalog included.
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import catalog, core, items, plugins, settings  # noqa: E402


_CFG_USERS = (core, items, settings, plugins, catalog)


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return path

def md(description="", body="body"):
    return f"---\ndescription: {description}\n---\n{body}"

def cache_plugin(name="pkg", marketplace="extmkt", **extra):
    """One valid plugin-catalog-cache.json entry."""
    me = {"name": name, "description": "a cached plugin", **extra.pop("me", {})}
    entry = {"sha": None, "source_sha": "a" * 64,
             "marketplace_entry": me}
    entry.update(extra)
    return entry

def cache_doc(plugins_map, version=1, fetched_at="2026-01-01T00:00:00Z"):
    return {"version": version, "fetchedAt": fetched_at,
            "catalog": {"plugins": plugins_map}}

def bulk_cache_plugins(n, marketplace="extmkt", prefix="pkg"):
    return {f"{prefix}{i}@{marketplace}": cache_plugin(f"{prefix}{i}", marketplace)
            for i in range(n)}


class Base(unittest.TestCase):
    """A temp config dir with one installed plugin (demo@mkt) and one on-disk-
    only marketplace registration (extmkt, no plugin dirs) — mirrors
    test_plugins.py's Base."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self._saved = [(m, m.config_dir) for m in _CFG_USERS]
        for m, _ in self._saved:
            m.config_dir = lambda t=self.tmp: t
        catalog._CACHE = None

        self.plugin = self.tmp / "plugins" / "marketplaces" / "mkt" / "plugins" / "demo"
        write(self.plugin / ".claude-plugin" / "plugin.json",
              {"name": "demo", "description": "a demo plugin"})
        write(self.plugin / "skills" / "helper" / "SKILL.md",
              md("a helper skill"))

        # register a second marketplace with no plugin dirs on disk — the
        # "ondisk" catalogue-not-fetched and cache-dedupe cases need a
        # marketplace name that is registered but not backed by plugins_state()
        write(self.tmp / "plugins" / "known_marketplaces.json", {
            "mkt": {},
            "extmkt": {"installLocation": str(self.tmp / "ext")},
        })

    def tearDown(self):
        for m, fn in self._saved:
            m.config_dir = fn
        catalog._CACHE = None
        self.tmpdir.cleanup()

    def cache_path(self):
        return self.tmp / "plugins" / catalog.CACHE_FILE

    def write_settings(self, data):
        write(self.tmp / "settings.json", data)

    def index(self):
        catalog._CACHE = None
        return catalog.build_index()

    def entries(self):
        return self.index()["entries"]

    def by_id(self):
        return {e["id"]: e for e in self.entries()}


class TestCacheAbsentOrInvalid(Base):
    """The single most important behavior: a bad or missing cache file must
    never take the other three corpora down with it."""

    def test_version_2_gives_zero_cache_entries_others_present(self):
        write(self.cache_path(), cache_doc(bulk_cache_plugins(25), version=2))
        idx = self.index()
        ids = {e["id"] for e in idx["entries"]}
        self.assertFalse(any(i.startswith("pkg") for i in ids))
        self.assertIn("demo@mkt", ids)                     # installed
        self.assertIn("demo@mkt/skills/helper", ids)        # installed component
        self.assertIn("marketplace:mkt", ids)               # ondisk marketplace
        self.assertIsNotNone(idx["cache_reason"])

    def test_missing_cache_file_gives_zero_cache_entries_others_present(self):
        self.assertFalse(self.cache_path().exists())
        idx = self.index()
        ids = {e["id"] for e in idx["entries"]}
        self.assertIn("demo@mkt", ids)
        self.assertIn("marketplace:mkt", ids)
        self.assertIsNotNone(idx["cache_reason"])

    def test_below_20_entries_refused(self):
        write(self.cache_path(), cache_doc(bulk_cache_plugins(3)))
        idx = self.index()
        ids = {e["id"] for e in idx["entries"]}
        self.assertFalse(any(i.startswith("pkg") for i in ids))
        self.assertIn("fewer than 20", idx["cache_reason"])

    def test_bad_key_dropped_rest_still_loads(self):
        plugins_map = bulk_cache_plugins(25)
        plugins_map["not a valid key!!"] = cache_plugin("bad", "extmkt")
        write(self.cache_path(), cache_doc(plugins_map))
        idx = self.index()
        ids = {e["id"] for e in idx["entries"]}
        self.assertIn("pkg0@extmkt", ids)
        self.assertNotIn("not a valid key!!", ids)


class TestNormalization(Base):
    def test_dict_source_normalizes(self):
        plugins_map = bulk_cache_plugins(20)
        plugins_map["one@extmkt"] = cache_plugin("one", "extmkt", me={
            "source": {"source": "github", "url": "https://github.com/o/r",
                       "path": "plugins/one", "ref": "main", "sha": "b" * 64}})
        write(self.cache_path(), cache_doc(plugins_map))
        e = self.by_id()["one@extmkt"]
        self.assertEqual(e["source"]["url"], "https://github.com/o/r")
        self.assertEqual(e["source"]["sha"], "b" * 64)
        self.assertTrue(e["pinned"])

    def test_string_source_falls_back_to_source_sha_and_stays_pinned(self):
        plugins_map = bulk_cache_plugins(20)
        entry = cache_plugin("two", "extmkt")
        entry["marketplace_entry"]["source"] = "./plugins/agent-sdk-dev"
        entry["sha"] = None
        entry["source_sha"] = "c" * 64
        plugins_map["two@extmkt"] = entry
        write(self.cache_path(), cache_doc(plugins_map))
        e = self.by_id()["two@extmkt"]
        self.assertEqual(e["source"]["path"], "./plugins/agent-sdk-dev")
        self.assertEqual(e["source"]["sha"], "c" * 64)
        self.assertTrue(e["pinned"])

    def test_bidi_override_stripped_from_name(self):
        plugins_map = bulk_cache_plugins(20)
        entry = cache_plugin("evil", "extmkt")
        entry["marketplace_entry"]["name"] = "safe‮exe.txt"
        plugins_map["evil@extmkt"] = entry
        write(self.cache_path(), cache_doc(plugins_map))
        names = {e["name"] for e in self.entries() if e.get("marketplace") == "extmkt"}
        self.assertIn("safeexe.txt", names)
        self.assertNotIn("safe‮exe.txt", names)

    def test_long_description_truncated_to_300(self):
        plugins_map = bulk_cache_plugins(20)
        entry = cache_plugin("longdesc", "extmkt")
        entry["marketplace_entry"]["description"] = "x" * 4000
        plugins_map["longdesc@extmkt"] = entry
        write(self.cache_path(), cache_doc(plugins_map))
        e = self.by_id()["longdesc@extmkt"]
        self.assertEqual(len(e["description"]), 300)

    def test_homepage_url_sanitized(self):
        plugins_map = bulk_cache_plugins(20)
        bad = cache_plugin("bad", "extmkt", me={"homepage": "javascript:alert(1)"})
        good = cache_plugin("good", "extmkt", me={"homepage": "https://example.com"})
        plugins_map["bad@extmkt"] = bad
        plugins_map["good@extmkt"] = good
        write(self.cache_path(), cache_doc(plugins_map))
        by = self.by_id()
        self.assertIsNone(by["bad@extmkt"]["homepage"])
        self.assertEqual(by["good@extmkt"]["homepage"], "https://example.com")

    def test_data_url_homepage_is_none(self):
        plugins_map = bulk_cache_plugins(20)
        plugins_map["dataurl@extmkt"] = cache_plugin(
            "dataurl", "extmkt", me={"homepage": "data:text/html,x"})
        write(self.cache_path(), cache_doc(plugins_map))
        self.assertIsNone(self.by_id()["dataurl@extmkt"]["homepage"])

    def test_unregistered_marketplace_left_out_of_index(self):
        plugins_map = bulk_cache_plugins(20, marketplace="nowhere")
        write(self.cache_path(), cache_doc(plugins_map))
        ids = {e["id"] for e in self.entries()}
        self.assertFalse(any("nowhere" in i for i in ids))

    def test_dedupe_against_installed(self):
        plugins_map = bulk_cache_plugins(20)
        plugins_map["demo@mkt"] = cache_plugin("demo", "mkt")
        write(self.cache_path(), cache_doc(plugins_map))
        hits = [e for e in self.entries() if e["id"] == "demo@mkt"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["group"], "installed")


class TestScoring(Base):
    def setUp(self):
        super().setUp()
        write(self.plugin / "agents" / "search-helper.md", md("does searching"))

    def _search(self, q, **kw):
        return catalog.search(q, **kw)

    def test_exact_beats_prefix_beats_word_beats_sub_beats_fuzzy(self):
        entries = {
            "e": {"id": "e", "kind": "skill", "group": "yours", "name": "git"},
            "p": {"id": "p", "kind": "skill", "group": "yours", "name": "github-tools"},
            "w": {"id": "w", "kind": "skill", "group": "yours", "name": "my git tool"},
            "s": {"id": "s", "kind": "skill", "group": "yours", "name": "legitimate"},
            "f": {"id": "f", "kind": "skill", "group": "yours", "name": "g-i-t-x-y-z"},
        }
        for e in entries.values():
            e.setdefault("tags", [])
        id_to_name = {k: v["name"] for k, v in entries.items()}
        scores = {k: catalog._score_entry(["git"], v, id_to_name)[0]
                  for k, v in entries.items()}
        self.assertGreater(scores["e"], scores["p"])
        self.assertGreater(scores["p"], scores["w"])
        self.assertGreater(scores["w"], scores["s"])
        self.assertGreater(scores["s"], scores["f"])

    def test_short_query_does_not_match_description_subsequence(self):
        # "sle" is a subsequence of this description (s...earch across...e-L-s-E)
        # but never a literal substring — descriptions are substring-only, and
        # the whole point of the split is that fuzzy noise doesn't leak in.
        entry = {"id": "x", "kind": "skill", "group": "yours", "name": "unrelated",
                 "description": "used for A search across everything else", "tags": []}
        self.assertNotIn("sle", entry["description"].lower())
        self.assertIsNone(catalog._term_score("sle", entry, {}))
        # sanity: "search" (a real substring) does match
        r = catalog._term_score("search", entry, {})
        self.assertIsNotNone(r)

    def test_installs_do_not_beat_a_name_prefix_hit(self):
        popular_but_buried = {"id": "a", "kind": "plugin", "group": "community",
                              "name": "unrelated-tool", "tags": [],
                              "description": "a git wrapper for teams", "installs": 100000}
        prefix_no_installs = {"id": "b", "kind": "plugin", "group": "community",
                              "name": "git-helper", "tags": [], "installs": 0}
        id_to_name = {}
        s1 = catalog._score_entry(["git"], popular_but_buried, id_to_name)[0]
        s2 = catalog._score_entry(["git"], prefix_no_installs, id_to_name)[0]
        self.assertLess(s1, s2)

    def test_and_semantics_across_terms(self):
        write(self.tmp / "skills" / "alpha" / "SKILL.md", md("about databases"))
        write(self.tmp / "skills" / "beta" / "SKILL.md", md("about networking"))
        only_alpha = self._search("alpha databases")
        only_beta = self._search("beta networking")
        cross = self._search("alpha networking")
        self.assertTrue(any(h["entry"]["name"] == "alpha" for h in only_alpha))
        self.assertTrue(any(h["entry"]["name"] == "beta" for h in only_beta))
        self.assertFalse(any(h["entry"]["name"] in ("alpha", "beta") for h in cross))

    def test_and_semantics_same_entry_both_terms(self):
        write(self.tmp / "skills" / "gamma" / "SKILL.md", md("databases and networking"))
        hits = self._search("gamma networking")
        self.assertTrue(any(h["entry"]["name"] == "gamma" for h in hits))


class TestPolicy(Base):
    def test_blocked_marketplace_sets_blocked_and_clears_installable(self):
        plugins_map = bulk_cache_plugins(20)
        self.write_settings({"blockedMarketplaces": ["extmkt"]})
        write(self.cache_path(), cache_doc(plugins_map))
        e = self.by_id()["pkg0@extmkt"]
        self.assertTrue(e["blocked"])
        self.assertFalse(e["installable"])

    def test_strict_allowlist_blocks_everything_not_listed(self):
        plugins_map = bulk_cache_plugins(20)
        self.write_settings({"strictKnownMarketplaces": ["somewhere-else"]})
        write(self.cache_path(), cache_doc(plugins_map))
        e = self.by_id()["pkg0@extmkt"]
        self.assertTrue(e["blocked"])

    def test_empty_strict_list_blocks_nothing(self):
        plugins_map = bulk_cache_plugins(20)
        self.write_settings({"strictKnownMarketplaces": []})
        write(self.cache_path(), cache_doc(plugins_map))
        e = self.by_id()["pkg0@extmkt"]
        self.assertFalse(e["blocked"])
        self.assertTrue(e["installable"])

    def test_installed_entries_never_blocked(self):
        self.write_settings({"blockedMarketplaces": ["mkt"]})
        e = self.by_id()["demo@mkt"]
        self.assertFalse(e["blocked"])


class TestResolveInstall(Base):
    def test_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            catalog.resolve_install("nope@nowhere", None)

    def test_component_raises(self):
        with self.assertRaises(ValueError):
            catalog.resolve_install("demo@mkt/skills/helper", None)

    def test_blocked_raises(self):
        plugins_map = bulk_cache_plugins(20)
        self.write_settings({"blockedMarketplaces": ["extmkt"]})
        write(self.cache_path(), cache_doc(plugins_map))
        with self.assertRaises(ValueError):
            catalog.resolve_install("pkg0@extmkt", None)

    def test_valid_installable_id_resolves(self):
        plugins_map = bulk_cache_plugins(20)
        write(self.cache_path(), cache_doc(plugins_map))
        self.assertEqual(catalog.resolve_install("pkg0@extmkt", None), "pkg0@extmkt")


class TestGetEntry(Base):
    """get_entry(): the read-only id -> Entry lookup /api/skill-audit uses to
    resolve a request's `id` server-side, mirroring resolve_install()'s
    "the request's copy is used to look it up and then discarded" property
    without resolve_install()'s installability/policy checks."""

    def test_unknown_id_returns_none(self):
        self.assertIsNone(catalog.get_entry("nope@nowhere"))

    def test_known_plugin_id_resolves_to_its_entry(self):
        e = catalog.get_entry("demo@mkt")
        self.assertIsNotNone(e)
        self.assertEqual(e["kind"], "plugin")
        self.assertEqual(e["name"], "demo")

    def test_known_skill_component_id_resolves_with_marketplace(self):
        e = catalog.get_entry("demo@mkt/skills/helper")
        self.assertIsNotNone(e)
        self.assertEqual(e["kind"], "skill")
        self.assertEqual(e["name"], "helper")
        self.assertEqual(e["marketplace"], "mkt")

    def test_yours_skill_has_no_marketplace(self):
        """A "yours" skill (authored locally, not from any marketplace) is
        exactly the case /api/skill-audit refuses to audit — get_entry()
        itself does not refuse anything, it just reports what is on record."""
        write(self.tmp / "skills" / "myskill" / "SKILL.md", md("mine"))
        e = catalog.get_entry("yours:skills:myskill")
        self.assertIsNotNone(e)
        self.assertIsNone(e["marketplace"])


if __name__ == "__main__":
    unittest.main()
