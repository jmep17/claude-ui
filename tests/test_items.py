"""Creating, copying and deleting item files, in both scopes.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_items.py`.

config_dir is patched in both namespaces that reach the filesystem here: items
(item_root builds the live path from it) and core (disabled_dir resolves it at
call time to find the parking area).

The Scope and Copy classes run the same operations against a project's own
.claude/ instead. One implementation serves both, so what they pin is that the
scope argument reaches the bottom, that nothing lands in the config dir by
accident, and that the tidy-up which follows a move stops at the project rather
than climbing into the repo.
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


class SkillFactsShape(Base):
    """skill_facts() is the one reader of a skill directory — items.py scans
    with it and plugins.py decorates its result. Both record shapes used to be
    written out by hand in two places, and drifted; this pins the key set so a
    field added for one consumer cannot quietly go missing for the other."""

    KEYS = {"name", "description", "path", "symlink", "broken", "incomplete",
            "mtime", "chars", "todo", "todo_line", "source", "name_mismatch",
            "long_desc", "no_model_invoke"}

    def test_facts_keys(self):
        self.write("skills/pdf/SKILL.md", "---\ndescription: d\n---\nhow\n")
        facts = items.skill_facts(self.tmp / "skills" / "pdf")
        self.assertEqual(set(facts), self.KEYS)
        # deliberately absent: nothing about where the directory was found
        self.assertNotIn("enabled", facts)

    def test_scan_adds_only_enabled(self):
        self.write("skills/pdf/SKILL.md", "---\ndescription: d\n---\nhow\n")
        (it,) = items.scan_items("skills")
        self.assertEqual(set(it), self.KEYS | {"enabled"})

    def test_carries_every_key_its_consumers_read(self):
        # context.py weighs items, doctor.py flags them, catalog.py indexes
        # them — each reads these off a scan_items record by name
        self.write("skills/pdf/SKILL.md", "---\ndescription: d\n---\nhow\n")
        (it,) = items.scan_items("skills")
        for k in ("name", "description", "path", "enabled", "chars", "broken",
                  "incomplete", "todo", "todo_line", "long_desc",
                  "name_mismatch"):
            self.assertIn(k, it)

    def test_skill_dirs_takes_dirs_and_links_not_files(self):
        self.write("skills/pdf/SKILL.md", "x")
        self.write("skills/.hidden/SKILL.md", "x")
        self.write("skills/loose.md", "x")
        (self.tmp / "skills" / "link").symlink_to(self.tmp / "skills" / "pdf")
        self.assertEqual([e.name for e in items.skill_dirs(self.tmp / "skills")],
                         ["link", "pdf"])

    def test_skill_dirs_on_a_missing_root(self):
        self.assertEqual(items.skill_dirs(self.tmp / "nope"), [])


class Chars(Base):
    """Every scanned item reports its file size in chars — the Context tab
    weighs items with it, so it must track the bytes actually on disk."""

    def test_chars_matches_written_length(self):
        body = "---\nname: reviewer\n---\n" + "x" * 100
        self.write("agents/reviewer.md", body)
        (it,) = items.scan_items("agents")
        self.assertEqual(it["chars"], len(body))

    def test_skill_counts_its_skill_md(self):
        body = "---\ndescription: d\n---\nhow\n"
        self.write("skills/pdf/SKILL.md", body)
        (it,) = items.scan_items("skills")
        self.assertEqual(it["chars"], len(body))

    def test_broken_symlink_is_zero(self):
        (self.tmp / "agents").mkdir()
        (self.tmp / "agents" / "ghost.md").symlink_to(self.tmp / "gone.md")
        (it,) = items.scan_items("agents")
        self.assertTrue(it["broken"])
        self.assertEqual(it["chars"], 0)


class MetaName(Base):
    """Claude Code selects an output style by its frontmatter name when set,
    so scan_items must surface it alongside the filename-derived name."""

    def test_meta_name_from_frontmatter(self):
        self.write("output-styles/adhd.md", "---\nname: ADHD\n---\nbody\n")
        self.write("output-styles/plain.md", "body only\n")
        got = {s["name"]: s["meta_name"]
               for s in items.scan_items("output-styles")}
        self.assertEqual(got["adhd"], "ADHD")
        self.assertEqual(got["plain"], "")


class ProjectBase(Base):
    """Base plus a project of its own, well outside the config dir.

    `self.out` is a second temp root — a project must not be a child of the
    stand-in ~/.claude, or a test could pass because a path was contained when
    the real arrangement has it nowhere near.
    """

    def setUp(self):
        super().setUp()
        self.outdir = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.outdir.name)
        self.cdir = self.out / "proj" / ".claude"
        self.cdir.mkdir(parents=True)

    def tearDown(self):
        self.outdir.cleanup()
        super().tearDown()


class Scope(ProjectBase):
    """Every item op again, this time against a project's own .claude/.

    One implementation serves both scopes, so what these pin is not that the
    logic works — Create and the rest already prove that — but that the scope
    argument reaches all the way down, that nothing lands in the config dir by
    accident, and that the prune which tidies empty directories stops at the
    project instead of eating it.
    """

    def test_create_lands_in_the_project_and_not_the_config_dir(self):
        items.item_create("agents", "reviewer", AGENT, scope=self.cdir)
        self.assertEqual((self.cdir / "agents" / "reviewer.md").read_text(), AGENT)
        self.assertFalse((self.tmp / "agents").exists())

    def test_skill_create_read_save_round_trip(self):
        items.item_create("skills", "pdf", "---\nname: pdf\n---\nhow to\n",
                          scope=self.cdir)
        got = items.item_read("skills", "pdf", None, True, self.cdir)
        self.assertEqual(got["file"], "SKILL.md")
        self.assertIn("how to", got["content"])
        items.item_save("skills", "pdf", "SKILL.md", "changed\n",
                        scope=self.cdir)
        self.assertEqual((self.cdir / "skills" / "pdf" / "SKILL.md").read_text(),
                         "changed\n")

    def test_scan_sees_only_this_scope(self):
        self.write("skills/mine/SKILL.md", "---\nname: mine\n---\nx\n")
        (self.cdir / "skills" / "theirs").mkdir(parents=True)
        (self.cdir / "skills" / "theirs" / "SKILL.md").write_text(
            "---\nname: theirs\ndescription: d\n---\nx\n")
        self.assertEqual([s["name"] for s in items.scan_items("skills")], ["mine"])
        rows = items.scan_items("skills", scope=self.cdir)
        self.assertEqual([s["name"] for s in rows], ["theirs"])
        self.assertEqual(rows[0]["description"], "d")

    def test_disable_parks_inside_the_project(self):
        items.item_create("commands", "ship", "body\n", scope=self.cdir)
        items.set_enabled("commands", "ship", False, self.cdir)
        self.assertTrue((self.cdir / "disabled" / "commands" / "ship.md").is_file())
        self.assertFalse((self.tmp / "disabled").exists())
        items.set_enabled("commands", "ship", True, self.cdir)
        self.assertTrue((self.cdir / "commands" / "ship.md").is_file())

    def test_disabled_row_is_scanned_with_enabled_false(self):
        items.item_create("commands", "ship", "body\n", scope=self.cdir)
        items.set_enabled("commands", "ship", False, self.cdir)
        rows = items.scan_items("commands", scope=self.cdir)
        self.assertEqual([(r["name"], r["enabled"]) for r in rows], [("ship", False)])

    def test_prune_tidies_the_parking_area_and_keeps_the_type_dir(self):
        """A round trip must leave no trace in disabled/, must keep commands/,
        and must not climb out of .claude/ — the default stop is the config
        dir, which under a project scope is the repo."""
        items.item_create("commands", "git/pr", "body\n", scope=self.cdir)
        items.set_enabled("commands", "git/pr", False, self.cdir)
        self.assertTrue((self.cdir / "commands").is_dir())
        items.set_enabled("commands", "git/pr", True, self.cdir)
        self.assertFalse((self.cdir / "disabled").exists())
        self.assertTrue((self.cdir / "commands" / "git" / "pr.md").is_file())
        self.assertTrue(self.cdir.is_dir())
        self.assertTrue(self.cdir.parent.is_dir())

    def test_delete_keeps_the_type_dir_and_the_project(self):
        items.item_create("commands", "ship", "body\n", scope=self.cdir)
        items.item_delete("commands", "ship", True, self.cdir)
        self.assertFalse((self.cdir / "commands" / "ship.md").exists())
        self.assertTrue((self.cdir / "commands").is_dir())
        self.assertTrue(self.cdir.is_dir())

    def test_bad_name_cannot_escape_the_project(self):
        for name in ("../escape", "a/../../b", ".ssh/key"):
            with self.assertRaises(ValueError):
                items.item_create("commands", name, "x\n", scope=self.cdir)
        self.assertEqual(list(self.out.glob("escape*")), [])

    def test_an_absolute_name_is_confined_not_obeyed(self):
        """item_rel() drops the leading slash rather than refusing, so the
        write lands under the project's own commands/ — the same answer the
        config dir has always given, checked here because the blast radius of
        getting it wrong is now someone's repo."""
        items.item_create("commands", "/etc/passwd", "x\n", scope=self.cdir)
        self.assertTrue((self.cdir / "commands" / "etc" / "passwd.md").is_file())

    def test_agent_model_set_is_scoped(self):
        items.item_create("agents", "reviewer", AGENT, scope=self.cdir)
        items.item_set_model("reviewer", "haiku", True, self.cdir)
        self.assertIn("model: haiku",
                      (self.cdir / "agents" / "reviewer.md").read_text())


class Copy(ProjectBase):
    """item_copy between the config dir and a project."""

    def test_copies_a_command_into_a_project(self):
        self.write("commands/ship.md", "body\n")
        out = items.item_copy("commands", "ship", None, self.cdir)
        self.assertEqual((self.cdir / "commands" / "ship.md").read_text(), "body\n")
        self.assertTrue((self.tmp / "commands" / "ship.md").is_file())  # not a move
        self.assertIn("ship", out["path"])

    def test_copies_a_skill_tree_back_out_to_the_config_dir(self):
        (self.cdir / "skills" / "pdf" / "ref").mkdir(parents=True)
        (self.cdir / "skills" / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n")
        (self.cdir / "skills" / "pdf" / "ref" / "notes.md").write_text("notes\n")
        items.item_copy("skills", "pdf", self.cdir, None)
        self.assertEqual((self.tmp / "skills" / "pdf" / "ref" / "notes.md").read_text(),
                         "notes\n")

    def test_a_symlinked_skill_copies_its_contents_not_the_link(self):
        real = self.out / "checkout" / "pdf"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: pdf\n---\nreal\n")
        (self.tmp / "skills").mkdir()
        (self.tmp / "skills" / "pdf").symlink_to(real)
        items.item_copy("skills", "pdf", None, self.cdir)
        dst = self.cdir / "skills" / "pdf"
        self.assertFalse(dst.is_symlink())
        self.assertEqual((dst / "SKILL.md").read_text(), "---\nname: pdf\n---\nreal\n")
        # the checkout it pointed at is untouched
        self.assertTrue((real / "SKILL.md").is_file())

    def test_refuses_a_name_taken_on_either_side_of_the_destination(self):
        self.write("commands/ship.md", "mine\n")
        (self.cdir / "disabled" / "commands").mkdir(parents=True)
        (self.cdir / "disabled" / "commands" / "ship.md").write_text("theirs\n")
        with self.assertRaises(ValueError):
            items.item_copy("commands", "ship", None, self.cdir)
        self.assertFalse((self.cdir / "commands").exists())

    def test_refuses_a_missing_source_and_a_same_place_copy(self):
        with self.assertRaises(ValueError):
            items.item_copy("commands", "nope", None, self.cdir)
        with self.assertRaises(ValueError):
            items.item_copy("commands", "ship", self.cdir, self.cdir)

    def test_leaves_no_temp_directory_behind(self):
        (self.tmp / "skills" / "pdf").mkdir(parents=True)
        (self.tmp / "skills" / "pdf" / "SKILL.md").write_text("x\n")
        items.item_copy("skills", "pdf", None, self.cdir)
        self.assertEqual([p.name for p in (self.cdir / "skills").iterdir()], ["pdf"])


class Move(ProjectBase):
    """item_move between the config dir and a project: copy, then delete."""

    def test_moves_a_command_into_a_project(self):
        self.write("commands/ship.md", "body\n")
        out = items.item_move("commands", "ship", None, self.cdir)
        self.assertEqual((self.cdir / "commands" / "ship.md").read_text(), "body\n")
        # the type root stays (delete's rule); the item itself is gone
        self.assertFalse((self.tmp / "commands" / "ship.md").exists())
        self.assertTrue(out["moved"])

    def test_moves_a_skill_tree_back_out_to_the_config_dir(self):
        (self.cdir / "skills" / "pdf" / "ref").mkdir(parents=True)
        (self.cdir / "skills" / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n")
        (self.cdir / "skills" / "pdf" / "ref" / "notes.md").write_text("notes\n")
        items.item_move("skills", "pdf", self.cdir, None)
        self.assertEqual((self.tmp / "skills" / "pdf" / "ref" / "notes.md").read_text(),
                         "notes\n")
        self.assertFalse((self.cdir / "skills" / "pdf").exists())

    def test_a_disabled_item_moves_and_stays_disabled(self):
        self.write("disabled/commands/ship.md", "parked\n")
        items.item_move("commands", "ship", None, self.cdir, enabled=False)
        self.assertEqual((self.cdir / "disabled" / "commands" / "ship.md").read_text(),
                         "parked\n")
        self.assertFalse((self.tmp / "disabled" / "commands" / "ship.md").exists())

    def test_a_collision_refuses_and_leaves_the_source_alone(self):
        self.write("commands/ship.md", "mine\n")
        (self.cdir / "disabled" / "commands").mkdir(parents=True)
        (self.cdir / "disabled" / "commands" / "ship.md").write_text("theirs\n")
        with self.assertRaises(ValueError):
            items.item_move("commands", "ship", None, self.cdir)
        self.assertEqual((self.tmp / "commands" / "ship.md").read_text(), "mine\n")
        self.assertFalse((self.cdir / "commands").exists())

    def test_a_symlinked_skill_moves_its_contents_and_only_unlinks(self):
        real = self.out / "checkout" / "pdf"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: pdf\n---\nreal\n")
        (self.tmp / "skills").mkdir()
        (self.tmp / "skills" / "pdf").symlink_to(real)
        items.item_move("skills", "pdf", None, self.cdir)
        dst = self.cdir / "skills" / "pdf"
        self.assertFalse(dst.is_symlink())
        self.assertEqual((dst / "SKILL.md").read_text(), "---\nname: pdf\n---\nreal\n")
        # the link is gone; the checkout it pointed at is untouched
        self.assertFalse((self.tmp / "skills" / "pdf").is_symlink())
        self.assertTrue((real / "SKILL.md").is_file())

    def test_refuses_a_missing_source_and_a_same_place_move(self):
        with self.assertRaises(ValueError):
            items.item_move("commands", "nope", None, self.cdir)
        with self.assertRaises(ValueError):
            items.item_move("commands", "ship", self.cdir, self.cdir)


if __name__ == "__main__":
    unittest.main()
