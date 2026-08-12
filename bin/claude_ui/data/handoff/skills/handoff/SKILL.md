---
name: handoff
description: End this session deliberately and write a structured briefing that a fresh session reads to pick up exactly where this one left off. Use instead of /compact when the context window is nearly full, or when deliberately stopping work for the day.
disable-model-invocation: true
allowed-tools: Read, Write, Bash(date:*), Bash(uuidgen), Bash(tr:*), Bash(pwd), Bash(ls:*), Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(git branch:*), Bash(git rev-parse:*), Bash(git stash list:*)
---

# Handoff

Compaction summarizes a conversation. A handoff is a **briefing written for a stranger** — someone competent, working in this same directory tomorrow, who has no transcript, no memory of this session, and no way to ask a follow-up question. Everything they need to resume must be on the page, and everything they can re-read for themselves should be a pointer instead of a copy. Write the brief that would have saved *you* the last three hours.

## Hard rules

1. **Write the brief, not a summary.** Delete any sentence starting "I then", "we discussed", or "the user asked". The reader does not care what happened; they care what is true now.
2. **Pointers, not contents.** Never paste file bodies, diffs, or transcript quotes. A path with a line range beats a copy that is already stale.
3. **No invented facts.** Anything believed but not checked this session is marked `(unverified)`.
4. **Write for a stranger.** No "as discussed", no "the file we changed", no pronoun whose antecedent isn't in the same sentence.
5. **Run the commands, don't recall.** Disk state comes from `git`, never from memory of what was edited.
6. **An empty section says `None.`** One invented bullet makes the whole brief untrustworthy.
7. **Hard cap 300 lines.** Over it, cut Decisions to ≤10 and Files that matter to ≤12 first.
8. **Never write a secret.** Record the location and the kind of credential, never the value.
9. **`status:` stays `pending`.** The SessionStart hook flips it. Never edit a brief from a previous session.
10. **One brief, then stop.** Do no further work — no "one last fix" before writing, no follow-up after.

## Gather

Run these in one batch. A command that fails is itself a fact — record it, don't retry around it.

```bash
date +%Y-%m-%dT%H:%M:%S%z
uuidgen | tr 'A-Z' 'a-z'          # the session this brief is reserved for
pwd
git rev-parse --show-toplevel      # fails => not a git repo; omit `repo:`
git branch --show-current          # empty => detached HEAD
git status --porcelain=v1
git log --oneline -15
git log --oneline @{u}..HEAD       # fails => no upstream; say "no upstream"
git diff --stat HEAD
git stash list
ls -t __PROJECTS_TILDE__/"$(pwd | sed 's|[/.]|-|g')"/*.jsonl 2>/dev/null | head -1
```

The last line is a best-effort pointer for `transcript:`. If it prints nothing, write `transcript: unknown`.

The `uuidgen` line is the one output that is load-bearing rather than descriptive: it names the session that will receive this brief. Copy it exactly as printed, lowercased. Hard rule 5 applies with no exception here — a guessed UUID collides silently, and the brief is then reserved for a session nobody will ever start.

## The brief

Frontmatter first — flat, one line per key, no nesting and no lists. `target` is the match key the hook uses: the brief loads into the session started under that id, from any directory and after any delay, and into nothing else. `cwd` must still be the absolute path `pwd` printed, never `~` — it is advisory context for the reader and what `/handoffs` lists briefs by, but with `target:` present it no longer decides delivery. `repo` is advisory too (omit it outside a repo) and is deliberately **not** used for matching.

```yaml
---
title: <the one-line name of the work>
target: session:<uuid output>
cwd: <pwd output>
repo: <git toplevel, or omit>
branch: <branch, or "detached">
created: <date output>
transcript: <jsonl path, or "unknown">
status: pending
---
```

Then the body. The parenthetical under each heading is the instruction — don't reproduce it in the brief.

```markdown
# <title>

## Goal
<1–3 sentences: what we're trying to achieve, not what we did.>
**Done when:** <a checkable condition — a command that passes, a behaviour observable in the app. Never "it works".>

## Status
- [x] <finished AND verified — say how it was verified>
- [ ] <not started, or done but unverified>

## Changed on disk
- Branch: `<branch>` — <n> ahead of `<base>`, <pushed|unpushed>
- Commits this session: `<sha> <subject>` (one per line, or "none")
- Uncommitted: `<path>` — <what changed, one clause> (or "clean")
- Stashes: <or "none">

## Decisions
<Only decisions that had a live alternative and would cost real time to re-litigate.>
- **<decision>** — <why>. Rejected: <alternative>, because <reason>.

## Constraints & preferences
<Facts that bind the next session and are written down nowhere else it will look.>
- e.g. "python3 here is 3.9.6 — no 3.10+ syntax"

## Open questions
- <question> — blocks: <step number, or "nothing">. Answered by: <who or what>.

## Next steps
<Numbered, imperative. Step 1 must be executable immediately: exact file, exact command.
If step 1 needs a user decision, it's an Open question, not a step.>
1. …

## Files that matter
<Path + one line of why. No contents. ≤12, ordered by how soon they'll be opened.>
- `bin/claude_ui/insight.py` — transcript parser; token counts start at `_local_day`.

## Commands
<command>          # expects: <what a good result looks like>

## Dead ends
<Compaction always drops these, and they're the most expensive thing to rediscover.>
- <what was tried> — <what happened> — <what would have to change first>
```

## Why each section

| Section | What compaction leaves you | What the brief gives instead |
|---|---|---|
| Goal / Done when | An implied objective, drifting each round | A stated target and a checkable finish line |
| Status | Prose about progress | Verified vs unverified, split explicitly |
| Changed on disk | Recollection of edits, sometimes wrong | `git` output, true at write time |
| Decisions | The debate, or nothing | The verdict *and* the rejected branch, so it isn't re-argued |
| Constraints | Dropped — they were said once, early | The rules that still bind, restated |
| Open questions | Blurred into statements | Separated, with what they block |
| Next steps | "Continue where we left off" | An executable first action |
| Files that matter | Stale pasted contents | Live pointers the reader opens themselves |
| Dead ends | **Dropped first** — failures compact away soonest | The single most expensive thing to rediscover |

## Focus

The user may pass focus text: `/handoff focus on the failing tests`. It arrives as: $ARGUMENTS

If that reads literally as `$ARGUMENTS`, no substitution happened — read the invoking message instead, and if there's no focus there, proceed unweighted.

Focus **weights** the brief, it never truncates it. Every section is still filled, and every hard rule still applies. The named thing goes first in Next steps and gets the detail; everything else compresses to one line each.

## Write it

1. `mkdir -p __STORE_TILDE__`
2. Build the filename from the `date` output — never from a guessed clock: `YYYY-MM-DD-HHMM-<slug>.md`, where `<slug>` is the title lowercased with every non-alphanumeric run collapsed to `-`, trimmed to 40 characters.
3. If that path exists, append `-2` before `.md`.
4. Write that one file. Touch nothing else in `__STORE_TILDE__/` — the other files there are other sessions' records.

## Hand off

Print exactly this, then stop:

```
Handoff written: <title>
  __STORE__/2026-08-12-1432-token-ledger.md

Parked for a session you choose. Continue it any time, from anywhere:
  claude --session-id <uuid>

First step: <step 1, one line>
```

The command is the only way this brief gets delivered, so print the real uuid, not a placeholder. Lost it? `/handoffs` lists every parked brief with its command.
