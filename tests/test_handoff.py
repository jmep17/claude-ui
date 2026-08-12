"""The handoff setup piece: the vendored hook, two skills, the settings merge.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_handoff.py`.

Mirrors tests/test_caveman.py's pattern: config_dir is patched in the four
namespaces that reach the filesystem here (handoff, items, core, settings).
Unlike caveman, this piece's hooks.SessionStart/PreCompact commands run *our*
script, so `foreign()` here must use a different command than the one this
piece owns — `echo hi` — or every "foreign blocks survive" case would
accidentally be testing our own entries.
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

from claude_ui import core, handoff, items, settings, setup  # noqa: E402


def foreign():
    """What the user's own SessionStart blocks look like — a command that is
    not ours, so it must survive apply/remove untouched."""
    return [{"matcher": m, "hooks": [{"type": "command",
                                      "command": "echo hi", "timeout": 5}]}
            for m in ("startup", "clear", "resume", "fork")]


class Base(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = pathlib.Path(self.tmpdir.name)
        saved = [(m, m.config_dir) for m in (handoff, items, core, settings)]
        for m, _ in saved:
            m.config_dir = lambda t=self.tmp: t

        def restore():
            for m, fn in saved:
                m.config_dir = fn
        self.addCleanup(restore)
        self.hookp, self.storep = handoff.handoff_paths()
        self.settingsp = self.tmp / "settings.json"

    def write_settings(self, data):
        self.settingsp.write_text(json.dumps(data, indent=2) + "\n")

    def read_settings(self):
        return json.loads(self.settingsp.read_text())

    def session_start(self):
        return self.read_settings().get("hooks", {}).get("SessionStart", [])

    def precompact(self):
        return self.read_settings().get("hooks", {}).get("PreCompact", [])

    def skill_path(self, name):
        return self.tmp / "skills" / name / "SKILL.md"


class Apply(Base):

    def test_empty_config_is_not_installed(self):
        st = handoff.handoff_state()
        self.assertFalse(st["installed"])
        self.assertEqual(st["id"], "handoff")
        self.assertTrue(st["removable"])
        self.assertIn("no plugin, no network", st["detail"])

    def test_apply_installs_everything(self):
        handoff.handoff_apply()

        self.assertEqual(self.hookp.read_text(), handoff._hook_payload())

        for name in handoff.SKILLS:
            text = self.skill_path(name).read_text()
            meta = core.parse_frontmatter(text)
            self.assertEqual(meta[handoff.PRESET_KEY], handoff.PRESET_REF)
            self.assertEqual(meta["name"], name)

        self.assertTrue(self.storep.is_dir())

        starts = self.session_start()
        self.assertEqual(len(starts), 4)
        self.assertEqual([b["matcher"] for b in starts],
                         ["startup", "clear", "resume", "fork"])

        pre = self.precompact()
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["matcher"], "auto")
        self.assertTrue(pre[0]["hooks"][0]["command"].endswith(" --precompact"))

        dirs = self.read_settings()["permissions"]["additionalDirectories"]
        self.assertEqual(dirs, [handoff._store_spellings()[0]])

        st = handoff.handoff_state()
        self.assertTrue(st["installed"])

    def test_foreign_blocks_and_entries_survive(self):
        self.write_settings({
            "hooks": {"SessionStart": foreign()},
            "permissions": {"additionalDirectories": ["~/src"],
                            "allow": ["WebFetch"]},
        })
        handoff.handoff_apply()
        starts = self.session_start()
        self.assertEqual(starts[:4], foreign())
        data = self.read_settings()
        self.assertEqual(data["permissions"]["additionalDirectories"][0], "~/src")
        self.assertEqual(data["permissions"]["allow"], ["WebFetch"])

    def test_reapply_writes_nothing(self):
        handoff.handoff_apply()
        before = self.settingsp.read_text()
        calls = []
        for mod, name in ((handoff, "atomic_write"), (settings, "atomic_write"),
                          (items, "atomic_write")):
            orig = getattr(mod, name)
            setattr(mod, name, lambda p, *a, _o=orig, **k: calls.append(p) or _o(p, *a, **k))
            self.addCleanup(setattr, mod, name, orig)
        handoff.handoff_apply()
        self.assertEqual(calls, [])
        self.assertEqual(self.settingsp.read_text(), before)

    def test_already_wired_in_the_other_spelling_causes_no_churn(self):
        session_cmd = handoff._commands()[0]           # tilde spelling
        precompact_cmd = handoff._commands()[1]
        blocks = [{"matcher": m, "hooks": [{"type": "command",
                                            "command": session_cmd, "timeout": 10}]}
                  for m in handoff.SESSION_MATCHERS]
        pre = {"matcher": "auto", "hooks": [{"type": "command",
                                             "command": precompact_cmd, "timeout": 10}]}
        self.write_settings({
            "hooks": {"SessionStart": blocks, "PreCompact": [pre]},
            "permissions": {"additionalDirectories": [handoff._store_spellings()[1]]},
        })
        before = self.settingsp.read_text()
        handoff.handoff_apply()
        self.assertEqual(self.settingsp.read_text(), before)
        # but the skills and script were still written
        self.assertTrue(self.hookp.is_file())
        for name in handoff.SKILLS:
            self.assertTrue(self.skill_path(name).is_file())

    def test_adoption_of_an_unstamped_identical_copy(self):
        handoff.handoff_apply()
        name = "handoffs"
        payload = handoff._payload(name)
        unstamped = core.set_frontmatter_key(payload, handoff.PRESET_KEY, None)
        self.skill_path(name).write_text(unstamped)
        handoff.handoff_apply()
        self.assertEqual(self.skill_path(name).read_text(), payload)

    def test_a_stamped_revision_is_updated_on_reapply(self):
        handoff.handoff_apply()
        name = "handoffs"
        stamped_old = core.set_frontmatter_key(
            "---\nname: handoffs\n---\nold revision\n",
            handoff.PRESET_KEY, handoff.PRESET_REF)
        self.skill_path(name).write_text(stamped_old)
        handoff.handoff_apply()
        self.assertEqual(self.skill_path(name).read_text(), handoff._payload(name))

    def test_an_unstamped_edit_is_still_left_alone(self):
        handoff.handoff_apply()
        name = "handoffs"
        self.skill_path(name).write_text("---\nname: handoffs\n---\nmine now\n")
        handoff.handoff_apply()
        self.assertEqual(self.skill_path(name).read_text(),
                         "---\nname: handoffs\n---\nmine now\n")

    def test_a_genuinely_edited_skill_is_left_alone(self):
        handoff.handoff_apply()
        name = "handoffs"
        self.skill_path(name).write_text("---\nname: handoffs\n---\nmine now\n")
        handoff.handoff_apply()
        self.assertEqual(self.skill_path(name).read_text(),
                         "---\nname: handoffs\n---\nmine now\n")
        st = handoff.handoff_state()
        self.assertIn("edited since", " ".join(st["notes"]))

    def test_a_differing_hook_script_is_overwritten(self):
        handoff.handoff_apply()
        with open(self.hookp, "a") as f:
            f.write("# drift\n")
        handoff.handoff_apply()
        self.assertEqual(self.hookp.read_text(), handoff._hook_payload())

    def test_a_disabled_twin_is_refused(self):
        parked = self.tmp / "disabled" / "skills" / "handoff"
        parked.mkdir(parents=True)
        (parked / "SKILL.md").write_text("---\nname: handoff\n---\nparked\n")
        with self.assertRaises(ValueError) as cm:
            handoff.handoff_apply()
        self.assertIn("disabled", str(cm.exception))

    def test_invalid_settings_json_is_soft_in_state_loud_on_apply(self):
        self.settingsp.write_text("{not json")
        st = handoff.handoff_state()
        self.assertFalse(st["installed"])
        with self.assertRaises(ValueError):
            handoff.handoff_apply()
        self.assertEqual(self.settingsp.read_text(), "{not json")

    def test_hooks_of_the_wrong_shape_is_loud(self):
        self.write_settings({"hooks": ["nope"]})
        with self.assertRaises(ValueError):
            handoff.handoff_apply()
        self.assertFalse((self.tmp / "skills").exists())

    def test_permissions_additional_directories_of_the_wrong_shape_is_loud(self):
        self.write_settings({"permissions": {"additionalDirectories": "nope"}})
        with self.assertRaises(ValueError):
            handoff.handoff_apply()
        self.assertFalse((self.tmp / "skills").exists())

    def test_a_missing_vendored_payload_is_loud_and_writes_nothing(self):
        saved = handoff.DATA_DIR
        handoff.DATA_DIR = self.tmp / "gone"
        self.addCleanup(setattr, handoff, "DATA_DIR", saved)
        with self.assertRaises(ValueError):
            handoff.handoff_apply()
        self.assertFalse(self.settingsp.exists())
        self.assertFalse((self.tmp / "skills").exists())


class Remove(Base):

    def test_remove_drops_only_ours(self):
        self.write_settings({
            "hooks": {"SessionStart": foreign()},
            "permissions": {"additionalDirectories": ["~/src"]},
        })
        handoff.handoff_apply()
        handoff.handoff_remove()
        data = self.read_settings()
        self.assertEqual(data["hooks"]["SessionStart"], foreign())
        self.assertNotIn("PreCompact", data.get("hooks", {}))
        self.assertEqual(data["permissions"]["additionalDirectories"], ["~/src"])
        self.assertFalse(self.hookp.exists())
        text = self.settingsp.read_text()
        for cmd in handoff._commands():
            self.assertNotIn(cmd, text)
        for d in handoff._store_spellings():
            self.assertNotIn(f'"{d}"', text)

    def test_remove_prunes_cleanly(self):
        handoff.handoff_apply()
        handoff.handoff_remove()
        data = self.read_settings()
        self.assertNotIn("hooks", data)
        self.assertNotIn("permissions", data)

    def test_remove_keeps_skills_store_and_briefs(self):
        handoff.handoff_apply()
        (self.storep / "x.md").write_text("a brief\n")
        handoff.handoff_remove()
        for name in handoff.SKILLS:
            self.assertTrue(self.skill_path(name).is_file())
        self.assertTrue((self.storep / "x.md").is_file())

    def test_remove_on_a_clean_config_is_a_no_op(self):
        handoff.handoff_remove()
        self.assertFalse(self.settingsp.exists())


class CaseSuite(unittest.TestCase):
    """The vendored hook must stay green against its own case suite — this is
    the repo-side replacement for running it in ~/.claude."""

    def test_the_vendored_suite_is_green(self):
        suite = pathlib.Path(__file__).resolve().parent.parent / \
            "bin" / "claude_ui" / "data" / "handoff" / "handoff_load_cases.sh"
        r = subprocess.run(["bash", str(suite)], capture_output=True,
                           text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("failed 0", r.stdout)


class VendoredPayload(unittest.TestCase):
    """A bad shipped file is a bug in this repo, not a runtime error."""

    def test_skill_files_have_placeholders_and_name_themselves(self):
        for name in handoff.SKILLS:
            path = handoff.DATA_DIR / "skills" / name / "SKILL.md"
            text = path.read_text()
            self.assertTrue("__HOOK__" in text or "__STORE" in text, name)
            meta = core.parse_frontmatter(text)
            self.assertEqual(meta.get("name"), name)

    def test_payload_has_no_placeholders_and_is_stamped(self):
        for name in handoff.SKILLS:
            rendered = handoff._payload(name)
            self.assertNotIn("__", rendered)
            self.assertIn(handoff.PRESET_KEY, rendered)

    def test_no_vendored_file_contains_a_users_path(self):
        for p in handoff.DATA_DIR.rglob("*"):
            if p.is_file():
                self.assertNotIn("/Users/", p.read_text(), str(p))

    def test_handoff_skill_shape_guards_the_token_cost(self):
        path = handoff.DATA_DIR / "skills" / "handoff" / "SKILL.md"
        text = path.read_text()
        meta = core.parse_frontmatter(text)
        self.assertIn("__HOOK__", meta.get("allowed-tools", ""))
        # <= 90 lines: the mechanics this piece cut (Gather, frontmatter
        # authoring, filename/collision handling) must not creep back in as
        # prose — that was the whole point of the --new/--facts move.
        self.assertLessEqual(len(text.splitlines()), 90)


class NewEndToEnd(unittest.TestCase):
    """Runs the *installed* hook script's --new mode as a real subprocess,
    the way a session actually invokes it."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = pathlib.Path(self.tmpdir.name)
        saved = [(m, m.config_dir) for m in (handoff, items, core, settings)]
        for m, _ in saved:
            m.config_dir = lambda t=self.tmp: t

        def restore():
            for m, fn in saved:
                m.config_dir = fn
        self.addCleanup(restore)
        self.hookp, self.storep = handoff.handoff_paths()

    def test_new_round_trips_through_the_installed_hook(self):
        handoff.handoff_apply()
        body = self.tmp / "body.md"
        body.write_text("## Next steps\n1. Say hi\n")
        work = self.tmp / "work"
        work.mkdir()
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.tmp))
        r = subprocess.run(
            [sys.executable, str(self.hookp), "--new", "--title", "E2E brief",
             "--body-file", str(body), "--cwd", str(work)],
            capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Handoff written", r.stdout)
        written = [p for p in (self.storep / "work").glob("*.md")
                  if p.name != "INDEX.md"]
        self.assertEqual(len(written), 1)
        self.assertIn("status: pending", written[0].read_text())


class Registry(unittest.TestCase):

    def test_the_piece_is_registered(self):
        self.assertIn("handoff", setup.PIECES)
        self.assertIn("handoff", [p["id"] for p in setup.setup_state()["pieces"]])

    def test_dispatch_reaches_it(self):
        self.assertIs(setup.PIECES["handoff"]["state"], handoff.handoff_state)
        self.assertIs(setup.PIECES["handoff"]["apply"], handoff.handoff_apply)
        self.assertIs(setup.PIECES["handoff"]["remove"], handoff.handoff_remove)


if __name__ == "__main__":
    unittest.main()
