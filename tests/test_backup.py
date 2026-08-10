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

    def test_inspect_rows_carry_the_unit_so_a_skill_is_one_tick(self):
        """Restore groups its rows by the unit recorded at create time — the
        whole pdf skill is one checkbox, not one per file inside it."""
        rows = {e["path"]: e
                for e in backup.backup_inspect(self.create()["name"])["entries"]}
        self.assertEqual(rows["files/skills/pdf/SKILL.md"]["unit"], "skills/pdf")
        self.assertEqual(rows["files/skills/pdf/logo.bin"]["unit"], "skills/pdf")
        self.assertEqual(rows["files/skills/pdf/SKILL.md"]["unit_label"], "pdf")
        self.assertEqual(rows["files/skills/pdf/SKILL.md"]["unit_desc"], "skill")
        self.assertEqual(rows["mcp/servers/gh.json"]["unit"], "gh")

    def test_an_archive_without_units_still_inspects(self):
        """Archives from before units were recorded in the manifest fall back
        to empty unit fields — one row per file, never an error."""
        name = self.create()["name"]
        path = self.dest / name
        with zipfile.ZipFile(path) as z:
            m = json.loads(z.read("manifest.json"))
            blobs = {n: z.read(n) for n in z.namelist() if n != "manifest.json"}
        for e in m["entries"]:
            for k in ("unit", "unit_label", "unit_desc"):
                e.pop(k, None)
        with zipfile.ZipFile(path, "w") as z:
            for n, b in blobs.items():
                z.writestr(n, b)
            z.writestr("manifest.json", json.dumps(m))
        rows = backup.backup_inspect(name)["entries"]
        self.assertTrue(rows)
        self.assertTrue(all(r["unit"] == "" for r in rows))

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


class TestProjectRestore(Base):
    """Restoring into <project>/.claude/ instead of the config dir.

    Two properties carry the feature: only a registered project can be written
    to at all, and only the three item types a project directory can hold are
    ever offered — decided from the member's own path, never from what the
    manifest claims the member is.
    """

    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "proj"
        (self.proj / ".claude").mkdir(parents=True)
        self.other = self.tmp / "other"
        self.other.mkdir()
        self.register(self.proj)
        write(self.cfg / "skills" / "pdf" / "scripts" / "run.sh", "#!/bin/sh\necho hi\n")
        (self.cfg / "skills" / "pdf" / "scripts" / "run.sh").chmod(0o755)
        write(self.cfg / "commands" / "git" / "pr.md", "---\n---\nopen a pr\n")

    def register(self, *roots):
        # the registry is a plain file in the config dir, which Base already
        # patches — no need to pull projects.py into this harness
        (self.cfg / core.PROJECTS_REGISTRY).write_text(
            "".join(f"{r.resolve()}\n" for r in roots))

    def cdir(self):
        return self.proj / ".claude"

    def rows(self, name, root=None):
        rep = backup.project_restore_inspect(str(root or self.proj), name)
        return {e["path"]: e for e in rep["entries"]}, rep

    def restore_all(self, name):
        rows, _ = self.rows(name)
        return backup.project_restore(str(self.proj), name, list(rows))

    def test_a_skill_lands_in_the_project_not_the_config_dir(self):
        name = self.create("items")["name"]
        before = sorted(p.name for p in self.cfg.iterdir())
        self.restore_all(name)
        self.assertEqual((self.cdir() / "skills" / "pdf" / "SKILL.md").read_text(),
                         (self.cfg / "skills" / "pdf" / "SKILL.md").read_text())
        self.assertEqual(sorted(p.name for p in self.cfg.iterdir()), before)

    def test_an_archived_disabled_item_becomes_an_ordinary_project_item(self):
        """A project has no disabled/ area — that is this app's own parking
        place inside the config dir, and it must not be recreated in a repo."""
        name = self.create("items")["name"]
        self.restore_all(name)
        self.assertTrue((self.cdir() / "commands" / "old.md").is_file())
        self.assertFalse((self.cdir() / "disabled").exists())

    def test_a_nested_command_keeps_its_path_and_its_own_unit(self):
        name = self.create("items")["name"]
        rows, _ = self.rows(name)
        self.assertEqual(rows["files/commands/git/pr.md"]["unit"], "commands/git/pr")
        self.assertEqual(rows["files/disabled/commands/old.md"]["unit"], "commands/old")
        self.restore_all(name)
        self.assertTrue((self.cdir() / "commands" / "git" / "pr.md").is_file())

    def test_the_executable_bit_survives_into_a_project(self):
        name = self.create("items")["name"]
        self.restore_all(name)
        p = self.cdir() / "skills" / "pdf" / "scripts" / "run.sh"
        self.assertTrue(os.access(p, os.X_OK))

    def test_only_skills_commands_and_agents_are_offered(self):
        name = self.create()["name"]                     # every group
        rows, _ = self.rows(name)
        self.assertTrue(rows)
        for path in rows:
            self.assertRegex(path, r"^files/(disabled/)?(skills|commands|agents)/")
        self.assertNotIn("files/settings.json", rows)
        self.assertNotIn(backup.MCP_PREFIX + "gh.json", rows)

    def test_a_non_item_member_is_never_written(self):
        name = self.create()["name"]
        res = backup.project_restore(str(self.proj), name, [
            "files/settings.json", "files/CLAUDE.md", backup.MCP_PREFIX + "gh.json"])
        self.assertEqual(res["count"], 0, res)
        self.assertEqual(res["failed_count"], 3)
        self.assertFalse((self.cdir() / "settings.json").exists())
        self.assertEqual(json.loads(self.claude_json.read_text())["mcpServers"]["gh"]
                         ["env"]["TOKEN"], "sekrit")

    def test_an_unregistered_project_is_refused(self):
        name = self.create("items")["name"]
        for call in (lambda: backup.project_restore_inspect(str(self.other), name),
                     lambda: backup.project_restore(str(self.other), name,
                                                    ["files/skills/pdf/SKILL.md"])):
            with self.assertRaises(ValueError) as cm:
                call()
            self.assertIn("not a registered project", str(cm.exception))
        self.assertFalse((self.other / ".claude").exists())

    def test_a_claude_dir_that_is_a_symlink_is_refused(self):
        """The case containment cannot catch: resolving the target and the base
        both go through the symlink, so they agree. It has to be its own rule."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.cdir()).rmdir()
        self.cdir().symlink_to(elsewhere)
        name = self.create("items")["name"]
        with self.assertRaises(ValueError) as cm:
            backup.project_restore(str(self.proj), name, ["files/skills/pdf/SKILL.md"])
        self.assertIn("symlink", str(cm.exception))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_symlink_inside_the_project_is_not_written_through(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("original\n")
        d = self.cdir() / "skills" / "pdf"
        d.mkdir(parents=True)
        (d / "SKILL.md").symlink_to(outside)
        name = self.create("items")["name"]
        rows, _ = self.rows(name)
        self.assertEqual(rows["files/skills/pdf/SKILL.md"]["status"], "refused")
        backup.project_restore(str(self.proj), name, ["files/skills/pdf/SKILL.md"])
        self.assertEqual(outside.read_text(), "original\n")

    def test_traversal_and_absolute_members_are_refused(self):
        victim = self.tmp / "victim.txt"
        for member in ("files/skills/../../../victim.txt", "files/skills",
                       "files/skills/pdf", "/files/skills/pdf/x.md",
                       "files/commands/notes.txt", "files/output-styles/x.md"):
            with self.subTest(member=member):
                with self.assertRaises(ValueError):
                    backup._project_member(member)
                self.assertFalse(victim.exists())

    def test_a_crafted_unit_label_cannot_redirect_the_write(self):
        """The unit comes from the path. An archive we did not write gets to
        claim settings.json is the pdf skill; it does not get to land there."""
        data = b"pwned"
        self.dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.dest / "evil.zip", "w") as z:
            z.writestr("files/settings.json", data)
            z.writestr("manifest.json", json.dumps({
                "format": 2, "entries": [
                    {"path": "files/settings.json", "group": "items",
                     "size": len(data), "mode": 0o644, "sha256": backup._sha(data),
                     "unit": "skills/pdf", "unit_label": "pdf",
                     "unit_desc": "skill"}]}))
        rows, _ = self.rows("evil.zip")
        self.assertEqual(rows, {})
        res = backup.project_restore(str(self.proj), "evil.zip", ["files/settings.json"])
        self.assertEqual(res["count"], 0, res)

    def test_inspect_reports_new_same_and_differs_with_a_diff(self):
        name = self.create("items")["name"]
        _, rep = self.rows(name)
        self.assertEqual(set(rep["counts"]), {"new"})
        self.assertEqual(rep["present"]["skills"], [])

        self.restore_all(name)
        rows, rep = self.rows(name)
        self.assertEqual(set(rep["counts"]), {"same"})
        self.assertEqual(rep["present"]["skills"], ["pdf"])
        self.assertEqual(rep["present"]["commands"], ["git/pr", "old"])

        (self.cdir() / "skills" / "pdf" / "SKILL.md").write_text("edited\n")
        rows, rep = self.rows(name)
        row = rows["files/skills/pdf/SKILL.md"]
        self.assertEqual(row["status"], "differs")
        self.assertIn("edited", row["diff"])

    def test_a_file_in_the_way_is_refused_before_writing(self):
        write(self.cdir() / "skills", "not a directory\n")
        name = self.create("items")["name"]
        rows, _ = self.rows(name)
        row = rows["files/skills/pdf/SKILL.md"]
        self.assertEqual(row["status"], "refused")
        self.assertIn("is a file", row["error"])
        self.assertEqual((self.cdir() / "skills").read_text(), "not a directory\n")

    def test_restore_never_deletes_what_the_archive_does_not_carry(self):
        name = self.create("items")["name"]
        self.restore_all(name)
        notes = self.cdir() / "skills" / "pdf" / "notes.md"
        notes.write_text("mine\n")
        self.restore_all(name)
        self.assertEqual(notes.read_text(), "mine\n")

    def test_only_the_picked_members_are_written(self):
        name = self.create("items")["name"]
        backup.project_restore(str(self.proj), name, ["files/skills/pdf/SKILL.md"])
        self.assertTrue((self.cdir() / "skills" / "pdf" / "SKILL.md").is_file())
        self.assertFalse((self.cdir() / "commands").exists())

    def test_an_archive_without_units_still_offers_its_items(self):
        """Units are derived from the path here, so an archive written before
        they were recorded restores into a project exactly the same."""
        data = b"---\nname: pdf\n---\nbody\n"
        self.dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.dest / "old.zip", "w") as z:
            z.writestr("files/skills/pdf/SKILL.md", data)
            z.writestr("manifest.json", json.dumps({
                "format": 1, "entries": [
                    {"path": "files/skills/pdf/SKILL.md", "group": "items",
                     "size": len(data), "mode": 0o644, "sha256": backup._sha(data)}]}))
        rows, _ = self.rows("old.zip")
        self.assertEqual(rows["files/skills/pdf/SKILL.md"]["unit"], "skills/pdf")
        self.assertEqual(rows["files/skills/pdf/SKILL.md"]["unit_label"], "pdf")
        backup.project_restore(str(self.proj), "old.zip", ["files/skills/pdf/SKILL.md"])
        self.assertEqual((self.cdir() / "skills" / "pdf" / "SKILL.md").read_bytes(), data)

    def test_two_projects_do_not_see_each_others_restores(self):
        second = self.tmp / "second"
        (second / ".claude").mkdir(parents=True)
        self.register(self.proj, second)
        name = self.create("items")["name"]
        self.restore_all(name)
        self.assertTrue((self.cdir() / "skills" / "pdf").is_dir())
        self.assertFalse((second / ".claude" / "skills").exists())

    def test_the_archive_name_gate_still_applies(self):
        with self.assertRaises(ValueError):
            backup.project_restore_inspect(str(self.proj), "../../evil.zip")

    def test_an_empty_selection_is_refused(self):
        name = self.create("items")["name"]
        with self.assertRaises(ValueError):
            backup.project_restore(str(self.proj), name, [])


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


class TestFreshStart(Base):
    """Snapshot first, targeted deletion second, login always intact."""

    def test_reset_deletes_the_modelled_slice_and_nothing_else(self):
        write(self.cfg / "todos" / "keep.json", "{}")
        write(self.cfg / ".credentials.json", '{"token": "keep"}')
        write(self.cfg / "plugins" / "config.json", "{}")
        write(self.cfg / "keybindings.json", "{}")
        backup.reset_config()
        for gone in ("skills", "disabled", "plugins", "CLAUDE.md",
                     "settings.json", "keybindings.json", "statusline.sh"):
            self.assertFalse((self.cfg / gone).exists(), gone)
        self.assertTrue((self.cfg / "todos" / "keep.json").is_file())
        self.assertTrue((self.cfg / ".credentials.json").is_file())

    def test_reset_pops_mcp_servers_and_keeps_the_login(self):
        result = backup.reset_config()
        data = json.loads(self.claude_json.read_text())
        self.assertNotIn("mcpServers", data)
        self.assertEqual(data["oauthAccount"]["email"], "me@example.com")
        self.assertEqual(data["projects"], {"/tmp/x": {"history": []}})
        self.assertEqual(result["mcp_cleared"], 1)

    def test_reset_is_a_noop_on_a_missing_or_serverless_claude_json(self):
        self.claude_json.unlink()
        self.assertEqual(backup.reset_config()["mcp_cleared"], 0)
        self.assertFalse(self.claude_json.exists())
        self.claude_json.write_text(json.dumps({"oauthAccount": {}}))
        backup.reset_config()
        self.assertEqual(json.loads(self.claude_json.read_text()),
                         {"oauthAccount": {}})

    def test_a_corrupt_claude_json_is_reported_not_clobbered(self):
        self.claude_json.write_text("{nope")
        result = backup.reset_config()
        self.assertEqual(self.claude_json.read_text(), "{nope")
        self.assertTrue(any("mcpServers not cleared" in f["error"]
                            for f in result["failed"]))

    def test_transcripts_survive_by_default_and_go_when_asked(self):
        backup.reset_config(keep_transcripts=True)
        self.assertTrue((self.cfg / "projects" / "proj" / "a.jsonl").is_file())
        backup.reset_config(keep_transcripts=False)
        self.assertFalse((self.cfg / "projects").exists())

    def test_a_symlinked_item_dir_loses_the_pointer_not_the_target(self):
        target = self.tmp / "checkout"
        write(target / "mine.md", "---\n---\nreal file\n")
        (self.cfg / "commands").symlink_to(target)
        backup.reset_config()
        self.assertFalse((self.cfg / "commands").is_symlink())
        self.assertTrue((target / "mine.md").is_file())

    def test_a_dangerous_config_dir_is_refused(self):
        backup.config_dir = lambda: pathlib.Path("/")
        with self.assertRaises(ValueError):
            backup.reset_config()

    def test_fresh_start_snapshots_then_resets(self):
        result = backup.fresh_start()
        self.assertFalse((self.cfg / "skills").exists())
        self.assertNotIn("mcpServers", json.loads(self.claude_json.read_text()))
        names = self.members(result["snapshot"])
        self.assertIn("files/skills/pdf/SKILL.md", names)
        self.assertIn("mcp/servers/gh.json", names)
        # transcripts stayed on disk, so the snapshot leaves them out
        self.assertNotIn("files/projects/proj/a.jsonl", names)
        self.assertTrue((self.cfg / "projects" / "proj" / "a.jsonl").is_file())

    def test_deleted_transcripts_ride_in_the_snapshot(self):
        result = backup.fresh_start(keep_transcripts=False)
        self.assertFalse((self.cfg / "projects").exists())
        self.assertIn("files/projects/proj/a.jsonl",
                      self.members(result["snapshot"]))

    def test_nothing_is_deleted_when_the_snapshot_cannot_be_written(self):
        self.dest.write_text("a file where the backup dir should be")
        with self.assertRaises((ValueError, OSError)):
            backup.fresh_start()
        self.assertTrue((self.cfg / "skills" / "pdf" / "SKILL.md").is_file())
        self.assertIn("mcpServers", json.loads(self.claude_json.read_text()))

    def test_the_round_trip_restores_what_was_picked_and_only_that(self):
        result = backup.fresh_start()
        picked = ["files/skills/pdf/SKILL.md", "files/skills/pdf/logo.bin",
                  "mcp/servers/gh.json", "files/CLAUDE.md"]
        r = backup.backup_restore(result["snapshot"], picked)
        self.assertEqual(r["failed"], [])
        self.assertEqual((self.cfg / "CLAUDE.md").read_text(), "be brief\n")
        self.assertTrue((self.cfg / "skills" / "pdf" / "logo.bin").is_file())
        data = json.loads(self.claude_json.read_text())
        self.assertEqual(data["mcpServers"]["gh"]["command"], "gh-mcp")
        self.assertEqual(data["oauthAccount"]["email"], "me@example.com")
        # what was not picked stays gone
        self.assertFalse((self.cfg / "settings.json").exists())
        self.assertFalse((self.cfg / "disabled").exists())


if __name__ == "__main__":
    unittest.main()
