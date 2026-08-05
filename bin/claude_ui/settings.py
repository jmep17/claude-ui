"""settings.json schema + editing, and lifecycle hooks (incl. test-fire)."""

from pathlib import Path
import functools
import json
import re
import subprocess
import threading
import urllib.request

from . import schema
from .core import CONFIG_FILES, atomic_write, config_dir, tilde


# User-scope settings.json keys. The entries below are hand-written and carry
# what the official JSON Schema cannot: which *control* to render, which
# category to file the key under, and a one-line description short enough to sit
# under the key name. Everything the official schema is authoritative about —
# allowed values, defaults, the long description, the exact docs URL — is merged
# in over the top at import time by schema.merge(). See schema.py.
#
# So: add a key here to make it appear in the UI, and let the merge supply its
# facts. Never paste a default or an enum list in by hand when the official
# schema has one; it will be overwritten, and the merge tests will say so.
#
# Control types the frontend understands:
#   bool             true/false dropdown (three-state with unset)
#   number           numeric input; optional "values" suggestions (rendered as a
#                    text input with datalist, since datalist on type=number is
#                    ignored by Safari/Firefox)
#   string           free text input (prefer combo when suggestions exist)
#   enum             fixed-choice dropdown; requires "values" (live docs-
#                    discovered values for the key are merged in as options)
#   combo            free text with suggested "values" (datalist); freeform still allowed
#   list             one-value-per-row editor; optional "item_values" suggestions per row
#   kv               key/value row editor. Optional "value_type": "number" for numeric
#                    values, or "values" to make each value a dropdown, or
#                    "key_values" to suggest keys (datalist; freeform allowed).
#   object           declared-field mini form; requires "fields":
#                    [{"key", "type", "values"?, "desc"?, "const"?}]. A field with
#                    "const" is always written and gets no input (e.g. type: "command").
#   json             raw-JSON textarea, for deeply nested / rarely-edited configs.
#                    Optional "templates": [{"name", "value"}] starter configs
#                    offered via an insert picker above the textarea.
# Any entry may also carry "aka": [...] — extra keywords the settings filter
# matches, for keys whose name and description don't contain the word people
# actually search for (e.g. "background", or a deprecated env var name).
# The frontend also merges live suggestions (git identity, installed skills,
# MCP server names, ...) into datalists — see suggestFor() in static/app.js.
# In string/combo inputs a literal "" (two quote characters) writes the empty
# string; a blank input unsets the key.
MODEL_ALIAS_NAMES = ["default", "best", "fable", "sonnet", "opus", "haiku",
                     "sonnet[1m]", "opus[1m]", "opusplan", "opusplan[1m]"]
# full model IDs (aliases resolve to these; snapshot 2026-07)
MODEL_IDS = ["claude-fable-5", "claude-opus-5", "claude-opus-4-8",
             "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
             "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5",
             "claude-haiku-4-5"]
MODEL_ALIASES = MODEL_ALIAS_NAMES + MODEL_IDS


def _family_first(fam):
    """Full model IDs, that family first — for the ANTHROPIC_DEFAULT_*_MODEL
    keys, which take an ID (an alias there would be circular)."""
    return ([m for m in MODEL_IDS if fam in m]
            + [m for m in MODEL_IDS if fam not in m])


# Settings keys whose values are model IDs. Live IDs scraped from the models
# doc are merged into each one's suggestions (see _fetch_docs_values). A ":key"
# suffix targets a kv control's key input, matching suggestFor() in app.js.
MODEL_VALUED_KEYS = [
    "model", "fallbackModel", "availableModels", "advisorModel",
    "modelOverrides:key",
    "env.ANTHROPIC_MODEL", "env.CLAUDE_CODE_SUBAGENT_MODEL",
    "env.ANTHROPIC_DEFAULT_OPUS_MODEL", "env.ANTHROPIC_DEFAULT_SONNET_MODEL",
    "env.ANTHROPIC_DEFAULT_HAIKU_MODEL", "env.ANTHROPIC_DEFAULT_FABLE_MODEL",
]
LANGS = ["en", "ja", "fr", "es", "de", "zh", "ko", "pt", "it", "ru"]
# `language` takes the English name of a language, not an ISO code — the docs
# example is "japanese"/"spanish"/"french".
LANG_NAMES = ["english", "japanese", "spanish", "french", "german", "chinese",
              "korean", "portuguese", "italian", "russian"]

# Env var names suggested as keys in the `env` editor. Derived from the official
# schema's env.* properties (340 of them) rather than kept by hand — the
# hand-maintained list had drifted in both directions. Suggestions only: any key
# can still be typed.

# Real vars the official schema omits: third-party (AWS/gcloud) credentials
# Claude Code reads through its SDKs, and the ubiquitous colour conventions.
ENV_EXTRA = [
    "AWS_CONFIG_FILE", "AWS_DEFAULT_REGION", "AWS_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE", "DISABLE_BUG_COMMAND",
    "ENABLE_PROMPT_CACHING_1H_BEDROCK", "FORCE_COLOR", "GCLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT", "NODE_TLS_REJECT_UNAUTHORIZED", "NO_COLOR",
]

# Documented, but Claude Code *sets* these in the subprocesses it spawns — they
# are signals to read from a hook, not settings to write. Suggesting them would
# invite writing a value that gets overwritten anyway.
ENV_READONLY = frozenset({
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_PROJECT_DIR", "CLAUDE_EFFORT",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_REMOTE",
    "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_TEAM_NAME",
})

ENV_VARS = sorted((set(ENV_EXTRA) | schema.env_var_names()) - ENV_READONLY)

SETTINGS_RAW = [
    {"key": "model", "type": "combo", "values": MODEL_ALIASES, "cat": "model",
     "aka": ["main", "session"],
     "desc": "Model for the main session — alias (opus, sonnet, haiku…) or full model ID; read at startup"},
    {"key": "fallbackModel", "type": "list", "item_values": MODEL_ALIASES, "cat": "model",
     "aka": ["overload"],
     "desc": "Fallback model chain tried in order on overload, max 3 models"},
    {"key": "effortLevel", "type": "enum", "values": ["low", "medium", "high", "xhigh"], "cat": "model",
     "desc": "Persist reasoning effort level across sessions"},
    {"key": "alwaysThinkingEnabled", "type": "bool", "cat": "model",
     "desc": "Enable extended thinking by default"},
    {"key": "thinkingBudgetTokens", "type": "number", "cat": "model",
     "values": [1024, 4096, 8192, 16000, 31999],
     "desc": "Token budget for extended thinking when always-thinking is on"},
    {"key": "advisorModel", "type": "combo", "values": ["opus", "sonnet"], "cat": "model",
     "desc": "Model for the server-side advisor tool — opus, sonnet, or a full model ID; unset to disable"},
    {"key": "fastMode", "type": "bool", "cat": "model",
     "desc": "Enable fast mode for sessions where available"},
    {"key": "fastModePerSessionOptIn", "type": "bool", "cat": "model",
     "desc": "Require per-session opt-in for fast mode"},
    {"key": "agent", "type": "combo", "values": [], "cat": "model",
     "aka": ["main thread", "subagent"],
     "desc": "Run the main thread as a named subagent, applying its prompt, tools, and model"},
    {"key": "availableModels", "type": "list", "item_values": MODEL_ALIASES, "cat": "model",
     "desc": "Allowlist of models selectable for the session, subagents, skills, and the advisor"},
    {"key": "switchModelsOnFlag", "type": "bool", "default": True, "cat": "model",
     "desc": "Switch to the fallback model when a safety classifier flags a request"},
    {"key": "modelOverrides", "type": "kv", "key_values": MODEL_ALIASES, "cat": "model",
     "desc": "Map Anthropic model IDs to provider-specific IDs, e.g. a Bedrock inference profile ARN"},

    # Model selection for subagents and for alias resolution lives in env vars,
    # not top-level keys. They are editable in the `env` map too, but nobody
    # finds them there — these rows put them in front of the model settings
    # with model-ID suggestions. Writing env.X nests under `env` (settings_set).
    {"key": "env.CLAUDE_CODE_SUBAGENT_MODEL", "type": "combo", "cat": "model",
     "values": ["inherit"] + MODEL_ALIASES,
     "aka": ["subagent", "task tool", "background", "delegate"],
     "desc": "Model for every subagent — alias, full model ID, or 'inherit'; "
             "overrides each agent's own model: frontmatter"},
    {"key": "env.ANTHROPIC_MODEL", "type": "combo", "values": MODEL_ALIASES, "cat": "model",
     "aka": ["launch", "session", "main"],
     "desc": "Model for the main session at launch — the env-var form of the `model` key above"},
    {"key": "env.ANTHROPIC_DEFAULT_HAIKU_MODEL", "type": "combo", "cat": "model",
     "values": _family_first("haiku"),
     "aka": ["background", "small fast model", "ANTHROPIC_SMALL_FAST_MODEL"],
     "desc": "Model the 'haiku' alias resolves to, and what background functionality uses; "
             "replaces the deprecated ANTHROPIC_SMALL_FAST_MODEL"},
    {"key": "env.ANTHROPIC_DEFAULT_OPUS_MODEL", "type": "combo", "cat": "model",
     "values": _family_first("opus"), "aka": ["alias"],
     "desc": "Model the 'opus' alias resolves to — a full model ID"},
    {"key": "env.ANTHROPIC_DEFAULT_SONNET_MODEL", "type": "combo", "cat": "model",
     "values": _family_first("sonnet"), "aka": ["alias"],
     "desc": "Model the 'sonnet' alias resolves to — a full model ID"},
    {"key": "env.ANTHROPIC_DEFAULT_FABLE_MODEL", "type": "combo", "cat": "model",
     "values": _family_first("fable"), "aka": ["alias"],
     "desc": "Model the 'fable' alias resolves to — a full model ID"},

    {"key": "permissions.defaultMode", "type": "enum", "cat": "permissions",
     "values": ["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "manual"],
     "desc": "Startup permission mode: prompt on first use / auto-accept edits / read-only plan / auto with safety checks / deny unless pre-approved / skip prompts / manual (alias of default)"},
    {"key": "permissions.allow", "type": "list", "cat": "permissions",
     "item_values": ["Bash(git diff *)", "Bash(npm run *)", "Read(~/notes/**)",
                     "Edit(docs/**)", "WebFetch(domain:docs.example.com)",
                     "WebSearch", "mcp__github__*"],
     "desc": "Rules to auto-approve, e.g. Bash(npm run test *)"},
    {"key": "permissions.ask", "type": "list", "cat": "permissions",
     "item_values": ["Bash(git push *)", "Bash(rm *)", "Edit(**)", "WebFetch"],
     "desc": "Rules that always require confirmation"},
    {"key": "permissions.deny", "type": "list", "cat": "permissions",
     "item_values": ["Read(./.env)", "Read(./secrets/**)", "Bash(curl *)",
                     "WebFetch"],
     "desc": "Rules to block, e.g. Read(./.env)"},
    {"key": "permissions.additionalDirectories", "type": "list", "cat": "permissions",
     "item_values": ["~/src", "~/notes"],
     "desc": "Extra directories Claude may access (like --add-dir)"},
    {"key": "permissions.disableBypassPermissionsMode", "type": "enum", "values": ["disable"], "cat": "permissions",
     "desc": "Set to 'disable' to prevent bypassPermissions mode"},
    {"key": "disableAutoMode", "type": "enum", "values": ["disable"], "cat": "permissions",
     "desc": "Set to 'disable' to keep auto mode off and drop it from the mode picker"},
    {"key": "useAutoModeDuringPlan", "type": "bool", "default": True, "cat": "permissions",
     "desc": "Use auto-mode semantics during plan mode when auto mode is available"},

    {"key": "env", "type": "kv", "key_values": ENV_VARS, "cat": "environment & hooks",
     "aka": ["environment variable", "env var"],
     "desc": "Environment variables applied to every session and subprocess. "
             "Model-related vars have dedicated rows under “model” above; they also appear here"},
    {"key": "hooks", "type": "json", "cat": "environment & hooks",
     "templates": [
         {"name": "notify when done", "value": {"Stop": [{"hooks": [
             {"type": "command",
              "command": "osascript -e 'display notification \"Claude is done\""
                         " with title \"claude\"'"}]}]}},
         {"name": "guard bash commands", "value": {"PreToolUse": [
             {"matcher": "Bash", "hooks": [
                 {"type": "command",
                  "command": "~/.claude/hooks/check-bash.sh"}]}]}},
         {"name": "format after edits", "value": {"PostToolUse": [
             {"matcher": "Edit|Write", "hooks": [
                 {"type": "command",
                  "command": "~/.claude/hooks/format-file.sh"}]}]}},
     ],
     "desc": "Lifecycle hooks — use the hooks builder above; edit here only for non-standard shapes"},
    {"key": "disableAllHooks", "type": "bool", "cat": "environment & hooks",
     "desc": "Disable all hooks and custom status line"},
    {"key": "statusLine", "type": "object", "cat": "environment & hooks",
     "desc": "Custom status line: a command whose first stdout line is the status line",
     "fields": [
         {"key": "type", "const": "command"},
         {"key": "command", "type": "combo", "values": ["~/.claude/statusline.sh"],
          "desc": "command run each refresh, e.g. ~/.claude/statusline.sh"},
         {"key": "padding", "type": "number", "values": [0, 1, 2],
          "desc": "leading spaces (0 hugs the edge)"},
         {"key": "refreshInterval", "type": "number", "values": [300, 1000, 5000],
          "desc": "refresh interval in ms"},
         {"key": "hideVimModeIndicator", "type": "bool", "desc": "hide the vim mode indicator"},
     ]},
    {"key": "subagentStatusLine", "type": "object", "cat": "environment & hooks",
     "desc": "Custom row body for each subagent in the agent panel, in place of name · description · tokens",
     "fields": [
         {"key": "type", "const": "command"},
         {"key": "command", "type": "combo",
          "values": ["~/.claude/subagent-statusline.sh"],
          "desc": "command run per subagent row"},
     ]},
    {"key": "allowedHttpHookUrls", "type": "list", "cat": "environment & hooks",
     "item_values": ["https://hooks.example.com/*"],
     "desc": "URL patterns HTTP hooks may target ('*' wildcards); an empty array blocks every HTTP hook"},
    {"key": "httpHookAllowedEnvVars", "type": "list", "cat": "environment & hooks",
     "desc": "Environment variable names HTTP hooks may interpolate into request headers"},

    {"key": "editorMode", "type": "enum", "values": ["normal", "vim"], "default": "normal", "cat": "interface",
     "desc": "Key binding mode for the input prompt"},
    {"key": "tui", "type": "enum", "values": ["default", "fullscreen"], "cat": "interface",
     "desc": "TUI renderer mode"},
    {"key": "theme", "type": "combo", "cat": "interface", "default": "dark",
     "values": ["auto", "dark", "light", "dark-daltonized", "light-daltonized", "dark-ansi", "light-ansi"],
     "desc": "Color theme; also custom:<slug> for a themes/ file or custom:<plugin>:<slug> for a plugin theme"},
    {"key": "interfaceLanguage", "type": "combo", "values": LANGS, "cat": "interface",
     "desc": "Interface language, e.g. en, ja, fr"},
    {"key": "language", "type": "combo", "values": LANG_NAMES, "cat": "interface",
     "desc": "Preferred language for Claude's responses, as an English name like japanese or spanish"},
    {"key": "outputStyle", "type": "combo", "values": ["default", "Explanatory", "Learning"], "cat": "interface",
     "desc": "Output rendering style (read at startup); your installed styles are suggested"},
    {"key": "preferredNotifChannel", "type": "enum", "cat": "interface", "default": "auto",
     "values": ["auto", "terminal_bell", "iterm2", "iterm2_with_bell", "kitty", "ghostty", "notifications_disabled"],
     "desc": "How desktop notifications are delivered"},
    {"key": "viewMode", "type": "enum", "values": ["default", "verbose", "focus"], "cat": "interface",
     "desc": "Startup transcript view mode"},
    {"key": "spinnerTipsEnabled", "type": "bool", "default": True, "cat": "interface",
     "desc": "Show tips while waiting for the model"},
    {"key": "autoScrollEnabled", "type": "bool", "default": True, "cat": "interface",
     "desc": "Follow new output to the bottom in fullscreen rendering"},
    {"key": "strikethrough", "type": "bool", "default": True, "cat": "interface",
     "desc": "Show strikethrough for deleted text"},
    {"key": "awaySummaryEnabled", "type": "bool", "default": True, "cat": "interface",
     "desc": "One-line session recap when returning after time away"},
    {"key": "interactiveEditingEnabled", "type": "bool", "default": True, "cat": "interface",
     "desc": "Inline editing UI for applying changes"},
    {"key": "askUserQuestionTimeout", "type": "enum", "values": ["60s", "5m", "10m", "never"],
     "default": "never", "cat": "interface",
     "desc": "Idle time before unanswered question dialogs auto-continue"},
    {"key": "axScreenReader", "type": "bool", "cat": "interface",
     "desc": "Screen-reader friendly flat text output"},
    {"key": "showHiddenFiles", "type": "bool", "default": False, "cat": "interface",
     "desc": "Show hidden files in file operations"},
    {"key": "keyBindings", "type": "json", "cat": "interface",
     "templates": [
         {"name": "rebind example", "value": {"bindings": [
             {"context": "Chat", "bindings": {
                 "ctrl+g": None, "ctrl+e": "chat:externalEditor"}}]}},
     ],
     "desc": "Custom keybindings for the input prompt"},
    {"key": "fileSuggestion", "type": "object", "cat": "interface",
     "desc": "Custom script for @-file autocomplete",
     "fields": [
         {"key": "type", "const": "command"},
         {"key": "command", "type": "combo",
          "values": ["rg --files", "fd --type f --hidden"],
          "desc": "command that emits candidate paths"},
     ]},
    {"key": "verbose", "type": "bool", "default": False, "cat": "interface",
     "desc": "Show full tool output instead of truncated summaries"},
    {"key": "showTurnDuration", "type": "bool", "default": True, "cat": "interface",
     "desc": "Show the turn duration line after each response, e.g. \"Cooked for 1m 6s\""},
    {"key": "showThinkingSummaries", "type": "bool", "default": False, "cat": "interface",
     "desc": "Show extended thinking summaries instead of a collapsed redacted stub"},
    {"key": "showClearContextOnPlanAccept", "type": "bool", "default": False, "cat": "interface",
     "desc": "Offer the clear-context option on the plan accept screen"},
    {"key": "respondToBashCommands", "type": "bool", "default": True, "cat": "interface",
     "desc": "Reply after an input-box ! shell command; false just adds its output to context"},
    {"key": "emojiCompletionEnabled", "type": "bool", "default": True, "cat": "interface",
     "desc": "Suggest and replace emoji shortcodes typed as :name: in the prompt input"},
    {"key": "syntaxHighlightingDisabled", "type": "bool", "cat": "interface",
     "desc": "Disable syntax highlighting in diffs, code blocks, and file previews"},
    {"key": "terminalProgressBarEnabled", "type": "bool", "default": True, "cat": "interface",
     "desc": "Show the terminal progress bar in ConEmu, Ghostty 1.2+, and iTerm2 3.6.6+"},
    {"key": "wheelScrollAccelerationEnabled", "type": "bool", "default": True, "cat": "interface",
     "desc": "Accelerate mouse-wheel scrolling during fast scrolls in fullscreen rendering"},
    {"key": "prefersReducedMotion", "type": "bool", "cat": "interface",
     "desc": "Reduce or disable UI animations — spinners, shimmer, flash effects"},
    {"key": "vimInsertModeRemaps", "type": "kv", "values": ["<Esc>"], "cat": "interface",
     "desc": "Two-key INSERT-mode sequences mapped to Escape, e.g. jj → <Esc>; needs editorMode vim"},
    {"key": "voice", "type": "object", "cat": "interface",
     "desc": "Voice dictation: turn it on, pick hold or tap, and auto-submit on key release",
     "fields": [
         {"key": "enabled", "type": "bool", "desc": "enable voice dictation"},
         {"key": "mode", "type": "enum", "values": ["hold", "tap"],
          "desc": "hold the key to record, or tap to start and stop"},
         {"key": "autoSubmit", "type": "bool",
          "desc": "send the prompt as soon as the key is released"},
     ]},
    {"key": "voiceEnabled", "type": "bool", "cat": "interface",
     "desc": "Legacy alias for voice.enabled — prefer the voice object"},
    {"key": "spinnerVerbs", "type": "json", "cat": "interface",
     "templates": [
         {"name": "append to the defaults",
          "value": {"mode": "append", "verbs": ["Pondering", "Crafting"]}},
         {"name": "replace the defaults",
          "value": {"mode": "replace", "verbs": ["Working"]}},
     ],
     "desc": "Custom in-progress verbs: {\"mode\": \"append\"|\"replace\", \"verbs\": [...]}"},
    {"key": "spinnerTipsOverride", "type": "json", "cat": "interface",
     "templates": [
         {"name": "only my tips",
          "value": {"excludeDefault": True, "tips": ["Use our internal tool X"]}},
     ],
     "desc": "Custom spinner tips: {\"tips\": [...], \"excludeDefault\": true}"},
    {"key": "footerLinksRegexes", "type": "json", "cat": "interface",
     "templates": [
         {"name": "link ticket ids", "value": [
             {"pattern": "([A-Z]+-\\d+)",
              "url": "https://jira.example.com/browse/{1}"}]},
     ],
     "desc": "Regex-to-URL rules that render clickable badges in the footer when turn output matches"},

    {"key": "attribution.commit", "type": "combo", "cat": "git",
     "values": ['""', "Co-Authored-By: Claude <noreply@anthropic.com>"],
     "desc": "Custom commit attribution trailer (type \"\" to set empty, which hides it)"},
    {"key": "attribution.pr", "type": "combo", "cat": "git",
     "values": ['""', "Generated with [Claude Code](https://claude.com/claude-code)"],
     "desc": "Custom PR attribution string (type \"\" to set empty, which hides it)"},
    {"key": "attribution.sessionUrl", "type": "bool", "default": True, "cat": "git",
     "desc": "Append a Claude-Session trailer from web/Remote Control sessions"},
    {"key": "gitAttributionName", "type": "combo", "values": [], "cat": "git",
     "desc": "Name for commits/PRs when different from git config"},
    {"key": "gitAttributionEmail", "type": "combo", "values": [], "cat": "git",
     "desc": "Email for commits/PRs when different from git config"},
    {"key": "includeCoAuthoredBy", "type": "bool", "cat": "git",
     "desc": "Co-authored-by trailer in commits (older versions; superseded by attribution)"},
    {"key": "includeGitInstructions", "type": "bool", "default": True, "cat": "git",
     "desc": "Include the built-in commit/PR instructions and git status snapshot in the system prompt"},
    {"key": "prUrlTemplate", "type": "combo", "cat": "git",
     "values": ["https://reviews.example.com/{owner}/{repo}/pull/{number}"],
     "desc": "URL template for the PR footer badge; substitutes {host} {owner} {repo} {number} {url}"},

    {"key": "autoMemoryEnabled", "type": "bool", "default": True, "cat": "memory & context",
     "desc": "Auto memory: Claude reads and writes its memory directory"},
    {"key": "autoMemoryDirectory", "type": "combo", "values": ["~/.claude/memory"],
     "cat": "memory & context",
     "desc": "Custom auto-memory directory (absolute or ~/ path)"},
    {"key": "claudeMdExcludes", "type": "list", "cat": "memory & context",
     "item_values": ["**/node_modules/**", "**/.venv/**", "**/vendor/**",
                     "**/CLAUDE.local.md"],
     "desc": "Glob patterns of CLAUDE.md files to skip"},
    {"key": "autoCompactEnabled", "type": "bool", "default": True, "cat": "memory & context",
     "desc": "Auto-compact conversation near the context limit"},
    {"key": "maxCompactMessages", "type": "number", "values": [20, 50, 100],
     "cat": "memory & context",
     "desc": "Max messages to compact in one operation"},
    {"key": "sessionHistorySize", "type": "number", "values": [100, 500, 1000],
     "cat": "memory & context",
     "desc": "Max messages retained in session history"},
    {"key": "cleanupPeriodDays", "type": "number", "values": [1, 7, 30, 90, 365],
     "default": 30, "cat": "memory & context",
     "desc": "Days before session files auto-delete (min 1; 0 is a validation error)"},
    {"key": "plansDirectory", "type": "combo", "default": "~/.claude/plans",
     "values": ["~/.claude/plans", "./plans"], "cat": "memory & context",
     "desc": "Where plan files are stored; a relative path resolves from the project root"},

    {"key": "enableAllProjectMcpServers", "type": "bool", "cat": "mcp & plugins",
     "desc": "Auto-approve every MCP server in project .mcp.json"},
    {"key": "enabledMcpjsonServers", "type": "list", "cat": "mcp & plugins",
     "desc": "Specific .mcp.json servers to approve"},
    {"key": "disabledMcpjsonServers", "type": "list", "cat": "mcp & plugins",
     "desc": "Specific .mcp.json servers to reject"},
    {"key": "mcpServerTimeouts", "type": "kv", "value_type": "number", "cat": "mcp & plugins",
     "desc": "Per-server startup timeout in seconds, e.g. github → 30"},
    {"key": "extraKnownMarketplaces", "type": "json", "cat": "mcp & plugins",
     "templates": [
         {"name": "a github marketplace", "value": {
             "acme-tools": {"source": {"source": "github",
                                       "repo": "acme-corp/claude-plugins"}}}},
         {"name": "a local marketplace", "value": {
             "my-marketplace": {"source": {"source": "directory",
                                           "path": "~/src/my-marketplace"}}}},
     ],
     "desc": "Extra plugin marketplaces, keyed by name, each with a source object"},
    {"key": "enabledPlugins", "type": "json", "cat": "mcp & plugins",
     "templates": [
         {"name": "enable / disable a plugin",
          "value": {"formatter@acme-tools": True, "analyzer@acme-tools": False}},
     ],
     "desc": "Per-plugin enablement keyed by plugin@marketplace"},
    {"key": "pluginConfigs", "type": "json", "cat": "mcp & plugins",
     "templates": [
         {"name": "plugin option values",
          "value": {"formatter@acme-tools": {"style": "compact"}}},
     ],
     "desc": "Plugin userConfig values by plugin ID; user/managed scope only, project settings ignored"},
    {"key": "skillOverrides", "type": "kv", "cat": "mcp & plugins",
     "values": ["on", "name-only", "user-invocable-only", "off"],
     "desc": "Per-skill visibility override (skill name → visibility)"},
    {"key": "skillListingBudgetFraction", "type": "number", "values": [0.01, 0.02, 0.05],
     "default": 0.01, "cat": "mcp & plugins",
     "desc": "Fraction of the context window reserved for the per-turn skill listing"},
    {"key": "skillListingMaxDescChars", "type": "number", "values": [1024, 1536, 2048],
     "default": 1536, "cat": "mcp & plugins",
     "desc": "Per-skill cap on the description text shown in the skill listing"},
    {"key": "disableBundledSkills", "type": "bool", "cat": "mcp & plugins",
     "desc": "Disable bundled skills and workflows"},
    {"key": "disableClaudeAiConnectors", "type": "bool", "cat": "mcp & plugins",
     "desc": "Disable auto-fetch of claude.ai MCP connectors"},

    {"key": "sandbox", "type": "json", "cat": "sandbox & security",
     "templates": [
         {"name": "basic sandbox", "value": {
             "enabled": True, "autoAllowBashIfSandboxed": True,
             "excludedCommands": ["docker *"]}},
         {"name": "locked down", "value": {
             "enabled": True,
             "network": {"allowedDomains": ["api.anthropic.com", "github.com",
                                            "*.githubusercontent.com"]},
             "filesystem": {"denyRead": ["~/.ssh", "~/.aws"]}}},
     ],
     "desc": "Sandbox config: {\"enabled\": true, \"filesystem\": {...}, \"network\": {...}}"},
    {"key": "warningOnSandboxEscape", "type": "bool", "default": True, "cat": "sandbox & security",
     "desc": "Warn when processes escape the sandbox"},
    {"key": "disableSkillShellExecution", "type": "bool", "cat": "sandbox & security",
     "desc": "Disable inline shell execution in skills/commands"},
    {"key": "invalidSSLWarning", "type": "bool", "cat": "sandbox & security",
     "desc": "Warn about self-signed certificates"},
    {"key": "apiKeyHelper", "type": "combo",
     "values": ["~/.claude/api-key-helper.sh"], "cat": "sandbox & security",
     "desc": "Command generating an auth value (sent as X-Api-Key / Authorization)"},
    {"key": "awsAuthRefresh", "type": "combo",
     "values": ["aws sso login --profile=default"], "cat": "sandbox & security",
     "desc": "Script refreshing AWS credentials (.aws directory)"},
    {"key": "awsCredentialExport", "type": "combo",
     "values": ["aws configure export-credentials --format json"],
     "cat": "sandbox & security",
     "desc": "Script printing JSON with AWS credentials"},
    {"key": "gcpAuthRefresh", "type": "combo",
     "values": ["gcloud auth application-default login"],
     "cat": "sandbox & security",
     "desc": "Script refreshing GCP Application Default Credentials when they expire"},
    {"key": "skipWebFetchPreflight", "type": "bool", "cat": "sandbox & security",
     "desc": "Skip the WebFetch domain safety check that asks api.anthropic.com about each hostname"},
    {"key": "forceLoginMethod", "type": "enum",
     "values": ["claudeai", "console", "gateway"], "cat": "sandbox & security",
     "desc": "Restrict login to Claude.ai, Claude Console, or a cloud gateway"},
    {"key": "forceLoginOrgUUID", "type": "combo", "cat": "sandbox & security",
     "desc": "Require login to a specific Anthropic org UUID"},
    {"key": "proxy", "type": "json", "cat": "sandbox & security",
     "templates": [
         {"name": "http proxy example", "value": {
             "url": "http://user:pass@proxy.example.com:8080",
             "noProxy": ["localhost", "127.0.0.1"]}},
     ],
     "desc": "HTTP proxy configuration"},
    {"key": "autoMode", "type": "json", "cat": "sandbox & security",
     "templates": [
         {"name": "built-in defaults", "value": {
             "environment": ["$defaults"], "allow": ["$defaults"],
             "soft_deny": [], "hard_deny": []}},
     ],
     "desc": "Auto-mode classifier rules: environment/allow/soft_deny/hard_deny arrays"},

    {"key": "autoUpdatesChannel", "type": "enum", "values": ["latest", "stable"],
     "default": "latest", "cat": "system",
     "desc": "Release channel for auto-updates"},
    {"key": "defaultShell", "type": "enum", "values": ["bash", "powershell"], "cat": "system",
     "desc": "Shell for input-box ! commands"},
    {"key": "teammateMode", "type": "enum", "values": ["in-process", "auto", "tmux", "iterm2"],
     "default": "in-process", "cat": "system",
     "desc": "How agent-team members are displayed"},
    {"key": "restartOnConfigChange", "type": "bool", "cat": "system",
     "desc": "Restart session when config files change"},
    {"key": "telemetryEnabled", "type": "bool", "cat": "system",
     "desc": "Telemetry collection"},
    {"key": "otelHeadersHelper", "type": "combo",
     "values": ["/bin/generate_otel_headers.sh"], "cat": "system",
     "desc": "Script generating dynamic OpenTelemetry headers; runs at startup and periodically"},
    {"key": "fileCheckpointingEnabled", "type": "bool", "default": True, "cat": "system",
     "desc": "Snapshot files before edits for /rewind"},
    {"key": "workspaceInitScript", "type": "combo",
     "values": ["~/.claude/workspace-init.sh"], "cat": "system",
     "desc": "Script run when opening a new workspace"},
    {"key": "skipFirstRunQuestions", "type": "bool", "cat": "system",
     "desc": "Skip first-run setup questions"},
    {"key": "respectGitignore", "type": "bool", "default": True, "cat": "system",
     "desc": "Whether the @ file picker respects .gitignore patterns"},
    {"key": "llmConnectionTimeout", "type": "number", "values": [10, 30, 60],
     "cat": "system",
     "desc": "Model connection timeout (seconds)"},
    {"key": "llmRequestTimeout", "type": "number", "values": [60, 300, 600],
     "cat": "system",
     "desc": "Overall model request timeout (seconds)"},
    {"key": "feedbackSurveyRate", "type": "number", "values": [0, 0.05, 0.25, 1],
     "cat": "system",
     "desc": "Probability 0–1 of the session quality survey"},
    {"key": "disableAgentView", "type": "bool", "cat": "system",
     "desc": "Disable background agents, agent view, supervisor"},
    {"key": "disableArtifact", "type": "bool", "cat": "system",
     "desc": "Disable the Artifact tool (publishes to claude.ai)"},
    {"key": "enableArtifact", "type": "bool", "cat": "system",
     "desc": "Enable the Artifact tool for this user; unset follows account availability"},
    {"key": "disableRemoteControl", "type": "bool", "cat": "system",
     "desc": "Disable Remote Control"},
    {"key": "remoteControlAtStartup", "type": "bool", "cat": "system",
     "desc": "Connect Remote Control automatically when an interactive session starts"},
    {"key": "agentPushNotifEnabled", "type": "bool", "default": False, "cat": "system",
     "desc": "Proactive push notifications when Remote Control is connected"},
    {"key": "inputNeededNotifEnabled", "type": "bool", "default": False, "cat": "system",
     "desc": "Push to your phone when a permission prompt or question needs input"},
    {"key": "remote.defaultEnvironmentId", "type": "combo", "cat": "system",
     "values": ["env_0123abcd"],
     "desc": "Default cloud environment for sessions created with claude --cloud"},
    {"key": "disableWorkflows", "type": "bool", "default": False, "cat": "system",
     "desc": "Disable dynamic workflows and bundled workflow commands"},
    {"key": "workflowKeywordTriggerEnabled", "type": "bool", "default": True, "cat": "system",
     "desc": "Whether typing 'ultracode' in a prompt triggers a dynamic workflow"},
    {"key": "workflowSizeGuideline", "type": "enum", "default": "medium", "cat": "system",
     "values": ["unrestricted", "small", "medium", "large"],
     "desc": "Agent count Claude aims for in the dynamic workflows it writes"},
    {"key": "minimumVersion", "type": "combo", "values": ["2.1.150"], "cat": "system",
     "desc": "Version floor that stops auto-updates and claude update installing anything older"},
    {"key": "processWrapper", "type": "combo", "cat": "system",
     "values": ["/opt/corp/launcher --profile claude"],
     "desc": "Corporate launcher placed in front of the background processes Claude Code starts"},
    {"key": "disableDeepLinkRegistration", "type": "enum", "values": ["disable"], "cat": "system",
     "desc": "Set to 'disable' to skip registering the claude-cli:// protocol handler"},
    {"key": "sshConfigs", "type": "json", "cat": "system",
     "templates": [
         {"name": "one host", "value": [
             {"id": "devbox", "name": "Dev box", "host": "dev.example.com",
              "user": "me"}]},
     ],
     "desc": "SSH connections offered in the Desktop environment dropdown"},
    {"key": "companyAnnouncements", "type": "list", "cat": "system",
     "desc": "Startup announcements shown to users; cycled at random when several are set"},
    {"key": "worktree.baseRef", "type": "enum", "values": ["fresh", "head"],
     "default": "fresh", "cat": "system",
     "desc": "Base ref for new worktrees: clean tree from the remote, or current HEAD"},
    {"key": "worktree.bgIsolation", "type": "enum", "values": ["worktree", "none"],
     "default": "worktree", "cat": "system",
     "desc": "Isolate background agents in their own worktree"},
    {"key": "worktree.symlinkDirectories", "type": "list", "cat": "system",
     "item_values": ["node_modules", ".cache", "vendor"],
     "desc": "Directories symlinked from the main repository into each worktree"},
    {"key": "worktree.sparsePaths", "type": "list", "cat": "system",
     "item_values": ["packages/my-app", "shared/utils"],
     "desc": "Directories checked out in each worktree via git sparse-checkout"},
]

# Keys that only do something in managed/enterprise scope. They are listed so
# the settings tab can answer "what is allowManagedHooksOnly", not because you
# would set one here — the UI files them in their own collapsed group, marked as
# no-ops in user scope.
#
# This list is explicit rather than derived from schema.is_managed(). The
# "(Managed settings)" prefix is a prose convention, and it also tags
# disableAgentView and sshConfigs, which the app has always shipped as ordinary
# user-facing rows under "system". Deriving placement from it would silently
# relocate two working rows; the flag badges, this list places.
MANAGED_KEYS = [
    ("allowManagedHooksOnly", "bool"), ("allowManagedMcpServersOnly", "bool"),
    ("allowManagedPermissionRulesOnly", "bool"), ("allowAllClaudeAiMcps", "bool"),
    ("allowedChannelPlugins", "list"), ("allowedMcpServers", "json"),
    ("deniedMcpServers", "json"), ("managedMcpServers", "json"),
    ("blockedMarketplaces", "list"), ("strictKnownMarketplaces", "list"),
    ("pluginSuggestionMarketplaces", "list"), ("pluginTrustMessage", "string"),
    ("strictPluginOnlyCustomization", "json"), ("disableSideloadFlags", "bool"),
    ("policyHelper", "json"), ("parentSettingsBehavior", "enum"),
    ("forceRemoteSettingsRefresh", "bool"), ("forceLoginGatewayUrl", "string"),
    ("enforceAvailableModels", "bool"), ("claudeMd", "string"),
    ("channelsEnabled", "bool"), ("browserExternalPageTools", "enum"),
    ("disableBrowserExternalNavigation", "bool"),
    ("disableMobileSimulatorTools", "bool"), ("requireCoworkFullVmSandbox", "bool"),
    ("sshHostAllowlist", "list"), ("wslInheritsWindowsSettings", "bool"),
    ("requiredMinimumVersion", "string"), ("requiredMaximumVersion", "string"),
]

# Keys the official schema documents that the hand-written list above missed.
# Their descriptions, allowed values and defaults all come from the merge, so
# each entry only says which control to draw and where to file it.
SETTINGS_RAW += [
    {"key": "skipDangerousModePermissionPrompt", "type": "bool", "cat": "permissions",
     "desc": "Record that you've accepted the bypassPermissions dialog, so it stops "
             "appearing. Normally written by the CLI, not by hand"},
    {"key": "permissions.disableAutoMode", "type": "enum", "values": ["disable"],
     "cat": "permissions",
     "desc": "Set to 'disable' to keep auto mode off. Note the top-level "
             "disableAutoMode above does the same thing; both are documented"},
    {"key": "skippedPlugins", "type": "list", "cat": "mcp & plugins",
     "desc": "Plugins (plugin@marketplace) you declined when prompted, so you "
             "aren't asked again"},
    {"key": "skippedMarketplaces", "type": "list", "cat": "mcp & plugins",
     "desc": "Marketplaces you declined to install when prompted"},
] + [{"key": k, "type": t, "cat": schema.MANAGED_CAT, "desc": ""}
     for k, t in MANAGED_KEYS]

# dedupe (keep first occurrence)
_seen = set()

SETTINGS_RAW = [s for s in SETTINGS_RAW
                if not (s["key"] in _seen or _seen.add(s["key"]))]

# Official facts (allowed values, defaults, docs URL, managed/unverified flags)
# applied over the hand-written entries. Recomputed by settings_schema() once a
# live fetch lands; this is the boot-time value everything else imports.
SETTINGS_SCHEMA = schema.merge(SETTINGS_RAW)

_schema_cache = (-1, SETTINGS_SCHEMA)

def settings_schema():
    """The merged schema, refreshed when a live schema fetch has landed."""
    global _schema_cache
    gen = schema.generation()
    if _schema_cache[0] != gen:
        _schema_cache = (gen, schema.merge(SETTINGS_RAW))
    return _schema_cache[1]

SETTINGS_KEY_RE = re.compile(r"^[A-Za-z0-9_$][A-Za-z0-9_.$-]*$")

# Live option values fetched once in the background at server start from the
# public docs (no auth needed); on any failure the statics above remain the
# fallback. Two sources:
#   - models overview page: "Claude API ID/alias" table rows → model IDs,
#     merged into every key in MODEL_VALUED_KEYS
#   - settings reference page: per-key table rows, where allowed values appear
#     as backtick-wrapped quoted strings (`"latest"`) — merged into that
#     setting's options/suggestions (incl. enum dropdowns, via suggestFor)
MODELS_DOC_URL = "https://platform.claude.com/docs/en/about-claude/models/overview.md"
SETTINGS_DOC_URL = "https://code.claude.com/docs/en/settings.md"

_docs_values: dict = {}

def _get(url):
    req = urllib.request.Request(url, headers={"user-agent": "claude-ui"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode(errors="replace")

def _fetch_docs_values():
    found = {}
    try:
        ids = []
        for line in _get(MODELS_DOC_URL).splitlines():
            if line.lstrip("| *").startswith("Claude API"):
                ids += [c for c in (c.strip() for c in line.split("|"))
                        if re.fullmatch(r"claude-[a-z0-9-]+", c)]
        if ids:
            ids = list(dict.fromkeys(ids))
            # a fresh list per key — the second pass below appends to these
            for k in MODEL_VALUED_KEYS:
                found[k] = list(ids)
    except (OSError, ValueError):
        pass
    try:
        keys = {s["key"] for s in SETTINGS_SCHEMA
                if s["type"] in ("enum", "combo", "string", "number", "list")}
        for line in _get(SETTINGS_DOC_URL).splitlines():
            if not line.startswith("|"):
                continue
            key = line.split("|")[1].strip().strip("`")
            if key not in keys:
                continue
            vals = [v for v in dict.fromkeys(re.findall(r'`"([^"`]*)"`', line))
                    if len(v) <= 40]
            if vals:
                found[key] = found.get(key, []) + vals
    except (OSError, ValueError, IndexError):
        pass
    _docs_values.update(found)

def start_docs_fetch():
    threading.Thread(target=_fetch_docs_values, daemon=True).start()

def suggest_state():
    out = dict(_local_suggest())
    for key, vals in _docs_values.items():
        out[key] = list(dict.fromkeys(out.get(key, []) + vals))
    return out

@functools.lru_cache(maxsize=1)
def _local_suggest():
    """Machine-local datalist suggestions, keyed by settings key (dotted for
    object subfields). Cached for the server's lifetime — restart to pick up
    a changed git identity or new scripts."""
    def git_config(key):
        try:
            r = subprocess.run(["git", "config", "--get", key],
                               cwd=str(Path.home()), capture_output=True,
                               text=True, timeout=2)
            v = r.stdout.strip()
            return [v] if r.returncode == 0 and v else []
        except (OSError, subprocess.SubprocessError):
            return []

    out = {"gitAttributionName": git_config("user.name"),
           "gitAttributionEmail": git_config("user.email"),
           "autoMemoryDirectory": [tilde(config_dir() / "memory")]}
    scripts = sorted(tilde(p) for p in config_dir().glob("*.sh"))
    for key in ("statusLine.command", "apiKeyHelper", "workspaceInitScript"):
        out[key] = scripts
    try:
        out["permissions.additionalDirectories"] = sorted(
            "~/" + p.name for p in Path.home().iterdir()
            if p.is_dir() and not p.name.startswith("."))[:15]
    except OSError:
        pass
    return {k: v for k, v in out.items() if v}

def settings_state():
    path = config_dir() / "settings.json"
    st = {"path": tilde(path), "exists": path.is_file(), "data": {}, "error": None}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                st["data"] = data
            else:
                st["error"] = "top level is not a JSON object"
        except json.JSONDecodeError as e:
            st["error"] = str(e)
    return st

def file_read(mid):
    if mid not in CONFIG_FILES:
        raise ValueError("not an editable config file")
    path = config_dir() / mid
    return {"id": mid, "path": tilde(path), "exists": path.is_file(),
            "content": path.read_text(errors="replace") if path.is_file() else ""}

def file_save(mid, content):
    if mid not in CONFIG_FILES:
        raise ValueError("not an editable config file")
    if not isinstance(content, str) or len(content) > 2 * 1024 * 1024:
        raise ValueError("bad content")
    if mid.endswith(".json") and content.strip():
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from None
    atomic_write(config_dir() / mid, content)

def settings_set(key, value):
    if not SETTINGS_KEY_RE.match(key or ""):
        raise ValueError("bad settings key")
    path = config_dir() / "settings.json"
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"{path.name} has invalid JSON — fix it by hand first ({e})")
        if not isinstance(data, dict):
            raise ValueError(f"{path.name}: top level is not a JSON object")
    parts = key.split(".")
    node = data
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            if value is None:
                return
            nxt = {}
            node[p] = nxt
        node = nxt
    if value is None:
        node.pop(parts[-1], None)

        def prune(d):
            for k in list(d):
                if isinstance(d[k], dict):
                    prune(d[k])
                    if not d[k]:
                        del d[k]
        prune(data)
    else:
        node[parts[-1]] = value
    atomic_write(path, json.dumps(data, indent=2) + "\n")

# The nine events the hooks builder used to know, kept as the head of the list
# so the common ones stay at the top of the event picker. The rest — 22 more —
# come from the official schema's hooks.* properties.
HOOK_EVENTS_COMMON = ["SessionStart", "UserPromptSubmit", "PreToolUse",
                      "PostToolUse", "Notification", "Stop", "SubagentStop",
                      "PreCompact", "SessionEnd"]

HOOK_EVENTS = HOOK_EVENTS_COMMON + [e for e in schema.hook_events()
                                    if e not in HOOK_EVENTS_COMMON]

def hook_sample(event):
    """Representative stdin payload for test-firing a hook command."""
    base = {"session_id": "claude-ui-test", "transcript_path": "/tmp/transcript.jsonl",
            "cwd": str(Path.home()), "hook_event_name": event}
    if event in ("PreToolUse", "PostToolUse"):
        base.update(tool_name="Bash", tool_input={"command": "echo hello"})
        if event == "PostToolUse":
            base["tool_response"] = {"stdout": "hello\n", "stderr": ""}
    elif event == "UserPromptSubmit":
        base["prompt"] = "test prompt from claude-ui"
    elif event == "Notification":
        base["message"] = "test notification"
    elif event == "SessionStart":
        base["source"] = "startup"
    return base

def hook_test(command, event):
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command required")
    if event not in HOOK_EVENTS:
        event = "PreToolUse"
    try:
        r = subprocess.run(command, shell=True, cwd=str(Path.home()),
                           input=json.dumps(hook_sample(event)),
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": None, "stdout": "", "stderr": "",
                "detail": "timed out after 10s"}
    return {"ok": r.returncode == 0, "exit": r.returncode,
            "stdout": r.stdout[-2000:], "stderr": r.stderr[-2000:],
            "detail": f"exit {r.returncode}"}
