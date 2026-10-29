#!/usr/bin/env bash
# Advisory push for System 2 (RFC 004 §2.3, Decision 5). SUGGESTS, never decides.
#
# A Claude Code hook script: reads one hook payload on stdin, keeps a per-session count of
# consecutive tool failures, and once the count reaches the retry limit prints advisory
# additionalContext suggesting the `harness2_trigger` tool. It never starts a search, never
# blocks, never exits non-zero, and makes no network call.
#
#   PostToolUseFailure  count one failure (user interrupts are not counted); advise at the limit
#   PostToolUse         a tool succeeded: reset
#   UserPromptSubmit    a new task: reset
#   Stop                if the limit was reached, show the user a one-line suggestion (systemMessage)
#   anything else       ignored
#
# State is one file per session holding one short numeric token line per failure (an append this
# small is atomic, so parallel tool calls and concurrent sessions need no lock; each failure learns
# its own position from its own line number). It holds NO prompt text, tool input, error text or
# secret: only numeric tokens, in a file named after the sanitised session id.
#
# Env: COGNITIVE_HARNESS2_HOOK_STATE_DIR  state directory (default $XDG_STATE_HOME or
#      ~/.local/state, then cognitive-engine/harness2-hook)
#
# RETRY_LIMIT mirrors cognitive_harness2.triggers.RETRY_LIMIT; a test pins them together.

RETRY_LIMIT=3
MAX_STDIN=1048576
STALE_DAYS=7

advise_text() {
  printf 'Harness² (advisory, nothing was started): %s tool calls in a row have failed. If the task is stuck, consider calling the harness2_trigger tool for a read-only check of whether a System 2 probe is worth running. You decide; System 2 only runs when you explicitly call its tools.' "$1"
}

main() {
  command -v jq >/dev/null 2>&1 || { echo "harness2-advise: jq not installed, hook disabled (install jq)" >&2; return 0; }

  local fields event sid interrupt
  fields=$(head -c "$MAX_STDIN" | jq -r 'def clean: strings | select(contains("\n") | not);
    if type == "object" then
      [(.hook_event_name | clean // ""), (.session_id | clean // ""), ((.is_interrupt == true) | tostring)] | join("|")
    else empty end' 2>/dev/null) || return 0
  IFS='|' read -r event sid interrupt <<<"$fields"
  local LC_ALL=C   # byte-wise ranges: [A-Za-z] must not match accented letters
  [[ "$sid" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || return 0

  local dir="${COGNITIVE_HARNESS2_HOOK_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cognitive-engine/harness2-hook}"
  local file="$dir/$sid.count" count=0
  # Never read, append to, reset or create through a symlink: a link planted at the state file or the
  # state directory would redirect the write (or the rm) somewhere else. Fail open: do nothing.
  [[ -L "$file" || -L "$dir" ]] && return 0
  [[ -f "$file" ]] && count=$(wc -l <"$file" 2>/dev/null | tr -d '[:space:]')
  [[ "$count" =~ ^[0-9]+$ ]] || count=0

  case "$event" in
    PostToolUseFailure)
      [[ "$interrupt" == "true" ]] && return 0
      mkdir -p "$dir" 2>/dev/null && chmod 700 "$dir" 2>/dev/null || return 0
      [[ "$count" -eq 0 ]] && find "$dir" -type f -name '*.count' -mtime +"$STALE_DAYS" -delete 2>/dev/null
      # Lock-free position: append one short unique token line (an O_APPEND write this small is
      # atomic and totally ordered), then take MY OWN line number as my position. Reading the file
      # size instead lets a burst of simultaneous failures all see a count past the limit multiple,
      # so nobody advises (a lock does not help: mkdir was not exclusive under rapid create/remove
      # cycling on the test host). With distinct positions each multiple is seen by exactly one process.
      local tok="$$.$RANDOM.$RANDOM"
      printf '%s\n' "$tok" >>"$file" 2>/dev/null || return 0
      count=$(grep -n -F -x -m1 -- "$tok" "$file" 2>/dev/null | cut -d: -f1)
      [[ "$count" =~ ^[0-9]+$ ]] || return 0
      # at the limit, then again at each further multiple of it, so a long streak is not silent but not noisy
      if [[ "$count" -ge "$RETRY_LIMIT" && $((count % RETRY_LIMIT)) -eq 0 ]]; then
        jq -nc --arg t "$(advise_text "$count")" \
          '{hookSpecificOutput: {hookEventName: "PostToolUseFailure", additionalContext: $t}}'
      fi ;;
    PostToolUse|UserPromptSubmit)
      [[ "$count" -gt 0 ]] && rm -f "$file" 2>/dev/null ;;
    Stop)
      if [[ "$count" -ge "$RETRY_LIMIT" ]]; then
        jq -nc --arg t "Harness² suggestion: $count tool failures in a row this task. Consider the harness2_trigger tool (read-only check)." \
          '{systemMessage: $t}'
      fi ;;
  esac
  return 0
}

main || true
exit 0
