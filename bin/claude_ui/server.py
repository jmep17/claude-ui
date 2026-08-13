"""HTTP handler, static file serving, and the CLI entry point."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import errno
import json
import os
import re
import secrets
import sys
import urllib.parse
import webbrowser

from .backup import (backup_create, backup_delete, backup_inspect, backup_list,
                     backup_plan, backup_restore, fresh_start,
                     project_restore, project_restore_inspect, set_backup_dir)
from . import catalog
from .core import ITEM_TYPES, TOKEN, config_dir, read_cfg, set_config_dir, tilde
from . import remote
from .items import (Conflict, config_files_state, item_copy, item_create,
                    item_delete, item_move, item_read, item_save, item_scope,
                    item_set_model, path_read, path_save, scan_items,
                    set_enabled)
from .localmodel import local_config_set, local_probe, local_test
from .mcp import mcp_machine_set, mcp_set_enabled, mcp_state, mcp_test
from . import output_styles
from . import schema
from .plugins import (adopted_items, plugin_env_set, plugin_env_vars,
                      plugin_resync, plugin_scope_move, plugin_set_enabled,
                      plugins_split, plugins_state, skill_env_vars,
                      skill_override_set)
from .projects import (mcp_move, project_init, project_mcp_approve,
                       project_mcp_set, project_set_mode, project_setting_set,
                       project_skill_override, project_toggle, projects_state,
                       registry_add, registry_remove, wrapper_check,
                       wrapper_test, wrapper_write)
from .registry import (marketplace_add, marketplace_remove, plugin_install,
                       plugin_uninstall, registry_state, user_marketplace_add,
                       user_marketplace_remove, user_plugin_install,
                       user_plugin_uninstall, user_registry_state)
from .settings import (hook_test, settings_schema, settings_set, settings_state,
                       start_docs_fetch, suggest_state)
from .statusline import statusline_save, statusline_state
from .setup import setup_apply, setup_config, setup_remove, setup_state
from .insight import cost_diagnostics, cost_stats, usage_stats
from .context import context_state
from .assist import assist
from .doctor import doctor


STATIC = Path(__file__).resolve().parent / "static"
# Explicit allowlist rather than path joining — nothing user-supplied ever
# reaches the filesystem here.
_STATIC_NAMES = ("theme.css", "components.css", "app.css", "ui.js", "editor.js",
                 "output-styles.js", "app.js")
STATIC_FILES = {
    "/" + n: (n, ("text/css" if n.endswith(".css") else "text/javascript")
              + "; charset=utf-8")
    for n in _STATIC_NAMES
}

# json.dumps leaves <, >, & and the two line-separator code points alone, so
# remote catalog/schema text containing "</script>" (or a U+2028 that some
# JS parsers treat as a line break) can close the inlining script block.
# All five escapes below are valid \uXXXX JSON escapes and parse back
# identical to the source value.
_JS_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
               "\u2028": "\\u2028", "\u2029": "\\u2029"}
_JS_ESCAPE_RE = re.compile("[<>&\u2028\u2029]")


def _js_json(obj):
    return _JS_ESCAPE_RE.sub(lambda m: _JS_ESCAPES[m.group()], json.dumps(obj))


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
        # Sent from here, not per-endpoint, so no future handler can forget
        # them. script-src carries a nonce (see do_GET's "/" branch) rather
        # than 'unsafe-inline' — the four static JS files have zero inline
        # event handlers (grepped), so nothing else needs it.
        nonce = getattr(self, "_csp_nonce", None) or secrets.token_urlsafe(16)
        self.send_header(
            "content-security-policy",
            "default-src 'none'; script-src 'self' 'nonce-" + nonce + "'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")
        self.send_header("x-frame-options", "DENY")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header("x-content-type-options", "nosniff")
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
            self._csp_nonce = secrets.token_urlsafe(16)
            # __TOKEN__ first: it's hex and can't contain another marker, so
            # substituting it before __SCHEMA__ means schema content (remote,
            # from schemastore) can never steer a later substitution by
            # containing the literal string "__TOKEN__".
            # a call, not the module constant: a live schema fetch that landed
            # after start-up is picked up on the next page render
            page = (page.replace("__TOKEN__", TOKEN)
                        .replace("__SCHEMA__", _js_json(settings_schema()))
                        .replace("__NONCE__", self._csp_nonce))
            self.send(200, page,
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
        elif self.path == "/api/output-style-presets":
            # bundled starting points plus the field reference the create form
            # renders. Static and several KB, so it is fetched when the form
            # opens rather than riding along on every /api/state refresh.
            self.send(200, {"presets": output_styles.presets(),
                            "fields": output_styles.FIELDS,
                            "doc": schema.DOC_BASE + output_styles.DOC})
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
                # `root` names a registered project when the Projects tab is
                # reading that project's own copy; absent, this is the config
                # dir as it has always been.
                self.send(200, item_read(get("type"), get("name"),
                                         get("file") or None,
                                         get("enabled", "1") == "1",
                                         item_scope(get("root") or None)))
            except (ValueError, OSError) as e:
                self.send(400, {"error": str(e)})
        elif self.path.startswith("/api/insight"):
            rescan = "rescan" in self.path
            self.send(200, {"usage": usage_stats(rescan=rescan),
                            "allow": (settings_state()["data"]
                                      .get("permissions", {}) or {}).get("allow", [])})
        elif self.path.startswith("/api/costs/diagnose"):
            self.send(200, cost_diagnostics(rescan="rescan" in self.path))
        elif self.path.startswith("/api/costs"):
            self.send(200, cost_stats(rescan="rescan" in self.path))
        elif self.path.startswith("/api/context"):
            self.send(200, context_state(rescan="rescan" in self.path))
        elif self.path == "/api/doctor":
            self.send(200, doctor())
        elif self.path == "/api/setup":
            self.send(200, setup_state())
        elif self.path == "/api/projects":
            self.send(200, projects_state())
        elif self.path == "/api/plugins":
            # adopted items are the other half of the plugin story — what a
            # Split actually left in your config — and only the doctor could
            # see them before
            self.send(200, {**plugins_state(), "adopted": adopted_items()})
        elif self.path == "/api/backup":
            self.send(200, {**backup_list(), "plan": backup_plan()})
        elif self.path == "/api/archives":
            # the archive list alone, for the Projects tab's picker. /api/backup
            # would also compute backup_plan(), a full walk of the config dir
            # including transcripts — none of it relevant to restoring one skill
            self.send(200, backup_list())
        elif self.path.startswith("/api/backup-inspect?"):
            # the dry run: every file in the archive against what's on disk now
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self.send(200, backup_inspect((q.get("name") or [""])[0]))
            except (ValueError, OSError) as e:
                self.send(400, {"error": str(e)})
        elif self.path.startswith("/api/plugin-detail?"):
            # the env vars a plugin reads, found by walking its tree — too
            # expensive for /api/plugins, which the doctor and insight also hit
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (q.get("id") or [""])[0]
            try:
                self.send(200, {"id": pid, "env": plugin_env_vars(pid)})
            except (ValueError, OSError) as e:
                self.send(400, {"error": str(e)})
        elif self.path == "/api/catalog":
            # index metadata only — counts per source, cache freshness, policy
            # as configured. No search here; that's /api/search below.
            # discover_consent rides along here (not catalog.catalog_state()
            # itself) because catalog.py cannot import remote.py — remote.py
            # already imports catalog.py for its sanitization helpers, and the
            # reverse would be a cycle.
            self.send(200, {**catalog.catalog_state(),
                            "discover_consent": remote.consent_get()})
        elif self.path == "/api/search" or self.path.startswith("/api/search?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            get = lambda k, d="": (q.get(k) or [d])[0]
            try:
                limit = int(get("limit") or 20)
            except ValueError:
                limit = 20
            self.send(200, {"hits": catalog.search(
                get("q"), limit=limit, root=get("root") or None)})
        elif self.path.startswith("/api/skill-detail?"):
            # the env vars one skill reads — same walk as plugin-detail, and
            # off the /api/state path for the same reason
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            get = lambda k, d="": (q.get(k) or [d])[0]
            try:
                self.send(200, {"name": get("name"),
                                "env": skill_env_vars(get("name"),
                                                      get("enabled", "1") == "1")})
            except (ValueError, OSError) as e:
                self.send(400, {"error": str(e)})
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
                                   bool(req.get("enabled")),
                                   item_scope(req.get("root")))
                self.send(200, {"ok": True, "path": path})
            elif action == "settings-set":
                key, value = req.get("key", ""), req.get("value")
                # Claude Code matches outputStyle exactly (frontmatter name,
                # not filename) and silently ignores a miss — repair the value
                # rather than store one that will never apply
                if key == "outputStyle":
                    value = output_styles.normalize_setting(value)
                settings_set(key, value)
                self.send(200, {"ok": True, "value": value})
            elif action == "path-save":
                self.send(200, {"ok": True, **path_save(
                    req.get("path", ""), req.get("content", ""),
                    req.get("base"))})
            elif action == "item-create":
                self.send(200, {"ok": True, **item_create(
                    req.get("type", ""), req.get("name", ""),
                    req.get("content", ""),
                    bool(req.get("enabled", True)),
                    item_scope(req.get("root")))})
            elif action == "item-delete":
                self.send(200, {"ok": True, **item_delete(
                    req.get("type", ""), req.get("name", ""),
                    bool(req.get("enabled", True)),
                    item_scope(req.get("root")))})
            elif action == "item-save":
                self.send(200, {"ok": True, **item_save(
                    req.get("type", ""), req.get("name", ""), req.get("file"),
                    req.get("content", ""), bool(req.get("enabled", True)),
                    req.get("base"), item_scope(req.get("root")))})
            elif action == "item-copy":
                # Either end may be the config dir: absent `from_root` copies
                # one of your own items into a project, absent `to_root`
                # copies a project's back out.
                self.send(200, {"ok": True, **item_copy(
                    req.get("type", ""), req.get("name", ""),
                    item_scope(req.get("from_root")),
                    item_scope(req.get("to_root")),
                    bool(req.get("enabled", True)))})
            elif action == "item-move":
                # same ends as item-copy above, but the source is removed
                self.send(200, {"ok": True, **item_move(
                    req.get("type", ""), req.get("name", ""),
                    item_scope(req.get("from_root")),
                    item_scope(req.get("to_root")),
                    bool(req.get("enabled", True)))})
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
            elif action == "setup-config":
                setup_config(req.get("id", ""), req.get("values") or {})
                self.send(200, {"ok": True})
            elif action == "local-config":
                local_config_set(req.get("base_url", ""), req.get("model", ""),
                                 req.get("api_key", ""))
                self.send(200, {"ok": True})
            elif action == "local-probe":
                # POST despite being read-only: it triggers network activity
                # and must stay behind the token
                self.send(200, local_probe(req.get("base_url")))
            elif action == "local-test":
                self.send(200, local_test())
            elif action == "project-add":
                registry_add(req.get("path", ""))
                self.send(200, {"ok": True})
            elif action == "project-remove":
                registry_remove(req.get("root", ""))
                self.send(200, {"ok": True})
            elif action == "project-init":
                project_init(req.get("root", ""), req.get("mode", ""))
                self.send(200, {"ok": True})
            elif action == "project-toggle":
                project_toggle(req.get("root", ""), bool(req.get("enabled")))
                self.send(200, {"ok": True})
            elif action == "project-mode":
                project_set_mode(req.get("root", ""), req.get("mode", ""))
                self.send(200, {"ok": True})
            elif action == "project-wrapper":
                wrapper_write(req.get("root", ""), bool(req.get("force")))
                self.send(200, {"ok": True})
            elif action == "project-check":
                self.send(200, wrapper_check(req.get("root", "")))
            elif action == "project-test":
                self.send(200, wrapper_test(req.get("root", "")))
            elif action == "project-restore-inspect":
                # a POST, not a GET, for the same reason project-check is one:
                # it names a project root, and the registry check that guards
                # that belongs behind the token like every other write path
                self.send(200, project_restore_inspect(
                    req.get("root", ""), req.get("name", "")))
            elif action == "project-restore":
                self.send(200, {"ok": True, **project_restore(
                    req.get("root", ""), req.get("name", ""),
                    req.get("paths") or [])})
            elif action == "project-mcp-set":
                self.send(200, {"ok": True, **project_mcp_set(
                    req.get("root", ""), req.get("name", ""),
                    req.get("config"))})
            elif action == "project-mcp-delete":
                self.send(200, {"ok": True, **project_mcp_set(
                    req.get("root", ""), req.get("name", ""), None)})
            elif action == "project-mcp-approve":
                # three answers, so the field is the tri-state itself rather
                # than a bool: absent means "un-answer", not "reject"
                self.send(200, {"ok": True, **project_mcp_approve(
                    req.get("root", ""), req.get("name", ""),
                    req.get("approved"))})
            elif action == "project-registry":
                # a POST like project-restore-inspect above, and for the same
                # reason: it names a project root, and it shells out
                self.send(200, registry_state(req.get("root", "")))
            elif action == "project-marketplace-add":
                self.send(200, marketplace_add(req.get("root", ""),
                                               req.get("source", "")))
            elif action == "project-marketplace-remove":
                self.send(200, marketplace_remove(req.get("root", ""),
                                                  req.get("name", "")))
            elif action == "project-plugin-install":
                self.send(200, plugin_install(req.get("root", ""),
                                              req.get("id", "")))
            elif action == "project-plugin-uninstall":
                self.send(200, plugin_uninstall(req.get("root", ""),
                                                req.get("id", "")))
            elif action == "user-registry":
                # a POST although it only reads: it shells out, like
                # project-registry above
                self.send(200, user_registry_state())
            elif action == "user-marketplace-add":
                self.send(200, user_marketplace_add(req.get("source", "")))
            elif action == "user-marketplace-remove":
                self.send(200, user_marketplace_remove(req.get("name", "")))
            elif action == "user-plugin-install":
                self.send(200, user_plugin_install(req.get("id", "")))
            elif action == "user-plugin-uninstall":
                self.send(200, user_plugin_uninstall(req.get("id", "")))
            elif action == "project-skill-override":
                self.send(200, {"ok": True, **project_skill_override(
                    req.get("root", ""), req.get("name", ""),
                    req.get("value"))})
            elif action == "project-setting-set":
                self.send(200, {"ok": True, **project_setting_set(
                    req.get("root", ""), req.get("key", ""), req.get("value"),
                    bool(req.get("local", True)))})
            elif action == "mcp-move":
                self.send(200, {"ok": True, **mcp_move(
                    req.get("name", ""), req.get("from"), req.get("to"))})
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
            elif action == "plugin-scope-move":
                self.send(200, {"ok": True, **plugin_scope_move(
                    req.get("id", ""), req.get("from"), req.get("to"))})
            elif action == "plugin-split":
                self.send(200, {"ok": True, **plugins_split(
                    req.get("id", ""), req.get("picks") or [],
                    bool(req.get("disable", True)),
                    req.get("models") or {})})
            elif action == "plugin-env-set":
                plugin_env_set(req.get("name", ""), req.get("value"))
                self.send(200, {"ok": True})
            elif action == "item-model-set":
                self.send(200, {"ok": True, **item_set_model(
                    req.get("name", ""), req.get("model", ""),
                    bool(req.get("enabled", True)),
                    item_scope(req.get("root")))})
            elif action == "plugin-resync":
                self.send(200, {"ok": True, **plugin_resync(
                    req.get("type", ""), req.get("name", ""))})
            elif action == "skill-override":
                skill_override_set(req.get("name", ""), req.get("value"))
                self.send(200, {"ok": True})
            elif action == "backup-dir":
                set_backup_dir((req.get("path") or "").strip())
                self.send(200, {"ok": True})
            elif action == "backup-create":
                self.send(200, {"ok": True, **backup_create(
                    req.get("picks") or [], req.get("note") or "",
                    req.get("units"))})
            elif action == "backup-restore":
                self.send(200, {"ok": True, **backup_restore(
                    req.get("name", ""), req.get("paths") or [])})
            elif action == "backup-delete":
                self.send(200, {"ok": True, **backup_delete(req.get("name", ""))})
            elif action == "fresh-start":
                self.send(200, {"ok": True, **fresh_start(
                    keep_transcripts=bool(req.get("keep_transcripts", True)))})
            elif action == "assist":
                self.send(200, assist(req.get("mode", ""), req.get("custom", ""),
                                      req.get("content", ""), req.get("path", "")))
            elif action == "catalog-install":
                # resolve_install is the gate: an unknown id, a non-installable
                # component, or a blocked marketplace raises ValueError *before*
                # any subprocess runs — caught below, with no `claude` call made.
                # The resolved id (the index's own copy) is what goes to the
                # CLI, never the raw request string past this point.
                scope = req.get("scope") or "user"
                pid = catalog.resolve_install(req.get("id", ""), scope)
                self.send(200, user_plugin_install(pid) if scope == "user"
                              else plugin_install(scope, pid))
            elif action == "catalog-marketplace-add":
                # the policy chokepoint: blockedMarketplaces/strictKnownMarketplaces
                # are checked here, before anything shells out. The source string
                # itself stands in for both the eventual marketplace name and its
                # owner/repo form — is_blocked() matches whichever applies.
                source = req.get("source", "")
                scope = req.get("scope") or "user"
                settings_data = settings_state()["data"]
                if catalog.is_blocked(source, source, settings_data):
                    self.send(400, {"error": f"{source}: blocked by your settings "
                                    "(blockedMarketplaces or strictKnownMarketplaces). "
                                    "That is Claude Code's rule, not ours — this app "
                                    "just won't offer you the button."})
                else:
                    self.send(200, user_marketplace_add(source) if scope == "user"
                                  else marketplace_add(scope, source))
            elif action == "discover-consent":
                # source is one of the two static-document keys, or skills_sh
                # (Phase 4's per-skill audit lookup) — a different consent
                # shape ({ok, query_ok}), so it gets its own setter rather
                # than routing through consent_set().
                source = req.get("source", "")
                if source == "skills_sh":
                    self.send(200, {"ok": True, "consent":
                                    remote.consent_set_skills_sh(bool(req.get("ok")))})
                elif source not in ("official", "community"):
                    self.send(400, {"error": f"{source!r}: not a source this app manages"})
                else:
                    self.send(200, {"ok": True,
                                    "consent": remote.consent_set(source, bool(req.get("ok")))})
            elif action == "catalog-refresh":
                # `sources` is a list of source KEYS, never URLs — remote.py's
                # fetch_source() enforces that on its own, but the request
                # shape here never carries anything else to enforce.
                sources = req.get("sources")
                if not isinstance(sources, list) or not sources:
                    self.send(400, {"error": "sources: non-empty list of source keys required"})
                else:
                    results = {}
                    for name in sources:
                        if not isinstance(name, str) or name not in ("official", "community"):
                            results[str(name)] = {"ok": False,
                                                  "detail": f"{name!r}: not a known discover source"}
                            continue
                        try:
                            results[name] = remote.fetch_source(name)
                        except ValueError as e:
                            results[name] = {"ok": False, "detail": str(e)}
                    self.send(200, {"results": results})
            elif action == "skill-audit":
                # id is resolved server-side against the catalog index, same
                # security property as catalog-install above: the request's
                # copy of `id` is used only to look it up via
                # catalog.get_entry(), then discarded — source/skill (what
                # actually reaches remote.audit_skill(), and from there
                # skills.sh) come from the resolved Entry, never the raw
                # request body.
                e = catalog.get_entry(req.get("id", ""))
                if e is None:
                    raise ValueError(f"{req.get('id', '')!r}: not in the index")
                if e.get("kind") != "skill":
                    raise ValueError(f"{e['id']}: not a skill — only skills can be audited")
                # skills.sh's audit path is an npm-style source/skill pair; the
                # closest thing an Entry has to "source" for a skill is the
                # marketplace it came from (set on skill entries by every
                # corpus that produces them — installed/ondisk/discover), not
                # the plugin's own package name, which the index does not
                # carry separately. A "yours" skill (not from any
                # marketplace) has no marketplace field and is refused.
                source = e.get("marketplace")
                if not source:
                    raise ValueError(f"{e['id']}: no marketplace/source on record — "
                                     "cannot audit a skill that isn't from a marketplace")
                self.send(200, remote.audit_skill(source, e["name"]))
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
