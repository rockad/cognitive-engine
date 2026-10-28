#!/usr/bin/env bash
# Log-only hook canary: append one JSONL line {event, ts, payload} per call to
# $CANARY_DIR/<event>.jsonl. Fail-open by construction: always exits 0, writes nothing to
# stdout, never logs the environment. Usage: canary.sh <EventName>
event="${1:-unknown}"
dir="${CANARY_DIR:-$HOME/.claude/jobs/317124a3/tmp/canary}"
mkdir -p "$dir" 2>/dev/null || exit 0
payload="$(cat)"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if ! printf '%s' "$payload" | jq -c --arg event "$event" --arg ts "$ts" \
  '{event: $event, ts: $ts, payload: .}' >>"$dir/$event.jsonl" 2>/dev/null; then
  jq -cn --arg event "$event" --arg ts "$ts" --arg raw "$payload" \
    '{event: $event, ts: $ts, unparsed: $raw}' >>"$dir/$event.jsonl" 2>/dev/null
fi
exit 0
