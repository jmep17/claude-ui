# claude-ui

A local web dashboard + editor for your live [Claude Code](https://claude.com/claude-code)
user-level configuration. Python 3 stdlib only — no dependencies, nothing to
install.

It shows everything actually on this machine and edits it in place — it never
owns, links, tracks, or syncs anything:

- **Inventory** of `<config>/skills/`, `commands/`, `agents/`, and
  `output-styles/`, with enable/disable (disabled items move to
  `<config>/disabled/`, a plain visible directory outside everything Claude
  Code scans — the filesystem is the entire state, no manifest).
- **Editor** for item files and the config-dir files `CLAUDE.md`,
  `settings.json`, and `keybindings.json`.
- **Settings** form editor for every documented `settings.json` key, with
  valid-option dropdowns and live value suggestions fetched from the public
  Claude Code docs.
- **MCP** inventory and toggling of user-scope servers (`~/.claude.json`).
- **Statusline** generator — a setup piece that writes a dependency-free
  statusline script into the config dir and points the `statusLine` key at it.
  Setup pieces are idempotent, derive their installed state by inspection, and
  are removable.
- **Doctor** for machine health: broken symlinks, leftover backups, hooks or
  statusline entries pointing at missing executables, and more.

## Run

```sh
git clone https://github.com/jmep17/claude-ui.git
claude-ui/bin/claude-ui
```

That starts a local server (default port 7333) and opens your browser.
Headless: `bin/claude-ui --no-open --port 7455`. Optionally add `bin/` to your
PATH.

## Config dir resolution

The Claude Code config dir is resolved in this order:

1. `.claude-ui.json` beside this checkout (machine-local, gitignored; can be
   set from the UI)
2. `$CLAUDE_CONFIG_DIR`
3. `~/.claude`

## Layout

`bin/claude-ui` is a thin launcher; the code lives in `bin/claude_ui/*.py`
(core → items/mcp/settings → statusline/insight/assist/setup → doctor →
server, a clean DAG). The frontend is plain files in `bin/claude_ui/static/`
(no build step).

---

Extracted from [jmep17/workspace](https://github.com/jmep17/workspace)@773bd864281c7bdcd0ece51ae4fcdfa236aaac92.
