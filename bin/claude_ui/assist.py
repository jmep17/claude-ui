"""Claude-assisted authoring via the local `claude -p` CLI."""

from pathlib import Path
import re
import shutil
import subprocess


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
        r = subprocess.run([exe, "-p", prompt], capture_output=True, text=True,
                           timeout=240, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        raise ValueError("claude -p timed out after 240s") from None
    if r.returncode != 0:
        raise ValueError("claude -p failed: " + (r.stderr.strip() or f"exit {r.returncode}")[:500])
    text = r.stdout.strip()
    if wants_file and text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return {"result": text, "replaces": wants_file}
