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

## Interface

The UI is a hand-written port of the [shadcn/ui](https://ui.shadcn.com) design
system — its token contract, component anatomy, and variant matrix — as plain
CSS and DOM helpers. shadcn itself is React + Tailwind + Radix; none of that
fits a zero-dependency, no-build-step app, so what is ported is the part that
defines the look, not the runtime.

- `static/theme.css` — the token layer. Semantic pairs
  (`--background`/`--foreground`, `--card`, `--popover`, `--primary`, `--muted`,
  `--accent`, `--destructive`), plus `--border`/`--input`/`--ring`, a `--radius`
  scale, `--chart-1…5`, and an ANSI terminal palette for the statusline
  preview. A theme is nothing but a block of custom properties.
- `static/components.css` — the component layer, named after shadcn's own
  vocabulary: `.btn` and its variants, `.card`, `.badge`, `.input`,
  `.tabs-list`/`.tabs-trigger`, `.dialog-*`, `.dropdown-menu`, `.command-*`,
  `.table`, `.switch`, `.alert`, `.skeleton`, `.toast`. Nothing here knows a
  colour.
- `static/app.css` — layout and views, in terms of the two layers above.
- `static/ui.js` — the behavioural half: theme controller, a small inlined
  [lucide](https://lucide.dev) icon set, toasts, focus-trapped dialogs, dropdown
  menus, and the filterable combobox.

Four themes — **clay** (default), **slate**, **gruvbox**, **nord** — each in
light and dark, plus a *system* mode that follows `prefers-color-scheme`. The
choice is stored in `localStorage` and applied before first paint, so there is
no flash. Pick one from the header, or with <kbd>⌘K</kbd>.

Keyboard: <kbd>⌘K</kbd> command palette, <kbd>1</kbd>–<kbd>9</kbd> for tabs,
<kbd>/</kbd> to focus the current filter, <kbd>⌘S</kbd> to save in the editor,
<kbd>Esc</kbd> to close.

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
(no build step), layered theme → components → app, with `ui.js` before
`app.js`.

Tests are stdlib `unittest`: `python3 -m unittest discover tests`.

[`docs/IDEAS.md`](docs/IDEAS.md) is the ranked backlog — what is worth building
next and why, written against the code that exists.

---

Extracted from [jmep17/workspace](https://github.com/jmep17/workspace)@773bd864281c7bdcd0ece51ae4fcdfa236aaac92.
