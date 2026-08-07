"""Claude-assisted authoring via the local `claude -p` CLI."""

from pathlib import Path
import os
import re
import shutil
import subprocess


# Replaces the default Claude Code system prompt on CLIs that support it: the
# assist task is pure text-in/text-out, so the preset's tool instructions,
# MCP schemas and skills listing are dead weight billed to the user.
ASSIST_SYSTEM_PROMPT = (
    "You are helping the user edit a Claude Code configuration file "
    "(CLAUDE.md, skills, agents, commands, settings). Follow the "
    "instruction you are given and return exactly what it asks for, "
    "with no preamble and no tool use.")

ASSIST_PRESETS = {
    "improve": (
        "Improve this Claude Code config file. Tighten the description so its "
        "triggers are unambiguous (a skill description should say what it does, "
        "then 'Use when ...' trigger conditions), fix frontmatter issues, and "
        "improve clarity without changing the intent.", True),
    "review": (
        "Review this Claude Code config file. List concrete problems only: "
        "vague or missing 'Use when' triggers, contradictions, verbosity that "
        "wastes context, frontmatter mistakes. Be specific and brief.", False),
}

_FLAG_CACHE = {}   # (exe path, mtime) -> frozenset of --long-options in --help

def _cli_flags(exe):
    """Long options the installed claude CLI advertises in --help.

    Cached per (path, mtime) so an upgrade or downgrade mid-session re-probes;
    any failure (missing exe, hung or erroring --help) caches an empty set,
    which downgrades assist to the legacy argv rather than breaking it."""
    try:
        mtime = os.stat(exe).st_mtime
    except OSError:
        mtime = 0
    key = (exe, mtime)
    if key not in _FLAG_CACHE:
        try:
            r = subprocess.run([exe, "--help"], capture_output=True, text=True,
                               timeout=20)
            text = r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            text = ""
        flags = set(re.findall(r"--[A-Za-z][A-Za-z0-9-]*", text))
        # --help abbreviates optional suffixes as --system-prompt[-file];
        # expand those so the -file variants are discoverable too
        for base, suf in re.findall(
                r"(--[A-Za-z][A-Za-z0-9-]*)\[(-[A-Za-z0-9-]+)\]", text):
            flags.add(base + suf)
        _FLAG_CACHE[key] = frozenset(flags)
    return _FLAG_CACHE[key]

def _assist_argv(exe, prompt):
    """[exe, -p, prompt] plus whatever lean flags this CLI supports.

    The prompt is a positional and must stay ahead of --tools, which is
    variadic (commander): any bare token after it would be swallowed as a
    tool name and claude would block on stdin with no prompt at all, so
    --tools "" stays last. --bare is deliberately not used: it drops
    OAuth/keychain auth, breaking subscription users."""
    flags = _cli_flags(exe)
    argv = [exe, "-p", prompt]
    if "--system-prompt" in flags:
        argv += ["--system-prompt", ASSIST_SYSTEM_PROMPT]
    if "--strict-mcp-config" in flags:
        argv += ["--strict-mcp-config"]
    if "--tools" in flags:
        argv += ["--tools", ""]
    return argv

def assist(mode, custom, content, path):
    exe = shutil.which("claude")
    if not exe:
        raise ValueError("claude CLI not found on PATH — assist needs Claude Code installed")
    if not isinstance(content, str) or not content.strip() or len(content) > 200_000:
        raise ValueError("nothing to work on (or file too large)")
    if mode == "custom":
        if not (custom or "").strip():
            raise ValueError("custom instruction required")
        instruction, wants_file = custom.strip(), True
    elif mode in ASSIST_PRESETS:
        instruction, wants_file = ASSIST_PRESETS[mode]
    else:
        raise ValueError("unknown assist mode")
    prompt = (f"{instruction}\n\nThe file is {path}:\n"
              f"<file>\n{content}\n</file>\n")
    if wants_file:
        prompt += ("\nReturn ONLY the complete revised file content. "
                   "No preamble, no explanation, no code fences.")
    try:
        r = subprocess.run(_assist_argv(exe, prompt), capture_output=True,
                           text=True, timeout=240, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        raise ValueError("claude -p timed out after 240s") from None
    if r.returncode != 0:
        raise ValueError("claude -p failed: " + (r.stderr.strip() or f"exit {r.returncode}")[:500])
    text = r.stdout.strip()
    if wants_file and text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return {"result": text, "replaces": wants_file}
