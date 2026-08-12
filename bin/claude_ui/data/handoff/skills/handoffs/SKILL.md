---
name: handoffs
description: List every parked handoff brief — title, age, directory, and the exact command that continues it — and resume one right here. Use when you've lost the session id a /handoff printed, or want to pick up work that is still waiting.
disable-model-invocation: true
allowed-tools: Bash(python3 __HOOK__:*)
---

# Handoffs

`/handoff` reserves a session id and prints one command. This is what you run
when that command has scrolled away — and it can load the brief here instead.

The script is authoritative. It sees exactly what the SessionStart hook sees, so
its answer and the hook's can never disagree. Your job is to run it and pass its
output through, not to re-derive anything.

## No argument — list

```bash
python3 __HOOK__ --list
```

Print its stdout **verbatim** and stop. Do not reformat it, do not re-sort it,
do not recompute an age, do not add a summary or a recommendation about which to
pick up. If it printed a warning on stderr, print that too.

## With an argument — resume it here

The argument arrives as: $ARGUMENTS

If that reads literally as `$ARGUMENTS`, no substitution happened — treat it as
no argument and list.

1. Run `--list` first, always.
2. Resolve the argument to the **name** printed as the third field on each
   entry's second line (a store-relative path, e.g. `claude-ui/2026-08-12-...md`):
   - a number picks that numbered entry;
   - text matches case-insensitively against the titles.
3. Run `python3 __HOOK__ --take <basename>`.

Resolving to a basename rather than passing the number straight through is
deliberate: if the store changed between the two calls, an index could silently
hand over a different brief. A basename cannot.

Ambiguous text → print the list, name the ambiguity, and stop. **Never guess** —
taking the wrong brief consumes it.

On success, `--take` prints the brief to stdout. Read it and continue the work
from its "Next steps". You are now that session.

## Failure

**A non-zero exit is never "nothing parked".** Print the script's stderr
verbatim and stop. Translating a failure into `No handoff briefs parked.` is the
exact bug this skill was rewritten to kill: the store being unreadable and the
store being empty are different answers and must read differently.

## Writing

Taking a brief **does** write `status:`, `consumed:` and `consumed_by:` — but
only ever through `--take`. Never edit a brief by hand: doing so either destroys
one or resurrects one that was already delivered.
