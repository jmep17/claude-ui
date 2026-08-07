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
- **Plugins** inventory of everything under `<config>/plugins/`, with per-plugin
  enablement (`enabledPlugins`) and **Split** — Claude Code enables a plugin as
  a whole, so splitting copies just the components you tick into your own
  `agents/`, `commands/` and `skills/`, then turns the plugin off. Copies rather
  than masks, because a marketplace is a tarball extract with no git history and
  an update replaces the tree wholesale. Each copy records where it came from in
  its own frontmatter, so Doctor can flag it when the two diverge.
  Expanding a plugin shows **what model its agents run on** and why — the
  agent's own `model:` line, or `CLAUDE_CODE_SUBAGENT_MODEL` overriding all of
  them — and lets you set it on the copies that are yours, including at split
  time. Below that are **the environment variables the plugin reads**, found by
  reading its code: Claude Code has no per-agent model setting, so a plugin that
  wants one ships its own (caveman's `CAVECREW_REVIEWER_MODEL` and friends,
  documented in a README table three directories deep). They are ordinary
  `env` entries in `settings.json` once you know the names.
- **Statusline** generator — a setup piece that writes a dependency-free
  statusline script into the config dir and points the `statusLine` key at it.
  Setup pieces are idempotent, derive their installed state by inspection, and
  are removable.
- **Token saver** — a setup piece that points eight `settings.json` keys at
  cheaper defaults for pay-per-token API use: Sonnet main model, Haiku
  subagents, medium effort, smaller workflows, and fewer model-generated
  extras. Applied as one atomic write; Remove clears only the keys still at
  preset values, so anything you changed since stays yours.
- **Doctor** for machine health: broken symlinks, leftover backups, hooks or
  statusline entries pointing at missing executables, and more.
- **Backup** — a zip of the parts of your config that took work to build, kept
  outside the config dir, and restored file by file after a dry run. See below.

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
- `static/editor.js` — the file editor: a textarea over a tokenized `<pre>` for
  syntax highlighting and line numbers, a markdown toolbar, a formatter, a live
  split preview, and the findings strip that puts doctor warnings on the line
  they refer to.

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
(core → schema → settings → items/mcp/plugins → statusline/insight/assist/setup
→ doctor → server, a clean DAG). The frontend is plain files in `bin/claude_ui/static/`
(no build step), layered theme → components → app, with `ui.js` and `editor.js`
before `app.js`.

Tests are stdlib `unittest`: `python3 -m unittest discover tests`.

## Backup and restore

The Backup tab writes a zip you can reinstall Claude Code around. Tick what goes
in:

| Group | What it holds |
| :--- | :--- |
| Skills, commands, agents, output styles | Every file of every item, enabled and in `disabled/` |
| CLAUDE.md, settings.json, keybindings.json | Your memory file, all settings, key bindings |
| Statusline | `statusline.json` and the generated `statusline.sh`, executable bit kept |
| MCP servers | The `mcpServers` map from `~/.claude.json` |
| Plugin list and marketplaces | Which plugins and marketplaces you had — not their installed trees |
| Transcripts | `projects/**.jsonl`, which is where cost history comes from |

Three things are worth knowing:

- **Archives are kept outside the config dir**, defaulting to
  `$XDG_DATA_HOME/claude-ui/backups` (`~/.local/share/…`) and settable from the
  tab. Not `~/.claude/backups`: that is Claude Code's own, is pruned by
  `cleanupPeriodDays`, and is inside the directory an uninstall removes.
- **MCP configs are copied verbatim, credentials included.** A redacted copy
  would not restore. The tab says so before you create one, and any archive
  holding them is badged. Only the `mcpServers` map is taken — never the whole
  `~/.claude.json`, which also holds your project history and account.
- **Restore is a dry run first.** Every file is reported as *new*, *identical*
  or *differs* with a diff, and only the rows you leave ticked are written.
  Nothing is ever deleted, and MCP servers are merged into `~/.claude.json` one
  at a time so the rest of that file is untouched. A member that would land
  outside the config dir — including one whose target is a symlink pointing out
  of it — is refused rather than followed.

Reinstalling: restore the archive, restart Claude Code, and reinstall plugins
from their marketplaces (`claude plugin install`). Costs come back with the
transcripts — the Costs tab rescans them on next open, and its per-message
de-duplication means the totals match what they were.

## Settings help

Every settings row carries an ⓘ that opens the official description of the key,
its type, default and allowed values, and a link to the exact docs anchor. That
text comes from Claude Code's published JSON Schema, vendored at
`bin/claude_ui/data/settings_schema.json` so the app works offline, and
re-fetched in the background at start-up so it stays current.

To refresh the vendored copy:

```sh
python3 tools/sync_settings_schema.py           # fetch and write
python3 tools/sync_settings_schema.py --check   # diff only, exit 1 if stale
```

Read the diff before committing: a reworded description is upstream telling you
a setting changed meaning. `CLAUDE_UI_NET_TESTS=1 python3 -m unittest discover
tests` runs the same check as a test.

The hand-written entries in `settings.py` are never regenerated — they supply
the control to draw and the category to file each key under, and the schema
supplies the facts.

[`docs/IDEAS.md`](docs/IDEAS.md) is the ranked backlog — what is worth building
next and why, written against the code that exists.

---

Extracted from [jmep17/workspace](https://github.com/jmep17/workspace)@773bd864281c7bdcd0ece51ae4fcdfa236aaac92.
