#!/usr/bin/env bash
# Redact one canary payload (JSON on stdin) for committing as a fixture: home dir -> ~, session /
# prompt / tool-use ids and UUIDs replaced by placeholders, job id dropped. Field names are kept.
set -euo pipefail
home="${HOME}"
jq --arg home "$home" '
  def scrub: gsub($home; "~")
    | gsub("-" + ($home | ltrimstr("/") | gsub("/"; "-")) + "-"; "-~-")
    | gsub("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"; "<uuid>")
    | gsub("toolu_[A-Za-z0-9]+"; "toolu_REDACTED")
    | gsub("jobs(?<s>[/-])[0-9a-f]{8}"; "jobs\(.s)JOB");
  walk(if type == "string" then scrub else . end)
  | (if has("session_id") then .session_id = "<session-id>" else . end)
  | (if has("prompt_id") then .prompt_id = "<prompt-id>" else . end)
' 
