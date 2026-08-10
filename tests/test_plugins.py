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
    """skillOverrides reaches your own skills, and only those.

    The docs are explicit — "Plugin skills are not affected by skillOverrides.
    Manage those through /plugin instead." — and a plugin's skill answers to
    plugin-name:skill-name, a key this cannot even spell. The Plugins tab used
    to offer this per plugin skill; what it actually wrote was an entry naming
    a skill of the user's own.
    """

    def test_a_namespaced_plugin_skill_name_cannot_be_written(self):
        """The refusal that makes the mistake impossible to repeat: NAME_RE has
        no colon, so there is no way to spell the key a plugin skill would need
        even if one worked."""
        for name in ("myplugin:helper", "myplugin:", ":helper"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    plugins.skill_override_set(name, "off")
        self.assertFalse((self.tmp / "settings.json").exists())

    def test_an_entry_names_your_skill_not_a_plugins(self):
        """Written for a plugin's `helper`, the entry is indistinguishable from
        one written for your own `helper` — which is the whole bug, and why the
        button that did it is gone."""
        plugins.skill_override_set("helper", "off")
        self.assertEqual(self.settings_json()["skillOverrides"], {"helper": "off"})

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


class TestCatalogueSource(Base):
    """A marketplace's catalogue says where its plugins live. Scanning
    plugins/ finds the common layout; it cannot find "source": "./", where the
    marketplace root is itself the plugin."""

    def catalogue(self, mroot, entries):
        write(mroot / ".claude-plugin" / "marketplace.json",
              json.dumps({"name": mroot.name, "plugins": entries}))

    def test_a_root_source_plugin_is_found(self):
        mroot = self.tmp / "plugins" / "marketplaces" / "self"
        write(mroot / "agents" / "solo.md", md("Solo"))
        self.catalogue(mroot, [{"name": "self", "description": "d", "source": "./"}])
        p = self.one("self@self")
        self.assertEqual(p["description"], "d")
        self.assertEqual(p["counts"], {"agents": 1})

    def test_a_catalogued_subdir_is_not_listed_twice(self):
        self.catalogue(self.plugin.parent.parent,
                       [{"name": "demo", "description": "a demo",
                         "source": "./plugins/demo"}])
        ids = [p["id"] for p in plugins.plugins_state()["plugins"]]
        self.assertEqual(ids.count("demo@mkt"), 1)

    def test_the_catalogue_wins_over_a_lookalike_subdir(self):
        """caveman's repo ships both a root plugin and a plugins/caveman dir
        from another distribution; only the catalogued one is real."""
        mroot = self.tmp / "plugins" / "marketplaces" / "two"
        write(mroot / "agents" / "real.md", md("Real"))
        write(mroot / "plugins" / "two" / "agents" / "decoy.md", md("Decoy"))
        self.catalogue(mroot, [{"name": "two", "source": "./"}])
        p = self.one("two@two")
        self.assertEqual([c["name"] for c in p["components"]], ["real"])

    def test_a_git_source_is_ignored(self):
        self.catalogue(self.plugin.parent.parent,
                       [{"name": "remote", "source": {"source": "github",
                                                      "repo": "a/b"}}])
        ids = {p["id"] for p in plugins.plugins_state()["plugins"]}
        self.assertNotIn("remote@mkt", ids)

    def test_a_source_escaping_the_marketplace_is_ignored(self):
        mroot = self.tmp / "plugins" / "marketplaces" / "esc"
        self.catalogue(mroot, [{"name": "esc", "source": "../../../.."}])
        ids = {p["id"] for p in plugins.plugins_state()["plugins"]}
        self.assertNotIn("esc@esc", ids)


class TestAgentModel(Base):
    """An agent's model: line — reported everywhere, and settable on the copy
    that is ours. The plugin's own file is never written."""

    def agent(self, name="alpha"):
        return next(c for c in self.one()["components"]
                    if c["kind"] == "agents" and c["name"] == name)

    def test_an_agents_model_is_reported(self):
        write(self.plugin / "agents" / "alpha.md",
              "---\ndescription: A\nmodel: haiku\n---\nbody")
        self.assertEqual(self.agent()["model"], "haiku")

    def test_no_model_line_reports_empty(self):
        self.assertEqual(self.agent()["model"], "")

    def test_only_agents_carry_a_model(self):
        self.skill()
        for c in self.one()["components"]:
            if c["kind"] != "agents":
                self.assertEqual(c["model"], "", c["name"])

    def test_split_writes_the_chosen_model(self):
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}],
                              disable=False, models={"alpha": "opus"})
        text = (self.tmp / "agents" / "alpha.md").read_text()
        self.assertIn("model: opus", text)
        self.assertIn("x-claude-ui-source: demo@mkt/agents/alpha", text)

    def test_split_replaces_the_plugins_model_rather_than_adding_one(self):
        write(self.plugin / "agents" / "alpha.md",
              "---\ndescription: A\nmodel: haiku\n---\nbody")
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}],
                              disable=False, models={"alpha": "opus"})
        text = (self.tmp / "agents" / "alpha.md").read_text()
        self.assertEqual(text.count("model:"), 1)
        self.assertNotIn("haiku", text)

    def test_split_leaves_the_plugins_own_copy_alone(self):
        src = self.plugin / "agents" / "alpha.md"
        before = src.read_text()
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}],
                              disable=False, models={"alpha": "opus"})
        self.assertEqual(src.read_text(), before)

    def test_an_adopted_agent_reports_its_model(self):
        plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}],
                              disable=False, models={"alpha": "opus"})
        self.assertEqual(self.adopted("alpha")["model"], "opus")

    def test_a_bad_model_is_refused_before_anything_is_copied(self):
        with self.assertRaises(ValueError):
            plugins.plugins_split("demo@mkt", [{"kind": "agents", "name": "alpha"}],
                                  disable=False, models={"alpha": "opus\nname: x"})
        self.assertEqual(self.names("agents"), [])


class TestPluginEnvVars(Base):
    """The env vars a plugin reads, found by reading it. A guess, so the bar is
    that the guess is short and includes the ones that matter."""

    def hook(self, text, name="hook.js"):
        write(self.plugin / "src" / name, text)

    def test_finds_a_direct_read(self):
        self.hook("const m = process.env.DEMO_MODEL || 'x';")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertIn("DEMO_MODEL", names)

    def test_finds_a_name_held_in_a_table(self):
        """cavecrew's model overrides go through a list of literals; no
        process.env.X pattern can see them."""
        self.hook("const MAP = [{envVar: 'DEMO_REVIEWER_MODEL'}];\n"
                  "for (const e of MAP) v = process.env[e.envVar];")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertIn("DEMO_REVIEWER_MODEL", names)

    def test_a_literal_in_a_file_that_never_touches_the_env_is_ignored(self):
        self.hook("const HEADERS = ['CONTENT_TYPE_JSON'];")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertNotIn("CONTENT_TYPE_JSON", names)

    def test_a_python_read_is_found(self):
        write(self.plugin / "src" / "x.py", "import os\nos.environ.get('DEMO_FLAG')\n")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertIn("DEMO_FLAG", names)

    def test_a_shell_local_is_not_an_env_var(self):
        write(self.plugin / "src" / "i.sh",
              "#!/bin/sh\nDEMO_TMPDIR=/tmp\necho $DEMO_TMPDIR $DEMO_REAL\n")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertIn("DEMO_REAL", names)
        self.assertNotIn("DEMO_TMPDIR", names)

    def test_claude_codes_own_vars_are_left_to_the_settings_tab(self):
        self.hook("process.env.CLAUDE_PLUGIN_ROOT; process.env.CLAUDE_CODE_SUBAGENT_MODEL;")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", names)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", names)

    def test_generic_names_are_dropped(self):
        self.hook("process.env.PATH; process.env.HOME; process.env.NODE_ENV;")
        self.assertEqual(plugins.plugin_env_vars("demo@mkt"), [])

    def test_markdown_never_contributes_a_name(self):
        write(self.plugin / "README.md", "Run with $DEMO_PROSE set to something.")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertNotIn("DEMO_PROSE", names)

    def test_tests_and_evals_are_skipped(self):
        write(self.plugin / "tests" / "t.js", "process.env.DEMO_TEST_ONLY;")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertNotIn("DEMO_TEST_ONLY", names)

    def test_node_modules_is_skipped(self):
        write(self.plugin / "node_modules" / "dep" / "i.js", "process.env.DEP_SECRET;")
        names = [e["name"] for e in plugins.plugin_env_vars("demo@mkt")]
        self.assertNotIn("DEP_SECRET", names)

    def test_model_vars_are_flagged_and_come_first(self):
        self.hook("process.env.DEMO_FLAG; process.env.DEMO_MODEL;")
        got = plugins.plugin_env_vars("demo@mkt")
        self.assertEqual(got[0]["name"], "DEMO_MODEL")
        self.assertTrue(got[0]["model"])
        self.assertFalse(got[1]["model"])

    def test_the_current_value_comes_from_settings(self):
        self.hook("process.env.DEMO_MODEL;")
        settings.settings_set("env.DEMO_MODEL", "sonnet")
        got = plugins.plugin_env_vars("demo@mkt")
        self.assertEqual(got[0]["value"], "sonnet")

    def test_a_doc_line_is_attached_and_a_table_row_is_flattened(self):
        self.hook("process.env.DEMO_MODEL;")
        write(self.plugin / "README.md",
              "| Env var | Agent |\n|---|---|\n| `DEMO_MODEL` | `demo-agent` |\n")
        doc = plugins.plugin_env_vars("demo@mkt")[0]["doc"]
        self.assertEqual(doc["line"], "DEMO_MODEL → demo-agent")
        self.assertEqual(doc["file"], "README.md")

    def test_no_env_vars_is_an_empty_list_not_an_error(self):
        self.assertEqual(plugins.plugin_env_vars("demo@mkt"), [])

    def test_an_unknown_plugin_is_refused(self):
        with self.assertRaises(ValueError):
            plugins.plugin_env_vars("nope@mkt")


class TestPluginEnvSet(Base):
    def test_sets_and_clears_under_env(self):
        plugins.plugin_env_set("DEMO_MODEL", "sonnet")
        self.assertEqual(self.settings_json(), {"env": {"DEMO_MODEL": "sonnet"}})
        plugins.plugin_env_set("DEMO_MODEL", "")
        self.assertEqual(self.settings_json(), {})

    def test_a_name_that_is_not_an_env_var_is_refused(self):
        for bad in ("", "lower", "a.b", "X Y", "env.X"):
            with self.assertRaises(ValueError, msg=bad):
                plugins.plugin_env_set(bad, "v")


class TestScopeMoves(Base):
    """Where a plugin's enablement is recorded, and moving it between stores.

    projects.py owns the project settings files, so it is patched here too —
    project_setting_set resolves the root through the registry, which lives in
    the config dir this suite redirects."""

    def setUp(self):
        super().setUp()
        from claude_ui import projects
        self.projects = projects
        self._p_config_dir = projects.config_dir
        projects.config_dir = core.config_dir
        self.proj = self.tmp.parent / (self.tmp.name + "-proj")
        (self.proj / ".claude").mkdir(parents=True)
        projects.registry_add(str(self.proj))

    def tearDown(self):
        self.projects.config_dir = self._p_config_dir
        shutil.rmtree(self.proj, ignore_errors=True)
        super().tearDown()

    def user(self):
        return {"scope": "user"}

    def end(self, scope):
        return {"scope": scope, "root": str(self.proj)}

    def pfile(self, local=False):
        return (self.proj / ".claude"
                / ("settings.local.json" if local else "settings.json"))

    def pdata(self, local=False):
        p = self.pfile(local)
        return json.loads(p.read_text()) if p.is_file() else {}

    def test_entries_see_all_three_stores(self):
        settings.settings_set("enabledPlugins", {"demo@mkt": True})
        write(self.pfile(), json.dumps({"enabledPlugins": {"other@mkt": True}}))
        write(self.pfile(local=True),
              json.dumps({"enabledPlugins": {"third@mkt": False}}))
        got = plugins._plugin_scope_entries()
        self.assertEqual(got["demo@mkt"], [
            {"scope": "user", "root": None, "raw_root": None, "enabled": True}])
        self.assertEqual(got["other@mkt"][0]["scope"], "project")
        self.assertEqual(got["third@mkt"][0]["scope"], "local")
        self.assertFalse(got["third@mkt"][0]["enabled"])

    def test_state_carries_entries_without_changing_enabled(self):
        write(self.pfile(), json.dumps({"enabledPlugins": {"demo@mkt": True}}))
        p = next(p for p in plugins.plugins_state()["plugins"]
                 if p["id"] == "demo@mkt")
        # the user store has no answer, so the tab's own sections are unmoved
        self.assertEqual(p["state"], "available")
        self.assertFalse(p["enabled"])
        self.assertEqual([e["scope"] for e in p["entries"]], ["project"])

    def test_user_to_project_and_back(self):
        settings.settings_set("enabledPlugins", {"demo@mkt": True})
        plugins.plugin_scope_move("demo@mkt", self.user(), self.end("project"))
        self.assertEqual(self.pdata()["enabledPlugins"], {"demo@mkt": True})
        # an emptied map takes the key with it
        self.assertNotIn("enabledPlugins", settings.settings_state()["data"])
        plugins.plugin_scope_move("demo@mkt", self.end("project"), self.user())
        self.assertEqual(settings.settings_state()["data"]["enabledPlugins"],
                         {"demo@mkt": True})
        self.assertNotIn("enabledPlugins", self.pdata())

    def test_project_to_local_carries_a_false_verbatim(self):
        write(self.pfile(), json.dumps({"enabledPlugins": {"demo@mkt": False}}))
        plugins.plugin_scope_move("demo@mkt", self.end("project"), self.end("local"))
        self.assertEqual(self.pdata(local=True)["enabledPlugins"],
                         {"demo@mkt": False})
        self.assertNotIn("enabledPlugins", self.pdata())

    def test_other_entries_and_other_keys_survive(self):
        settings.settings_set("enabledPlugins",
                              {"demo@mkt": True, "keep@mkt": False})
        write(self.pfile(), json.dumps({"outputStyle": "terse"}))
        plugins.plugin_scope_move("demo@mkt", self.user(), self.end("project"))
        self.assertEqual(settings.settings_state()["data"]["enabledPlugins"],
                         {"keep@mkt": False})
        self.assertEqual(self.pdata()["outputStyle"], "terse")

    def test_a_destination_that_already_answers_refuses(self):
        settings.settings_set("enabledPlugins", {"demo@mkt": True})
        write(self.pfile(), json.dumps({"enabledPlugins": {"demo@mkt": False}}))
        with self.assertRaises(ValueError):
            plugins.plugin_scope_move("demo@mkt", self.user(), self.end("project"))
        self.assertEqual(settings.settings_state()["data"]["enabledPlugins"],
                         {"demo@mkt": True})
        self.assertEqual(self.pdata()["enabledPlugins"], {"demo@mkt": False})

    def test_refusals_missing_same_place_bad_id_bad_scope_unregistered(self):
        settings.settings_set("enabledPlugins", {"demo@mkt": True})
        with self.assertRaises(ValueError):   # not recorded at the source
            plugins.plugin_scope_move("nope@mkt", self.user(), self.end("project"))
        with self.assertRaises(ValueError):
            plugins.plugin_scope_move("demo@mkt", self.user(), self.user())
        with self.assertRaises(ValueError):   # an id is always name@marketplace
            plugins.plugin_scope_move("demo", self.user(), self.end("project"))
        with self.assertRaises(ValueError):
            plugins.plugin_scope_move("demo@mkt", self.user(), {"scope": "global"})
        other = self.tmp.parent / (self.tmp.name + "-other")
        other.mkdir()
        try:
            with self.assertRaises(ValueError):
                plugins.plugin_scope_move("demo@mkt", self.user(),
                                          {"scope": "project", "root": str(other)})
        finally:
            shutil.rmtree(other, ignore_errors=True)
        self.assertEqual(settings.settings_state()["data"]["enabledPlugins"],
                         {"demo@mkt": True})

    def test_bad_json_in_either_store_refuses_before_any_write(self):
        settings.settings_set("enabledPlugins", {"demo@mkt": True})
        write(self.pfile(), "{not json")
        with self.assertRaises(ValueError):
            plugins.plugin_scope_move("demo@mkt", self.user(), self.end("project"))
        self.assertEqual(self.pfile().read_text(), "{not json")
        self.assertEqual(settings.settings_state()["data"]["enabledPlugins"],
                         {"demo@mkt": True})


if __name__ == "__main__":
    unittest.main()
