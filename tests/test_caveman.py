"""The caveman setup piece: the skill file, the generated hook, the settings merge.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_caveman.py`.

The cases that matter are the ones about *other people's* hooks. This is the only
setup piece that merges into settings.json's `hooks`, which is a list of blocks
the user also owns — the machine this was written for has four SessionStart
blocks running a handoff script. A piece that installed by writing the whole
array would eat them, and one that uninstalled by deleting the event would eat
them on the way out. So `foreign` below is seeded into nearly every case.

config_dir is patched in all four namespaces that reach the filesystem here:
caveman (paths + the generated script), items (item_create), core, and settings.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import caveman, core, items, settings, setup  # noqa: E402


def foreign(n=4):
    """What the user's own SessionStart blocks look like."""
    return [{"matcher": m, "hooks": [{"type": "command",
                                      "command": "python3 ~/.claude/hooks/handoff_load.py",
                                      "timeout": 10}]}
            for m in ("startup", "clear", "resume", "fork")[:n]]


class Base(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = pathlib.Path(self.tmpdir.name)
        saved = [(m, m.config_dir) for m in (caveman, items, core, settings)]
        for m, _ in saved:
            m.config_dir = lambda t=self.tmp: t

        def restore():
            for m, fn in saved:
                m.config_dir = fn
        self.addCleanup(restore)
        self.skillp, self.scriptp, self.flagp = caveman.caveman_paths()
        self.settingsp = self.tmp / "settings.json"

    def write_settings(self, data):
        self.settingsp.write_text(json.dumps(data, indent=2) + "\n")

    def read_settings(self):
        return json.loads(self.settingsp.read_text())

    def session_start(self):
        return self.read_settings().get("hooks", {}).get("SessionStart", [])


class Apply(Base):

    def test_empty_config_is_not_installed(self):
        st = caveman.caveman_state()
        self.assertFalse(st["installed"])
        self.assertIn("no plugin, no marketplace", st["detail"])
        self.assertEqual(st["id"], "caveman")
        self.assertTrue(st["removable"])

    def test_apply_installs_all_the_artifacts(self):
        caveman.caveman_apply()
        self.assertTrue(self.skillp.is_file())
        self.assertTrue(self.scriptp.is_file())
        self.assertEqual(self.flagp.read_text().strip(), "full")
        self.assertEqual(len(self.session_start()), 1)
        st = caveman.caveman_state()
        self.assertTrue(st["installed"])
        self.assertIn("level full", st["detail"])

    def test_apply_installs_the_compress_skill_tree_whole(self):
        caveman.caveman_apply()
        root = self.tmp / "skills" / "caveman-compress"
        self.assertTrue((root / "SKILL.md").is_file())
        self.assertTrue((root / "SECURITY.md").is_file())
        scripts = sorted(p.name for p in (root / "scripts").glob("*.py"))
        self.assertIn("compress.py", scripts)
        self.assertIn("__main__.py", scripts)
        # every vendored file arrives, byte-identical bar the stamped SKILL.md
        for rel in caveman._payload_files("caveman-compress"):
            self.assertTrue((root / rel[0]).is_file(), rel[0])

    def test_only_caveman_is_hooked(self):
        caveman.caveman_apply()
        blocks = json.dumps(self.session_start())
        self.assertNotIn("compress", blocks)
        self.assertEqual(len(self.session_start()), 1)

    def test_both_skills_are_needed_to_read_as_installed(self):
        caveman.caveman_apply()
        import shutil
        shutil.rmtree(self.tmp / "skills" / "caveman-compress")
        st = caveman.caveman_state()
        self.assertFalse(st["installed"])
        self.assertIn("the caveman-compress skill", st["detail"])

    def test_the_script_is_executable(self):
        caveman.caveman_apply()
        self.assertTrue(os.access(self.scriptp, os.X_OK))

    def test_the_installed_skill_carries_the_upstream_ref(self):
        caveman.caveman_apply()
        meta = core.parse_frontmatter(self.skillp.read_text())
        self.assertEqual(meta[caveman.PRESET_KEY], caveman.PRESET_REF)
        self.assertEqual(meta["name"], "caveman")
        # not the adoption key: no plugin is installed to resolve it against
        self.assertNotIn(core.SOURCE_KEY, meta)

    def test_apply_appends_and_leaves_foreign_blocks_byte_identical(self):
        self.write_settings({"model": "opus", "hooks": {"SessionStart": foreign()}})
        caveman.caveman_apply()
        blocks = self.session_start()
        self.assertEqual(len(blocks), 5)
        self.assertEqual(blocks[:4], foreign())
        self.assertEqual(blocks[4], caveman._block())
        self.assertEqual(self.read_settings()["model"], "opus")

    def test_apply_keeps_other_hook_events(self):
        self.write_settings({"hooks": {"PreCompact": [{"matcher": "auto",
                                                       "hooks": [{"type": "command",
                                                                  "command": "x"}]}]}})
        caveman.caveman_apply()
        self.assertIn("PreCompact", self.read_settings()["hooks"])
        self.assertEqual(len(self.session_start()), 1)

    def test_reapply_writes_nothing(self):
        self.write_settings({"hooks": {"SessionStart": foreign()}})
        caveman.caveman_apply()
        before = self.settingsp.read_text()
        calls = []
        for mod, name in ((caveman, "atomic_write"), (caveman, "atomic_write_bytes"),
                          (settings, "atomic_write"), (items, "atomic_write")):
            orig = getattr(mod, name)
            setattr(mod, name, lambda p, *a, _o=orig, **k: calls.append(p) or _o(p, *a, **k))
            self.addCleanup(setattr, mod, name, orig)
        caveman.caveman_apply()
        self.assertEqual(calls, [])
        self.assertEqual(self.settingsp.read_text(), before)

    def test_apply_twice_leaves_one_block_not_two(self):
        caveman.caveman_apply()
        self.scriptp.unlink()               # force the second apply to do work
        caveman.caveman_apply()
        self.assertEqual(len(self.session_start()), 1)

    def test_apply_does_not_clobber_a_chosen_level(self):
        caveman.caveman_apply()
        caveman.caveman_level_set("ultra")
        caveman.caveman_apply()
        self.assertEqual(caveman.caveman_level(), "ultra")

    def test_an_edited_skill_is_left_alone(self):
        caveman.caveman_apply()
        self.skillp.write_text("---\nname: caveman\n---\nmine now\n")
        caveman.caveman_apply()
        self.assertIn("mine now", self.skillp.read_text())
        self.assertIn("edited since", " ".join(caveman.caveman_state()["notes"]))


class Off(Base):

    def test_a_missing_level_file_reads_as_switched_off(self):
        caveman.caveman_apply()
        self.flagp.unlink()
        st = caveman.caveman_state()
        self.assertFalse(st["installed"])
        self.assertIn("switched off", st["detail"])

    def test_apply_turns_it_back_on(self):
        caveman.caveman_apply()
        self.flagp.unlink()
        caveman.caveman_apply()
        self.assertTrue(caveman.caveman_state()["installed"])

    def test_half_installed_names_what_is_missing(self):
        caveman.caveman_apply()
        self.scriptp.unlink()
        self.assertIn("the hook script", caveman.caveman_state()["detail"])

    def test_an_unknown_level_is_refused(self):
        with self.assertRaises(ValueError):
            caveman.caveman_level_set("shouty")


class Remove(Base):

    def test_remove_drops_only_our_block(self):
        self.write_settings({"hooks": {"SessionStart": foreign()}})
        caveman.caveman_apply()
        caveman.caveman_remove()
        self.assertEqual(self.session_start(), foreign())
        self.assertFalse(self.scriptp.exists())

    def test_remove_keeps_the_skill_and_the_level(self):
        caveman.caveman_apply()
        caveman.caveman_remove()
        self.assertTrue(self.skillp.is_file())
        self.assertEqual(caveman.caveman_level(), "full")

    def test_remove_prunes_the_hooks_key_when_it_was_only_ours(self):
        caveman.caveman_apply()
        caveman.caveman_remove()
        self.assertNotIn("hooks", self.read_settings())

    def test_remove_keeps_a_sibling_command_sharing_our_block(self):
        caveman.caveman_apply()
        data = self.read_settings()
        mine = {"type": "command", "command": "echo hi"}
        data["hooks"]["SessionStart"][0]["hooks"].append(mine)
        self.write_settings(data)
        caveman.caveman_remove()
        self.assertEqual(self.session_start(), [{"hooks": [mine]}])

    def test_remove_on_a_clean_config_is_a_no_op(self):
        caveman.caveman_remove()
        self.assertFalse(self.settingsp.exists())

    def test_state_after_remove(self):
        caveman.caveman_apply()
        caveman.caveman_remove()
        self.assertFalse(caveman.caveman_state()["installed"])


class Refusals(Base):

    def test_invalid_settings_json_is_soft_in_state_loud_on_apply(self):
        self.settingsp.write_text("{not json")
        st = caveman.caveman_state()
        self.assertFalse(st["installed"])
        self.assertIn("unreadable", st["detail"])
        with self.assertRaises(ValueError):
            caveman.caveman_apply()
        self.assertEqual(self.settingsp.read_text(), "{not json")

    def test_hooks_of_the_wrong_shape_is_loud(self):
        self.write_settings({"hooks": ["nope"]})
        with self.assertRaises(ValueError):
            caveman.caveman_apply()
        self.assertIn("not a JSON object", caveman.caveman_state()["detail"])

    def test_a_disabled_twin_is_refused_by_name(self):
        parked = self.tmp / "disabled" / "skills" / "caveman"
        parked.mkdir(parents=True)
        (parked / "SKILL.md").write_text("---\nname: caveman\n---\nparked\n")
        with self.assertRaises(ValueError) as cm:
            caveman.caveman_apply()
        self.assertIn("disabled", str(cm.exception))
        self.assertIn("disabled", caveman.caveman_state()["notes"][0])

    def test_a_missing_payload_is_loud_and_writes_nothing(self):
        saved = caveman.PRESET_DIR
        caveman.PRESET_DIR = self.tmp / "gone"
        self.addCleanup(setattr, caveman, "PRESET_DIR", saved)
        with self.assertRaises(ValueError):
            caveman.caveman_apply()
        self.assertFalse(self.settingsp.exists())
        self.assertFalse(self.scriptp.exists())
        self.assertFalse((self.tmp / "skills").exists())

    def test_a_second_skill_missing_stops_the_whole_apply(self):
        # payloads are read up front precisely so a broken one costs nothing
        saved = caveman.PRESET_DIR
        broken = self.tmp / "half"
        (broken / "caveman").mkdir(parents=True)
        (broken / "caveman" / "SKILL.md").write_text("---\nname: caveman\n---\nx\n")
        caveman.PRESET_DIR = broken
        self.addCleanup(setattr, caveman, "PRESET_DIR", saved)
        with self.assertRaises(ValueError):
            caveman.caveman_apply()
        self.assertFalse((self.tmp / "skills").exists())


class GeneratedScript(Base):
    """The hook is the whole point of the piece, so run it."""

    def run_script(self):
        return subprocess.run([sys.executable, str(self.scriptp)],
                              capture_output=True, text=True, timeout=20)

    def test_it_emits_the_ruleset_as_session_start_context(self):
        caveman.caveman_apply()
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        ctx = out["additionalContext"]
        self.assertIn("level: full", ctx)
        self.assertIn("Drop: articles", ctx)          # a line from the body
        self.assertNotIn("description:", ctx)         # frontmatter is stripped
        self.assertNotIn(caveman.PRESET_KEY, ctx)

    def test_the_level_reaches_the_context(self):
        caveman.caveman_apply()
        caveman.caveman_level_set("ultra")
        self.assertIn("level: ultra",
                      json.loads(self.run_script().stdout)
                      ["hookSpecificOutput"]["additionalContext"])

    def test_deleting_the_level_file_silences_it(self):
        caveman.caveman_apply()
        self.flagp.unlink()
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_a_junk_level_falls_back_to_the_default(self):
        caveman.caveman_apply()
        self.flagp.write_text("gronk\n")
        self.assertIn("level: full",
                      json.loads(self.run_script().stdout)
                      ["hookSpecificOutput"]["additionalContext"])

    def test_deleting_the_skill_silences_it_rather_than_erroring(self):
        caveman.caveman_apply()
        self.skillp.unlink()
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class VendoredPayload(unittest.TestCase):
    """A bad shipped file is a bug in this repo, not a runtime error."""

    def test_every_skill_exists_and_its_frontmatter_names_itself(self):
        for name in caveman.ALL_SKILLS:
            meta = core.parse_frontmatter(
                (caveman.PRESET_DIR / name / "SKILL.md").read_text())
            self.assertEqual(meta.get("name"), name)
            self.assertTrue(meta.get("description", "").strip(), name)

    def test_they_are_unstamped_on_disk(self):
        # kept byte-identical to upstream so tools/sync_caveman_skill.py can
        # diff them without knowing anything about us
        for name in caveman.ALL_SKILLS:
            text = (caveman.PRESET_DIR / name / "SKILL.md").read_text()
            self.assertNotIn(caveman.PRESET_KEY, text)
            self.assertIn(caveman.PRESET_KEY, caveman._payload(name))

    def test_they_need_nothing_from_the_plugin(self):
        for name in caveman.ALL_SKILLS:
            for p in (caveman.PRESET_DIR / name).rglob("*"):
                if p.is_file():
                    self.assertNotIn("CLAUDE_PLUGIN_ROOT", p.read_text(), p.name)

    def test_the_compress_scripts_are_shipped(self):
        rels = [r.as_posix() for r, _ in caveman._payload_files("caveman-compress")]
        self.assertIn("scripts/compress.py", rels)
        self.assertIn("scripts/__main__.py", rels)
        self.assertIn("SECURITY.md", rels)

    def test_payload_files_stamps_only_skill_md(self):
        for rel, data in caveman._payload_files("caveman-compress"):
            has = caveman.PRESET_KEY.encode() in data
            self.assertEqual(has, rel.as_posix() == "SKILL.md", rel)

    def test_every_documented_level_is_named_in_the_body(self):
        text = caveman.PAYLOAD.read_text()
        for level in caveman.LEVELS:
            self.assertIn(level, text)
        self.assertIn(caveman.DEFAULT_LEVEL, caveman.LEVELS)


class Registry(unittest.TestCase):

    def test_the_piece_is_registered(self):
        self.assertIn("caveman", setup.PIECES)
        self.assertIn("caveman", [p["id"] for p in setup.setup_state()["pieces"]])

    def test_dispatch_reaches_it(self):
        self.assertIs(setup.PIECES["caveman"]["state"], caveman.caveman_state)
        self.assertIs(setup.PIECES["caveman"]["apply"], caveman.caveman_apply)
        self.assertIs(setup.PIECES["caveman"]["remove"], caveman.caveman_remove)


if __name__ == "__main__":
    unittest.main()
