"""Settings schema invariants and settings.json write/clear round-trips.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_settings.py`.

The write tests point settings.config_dir at a temp directory rather than
touching the real ~/.claude — patched in the `settings` namespace, since
core.config_dir() consults .claude-ui.json before $CLAUDE_CONFIG_DIR and so
can't be redirected by the environment alone.
"""

import datetime
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import schema, settings  # noqa: E402

SCHEMA = settings.SETTINGS_SCHEMA
BY_KEY = {s["key"]: s for s in SCHEMA}
RAW_BY_KEY = {s["key"]: s for s in settings.SETTINGS_RAW}

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


class TestSnapshot(unittest.TestCase):
    """The vendored copy of the official JSON Schema."""

    SNAP = schema.vendored()

    def test_snapshot_loads(self):
        for field in ("source", "resolved", "fetched", "keys"):
            self.assertIn(field, self.SNAP)
        self.assertGreaterEqual(len(self.SNAP["keys"]), 120)

    def test_entry_shapes(self):
        for key, e in self.SNAP["keys"].items():
            self.assertTrue(e.get("description", "").strip(), key)
            self.assertIsInstance(e.get("managed"), bool, key)
            if "enum" in e:
                self.assertTrue(e["enum"], key)
                for v in e["enum"]:
                    self.assertIsInstance(v, (str, int, float, bool), key)
            if "doc" in e:
                self.assertRegex(e["doc"], r"^https://\w+\.claude\.com/docs/", key)

    def test_snapshot_is_deterministic(self):
        """Catches a hand-edited snapshot: it must be exactly what the tool writes."""
        self.assertEqual(schema.OFFICIAL_PATH.read_text(),
                         schema.serialize(self.SNAP))

    def test_snapshot_is_not_ancient(self):
        """The only offline staleness signal there is — a repo with no CI gets one
        deliberate failure a year rather than silent rot."""
        age = (datetime.date.today()
               - datetime.date.fromisoformat(self.SNAP["fetched"])).days
        self.assertLess(age, 365,
                        "snapshot is over a year old — run: "
                        "python3 tools/sync_settings_schema.py")

    def test_missing_snapshot_degrades(self):
        """No snapshot on disk must mean stale help, never a crash."""
        real = schema.OFFICIAL_PATH
        try:
            schema.OFFICIAL_PATH = pathlib.Path("/nonexistent/settings_schema.json")
            schema.vendored.cache_clear()
            self.assertEqual(schema.vendored(), {})
            merged = schema.merge(settings.SETTINGS_RAW)
            self.assertEqual(len(merged), len(settings.SETTINGS_RAW))
            self.assertTrue(all(s.get("doc") for s in merged))
        finally:
            schema.OFFICIAL_PATH = real
            schema.vendored.cache_clear()


class TestMerge(unittest.TestCase):
    """Official facts land on the rows without displacing hand curation."""

    # what merge() must never touch
    CURATED = ("type", "cat", "aka", "fields", "templates", "item_values",
               "key_values", "value_type")

    def test_hand_curation_survives(self):
        for key, raw in RAW_BY_KEY.items():
            merged = BY_KEY[key]
            for f in self.CURATED:
                self.assertEqual(merged.get(f), raw.get(f), key + "." + f)
            if raw.get("desc"):
                self.assertEqual(merged["desc"], raw["desc"], key)

    def test_generated_descs_fill_the_gaps(self):
        """Every row shows something under its key, curated or derived."""
        for s in SCHEMA:
            self.assertTrue(s["desc"].strip(), s["key"] + " has no desc")

    def test_values_are_a_superset(self):
        for key, raw in RAW_BY_KEY.items():
            before = raw.get("values")
            if not before:
                continue
            after = BY_KEY[key].get("values") or []
            self.assertEqual(after[:len(before)], before,
                             key + ": curated order must stay the prefix")

    def test_official_enum_values_are_all_offered(self):
        """The live drift catcher: a value upstream adds must reach the dropdown."""
        off = schema.official()
        for s in SCHEMA:
            allowed = (off.get(s["key"]) or {}).get("enum")
            if not allowed:
                continue
            offered = s.get("values") or []
            for v in allowed:
                self.assertIn(v, offered, s["key"])

    def test_delegate_reached_default_mode(self):
        """The concrete drift that motivated all of this."""
        self.assertIn("delegate", BY_KEY["permissions.defaultMode"]["values"])

    def test_official_defaults_win(self):
        self.assertEqual(BY_KEY["workflowSizeGuideline"]["default"], "unrestricted")
        self.assertIs(BY_KEY["includeCoAuthoredBy"]["default"], True)
        self.assertIs(BY_KEY["fastMode"]["default"], False)

    def test_control_type_not_contradicted(self):
        """A hand control type must be able to hold the official JSON type.

        This is the check that would have caught modelling disableAutoMode
        (string with one allowed value) as a bool.
        """
        ok = {
            "bool": {"boolean"},
            "number": {"number", "integer"},
            "string": {"string"}, "combo": {"string"}, "enum": {"string", "number"},
            "list": {"array"},
            "kv": {"object"}, "object": {"object"},
            "json": {"object", "array", "string", "boolean", "number", "integer"},
        }
        off = schema.official()
        for s in SCHEMA:
            t = (off.get(s["key"]) or {}).get("type")
            if t is None:
                continue
            got = set(t) if isinstance(t, list) else {t}
            got.discard("null")
            if not got:
                continue
            self.assertTrue(got & ok[s["type"]],
                            f"{s['key']}: control {s['type']} vs official {sorted(got)}")

    def test_every_entry_resolves_a_doc_url(self):
        for s in SCHEMA:
            self.assertRegex(s.get("doc", ""), r"^https://\w+\.claude\.com/docs/",
                             s["key"])

    def test_unverified_set_is_frozen(self):
        """Not listed in the official schema. additionalProperties is true, so
        absence is not disproof — but the set should only change deliberately."""
        expected = {
            "gitAttributionEmail", "gitAttributionName", "interactiveEditingEnabled",
            "interfaceLanguage", "invalidSSLWarning", "keyBindings",
            "llmConnectionTimeout", "llmRequestTimeout", "maxCompactMessages",
            "mcpServerTimeouts", "proxy", "remote.defaultEnvironmentId",
            "restartOnConfigChange", "sessionHistorySize", "showHiddenFiles",
            "skipFirstRunQuestions", "strikethrough", "switchModelsOnFlag",
            "telemetryEnabled", "thinkingBudgetTokens", "warningOnSandboxEscape",
            "workspaceInitScript",
        }
        self.assertEqual({s["key"] for s in SCHEMA if s.get("unverified")}, expected)

    def test_managed_category_is_explicit(self):
        """The (Managed settings) prose convention also tags disableAgentView and
        sshConfigs, which ship as ordinary rows. It may badge; it must not place."""
        in_cat = {s["key"] for s in SCHEMA if s["cat"] == schema.MANAGED_CAT}
        self.assertEqual(in_cat, {k for k, _ in settings.MANAGED_KEYS})
        self.assertTrue(all(BY_KEY[k].get("managed") for k in in_cat))
        for key in ("disableAgentView", "sshConfigs"):
            self.assertEqual(BY_KEY[key]["cat"], "system", key)

    def test_managed_group_renders_last(self):
        """renderSettings buckets in SCHEMA order, so position is the ordering."""
        cats = list(dict.fromkeys(s["cat"] for s in SCHEMA))
        self.assertEqual(cats[-1], schema.MANAGED_CAT)

    def test_global_config_keys_are_not_rows(self):
        """The official schema lists them; the docs say settings.json ignores them."""
        for key in schema.GLOBAL_CONFIG_KEYS:
            self.assertNotIn(key, BY_KEY, key + " belongs to ~/.claude.json")

    def test_dangerous_mode_key_moved_to_top_level(self):
        self.assertIn("skipDangerousModePermissionPrompt", BY_KEY)
        self.assertNotIn("permissions.skipDangerousModePermissionPrompt", BY_KEY)

    def test_env_vars_derive_from_the_snapshot(self):
        self.assertTrue(set(settings.ENV_VARS) - set(settings.ENV_EXTRA)
                        <= schema.env_var_names())
        self.assertFalse(set(settings.ENV_VARS) & settings.ENV_READONLY)
        for name in settings.ENV_EXTRA:
            self.assertIn(name, settings.ENV_VARS)

    def test_hook_events_cover_the_documented_set(self):
        self.assertTrue(set(schema.hook_events()) <= set(settings.HOOK_EVENTS))
        self.assertEqual(settings.HOOK_EVENTS[:len(settings.HOOK_EVENTS_COMMON)],
                         settings.HOOK_EVENTS_COMMON)

    def test_help_payload_stays_small(self):
        """Guards the eager/lazy split — nobody folds 340 env descriptions in here."""
        payload = schema.help_payload(s["key"] for s in SCHEMA)
        self.assertLess(len(json.dumps(payload)), 120_000)
        self.assertIn("effortLevel", payload)


class TestSchemaBuild(unittest.TestCase):
    """flatten/validate/doc_url, against hand-built documents."""

    def doc(self, props, **kw):
        return {"$schema": "http://json-schema.org/draft-07/schema#",
                "properties": props, **kw}

    def test_flatten_handles_composition(self):
        flat = schema.flatten(self.doc({
            "theme": {"description": "d", "anyOf": [{"type": "string",
                                                     "enum": ["dark", "light"]},
                                                    {"type": "string"}]},
            "maybe": {"description": "d", "type": ["string", "null"]},
            "untyped": {"description": "d"},
            "nest": {"description": "d", "type": "object",
                     "properties": {"leaf": {"description": "d", "type": "boolean"}}},
        }))
        self.assertEqual(flat["theme"]["enum"], ["dark", "light"])
        self.assertEqual(flat["maybe"]["type"], ["string", "null"])
        self.assertNotIn("type", flat["untyped"])
        self.assertIn("nest.leaf", flat)

    def test_doc_url_prefers_the_anchored_one(self):
        # the three-URL `permissions` shape: only one carries an anchor
        self.assertEqual(
            schema.doc_url("See https://code.claude.com/docs/en/permissions and "
                           "https://code.claude.com/docs/en/settings#permission-settings "
                           "and https://code.claude.com/docs/en/tools-reference"),
            "https://code.claude.com/docs/en/settings#permission-settings")

    def test_doc_url_strips_trailing_punctuation(self):
        self.assertEqual(schema.doc_url("See https://code.claude.com/docs/en/hooks."),
                         "https://code.claude.com/docs/en/hooks")

    def test_doc_url_absent(self):
        self.assertIsNone(schema.doc_url("no link here"))

    def test_resolve_doc_falls_back(self):
        self.assertEqual(schema.resolve_doc("statusLine.command", ""),
                         schema.DOC_BASE + "statusline")
        self.assertEqual(schema.resolve_doc("someBrandNewKey", ""),
                         schema.DOC_BASE + "settings")

    def test_validate_rejects_a_truncated_document(self):
        with self.assertRaises(ValueError):
            schema.validate(self.doc({"a": {"description": "d", "type": "string"}}))

    def test_validate_rejects_a_wrong_draft(self):
        bad = self.doc({}, **{})
        bad["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        with self.assertRaises(ValueError):
            schema.validate(bad)

    def test_validate_rejects_undescribed_keys(self):
        with self.assertRaises(ValueError):
            schema.validate(self.doc(
                {f"k{i}": {"type": "string"} for i in range(130)}))


class TestLiveOverlay(unittest.TestCase):
    def setUp(self):
        self._get, self._live = schema._get, schema._live
        self._gen = schema._generation

    def tearDown(self):
        schema._get = self._get
        schema._live = self._live
        schema._generation = self._gen

    def fake_doc(self, extra=None):
        props = {f"k{i}": {"type": "string", "description": f"desc {i}"}
                 for i in range(130)}
        props.update(extra or {})
        return json.dumps({"$schema": "http://json-schema.org/draft-07/schema#",
                           "properties": props})

    def test_live_overlay_wins_and_bumps_generation(self):
        schema._get = lambda url: self.fake_doc({
            "model": {"type": "string", "description": "a fresher description"}})
        schema._fetch_official()
        self.assertEqual(schema._generation, self._gen + 1)
        self.assertEqual(schema.official()["model"]["description"],
                         "a fresher description")
        self.assertEqual(schema.merge(settings.SETTINGS_RAW)[0]["key"], "model")

    def test_live_never_deletes_vendored_keys(self):
        """The vendored snapshot is the floor: a thin live doc must not blank the UI."""
        schema._get = lambda url: self.fake_doc()
        schema._fetch_official()
        self.assertIn("effortLevel", schema.official())

    def test_fetch_failure_leaves_the_vendored_snapshot(self):
        def boom(url):
            raise OSError("offline")

        schema._get = boom
        schema._live = {}
        schema._fetch_official()       # must not raise
        self.assertEqual(schema._live, {})
        self.assertIn("effortLevel", schema.official())

    def test_fetch_rejects_a_truncated_document(self):
        schema._get = lambda url: json.dumps({
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {"model": {"type": "string", "description": "d"}}})
        schema._live = {}
        schema._fetch_official()
        self.assertEqual(schema._live, {})

    def test_settings_schema_tracks_generation(self):
        schema._get = lambda url: self.fake_doc({
            "model": {"type": "string", "description": "d", "default": "haiku"}})
        schema._fetch_official()
        by_key = {s["key"]: s for s in settings.settings_schema()}
        self.assertEqual(by_key["model"]["default"], "haiku")


@unittest.skipUnless(os.environ.get("CLAUDE_UI_NET_TESTS"),
                     "network test — set CLAUDE_UI_NET_TESTS=1 to run")
class TestSnapshotDrift(unittest.TestCase):
    """Opt-in rather than skip-on-failure: offline is indistinguishable from
    'the URL moved', and a test that silently skips stops running exactly when
    it matters most."""

    def test_vendored_matches_live(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "tools"))
        import sync_settings_schema as sync
        self.assertEqual(sync.main(["--check"]), 0)


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
