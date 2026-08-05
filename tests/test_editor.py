"""Path-scoped file access, lost-update protection, and doctor finding targets.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_editor.py`.

Like test_settings.py, these point config_dir at a temp directory rather than
touching the real ~/.claude. core.config_dir() consults .claude-ui.json before
$CLAUDE_CONFIG_DIR, so it can't be redirected by the environment alone; here it
is patched at module level in every namespace that imported it by value.
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import (core, doctor, items, mcp, plugins, settings,  # noqa: E402
                       statusline)

# Every module that did `from .core import config_dir` holds its own binding, so
# redirecting the config dir means rebinding all of them. doctor() reaches
# through settings_state(), mcp_state(), plugins_state() and statusline_paths();
# missing one silently reads the developer's real ~/.claude, and the test then
# passes or fails on whatever happens to be on that machine.
_CFG_USERS = (core, items, settings, mcp, plugins, statusline, doctor)


class TempConfig(unittest.TestCase):
    """A throwaway config dir installed as config_dir() for one test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self.tmp.name) / "claude"
        self.cfg.mkdir()
        self._real = {m: getattr(m, "config_dir", None) for m in _CFG_USERS}
        for m in _CFG_USERS:
            if self._real[m] is not None:
                m.config_dir = lambda: self.cfg

    def tearDown(self):
        for m, fn in self._real.items():
            if fn is not None:
                m.config_dir = fn
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.cfg / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class TestResolveEditable(TempConfig):
    def test_accepts_a_file_in_the_config_dir(self):
        p, readonly = core.resolve_editable(str(self.cfg / "settings.json"))
        self.assertEqual(p, (self.cfg / "settings.json").resolve())
        self.assertFalse(readonly)

    def test_accepts_a_file_that_does_not_exist_yet(self):
        p, _ = core.resolve_editable(str(self.cfg / "nope" / "new.md"))
        self.assertTrue(str(p).endswith("new.md"))

    def test_accepts_claude_json(self):
        p, readonly = core.resolve_editable("~/.claude.json")
        self.assertEqual(p, core.CLAUDE_JSON.resolve())
        self.assertFalse(readonly)

    def test_plugins_are_read_only(self):
        _, readonly = core.resolve_editable(
            str(core.plugins_dir() / "mp" / "pkg" / "skills" / "x" / "SKILL.md"))
        self.assertTrue(readonly)

    def test_rejects_an_outside_path(self):
        with self.assertRaises(ValueError):
            core.resolve_editable("/etc/passwd")

    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            core.resolve_editable(str(self.cfg / ".." / ".." / "etc" / "passwd"))

    def test_rejects_a_relative_path(self):
        with self.assertRaises(ValueError):
            core.resolve_editable("settings.json")

    def test_rejects_a_symlink_that_escapes(self):
        """The check runs on the resolved target, so a link living inside the
        config dir but pointing outside it is still refused."""
        outside = pathlib.Path(self.tmp.name) / "secret.txt"
        outside.write_text("nope")
        link = self.cfg / "innocent.txt"
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            core.resolve_editable(str(link))

    def test_accepts_a_symlink_that_stays_inside(self):
        target = self.write("real.md", "hi")
        link = self.cfg / "alias.md"
        link.symlink_to(target)
        p, readonly = core.resolve_editable(str(link))
        self.assertEqual(p, target.resolve())
        self.assertFalse(readonly)


class TestPathReadWrite(TempConfig):
    def test_round_trip(self):
        self.write("CLAUDE.md", "# hello\n")
        r = items.path_read(str(self.cfg / "CLAUDE.md"))
        self.assertEqual(r["content"], "# hello\n")
        self.assertTrue(r["exists"])
        self.assertFalse(r["readonly"])
        self.assertGreater(r["mtime"], 0)

        items.path_save(str(self.cfg / "CLAUDE.md"), "# bye\n")
        self.assertEqual((self.cfg / "CLAUDE.md").read_text(), "# bye\n")

    def test_missing_file_reads_empty(self):
        r = items.path_read(str(self.cfg / "keybindings.json"))
        self.assertFalse(r["exists"])
        self.assertEqual(r["content"], "")
        self.assertEqual(r["mtime"], 0)

    def test_save_creates_the_file(self):
        items.path_save(str(self.cfg / "new" / "deep.md"), "x\n")
        self.assertEqual((self.cfg / "new" / "deep.md").read_text(), "x\n")

    def test_bad_json_is_refused(self):
        self.write("settings.json", '{"a": 1}')
        with self.assertRaises(ValueError):
            items.path_save(str(self.cfg / "settings.json"), '{"a": 1,}')
        self.assertEqual((self.cfg / "settings.json").read_text(), '{"a": 1}')

    def test_oversize_is_refused(self):
        with self.assertRaises(ValueError):
            items.path_save(str(self.cfg / "big.md"), "x" * (items.MAX_EDIT + 1))

    def test_outside_path_is_refused(self):
        with self.assertRaises(ValueError):
            items.path_read("/etc/passwd")
        with self.assertRaises(ValueError):
            items.path_save("/etc/passwd", "pwned")

    def test_plugin_file_reads_but_does_not_write(self):
        with self.assertRaises(ValueError) as cm:
            items.path_save(
                str(core.plugins_dir() / "mp" / "pkg" / "skills" / "x" / "SKILL.md"),
                "nope")
        self.assertIn("read-only", str(cm.exception))


class TestLostUpdate(TempConfig):
    def test_stale_base_conflicts(self):
        p = self.write("CLAUDE.md", "one\n")
        base = items.path_read(str(p))["mtime"]
        os.utime(p, (base + 10, base + 10))     # someone else wrote it
        with self.assertRaises(items.Conflict):
            items.path_save(str(p), "mine\n", base)
        self.assertEqual(p.read_text(), "one\n")

    def test_matching_base_saves(self):
        p = self.write("CLAUDE.md", "one\n")
        base = items.path_read(str(p))["mtime"]
        items.path_save(str(p), "mine\n", base)
        self.assertEqual(p.read_text(), "mine\n")

    def test_no_base_skips_the_check(self):
        p = self.write("CLAUDE.md", "one\n")
        items.path_save(str(p), "mine\n", None)
        self.assertEqual(p.read_text(), "mine\n")

    def test_conflict_is_a_value_error(self):
        """server.py catches Conflict before ValueError; if it ever stopped
        being one, the generic 400 handler would still work."""
        self.assertTrue(issubclass(items.Conflict, ValueError))


class TestItemTodoLine(TempConfig):
    def test_todo_line_is_recorded(self):
        self.write("skills/demo/SKILL.md",
                   "---\nname: demo\ndescription: d\n---\n\nbody\nTODO: finish\n")
        it = [i for i in items.scan_items("skills") if i["name"] == "demo"][0]
        self.assertTrue(it["todo"])
        self.assertEqual(it["todo_line"], 7)

    def test_no_todo_is_zero(self):
        self.write("skills/clean/SKILL.md", "---\nname: clean\n---\nbody\n")
        it = [i for i in items.scan_items("skills") if i["name"] == "clean"][0]
        self.assertFalse(it["todo"])
        self.assertEqual(it["todo_line"], 0)


class TestDoctorTargets(TempConfig):
    """Every finding the UI offers an Open button for has to carry a target
    that actually resolves. A target pointing at the wrong line is worse than
    no button at all."""

    def find(self, findings, needle, area=None):
        """One finding by message substring. `area` matters more than it looks:
        a malformed settings.json also breaks enabledPlugins, so the same parse
        error legitimately shows up under both `settings` and `plugins`."""
        hits = [f for f in findings
                if needle in f["msg"] and (area is None or f["area"] == area)]
        self.assertTrue(hits, "no finding mentioning " + needle)
        return hits[0]

    def test_bad_settings_json_targets_the_error_line(self):
        self.write("settings.json", '{\n  "a": 1,\n  "b" 2\n}\n')
        f = self.find(doctor.doctor()["findings"], "Expecting", area="settings")
        self.assertEqual(f["target"]["kind"], "path")
        self.assertTrue(f["target"]["path"].endswith("settings.json"))
        self.assertEqual(f["target"]["line"], 3)

    def test_leftover_backup_targets_the_file(self):
        self.write("settings.json.bak", "{}")
        f = self.find(doctor.doctor()["findings"], "leftover backup")
        self.assertEqual(f["target"]["kind"], "path")
        self.assertTrue(f["target"]["path"].endswith("settings.json.bak"))

    def test_todo_targets_the_item_and_line(self):
        self.write("skills/demo/SKILL.md",
                   "---\nname: demo\ndescription: Use when demoing\n---\n"
                   "\nTODO: write this\n")
        f = self.find(doctor.doctor()["findings"], "TODO placeholder")
        t = f["target"]
        self.assertEqual(t["kind"], "item")
        self.assertEqual((t["type"], t["name"], t["file"]), ("skills", "demo", "SKILL.md"))
        self.assertTrue(t["enabled"])
        self.assertEqual(t["line"], 6)

    def test_missing_use_when_targets_the_description(self):
        self.write("skills/vague/SKILL.md",
                   "---\nname: vague\ndescription: does things\n---\nbody\n")
        f = self.find(doctor.doctor()["findings"], "Use when")
        self.assertEqual(f["target"]["find"], "description:")
        self.assertEqual(f["target"]["name"], "vague")

    def test_undocumented_key_targets_a_searchable_string(self):
        self.write("settings.json", json.dumps({"totallyMadeUpKey": 1}))
        f = self.find(doctor.doctor()["findings"], "totallyMadeUpKey")
        self.assertEqual(f["target"]["find"], '"totallyMadeUpKey"')

    def test_broken_symlink_has_no_target(self):
        """Nothing to open — the UI falls back to Copy path."""
        (self.cfg / "dangling").symlink_to(self.cfg / "does-not-exist")
        f = self.find(doctor.doctor()["findings"], "broken symlink")
        self.assertNotIn("target", f)

    def test_every_path_target_is_absolute_and_not_tilded(self):
        """tilde() is a lossy display transform; a target we can't reopen is
        the whole bug this replaced."""
        self.write("settings.json", json.dumps({"totallyMadeUpKey": 1}))
        self.write("settings.json.bak", "{}")
        for f in doctor.doctor()["findings"]:
            t = f.get("target") or {}
            if t.get("kind") == "path":
                self.assertFalse(t["path"].startswith("~"), f["msg"])
                self.assertTrue(os.path.isabs(t["path"]), f["msg"])

    def test_findings_without_targets_still_work(self):
        """The target is additive — level/area/msg keep their old shape."""
        for f in doctor.doctor()["findings"]:
            self.assertIn(f["level"], ("warn", "info"))
            self.assertTrue(f["area"])
            self.assertTrue(f["msg"])


if __name__ == "__main__":
    unittest.main()
