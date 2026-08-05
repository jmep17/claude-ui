"""Settings schema invariants and settings.json write/clear round-trips.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_settings.py`.

The write tests point settings.config_dir at a temp directory rather than
touching the real ~/.claude — patched in the `settings` namespace, since
core.config_dir() consults .claude-ui.json before $CLAUDE_CONFIG_DIR and so
can't be redirected by the environment alone.
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import settings  # noqa: E402

SCHEMA = settings.SETTINGS_SCHEMA
BY_KEY = {s["key"]: s for s in SCHEMA}

VALUE_LISTS = ("values", "item_values", "key_values")
CONTROL_TYPES = {"bool", "number", "string", "enum", "combo", "list", "kv",
                 "object", "json"}


class TestSchema(unittest.TestCase):
    def test_required_fields(self):
        for s in SCHEMA:
            self.assertIn("key", s)
            for field in ("type", "cat", "desc"):
                self.assertIn(field, s, s["key"] + " is missing " + field)
            self.assertIn(s["type"], CONTROL_TYPES, s["key"])
            self.assertTrue(s["desc"].strip(), s["key"] + " has an empty desc")

    def test_no_duplicate_keys(self):
        """The dedupe at import time hides duplicates — assert it removed none."""
        self.assertEqual(len(SCHEMA), len(BY_KEY))

    def test_keys_are_writable(self):
        """A mistyped key fails here rather than as a 400 from /api/settings-set."""
        for s in SCHEMA:
            self.assertRegex(s["key"], settings.SETTINGS_KEY_RE)

    def test_value_lists_are_scalars(self):
        for s in SCHEMA:
            for field in VALUE_LISTS:
                for v in s.get(field, []):
                    self.assertIsInstance(v, (str, int, float),
                                          s["key"] + "." + field)

    def test_aka_shape(self):
        for s in SCHEMA:
            if "aka" not in s:
                continue
            self.assertIsInstance(s["aka"], list, s["key"])
            for a in s["aka"]:
                self.assertIsInstance(a, str, s["key"])
                self.assertTrue(a.strip(), s["key"] + " has a blank aka entry")

    def test_enums_have_values(self):
        for s in SCHEMA:
            if s["type"] == "enum":
                self.assertTrue(s.get("values"), s["key"] + " enum has no values")
            if s["type"] == "object":
                self.assertTrue(s.get("fields"), s["key"] + " object has no fields")


class TestModelKeys(unittest.TestCase):
    """The model settings the UI is meant to surface."""

    PROMOTED = ["env.CLAUDE_CODE_SUBAGENT_MODEL", "env.ANTHROPIC_MODEL",
                "env.ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "env.ANTHROPIC_DEFAULT_OPUS_MODEL",
                "env.ANTHROPIC_DEFAULT_SONNET_MODEL",
                "env.ANTHROPIC_DEFAULT_FABLE_MODEL"]

    def test_promoted_keys_present(self):
        for key in self.PROMOTED:
            self.assertIn(key, BY_KEY)
            s = BY_KEY[key]
            self.assertEqual(s["cat"], "model")
            # combo, not enum: Bedrock/Vertex/gateway IDs must stay typeable
            self.assertEqual(s["type"], "combo", key)

    def test_promoted_keys_filter_past_the_datalist_threshold(self):
        """>6 suggestions is what makes filterInput() render the filter popup."""
        for key in self.PROMOTED:
            self.assertGreater(len(BY_KEY[key]["values"]), 6, key)

    def test_subagent_key_offers_inherit(self):
        self.assertIn("inherit", BY_KEY["env.CLAUDE_CODE_SUBAGENT_MODEL"]["values"])

    def test_searchable_by_intent(self):
        """Typing these words in the settings filter must reach the right row."""
        def hits(q):
            return {s["key"] for s in SCHEMA
                    if q in s["key"].lower() or q in s["desc"].lower()
                    or any(q in a.lower() for a in s.get("aka", []))}

        self.assertIn("env.CLAUDE_CODE_SUBAGENT_MODEL", hits("subagent"))
        self.assertIn("env.ANTHROPIC_DEFAULT_HAIKU_MODEL", hits("background"))
        # the deprecated name someone will paste from an old config
        self.assertIn("env.ANTHROPIC_DEFAULT_HAIKU_MODEL",
                      hits("anthropic_small_fast_model"))

    def test_family_first_ordering(self):
        for fam in ("haiku", "opus", "sonnet", "fable"):
            ordered = settings._family_first(fam)
            self.assertCountEqual(ordered, settings.MODEL_IDS, fam)
            first = [m for m in ordered if fam in m]
            self.assertEqual(ordered[:len(first)], first, fam)

    def test_model_valued_keys_resolve(self):
        """This list rots silently otherwise — every entry must name a real key."""
        for entry in settings.MODEL_VALUED_KEYS:
            if entry.endswith(":key"):
                base = entry[:-len(":key")]
                self.assertIn(base, BY_KEY, entry)
                self.assertEqual(BY_KEY[base]["type"], "kv", entry)
            else:
                self.assertIn(entry, BY_KEY, entry)


class TestDocsFetch(unittest.TestCase):
    MODELS_DOC = "\n".join([
        "| Feature | Claude Opus 5 | Claude Sonnet 5 |",
        "| Claude API | claude-opus-5 | claude-sonnet-5 |",
    ])

    def setUp(self):
        self._get = settings._get
        self._values = dict(settings._docs_values)
        settings._docs_values.clear()

    def tearDown(self):
        settings._get = self._get
        settings._docs_values.clear()
        settings._docs_values.update(self._values)

    def test_ids_reach_every_model_valued_key(self):
        def fake_get(url):
            if url == settings.MODELS_DOC_URL:
                return self.MODELS_DOC
            raise OSError("settings doc not under test")

        settings._get = fake_get
        settings._fetch_docs_values()
        for key in settings.MODEL_VALUED_KEYS:
            self.assertEqual(settings._docs_values.get(key),
                             ["claude-opus-5", "claude-sonnet-5"], key)

    def test_keys_do_not_share_one_list(self):
        """Aliased lists would let one key's later merge leak into another's."""
        def fake_get(url):
            if url == settings.MODELS_DOC_URL:
                return self.MODELS_DOC
            raise OSError("settings doc not under test")

        settings._get = fake_get
        settings._fetch_docs_values()
        self.assertIsNot(settings._docs_values["model"],
                         settings._docs_values["fallbackModel"])

    def test_network_failure_leaves_statics(self):
        def boom(url):
            raise OSError("offline")

        settings._get = boom
        settings._fetch_docs_values()   # must not raise
        self.assertEqual(settings._docs_values, {})


class TestSettingsSet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "settings.json")
        self._config_dir = settings.config_dir
        settings.config_dir = lambda: pathlib.Path(self.tmp.name)

    def tearDown(self):
        settings.config_dir = self._config_dir
        self.tmp.cleanup()

    def read(self):
        return json.loads(self.raw())

    def raw(self):
        with open(self.path) as fh:
            return fh.read()

    def test_dotted_key_nests(self):
        settings.settings_set("env.ANTHROPIC_MODEL", "opus")
        self.assertEqual(self.read(), {"env": {"ANTHROPIC_MODEL": "opus"}})

    def test_siblings_coexist(self):
        settings.settings_set("env.ANTHROPIC_MODEL", "opus")
        settings.settings_set("env.CLAUDE_CODE_SUBAGENT_MODEL", "haiku")
        self.assertEqual(self.read(), {"env": {"ANTHROPIC_MODEL": "opus",
                                               "CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}})

    def test_clear_one_keeps_the_other(self):
        settings.settings_set("env.ANTHROPIC_MODEL", "opus")
        settings.settings_set("env.CLAUDE_CODE_SUBAGENT_MODEL", "haiku")
        settings.settings_set("env.ANTHROPIC_MODEL", None)
        self.assertEqual(self.read(), {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}})

    def test_clearing_the_last_child_drops_the_parent(self):
        settings.settings_set("model", "opus")
        settings.settings_set("env.ANTHROPIC_MODEL", "opus")
        settings.settings_set("env.ANTHROPIC_MODEL", None)
        self.assertEqual(self.read(), {"model": "opus"})

    def test_clear_without_the_parent_does_not_touch_the_file(self):
        settings.settings_set("model", "opus")
        before = self.raw()
        settings.settings_set("env.ANTHROPIC_MODEL", None)
        self.assertEqual(self.raw(), before)

    def test_bad_key_rejected(self):
        with self.assertRaises(ValueError):
            settings.settings_set("env.ANTHROPIC MODEL", "opus")

    def test_invalid_json_is_not_clobbered(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            settings.settings_set("env.ANTHROPIC_MODEL", "opus")
        self.assertEqual(self.raw(), "{not json")


if __name__ == "__main__":
    unittest.main()
