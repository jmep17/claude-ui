"""The Projects tab backend: registry, prompt-file state, wrappers, and the
resolve_editable extension that lets the editor into <project>/.claude/.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from
the repo root, or just `python3 tests/test_projects.py`.

The ResolveEditable cases are the security battery: registering a project must
open exactly its real .claude/ subtree and nothing else — not the rest of the
project, not a symlink target outside it, and not anything reached through a
.claude that is itself a symlink. The template cases pin the other load-bearing
property: generated shell code never executes or sources repo content."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import core, projects  # noqa: E402


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

    def tearDown(self):
        core.config_dir = self._config_dir
        projects.config_dir = self._p_config_dir
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
