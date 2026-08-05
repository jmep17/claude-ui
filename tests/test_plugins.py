"""Plugin inventory, splitting a plugin into config-dir items, and drift.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_plugins.py`.

config_dir is patched in every namespace that reads it (plugins reaches the
plugin tree through it, settings writes settings.json through it), since
core.config_dir() consults .claude-ui.json before $CLAUDE_CONFIG_DIR and so
can't be redirected by the environment alone. plugins_root() is a function over
config_dir() precisely so this works.
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import core, plugins, settings  # noqa: E402


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path

def md(description="", body="body"):
    return f"---\ndescription: {description}\n---\n{body}"


class Base(unittest.TestCase):
    """A temp config dir holding one marketplace with one 'demo' plugin."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self._saved = [(m, m.config_dir) for m in (plugins, settings, core)]
        for m, _ in self._saved:
            m.config_dir = lambda t=self.tmp: t
        self.plugin = self.tmp / "plugins" / "marketplaces" / "mkt" / "plugins" / "demo"
        write(self.plugin / ".claude-plugin" / "plugin.json",
              json.dumps({"name": "demo", "description": "a demo"}))
        write(self.plugin / "agents" / "alpha.md", md("Agent A"))
        write(self.plugin / "agents" / "beta.md", md("Agent B"))
        write(self.plugin / "commands" / "run.md", md("Command R"))

    def tearDown(self):
        for m, fn in self._saved:
            m.config_dir = fn
        self.tmpdir.cleanup()

    def split(self, *picks):
        plugins.plugins_split("demo@mkt", list(picks), disable=False)

    def adopted(self, name):
        return next(a for a in plugins.adopted_items() if a["name"] == name)

    def skill(self, name="helper", plugin=None):
        root = (plugin or self.plugin) / "skills" / name
        write(root / "SKILL.md", f"---\nname: {name}\ndescription: S\n---\nsk")
        write(root / "refs" / "note.md", "note")
        (root / "refs" / "blob.bin").write_bytes(b"\xff\xfe\x00ok")
        script = write(root / "run.sh", "#!/bin/sh\necho hi")
        script.chmod(0o755)
        return root

    def settings_json(self):
        return json.loads((self.tmp / "settings.json").read_text())

    def names(self, type_, ext=".md"):
        d = self.tmp / type_
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def one(self, pid="demo@mkt"):
        return next(p for p in plugins.plugins_state()["plugins"] if p["id"] == pid)


class TestScan(Base):
    def test_empty_when_no_plugins_dir(self):
        shutil.rmtree(self.tmp / "plugins")
        st = plugins.plugins_state()
        self.assertEqual(st["plugins"], [])
        self.assertIsNone(st["error"])

    def test_finds_plugin_under_marketplace(self):
        p = self.one()
        self.assertEqual(p["name"], "demo")
        self.assertEqual(p["marketplace"], "mkt")
        self.assertEqual(p["description"], "a demo")
        self.assertEqual(p["counts"], {"agents": 2, "commands": 1})

    def test_finds_plugin_under_repos(self):
        pdir = self.tmp / "plugins" / "repos" / "owner" / "repo"
        write(pdir / ".claude-plugin" / "plugin.json",
              json.dumps({"name": "fromgit", "description": "g"}))
        write(pdir / "agents" / "solo.md", md("Solo"))
        ids = {p["id"] for p in plugins.plugins_state()["plugins"]}
        self.assertIn("fromgit@owner", ids)

    def test_plugin_without_manifest_still_listed(self):
        """Every *-lsp plugin in the official marketplace ships no plugin.json."""
        bare = self.plugin.parent / "nolsp"
        write(bare / "commands" / "go.md", md("Go"))
        p = self.one("nolsp@mkt")
        self.assertEqual(p["counts"], {"commands": 1})

    def test_bad_plugin_json_does_not_raise(self):
        write(self.plugin / ".claude-plugin" / "plugin.json", "{not json")
        self.assertEqual(self.one()["description"], "")

    def test_bad_known_marketplaces_falls_back_to_listing(self):
        write(self.tmp / "plugins" / "known_marketplaces.json", "{not json")
        st = plugins.plugins_state()
        self.assertEqual([p["id"] for p in st["plugins"]], ["demo@mkt"])
        self.assertIsNotNone(st["error"])

    def test_known_marketplaces_install_location_is_honoured(self):
        elsewhere = self.tmp / "elsewhere"
        write(elsewhere / "plugins" / "other" / "agents" / "x.md", md("X"))
        write(self.tmp / "plugins" / "known_marketplaces.json", json.dumps(
            {"far": {"installLocation": str(elsewhere)}}))
        ids = {p["id"] for p in plugins.plugins_state()["plugins"]}
        self.assertEqual(ids, {"other@far"})

    def test_state_reflects_settings(self):
        self.assertEqual(self.one()["state"], "available")
        settings.settings_set("enabledPlugins", {"demo@mkt": True})
        self.assertEqual(self.one()["state"], "enabled")
        settings.settings_set("enabledPlugins", {"demo@mkt": False})
        self.assertEqual(self.one()["state"], "disabled")

    def test_description_from_frontmatter(self):
        c = next(c for c in self.one()["components"] if c["name"] == "alpha")
        self.assertEqual(c["description"], "Agent A")

    def test_hooks_are_listed_but_not_adoptable(self):
        write(self.plugin / "hooks" / "hooks.json", json.dumps(
            {"hooks": {"PreToolUse": []}}))
        c = next(c for c in self.one()["components"] if c["kind"] == "hooks")
        self.assertFalse(c["adoptable"])
        self.assertIn("CLAUDE_PLUGIN_ROOT", c["reason"])

    def test_mcp_server_adoptable_unless_it_needs_the_plugin_root(self):
        write(self.plugin / ".mcp.json", json.dumps({
            "clean": {"command": "npx", "args": ["-y", "pkg"]},
            "rooted": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/serve"}}))
        by = {c["name"]: c for c in self.one()["components"] if c["kind"] == "mcp"}
        self.assertTrue(by["clean"]["adoptable"])
        self.assertFalse(by["rooted"]["adoptable"])

    def test_plugin_root_reference_warns(self):
        write(self.plugin / "agents" / "gamma.md", md("G", "run ${CLAUDE_PLUGIN_ROOT}/x"))
        c = next(c for c in self.one()["components"] if c["name"] == "gamma")
        self.assertTrue(c["adoptable"])
        self.assertIn("CLAUDE_PLUGIN_ROOT", c["warn"])

    def test_conflict_is_reported_on_the_component(self):
        write(self.tmp / "agents" / "alpha.md", "MINE")
        c = next(c for c in self.one()["components"] if c["name"] == "alpha")
        self.assertIn("already have", c["conflict"])


class TestSplit(Base):
    def test_copies_only_ticked(self):
        r = plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}])
        self.assertEqual(r["kept"], 1)
        self.assertEqual(self.names("agents"), ["alpha.md"])
        self.assertEqual(self.names("commands"), [])

    def test_copies_skill_tree(self):
        self.skill()
        plugins.plugins_split("demo@mkt", [{"kind": "skills", "name": "helper"}])
        root = self.tmp / "skills" / "helper"
        got = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        self.assertEqual(got, ["SKILL.md", "refs/blob.bin", "refs/note.md", "run.sh"])

    def test_preserves_exec_bit(self):
        self.skill()
        plugins.plugins_split("demo@mkt", [{"kind": "skills", "name": "helper"}])
        self.assertTrue(os.access(self.tmp / "skills" / "helper" / "run.sh", os.X_OK))

    def test_binary_file_copied_unmangled(self):
        """Skills ship real binaries; read_text(errors='replace') would corrupt."""
        self.skill()
        plugins.plugins_split("demo@mkt", [{"kind": "skills", "name": "helper"}])
        blob = self.tmp / "skills" / "helper" / "refs" / "blob.bin"
        self.assertEqual(blob.read_bytes(), b"\xff\xfe\x00ok")

    def test_writes_provenance(self):
        self.skill()
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"},
                                           {"kind": "skills", "name": "helper"}])
        agent = core.parse_frontmatter((self.tmp / "agents" / "alpha.md").read_text())
        skill = core.parse_frontmatter(
            (self.tmp / "skills" / "helper" / "SKILL.md").read_text())
        self.assertEqual(agent[plugins.SOURCE_KEY], "demo@mkt/agents/alpha")
        self.assertEqual(skill[plugins.SOURCE_KEY], "demo@mkt/skills/helper")

    def test_provenance_added_to_a_file_with_no_frontmatter(self):
        write(self.plugin / "agents" / "bare.md", "just a body")
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "bare"}])
        text = (self.tmp / "agents" / "bare.md").read_text()
        self.assertEqual(core.parse_frontmatter(text)[plugins.SOURCE_KEY],
                         "demo@mkt/agents/bare")
        self.assertIn("just a body", text)

    def test_disables_the_plugin_and_leaves_siblings_alone(self):
        settings.settings_set("model", "opus")
        settings.settings_set("enabledPlugins", {"other@mkt": True})
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}])
        self.assertEqual(self.settings_json(), {
            "model": "opus",
            "enabledPlugins": {"other@mkt": True, "demo@mkt": False}})

    def test_disable_false_leaves_settings_alone(self):
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}],
                              disable=False)
        self.assertFalse((self.tmp / "settings.json").exists())

    def test_nested_command_name(self):
        write(self.plugin / "commands" / "git" / "pr.md", md("PR"))
        plugins.plugins_split("demo@mkt", [{"kind": "commands", "name": "git/pr"}])
        self.assertTrue((self.tmp / "commands" / "git" / "pr.md").is_file())

    def test_mcp_server_lands_in_the_machine_store(self):
        write(self.plugin / ".mcp.json", json.dumps(
            {"srv": {"command": "npx", "args": ["-y", "pkg"]}}))
        saved = plugins.mcp_machine_set
        calls = []
        plugins.mcp_machine_set = lambda n, c, enabled=True: calls.append((n, c))
        try:
            plugins.plugins_split("demo@mkt", [{"kind": "mcp", "name": "srv"}])
        finally:
            plugins.mcp_machine_set = saved
        self.assertEqual(calls, [("srv", {"command": "npx", "args": ["-y", "pkg"]})])


class TestRefusals(Base):
    """Every refusal must leave the machine exactly as it was."""

    def assertRefused(self, picks, fragment):
        with self.assertRaises(ValueError) as cm:
            plugins.plugins_split("demo@mkt", picks)
        self.assertIn(fragment, str(cm.exception))

    def test_live_collision_raises_and_copies_nothing(self):
        write(self.tmp / "agents" / "alpha.md", "MINE")
        self.assertRefused([{"kind": "agents", "name": "alpha"},
                            {"kind": "agents", "name": "beta"}], "already have")
        self.assertEqual((self.tmp / "agents" / "alpha.md").read_text(), "MINE")
        self.assertEqual(self.names("agents"), ["alpha.md"])

    def test_disabled_collision_raises(self):
        write(self.tmp / "disabled" / "agents" / "alpha.md", "PARKED")
        self.assertRefused([{"kind": "agents", "name": "alpha"}], "disabled/")
        self.assertEqual(self.names("agents"), [])

    def test_symlink_in_skill_tree_raises(self):
        root = self.skill()
        (root / "escape").symlink_to("/etc/hosts")
        self.assertRefused([{"kind": "skills", "name": "helper"}], "symlink")
        self.assertFalse((self.tmp / "skills").exists())

    def test_oversize_file_raises(self):
        root = self.skill()
        (root / "big.txt").write_text("x" * (plugins.MAX_BYTES + 1))
        self.assertRefused([{"kind": "skills", "name": "helper"}], "file limit")

    def test_too_many_files_raises(self):
        root = self.skill()
        for i in range(plugins.MAX_FILES + 1):
            (root / f"f{i}.txt").write_text("x")
        self.assertRefused([{"kind": "skills", "name": "helper"}], "file limit")

    def test_bad_name_rejected(self):
        for bad in ("../../etc/passwd", "a/b", ".hidden", ""):
            self.assertRefused([{"kind": "agents", "name": bad}], "no agents component")

    def test_unknown_plugin_raises(self):
        with self.assertRaises(ValueError) as cm:
            plugins.plugins_split("ghost@mkt", [{"kind": "agents", "name": "alpha"}])
        self.assertIn("unknown plugin", str(cm.exception))

    def test_nothing_selected_raises(self):
        self.assertRefused([], "nothing selected")

    def test_hooks_cannot_be_split_out(self):
        write(self.plugin / "hooks" / "hooks.json", json.dumps({"hooks": {}}))
        self.assertRefused([{"kind": "hooks", "name": "hooks"}], "CLAUDE_PLUGIN_ROOT")

    def test_corrupt_settings_json_is_not_clobbered(self):
        write(self.tmp / "settings.json", "{not json")
        self.assertRefused([{"kind": "agents", "name": "alpha"}], "fix it by hand")
        self.assertEqual((self.tmp / "settings.json").read_text(), "{not json")
        self.assertEqual(self.names("agents"), [])

    def test_no_staging_dirs_left_behind(self):
        self.skill()
        plugins.plugins_split("demo@mkt", [{"kind": "skills", "name": "helper"}])
        leftover = [p.name for p in (self.tmp / "skills").iterdir()
                    if p.name.startswith(".")]
        self.assertEqual(leftover, [])

    def test_toggle_refuses_a_bad_plugin_id(self):
        with self.assertRaises(ValueError):
            plugins.plugin_set_enabled("nomarketplace", False)


class TestSkillOverrides(Base):
    def test_set_and_clear(self):
        plugins.skill_override_set("helper", "off")
        self.assertEqual(self.settings_json(), {"skillOverrides": {"helper": "off"}})
        plugins.skill_override_set("helper", None)
        self.assertEqual(self.settings_json(), {})

    def test_keeps_other_entries(self):
        plugins.skill_override_set("a", "off")
        plugins.skill_override_set("b", "name-only")
        self.assertEqual(self.settings_json()["skillOverrides"],
                         {"a": "off", "b": "name-only"})

    def test_rejects_a_bad_value(self):
        with self.assertRaises(ValueError):
            plugins.skill_override_set("helper", "sometimes")

    def test_rejects_a_bad_name(self):
        with self.assertRaises(ValueError):
            plugins.skill_override_set("../evil", "off")


class TestDrift(Base):
    def test_no_drift_when_identical(self):
        self.skill()
        self.split({"kind": "agents", "name": "alpha"},
                   {"kind": "skills", "name": "helper"})
        for name in ("alpha", "helper"):
            a = self.adopted(name)
            self.assertFalse(a["drift"], name)
            self.assertFalse(a["missing"], name)

    def test_provenance_line_is_not_itself_drift(self):
        """Without _strip_source every adopted item drifts the moment it lands."""
        self.split({"kind": "agents", "name": "alpha"})
        ours = (self.tmp / "agents" / "alpha.md").read_text()
        theirs = (self.plugin / "agents" / "alpha.md").read_text()
        self.assertNotEqual(ours, theirs)
        self.assertFalse(self.adopted("alpha")["drift"])

    def test_drift_when_upstream_changes(self):
        self.split({"kind": "agents", "name": "alpha"})
        write(self.plugin / "agents" / "alpha.md", md("Agent A", "changed upstream"))
        self.assertTrue(self.adopted("alpha")["drift"])

    def test_drift_when_we_edit_our_copy(self):
        self.split({"kind": "agents", "name": "alpha"})
        p = self.tmp / "agents" / "alpha.md"
        p.write_text(p.read_text() + "\nlocal edit")
        self.assertTrue(self.adopted("alpha")["drift"])

    def test_drift_when_a_skill_file_is_added(self):
        self.skill()
        self.split({"kind": "skills", "name": "helper"})
        write(self.tmp / "skills" / "helper" / "extra.md", "extra")
        self.assertTrue(self.adopted("helper")["drift"])

    def test_missing_source_reported(self):
        self.split({"kind": "agents", "name": "alpha"})
        (self.plugin / "agents" / "alpha.md").unlink()
        self.assertTrue(self.adopted("alpha")["missing"])

    def test_items_without_provenance_are_not_listed(self):
        write(self.tmp / "agents" / "mine.md", md("Mine"))
        self.assertEqual([a["name"] for a in plugins.adopted_items()], [])

    def test_resync_overwrites_local(self):
        self.split({"kind": "agents", "name": "alpha"})
        p = self.tmp / "agents" / "alpha.md"
        p.write_text(p.read_text() + "\nlocal edit")
        plugins.plugin_resync("agents", "alpha")
        self.assertNotIn("local edit", p.read_text())
        self.assertFalse(self.adopted("alpha")["drift"])

    def test_resync_a_skill_replaces_the_tree(self):
        self.skill()
        self.split({"kind": "skills", "name": "helper"})
        write(self.tmp / "skills" / "helper" / "stray.md", "stray")
        plugins.plugin_resync("skills", "helper")
        self.assertFalse((self.tmp / "skills" / "helper" / "stray.md").exists())
        self.assertFalse(self.adopted("helper")["drift"])

    def test_resync_without_provenance_raises(self):
        write(self.tmp / "agents" / "mine.md", md("Mine"))
        with self.assertRaises(ValueError) as cm:
            plugins.plugin_resync("agents", "mine")
        self.assertIn(plugins.SOURCE_KEY, str(cm.exception))

    def test_resync_of_a_missing_item_raises(self):
        with self.assertRaises(ValueError):
            plugins.plugin_resync("agents", "ghost")


class TestSourcePath(Base):
    """source_path is what the UI opens to show you the plugin's own copy, so
    it has to be an absolute, readable *file* — a skill is a directory."""

    def test_points_at_a_readable_file(self):
        self.split({"kind": "agents", "name": "alpha"})
        p = self.adopted("alpha")["source_path"]
        self.assertTrue(os.path.isabs(p))
        self.assertTrue(pathlib.Path(p).is_file())

    def test_a_skill_points_at_its_skill_md(self):
        self.skill()
        self.split({"kind": "skills", "name": "helper"})
        p = pathlib.Path(self.adopted("helper")["source_path"])
        self.assertEqual(p.name, "SKILL.md")
        self.assertTrue(p.is_file())

    def test_empty_when_the_source_is_gone(self):
        self.split({"kind": "agents", "name": "alpha"})
        (self.plugin / "agents" / "alpha.md").unlink()
        a = self.adopted("alpha")
        self.assertTrue(a["missing"])
        self.assertEqual(a["source_path"], "")

    def test_is_not_tilded(self):
        """tilde() is a display transform; reopening a tilde'd path fails."""
        self.split({"kind": "agents", "name": "alpha"})
        self.assertFalse(self.adopted("alpha")["source_path"].startswith("~"))


class TestItemSource(Base):
    """scan_items reports the plugin an item was split from, so the inventory
    can say so where you actually edit it."""

    def setUp(self):
        super().setUp()
        from claude_ui import items
        self.items = items
        self._items_cfg = items.config_dir
        items.config_dir = lambda t=self.tmp: t

    def tearDown(self):
        self.items.config_dir = self._items_cfg
        super().tearDown()

    def test_adopted_item_reports_its_source(self):
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}],
                              disable=False)
        it = next(i for i in self.items.scan_items("agents") if i["name"] == "alpha")
        self.assertEqual(it["source"], "demo@mkt/agents/alpha")

    def test_a_hand_written_item_has_no_source(self):
        write(self.tmp / "agents" / "mine.md", md("Mine"))
        it = next(i for i in self.items.scan_items("agents") if i["name"] == "mine")
        self.assertEqual(it["source"], "")

    def test_adopted_skill_reports_its_source(self):
        self.skill()
        plugins.plugins_split("demo@mkt", [{"kind": "skills", "name": "helper"}],
                              disable=False)
        it = next(i for i in self.items.scan_items("skills") if i["name"] == "helper")
        self.assertEqual(it["source"], "demo@mkt/skills/helper")


if __name__ == "__main__":
    unittest.main()
