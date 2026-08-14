"""PROTOTYPE — throwaway. Backend for the transcript-viewer UI prototype.

Three variants of a session transcript viewer live on the throwaway tab
`#prototype-transcript`, switchable via `?variant=A|B|C`. This module only
feeds them: it lists the sessions under <config>/projects/ and parses one
`.jsonl` transcript into display-shaped entries. Read-only, no cache, no
tests, no error handling beyond keeping the server up. Delete this file and
static/prototype-transcript.js to remove the prototype.

There is no production build to gate the prototype on — this dashboard only
ever runs locally — so the `prototype` in the tab name and these paths is the
gate.
"""

from pathlib import Path
import json

from .core import tilde
from .insight import MAX_TRANSCRIPT, projects_dir, transcript_stats

# Per-block and per-entry caps. A tool_result holding a whole file read is
# routinely megabytes; nothing readable needs more than this.
MAX_BLOCK = 4000
MAX_RAW = 40000
WINDOW = 300          # entries returned per request unless asked otherwise
TAIL_BYTES = 256 * 1024   # how much of a file's end is read to find its title

_TITLES = {}          # path -> (sig, title, last_prompt), process lifetime only


# ------------------------------------------------------------ session list

def _tail_meta(path, st):
    """(title, last prompt) for a session, from the tail of its transcript.

    Claude Code appends `ai-title` and `last-prompt` records as the session
    goes, so the newest of each sits near the end of the file. Reading the
    last chunk keeps listing ~40 sessions cheap; scanning them whole would
    not be.
    """
    sig = (int(st.st_mtime), st.st_size)
    hit = _TITLES.get(str(path))
    if hit and hit[0] == sig:
        return hit[1], hit[2]
    title = prompt = ""
    try:
        with open(path, "rb") as f:
            if st.st_size > TAIL_BYTES:
                f.seek(st.st_size - TAIL_BYTES)
                f.readline()   # discard the half line the seek landed inside
            for raw in f:
                if b"aiTitle" not in raw and b"lastPrompt" not in raw:
                    continue
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                title = d.get("aiTitle") or title
                prompt = d.get("lastPrompt") or prompt
    except OSError:
        pass
    title, prompt = str(title)[:200], str(prompt)[:200]
    _TITLES[str(path)] = (sig, title, prompt)
    return title, prompt


def sessions_state():
    """Every transcript on this machine, grouped by the project it ran in."""
    st = transcript_stats()
    pdir = projects_dir()
    by_cwd = {}
    for r in st.get("session_rows") or []:
        p = Path(r["path"])
        try:
            fs = p.stat()
        except OSError:
            continue
        sess = r.get("sess") or {}
        title, prompt = _tail_meta(p, fs)
        try:
            slug = p.relative_to(pdir).parts[0]
        except ValueError:
            slug = ""
        row = {
            "id": p.stem, "short": p.stem[:8], "path": str(p), "slug": slug,
            "subagent": "subagents" in p.parts,
            "msgs": r.get("msgs") or 0, "bytes": fs.st_size,
            "mtime": int(fs.st_mtime),
            "first_ts": sess.get("first_ts") or "", "last_ts": sess.get("last_ts") or "",
            "model": sess.get("model") or "", "title": title, "prompt": prompt,
        }
        by_cwd.setdefault(r["cwd"] or "(unknown)", []).append(row)
    projects = []
    for cwd, rows in by_cwd.items():
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        projects.append({
            "cwd": cwd, "tilde": tilde(cwd), "sessions": rows,
            "count": len(rows), "bytes": sum(r["bytes"] for r in rows),
            "mtime": max(r["mtime"] for r in rows),
        })
    projects.sort(key=lambda p: -p["mtime"])
    return {"available": st["available"], "dir": st["dir"],
            "projects": projects, "total": sum(p["count"] for p in projects)}


# ---------------------------------------------------------------- one file

def _resolve(raw):
    """Client-supplied path -> a transcript under <config>/projects/.

    The listing hands out absolute paths and they come straight back; this is
    the only thing standing between the query string and open().
    """
    if not raw:
        raise ValueError("path is required")
    pdir = projects_dir().resolve()
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(str(e))
    if pdir not in p.parents:
        raise ValueError("not a transcript under " + tilde(pdir))
    if p.suffix != ".jsonl" or not p.is_file():
        raise ValueError("not a .jsonl transcript")
    if p.stat().st_size > MAX_TRANSCRIPT:
        raise ValueError("transcript is larger than the scan limit")
    return p


def _clip(s, n=MAX_BLOCK):
    s = s if isinstance(s, str) else json.dumps(s, default=str)
    return (s[:n], len(s)) if len(s) > n else (s, 0)


def _text_of(content):
    """Whatever text a tool_result's content carries, list-shaped or not."""
    if isinstance(content, str):
        return content
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict):
            out.append(b.get("text") or ("[" + str(b.get("type") or "block") + "]"))
        else:
            out.append(str(b))
    return "\n".join(out)


def _blocks(msg):
    """message.content -> the block list the viewer renders."""
    content = msg.get("content")
    if isinstance(content, str):
        text, over = _clip(content)
        return [{"kind": "text", "text": text, "over": over}]
    out = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text, over = _clip(b.get("text") or "")
            out.append({"kind": "text", "text": text, "over": over})
        elif t == "thinking":
            text, over = _clip(b.get("thinking") or "")
            # An empty thinking block is a redacted one — say so rather than
            # rendering a blank card.
            out.append({"kind": "thinking", "text": text, "over": over,
                        "redacted": not text})
        elif t == "tool_use":
            inp, over = _clip(json.dumps(b.get("input") or {}, indent=1,
                                         default=str))
            out.append({"kind": "tool_use", "name": b.get("name") or "?",
                        "id": b.get("id") or "", "input": inp, "over": over,
                        "brief": _brief(b.get("name"), b.get("input"))})
        elif t == "tool_result":
            text, over = _clip(_text_of(b.get("content")))
            out.append({"kind": "tool_result", "id": b.get("tool_use_id") or "",
                        "text": text, "over": over,
                        "error": bool(b.get("is_error"))})
        elif t == "image":
            out.append({"kind": "image"})
        else:
            text, over = _clip(json.dumps(b, default=str))
            out.append({"kind": t or "block", "text": text, "over": over})
    return out


# The one line that says what a tool call did, per tool. Everything else is
# behind the expander.
_BRIEF_KEYS = ("command", "file_path", "pattern", "path", "url", "prompt",
               "skill", "query", "description", "notebook_path")

def _brief(name, inp):
    if not isinstance(inp, dict):
        return ""
    for k in _BRIEF_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:160]
    return ""


def _kind(entry, blocks):
    """The one word a row is filtered and coloured by."""
    t = entry.get("type")
    kinds = {b["kind"] for b in blocks}
    if t == "user":
        if "tool_result" in kinds:
            return "result"
        return "user"
    if t == "assistant":
        if "tool_use" in kinds:
            return "tool"
        if "text" in kinds:
            return "assistant"
        if "thinking" in kinds:
            return "thinking"
        return "assistant"
    if t in ("summary", "system"):
        return t
    return "meta"


def _lines(path):
    """(0-based line number, parsed object) for every line that parses.

    The number is the line's real position, so `entry_raw` can find the same
    line again by counting, and a malformed line in the middle does not shift
    every index after it.
    """
    with open(path, errors="replace") as f:
        for i, line in enumerate(f):
            try:
                yield i, json.loads(line)
            except json.JSONDecodeError:
                continue


def session_state(raw_path, start=None, count=WINDOW):
    """One transcript: a light index of every line, plus a window of entries.

    The index is small enough to ship whole (a 12 MB session is ~1,600 lines),
    which is what lets a variant draw the shape of the session without pulling
    every tool result across the wire. `start` defaults to the tail, which is
    where a session's interesting part usually is.
    """
    p = _resolve(raw_path)
    index, entries = [], []
    head = {}
    tools, models = {}, {}
    totals = {"in": 0, "out": 0, "cr": 0, "cw": 0}
    for i, d in _lines(p):
        for k in ("cwd", "version", "gitBranch", "sessionId", "slug"):
            if not head.get(k) and isinstance(d.get(k), str):
                head[k] = d[k]
        msg = d.get("message") or {}
        blocks = _blocks(msg) if msg else []
        kind = _kind(d, blocks)
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        model = msg.get("model") or ""
        if model:
            models[model] = models.get(model, 0) + 1
        if usage:
            totals["in"] += int(usage.get("input_tokens") or 0)
            totals["out"] += int(usage.get("output_tokens") or 0)
            totals["cr"] += int(usage.get("cache_read_input_tokens") or 0)
            totals["cw"] += int(usage.get("cache_creation_input_tokens") or 0)
        names = [b["name"] for b in blocks if b["kind"] == "tool_use"]
        for n in names:
            tools[n] = tools.get(n, 0) + 1
        chars = sum(len(b.get("text") or b.get("input") or "") + (b.get("over") or 0)
                    for b in blocks)
        row = {
            "i": i, "kind": kind, "type": d.get("type") or "",
            "ts": d.get("timestamp") or "", "side": bool(d.get("isSidechain")),
            "agent": d.get("agentName") or "", "chars": chars,
            "tools": names, "model": model,
            "in": int(usage.get("input_tokens") or 0) if usage else 0,
            "out": int(usage.get("output_tokens") or 0) if usage else 0,
            "cr": int(usage.get("cache_read_input_tokens") or 0) if usage else 0,
        }
        index.append(row)
        entries.append({**row, "blocks": blocks,
                        "uuid": d.get("uuid") or "",
                        "parent": d.get("parentUuid") or "",
                        "meta": bool(d.get("isMeta"))})
    total = len(index)
    count = max(1, min(int(count or WINDOW), 2000))
    start = total - count if start is None else int(start)
    start = max(0, min(start, max(0, total - 1)))
    title, prompt = _tail_meta(p, p.stat())
    return {"path": str(p), "tilde": tilde(p), "id": p.stem,
            "cwd": head.get("cwd") or "", "cwd_tilde": tilde(head.get("cwd") or ""),
            "version": head.get("version") or "", "branch": head.get("gitBranch") or "",
            "title": title, "prompt": prompt,
            "bytes": p.stat().st_size, "total": total,
            "start": start, "count": count,
            "index": index, "entries": entries[start:start + count],
            "tools": [{"name": k, "n": v}
                      for k, v in sorted(tools.items(), key=lambda kv: -kv[1])],
            "models": [{"model": k, "n": v}
                       for k, v in sorted(models.items(), key=lambda kv: -kv[1])],
            "totals": totals,
            "first_ts": next((r["ts"] for r in index if r["ts"]), ""),
            "last_ts": next((r["ts"] for r in reversed(index) if r["ts"]), "")}


def entry_raw(raw_path, i):
    """The raw JSON line behind one entry, for the inspector variant."""
    p = _resolve(raw_path)
    i = int(i)
    with open(p, errors="replace") as f:
        for n, line in enumerate(f):
            if n == i:
                text, over = _clip(line.strip(), MAX_RAW)
                try:
                    pretty = json.dumps(json.loads(text), indent=2)[:MAX_RAW]
                except json.JSONDecodeError:
                    pretty = text
                return {"i": i, "raw": pretty, "over": over}
    raise ValueError("no line " + str(i))
