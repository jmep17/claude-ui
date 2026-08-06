"""Output-style frontmatter fields, and the presets shipped in the package.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_output_styles.py`.

The point of the preset cases is drift: FIELDS is hand-written from
https://code.claude.com/docs/en/output-styles because there is no official
schema for these four keys, and a preset that quietly stops matching it would
otherwise ship as a broken starting point.
"""

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import core, output_styles, settings  # noqa: E402


DOCUMENTED = {"name", "description", "keep-coding-instructions",
              "force-for-plugin"}


class Fields(unittest.TestCase):

    def test_fields_cover_documented_frontmatter(self):
        self.assertEqual({f["key"] for f in output_styles.FIELDS}, DOCUMENTED)

    def test_every_field_carries_form_and_popover_text(self):
        for f in output_styles.FIELDS:
            self.assertIn(f["type"], ("text", "bool"), f["key"])
            self.assertTrue(f["desc"].strip(), f["key"])
            self.assertTrue(len(f["help"]) > 40, f["key"])


class Validate(unittest.TestCase):

    def test_accepts_the_documented_fields(self):
        self.assertEqual(output_styles.validate({
            "name": "ADHD", "description": "brief",
            "keep-coding-instructions": "true", "force-for-plugin": "false",
        }, strict=True), [])

    def test_rejects_unknown_key_when_strict(self):
        errs = output_styles.validate({"colour": "red"}, strict=True)
        self.assertEqual(len(errs), 1)
        self.assertIn("colour", errs[0])

    def test_allows_unknown_key_when_loose(self):
        # Claude Code tolerates extra frontmatter, so this app must not be the
        # stricter of the two on something a user typed
        self.assertEqual(output_styles.validate({"colour": "red"}), [])

    def test_rejects_non_boolean(self):
        errs = output_styles.validate({"keep-coding-instructions": "maybe"})
        self.assertEqual(len(errs), 1)
        self.assertIn("keep-coding-instructions", errs[0])

    def test_quoted_true_is_not_true(self):
        # parse_frontmatter keeps quotes, and Claude Code would not read
        # '"true"' as true either — so neither do we
        self.assertFalse(output_styles.is_true({"force-for-plugin": '"true"'},
                                               "force-for-plugin"))
        self.assertTrue(output_styles.is_true({"force-for-plugin": "TRUE"},
                                              "force-for-plugin"))


class Presets(unittest.TestCase):

    def test_bundled_presets_validate_strictly(self):
        files = sorted(output_styles.PRESET_DIR.glob("*.md"))
        self.assertTrue(files, "no presets are shipped")
        for p in files:
            meta = core.parse_frontmatter(p.read_text())
            self.assertEqual(output_styles.validate(meta, strict=True), [],
                             f"{p.name} does not match FIELDS")

    def test_presets_lists_adhd(self):
        by_id = {s["id"]: s for s in output_styles.presets()}
        self.assertIn("adhd", by_id)
        s = by_id["adhd"]
        self.assertEqual(s["name"], "ADHD")
        self.assertTrue(s["description"].strip())
        self.assertTrue(s["keep-coding-instructions"])
        self.assertTrue(s["body"].strip())
        self.assertTrue(s["content"].startswith("---\n"))

    def test_adhd_is_not_a_plugin_style(self):
        meta = core.parse_frontmatter(
            (output_styles.PRESET_DIR / "adhd.md").read_text())
        self.assertNotIn("force-for-plugin", meta)

    def test_preset_body_is_the_content_minus_frontmatter(self):
        for s in output_styles.presets():
            self.assertTrue(s["content"].endswith(s["body"]))
            self.assertNotIn("keep-coding-instructions", s["body"])

    def test_bad_preset_is_skipped_not_raised(self):
        saved = output_styles.PRESET_DIR
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            (tmp / "good.md").write_text("---\nname: Good\n---\nbody\n")
            (tmp / "bad.md").write_text("---\nname: Bad\ncolour: red\n---\nx\n")
            output_styles.PRESET_DIR = tmp
            try:
                got = [s["id"] for s in output_styles.presets()]
            finally:
                output_styles.PRESET_DIR = saved
        self.assertEqual(got, ["good"])


class SettingsIntegration(unittest.TestCase):

    def test_output_style_values_come_from_the_official_schema(self):
        hand = [s for s in settings.SETTINGS_RAW if s["key"] == "outputStyle"][0]
        self.assertNotIn("values", hand,
                         "built-in styles must come from the vendored schema")
        merged = [s for s in settings.settings_schema()
                  if s["key"] == "outputStyle"][0]
        self.assertEqual(merged["values"],
                         ["default", "Proactive", "Explanatory", "Learning"])


if __name__ == "__main__":
    unittest.main()
