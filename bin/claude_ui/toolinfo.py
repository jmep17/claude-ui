"""The built-in tool catalog: every tool Claude Code loads into a session.

One place, data only, imported by settings.py (the permission-rule pickers)
and tooluse.py (the Insight tab's off switch) — two vocabularies that must
not drift.

Source of truth: the official tools reference at
https://code.claude.com/docs/en/tools-reference (checked 2026-08-21), which
documents each name as "the exact strings you use in permission rules". The
UNVERIFIED entries are tools current sessions demonstrably expose that the
reference does not list yet — the settings tab's rule for its unverified
badge applies here too: absence is not disproof, and the wording is "not
listed", never "not real".

Why a deny is a context saving, per the permissions doc: "A bare tool name
like `Bash` removes the tool from Claude's context entirely, so Claude never
sees it." The one documented exception is EndConversation — a deny rule
can't remove it while any other tool remains — so it is the one tool the
off switch refuses.

`core` marks the six tools the whole coding workflow stands on; the UI still
offers their switch, behind a confirm.
"""

_CORE = frozenset({"Bash", "Read", "Edit", "Write", "Glob", "Grep"})

# (name, blurb), straight from the tools reference's table, compressed.
_DOCUMENTED = [
    ("Agent", "spawns subagents — your agents stop working without it"),
    ("Artifact", "publishes a page to claude.ai as an artifact"),
    ("AskUserQuestion", "asks multiple-choice questions mid-task"),
    ("Bash", "runs shell commands"),
    ("CronCreate", "schedules a recurring or one-shot prompt in this session"),
    ("CronDelete", "cancels a scheduled task"),
    ("CronList", "lists scheduled tasks"),
    ("Edit", "edits files in place"),
    ("EndConversation", "ends the session on sustained abuse — the one tool "
                        "a deny rule can't remove"),
    ("EnterPlanMode", "switches into plan mode"),
    ("EnterWorktree", "creates and enters an isolated git worktree"),
    ("ExitPlanMode", "presents the plan for approval — plan mode needs it"),
    ("ExitWorktree", "leaves a worktree and returns"),
    ("Glob", "finds files by name pattern"),
    ("Grep", "searches file contents"),
    ("ListAgents", "lists agents reachable with SendMessage"),
    ("ListMcpResourcesTool", "lists resources from connected MCP servers"),
    ("LSP", "code intelligence via language servers — definitions, "
            "references, diagnostics"),
    ("Monitor", "watches a background command or WebSocket feed and reacts "
                "to events"),
    ("NotebookEdit", "edits Jupyter notebook cells"),
    ("PowerShell", "runs PowerShell natively"),
    ("PushNotification", "sends a desktop or phone notification"),
    ("Read", "reads files"),
    ("ReadMcpResourceTool", "reads one MCP resource by URI"),
    ("RemoteTrigger", "manages claude.ai Routines — backs /schedule"),
    ("ReportFindings", "reports code-review findings as a structured list"),
    ("ScheduleWakeup", "paces a self-timed /loop"),
    ("SendMessage", "messages another agent or session"),
    ("SendUserFile", "sends a file from the session to your device"),
    ("ShareOnboardingGuide", "shares ONBOARDING.md — backs /team-onboarding"),
    ("Skill", "invokes skills — skills stop loading without it"),
    ("TaskCreate", "adds to the task list (left out on the newest models "
                   "unless opted in)"),
    ("TaskGet", "reads one task's details (left out on the newest models "
                "unless opted in)"),
    ("TaskList", "lists the task list (left out on the newest models "
                 "unless opted in)"),
    ("TaskOutput", "reads background task output (deprecated in favor of "
                   "Read)"),
    ("TaskStop", "stops a background task or agent"),
    ("TaskUpdate", "updates the task list (left out on the newest models "
                   "unless opted in)"),
    ("TodoWrite", "the older session checklist (left out on the newest "
                  "models unless opted in)"),
    ("ToolSearch", "loads deferred tools on demand — deferred MCP tools "
                   "need it"),
    ("WaitForMcpServers", "waits for MCP servers still connecting"),
    ("WebFetch", "fetches and reads a URL"),
    ("WebSearch", "searches the web (billed per search)"),
    ("Workflow", "runs multi-agent workflow scripts"),
    ("Write", "creates and overwrites files"),
]

# Present in current sessions, not (yet) in the tools reference.
_UNVERIFIED = [
    ("DesignSync", "backs Claude Design canvases"),
    ("ListConnectors", "lists your claude.ai connectors"),
    ("ListPlugins", "lists installed plugins"),
    ("ListSkills", "lists available skills"),
    ("ReadMcpResourceDirTool", "reads an MCP resource directory"),
    ("ReadNotifications", "reads queued session notifications"),
    ("SearchMcpRegistry", "searches the MCP server registry"),
    ("SearchPlugins", "searches the plugin marketplaces"),
    ("SearchSkills", "searches for skills to add"),
    ("ShowOnboardingRolePicker", "Cowork onboarding role picker"),
    ("SuggestConnectors", "suggests connectors for the task at hand"),
    ("SuggestPluginInstall", "suggests plugins to install"),
    ("SuggestSkills", "suggests skills to add"),
]

TOOLS = sorted(
    [{"name": n, "core": n in _CORE, "blurb": b, "unverified": False}
     for n, b in _DOCUMENTED]
    + [{"name": n, "core": False, "blurb": b, "unverified": True}
       for n, b in _UNVERIFIED],
    key=lambda t: t["name"])

# The documented exception to bare-name removal (see the module docstring):
# suggesting or writing a deny for it would promise something Claude Code
# says it will not do.
NO_DENY = frozenset({"EndConversation"})

# Superseded or removed names. They still appear in transcripts, so the
# advisor labels an observed one instead of calling it "seen in your
# transcripts" — but none of them loads into a current session, so there is
# nothing to turn off and no rule worth suggesting.
LEGACY = {
    "Task": "older name for the Agent tool",
    "SlashCommand": "ran /commands mid-task; not a current tool",
    "BashOutput": "read background shell output; superseded by TaskOutput/Read",
    "KillShell": "killed a background shell; superseded by TaskStop",
    "KillBash": "killed a background shell; superseded by TaskStop",
    "MultiEdit": "batched several edits; folded into Edit",
    "NotebookRead": "read notebook cells; folded into Read",
    "LS": "listed a directory; superseded by Glob and Bash",
    "TodoRead": "read the checklist; folded into TodoWrite",
}

# Every current tool, for the advisor's roster…
TOOL_NAMES = [t["name"] for t in TOOLS]

# …and the ones worth offering in a permission rule (all but the deny-proof).
RULE_NAMES = [n for n in TOOL_NAMES if n not in NO_DENY]
