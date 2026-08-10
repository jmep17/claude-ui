"""The Projects tab backend: registry, prompt-file state, wrappers, and the
resolve_editable extension that lets the editor into <project>/.claude/.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from
the repo root, or just `python3 tests/test_projects.py`.

The ResolveEditable and ItemScope cases are the security battery: registering a
project must open exactly its real .claude/ subtree, the handful of files
Claude Code documents beside it, and nothing else — not the rest of the
project, not a symlink target outside it, and not anything reached through a
.claude that is itself a symlink. The template cases pin the other load-bearing
property: generated shell code never executes or sources repo content."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import core, items, projects  # noqa: E402


class Base(unittest.TestCase):
    """Tempdir config dir + a tempdir project, patched into core."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.cfg = base / "config"
        self.cfg.mkdir()
        self.proj = base / "proj"
        (self.proj / ".claude").mkdir(parents=True)
        self._config_dir = core.config_dir
        core.config_dir = lambda: self.cfg
        # projects.py binds the name at import time via `from .core import`?
        # No — it imports the function itself; patch both to be explicit.
        self._p_config_dir = projects.config_dir
        projects.config_dir = core.config_dir
        # items.py reaches the filesystem too, now that project_state scans a
        # project's own .claude/ through it. Every scan here passes an explicit
        # scope, so config_dir is not consulted — patched anyway, because a
        # module that can read the developer's real ~/.claude is exactly how a
        # suite starts passing for the wrong reason (see test_editor.py).
        self._i_config_dir = items.config_dir
        items.config_dir = core.config_dir

    def tearDown(self):
        core.config_dir = self._config_dir
        projects.config_dir = self._p_config_dir
        items.config_dir = self._i_config_dir
        self.tmp.cleanup()

    def cdir(self):
        return self.proj / ".claude"

    def add(self):
        return projects.registry_add(str(self.proj))


class Registry(Base):
    """claude-ui-projects.txt round-trips and validation."""

    def test_add_stores_resolved_and_round_trips(self):
        self.add()
        roots = core.project_roots()
        self.assertEqual(roots, [self.proj.resolve()])
        text = projects.registry_path().read_text()
        self.assertTrue(text.startswith("#"))
        self.assertTrue(text.endswith("\n"))

    def test_add_is_idempotent(self):
        self.add()
        self.add()
        self.assertEqual(len(core.project_roots()), 1)

    def test_add_rejects_relative_missing_file_and_root(self):
        for bad in ("", "relative/path", str(self.proj / "nope")):
            with self.assertRaises(ValueError):
                projects.registry_add(bad)
        f = self.proj / "afile"
        f.write_text("x")
        with self.assertRaises(ValueError):
            projects.registry_add(str(f))
        with self.assertRaises(ValueError):
            projects.registry_add("/")

    def test_add_resolves_symlinks(self):
        link = pathlib.Path(self.tmp.name) / "link"
        link.symlink_to(self.proj)
        projects.registry_add(str(link))
        self.assertEqual(core.project_roots(), [self.proj.resolve()])

    def test_comments_and_blanks_are_skipped(self):
        projects.registry_path().write_text(
            f"# comment\n\n{self.proj}\n  \n# another\n")
        self.assertEqual(core.project_roots(), [self.proj])

    def test_remove_drops_exactly_one_and_unknown_raises(self):
        other = pathlib.Path(self.tmp.name) / "other"
        other.mkdir()
        self.add()
        projects.registry_add(str(other))
        projects.registry_remove(str(self.proj))
        self.assertEqual(core.project_roots(), [other.resolve()])
        with self.assertRaises(ValueError):
            projects.registry_remove(str(self.proj))

    def test_missing_registry_means_no_roots(self):
        self.assertEqual(core.project_roots(), [])


class State(Base):
    """project_state derives everything by inspection."""

    def st(self):
        return projects.project_state(self.proj)

    def test_bare_project(self):
        st = self.st()
        self.assertFalse(st["missing"])
        self.assertTrue(st["has_claude_dir"])
        self.assertIsNone(st["mode"])
        self.assertFalse(st["enabled"])
        self.assertFalse(st["conflict"])
        self.assertEqual(st["wrapper"], "none")

    def test_replace_live(self):
        (self.cdir() / projects.REPLACE_MD).write_text("x")
        st = self.st()
        self.assertEqual(st["mode"], "replace")
        self.assertTrue(st["enabled"])

    def test_append_off_only(self):
        (self.cdir() / (projects.APPEND_MD + ".off")).write_text("x")
        st = self.st()
        self.assertEqual(st["mode"], "append")
        self.assertFalse(st["enabled"])
        self.assertFalse(st["conflict"])

    def test_both_live_is_conflict_replace_wins(self):
        (self.cdir() / projects.REPLACE_MD).write_text("x")
        (self.cdir() / projects.APPEND_MD).write_text("x")
        st = self.st()
        self.assertTrue(st["conflict"])
        self.assertEqual(st["mode"], "replace")

    def test_live_plus_off_same_mode_is_conflict(self):
        (self.cdir() / projects.REPLACE_MD).write_text("x")
        (self.cdir() / (projects.REPLACE_MD + ".off")).write_text("x")
        self.assertTrue(self.st()["conflict"])

    def test_missing_root_never_raises(self):
        gone = pathlib.Path(self.tmp.name) / "gone"
        st = projects.project_state(gone)
        self.assertTrue(st["missing"])
        self.assertEqual(st["wrapper"], "none")

    def test_output_styles_and_setting_are_reported(self):
        sd = self.cdir() / "output-styles"
        sd.mkdir()
        (sd / "focus.md").write_text("x")
        (self.cdir() / "settings.json").write_text('{"outputStyle": "focus"}')
        (self.cdir() / "settings.local.json").write_text('{"outputStyle": "other"}')
        st = self.st()
        self.assertEqual(st["output_styles"], ["focus.md"])
        self.assertEqual(st["output_style_setting"],
                         {"settings.json": "focus",
                          "settings.local.json": "other"})


class Wrapper(Base):
    """claude.sh generation, staleness, and the foreign-file refusal."""

    def test_write_sets_exec_and_reports_current(self):
        self.add()
        projects.wrapper_write(self.proj)
        p = self.cdir() / projects.WRAPPER_NAME
        self.assertTrue(os.access(p, os.X_OK))
        self.assertEqual(p.read_text(), projects.WRAPPER_SCRIPT)
        self.assertEqual(projects.project_state(self.proj)["wrapper"], "current")

    def test_not_executable_and_stale_and_foreign(self):
        self.add()
        projects.wrapper_write(self.proj)
        p = self.cdir() / projects.WRAPPER_NAME
        p.chmod(0o644)
        self.assertEqual(projects.project_state(self.proj)["wrapper"],
                         "not-executable")
        p.write_text(projects.WRAPPER_SCRIPT + "# tweak\n")
        self.assertEqual(projects.project_state(self.proj)["wrapper"], "stale")
        p.write_text("#!/bin/sh\nexec claude \"$@\"\n")
        self.assertEqual(projects.project_state(self.proj)["wrapper"], "foreign")

    def test_foreign_refused_without_force(self):
        self.add()
        p = self.cdir() / projects.WRAPPER_NAME
        p.write_text("#!/bin/sh\necho mine\n")
        with self.assertRaises(ValueError):
            projects.wrapper_write(self.proj)
        projects.wrapper_write(self.proj, force=True)
        self.assertEqual(p.read_text(), projects.WRAPPER_SCRIPT)

    def test_ops_require_registration(self):
        for op in (lambda: projects.wrapper_write(self.proj),
                   lambda: projects.project_init(self.proj, "replace"),
                   lambda: projects.project_toggle(self.proj, True),
                   lambda: projects.project_set_mode(self.proj, "append")):
            with self.assertRaises(ValueError):
                op()

    def test_wrapper_syntax_checks_with_sh(self):
        self.add()
        projects.wrapper_write(self.proj)
        r = subprocess.run(["sh", "-n", str(self.cdir() / projects.WRAPPER_NAME)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class Templates(Base):
    """The generated shell code's load-bearing properties, pinned."""

    def test_wrapper_essentials(self):
        s = projects.WRAPPER_SCRIPT
        for needle in ("#!/bin/sh", "CDPATH=", "pwd -P",
                       "--system-prompt-file", "--append-system-prompt-file",
                       'exec claude "$@"', projects.WRAPPER_MARKER):
            self.assertIn(needle, s)

    def test_zsh_function_essentials(self):
        s = projects.zsh_function_text()
        for needle in ("grep -Fxq --", "pwd -P", "command claude",
                       str(projects.registry_path()), projects.ZSH_MARKER):
            self.assertIn(needle, s)

    def test_generated_code_never_executes_repo_content(self):
        # prompt files are data: no source/eval/dot-execution anywhere
        for s in (projects.WRAPPER_SCRIPT, projects.zsh_function_text()):
            for forbidden in ("source \"$dir", ". \"$dir", "eval"):
                self.assertNotIn(forbidden, s)


class Ops(Base):
    """init / toggle / mode-switch: renames that never clobber user data."""

    def setUp(self):
        super().setUp()
        self.add()

    def live(self, mode):
        return self.cdir() / projects.MODES[mode]

    def off(self, mode):
        return self.cdir() / (projects.MODES[mode] + projects.OFF_SUFFIX)

    def test_init_writes_starter_and_wrapper(self):
        projects.project_init(self.proj, "append")
        self.assertEqual(self.live("append").read_text(),
                         projects.STARTERS["append"])
        self.assertEqual(projects.project_state(self.proj)["wrapper"], "current")

    def test_init_is_idempotent_and_respects_off_files(self):
        self.live("append").write_text("mine")
        projects.project_init(self.proj, "append")
        self.assertEqual(self.live("append").read_text(), "mine")
        self.live("append").rename(self.off("append"))
        projects.project_init(self.proj, "append")
        self.assertFalse(self.live("append").exists())
        self.assertEqual(self.off("append").read_text(), "mine")

    def test_init_rejects_bad_mode(self):
        with self.assertRaises(ValueError):
            projects.project_init(self.proj, "both")

    def test_toggle_round_trips_content(self):
        self.live("replace").write_text("keep me intact")
        projects.project_toggle(self.proj, False)
        self.assertFalse(self.live("replace").exists())
        self.assertEqual(self.off("replace").read_text(), "keep me intact")
        projects.project_toggle(self.proj, True)
        self.assertEqual(self.live("replace").read_text(), "keep me intact")

    def test_toggle_with_nothing_raises(self):
        with self.assertRaises(ValueError):
            projects.project_toggle(self.proj, True)

    def test_toggle_refuses_conflict(self):
        self.live("replace").write_text("a")
        self.off("replace").write_text("b")
        with self.assertRaises(ValueError):
            projects.project_toggle(self.proj, False)
        self.assertEqual(self.live("replace").read_text(), "a")
        self.assertEqual(self.off("replace").read_text(), "b")

    def test_mode_switch_offs_the_other_and_revives_or_creates(self):
        self.live("replace").write_text("r")
        projects.project_set_mode(self.proj, "append")
        self.assertEqual(self.off("replace").read_text(), "r")
        self.assertEqual(self.live("append").read_text(),
                         projects.STARTERS["append"])
        self.off("replace").rename(self.off("replace"))  # still parked
        projects.project_set_mode(self.proj, "replace")
        self.assertEqual(self.live("replace").read_text(), "r")
        self.assertEqual(self.off("append").read_text(),
                         projects.STARTERS["append"])

    def test_mode_switch_refuses_when_pair_conflicts(self):
        self.live("append").write_text("a")
        self.off("append").write_text("b")
        with self.assertRaises(ValueError):
            projects.project_set_mode(self.proj, "replace")


class ResolveEditable(Base):
    """The security battery: exactly the real .claude/ subtree, nothing else."""

    def ok(self, path):
        return core.resolve_editable(str(path))

    def test_registered_claude_subtree_is_editable(self):
        self.add()
        p, readonly = self.ok(self.cdir() / "system-prompt.md")
        self.assertFalse(readonly)
        self.assertEqual(p, (self.cdir() / "system-prompt.md").resolve())

    def test_project_outside_claude_dir_is_rejected(self):
        self.add()
        (self.proj / "src").mkdir()
        with self.assertRaises(ValueError):
            self.ok(self.proj / "src" / "x.py")
        with self.assertRaises(ValueError):
            self.ok(self.proj / "README.md")

    def test_unregistered_project_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ok(self.cdir() / "system-prompt.md")

    def test_dotdot_escape_is_rejected(self):
        self.add()
        (self.proj / "secret").write_text("x")
        with self.assertRaises(ValueError):
            self.ok(f"{self.cdir()}/../secret")

    def test_symlink_inside_claude_pointing_out_is_rejected(self):
        self.add()
        outside = pathlib.Path(self.tmp.name) / "outside.md"
        outside.write_text("x")
        (self.cdir() / "evil.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.ok(self.cdir() / "evil.md")

    def test_claude_dir_itself_a_symlink_is_rejected(self):
        evil = pathlib.Path(self.tmp.name) / "evil-proj"
        evil.mkdir()
        target = pathlib.Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        (target / "x.md").write_text("x")
        (evil / ".claude").symlink_to(target)
        projects.registry_add(str(evil))
        with self.assertRaises(ValueError):
            self.ok(evil / ".claude" / "x.md")

    def test_project_root_files_are_editable(self):
        self.add()
        for name in core.PROJECT_ROOT_FILES:
            with self.subTest(name=name):
                p, readonly = self.ok(self.proj / name)
                self.assertFalse(readonly)
                self.assertEqual(p, (self.proj / name).resolve())

    def test_a_symlinked_project_root_file_is_rejected(self):
        """The equality is against the resolved path, so a CLAUDE.md pointing
        anywhere else stops matching and falls through to the refusal — the
        containment test's protection, without a branch of its own."""
        self.add()
        outside = pathlib.Path(self.tmp.name) / "outside.md"
        outside.write_text("x")
        (self.proj / "CLAUDE.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.ok(self.proj / "CLAUDE.md")

    def test_a_neighbouring_file_is_still_rejected(self):
        self.add()
        for name in ("CLAUDE.md.bak", "package.json", ".mcp.json.orig"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.ok(self.proj / name)

    def test_config_dir_and_claude_json_behavior_unchanged(self):
        (self.cfg / "CLAUDE.md").write_text("x")
        p, readonly = self.ok(self.cfg / "CLAUDE.md")
        self.assertFalse(readonly)
        p, readonly = self.ok(core.CLAUDE_JSON)
        self.assertFalse(readonly)
        plug = self.cfg / "plugins" / "x" / "SKILL.md"
        plug.parent.mkdir(parents=True)
        plug.write_text("x")
        p, readonly = self.ok(plug)
        self.assertTrue(readonly)


class ItemScope(Base):
    """The gate between a request naming a project and an item op writing one.

    Everything the Projects tab does to a project's skills, commands, agents
    and output styles resolves its directory here first, so these are the
    cases that decide whether an endpoint can be pointed at somewhere it was
    never invited: the refusals belong to this one function, not to each of
    the eight operations behind it.
    """

    def test_no_root_means_the_config_dir(self):
        self.assertIsNone(items.item_scope(None))
        self.assertIsNone(items.item_scope(""))

    def test_registered_root_gives_its_claude_dir(self):
        self.add()
        self.assertEqual(items.item_scope(str(self.proj)), self.cdir().resolve())

    def test_unregistered_root_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            items.item_scope(str(self.proj))
        self.assertIn("not a registered project", str(cm.exception))

    def test_unregistering_closes_the_door_again(self):
        self.add()
        items.item_scope(str(self.proj))
        projects.registry_remove(str(self.proj))
        with self.assertRaises(ValueError):
            items.item_scope(str(self.proj))

    def test_a_claude_dir_that_is_a_symlink_is_refused(self):
        evil = pathlib.Path(self.tmp.name) / "evil-proj"
        evil.mkdir()
        target = pathlib.Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        (evil / ".claude").symlink_to(target)
        projects.registry_add(str(evil))
        with self.assertRaises(ValueError):
            items.item_scope(str(evil))
        # and nothing reached the directory it pointed at
        self.assertEqual(list(target.iterdir()), [])

    def test_a_sibling_of_a_registered_root_is_refused(self):
        self.add()
        sibling = self.proj.parent / "proj-evil"
        (sibling / ".claude").mkdir(parents=True)
        for raw in (str(sibling), str(self.proj) + "-evil",
                    str(self.proj / ".." / "proj-evil")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    items.item_scope(raw)

    def test_a_create_through_the_gate_lands_in_the_project(self):
        self.add()
        items.item_create("commands", "ship", "body\n",
                          scope=items.item_scope(str(self.proj)))
        self.assertEqual((self.cdir() / "commands" / "ship.md").read_text(),
                         "body\n")
        self.assertFalse((self.cfg / "commands").exists())


class ProjectStateItems(Base):
    """What the card reads: full rows, per scope, never raising."""

    def test_items_are_scanned_rows_for_every_managed_type(self):
        self.add()
        (self.cdir() / "skills" / "pdf").mkdir(parents=True)
        (self.cdir() / "skills" / "pdf" / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: reads pdfs\n---\nx\n")
        st = projects.project_state(self.proj)
        self.assertEqual(sorted(st["items"]), sorted(core.PROJECT_MANAGED_TYPES))
        self.assertEqual(st["items"]["skills"][0]["description"], "reads pdfs")
        self.assertTrue(st["items"]["skills"][0]["enabled"])
        self.assertEqual(st["items"]["commands"], [])

    def test_the_disabled_side_shows_up_too(self):
        self.add()
        (self.cdir() / "disabled" / "commands").mkdir(parents=True)
        (self.cdir() / "disabled" / "commands" / "old.md").write_text("x\n")
        rows = projects.project_state(self.proj)["items"]["commands"]
        self.assertEqual([(r["name"], r["enabled"]) for r in rows], [("old", False)])

    def test_a_project_that_vanished_still_reports(self):
        st = projects.project_state(self.proj.parent / "gone")
        self.assertTrue(st["missing"])
        self.assertEqual(st["items"]["skills"], [])

    def test_two_projects_do_not_see_each_others_items(self):
        other = self.proj.parent / "other"
        (other / ".claude" / "commands").mkdir(parents=True)
        (other / ".claude" / "commands" / "theirs.md").write_text("x\n")
        (self.cdir() / "commands").mkdir(parents=True)
        (self.cdir() / "commands" / "mine.md").write_text("x\n")
        self.assertEqual(
            [r["name"] for r in projects.project_state(self.proj)["items"]["commands"]],
            ["mine"])
        self.assertEqual(
            [r["name"] for r in projects.project_state(other)["items"]["commands"]],
            ["theirs"])


class ProjectSettings(Base):
    """The second, deliberately small write path into settings files."""

    def test_writes_local_by_default_and_keeps_the_other_keys(self):
        self.add()
        (self.cdir() / "settings.local.json").write_text(
            '{"model": "opus", "outputStyle": "old"}')
        projects.project_setting_set(self.proj, "outputStyle", "new")
        data = json.loads((self.cdir() / "settings.local.json").read_text())
        self.assertEqual(data, {"model": "opus", "outputStyle": "new"})
        self.assertFalse((self.cdir() / "settings.json").exists())

    def test_none_removes_the_key_rather_than_storing_null(self):
        self.add()
        projects.project_setting_set(self.proj, "outputStyle", "x")
        projects.project_setting_set(self.proj, "outputStyle", None)
        data = json.loads((self.cdir() / "settings.local.json").read_text())
        self.assertEqual(data, {})

    def test_shared_file_when_asked(self):
        self.add()
        projects.project_setting_set(self.proj, "outputStyle", "x", local=False)
        self.assertTrue((self.cdir() / "settings.json").is_file())
        self.assertFalse((self.cdir() / "settings.local.json").exists())

    def test_refuses_a_key_outside_the_allowlist(self):
        self.add()
        for key in ("permissions", "hooks", "env", ""):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    projects.project_setting_set(self.proj, key, {"x": 1})
        self.assertFalse((self.cdir() / "settings.local.json").exists())

    def test_requires_registration(self):
        with self.assertRaises(ValueError):
            projects.project_setting_set(self.proj, "outputStyle", "x")

    def test_refuses_a_settings_file_that_does_not_parse(self):
        self.add()
        (self.cdir() / "settings.local.json").write_text("{not json")
        with self.assertRaises(ValueError):
            projects.project_setting_set(self.proj, "outputStyle", "x")
        self.assertEqual((self.cdir() / "settings.local.json").read_text(),
                         "{not json")


class SkillOverrides(Base):
    """Claude Code's own per-skill switch, applied to a project.

    The reason this exists beside the file move: a skill committed by someone
    else must be switchable off without the repo showing a deletion. The docs
    name settings.local.json for exactly that case, so that is where it goes
    and the shared file stays untouched.
    """

    def setUp(self):
        super().setUp()
        self.add()

    def local(self):
        return json.loads((self.cdir() / "settings.local.json").read_text())

    def test_off_lands_in_settings_local_and_the_file_is_untouched(self):
        (self.cdir() / "skills" / "shared").mkdir(parents=True)
        skill = self.cdir() / "skills" / "shared" / "SKILL.md"
        skill.write_text("---\nname: shared\n---\nx\n")
        projects.project_skill_override(self.proj, "shared", "off")
        self.assertEqual(self.local()["skillOverrides"], {"shared": "off"})
        self.assertTrue(skill.is_file())
        self.assertFalse((self.cdir() / "disabled").exists())
        self.assertFalse((self.cdir() / "settings.json").exists())

    def test_clearing_removes_the_entry_and_then_the_key(self):
        projects.project_skill_override(self.proj, "a", "off")
        projects.project_skill_override(self.proj, "b", "name-only")
        projects.project_skill_override(self.proj, "a", None)
        self.assertEqual(self.local()["skillOverrides"], {"b": "name-only"})
        projects.project_skill_override(self.proj, "b", None)
        self.assertNotIn("skillOverrides", self.local())

    def test_other_entries_and_other_keys_survive(self):
        (self.cdir() / "settings.local.json").write_text(
            '{"model": "opus", "skillOverrides": {"keep": "off"}}')
        projects.project_skill_override(self.proj, "new", "off")
        d = self.local()
        self.assertEqual(d["skillOverrides"], {"keep": "off", "new": "off"})
        self.assertEqual(d["model"], "opus")

    def test_only_the_four_documented_states(self):
        for bad in ("disabled", "hidden", "OFF", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    projects.project_skill_override(self.proj, "a", bad)
        self.assertFalse((self.cdir() / "settings.local.json").exists())

    def test_bad_names_and_unregistered_roots_are_refused(self):
        with self.assertRaises(ValueError):
            projects.project_skill_override(self.proj, "../evil", "off")
        with self.assertRaises(ValueError):
            projects.project_skill_override(self.proj.parent / "nope", "a", "off")

    def test_state_reports_both_files_with_local_winning(self):
        (self.cdir() / "settings.json").write_text(
            '{"skillOverrides": {"a": "off", "b": "off"}}')
        (self.cdir() / "settings.local.json").write_text(
            '{"skillOverrides": {"b": "on"}}')
        st = projects.project_state(self.proj)
        self.assertEqual(st["skill_overrides"], {"a": "off", "b": "on"})

    def test_no_overrides_is_an_empty_map_not_a_missing_key(self):
        self.assertEqual(projects.project_state(self.proj)["skill_overrides"], {})


class ProjectMcp(Base):
    """<project>/.mcp.json — the servers a repo ships to whoever clones it."""

    SERVER = {"command": "npx", "args": ["-y", "server"]}

    def mcpfile(self):
        return self.proj / ".mcp.json"

    def test_add_edit_and_remove_one_server(self):
        self.add()
        projects.project_mcp_set(self.proj, "docs", self.SERVER)
        data = json.loads(self.mcpfile().read_text())
        self.assertEqual(data["mcpServers"]["docs"], self.SERVER)
        projects.project_mcp_set(self.proj, "docs", {"url": "https://x/mcp"})
        data = json.loads(self.mcpfile().read_text())
        self.assertEqual(data["mcpServers"]["docs"], {"url": "https://x/mcp"})
        projects.project_mcp_set(self.proj, "docs", None)
        self.assertFalse(self.mcpfile().exists())

    def test_removing_one_of_two_leaves_the_other(self):
        self.add()
        projects.project_mcp_set(self.proj, "a", self.SERVER)
        projects.project_mcp_set(self.proj, "b", self.SERVER)
        projects.project_mcp_set(self.proj, "a", None)
        data = json.loads(self.mcpfile().read_text())
        self.assertEqual(list(data["mcpServers"]), ["b"])

    def test_other_keys_in_the_file_survive_and_keep_it_alive(self):
        self.add()
        self.mcpfile().write_text('{"note": "ours", "mcpServers": {}}')
        projects.project_mcp_set(self.proj, "docs", self.SERVER)
        projects.project_mcp_set(self.proj, "docs", None)
        self.assertEqual(json.loads(self.mcpfile().read_text()), {"note": "ours"})

    def test_a_config_with_neither_command_nor_url_is_refused(self):
        self.add()
        with self.assertRaises(ValueError):
            projects.project_mcp_set(self.proj, "docs", {"args": []})
        self.assertFalse(self.mcpfile().exists())

    def test_bad_json_is_reported_and_never_overwritten(self):
        self.add()
        self.mcpfile().write_text("{not json")
        st = projects.project_mcp_state(self.proj)
        self.assertTrue(st["error"])
        self.assertEqual(st["servers"], [])
        with self.assertRaises(ValueError):
            projects.project_mcp_set(self.proj, "docs", self.SERVER)
        self.assertEqual(self.mcpfile().read_text(), "{not json")

    def test_a_symlinked_mcp_file_is_refused(self):
        self.add()
        outside = pathlib.Path(self.tmp.name) / "outside.json"
        outside.write_text("{}")
        self.mcpfile().symlink_to(outside)
        with self.assertRaises(ValueError):
            projects.project_mcp_set(self.proj, "docs", self.SERVER)
        self.assertEqual(outside.read_text(), "{}")
        # and the card reports it instead of taking the payload down
        self.assertIn("symlink", projects.project_state(self.proj)["mcp"]["error"])

    def test_requires_registration(self):
        with self.assertRaises(ValueError):
            projects.project_mcp_set(self.proj, "docs", self.SERVER)
        with self.assertRaises(ValueError):
            projects.project_mcp_state(self.proj)
        self.assertFalse(self.mcpfile().exists())

    def test_bad_server_names_are_refused(self):
        self.add()
        for name in ("", "../evil", "a b", ".hidden"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    projects.project_mcp_set(self.proj, name, self.SERVER)


class McpMove(Base):
    """mcp_move between the three scopes: user, project, local."""

    SERVER = {"command": "npx", "args": ["-y", "server"], "env": {"K": "v"}}

    def setUp(self):
        super().setUp()
        from claude_ui import mcp
        self.claude_json = pathlib.Path(self.tmp.name) / "claude.json"
        self._saved_json = [(m, m.CLAUDE_JSON) for m in (core, mcp, projects)]
        for m, _ in self._saved_json:
            m.CLAUDE_JSON = self.claude_json
        self.add()

    def tearDown(self):
        for m, p in self._saved_json:
            m.CLAUDE_JSON = p
        super().tearDown()

    def user_end(self):
        return {"scope": "user"}

    def end(self, scope):
        return {"scope": scope, "root": str(self.proj)}

    def cj(self):
        return json.loads(self.claude_json.read_text())

    def seed_user(self, name="docs"):
        self.claude_json.write_text(json.dumps({"mcpServers": {name: self.SERVER}}))

    def seed_local(self, name="docs", extra=None):
        entry = {"mcpServers": {name: self.SERVER}, **(extra or {})}
        self.claude_json.write_text(json.dumps(
            {"projects": {str(self.proj.resolve()): entry}}))

    def test_user_to_project_moves_verbatim_and_approves(self):
        self.seed_user()
        projects.mcp_move("docs", self.user_end(), self.end("project"))
        data = json.loads((self.proj / ".mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["docs"], self.SERVER)
        self.assertNotIn("mcpServers", self.cj())
        local = json.loads((self.cdir() / "settings.local.json").read_text())
        self.assertIn("docs", local["enabledMcpjsonServers"])

    def test_project_to_user_cleans_the_approval_lists(self):
        (self.proj / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"docs": self.SERVER}}))
        projects.project_mcp_approve(self.proj, "docs", True)
        projects.mcp_move("docs", self.end("project"), self.user_end())
        self.assertEqual(self.cj()["mcpServers"]["docs"], self.SERVER)
        self.assertFalse((self.proj / ".mcp.json").exists())  # emptied file goes
        local = json.loads((self.cdir() / "settings.local.json").read_text())
        self.assertNotIn("enabledMcpjsonServers", local)

    def test_user_and_local_swap_in_one_file(self):
        self.seed_user()
        projects.mcp_move("docs", self.user_end(), self.end("local"))
        data = self.cj()
        self.assertNotIn("mcpServers", data)
        key = str(self.proj.resolve())
        self.assertEqual(data["projects"][key]["mcpServers"]["docs"], self.SERVER)
        projects.mcp_move("docs", self.end("local"), self.user_end())
        data = self.cj()
        self.assertEqual(data["mcpServers"]["docs"], self.SERVER)
        # the emptied map goes, but the project entry itself stays: session
        # state lives in there
        self.assertIn(key, data["projects"])
        self.assertNotIn("mcpServers", data["projects"][key])

    def test_local_to_project_and_back(self):
        self.seed_local(extra={"history": ["kept"]})
        projects.mcp_move("docs", self.end("local"), self.end("project"))
        data = json.loads((self.proj / ".mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["docs"], self.SERVER)
        key = str(self.proj.resolve())
        self.assertEqual(self.cj()["projects"][key], {"history": ["kept"]})
        projects.mcp_move("docs", self.end("project"), self.end("local"))
        self.assertEqual(self.cj()["projects"][key]["mcpServers"]["docs"],
                         self.SERVER)
        self.assertFalse((self.proj / ".mcp.json").exists())

    def test_local_servers_show_in_project_mcp_state(self):
        self.seed_local()
        st = projects.project_mcp_state(self.proj)
        self.assertEqual(st["local_servers"],
                         [{"name": "docs", "config": self.SERVER}])

    def test_a_destination_collision_refuses_and_moves_nothing(self):
        self.seed_user()
        (self.proj / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"docs": {"url": "https://x/mcp"}}}))
        with self.assertRaises(ValueError):
            projects.mcp_move("docs", self.user_end(), self.end("project"))
        self.assertEqual(self.cj()["mcpServers"]["docs"], self.SERVER)
        data = json.loads((self.proj / ".mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["docs"], {"url": "https://x/mcp"})

    def test_a_parked_twin_at_user_scope_also_refuses(self):
        (self.proj / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"docs": self.SERVER}}))
        (self.cfg / "disabled").mkdir()
        (self.cfg / "disabled" / "mcp-servers.json").write_text(
            json.dumps({"mcpServers": {"docs": {"url": "https://x/mcp"}}}))
        with self.assertRaises(ValueError):
            projects.mcp_move("docs", self.end("project"), self.user_end())
        self.assertTrue((self.proj / ".mcp.json").exists())

    def test_a_config_with_neither_command_nor_url_is_refused(self):
        self.claude_json.write_text(json.dumps({"mcpServers": {"docs": {"args": []}}}))
        with self.assertRaises(ValueError) as cm:
            projects.mcp_move("docs", self.user_end(), self.end("project"))
        self.assertIn("~", str(cm.exception))  # names the source file

    def test_refusals_missing_same_place_bad_scope_unregistered(self):
        self.seed_user()
        with self.assertRaises(ValueError):
            projects.mcp_move("nope", self.user_end(), self.end("project"))
        with self.assertRaises(ValueError):
            projects.mcp_move("docs", self.user_end(), self.user_end())
        with self.assertRaises(ValueError):
            projects.mcp_move("docs", self.user_end(), {"scope": "global"})
        other = pathlib.Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        with self.assertRaises(ValueError):
            projects.mcp_move("docs", self.user_end(),
                              {"scope": "project", "root": str(other)})
        self.assertEqual(self.cj()["mcpServers"]["docs"], self.SERVER)

    def test_bad_json_in_claude_json_refuses_before_any_write(self):
        self.claude_json.write_text("{not json")
        (self.proj / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"docs": self.SERVER}}))
        with self.assertRaises(ValueError):
            projects.mcp_move("docs", self.end("project"), self.user_end())
        self.assertEqual(self.claude_json.read_text(), "{not json")
        self.assertTrue((self.proj / ".mcp.json").exists())


class McpApproval(Base):
    """Approved / rejected / undecided, and where the answer is recorded."""

    def setUp(self):
        super().setUp()
        self.add()
        projects.project_mcp_set(self.proj, "docs", {"command": "npx"})

    def approval(self):
        return projects.project_mcp_state(self.proj)["servers"][0]["approval"]

    def test_starts_undecided(self):
        self.assertEqual(self.approval(), "undecided")

    def test_approve_reject_and_clear_round_trip(self):
        projects.project_mcp_approve(self.proj, "docs", True)
        self.assertEqual(self.approval(), "approved")
        projects.project_mcp_approve(self.proj, "docs", False)
        self.assertEqual(self.approval(), "rejected")
        projects.project_mcp_approve(self.proj, "docs", None)
        self.assertEqual(self.approval(), "undecided")

    def test_the_answer_lands_in_settings_local_only(self):
        projects.project_mcp_approve(self.proj, "docs", True)
        local = json.loads((self.cdir() / "settings.local.json").read_text())
        self.assertEqual(local["enabledMcpjsonServers"], ["docs"])
        self.assertNotIn("disabledMcpjsonServers", local)
        self.assertFalse((self.cdir() / "settings.json").exists())

    def test_approving_removes_a_previous_rejection(self):
        projects.project_mcp_approve(self.proj, "docs", False)
        projects.project_mcp_approve(self.proj, "docs", True)
        local = json.loads((self.cdir() / "settings.local.json").read_text())
        self.assertEqual(local["enabledMcpjsonServers"], ["docs"])
        self.assertNotIn("disabledMcpjsonServers", local)

    def test_other_servers_answers_are_left_alone(self):
        (self.cdir() / "settings.local.json").write_text(
            '{"enabledMcpjsonServers": ["other"], "model": "opus"}')
        projects.project_mcp_approve(self.proj, "docs", True)
        local = json.loads((self.cdir() / "settings.local.json").read_text())
        self.assertEqual(local["enabledMcpjsonServers"], ["docs", "other"])
        self.assertEqual(local["model"], "opus")

    def test_the_shared_file_is_read_even_though_it_is_not_written(self):
        (self.cdir() / "settings.json").write_text(
            '{"enabledMcpjsonServers": ["docs"]}')
        self.assertEqual(self.approval(), "approved")

    def test_enable_all_approves_the_undecided(self):
        (self.cdir() / "settings.json").write_text(
            '{"enableAllProjectMcpServers": true}')
        self.assertEqual(self.approval(), "approved")

    def test_an_explicit_rejection_beats_enable_all(self):
        (self.cdir() / "settings.json").write_text(
            '{"enableAllProjectMcpServers": true}')
        projects.project_mcp_approve(self.proj, "docs", False)
        self.assertEqual(self.approval(), "rejected")


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class WrapperRun(Base):
    """wrapper_check / wrapper_test: the in-UI verification buttons."""

    def setUp(self):
        super().setUp()
        self.add()
        projects.wrapper_write(self.proj)
        self._run = projects.subprocess.run
        self.seen = None

    def tearDown(self):
        projects.subprocess.run = self._run
        super().tearDown()

    def fake(self, result, raise_=None):
        def run(argv, **kw):
            self.seen = argv
            if raise_:
                raise raise_
            return result
        projects.subprocess.run = run

    def test_check_reports_the_branch_that_fired(self):
        (self.cdir() / projects.APPEND_MD).write_text("x")
        self.fake(_Result(0, "2.1.224 (Claude Code)",
                          "+ exec claude --append-system-prompt-file "
                          "/p/.claude/append-system-prompt.md --version\n"))
        r = projects.wrapper_check(self.proj)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "append")
        self.assertEqual(self.seen[:2], ["sh", "-x"])

    def test_check_plain_claude_when_nothing_live(self):
        self.fake(_Result(0, "2.1.224", "+ exec claude --version\n"))
        self.assertEqual(projects.wrapper_check(self.proj)["mode"], "none")

    def test_check_nonzero_exit_reports_stderr(self):
        self.fake(_Result(1, "", "boom"))
        r = projects.wrapper_check(self.proj)
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["stderr"])

    def test_check_refuses_foreign_and_missing_wrapper(self):
        (self.cdir() / projects.WRAPPER_NAME).write_text("#!/bin/sh\necho mine\n")
        with self.assertRaises(ValueError):
            projects.wrapper_check(self.proj)
        (self.cdir() / projects.WRAPPER_NAME).unlink()
        with self.assertRaises(ValueError):
            projects.wrapper_check(self.proj)

    def test_live_test_matches_any_line_of_the_file(self):
        (self.cdir() / projects.REPLACE_MD).write_text(
            "# Heading line.\nAlways answer in haiku form.\n")
        self.fake(_Result(0, '"Always answer in haiku form."'))
        r = projects.wrapper_test(self.proj)
        self.assertTrue(r["ok"])
        self.assertEqual(r["matched_line"], "Always answer in haiku form.")
        self.assertEqual(self.seen[0], "sh")
        self.assertEqual(self.seen[2], "-p")

    def test_live_test_reports_mismatch_without_raising(self):
        (self.cdir() / projects.REPLACE_MD).write_text("# You are the tester.\n")
        self.fake(_Result(0, "I have no such instructions in my prompt."))
        self.assertFalse(projects.wrapper_test(self.proj)["ok"])

    def test_live_test_short_answers_cannot_match(self):
        (self.cdir() / projects.REPLACE_MD).write_text("# You are the tester.\n")
        self.fake(_Result(0, "the"))
        self.assertFalse(projects.wrapper_test(self.proj)["ok"])

    def test_live_test_requires_enabled_prompt(self):
        (self.cdir() / (projects.REPLACE_MD + ".off")).write_text("x")
        with self.assertRaises(ValueError):
            projects.wrapper_test(self.proj)

    def test_live_test_surfaces_claude_failure(self):
        (self.cdir() / projects.REPLACE_MD).write_text("x")
        self.fake(_Result(1, "", "auth gone"))
        with self.assertRaisesRegex(ValueError, "auth gone"):
            projects.wrapper_test(self.proj)


class ZshPiece(Base):
    """The zsh setup piece: function file + marker block in .zshrc."""

    def setUp(self):
        super().setUp()
        self.rc = pathlib.Path(self.tmp.name) / ".zshrc"
        self._zshrc_path = projects.zshrc_path
        projects.zshrc_path = lambda: self.rc

    def tearDown(self):
        projects.zshrc_path = self._zshrc_path
        super().tearDown()

    def func(self):
        return self.cfg / projects.ZSH_WRAPPER_NAME

    def test_full_lifecycle(self):
        self.assertFalse(projects.zsh_state()["installed"])
        self.rc.write_text("export EDITOR=vim\n")
        projects.zsh_apply()
        st = projects.zsh_state()
        self.assertTrue(st["installed"])
        self.assertEqual(self.func().read_text(), projects.zsh_function_text())
        self.assertIn(projects.ZSHRC_BEGIN, self.rc.read_text())
        self.assertIn("export EDITOR=vim", self.rc.read_text())
        # outdated: byte-flip the function file, state says re-apply
        self.func().write_text(projects.zsh_function_text() + "# edit\n")
        st = projects.zsh_state()
        self.assertFalse(st["installed"])
        self.assertIn("outdated", st["detail"])
        projects.zsh_apply()
        self.assertTrue(projects.zsh_state()["installed"])
        # remove: block gone, user bytes untouched, function gone
        projects.zsh_remove()
        self.assertEqual(self.rc.read_text(), "export EDITOR=vim\n")
        self.assertFalse(self.func().exists())
        self.assertFalse(projects.zsh_state()["removable"])

    def test_apply_without_zshrc_creates_it(self):
        projects.zsh_apply()
        self.assertTrue(self.rc.read_text().startswith(projects.ZSHRC_BEGIN))
        self.assertTrue(projects.zsh_state()["installed"])

    def test_apply_is_idempotent(self):
        projects.zsh_apply()
        once = self.rc.read_text()
        projects.zsh_apply()
        self.assertEqual(self.rc.read_text(), once)

    def test_foreign_function_file_refused(self):
        self.func().write_text("claude() { echo mine; }\n")
        with self.assertRaises(ValueError):
            projects.zsh_apply()
        self.assertEqual(self.func().read_text(), "claude() { echo mine; }\n")

    def test_damaged_block_refused(self):
        self.rc.write_text(f"{projects.ZSHRC_BEGIN}\nsource x\n")  # no end marker
        with self.assertRaises(ValueError):
            projects.zsh_apply()
        with self.assertRaises(ValueError):
            projects.zsh_remove()

    def test_remove_leaves_foreign_function_file(self):
        projects.zsh_apply()
        self.func().write_text("claude() { echo mine; }\n")
        projects.zsh_remove()
        self.assertNotIn(projects.ZSHRC_BEGIN, self.rc.read_text())
        self.assertEqual(self.func().read_text(), "claude() { echo mine; }\n")

    def test_registered_in_setup_pieces(self):
        from claude_ui import setup
        self.assertIn("zsh-claude", setup.PIECES)
        ids = [p["id"] for p in setup.setup_state()["pieces"]]
        self.assertIn("zsh-claude", ids)

    def test_function_baked_registry_path_tracks_config_dir(self):
        self.assertIn(str(self.cfg / core.PROJECTS_REGISTRY),
                      projects.zsh_function_text())

    def test_zsh_syntax_checks_if_zsh_present(self):
        import shutil as _sh
        import subprocess as _sp
        if not _sh.which("zsh"):
            self.skipTest("zsh not on PATH")
        f = pathlib.Path(self.tmp.name) / "fn.zsh"
        f.write_text(projects.zsh_function_text())
        r = _sp.run(["zsh", "-n", str(f)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
