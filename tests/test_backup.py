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


class TestUnits(Base):
    """Units are what the pick list ticks: one skill, one file, one server."""

    def units(self, group):
        plan = {g["id"]: g for g in backup.backup_plan()}
        return {u["id"]: u for u in plan[group]["units"]}

    def test_each_item_is_its_own_unit_with_its_files_counted(self):
        u = self.units("items")
        self.assertEqual(set(u), {"skills/pdf", "commands/old"})
        self.assertEqual(u["skills/pdf"]["files"], 2)   # SKILL.md + logo.bin
        self.assertEqual(u["skills/pdf"]["label"], "pdf")
        self.assertEqual(u["skills/pdf"]["desc"], "skill")
        self.assertIn("disabled", u["commands/old"]["desc"])

    def test_a_file_no_item_claims_is_still_backed_up(self):
        """An item scan is a view of the type directories, not an inventory of
        them. Anything it does not model must still land in the archive."""
        write(self.cfg / "commands" / "notes.txt", "not a command\n")
        write(self.cfg / "skills" / "loose.md", "not a skill\n")
        u = self.units("items")
        self.assertEqual(u["other"]["files"], 2)
        names = self.members(self.create("items")["name"])
        self.assertIn("files/commands/notes.txt", names)
        self.assertIn("files/skills/loose.md", names)

    def test_the_catch_all_does_not_duplicate_files_an_item_claims(self):
        entries = backup._g_items()
        paths = [e["path"] for e in entries]
        self.assertEqual(len(paths), len(set(paths)))
        skill = "files/skills/pdf/SKILL.md"
        self.assertEqual([e["unit"] for e in entries if e["path"] == skill],
                         ["skills/pdf"])

    def test_config_statusline_and_mcp_split_by_the_obvious_thing(self):
        self.assertEqual(set(self.units("config")), {"CLAUDE.md", "settings.json"})
        self.assertEqual(set(self.units("statusline")), {"statusline.sh"})
        self.assertEqual(set(self.units("mcp")), {"gh"})

    def test_transcripts_are_one_unit_per_project(self):
        write(self.cfg / "projects" / "other" / "b.jsonl", '{"usage": 2}\n')
        self.assertEqual(set(self.units("transcripts")), {"proj", "other"})

    def test_a_unit_subset_writes_only_that_unit(self):
        write(self.cfg / "skills" / "mine" / "SKILL.md", "---\n---\nmine\n")
        res = backup.backup_create(["items"], "", {"items": ["skills/mine"]})
        self.assertEqual(self.members(res["name"]),
                         {"manifest.json", "files/skills/mine/SKILL.md"})

    def test_a_group_left_out_of_the_subset_still_takes_everything(self):
        res = backup.backup_create(["items", "config"], "",
                                   {"items": ["skills/pdf"]})
        names = self.members(res["name"])
        self.assertIn("files/CLAUDE.md", names)
        self.assertIn("files/settings.json", names)
        self.assertNotIn("files/disabled/commands/old.md", names)

    def test_one_server_can_be_archived_and_restored_on_its_own(self):
        self.claude_json.write_text(json.dumps({"mcpServers": {
            "gh": {"command": "gh-mcp"}, "fs": {"command": "fs-mcp"}}}))
        res = backup.backup_create(["mcp"], "", {"mcp": ["fs"]})
        self.assertEqual(self.members(res["name"]),
                         {"manifest.json", backup.MCP_PREFIX + "fs.json"})
        self.claude_json.write_text(json.dumps({"mcpServers": {}}))
        backup.backup_restore(res["name"], [backup.MCP_PREFIX + "fs.json"])
        live = json.loads(self.claude_json.read_text())
        self.assertEqual(list(live["mcpServers"]), ["fs"])

    def test_a_subset_that_matches_nothing_writes_an_empty_archive(self):
        res = backup.backup_create(["items"], "", {"items": ["skills/gone"]})
        self.assertEqual(res["files"], 0)


class TestCreate(Base):
    def test_archive_holds_files_and_a_manifest(self):
        res = self.create()
        names = self.members(res["name"])
        self.assertIn("manifest.json", names)
        self.assertIn("files/CLAUDE.md", names)
        self.assertIn("files/skills/pdf/SKILL.md", names)
        self.assertIn("files/disabled/commands/old.md", names)
        self.assertIn("files/projects/proj/a.jsonl", names)
        self.assertIn(backup.MCP_PREFIX + "gh.json", names)

    def test_whole_claude_json_is_never_copied(self):
        """Only the mcpServers map: that file also holds history and the account."""
        res = self.create()
        with zipfile.ZipFile(self.dest / res["name"]) as z:
            blob = json.loads(z.read(backup.MCP_PREFIX + "gh.json"))
        self.assertEqual(list(blob), ["mcpServers"])
        self.assertEqual(list(blob["mcpServers"]), ["gh"])
        joined = "".join(self.members(res["name"]))
        self.assertNotIn("oauthAccount", joined)

    def test_each_server_is_its_own_member(self):
        """One blob for the lot could not be picked apart on the way in or out."""
        self.claude_json.write_text(json.dumps({"mcpServers": {
            "gh": {"command": "gh-mcp"}, "fs": {"command": "fs-mcp"}}}))
        names = self.members(self.create("mcp")["name"])
        self.assertIn(backup.MCP_PREFIX + "gh.json", names)
        self.assertIn(backup.MCP_PREFIX + "fs.json", names)
        self.assertNotIn(backup.MCP_MEMBER, names)

    def test_a_server_name_we_could_not_write_back_is_skipped(self):
        self.claude_json.write_text(json.dumps({"mcpServers": {
            "ok": {"command": "x"}, "../evil": {"command": "y"}}}))
        names = self.members(self.create("mcp")["name"])
        self.assertEqual([n for n in names if n.startswith(backup.MCP_PREFIX)],
                         [backup.MCP_PREFIX + "ok.json"])

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
        mcp_member = backup.MCP_PREFIX + "gh.json"
        self.assertEqual({v for k, v in st.items() if k != mcp_member}, {"new"})
        self.assertEqual(st[mcp_member], "same")
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
        backup.backup_restore(name, [backup.MCP_PREFIX + "gh.json"])
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

    def test_a_format_1_archive_still_inspects_and_restores(self):
        """Archives written before MCP servers became one member each hold the
        whole map in mcp/mcpServers.json. That shape is read, never written."""
        blob = (json.dumps({"mcpServers": {"old": {"command": "old-mcp"}}},
                           indent=2) + "\n").encode()
        self.dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.dest / "v1.zip", "w") as z:
            z.writestr(backup.MCP_MEMBER, blob)
            z.writestr("manifest.json", json.dumps({
                "format": 1, "groups": ["mcp"], "entries": [
                    {"path": backup.MCP_MEMBER, "group": "mcp", "size": len(blob),
                     "mode": None, "sha256": backup._sha(blob)}]}))
        row = backup.backup_inspect("v1.zip")["entries"][0]
        self.assertEqual(row["status"], "differs")
        self.assertTrue(row["target"].endswith("mcpServers"))
        backup.backup_restore("v1.zip", [backup.MCP_MEMBER])
        live = json.loads(self.claude_json.read_text())
        self.assertEqual(live["mcpServers"]["old"]["command"], "old-mcp")
        self.assertIn("gh", live["mcpServers"])          # merged, not replaced

    def test_a_crafted_server_member_cannot_name_its_own_key(self):
        for member in (backup.MCP_PREFIX + "../../pwned.json",
                       backup.MCP_PREFIX + "no-suffix",
                       backup.MCP_PREFIX + ".json"):
            with self.subTest(member=member):
                name = self.hand_built(member, b'{"mcpServers": {"x": {}}}')
                self.assertEqual(
                    backup.backup_inspect(name)["entries"][0]["status"], "refused")
                self.assertEqual(backup.backup_restore(name, [member])["count"], 0)
                (self.dest / name).unlink()

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
