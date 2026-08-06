"""Creating a new item file, and the one skill flag the agent picker needs.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_items.py`.

config_dir is patched in both namespaces that reach the filesystem here: items
(item_root builds the live path from it) and core (disabled_dir resolves it at
call time to find the parking area).
"""

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import core, items  # noqa: E402


AGENT = "---\nname: reviewer\n---\nbody\n"


class Base(unittest.TestCase):
    """An empty temp config dir standing in for ~/.claude."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self._saved = [(m, m.config_dir) for m in (items, core)]
        for m, _ in self._saved:
            m.config_dir = lambda t=self.tmp: t

    def tearDown(self):
        for m, fn in self._saved:
            m.config_dir = fn
        self.tmpdir.cleanup()

    def write(self, rel, text):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class Create(Base):

    def test_creates_agent(self):
        out = items.item_create("agents", "reviewer", AGENT)
        p = self.tmp / "agents" / "reviewer.md"
        self.assertEqual(p.read_text(), AGENT)
        self.assertEqual(out["name"], "reviewer")
        self.assertTrue(out["exists"])
        self.assertEqual(out["content"], AGENT)

    def test_creates_skill_as_directory(self):
        items.item_create("skills", "pdf", "---\nname: pdf\n---\nhow to\n")
        self.assertTrue((self.tmp / "skills" / "pdf").is_dir())
        self.assertEqual((self.tmp / "skills" / "pdf" / "SKILL.md").read_text(),
                         "---\nname: pdf\n---\nhow to\n")

    def test_creates_nested_command(self):
        items.item_create("commands", "git/pr", "open a PR\n")
        self.assertEqual((self.tmp / "commands" / "git" / "pr.md").read_text(),
                         "open a PR\n")

    def test_refuses_existing(self):
        items.item_create("agents", "reviewer", AGENT)
        with self.assertRaises(ValueError):
            items.item_create("agents", "reviewer", "second\n")
        self.assertEqual((self.tmp / "agents" / "reviewer.md").read_text(), AGENT)

    def test_refuses_name_taken_on_disabled_side(self):
        # the case a one-sided check gets wrong: re-enabling the twin later
        # would collide, so the collision has to be refused up front
        self.write("disabled/agents/reviewer.md", AGENT)
        with self.assertRaises(ValueError):
            items.item_create("agents", "reviewer", "new\n")
        self.assertFalse((self.tmp / "agents" / "reviewer.md").exists())

    def test_refuses_bad_name(self):
        with self.assertRaises(ValueError):
            items.item_create("agents", "../escape", "x\n")
        self.assertFalse((self.tmp.parent / "escape.md").exists())

    def test_refuses_empty_content(self):
        with self.assertRaises(ValueError):
            items.item_create("agents", "a", "   ")
        self.assertFalse((self.tmp / "agents" / "a.md").exists())


class Delete(Base):
    """The one call that destroys. Everything here is about what it must not
    reach: a symlink's target, or a directory Claude Code scans."""

    def test_deletes_a_markdown_item(self):
        self.write("agents/reviewer.md", AGENT)
        out = items.item_delete("agents", "reviewer")
        self.assertFalse((self.tmp / "agents" / "reviewer.md").exists())
        self.assertEqual(out["files"], 1)
        self.assertTrue((self.tmp / "agents").is_dir())

    def test_deletes_a_skill_directory_and_everything_in_it(self):
        self.write("skills/pdf/SKILL.md", "---\nname: pdf\n---\nx\n")
        self.write("skills/pdf/ref/notes.md", "notes\n")
        out = items.item_delete("skills", "pdf")
        self.assertFalse((self.tmp / "skills" / "pdf").exists())
        self.assertEqual(out["files"], 2)

    def test_deletes_a_disabled_item_from_the_parking_area(self):
        self.write("disabled/commands/old.md", "parked\n")
        items.item_delete("commands", "old", enabled=False)
        self.assertFalse((self.tmp / "disabled" / "commands" / "old.md").exists())

    def test_a_symlinked_skill_loses_the_link_not_the_target(self):
        real = self.tmp / "elsewhere" / "pdf"
        (real).mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: pdf\n---\nx\n")
        (self.tmp / "skills").mkdir()
        (self.tmp / "skills" / "pdf").symlink_to(real)
        items.item_delete("skills", "pdf")
        self.assertFalse((self.tmp / "skills" / "pdf").exists())
        self.assertTrue((real / "SKILL.md").is_file())

    def test_an_empty_subdirectory_goes_but_the_type_root_stays(self):
        self.write("commands/git/pr.md", "x\n")
        items.item_delete("commands", "git/pr")
        self.assertFalse((self.tmp / "commands" / "git").exists())
        self.assertTrue((self.tmp / "commands").is_dir())

    def test_a_sibling_keeps_the_subdirectory(self):
        self.write("commands/git/pr.md", "x\n")
        self.write("commands/git/log.md", "x\n")
        items.item_delete("commands", "git/pr")
        self.assertTrue((self.tmp / "commands" / "git" / "log.md").is_file())

    def test_refuses_a_name_that_is_not_there(self):
        with self.assertRaises(ValueError):
            items.item_delete("agents", "nope")

    def test_refuses_a_traversing_name_and_an_unknown_type(self):
        victim = self.write("victim.md", "keep me\n")
        for type_, name in (("agents", "../victim"), ("agents", "/etc/passwd"),
                            ("nope", "reviewer")):
            with self.subTest(type=type_, name=name):
                with self.assertRaises(ValueError):
                    items.item_delete(type_, name)
        self.assertTrue(victim.is_file())


class SkillFlags(Base):

    def test_no_model_invoke_flag(self):
        self.write("skills/private/SKILL.md",
                   "---\nname: private\ndisable-model-invocation: true\n---\nx\n")
        self.write("skills/open/SKILL.md", "---\nname: open\n---\nx\n")
        flags = {s["name"]: s["no_model_invoke"]
                 for s in items.scan_items("skills")}
        self.assertTrue(flags["private"])
        self.assertFalse(flags["open"])


if __name__ == "__main__":
    unittest.main()
