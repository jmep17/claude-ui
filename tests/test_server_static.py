"""Response headers and the two inline-substitution guards.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from
the repo root, or just `python3 tests/test_server_static.py`.

A real ThreadingHTTPServer bound to 127.0.0.1:0 (ephemeral port), never a
mocked socket: the headers under test (CSP, frame-ancestors, the nonce) are
set by BaseHTTPRequestHandler plumbing that a hand-rolled fake would not
exercise honestly.
"""

import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import core, doctor, items, mcp, plugins, server, settings  # noqa: E402
from claude_ui import statusline  # noqa: E402

_CFG_USERS = (core, items, settings, mcp, plugins, statusline, doctor)


class ServerUp(unittest.TestCase):
    """A live server on a throwaway config dir, torn down after each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self.tmp.name) / "claude"
        self.cfg.mkdir()
        self._real = {m: getattr(m, "config_dir", None) for m in _CFG_USERS}
        for m in _CFG_USERS:
            if self._real[m] is not None:
                m.config_dir = lambda: self.cfg

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        for m, fn in self._real.items():
            if fn is not None:
                m.config_dir = fn
        self.tmp.cleanup()

    def get(self, path):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        try:
            resp = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            resp = e
        return resp


class TestSecurityHeaders(ServerUp):
    def _assert_headers(self, resp):
        csp = resp.headers.get("content-security-policy") or ""
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline'; style-src", csp)  # sanity: order stable
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertEqual(resp.headers.get("x-frame-options"), "DENY")
        self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(resp.headers.get("referrer-policy"), "no-referrer")

    def test_index_carries_the_security_headers(self):
        self._assert_headers(self.get("/"))

    def test_static_file_carries_the_security_headers(self):
        self._assert_headers(self.get("/app.js"))

    def test_api_response_carries_the_security_headers(self):
        self._assert_headers(self.get("/api/state"))

    def test_404_carries_the_security_headers(self):
        self._assert_headers(self.get("/api/nope"))


class TestNonce(ServerUp):
    def test_both_inline_scripts_carry_a_nonce_matching_the_csp(self):
        resp = self.get("/")
        page = resp.read().decode()
        csp = resp.headers.get("content-security-policy") or ""
        m = re.search(r"'nonce-([^']+)'", csp)
        self.assertIsNotNone(m)
        nonce = m.group(1)
        scripts = re.findall(r'<script nonce="([^"]*)"', page)
        self.assertEqual(len(scripts), 2)
        for n in scripts:
            self.assertEqual(n, nonce)
        self.assertNotIn("__NONCE__", page)


class TestSchemaInlining(ServerUp):
    """server.py binds settings_schema by name at import, so patching the
    binding on the server module (not settings.SETTINGS_SCHEMA, which a live
    generation-cache recomputes from SETTINGS_RAW regardless) is what a
    request handled by this process actually calls."""

    def setUp(self):
        super().setUp()
        self._real_schema = server.settings_schema

    def tearDown(self):
        server.settings_schema = self._real_schema
        super().tearDown()

    def test_schema_close_script_tag_renders_escaped(self):
        server.settings_schema = lambda: [
            {"key": "x", "description": "</script><script>evil()</script>"}]
        page = self.get("/").read().decode()
        self.assertNotIn("</script><script>evil", page)
        self.assertIn("\\u003c/script\\u003e", page)

    def test_literal_token_marker_in_schema_is_not_substituted(self):
        server.settings_schema = lambda: [{"key": "x", "description": "__TOKEN__"}]
        page = self.get("/").read().decode()
        self.assertIn('"__TOKEN__"', page)
        self.assertEqual(page.count(server.TOKEN), 1)


class TestStaticJsInvariants(unittest.TestCase):
    """Grep-style checks: there is no JS runtime here, and adding one to
    verify four files would break the no-dependencies rule."""

    STATIC = pathlib.Path(__file__).resolve().parent.parent / "bin" / "claude_ui" / "static"

    def _read(self, name):
        return (self.STATIC / name).read_text()

    def test_editor_js_has_no_raw_href_interpolation(self):
        src = self._read("editor.js")
        self.assertNotIn('href="$2"', src)
        self.assertIn("safeHref", src)

    def test_safe_href_is_defined_in_ui_js(self):
        self.assertIn("function safeHref(", self._read("ui.js"))

    def test_no_static_js_file_has_an_inline_onclick_attribute(self):
        for name in ("ui.js", "editor.js", "output-styles.js", "app.js"):
            self.assertNotIn('onclick="', self._read(name), name)


if __name__ == "__main__":
    unittest.main()
