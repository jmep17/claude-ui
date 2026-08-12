---
name: handoff
description: End this session deliberately and write a structured briefing that a fresh session reads to pick up exactly where this one left off. Use instead of /compact when the context window is nearly full, or when deliberately stopping work for the day.
disable-model-invocation: true
allowed-tools: Read, Write, Bash(python3 __HOOK__:*), Bash(git diff:*), Bash(git log:*)
---

# Handoff

Compaction summarizes a conversation. A handoff is a **briefing written for a stranger** — someone competent, working in this same directory tomorrow, who has no transcript, no memory of this session, and no way to ask a follow-up question. Everything they need to resume must be on the page, and everything they can re-read for themselves should be a pointer instead of a copy. Write the brief that would have saved *you* the last three hours.

## Hard rules

1. **Write the brief, not a summary.** Delete any sentence starting "I then", "we discussed", or "the user asked". The reader does not care what happened; they care what is true now.
2. **Pointers, not contents.** Never paste file bodies, diffs, or transcript quotes. A path with a line range beats a copy that is already stale.
3. **No invented facts.** Anything believed but not checked this session is marked `(unverified)`.
4. **Write for a stranger.** No "as discussed", no "the file we changed", no pronoun whose antecedent isn't in the same sentence.
5. **Run the commands, don't recall.** Disk state comes from `--facts`, never from memory of what was edited.
6. **An empty section says `None.`** One invented bullet makes the whole brief untrustworthy.
7. **Hard cap 300 lines.** Over it, cut Decisions to ≤10 and Files that matter to ≤12 first.
8. **Never write a secret.** Record the location and the kind of credential, never the value.
9. **One brief, then stop.** Do no further work — no "one last fix" before writing, no follow-up after.

## The brief

Write the body only — frontmatter, the filename, and delivery are the script's job.

```markdown
# <title>

## Goal
<1–3 sentences: what we're trying to achieve, not what we did.>
**Done when:** <a checkable condition — a command that passes, a behaviour observable in the app. Never "it works".>

## Status
- [x] <finished AND verified — say how it was verified>
- [ ] <not started, or done but unverified>
## Changed on disk
<Leave this heading with nothing under it — the script fills it in from `--facts` at write time.>
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

The user may pass focus text: `/handoff focus on the failing tests`. It arrives as: $ARGUMENTS. If that reads literally as `$ARGUMENTS`, no substitution happened — read the invoking message instead, and if there's no focus there, proceed unweighted. Focus **weights** the brief, it never truncates it: every section is still filled and every hard rule still applies, the named thing goes first in Next steps and gets the detail, everything else compresses to one line each.

## How to write it

1. `python3 __HOOK__ --facts` — this is all the disk state you get; do not run your own `git log`/`git diff` beyond what it prints.
2. `Write` the body above to a scratch path, then `python3 __HOOK__ --new --title "<title>" --body-file <path>`. It writes the brief, grouped by repository, and prints the hand-off block — title, path, `claude --session-id <uuid>`, and the first step. Print that output verbatim, then stop.
**Fallback**, only if `--new` errors `unknown option --new` (an old installed hook): `Write` the frontmatter+body brief yourself to `__STORE_TILDE__/<slug>.md` with `status: pending` and `cwd: <pwd>` — omit `target:`, so it loads at the next start here instead of by reservation.
