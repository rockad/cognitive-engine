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
#
# Like the real `opencode run --standalone` (v2.0.25), it ALSO starts a private HTTP server on a random port of its own
# loopback and calls it, and it calls the model endpoint, both with a PROXY-AWARE client (python's urllib honours
# HTTP_PROXY and NO_PROXY, as opencode's HTTP stack does). The first real stage-1 run died here: the runner set no NO_PROXY,
# so the CLI's own loopback request went to the egress proxy and was denied (403), while an earlier version of this fake
# used a raw /dev/tcp socket that never looked at the proxy variables and so could not have caught it.

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
[ -n "$BASE" ] || { echo "fake opencode: no baseURL in OPENCODE_CONFIG_CONTENT" >&2; exit 3; }
STATUS=$(python3 -I - "$BASE" <<'PY'
import http.server, sys, threading, urllib.error, urllib.request

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                                   # noqa: N802
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return "HTTP %d" % r.status
    except urllib.error.HTTPError as e:
        return "HTTP %d" % e.code
    except Exception as e:                                              # noqa: BLE001
        return "ERR %s" % type(e).__name__

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)              # opencode's private server: random port, own loopback
threading.Thread(target=srv.serve_forever, daemon=True).start()
own = get("http://127.0.0.1:%d/" % srv.server_address[1])
if own != "HTTP 200":
    print("own-loopback server answered " + own); sys.exit(3)
print(get(sys.argv[1].rstrip("/") + "/models"))                         # the model endpoint, via the proxy
PY
) || { echo "fake opencode: $STATUS" >&2; exit 3; }
case "$STATUS" in "HTTP 200") ;; *) echo "fake opencode: the model endpoint answered '$STATUS'" >&2; exit 3 ;; esac

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
