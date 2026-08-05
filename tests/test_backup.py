"""Backup archives: what goes in, what a restore would change, what it refuses.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_backup.py`.

config_dir is patched in every namespace that reads it, the same way
test_plugins.py does it, and CLAUDE_JSON is redirected too: the MCP group reads
the real ~/.claude.json otherwise, and the restore path would write to it.
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import backup, core, insight, items, mcp, plugins, settings, statusline  # noqa: E402


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Base(unittest.TestCase):
    """A temp config dir with one of everything, and a temp backup dir."""

    CONFIG_MODULES = (backup, core, insight, items, plugins, settings, statusline)

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.cfg = self.tmp / "claude"
        self.dest = self.tmp / "backups"
        self.claude_json = self.tmp / "claude.json"

        self._saved = [(m, m.config_dir) for m in self.CONFIG_MODULES]
        for m, _ in self._saved:
            m.config_dir = lambda c=self.cfg: c
        self._saved_dir = backup.backup_dir
        backup.backup_dir = lambda d=self.dest: d
        # CLAUDE_JSON is a module constant, imported by name into both modules
        self._saved_json = [(m, m.CLAUDE_JSON) for m in (core, mcp, backup)]
        for m, _ in self._saved_json:
            m.CLAUDE_JSON = self.claude_json

        write(self.cfg / "CLAUDE.md", "be brief\n")
        write(self.cfg / "settings.json", json.dumps({"model": "opus"}))
        write(self.cfg / "skills" / "pdf" / "SKILL.md",
              "---\nname: pdf\ndescription: read pdfs\n---\nbody\n")
        (self.cfg / "skills" / "pdf" / "logo.bin").write_bytes(b"\x00\xff\xfeimg")
        write(self.cfg / "disabled" / "commands" / "old.md", "---\n---\nparked\n")
        self.script = write(self.cfg / "statusline.sh", "#!/bin/sh\necho hi\n")
        self.script.chmod(0o755)
        write(self.cfg / "projects" / "proj" / "a.jsonl", '{"usage": 1}\n')
        self.claude_json.write_text(json.dumps({
            "oauthAccount": {"email": "me@example.com"},
            "projects": {"/tmp/x": {"history": []}},
            "mcpServers": {"gh": {"command": "gh-mcp", "env": {"TOKEN": "sekrit"}}},
        }))

    def tearDown(self):
        for m, fn in self._saved:
            m.config_dir = fn
        for m, p in self._saved_json:
            m.CLAUDE_JSON = p
        backup.backup_dir = self._saved_dir
        self.tmpdir.cleanup()

    def create(self, *picks, note=""):
        return backup.backup_create(list(picks) or backup.GROUP_IDS, note)

    def members(self, name):
        with zipfile.ZipFile(self.dest / name) as z:
            return set(z.namelist())

    def statuses(self, name):
        return {e["path"]: e["status"] for e in backup.backup_inspect(name)["entries"]}


class TestPlan(Base):
    def test_every_group_reports_what_it_holds(self):
        plan = {g["id"]: g for g in backup.backup_plan()}
        self.assertEqual(set(plan), set(backup.GROUP_IDS))
        self.assertEqual(plan["items"]["files"], 3)      # SKILL.md, logo.bin, old.md
        self.assertEqual(plan["config"]["files"], 2)     # CLAUDE.md, settings.json
        self.assertEqual(plan["transcripts"]["files"], 1)
        self.assertTrue(plan["mcp"]["secrets"])
        self.assertGreater(plan["config"]["bytes"], 0)

    def test_empty_selection_is_refused(self):
        with self.assertRaises(ValueError):
            backup.backup_create([], "")


class TestCreate(Base):
    def test_archive_holds_files_and_a_manifest(self):
        res = self.create()
        names = self.members(res["name"])
        self.assertIn("manifest.json", names)
        self.assertIn("files/CLAUDE.md", names)
        self.assertIn("files/skills/pdf/SKILL.md", names)
        self.assertIn("files/disabled/commands/old.md", names)
        self.assertIn("files/projects/proj/a.jsonl", names)
        self.assertIn(backup.MCP_MEMBER, names)

    def test_whole_claude_json_is_never_copied(self):
        """Only the mcpServers map: that file also holds history and the account."""
        res = self.create()
        with zipfile.ZipFile(self.dest / res["name"]) as z:
            blob = json.loads(z.read(backup.MCP_MEMBER))
        self.assertEqual(list(blob), ["mcpServers"])
        self.assertIn("gh", blob["mcpServers"])
        joined = "".join(self.members(res["name"]))
        self.assertNotIn("oauthAccount", joined)

    def test_secrets_flag_follows_the_mcp_group(self):
        self.assertTrue(self.create()["contains_secrets"])
        self.assertFalse(self.create("items", "config")["contains_secrets"])

    def test_groups_listed_are_the_ones_with_content(self):
        res = self.create("items", "plugins")   # no plugins on this machine
        with zipfile.ZipFile(self.dest / res["name"]) as z:
            m = json.loads(z.read("manifest.json"))
        self.assertEqual(m["groups"], ["items"])

    def test_two_backups_in_the_same_second_do_not_collide(self):
        a, b = self.create("config"), self.create("config")
        self.assertNotEqual(a["name"], b["name"])
        self.assertEqual(len(backup.backup_list()["archives"]), 2)

    def test_list_reports_a_broken_archive_rather_than_hiding_it(self):
        self.dest.mkdir(parents=True, exist_ok=True)
        (self.dest / "junk.zip").write_bytes(b"not a zip")
        rows = {a["name"]: a for a in backup.backup_list()["archives"]}
        self.assertIn("error", rows["junk.zip"])


class TestInspect(Base):
    def test_identical_config_is_all_same(self):
        name = self.create()["name"]
        self.assertEqual(set(self.statuses(name).values()), {"same"})

    def test_changed_file_differs_and_the_diff_names_the_line(self):
        name = self.create("config")["name"]
        write(self.cfg / "CLAUDE.md", "be verbose\n")
        row = next(e for e in backup.backup_inspect(name)["entries"]
                   if e["path"] == "files/CLAUDE.md")
        self.assertEqual(row["status"], "differs")
        self.assertIn("-be verbose", row["diff"])
        self.assertIn("+be brief", row["diff"])

    def test_missing_file_is_new(self):
        name = self.create("config")["name"]
        (self.cfg / "CLAUDE.md").unlink()
        self.assertEqual(self.statuses(name)["files/CLAUDE.md"], "new")

    def test_binary_differences_report_without_a_diff(self):
        name = self.create("items")["name"]
        (self.cfg / "skills" / "pdf" / "logo.bin").write_bytes(b"\x00\xff\xfeother")
        row = next(e for e in backup.backup_inspect(name)["entries"]
                   if e["path"].endswith("logo.bin"))
        self.assertEqual(row["status"], "differs")
        self.assertNotIn("diff", row)

    def test_report_names_the_config_dir_both_ways(self):
        """The manifest records an absolute path; the UI compares against
        config_dir_abs, not the tilde'd display string."""
        rep = backup.backup_inspect(self.create("config")["name"])
        self.assertEqual(rep["config_dir_abs"], str(self.cfg))
        self.assertEqual(rep["manifest"]["config_dir"], rep["config_dir_abs"])

    def test_unknown_archive_is_refused(self):
        for bad in ("../../etc/passwd", "nope.zip", "no-extension", ""):
            with self.assertRaises(ValueError):
                backup.backup_inspect(bad)


class TestRestore(Base):
    def restore_all(self, name):
        entries = backup.backup_inspect(name)["entries"]
        return backup.backup_restore(name, [e["path"] for e in entries])

    def test_round_trip_into_an_empty_config_dir(self):
        name = self.create()["name"]
        fresh = self.tmp / "fresh"
        for m, _ in self._saved:
            m.config_dir = lambda f=fresh: f
        st = self.statuses(name)
        # every config-dir file is new; ~/.claude.json lives outside the config
        # dir and did not move, so its servers are still there
        self.assertEqual({v for k, v in st.items() if k != backup.MCP_MEMBER}, {"new"})
        self.assertEqual(st[backup.MCP_MEMBER], "same")
        res = self.restore_all(name)
        self.assertEqual(res["failed"], [])
        self.assertEqual((fresh / "CLAUDE.md").read_text(), "be brief\n")
        self.assertEqual((fresh / "skills" / "pdf" / "logo.bin").read_bytes(),
                         b"\x00\xff\xfeimg")
        self.assertEqual((fresh / "projects" / "proj" / "a.jsonl").read_text(),
                         '{"usage": 1}\n')
        self.assertTrue((fresh / "disabled" / "commands" / "old.md").is_file())

    def test_executable_bit_survives(self):
        name = self.create("statusline")["name"]
        self.script.chmod(0o644)
        self.restore_all(name)
        self.assertEqual(self.script.stat().st_mode & 0o777, 0o755)

    def test_only_the_paths_given_are_written(self):
        name = self.create("config")["name"]
        write(self.cfg / "CLAUDE.md", "edited\n")
        write(self.cfg / "settings.json", json.dumps({"model": "sonnet"}))
        backup.backup_restore(name, ["files/settings.json"])
        self.assertEqual((self.cfg / "CLAUDE.md").read_text(), "edited\n")
        self.assertEqual(json.loads((self.cfg / "settings.json").read_text())["model"],
                         "opus")

    def test_mcp_merges_and_leaves_the_rest_of_claude_json_alone(self):
        name = self.create("mcp")["name"]
        self.claude_json.write_text(json.dumps({
            "oauthAccount": {"email": "me@example.com"},
            "projects": {"/tmp/x": {"history": ["keep me"]}},
            "mcpServers": {"other": {"command": "other-mcp"}},
        }))
        backup.backup_restore(name, [backup.MCP_MEMBER])
        live = json.loads(self.claude_json.read_text())
        self.assertEqual(live["projects"]["/tmp/x"]["history"], ["keep me"])
        self.assertEqual(live["oauthAccount"]["email"], "me@example.com")
        self.assertIn("other", live["mcpServers"])          # not clobbered
        self.assertEqual(live["mcpServers"]["gh"]["env"]["TOKEN"], "sekrit")

    def test_restore_never_deletes(self):
        name = self.create("items")["name"]
        extra = write(self.cfg / "skills" / "mine" / "SKILL.md", "---\n---\nmine\n")
        self.restore_all(name)
        self.assertTrue(extra.is_file())

    def test_paths_not_in_the_archive_fail_without_writing(self):
        name = self.create("config")["name"]
        res = backup.backup_restore(name, ["files/nope.md"])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["failed"][0]["error"], "not in this archive")
        self.assertFalse((self.cfg / "nope.md").exists())


class TestSafety(Base):
    def hand_built(self, member, data=b"pwned"):
        """An archive we did not write, listing `member` in its manifest."""
        self.dest.mkdir(parents=True, exist_ok=True)
        name = "evil.zip"
        with zipfile.ZipFile(self.dest / name, "w") as z:
            z.writestr(member, data)
            z.writestr("manifest.json", json.dumps({
                "format": 1, "entries": [
                    {"path": member, "group": "items", "size": len(data),
                     "mode": 0o644, "sha256": backup._sha(data)}]}))
        return name

    def test_traversal_and_absolute_members_are_refused(self):
        victim = self.tmp / "victim.txt"
        for member in ("files/../../victim.txt", "files//etc/victim.txt",
                       "/etc/victim.txt", "../victim.txt"):
            with self.subTest(member=member):
                name = self.hand_built(member)
                row = backup.backup_inspect(name)["entries"][0]
                self.assertIn(row["status"], ("refused", "new"))
                res = backup.backup_restore(name, [member])
                if row["status"] == "refused":
                    self.assertEqual(res["count"], 0, res)
                self.assertFalse(victim.exists())
                (self.dest / name).unlink()

    def test_a_symlink_out_of_the_config_dir_is_not_written_through(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("original\n")
        (self.cfg / "escape").symlink_to(outside)
        name = self.hand_built("files/escape", b"pwned")
        backup.backup_restore(name, ["files/escape"])
        self.assertEqual(outside.read_text(), "original\n")

    def test_a_newer_format_is_refused_rather_than_guessed_at(self):
        self.dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.dest / "future.zip", "w") as z:
            z.writestr("manifest.json", json.dumps({"format": 99, "entries": []}))
        with self.assertRaises(ValueError):
            backup.backup_inspect("future.zip")

    def test_a_zip_that_is_not_ours_is_refused(self):
        self.dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.dest / "plain.zip", "w") as z:
            z.writestr("hello.txt", "hi")
        with self.assertRaises(ValueError):
            backup.backup_inspect("plain.zip")

    def test_delete_only_reaches_inside_the_backup_dir(self):
        outside = self.tmp / "precious.zip"
        outside.write_text("x")
        with self.assertRaises(ValueError):
            backup.backup_delete("../precious.zip")
        self.assertTrue(outside.is_file())


class TestDestination(unittest.TestCase):
    """backup_dir() is the one path in this app that must not be inside the
    config dir — a backup that an uninstall deletes is not a backup."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self._cfg_file = core.CONFIG_FILE
        core.CONFIG_FILE = self.tmp / ".claude-ui.json"
        backup.CONFIG_FILE = core.CONFIG_FILE
        self._saved = [(m, m.config_dir) for m in (backup, core)]
        for m, _ in self._saved:
            m.config_dir = lambda c=self.tmp / "claude": c

    def tearDown(self):
        core.CONFIG_FILE = self._cfg_file
        backup.CONFIG_FILE = self._cfg_file
        for m, fn in self._saved:
            m.config_dir = fn
        self.tmpdir.cleanup()

    def test_default_is_outside_the_config_dir(self):
        d = backup.default_backup_dir()
        self.assertNotIn(str(self.tmp / "claude"), str(d))
        self.assertTrue(str(d).endswith(os.path.join("claude-ui", "backups")))

    def test_xdg_data_home_is_honoured(self):
        old = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "xdg")
        try:
            self.assertEqual(backup.default_backup_dir(),
                             self.tmp / "xdg" / "claude-ui" / "backups")
        finally:
            if old is None:
                del os.environ["XDG_DATA_HOME"]
            else:
                os.environ["XDG_DATA_HOME"] = old

    def test_a_path_inside_the_config_dir_is_refused(self):
        with self.assertRaises(ValueError):
            backup.set_backup_dir(str(self.tmp / "claude" / "backups"))

    def test_relative_paths_are_refused(self):
        with self.assertRaises(ValueError):
            backup.set_backup_dir("backups")

    def test_set_and_reset_round_trip(self):
        backup.set_backup_dir(str(self.tmp / "elsewhere"))
        self.assertEqual(backup.backup_dir(), self.tmp / "elsewhere")
        backup.set_backup_dir("")
        self.assertEqual(backup.backup_dir(), backup.default_backup_dir())


if __name__ == "__main__":
    unittest.main()
