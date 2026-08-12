#!/bin/bash
# Offline cases for handoff_load.py. Never touches ~/.claude — CLAUDE_CONFIG_DIR
# points at a scratch tree that is rebuilt from scratch for every case.
set -u

HOOK=${HOOK:-$(cd "$(dirname "$0")" && pwd)/handoff_load.py}
ROOT=${TMPDIR:-/tmp}/handoff-cases
MINE=11111111-2222-3333-4444-555555555555
THEIRS=99999999-8888-7777-6666-555555555555

NOW=$(date +%Y-%m-%dT%H:%M:%S%z)
OLD=$(date -v-90d +%Y-%m-%dT%H:%M:%S%z)

pass=0; fail=0

reset() {
  rm -rf "$ROOT"
  mkdir -p "$ROOT/cfg/handoffs" "$ROOT/work" "$ROOT/elsewhere"
}

# brief <file> <target|-> <cwd> <created> <title>
brief() {
  local f="$ROOT/cfg/handoffs/$1"
  {
    echo "---"
    echo "title: $5"
    [ "$2" != "-" ] && echo "target: session:$2"
    echo "cwd: $3"
    echo "branch: main"
    echo "created: $4"
    echo "transcript: unknown"
    echo "status: pending"
    echo "---"
    echo
    echo "# $5"
    echo
    echo "## Next steps"
    echo "1. Say TOKEN-$1"
  } > "$f"
}

# run <session_id> <cwd> <reason>
run() {
  printf '{"session_id":"%s","cwd":"%s","source":"%s"}' "$1" "$2" "$3" \
    | CLAUDE_CONFIG_DIR="$ROOT/cfg" HANDOFF_DEBUG=1 python3 "$HOOK"
}

# runcli <args...> — the interactive modes. No stdin, and the exit code matters.
runcli() {
  CLAUDE_CONFIG_DIR="$ROOT/cfg" python3 "$HOOK" "$@" < /dev/null
}

# ok <label> <condition-result 0/1> <detail>
ok() {
  if [ "$2" = "0" ]; then
    pass=$((pass+1)); printf 'PASS  %s\n' "$1"
  else
    fail=$((fail+1)); printf 'FAIL  %s\n        %s\n' "$1" "$3"
  fi
}

statusof() { grep -m1 '^status:' "$ROOT/cfg/handoffs/$1" | sed 's/^status: *//'; }

injected() { case "$1" in *additionalContext*) return 0;; *) return 1;; esac; }

echo "== reserved id matches, wrong cwd =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
out=$(run "$MINE" "$ROOT/elsewhere" startup)
injected "$out"; ok "injects from a different directory" $? "$out"
[ "$(statusof a.md)" = "consumed" ]; ok "  and is marked consumed" $? "$(statusof a.md)"

echo "== reserved id matches, brief is 90 days old =="
reset
brief a.md "$MINE" "$ROOT/work" "$OLD" "Ancient reservation"
out=$(run "$MINE" "$ROOT/work" startup)
injected "$out"; ok "injects with no expiry" $? "$out"
[ "$(statusof a.md)" = "consumed" ]; ok "  and is marked consumed, not expired" $? "$(statusof a.md)"

echo "== unreserved, cwd matches, reason startup =="
reset
brief a.md - "$ROOT/work" "$NOW" "Plain cwd brief"
out=$(run "$THEIRS" "$ROOT/work" startup)
injected "$out"; ok "the original cwd path still injects" $? "$out"

echo "== unreserved, cwd matches, reason resume =="
reset
brief a.md - "$ROOT/work" "$NOW" "Plain cwd brief"
out=$(run "$THEIRS" "$ROOT/work" resume)
injected "$out"; [ $? -ne 0 ]; ok "resume does not eat a cwd brief" $? "$out"
[ "$(statusof a.md)" = "pending" ]; ok "  and leaves it pending" $? "$(statusof a.md)"

echo "== unreserved, cwd matches, reason fork =="
reset
brief a.md - "$ROOT/work" "$NOW" "Plain cwd brief"
out=$(run "$THEIRS" "$ROOT/work" fork)
injected "$out"; [ $? -ne 0 ]; ok "fork does not eat a cwd brief" $? "$out"

echo "== reserved id matches, reason resume =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
out=$(run "$MINE" "$ROOT/elsewhere" resume)
injected "$out"; ok "a reservation still loads on resume" $? "$out"

echo "== one reserved + one unreserved, same cwd, plain start =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
brief b.md - "$ROOT/work" "$NOW" "Plain cwd brief"
out=$(run "$THEIRS" "$ROOT/work" startup)
injected "$out"; ok "the unreserved one is delivered" $? "$out"
case "$out" in *"Plain cwd brief"*) r=0;; *) r=1;; esac
ok "  and it is the plain one, not the reservation" $r "$out"
[ "$(statusof a.md)" = "pending" ]; ok "  the reservation is still pending" $? "$(statusof a.md)"

echo "== one reserved + one unreserved, same cwd, reserved start =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
brief b.md - "$ROOT/work" "$NOW" "Plain cwd brief"
out=$(run "$MINE" "$ROOT/work" startup)
case "$out" in *"Reserved brief"*) r=0;; *) r=1;; esac
ok "the reservation outranks the cwd brief" $r "$out"
[ "$(statusof b.md)" = "pending" ]; ok "  and the cwd brief is not superseded" $? "$(statusof b.md)"

echo "== two unreserved, same cwd — supersede still works =="
reset
brief older-a.md - "$ROOT/work" "$(date -v-2d +%Y-%m-%dT%H:%M:%S%z)" "Older plain"
brief newer-b.md - "$ROOT/work" "$NOW" "Newer plain"
out=$(run "$THEIRS" "$ROOT/work" startup)
case "$out" in *"Newer plain"*) r=0;; *) r=1;; esac
ok "newest wins" $r "$out"
[ "$(statusof older-a.md)" = "superseded" ]; ok "  older is superseded" $? "$(statusof older-a.md)"

echo "== nothing matches, store non-empty =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
brief b.md - "$ROOT/elsewhere/deep" "$NOW" "Unrelated brief"
out=$(run "$THEIRS" "$ROOT/work" startup)
injected "$out"; [ $? -ne 0 ]; ok "nothing is injected" $? "$out"
case "$out" in *"2 handoff briefs parked"*) r=0;; *) r=1;; esac
ok "  the parked count is reported" $r "$out"

echo "== nothing matches on resume — no nag =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
out=$(run "$THEIRS" "$ROOT/work" resume)
[ -z "$out" ]; ok "resume is silent" $? "$out"

echo "== nothing matches, one brief parked — singular =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
out=$(run "$THEIRS" "$ROOT/elsewhere" startup)
case "$out" in *"1 handoff brief parked"*) r=0;; *) r=1;; esac
ok "singular wording" $r "$out"

echo "== empty store — silent =="
reset
out=$(run "$THEIRS" "$ROOT/work" startup)
[ -z "$out" ]; ok "no output at all" $? "$out"

echo "== unreserved, 90 days old, cwd matches — still expires =="
reset
brief a.md - "$ROOT/work" "$OLD" "Ancient plain brief"
out=$(run "$THEIRS" "$ROOT/work" startup)
injected "$out"; [ $? -ne 0 ]; ok "not injected" $? "$out"
[ "$(statusof a.md)" = "expired" ]; ok "  marked expired" $? "$(statusof a.md)"

echo "== malformed target is treated as unreserved =="
reset
brief a.md "not-a-uuid" "$ROOT/work" "$NOW" "Bad target"
out=$(run "$THEIRS" "$ROOT/work" startup)
injected "$out"; ok "falls back to the cwd rule" $? "$out"

echo "== reserved id compared case-insensitively =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
out=$(run "$(echo "$MINE" | tr 'a-f' 'A-F')" "$ROOT/elsewhere" startup)
injected "$out"; ok "uppercase session id matches" $? "$out"

echo "== --list, empty store =="
reset
out=$(runcli --list); rc=$?
case "$out" in *"No handoff briefs parked."*) r=0;; *) r=1;; esac
ok "reports nothing parked" $r "$out"
[ "$rc" = "0" ]; ok "  exit 0" $? "rc=$rc"

echo "== --list, one reserved + one unreserved =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
brief b.md - "$ROOT/work" "$NOW" "Plain cwd brief"
out=$(runcli --list); rc=$?
case "$out" in *"Reserved brief"*) r=0;; *) r=1;; esac
ok "reserved brief listed" $r "$out"
case "$out" in *"Plain cwd brief"*) r=0;; *) r=1;; esac
ok "  plain brief listed" $r "$out"
case "$out" in *"claude --session-id $MINE"*) r=0;; *) r=1;; esac
ok "  reserved shows session-id command" $r "$out"
case "$out" in *"loads when you next run claude in that directory"*) r=0;; *) r=1;; esac
ok "  plain shows load-on-next-start" $r "$out"
[ "$rc" = "0" ]; ok "  exit 0" $? "rc=$rc"

echo "== --list, a consumed brief present =="
reset
printf -- '---\ntitle: Already done\ncwd: %s\ncreated: %s\ntranscript: unknown\nstatus: consumed\n---\n\n# Already done\n' \
  "$ROOT/work" "$NOW" > "$ROOT/cfg/handoffs/a.md"
out=$(runcli --list); rc=$?
case "$out" in *"Already done"*) r=1;; *) r=0;; esac
ok "consumed brief's title does not appear" $r "$out"
case "$out" in *"No handoff briefs parked."*) r=0;; *) r=1;; esac
ok "  and store reports nothing parked" $r "$out"

echo "== --list, unreadable store =="
reset
brief a.md - "$ROOT/work" "$NOW" "Reachable brief"
chmod 000 "$ROOT/cfg/handoffs"
out=$(runcli --list 2>&1); rc=$?
chmod 755 "$ROOT/cfg/handoffs"
[ "$rc" = "2" ]; ok "exit 2 on unreadable store" $? "rc=$rc"
case "$out" in *"No handoff briefs parked"*) r=1;; *) r=0;; esac
ok "  never claims nothing parked — the regression test for this plan" $r "$out"

echo "== --take by basename =="
reset
brief a.md - "$ROOT/work" "$NOW" "Take me"
out=$(CLAUDE_CODE_SESSION_ID="$MINE" runcli --take a.md); rc=$?
case "$out" in *"<handoff-brief"*) r=0;; *) r=1;; esac
ok "frame is printed" $r "$out"
[ "$(statusof a.md)" = "consumed" ]; ok "  marked consumed" $? "$(statusof a.md)"
grep -q "^consumed_by: $MINE" "$ROOT/cfg/handoffs/a.md"
ok "  consumed_by matches the exported session id" $? "$(grep '^consumed_by:' "$ROOT/cfg/handoffs/a.md")"

echo "== --take by uuid and --take by index 1 resolve the same brief =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
out=$(runcli --take "$MINE"); rc=$?
case "$out" in *"Reserved brief"*) r=0;; *) r=1;; esac
ok "take by uuid resolves the reserved brief" $r "$out"
[ "$(statusof a.md)" = "consumed" ]; ok "  and marks it consumed" $? "$(statusof a.md)"

reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
out=$(runcli --take 1); rc=$?
case "$out" in *"Reserved brief"*) r=0;; *) r=1;; esac
ok "take by index 1 resolves the same brief" $r "$out"
[ "$(statusof a.md)" = "consumed" ]; ok "  and marks it consumed" $? "$(statusof a.md)"

echo "== --take on an already-consumed brief =="
reset
brief a.md - "$ROOT/work" "$NOW" "Done already"
runcli --take a.md > /dev/null 2>&1
before=$(shasum "$ROOT/cfg/handoffs/a.md")
out=$(runcli --take a.md 2>&1); rc=$?
after=$(shasum "$ROOT/cfg/handoffs/a.md")
[ "$rc" = "2" ]; ok "exit 2 on an already-consumed brief" $? "rc=$rc"
[ "$before" = "$after" ]; ok "  and the file is byte-identical" $? "$before vs $after"

echo "== --take path traversal is refused =="
reset
out1=$(runcli --take ../../etc/passwd 2>&1); rc1=$?
out2=$(runcli --take /etc/passwd 2>&1); rc2=$?
[ "$rc1" = "2" ]; ok "../../etc/passwd is refused" $? "rc=$rc1"
[ "$rc2" = "2" ]; ok "  /etc/passwd is refused" $? "rc=$rc2"
case "$out1$out2" in *root:*|*"/bin/sh"*) r=1;; *) r=0;; esac
ok "  and nothing in /etc was ever read" $r "$out1 / $out2"

echo "== --take on a brief reserved for another uuid =="
reset
brief a.md "$THEIRS" "$ROOT/work" "$NOW" "Someone else's reservation"
out=$(runcli --take a.md 2>&1); rc=$?
[ "$rc" = "0" ]; ok "exit 0 taking someone else's reservation" $? "rc=$rc"
case "$out" in *"is now spent"*) r=0;; *) r=1;; esac
ok "  and reports the reservation as spent" $r "$out"

echo "== after --take, the hook does not double-deliver =="
reset
brief a.md "$MINE" "$ROOT/work" "$NOW" "Reserved brief"
runcli --take a.md > /dev/null 2>&1
out=$(run "$MINE" "$ROOT/work" startup)
injected "$out"; [ $? -ne 0 ]; ok "hook does not re-inject after --take" $? "$out"

echo
echo "passed $pass, failed $fail"
[ "$fail" = "0" ]
