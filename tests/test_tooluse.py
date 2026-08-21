"""Per-tool usage counting and the permissions.deny-based off switch.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from
the repo root, or just `python3 tests/test_tooluse.py`.

The write tests point settings.config_dir at a temp directory (patched in the
`settings` namespace, same as tests/test_settings.py) and mcp's two file
anchors at the same temp tree, so nothing here reads or writes the real
~/.claude or ~/.claude.json.
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import insight, mcp, settings, tooluse  # noqa: E402


class ScanTools(unittest.TestCase):
    """insight._scan_transcript records every tool_use name under kind "tool"."""

    def scan(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        with open(path, "w") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")
        try:
            return insight._scan_transcript(path)
        finally:
            os.unlink(path)

    @staticmethod
    def tool_line(name, inp=None, ts="2026-07-30T11:25:27.932Z"):
        return {"timestamp": ts,
                "message": {"content": [{"type": "tool_use", "name": name,
                                         "input": inp or {}}]}}

    def test_every_tool_use_is_counted(self):
        counts = self.scan([self.tool_line("WebSearch"),
                            self.tool_line("WebSearch"),
                            self.tool_line("mcp__github__get_me")])["counts"]
        self.assertEqual(counts["tool\tWebSearch"][0], 2)
        self.assertEqual(counts["tool\tmcp__github__get_me"][0], 1)

    def test_no_other_line_marker_needed(self):
        """Regression guard on the scan's line prefilter: a tool_use line
        carrying no usage, no Skill/Task/Bash name and no cwd must still be
        scanned — '"tool_use"' itself is the marker."""
        line = self.tool_line("WebFetch")
        raw = json.dumps(line)
        for marker in ('"usage"', "command-name", '"cwd"'):
            self.assertNotIn(marker, raw)
        counts = self.scan([line])["counts"]
        self.assertEqual(counts["tool\tWebFetch"][0], 1)

    def test_special_kinds_still_counted(self):
        st = self.scan([self.tool_line("Skill", {"skill": "pdf"}),
                        self.tool_line("Task", {"subagent_type": "Explore"}),
                        self.tool_line("Bash", {"command": "git status"})])
        self.assertEqual(st["counts"]["skill\tpdf"][0], 1)
        self.assertEqual(st["counts"]["agent\tExplore"][0], 1)
        self.assertEqual(st["counts"]["tool\tBash"][0], 1)
        self.assertEqual(st["bash"]["git status"], 1)

    def test_nameless_block_is_skipped(self):
        counts = self.scan([self.tool_line(""),
                            {"timestamp": "2026-07-30T11:25:27.932Z",
                             "message": {"content": [{"type": "tool_use",
                                                      "name": 7}]}}])["counts"]
        self.assertEqual([k for k in counts if k.startswith("tool\t")], [])

    def test_cache_version_covers_the_new_kind(self):
        """The "tool" kind joined the cached per-file counts in v8; an older
        cache must be discarded wholesale, not read with the kind missing."""
        self.assertGreaterEqual(insight.CACHE_V, 8)


class TmpConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self._config_dir = settings.config_dir
        settings.config_dir = lambda: root
        self._claude_json = mcp.CLAUDE_JSON
        mcp.CLAUDE_JSON = root / "claude.json"
        self._disabled_dir = mcp.disabled_dir
        mcp.disabled_dir = lambda: root / "disabled"
        self.settings_path = root / "settings.json"

    def tearDown(self):
        settings.config_dir = self._config_dir
        mcp.CLAUDE_JSON = self._claude_json
        mcp.disabled_dir = self._disabled_dir
        self.tmp.cleanup()

    def read(self):
        if not self.settings_path.is_file():
            return {}
        return json.loads(self.settings_path.read_text())

    def write(self, data):
        self.settings_path.write_text(json.dumps(data))


class ToolSwitch(TmpConfig):
    """tool_set_enabled edits exactly one bare deny entry, nothing else."""

    def test_off_appends_a_bare_deny(self):
        tooluse.tool_set_enabled("WebSearch", False)
        self.assertEqual(self.read()["permissions"]["deny"], ["WebSearch"])
        tooluse.tool_set_enabled("WebSearch", False)   # idempotent
        self.assertEqual(self.read()["permissions"]["deny"], ["WebSearch"])

    def test_on_removes_only_that_entry(self):
        self.write({"permissions": {"deny": ["WebSearch", "Bash(git push:*)",
                                             "NotebookEdit"]}})
        tooluse.tool_set_enabled("WebSearch", True)
        self.assertEqual(self.read()["permissions"]["deny"],
                         ["Bash(git push:*)", "NotebookEdit"])

    def test_last_entry_removes_the_key(self):
        """settings_set prunes the emptied deny list and its emptied parent,
        so the file reads as if the switch was never used."""
        self.write({"permissions": {"deny": ["WebSearch"]}, "model": "opus"})
        tooluse.tool_set_enabled("WebSearch", True)
        self.assertEqual(self.read(), {"model": "opus"})

    def test_filter_rules_are_never_touched(self):
        """A Read(~/.ssh/**) rule is deliberate policy: turning the Read tool
        off adds a separate bare entry, and turning it back on removes only
        that entry."""
        self.write({"permissions": {"deny": ["Read(~/.ssh/**)"]}})
        tooluse.tool_set_enabled("Read", False)
        self.assertEqual(self.read()["permissions"]["deny"],
                         ["Read(~/.ssh/**)", "Read"])
        tooluse.tool_set_enabled("Read", True)
        self.assertEqual(self.read()["permissions"]["deny"],
                         ["Read(~/.ssh/**)"])

    def test_on_without_an_entry_is_a_noop(self):
        self.write({"permissions": {"deny": ["Bash(git:*)"]}})
        tooluse.tool_set_enabled("WebSearch", True)
        self.assertEqual(self.read()["permissions"]["deny"], ["Bash(git:*)"])

    def test_bad_names_are_refused(self):
        for bad in ("", None, "Web Search", "Bash(rm:*)", "-x", "a" * 200):
            with self.assertRaises(ValueError):
                tooluse.tool_set_enabled(bad, False)

    def test_broken_settings_are_refused(self):
        self.settings_path.write_text("{nope")
        with self.assertRaises(ValueError):
            tooluse.tool_set_enabled("WebSearch", False)
        self.write({"permissions": {"deny": "WebSearch"}})
        with self.assertRaises(ValueError):
            tooluse.tool_set_enabled("WebSearch", False)


class Report(TmpConfig):
    """tools_report joins the histogram, the deny list and the MCP inventory."""

    BY = {"Read": {"count": 7, "last": "2026-07-30T00:00:00Z"},
          "FooTool": {"count": 2, "last": "2026-07-01T00:00:00Z"},
          "mcp__github__get_me": {"count": 3, "last": "2026-07-02T00:00:00Z"},
          "mcp__github__create_pr": {"count": 1, "last": "2026-07-03T00:00:00Z"},
          "mcp__gone__x": {"count": 5, "last": "2026-07-04T00:00:00Z"}}

    def test_join(self):
        self.write({"permissions": {"deny": ["WebSearch", "Bash(git:*)"]}})
        mcp.CLAUDE_JSON.write_text(json.dumps(
            {"mcpServers": {"github": {"command": "github-mcp"}}}))
        rep = tooluse.tools_report(dict(self.BY))
        rows = {r["name"]: r for r in rep["builtin"]}
        self.assertTrue(rows["WebSearch"]["denied"])
        # an argument-filter rule gates calls but leaves the tool loaded —
        # it must not read as "off"
        self.assertFalse(rows["Bash"]["denied"])
        self.assertEqual(rows["Read"]["count"], 7)
        self.assertTrue(rows["Read"]["core"])
        # observed but uncatalogued: still a row, still switchable
        self.assertIn("FooTool", rows)
        self.assertFalse(rows["FooTool"]["core"])
        # mcp__ names never masquerade as built-ins
        self.assertNotIn("mcp__github__get_me", rows)
        servers = {s["name"]: s for s in rep["mcp"]}
        self.assertEqual(servers["github"]["scope"], "user")
        self.assertTrue(servers["github"]["enabled"])
        self.assertEqual(servers["github"]["count"], 4)
        self.assertEqual(servers["github"]["tools"],
                         [("get_me", 3), ("create_pr", 1)])
        # seen in transcripts, absent from ~/.claude.json: reported, untogglable
        self.assertEqual(servers["gone"]["scope"], "other")
        self.assertIsNone(servers["gone"]["enabled"])
        self.assertEqual(servers["gone"]["count"], 5)
        self.assertEqual(rep["deny"], ["WebSearch"])

    def test_disabled_server_and_empty_histogram(self):
        (pathlib.Path(self.tmp.name) / "disabled").mkdir()
        (pathlib.Path(self.tmp.name) / "disabled" / mcp.MCP_FILE).write_text(
            json.dumps({"mcpServers": {"parked": {"command": "x"}}}))
        rep = tooluse.tools_report(None)
        names = [r["name"] for r in rep["builtin"]]
        for expected in ("WebSearch", "Bash", "Task"):
            self.assertIn(expected, names)
        (srv,) = rep["mcp"]
        self.assertEqual((srv["name"], srv["enabled"], srv["count"]),
                         ("parked", False, 0))

    def test_broken_settings_still_reports(self):
        """The report is read-only, so a broken settings.json degrades to
        "nothing denied" plus the error — never an exception."""
        self.settings_path.write_text("{nope")
        rep = tooluse.tools_report({})
        self.assertTrue(rep["settings_error"])
        self.assertEqual(rep["deny"], [])


if __name__ == "__main__":
    unittest.main()
