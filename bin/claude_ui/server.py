"""HTTP handler, static file serving, and the CLI entry point."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import errno
import json
import os
import sys
import urllib.parse
import webbrowser

from .core import ITEM_TYPES, TOKEN, config_dir, read_cfg, set_config_dir, tilde
from .items import (Conflict, config_files_state, item_read, item_save,
                    path_read, path_save, scan_items, set_enabled)
from .mcp import mcp_machine_set, mcp_set_enabled, mcp_state, mcp_test
from . import schema
from .plugins import (adopted_items, plugin_resync, plugin_set_enabled,
                      plugins_split, plugins_state, skill_override_set)
from .settings import (hook_test, settings_schema, settings_set, settings_state,
                       start_docs_fetch, suggest_state)
from .statusline import statusline_save, statusline_state
from .setup import setup_apply, setup_remove, setup_state
from .insight import cost_stats, insight_budget, usage_stats
from .assist import assist
from .doctor import doctor


STATIC = Path(__file__).resolve().parent / "static"
# Explicit allowlist rather than path joining — nothing user-supplied ever
# reaches the filesystem here.
_STATIC_NAMES = ("theme.css", "components.css", "app.css", "ui.js", "editor.js",
                 "app.js")
STATIC_FILES = {
    "/" + n: (n, ("text/css" if n.endswith(".css") else "text/javascript")
              + "; charset=utf-8")
    for n in _STATIC_NAMES
}

# Brand mark, matching the header chip: a rounded square with a "C" cut out.
ICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#d0754e'/>"
    "<path d='M21.4 21.1a7.2 7.2 0 1 1 0-10.2' fill='none' stroke='#fff' "
    "stroke-width='3.4' stroke-linecap='round'/></svg>"
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, bytes):
            data = body
        else:
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def host_ok(self):
        """Reject non-loopback Host headers (DNS-rebinding protection)."""
        host = (self.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host in ("127.0.0.1", "localhost", "::1"):
            return True
        self.send(403, {"error": "bad host"})
        return False

    def do_GET(self):
        if not self.host_ok():
            return
        if self.path == "/":
            page = (STATIC / "index.html").read_text()
            # a call, not the module constant: a live schema fetch that landed
            # after start-up is picked up on the next page render
            self.send(200, page.replace("__SCHEMA__", json.dumps(settings_schema()))
                              .replace("__TOKEN__", TOKEN),
                      "text/html; charset=utf-8", {"cache-control": "no-store"})
        elif self.path in STATIC_FILES:
            fname, ctype = STATIC_FILES[self.path]
            self.send(200, (STATIC / fname).read_text(), ctype,
                      {"cache-control": "no-store"})
        elif self.path in ("/favicon.ico", "/icon.svg"):
            self.send(200, ICON_SVG, "image/svg+xml")
        elif self.path == "/manifest.webmanifest":
            self.send(200, {"name": "claude config", "short_name": "claude-ui",
                            "start_url": "/", "display": "standalone",
                            "background_color": "#1c1917", "theme_color": "#d0754e",
                            "icons": [{"src": "/icon.svg", "sizes": "any",
                                       "type": "image/svg+xml",
                                       "purpose": "any"}]},
                      "application/manifest+json")
        elif self.path == "/api/state":
            self.send(200, {
                "items": {t: scan_items(t) for t in ITEM_TYPES},
                "config_files": config_files_state(),
                "settings": settings_state(),
                "suggest": suggest_state(),
                "mcp": mcp_state(),
                "statusline": statusline_state(),
                "config_dir": tilde(config_dir()),
                "default_dir": "config_dir" not in read_cfg()
                               and not os.environ.get("CLAUDE_CONFIG_DIR"),
            })
        elif self.path == "/api/schema-help":
            # long-form help for the settings popovers, fetched once on the
            # first open. Kept out of the inlined page schema: it is bigger than
            # everything a row needs to render, and most rows never open one.
            self.send(200, {
                "generation": schema.generation(),
                "keys": schema.help_payload(s["key"] for s in settings_schema()),
            }, extra={"cache-control": "no-cache"})
        elif self.path.startswith("/api/path?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self.send(200, path_read((q.get("path") or [""])[0]))
            except (ValueError, OSError) as e:
                self.send(400, {"error": str(e)})
        elif self.path.startswith("/api/item?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            get = lambda k, d="": (q.get(k) or [d])[0]
            try:
                self.send(200, item_read(get("type"), get("name"),
                                         get("file") or None,
                                         get("enabled", "1") == "1"))
            except (ValueError, OSError) as e:
                self.send(400, {"error": str(e)})
        elif self.path.startswith("/api/insight"):
            rescan = "rescan" in self.path
            self.send(200, {"budget": insight_budget(),
                            "usage": usage_stats(rescan=rescan),
                            "allow": (settings_state()["data"]
                                      .get("permissions", {}) or {}).get("allow", [])})
        elif self.path.startswith("/api/costs"):
            self.send(200, cost_stats(rescan="rescan" in self.path))
        elif self.path == "/api/doctor":
            self.send(200, doctor())
        elif self.path == "/api/setup":
            self.send(200, setup_state())
        elif self.path == "/api/plugins":
            # adopted items are the other half of the plugin story — what a
            # Split actually left in your config — and only the doctor could
            # see them before
            self.send(200, {**plugins_state(), "adopted": adopted_items()})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.host_ok():
            return
        if self.headers.get("x-claude-ui") != TOKEN:
            self.send(403, {"error": "bad or missing token — reload the page"})
            return
        try:
            req = self.body()
            action = self.path.removeprefix("/api/")
            if action == "config-dir":
                set_config_dir((req.get("path") or "").strip())
                self.send(200, {"ok": True})
            elif action == "item-toggle":
                path = set_enabled(req.get("type", ""), req.get("name", ""),
                                   bool(req.get("enabled")))
                self.send(200, {"ok": True, "path": path})
            elif action == "settings-set":
                settings_set(req.get("key", ""), req.get("value"))
                self.send(200, {"ok": True})
            elif action == "path-save":
                self.send(200, {"ok": True, **path_save(
                    req.get("path", ""), req.get("content", ""),
                    req.get("base"))})
            elif action == "item-save":
                self.send(200, {"ok": True, **item_save(
                    req.get("type", ""), req.get("name", ""), req.get("file"),
                    req.get("content", ""), bool(req.get("enabled", True)),
                    req.get("base"))})
            elif action == "hook-test":
                self.send(200, hook_test(req.get("command", ""), req.get("event", "")))
            elif action == "statusline-save":
                statusline_save(req.get("config"), bool(req.get("apply")))
                self.send(200, {"ok": True})
            elif action == "setup-apply":
                setup_apply(req.get("id", ""))
                self.send(200, {"ok": True})
            elif action == "setup-remove":
                setup_remove(req.get("id", ""))
                self.send(200, {"ok": True})
            elif action == "mcp-save":
                mcp_machine_set(req.get("name", ""), req.get("config"),
                                bool(req.get("enabled", True)))
                self.send(200, {"ok": True})
            elif action == "mcp-delete":
                mcp_machine_set(req.get("name", ""), None,
                                bool(req.get("enabled", True)))
                self.send(200, {"ok": True})
            elif action == "mcp-toggle":
                mcp_set_enabled(req.get("name", ""), bool(req.get("enabled")))
                self.send(200, {"ok": True})
            elif action == "mcp-test":
                self.send(200, mcp_test(req.get("name", "")))
            elif action == "plugin-toggle":
                plugin_set_enabled(req.get("id", ""), bool(req.get("enabled")))
                self.send(200, {"ok": True})
            elif action == "plugin-split":
                self.send(200, {"ok": True, **plugins_split(
                    req.get("id", ""), req.get("picks") or [],
                    bool(req.get("disable", True)))})
            elif action == "plugin-resync":
                self.send(200, {"ok": True, **plugin_resync(
                    req.get("type", ""), req.get("name", ""))})
            elif action == "skill-override":
                skill_override_set(req.get("name", ""), req.get("value"))
                self.send(200, {"ok": True})
            elif action == "assist":
                self.send(200, assist(req.get("mode", ""), req.get("custom", ""),
                                      req.get("content", ""), req.get("path", "")))
            else:
                self.send(404, {"error": "not found"})
        except Conflict as e:  # a ValueError — must be caught first
            self.send(409, {"error": str(e), "conflict": True})
        except (ValueError, OSError, json.JSONDecodeError) as e:
            self.send(400, {"error": str(e)})

def main():
    ap = argparse.ArgumentParser(
        description="Local dashboard + editor for the live Claude Code config "
                    "(see bin/claude-ui's docstring for the model)")
    ap.add_argument("--port", type=int, default=7333)
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = ap.parse_args()
    srv = None
    for port in range(args.port, args.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
    if srv is None:
        sys.exit(f"claude-ui: ports {args.port}-{args.port + 19} all in use")
    if port != args.port:
        print(f"claude-ui: port {args.port} in use, using {port}")
    url = f"http://127.0.0.1:{port}"
    print(f"claude-ui: {url}  (config dir: {config_dir()})")
    # two threads, not one: a failure in either must not skip the other
    start_docs_fetch()
    schema.start_schema_fetch()
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
