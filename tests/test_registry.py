"""The marketplace bridge: what it refuses, and how it invokes the CLI.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from
the repo root, or just `python3 tests/test_registry.py`.

subprocess.run is replaced throughout. Nothing here should ever start a real
`claude`: what needs pinning is not that the CLI works — it is Anthropic's and
it has its own tests — but that we only ever ask it about a registered
project, from that project's own directory, with arguments that cannot be
mistaken for options. A test that shelled out for real would check the wrong
thing and need a network to do it.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import (catalog, core, items, plugins, projects, registry,  # noqa: E402
                       server, settings)


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Base(unittest.TestCase):
    """A registered project, and a subprocess.run that records instead of runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.cfg = base / "config"
        self.cfg.mkdir()
        self.proj = base / "proj"
        self.proj.mkdir()
        self._config_dir = core.config_dir
        core.config_dir = lambda: self.cfg
        self._p_config_dir = projects.config_dir
        projects.config_dir = core.config_dir
        self._r_config_dir = registry.config_dir
        registry.config_dir = core.config_dir
        self.calls = []
        self.result = _Done()
        self._run = registry.subprocess.run
        registry.subprocess.run = self.fake_run

    def tearDown(self):
        registry.subprocess.run = self._run
        core.config_dir = self._config_dir
        projects.config_dir = self._p_config_dir
        registry.config_dir = self._r_config_dir
        self.tmp.cleanup()

    def fake_run(self, argv, **kw):
        self.calls.append((argv, kw))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def add(self):
        return projects.registry_add(str(self.proj))


class Invocation(Base):

    def test_runs_claude_in_the_projects_own_directory(self):
        self.add()
        registry.plugin_install(self.proj, "hello@mkt")
        argv, kw = self.calls[0]
        self.assertEqual(argv, ["claude", "plugin", "install", "hello@mkt",
                                "--scope", "project"])
        self.assertEqual(kw["cwd"], str(self.proj.resolve()))

    def test_stdin_is_closed_so_a_prompt_fails_instead_of_hanging(self):
        self.add()
        registry.plugin_install(self.proj, "hello@mkt")
        self.assertEqual(self.calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.calls[0][1]["timeout"], registry.TIMEOUT)

    def test_every_call_is_scoped_to_the_project(self):
        self.add()
        registry.marketplace_add(self.proj, "owner/repo")
        registry.marketplace_remove(self.proj, "mkt")
        registry.plugin_install(self.proj, "hello@mkt")
        registry.plugin_uninstall(self.proj, "hello@mkt")
        for argv, _ in self.calls:
            with self.subTest(argv=argv):
                self.assertIn("--scope", argv)
                self.assertEqual(argv[argv.index("--scope") + 1], "project")

    def test_uninstall_answers_the_prune_confirmation(self):
        self.add()
        registry.plugin_uninstall(self.proj, "hello@mkt")
        self.assertIn("-y", self.calls[0][0])

    def test_the_clis_own_words_come_back(self):
        self.add()
        self.result = _Done(1, "", "no such marketplace: nope")
        r = registry.marketplace_add(self.proj, "nope")
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "no such marketplace: nope")

    def test_a_missing_cli_is_a_message_not_a_traceback(self):
        self.add()
        self.result = FileNotFoundError()
        with self.assertRaises(ValueError) as cm:
            registry.plugin_install(self.proj, "hello@mkt")
        self.assertIn("not found on PATH", str(cm.exception))

    def test_a_wedged_call_gives_up(self):
        self.add()
        self.result = subprocess.TimeoutExpired("claude", registry.TIMEOUT)
        with self.assertRaises(ValueError) as cm:
            registry.plugin_install(self.proj, "hello@mkt")
        self.assertIn("longer than", str(cm.exception))


class Refusals(Base):

    def test_an_unregistered_project_never_reaches_the_cli(self):
        for fn, arg in ((registry.plugin_install, "hello@mkt"),
                        (registry.marketplace_add, "owner/repo"),
                        (registry.marketplace_remove, "mkt"),
                        (registry.plugin_uninstall, "hello@mkt")):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(ValueError):
                    fn(self.proj, arg)
        self.assertEqual(self.calls, [])

    def test_registry_state_needs_registration_too(self):
        with self.assertRaises(ValueError):
            registry.registry_state(self.proj)
        self.assertEqual(self.calls, [])

    def test_an_argument_cannot_become_an_option(self):
        """argv needs no quoting, but it cannot tell an argument starting with
        a dash from a flag — so a typed name must not be able to be one."""
        self.add()
        for bad in ("--scope", "-y", "--help", "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    registry.plugin_install(self.proj, bad)
                with self.assertRaises(ValueError):
                    registry.marketplace_add(self.proj, bad)
        self.assertEqual(self.calls, [])


class UserScope(Base):
    """The user-scope entry points: same transport, no project required."""

    def test_runs_from_home_with_user_scope(self):
        registry.user_plugin_install("hello@mkt")
        argv, kw = self.calls[0]
        self.assertEqual(argv, ["claude", "plugin", "install", "hello@mkt",
                                "--scope", "user"])
        self.assertEqual(kw["cwd"], str(pathlib.Path.home()))

    def test_every_user_call_says_its_scope(self):
        registry.user_marketplace_add("owner/repo")
        registry.user_marketplace_remove("mkt")
        registry.user_plugin_install("hello@mkt")
        registry.user_plugin_uninstall("hello@mkt")
        for argv, _ in self.calls:
            with self.subTest(argv=argv):
                self.assertIn("--scope", argv)
                self.assertEqual(argv[argv.index("--scope") + 1], "user")

    def test_no_registered_project_is_needed(self):
        # the inverse of Refusals: an empty registry refuses project calls
        # but must not refuse user ones
        registry.user_plugin_install("hello@mkt")
        self.assertEqual(len(self.calls), 1)

    def test_uninstall_answers_the_prune_confirmation(self):
        registry.user_plugin_uninstall("hello@mkt")
        self.assertIn("-y", self.calls[0][0])

    def test_an_argument_cannot_become_an_option(self):
        for bad in ("--scope", "-y", "--help", "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    registry.user_plugin_install(bad)
                with self.assertRaises(ValueError):
                    registry.user_marketplace_add(bad)
        self.assertEqual(self.calls, [])

    def test_stdin_closed_and_timeout_pinned(self):
        registry.user_plugin_install("hello@mkt")
        self.assertEqual(self.calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.calls[0][1]["timeout"], registry.TIMEOUT)

    def test_the_config_dir_reaches_the_cli(self):
        """The app's config dir can be redirected by .claude-ui.json; the CLI
        only hears about that through CLAUDE_CONFIG_DIR."""
        registry.user_plugin_install("hello@mkt")
        self.add()
        registry.plugin_install(self.proj, "hello@mkt")
        for argv, kw in self.calls:
            with self.subTest(argv=argv):
                self.assertEqual(kw["env"]["CLAUDE_CONFIG_DIR"], str(self.cfg))

    def test_user_registry_state_reports_both_lists(self):
        outs = [json.dumps([{"name": "mkt"}]),
                json.dumps({"installed": [{"id": "hello@mkt", "scope": "user"}],
                            "available": [{"pluginId": "other@mkt"}]})]
        def fake(argv, **kw):
            self.calls.append((argv, kw))
            return _Done(0, outs[len(self.calls) - 1])
        registry.subprocess.run = fake
        st = registry.user_registry_state()
        self.assertIsNone(st["error"])
        self.assertNotIn("root", st)
        self.assertEqual([m["name"] for m in st["marketplaces"]], ["mkt"])
        self.assertEqual(st["installed"][0]["scope"], "user")
        self.assertEqual(st["available"][0]["pluginId"], "other@mkt")
        self.assertTrue(st["suggested"])

    def test_a_failing_cli_becomes_an_error_string_not_an_exception(self):
        self.result = _Done(1, "", "claude: something went wrong")
        st = registry.user_registry_state()
        self.assertEqual(st["error"], "claude: something went wrong")
        self.assertEqual(st["marketplaces"], [])
        self.assertEqual(st["available"], [])


class State(Base):

    def json_results(self, markets, plugins):
        """registry_state makes two calls; answer them in order. The counter
        restarts here so a test may call it more than once."""
        outs = [json.dumps(markets), json.dumps(plugins)]
        self.calls = []
        def fake(argv, **kw):
            self.calls.append((argv, kw))
            return _Done(0, outs[(len(self.calls) - 1) % len(outs)])
        registry.subprocess.run = fake

    def test_reports_marketplaces_installed_and_available(self):
        self.add()
        self.json_results(
            [{"name": "mkt"}],
            {"installed": [{"id": "hello@mkt", "scope": "project"}],
             "available": [{"pluginId": "other@mkt", "name": "other"}]})
        st = registry.registry_state(self.proj)
        self.assertIsNone(st["error"])
        self.assertEqual([m["name"] for m in st["marketplaces"]], ["mkt"])
        self.assertEqual(st["installed"][0]["id"], "hello@mkt")
        self.assertEqual(st["available"][0]["pluginId"], "other@mkt")
        self.assertTrue(st["suggested"])

    def test_a_failing_cli_becomes_an_error_string_not_an_exception(self):
        self.add()
        self.result = _Done(1, "", "claude: something went wrong")
        st = registry.registry_state(self.proj)
        self.assertEqual(st["error"], "claude: something went wrong")
        self.assertEqual(st["marketplaces"], [])
        self.assertEqual(st["available"], [])

    def test_unreadable_output_is_reported_the_same_way(self):
        self.add()
        self.result = _Done(0, "not json at all")
        st = registry.registry_state(self.proj)
        self.assertIn("unreadable output", st["error"])

    def test_suggested_sources_are_copies_callers_cannot_corrupt(self):
        self.add()
        self.json_results([], {})
        registry.registry_state(self.proj)["suggested"][0]["source"] = "evil"
        self.json_results([], {})
        self.assertEqual(registry.registry_state(self.proj)["suggested"][0]["source"],
                         registry.SUGGESTED[0]["source"])


class CatalogEndpoints(unittest.TestCase):
    """POST /api/catalog-install and /api/catalog-marketplace-add, through a
    real running server — the same posture test_server_static.py takes,
    because what is under test (headers, dispatch, the 400-before-subprocess
    ordering) is plumbing a hand-rolled fake handler would not exercise
    honestly. registry.subprocess.run is replaced as in Base above: nothing
    here should ever start a real `claude`."""

    _CFG_USERS = (core, items, settings, plugins, catalog, projects, registry)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self.tmp.name) / "claude"
        self.cfg.mkdir()
        self._saved = [(m, m.config_dir) for m in self._CFG_USERS]
        for m, _ in self._saved:
            m.config_dir = lambda t=self.cfg: t
        catalog._CACHE = None

        self.calls = []
        self._run = registry.subprocess.run
        registry.subprocess.run = self.fake_run

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        registry.subprocess.run = self._run
        for m, fn in self._saved:
            m.config_dir = fn
        catalog._CACHE = None
        self.tmp.cleanup()

    def fake_run(self, argv, **kw):
        self.calls.append((argv, kw))
        return _Done(0, "{}")

    def post(self, path, body):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json", "x-claude-ui": core.TOKEN})
        try:
            resp = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, json.loads(resp.read())

    def test_catalog_install_unknown_id_shells_out_to_nothing(self):
        status, body = self.post("/api/catalog-install",
                                 {"id": "nope@nowhere", "scope": "user"})
        self.assertEqual(status, 400)
        self.assertIn("nope@nowhere", body["error"])
        self.assertEqual(self.calls, [])

    def test_catalog_marketplace_add_blocked_shells_out_to_nothing(self):
        write = lambda path, obj: (path.parent.mkdir(parents=True, exist_ok=True),
                                   path.write_text(json.dumps(obj)))
        write(self.cfg / "settings.json", {"blockedMarketplaces": ["owner/repo"]})
        status, body = self.post("/api/catalog-marketplace-add",
                                 {"source": "owner/repo"})
        self.assertEqual(status, 400)
        self.assertIn("owner/repo", body["error"])
        self.assertEqual(self.calls, [])

    def test_catalog_marketplace_add_unblocked_runs_the_cli(self):
        status, body = self.post("/api/catalog-marketplace-add",
                                 {"source": "owner/repo"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("owner/repo", self.calls[0][0])


if __name__ == "__main__":
    unittest.main()
