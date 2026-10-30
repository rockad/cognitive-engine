#!/bin/bash
# A FAKE `opencode` for the stage-1 selftest. It NEVER talks to a model.
#
# It is bind-mounted read-only at /opt/cli/opencode and run exactly as the real stage-1 sessions are:
#     opencode run --standalone --auto --format json --model ollama/gemma4:12b "<prompt>"
# in the session's workspace. It plays both roles:
#   * a PROPOSER session has MISSION.md: it appends one line to context/harness/systemprompt.md and answers in
#     output/answer.md, the way upstream's mission asks;
#   * a SOLVER rollout has input.txt: it adds the signed amounts exactly (whole cents) and writes
#     output/answer.txt as "Total: X.XX".
# Before either, it reaches the "model endpoint" the way the real CLI would: an HTTP GET to the base URL in
# OPENCODE_CONFIG_CONTENT (the proxy alias on the internal network), which the proxy relays over the host bridge to
# whatever listens on the channel's loopback port (in the selftest, a throwaway fake). A session that cannot reach
# it prints no events and exits 3, so a broken bridge fails the sweep instead of passing silently.

jstr() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | awk 'BEGIN{ORS="\\n"} {print}' | sed 's/\\n$//'; }
ev_text() { printf '{"type":"text","part":{"type":"text","text":"%s"}}\n' "$(jstr "$1")"; }
ev_tool() { printf '{"type":"tool_use","part":{"type":"tool","tool":"%s","state":{"status":"completed","input":{"command":"%s"},"output":"%s"}}}\n' "$1" "$(jstr "$2")" "$(jstr "$3")"; }
cents_of() { awk '{ s=$0; neg=0; if (substr(s,1,1)=="-") { neg=1; s=substr(s,2) }
                    split(s,p,"."); c=p[1]*100+p[2]; t += neg ? -c : c } END { printf "%d\n", t }' "$1"; }
money() { awk -v t="$1" 'BEGIN { n=(t<0); a=n?-t:t; printf "%s%d.%02d\n", n?"-":"", int(a/100), a%100 }'; }

[ "$1" = run ] && [ "$2" = --standalone ] && [ "$3" = --auto ] && [ "$4" = --format ] && [ "$5" = json ] && [ "$6" = --model ] \
  || { echo "fake opencode: unexpected argv shape" >&2; exit 64; }
MODEL="$7"

# the model endpoint, via the base URL of the configured provider
BASE=$(printf '%s' "$OPENCODE_CONFIG_CONTENT" | sed -n 's/.*"baseURL":"\([^"]*\)".*/\1/p')
HP=${BASE#http://}; HP=${HP%%/*}; HOST=${HP%%:*}; PORT=${HP##*:}
if ! exec 3<>"/dev/tcp/$HOST/$PORT" 2>/dev/null; then echo "fake opencode: cannot reach $HOST:$PORT" >&2; exit 3; fi
printf 'GET /api/tags HTTP/1.0\r\nHost: model\r\n\r\n' >&3
STATUS=$(timeout 10 head -n 1 <&3 | tr -d '\r')
exec 3>&- 3<&-
case "$STATUS" in *" 200 "*) ;; *) echo "fake opencode: the model endpoint answered '$STATUS'" >&2; exit 3 ;; esac

echo '{"type":"step_start","part":{"type":"step-start"}}'
ev_text "model endpoint answered: $STATUS"
if [ -f MISSION.md ]; then
  f=context/harness/systemprompt.md
  [ -f "$f" ] && printf '\nCheck the sign of every amount before adding it.\n' >> "$f"
  mkdir -p output
  printf '<notes>fake proposer: added a sign check to systemprompt.md</notes>\n' > output/answer.md
  ev_text "<notes>fake proposer: added a sign check to systemprompt.md</notes>"
elif [ -f input.txt ]; then
  ev_text "AGENTS: $(head -n 2 AGENTS.md 2>/dev/null | tr '\n' ' ')"
  ev_tool bash "cat input.txt" "$(cat input.txt)"
  mkdir -p output
  printf 'Total: %s\n' "$(money "$(cents_of input.txt)")" > output/answer.txt
  ev_tool write "output/answer.txt" "done"
else
  echo "fake opencode: neither MISSION.md nor input.txt in $(pwd)" >&2; exit 65
fi
echo '{"type":"step_finish","part":{"type":"step-finish","cost":0}}'
