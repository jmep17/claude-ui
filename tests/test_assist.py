"""The lean `claude -p` invocation behind the editor's assist feature.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from
the repo root, or just `python3 tests/test_assist.py`.

Three properties are pinned here. The flag probe must parse --help by token,
not substring — --allowed-tools must never satisfy a --tools check, nor
--append-system-prompt a --system-prompt one. The argv must keep the positional
prompt ahead of the variadic --tools (commander swallows any bare token after
it, and claude would then block on stdin for 240 s). And on a CLI with no
recognised flags the argv must be byte-identical to the legacy form, so an old
installation keeps working untouched."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import assist  # noqa: E402


FULL_HELP = """\
Usage: claude [options] [command] [prompt]
  -p, --print                Print response and exit
  --append-system-prompt <p> Append a system prompt to the default
  --system-prompt <prompt>   System prompt to use for the session
  --allowed-tools <tools...> Allowed tools
  --disallowed-tools <t...>  Disallowed tools
  --tools <tools...>         Available tools
  --strict-mcp-config        Only use MCP servers from --mcp-config
  --bare                     Provide context via: --system-prompt[-file],
                             --append-system-prompt[-file]
"""

TRAP_HELP = """\
Usage: claude [options] [command] [prompt]
  --append-system-prompt <p> Append a system prompt to the default
  --allowed-tools <tools...> Allowed tools
"""


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Base(unittest.TestCase):
    def setUp(self):
        assist._FLAG_CACHE.clear()
        self._run = assist.subprocess.run
        self._which = assist.shutil.which

    def tearDown(self):
        assist.subprocess.run = self._run
        assist.shutil.which = self._which
        assist._FLAG_CACHE.clear()


class FlagProbe(Base):
    """_cli_flags: tokenized parsing, failure caching, mtime keying."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = pathlib.Path(self.tmp.name) / "claude"
        self.exe.write_text("#!/bin/sh\n")
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def fake(self, help_text=FULL_HELP, returncode=0, raise_=None):
        def run(argv, **kw):
            self.calls += 1
            if raise_:
                raise raise_
            return _Result(returncode, help_text)
        assist.subprocess.run = run

    def test_tokenized_membership(self):
        self.fake()
        flags = assist._cli_flags(str(self.exe))
        for f in ("--system-prompt", "--tools", "--strict-mcp-config",
                  "--append-system-prompt", "--allowed-tools"):
            self.assertIn(f, flags)

    def test_bracket_suffixes_expand(self):
        # --system-prompt[-file] in help must yield both flag spellings
        self.fake()
        flags = assist._cli_flags(str(self.exe))
        self.assertIn("--system-prompt-file", flags)
        self.assertIn("--append-system-prompt-file", flags)

    def test_substring_traps(self):
        self.fake(TRAP_HELP)
        flags = assist._cli_flags(str(self.exe))
        self.assertNotIn("--system-prompt", flags)
        self.assertNotIn("--tools", flags)
        self.assertIn("--append-system-prompt", flags)
        self.assertIn("--allowed-tools", flags)

    def test_failures_yield_empty_set(self):
        for kw in ({"returncode": 1},
                   {"raise_": OSError("gone")},
                   {"raise_": subprocess.TimeoutExpired("claude", 20)}):
            assist._FLAG_CACHE.clear()
            self.fake(**kw)
            self.assertEqual(assist._cli_flags(str(self.exe)), frozenset())

    def test_probe_is_cached_including_failure(self):
        self.fake()
        assist._cli_flags(str(self.exe))
        assist._cli_flags(str(self.exe))
        self.assertEqual(self.calls, 1)
        assist._FLAG_CACHE.clear()
        self.calls = 0
        self.fake(raise_=OSError("boom"))
        assist._cli_flags(str(self.exe))
        assist._cli_flags(str(self.exe))
        self.assertEqual(self.calls, 1)

    def test_mtime_bump_reprobes(self):
        self.fake(TRAP_HELP)
        self.assertNotIn("--tools", assist._cli_flags(str(self.exe)))
        st = self.exe.stat()
        os.utime(self.exe, (st.st_atime, st.st_mtime + 10))
        self.fake(FULL_HELP)
        self.assertIn("--tools", assist._cli_flags(str(self.exe)))
        self.assertEqual(self.calls, 2)  # initial probe + one re-probe


class ArgvAssembly(Base):
    """_assist_argv: exact shapes, safe ordering, legacy fallback."""

    def setUp(self):
        super().setUp()
        self._flags = assist._cli_flags

    def tearDown(self):
        assist._cli_flags = self._flags
        super().tearDown()

    def with_flags(self, *flags):
        assist._cli_flags = lambda exe: frozenset(flags)

    def test_full_support_exact_argv(self):
        self.with_flags("--system-prompt", "--strict-mcp-config", "--tools")
        argv = assist._assist_argv("/x/claude", "PROMPT")
        self.assertEqual(argv, ["/x/claude", "-p", "PROMPT",
                                "--system-prompt", assist.ASSIST_SYSTEM_PROMPT,
                                "--strict-mcp-config", "--tools", ""])
        self.assertEqual(argv[-1], "")  # the empty string survives verbatim

    def test_no_support_is_byte_identical_legacy(self):
        self.with_flags()
        self.assertEqual(assist._assist_argv("/x/claude", "PROMPT"),
                         ["/x/claude", "-p", "PROMPT"])

    def test_partial_support(self):
        self.with_flags("--system-prompt")
        self.assertEqual(assist._assist_argv("/x/c", "P"),
                         ["/x/c", "-p", "P",
                          "--system-prompt", assist.ASSIST_SYSTEM_PROMPT])
        self.with_flags("--tools")
        self.assertEqual(assist._assist_argv("/x/c", "P"),
                         ["/x/c", "-p", "P", "--tools", ""])
        self.with_flags("--strict-mcp-config")
        self.assertEqual(assist._assist_argv("/x/c", "P"),
                         ["/x/c", "-p", "P", "--strict-mcp-config"])

    def test_prompt_always_precedes_variadic_tools(self):
        self.with_flags("--system-prompt", "--strict-mcp-config", "--tools")
        argv = assist._assist_argv("/x/c", "P")
        self.assertLess(argv.index("P"), argv.index("--tools"))

    def test_forbidden_flags_never_appear(self):
        self.with_flags("--system-prompt", "--strict-mcp-config", "--tools",
                        "--bare", "--setting-sources", "--append-system-prompt")
        argv = assist._assist_argv("/x/c", "P")
        for f in ("--bare", "--setting-sources", "--append-system-prompt"):
            self.assertNotIn(f, argv)


class AssistEndToEnd(Base):
    """assist(): unchanged kwargs and error paths around the new argv."""

    def setUp(self):
        super().setUp()
        self._flags = assist._cli_flags
        assist.shutil.which = lambda n: "/fake/claude"
        assist._cli_flags = lambda exe: frozenset(
            ("--system-prompt", "--strict-mcp-config", "--tools"))
        self.seen = {}

    def tearDown(self):
        assist._cli_flags = self._flags
        super().tearDown()

    def fake_run(self, result=None, raise_=None):
        def run(argv, **kw):
            self.seen = {"argv": argv, **kw}
            if raise_:
                raise raise_
            return result or _Result(0, "OUT")
        assist.subprocess.run = run

    def test_success_lean_argv_and_kwargs(self):
        self.fake_run()
        r = assist.assist("improve", "", "content", "x.md")
        self.assertEqual(r["replaces"], True)
        self.assertEqual(r["result"], "OUT")
        self.assertEqual(self.seen["timeout"], 240)
        self.assertEqual(self.seen["cwd"], str(pathlib.Path.home()))
        self.assertTrue(self.seen["capture_output"])
        self.assertTrue(self.seen["text"])
        self.assertEqual(self.seen["argv"][0:2], ["/fake/claude", "-p"])
        self.assertIn("--system-prompt", self.seen["argv"])

    def test_legacy_fallback_end_to_end(self):
        assist._cli_flags = lambda exe: frozenset()
        self.fake_run()
        assist.assist("review", "", "content", "x.md")
        self.assertEqual(len(self.seen["argv"]), 3)

    def test_error_paths_unchanged(self):
        self.fake_run(_Result(1, "", "boom"))
        with self.assertRaisesRegex(ValueError, "boom"):
            assist.assist("review", "", "content", "x.md")
        self.fake_run(raise_=subprocess.TimeoutExpired("claude", 240))
        with self.assertRaisesRegex(ValueError, "timed out after 240s"):
            assist.assist("review", "", "content", "x.md")
        assist.shutil.which = lambda n: None
        with self.assertRaisesRegex(ValueError, "not found"):
            assist.assist("review", "", "content", "x.md")

    def test_input_validation_unchanged(self):
        with self.assertRaises(ValueError):
            assist.assist("nope", "", "content", "x.md")
        with self.assertRaises(ValueError):
            assist.assist("custom", "  ", "content", "x.md")
        with self.assertRaises(ValueError):
            assist.assist("improve", "", "", "x.md")

    def test_fence_stripping_still_applies(self):
        self.fake_run(_Result(0, "```md\nhello\n```"))
        r = assist.assist("improve", "", "content", "x.md")
        self.assertEqual(r["result"], "hello")


if __name__ == "__main__":
    unittest.main()
