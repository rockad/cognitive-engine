#!/usr/bin/env bash
# Deliberately crashing hook (exit 1, nothing on stdout) for the onFailure comparison.
cat >/dev/null
echo "crash.sh: deliberate failure" >&2
exit 1
