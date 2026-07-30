"""settings.json schema + editing, and lifecycle hooks (incl. test-fire)."""

from pathlib import Path
import functools
import json
import re
import subprocess
import threading
import urllib.request

from .core import CONFIG_FILES, atomic_write, config_dir, tilde


# User-scope settings.json keys, from https://code.claude.com/docs/en/settings
# and .scratch/claude-code-config-research/ (docs snapshot 2026-07-22).
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
# The frontend also merges live suggestions (git identity, installed skills,
# MCP server names, ...) into datalists — see suggestFor() in static/app.js.
# In string/combo inputs a literal "" (two quote characters) writes the empty
# string; a blank input unsets the key.
MODEL_ALIASES = ["default", "best", "fable", "sonnet", "opus", "haiku",
                 "sonnet[1m]", "opus[1m]", "opusplan", "opusplan[1m]",
                 # full model IDs (aliases resolve to these; snapshot 2026-07)
                 "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
                 "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-5",
                 "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-4-5"]
LANGS = ["en", "ja", "fr", "es", "de", "zh", "ko", "pt", "it", "ru"]

# Documented env vars, suggested as keys in the `env` editor. Extracted from
# .scratch/claude-code-config-research/claude-code-configuration.md (docs
# snapshot 2026-07-22), with documented family patterns expanded. Excludes
# auto-set read-only signals (CLAUDECODE, CLAUDE_PID, CLAUDE_CODE_SESSION_ID,
# ...) and removed/no-op vars. Suggestions only — any key can still be typed.
ENV_VARS = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_AWS_WORKSPACE_ID",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_BEDROCK_SERVICE_TIER",
    "ANTHROPIC_BETAS", "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_DESCRIPTION",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_SUPPORTED_CAPABILITIES",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_WORKSPACE_ID",
    "API_FORCE_IDLE_TIMEOUT", "API_TIMEOUT_MS", "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_CONFIG_FILE", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_REGION",
    "AWS_SHARED_CREDENTIALS_FILE", "BASH_DEFAULT_TIMEOUT_MS",
    "BASH_MAX_OUTPUT_LENGTH", "BASH_MAX_TIMEOUT_MS", "CCR_FORCE_BUNDLE",
    "CLAUDE_AFK_COUNTDOWN_MS", "CLAUDE_AFK_TIMEOUT_MS",
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS",
    "CLAUDE_AGENT_SDK_MCP_NO_PREFIX", "CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_AUTO_BACKGROUND_TASKS",
    "CLAUDE_AX_SCREEN_READER", "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR",
    "CLAUDE_CLIENT_PRESENCE_FILE", "CLAUDE_CODE_ACCESSIBILITY",
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT", "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT",
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "CLAUDE_CODE_ARTIFACT_AUTO_OPEN",
    "CLAUDE_CODE_ATTRIBUTION_HEADER", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_AUTO_CONNECT_IDE", "CLAUDE_CODE_AWS_CHAIN_RESOLVE_TIMEOUT_MS",
    "CLAUDE_CODE_CERT_STORE", "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "CLAUDE_CODE_DEBUG_LOGS_DIR", "CLAUDE_CODE_DEBUG_LOG_LEVEL",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "CLAUDE_CODE_DISABLE_ADVISOR_TOOL", "CLAUDE_CODE_DISABLE_AGENT_VIEW",
    "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN", "CLAUDE_CODE_DISABLE_ARTIFACT",
    "CLAUDE_CODE_DISABLE_ATTACHMENTS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
    "CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD",
    "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF",
    "CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS", "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
    "CLAUDE_CODE_DISABLE_CRON", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS", "CLAUDE_CODE_DISABLE_FAST_MODE",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
    "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS",
    "CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP", "CLAUDE_CODE_DISABLE_MOUSE",
    "CLAUDE_CODE_DISABLE_MOUSE_CLICKS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK",
    "CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
    "CLAUDE_CODE_DISABLE_THINKING", "CLAUDE_CODE_DISABLE_VIRTUAL_SCROLL",
    "CLAUDE_CODE_DISABLE_WORKFLOWS", "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_ENABLE_APPEND_SUBAGENT_PROMPT",
    "CLAUDE_CODE_ENABLE_AWAY_SUMMARY",
    "CLAUDE_CODE_ENABLE_BACKGROUND_PLUGIN_REFRESH",
    "CLAUDE_CODE_ENABLE_FEEDBACK_SURVEY_FOR_OTEL",
    "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION", "CLAUDE_CODE_ENABLE_TASKS",
    "CLAUDE_CODE_ENABLE_TELEMETRY", "CLAUDE_CODE_EXIT_AFTER_STOP_DELAY",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "CLAUDE_CODE_EXTRA_BODY",
    "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE", "CLAUDE_CODE_FORCE_STRIKETHROUGH",
    "CLAUDE_CODE_FORCE_SYNC_OUTPUT", "CLAUDE_CODE_FORK_SUBAGENT",
    "CLAUDE_CODE_FORWARD_SUBAGENT_TEXT", "CLAUDE_CODE_GIT_BASH_PATH",
    "CLAUDE_CODE_GLOB_HIDDEN", "CLAUDE_CODE_GLOB_NO_IGNORE",
    "CLAUDE_CODE_GLOB_TIMEOUT_SECONDS", "CLAUDE_CODE_HIDE_CWD",
    "CLAUDE_CODE_IDE_HOST_OVERRIDE", "CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL",
    "CLAUDE_CODE_IDE_SKIP_VALID_CHECK", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_MAX_RETRIES",
    "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION",
    "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", "CLAUDE_CODE_MAX_TURNS",
    "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION",
    "CLAUDE_CODE_MCP_ALLOWLIST_ENV", "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS",
    "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "CLAUDE_CODE_NATIVE_CURSOR",
    "CLAUDE_CODE_NEW_INIT", "CLAUDE_CODE_NO_FLICKER",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH",
    "CLAUDE_CODE_OTEL_DIAG_STDERR", "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS",
    "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS",
    "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS",
    "CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE", "CLAUDE_CODE_PERFORCE_MODE",
    "CLAUDE_CODE_PLUGIN_CACHE_DIR", "CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS",
    "CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE",
    "CLAUDE_CODE_PLUGIN_PREFER_HTTPS", "CLAUDE_CODE_PLUGIN_SEED_DIR",
    "CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY",
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "CLAUDE_CODE_PROCESS_WRAPPER",
    "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_PROXY_RESOLVES_HOSTS",
    "CLAUDE_CODE_RESUME_INTERRUPTED_TURN", "CLAUDE_CODE_RESUME_PROMPT",
    "CLAUDE_CODE_RETRY_WATCHDOG", "CLAUDE_CODE_SAFE_MODE",
    "CLAUDE_CODE_SCRIPT_CAPS", "CLAUDE_CODE_SCROLL_SPEED",
    "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS", "CLAUDE_CODE_SHELL",
    "CLAUDE_CODE_SHELL_PREFIX", "CLAUDE_CODE_SIMPLE",
    "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT", "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
    "CLAUDE_CODE_SKIP_AWS_CRED_CACHE", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS",
    "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_SKIP_MANTLE_AUTH", "CLAUDE_CODE_SKIP_PROMPT_HISTORY",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP",
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
    "CLAUDE_CODE_SYNC_PLUGIN_INSTALL", "CLAUDE_CODE_SYNC_SKILLS",
    "CLAUDE_CODE_SYNTAX_HIGHLIGHT", "CLAUDE_CODE_TASK_LIST_ID",
    "CLAUDE_CODE_TEAM_TEARDOWN_PARK_TIMEOUT_MS", "CLAUDE_CODE_TMPDIR",
    "CLAUDE_CODE_TMUX_TRUECOLOR", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_NATIVE_FILE_SEARCH",
    "CLAUDE_CODE_USE_POWERSHELL_TOOL", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CONFIG_DIR", "CLAUDE_DISABLE_ADOPT", "CLAUDE_ENABLE_BYTE_WATCHDOG",
    "CLAUDE_ENABLE_BYTE_WATCHDOG_BEDROCK", "CLAUDE_ENABLE_STREAM_WATCHDOG",
    "CLAUDE_ENV_FILE", "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX",
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS", "CLOUD_ML_REGION", "DEBUG",
    "DISABLE_AUTOUPDATER", "DISABLE_AUTO_COMPACT", "DISABLE_BUG_COMMAND",
    "DISABLE_COMPACT", "DISABLE_COST_WARNINGS", "DISABLE_DOCTOR_COMMAND",
    "DISABLE_ERROR_REPORTING", "DISABLE_EXTRA_USAGE_COMMAND",
    "DISABLE_FEEDBACK_COMMAND", "DISABLE_GROWTHBOOK",
    "DISABLE_INSTALLATION_CHECKS", "DISABLE_INSTALL_GITHUB_APP_COMMAND",
    "DISABLE_INTERLEAVED_THINKING", "DISABLE_LOGIN_COMMAND",
    "DISABLE_LOGOUT_COMMAND", "DISABLE_PROMPT_CACHING",
    "DISABLE_PROMPT_CACHING_FABLE", "DISABLE_PROMPT_CACHING_HAIKU",
    "DISABLE_PROMPT_CACHING_OPUS", "DISABLE_PROMPT_CACHING_SONNET",
    "DISABLE_TELEMETRY", "DISABLE_UPDATES", "DISABLE_UPGRADE_COMMAND",
    "DO_NOT_TRACK", "ENABLE_CLAUDEAI_MCP_SERVERS", "ENABLE_PROMPT_CACHING_1H",
    "ENABLE_PROMPT_CACHING_1H_BEDROCK", "ENABLE_TOOL_SEARCH",
    "FALLBACK_FOR_ALL_PRIMARY_MODELS", "FORCE_AUTOUPDATE_PLUGINS",
    "FORCE_COLOR", "FORCE_HYPERLINK", "FORCE_PROMPT_CACHING_5M",
    "GCLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
    "HTTPS_PROXY", "HTTP_PROXY", "IS_DEMO", "MAX_MCP_OUTPUT_TOKENS",
    "MAX_STRUCTURED_OUTPUT_RETRIES", "MAX_THINKING_TOKENS",
    "MCP_CLIENT_SECRET", "MCP_CONNECTION_NONBLOCKING",
    "MCP_CONNECT_TIMEOUT_MS", "MCP_OAUTH_CALLBACK_PORT",
    "MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE",
    "MCP_SERVER_CONNECTION_BATCH_SIZE", "MCP_TIMEOUT", "MCP_TOOL_TIMEOUT",
    "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "NO_COLOR",
    "NO_PROXY", "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_LOGS_EXPORTER",
    "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_RAW_API_BODIES",
    "OTEL_LOG_TOOL_CONTENT", "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_USER_PROMPTS",
    "OTEL_METRICS_EXPORTER", "OTEL_METRICS_INCLUDE_ACCOUNT_UUID",
    "OTEL_METRICS_INCLUDE_ENTRYPOINT",
    "OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES",
    "OTEL_METRICS_INCLUDE_SESSION_ID", "OTEL_METRICS_INCLUDE_VERSION",
    "OTEL_METRIC_EXPORT_INTERVAL", "OTEL_RESOURCE_ATTRIBUTES",
    "SLASH_COMMAND_TOOL_CHAR_BUDGET", "TASK_MAX_OUTPUT_LENGTH",
    "USE_BUILTIN_RIPGREP", "VERTEX_REGION_CLAUDE_3_5_HAIKU",
    "VERTEX_REGION_CLAUDE_3_5_SONNET", "VERTEX_REGION_CLAUDE_3_7_SONNET",
    "VERTEX_REGION_CLAUDE_4_0_OPUS", "VERTEX_REGION_CLAUDE_4_0_SONNET",
    "VERTEX_REGION_CLAUDE_4_1_OPUS", "VERTEX_REGION_CLAUDE_4_5_OPUS",
    "VERTEX_REGION_CLAUDE_4_5_SONNET", "VERTEX_REGION_CLAUDE_4_6_OPUS",
    "VERTEX_REGION_CLAUDE_4_6_SONNET", "VERTEX_REGION_CLAUDE_4_7_OPUS",
    "VERTEX_REGION_CLAUDE_4_8_OPUS", "VERTEX_REGION_CLAUDE_5_SONNET",
    "VERTEX_REGION_CLAUDE_FABLE_5", "VERTEX_REGION_CLAUDE_HAIKU_4_5",
]

SETTINGS_SCHEMA = [
    {"key": "model", "type": "combo", "values": MODEL_ALIASES, "cat": "model",
     "desc": "Model for the main session — alias (opus, sonnet, haiku…) or full model ID; read at startup"},
    {"key": "fallbackModel", "type": "list", "item_values": MODEL_ALIASES, "cat": "model",
     "desc": "Fallback model chain tried in order on overload, max 3 models"},
    {"key": "effortLevel", "type": "enum", "values": ["low", "medium", "high", "xhigh"], "cat": "model",
     "desc": "Persist reasoning effort level across sessions"},
    {"key": "alwaysThinkingEnabled", "type": "bool", "cat": "model",
     "desc": "Enable extended thinking by default"},
    {"key": "thinkingBudgetTokens", "type": "number", "cat": "model",
     "values": [1024, 4096, 8192, 16000, 31999],
     "desc": "Token budget for extended thinking when always-thinking is on"},
    {"key": "advisorModel", "type": "combo", "values": ["opus", "sonnet", "haiku"], "cat": "model",
     "desc": "Model for the server-side advisor tool; unset to disable"},
    {"key": "fastMode", "type": "bool", "cat": "model",
     "desc": "Enable fast mode for sessions where available"},
    {"key": "fastModePerSessionOptIn", "type": "bool", "cat": "model",
     "desc": "Require per-session opt-in for fast mode"},

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

    {"key": "env", "type": "kv", "key_values": ENV_VARS, "cat": "environment & hooks",
     "desc": "Environment variables applied to every session and subprocess"},
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

    {"key": "editorMode", "type": "enum", "values": ["normal", "vim"], "default": "normal", "cat": "interface",
     "desc": "Key binding mode for the input prompt"},
    {"key": "tui", "type": "enum", "values": ["default", "fullscreen"], "cat": "interface",
     "desc": "TUI renderer mode"},
    {"key": "theme", "type": "combo", "cat": "interface",
     "values": ["auto", "dark", "light", "dark-daltonized", "light-daltonized", "dark-ansi", "light-ansi"],
     "desc": "Color theme (or custom:<slug> for a themes/ file)"},
    {"key": "interfaceLanguage", "type": "combo", "values": LANGS, "cat": "interface",
     "desc": "Interface language, e.g. en, ja, fr"},
    {"key": "language", "type": "combo", "values": LANGS, "cat": "interface",
     "desc": "Preferred language for Claude's responses"},
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

    {"key": "attribution.commit", "type": "combo", "cat": "git",
     "values": ['""', "Generated with [Claude Code](https://claude.com/claude-code)"],
     "desc": "Custom commit attribution string (type \"\" to set empty, which hides it)"},
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
     "desc": "Days before session files auto-delete (min 1)"},

    {"key": "enableAllProjectMcpServers", "type": "bool", "cat": "mcp & plugins",
     "desc": "Auto-approve every MCP server in project .mcp.json"},
    {"key": "enabledMcpjsonServers", "type": "list", "cat": "mcp & plugins",
     "desc": "Specific .mcp.json servers to approve"},
    {"key": "disabledMcpjsonServers", "type": "list", "cat": "mcp & plugins",
     "desc": "Specific .mcp.json servers to reject"},
    {"key": "mcpServerTimeouts", "type": "kv", "value_type": "number", "cat": "mcp & plugins",
     "desc": "Per-server startup timeout in seconds, e.g. github → 30"},
    {"key": "pluginMarketplaces", "type": "list", "cat": "mcp & plugins",
     "item_values": ["anthropics/claude-code",
                     "https://github.com/OWNER/REPO",
                     "~/src/my-marketplace"],
     "desc": "Custom plugin marketplace sources"},
    {"key": "skillOverrides", "type": "kv", "cat": "mcp & plugins",
     "values": ["on", "name-only", "user-invocable-only", "off"],
     "desc": "Per-skill visibility override (skill name → visibility)"},
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
    {"key": "verboseLogging", "type": "bool", "default": False, "cat": "system",
     "desc": "Verbose logging output"},
    {"key": "fileCheckpointingEnabled", "type": "bool", "default": True, "cat": "system",
     "desc": "Snapshot files before edits for /rewind"},
    {"key": "workspaceInitScript", "type": "combo",
     "values": ["~/.claude/workspace-init.sh"], "cat": "system",
     "desc": "Script run when opening a new workspace"},
    {"key": "skipFirstRunQuestions", "type": "bool", "cat": "system",
     "desc": "Skip first-run setup questions"},
    {"key": "ignoreGitignore", "type": "bool", "cat": "system",
     "desc": "Ignore .gitignore when searching files"},
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
    {"key": "disableRemoteControl", "type": "bool", "cat": "system",
     "desc": "Disable Remote Control"},
    {"key": "disableWorkflows", "type": "bool", "default": False, "cat": "system",
     "desc": "Disable dynamic workflows and bundled workflow commands"},
    {"key": "agentPushNotifEnabled", "type": "bool", "default": False, "cat": "system",
     "desc": "Proactive push notifications when Remote Control is connected"},
    {"key": "worktree.baseRef", "type": "enum", "values": ["fresh", "head"], "cat": "system",
     "desc": "Base ref for new worktrees: clean tree or current HEAD"},
    {"key": "worktree.bgIsolation", "type": "enum", "values": ["worktree", "none"], "cat": "system",
     "desc": "Isolate background agents in their own worktree"},
]

# dedupe (keep first occurrence)
_seen = set()

SETTINGS_SCHEMA = [s for s in SETTINGS_SCHEMA
                   if not (s["key"] in _seen or _seen.add(s["key"]))]

SETTINGS_KEY_RE = re.compile(r"^[A-Za-z0-9_$][A-Za-z0-9_.$-]*$")

# Live option values fetched once in the background at server start from the
# public docs (no auth needed); on any failure the statics above remain the
# fallback. Two sources:
#   - models overview page: "Claude API ID/alias" table rows → model IDs for
#     the model/fallbackModel suggestions
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
            found["model"] = found["fallbackModel"] = list(dict.fromkeys(ids))
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

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "Notification", "Stop", "SubagentStop", "PreCompact", "SessionEnd"]

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
