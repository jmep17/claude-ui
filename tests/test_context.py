"""The Context tab's backend: import resolution, inventory, measured stats,
pointers.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_context.py`.

config_dir is patched in every namespace that reaches the filesystem (core,
items, insight, context), because each module bound the name at import time.
The measured tests replace transcript_stats with a synthetic table, the same
no-disk style test_costs.py uses.
"""

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import context, core, insight, items  # noqa: E402


class Base(unittest.TestCase):
    """A temp config dir standing in for ~/.claude, in all four namespaces."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self._saved = [(m, m.config_dir)
                       for m in (context, core, items, insight)]
        for m, _ in self._saved:
            m.config_dir = lambda t=self.tmp: t

    def tearDown(self):
        for m, fn in self._saved:
            m.config_dir = fn
        self.tmpdir.cleanup()

    def write(self, rel, text):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class ImportRefs(unittest.TestCase):
    """_import_refs finds @path tokens and nothing else."""

    def test_line_start_and_after_whitespace(self):
        self.assertEqual(context._import_refs("@a.md\nsee @b.md here"),
                         ["a.md", "b.md"])

    def test_mid_word_at_is_not_an_import(self):
        self.assertEqual(context._import_refs("mail me at jo@example.com"), [])

    def test_fenced_code_is_skipped(self):
        text = "@real.md\n```\n@fenced.md\n```\n@after.md\n"
        self.assertEqual(context._import_refs(text), ["real.md", "after.md"])

    def test_trailing_punctuation_stripped(self):
        self.assertEqual(context._import_refs("see @docs/style.md."),
                         ["docs/style.md"])


class Imports(Base):
    """_md_entry resolves imports recursively, bounded and cycle-safe."""

    def entry(self, rel="CLAUDE.md"):
        return context._md_entry(self.tmp / rel)

    def test_relative_import_is_counted(self):
        self.write("CLAUDE.md", "root\n@extra.md\n")
        self.write("extra.md", "x" * 40)
        e = self.entry()
        self.assertEqual(len(e["imports"]), 1)
        imp = e["imports"][0]
        self.assertTrue(imp["resolved"])
        self.assertEqual(imp["chars"], 40)
        self.assertEqual(e["total_tok"], e["tok"] + imp["tok"])

    def test_absolute_and_tilde_are_expanded(self):
        target = self.write("abs.md", "abs")
        self.write("CLAUDE.md", f"@{target}\n@~/nowhere.md\n")
        e = self.entry()
        self.assertTrue(e["imports"][0]["resolved"])
        # ~ expanded, not taken literally
        self.assertNotIn("~", e["imports"][1]["path"])
        self.assertFalse(e["imports"][1]["resolved"])

    def test_missing_import_gets_unresolved_row(self):
        self.write("CLAUDE.md", "@gone.md\n")
        e = self.entry()
        self.assertEqual(
            [(i["resolved"], i["chars"]) for i in e["imports"]], [(False, 0)])
        self.assertEqual(e["total_tok"], e["tok"])

    def test_same_file_counted_once(self):
        self.write("CLAUDE.md", "@extra.md\n@extra.md\n")
        self.write("extra.md", "x")
        self.assertEqual(len(self.entry()["imports"]), 1)

    def test_cycle_stops(self):
        self.write("CLAUDE.md", "@a.md\n")
        self.write("a.md", "@b.md\n")
        self.write("b.md", "@a.md\n")
        e = self.entry()
        self.assertEqual([i["ref"] for i in e["imports"]],
                         ["@a.md", "@b.md"])

    def test_depth_capped_at_five_hops(self):
        self.write("CLAUDE.md", "@f1.md\n")
        for i in range(1, 8):
            self.write(f"f{i}.md", f"@f{i + 1}.md\n")
        self.write("f8.md", "leaf")
        refs = [i["ref"] for i in self.entry()["imports"]]
        self.assertEqual(refs, ["@f1.md", "@f2.md", "@f3.md", "@f4.md",
                                "@f5.md"])

    def test_missing_file_entry(self):
        e = self.entry()
        self.assertFalse(e["exists"])
        self.assertEqual((e["chars"], e["tok"], e["total_tok"], e["imports"]),
                         (0, 0, 0, []))


class UserScope(Base):
    """The user scope folds items, plugins and MCP into one estimate."""

    def setUp(self):
        super().setUp()
        self._plug = context.plugins_state
        self._mcp = context.mcp_state
        context.plugins_state = lambda: {"plugins": []}
        context.mcp_state = lambda: {"servers": []}

    def tearDown(self):
        context.plugins_state = self._plug
        context.mcp_state = self._mcp
        super().tearDown()

    def test_est_tok_is_claude_md_plus_listings(self):
        self.write("CLAUDE.md", "x" * 40)                       # 10 tok
        self.write("skills/deploy/SKILL.md",
                   "---\ndescription: " + "d" * 78 + "\n---\nbody\n")
        sc = context._user_scope()
        skill = sc["types"]["skills"]["items"][0]
        # name "deploy" -> 2, description 78 chars -> 20
        self.assertEqual(skill["listing_tok"], 2 + 20)
        self.assertGreater(skill["file_chars"], 0)
        self.assertEqual(sc["est_tok"], 10 + 22)

    def test_disabled_item_rides_along_at_zero(self):
        self.write("disabled/skills/parked/SKILL.md",
                   "---\ndescription: hidden\n---\n")
        sc = context._user_scope()
        rows = sc["types"]["skills"]["items"]
        self.assertEqual([(r["enabled"], r["listing_tok"]) for r in rows],
                         [(False, 0)])
        self.assertEqual(sc["types"]["skills"]["listing_tok"], 0)

    def test_enabled_plugin_components_are_counted(self):
        context.plugins_state = lambda: {"plugins": [
            {"name": "acme", "enabled": True, "components": [
                {"kind": "skills", "name": "fmt", "description": "d" * 38}]},
            {"name": "off", "enabled": False, "components": [
                {"kind": "skills", "name": "x", "description": "y" * 400}]}]}
        sc = context._user_scope()
        rows = sc["types"]["plugins"]["items"]
        self.assertEqual([r["name"] for r in rows], ["acme:fmt"])
        self.assertEqual(sc["types"]["plugins"]["listing_tok"],
                         2 + 10)  # "acme:fmt" -> 2, 38 chars -> 10

    def test_mcp_servers_report_size_not_tokens(self):
        context.mcp_state = lambda: {"servers": [
            {"name": "gh", "enabled": True, "config": {"command": "gh-mcp"}},
            {"name": "web", "enabled": False,
             "config": {"type": "http", "url": "https://x"}}]}
        sc = context._user_scope()
        self.assertEqual(sc["mcp"]["count"], 2)
        by = {s["name"]: s for s in sc["mcp"]["servers"]}
        self.assertEqual(by["gh"]["transport"], "stdio")
        self.assertEqual(by["web"]["transport"], "http")
        self.assertGreater(by["gh"]["config_chars"], 0)
        # and none of it leaks into the estimate
        self.assertEqual(sc["est_tok"], 0)


class ProjectScope(Base):
    """A project scope adds CLAUDE.local.md and auto-memory."""

    def setUp(self):
        super().setUp()
        self.projdir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.projdir.name)
        self._pm = context.project_mcp_state

    def tearDown(self):
        context.project_mcp_state = self._pm
        self.projdir.cleanup()
        super().tearDown()

    def slug(self):
        return context.re.sub(r"[^A-Za-z0-9]", "-", str(self.root))

    def test_both_md_files_and_memory_in_estimate(self):
        (self.root / "CLAUDE.md").write_text("x" * 40)          # 10 tok
        (self.root / "CLAUDE.local.md").write_text("y" * 20)    # 5 tok
        self.write(f"projects/{self.slug()}/memory/MEMORY.md", "m" * 40)  # 10
        self.write(f"projects/{self.slug()}/memory/topic.md", "t" * 999)
        sc = context._project_scope(self.root, {})
        self.assertEqual([m["name"] for m in sc["claude_md"]],
                         ["CLAUDE.md", "CLAUDE.local.md"])
        self.assertEqual(sc["memory"]["memory_tok"], 10)
        # topic files are on-demand: listed, sized, not in the estimate
        self.assertEqual(sc["memory"]["topics_chars"], 999)
        self.assertEqual(sc["est_tok"], 10 + 5 + 10)

    def test_slug_map_wins_over_the_guess(self):
        self.write("projects/real-slug/memory/MEMORY.md", "m" * 4)
        sc = context._project_scope(self.root, {str(self.root): "real-slug"})
        self.assertEqual(sc["memory"]["memory_tok"], 1)

    def test_unregistered_root_never_raises(self):
        # project_mcp_state refuses unregistered roots; the scope shrugs
        sc = context._project_scope(self.root, {})
        self.assertEqual(sc["mcp"]["servers"], [])
        self.assertIsNone(sc["memory"])

    def test_project_items_are_scanned_from_dot_claude(self):
        (self.root / ".claude" / "commands").mkdir(parents=True)
        (self.root / ".claude" / "commands" / "ship.md").write_text(
            "---\ndescription: " + "d" * 38 + "\n---\n")
        sc = context._project_scope(self.root, {})
        self.assertEqual(sc["types"]["commands"]["listing_tok"],
                         1 + 10)  # "ship" -> 1, 38 chars -> 10


def sess(first=(100, 30000, 0), max_cr=50000, first_ts="2026-08-01T10:00:00Z",
         last_ts="2026-08-01T11:00:00Z", model="claude-opus-5"):
    return {"first": list(first), "max_cr": max_cr,
            "first_ts": first_ts, "last_ts": last_ts, "model": model}


def srow(path, cwd, s=None, msgs=10):
    return {"path": path, "cwd": cwd, "sess": s or sess(), "msgs": msgs}


class Measured(unittest.TestCase):
    """_measured aggregates synthetic session rows per project."""

    PDIR = "/cfg/projects"

    def setUp(self):
        self._pd = context.projects_dir
        context.projects_dir = lambda: pathlib.Path(self.PDIR)

    def tearDown(self):
        context.projects_dir = self._pd

    def stats(self, rows, projects=None):
        return {"session_rows": rows, "projects": projects or {},
                "sessions": len(rows), "scanned_now": 0,
                "dir": "~/.claude/projects", "available": True}

    def test_median_min_max_and_peak(self):
        rows = [srow(f"{self.PDIR}/slug/{i}.jsonl", "/p",
                     sess(first=(0, b, 0), max_cr=b * 2))
                for i, b in enumerate((10000, 30000, 20000))]
        m = context._measured(self.stats(rows), [])
        p = m["projects"][0]
        self.assertEqual((p["base_med"], p["base_min"], p["base_max"]),
                         (20000, 10000, 30000))
        self.assertEqual(p["peak_max"], 60000)
        self.assertEqual(p["sessions"], 3)

    def test_baseline_sums_input_writes_and_reads(self):
        rows = [srow(f"{self.PDIR}/slug/a.jsonl", "/p",
                     sess(first=(5, 30000, 12000)))]
        m = context._measured(self.stats(rows), [])
        self.assertEqual(m["projects"][0]["base_med"], 42005)

    def test_subagent_transcripts_only_counted(self):
        rows = [srow(f"{self.PDIR}/slug/a.jsonl", "/p"),
                srow(f"{self.PDIR}/slug/a-uuid/subagents/b.jsonl", "/p",
                     sess(first=(0, 999999, 0)))]
        m = context._measured(self.stats(rows), [])
        p = m["projects"][0]
        self.assertEqual((p["sessions"], p["subagents"]), (1, 1))
        # the subagent's usage never pollutes the baseline
        self.assertEqual(p["base_max"], 30100)
        self.assertEqual(len(m["sessions"]["/p"]), 1)

    def test_registered_covers_subdirectories(self):
        rows = [srow(f"{self.PDIR}/s1/a.jsonl", "/repo"),
                srow(f"{self.PDIR}/s2/b.jsonl", "/repo/sub/dir"),
                srow(f"{self.PDIR}/s3/c.jsonl", "/elsewhere")]
        m = context._measured(self.stats(rows), [pathlib.Path("/repo")])
        reg = {p["cwd"]: p["registered"] for p in m["projects"]}
        self.assertEqual(reg, {"/repo": True, "/repo/sub/dir": True,
                               "/elsewhere": False})

    def test_cache_spend_prices_reads_only(self):
        key = insight._rate_key("claude-opus-5", 1.0)
        day_rows = {"2026-08-01": {key: [1_000_000, 999, 999, 999, 2_000_000,
                                         0, 1]}}
        rows = [srow(f"{self.PDIR}/slug/a.jsonl", "/p")]
        m = context._measured(self.stats(rows, {"/p": day_rows}), [])
        p = m["projects"][0]
        self.assertEqual(p["cache_read_tok"], 2_000_000)
        # 2M reads at $5/Mtok input x 0.1 = $1.00; in/out/writes excluded
        self.assertAlmostEqual(p["cache_spend"], 1.0, places=6)

    def test_sessions_sorted_recent_first_and_capped(self):
        rows = [srow(f"{self.PDIR}/slug/{i:02}.jsonl", "/p",
                     sess(last_ts=f"2026-08-{i + 1:02}T00:00:00Z"))
                for i in range(20)]
        m = context._measured(self.stats(rows), [])
        got = m["sessions"]["/p"]
        self.assertEqual(len(got), context.SESSION_ROWS_PER_PROJECT)
        self.assertEqual(got[0]["last_ts"], "2026-08-20T00:00:00Z")
        self.assertEqual(got[0]["id"], "19")

    def test_slug_map_reads_ground_truth(self):
        rows = [srow(f"{self.PDIR}/the-slug/a.jsonl", "/p"),
                srow(f"{self.PDIR}/the-slug/a-uuid/subagents/b.jsonl", "/p")]
        self.assertEqual(context._slug_map(rows), {"/p": "the-slug"})


def scope(claude_md=(), types=None, mcp=(), memory=None, kind="user"):
    return {"scope": kind, "root": None if kind == "user" else "/p",
            "tilde": "~/.claude" if kind == "user" else "~/p",
            "claude_md": list(claude_md), "types": types or {},
            "mcp": {"count": len(mcp), "servers": list(mcp)},
            "memory": memory, "est_tok": 0}


def md_row(tok=0, imports=()):
    return {"name": "CLAUDE.md", "path": "/x/CLAUDE.md", "tilde": "~/x",
            "exists": True, "chars": tok * 4, "tok": tok,
            "imports": list(imports),
            "total_tok": tok + sum(i["tok"] for i in imports)}


def measured(projects=(), sessions=None):
    return {"available": True, "dir": "~", "sessions_total": 0,
            "scanned_now": 0, "projects": list(projects),
            "sessions": sessions or {}}


def mproj(cwd="/p", base_med=0, sessions=5, spend=1.0):
    return {"cwd": cwd, "tilde": cwd, "registered": True,
            "sessions": sessions, "subagents": 0, "base_med": base_med,
            "base_min": base_med, "base_max": base_med, "peak_max": 0,
            "cache_read_tok": 0, "cache_spend": spend, "last_ts": ""}


class Pointers(unittest.TestCase):
    """Each heuristic fires just past its threshold and not at it."""

    def test_big_claude_md_warns_past_threshold(self):
        at = context._pointers([scope(claude_md=[md_row(tok=4000)])],
                               measured())
        over = context._pointers([scope(claude_md=[md_row(tok=4001)])],
                                 measured())
        self.assertEqual([f["area"] for f in at], [])
        self.assertEqual([(f["level"], f["area"]) for f in over],
                         [("warn", "claude-md")])
        self.assertEqual(over[0]["target"],
                         {"kind": "path", "path": "/x/CLAUDE.md"})

    def test_unresolved_import_warns(self):
        imp = {"ref": "@gone.md", "path": "/x/gone.md", "resolved": False,
               "chars": 0, "tok": 0}
        finds = context._pointers(
            [scope(claude_md=[md_row(tok=1, imports=[imp])])], measured())
        self.assertIn("doesn't resolve", finds[0]["msg"])

    def test_long_descriptions_capped(self):
        rows = [{"name": f"s{i}", "enabled": True, "path": "~/x",
                 "listing_tok": 400, "desc_chars": 1500, "long_desc": True,
                 "file_chars": 0} for i in range(7)]
        finds = context._pointers(
            [scope(types={"skills": {"listing_tok": 0, "items": rows}})],
            measured())
        nags = [f for f in finds if f["area"] == "skills"]
        self.assertEqual(len(nags), context.LONG_DESC_CAP)
        self.assertTrue(any("2 more" in f["msg"] for f in finds))

    def test_mcp_count_past_threshold(self):
        servers = [{"name": f"s{i}", "enabled": True, "config_chars": 1}
                   for i in range(4)]
        finds = context._pointers([scope(mcp=servers)], measured())
        self.assertEqual([f["area"] for f in finds], ["mcp"])
        finds = context._pointers([scope(mcp=servers[:3])], measured())
        self.assertEqual(finds, [])

    def test_memory_index_past_threshold(self):
        mem = {"dir": "/cfg/projects/slug/memory", "tilde": "~", "topics": [],
               "topics_chars": 0, "memory_chars": 8008, "memory_tok": 2002}
        finds = context._pointers([scope(memory=mem, kind="project")],
                                  measured())
        self.assertEqual([f["area"] for f in finds], ["memory"])
        self.assertTrue(finds[0]["target"]["path"].endswith("MEMORY.md"))

    def test_baseline_is_relative_to_leanest_project(self):
        # 30k medians everywhere: no warning, however large the floor
        even = measured(projects=[mproj("/a", 30000), mproj("/b", 30000)])
        self.assertEqual(context._pointers([], even), [])
        # one project 8k+ past the floor: that one is named
        skew = measured(projects=[mproj("/a", 30000), mproj("/b", 38001)])
        finds = context._pointers([], skew)
        self.assertEqual(len(finds), 1)
        self.assertIn("/b", finds[0]["msg"])

    def test_few_session_projects_do_not_set_the_floor(self):
        skew = measured(projects=[mproj("/a", 1000, sessions=2),
                                  mproj("/b", 30000), mproj("/c", 31000)])
        self.assertEqual(context._pointers([], skew), [])

    def test_peak_growth_noted_for_top_spenders_only(self):
        projs = [mproj(f"/p{i}", spend=10.0 - i) for i in range(7)]
        sessions = {f"/p{i}": [{"id": "x", "first_ts": "", "last_ts": "",
                                "msgs": 1, "model": "m", "baseline": 0,
                                "peak": 200000}] for i in range(7)}
        finds = context._pointers([], measured(projs, sessions))
        peaks = [f for f in finds if "grew past" in f["msg"]]
        self.assertEqual(len(peaks), context.PEAK_PROJECT_CAP)

    def test_warns_sort_before_notes(self):
        finds = context._pointers(
            [scope(claude_md=[md_row(tok=9000)],
                   mcp=[{"name": f"s{i}", "enabled": True, "config_chars": 1}
                        for i in range(4)])], measured())
        self.assertEqual([f["level"] for f in finds], ["warn", "info"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
