"""Setup pieces, and the token-saver settings preset shipped in the package.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_setup.py`.

The point of the PresetData cases is drift: the preset names real settings.json
keys and the vendored official schema is the floor for what it may name. A key
upstream renames or retires, a value that stops matching its enum, or — the
trap this repo already documents — a key Claude Code silently ignores in
settings.json (schema.GLOBAL_CONFIG_KEYS) must fail the build here, loudly,
not ship as a preset that quietly does nothing.
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import caveman, schema, settings, settings_presets, setup  # noqa: E402


class PresetData(unittest.TestCase):
    """The shipped token-saver.json, held to the vendored official schema."""

    def entries(self):
        return settings_presets.preset_entries("token-saver")

    def test_nine_unique_valid_keys_each_with_a_why(self):
        entries = self.entries()
        keys = [e["key"] for e in entries]
        self.assertEqual(len(keys), 9)
        self.assertEqual(len(keys), len(set(keys)))
        for e in entries:
            self.assertTrue(settings.SETTINGS_KEY_RE.match(e["key"]))
            self.assertTrue(e["why"].strip())

    def test_every_key_is_in_the_official_schema(self):
        known = schema.vendored()["keys"]
        for e in self.entries():
            self.assertIn(e["key"], known)

    def test_values_conform_to_schema_type_and_enum(self):
        pytypes = {"string": str, "boolean": bool, "number": (int, float)}
        known = schema.vendored()["keys"]
        for e in self.entries():
            spec = known[e["key"]]
            want = pytypes.get(spec.get("type"))
            if want:
                self.assertIsInstance(e["value"], want, e["key"])
            if spec.get("enum"):
                self.assertIn(e["value"], spec["enum"], e["key"])

    def test_no_key_is_a_global_config_trap(self):
        # settings.json silently ignores these; the doctor warns on them.
        # A preset that wrote one would be a no-op that trips our own doctor.
        for e in self.entries():
            self.assertNotIn(e["key"], schema.GLOBAL_CONFIG_KEYS)
            self.assertNotIn(e["key"].split(".")[0], schema.GLOBAL_CONFIG_KEYS)

    def test_the_two_headline_values(self):
        by_key = {e["key"]: e["value"] for e in self.entries()}
        self.assertEqual(by_key["model"], "sonnet")
        self.assertEqual(by_key["env.CLAUDE_CODE_SUBAGENT_MODEL"], "haiku")


class PieceLifecycle(unittest.TestCase):
    """Apply / drift / remove against a tempdir settings.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "settings.json")
        self._config_dir = settings.config_dir
        settings.config_dir = lambda: pathlib.Path(self.tmp.name)
        self.entries = settings_presets.preset_entries("token-saver")

    def tearDown(self):
        settings.config_dir = self._config_dir
        self.tmp.cleanup()

    def read(self):
        with open(self.path) as fh:
            return json.load(fh)

    def write(self, data):
        with open(self.path, "w") as fh:
            json.dump(data, fh)

    def state(self):
        return settings_presets.preset_state("token-saver")

    def test_empty_config_is_not_installed(self):
        st = self.state()
        self.assertFalse(st["installed"])
        self.assertIn("none currently", st["detail"])
        self.assertEqual(len(st["notes"]), len(self.entries))

    def test_apply_patches_never_replaces(self):
        self.write({"theme": "dark", "env": {"FOO": "bar"}})
        settings_presets.preset_apply("token-saver")
        data = self.read()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"]["FOO"], "bar")
        for e in self.entries:
            self.assertEqual(settings_presets._current(data, e["key"]),
                             e["value"])
        st = self.state()
        self.assertTrue(st["installed"])
        self.assertIn("all 9", st["detail"])

    def test_reapply_writes_nothing(self):
        settings_presets.preset_apply("token-saver")
        with open(self.path) as fh:
            before = fh.read()
        orig, calls = settings.atomic_write, []
        settings.atomic_write = lambda p, t: calls.append(p) or orig(p, t)
        try:
            settings_presets.preset_apply("token-saver")
        finally:
            settings.atomic_write = orig
        self.assertEqual(calls, [])
        with open(self.path) as fh:
            self.assertEqual(fh.read(), before)

    def test_drifted_key_shows_partial(self):
        settings_presets.preset_apply("token-saver")
        settings.settings_set("model", "opus")
        st = self.state()
        self.assertFalse(st["installed"])
        self.assertIn("8 of 9", st["detail"])

    def test_remove_leaves_drift_and_user_siblings(self):
        self.write({"env": {"FOO": "bar"}})
        settings_presets.preset_apply("token-saver")
        settings.settings_set("model", "opus")
        settings_presets.preset_remove("token-saver")
        data = self.read()
        self.assertEqual(data["model"], "opus")     # the user's edit stays
        self.assertEqual(data["env"], {"FOO": "bar"})
        for e in self.entries:
            if e["key"] != "model":
                self.assertIs(settings_presets._current(data, e["key"]),
                              settings_presets._MISSING)

    def test_remove_after_clean_apply_prunes_env(self):
        settings_presets.preset_apply("token-saver")
        settings_presets.preset_remove("token-saver")
        self.assertEqual(self.read(), {})

    def test_invalid_settings_json_is_soft_in_state_loud_on_apply(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        st = self.state()
        self.assertFalse(st["installed"])
        self.assertIn("unreadable", st["detail"])
        with self.assertRaises(ValueError):
            settings_presets.preset_apply("token-saver")
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "{not json")


class BrokenPresetFile(unittest.TestCase):
    """A bad shipped file must cost a visible broken row, not a dead
    /api/setup — and must refuse to apply."""

    def broken(self, text):
        saved = settings_presets.PRESET_DIR
        tmp = pathlib.Path(tempfile.mkdtemp())
        (tmp / "token-saver.json").write_text(text)
        settings_presets.PRESET_DIR = tmp
        self.addCleanup(setattr, settings_presets, "PRESET_DIR", saved)

    def test_state_is_soft(self):
        self.broken('[{"key": "model"}]')
        st = settings_presets.preset_state("token-saver")
        self.assertFalse(st["installed"])
        self.assertIn("token-saver", st["detail"])
        self.assertEqual(st["notes"], [])

    def test_apply_is_loud(self):
        self.broken("{not json")
        with self.assertRaises(ValueError):
            settings_presets.preset_apply("token-saver")


class Registry(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._config_dir = caveman.config_dir
        caveman.config_dir = lambda: pathlib.Path(self.tmp.name)
        self.addCleanup(setattr, caveman, "config_dir", self._config_dir)

    def test_pieces_and_dispatch(self):
        ids = [p["id"] for p in setup.setup_state()["pieces"]]
        self.assertEqual(ids, ["statusline", "token-saver", "zsh-claude",
                               "local-model", "caveman", "handoff"])

    def test_token_saver_is_removable(self):
        piece = [p for p in setup.setup_state()["pieces"]
                 if p["id"] == "token-saver"][0]
        self.assertTrue(piece["removable"])

    def test_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            setup.setup_apply("nope")
        with self.assertRaises(ValueError):
            setup.setup_remove("nope")

    def test_setup_config_dispatches_to_caveman(self):
        setup.setup_config("caveman", {"level": "ultra"})
        self.assertEqual(caveman.caveman_level(), "ultra")

    def test_setup_config_refuses_empty_values(self):
        caveman.caveman_level_set("ultra")
        with self.assertRaises(ValueError):
            setup.setup_config("caveman", {})
        self.assertEqual(caveman.caveman_level(), "ultra")

    def test_setup_config_refuses_a_piece_without_settings(self):
        with self.assertRaises(ValueError):
            setup.setup_config("statusline", {})

    def test_setup_config_refuses_an_unknown_piece(self):
        with self.assertRaises(ValueError):
            setup.setup_config("nope", {})


if __name__ == "__main__":
    unittest.main()
