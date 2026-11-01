"""Test helper: start a long `sleep` the way `SbxSessionRunner` starts its host proxy (own session, `_die_with_parent`), print
its pid, then wait to be killed. The test kills THIS process with SIGKILL (no cleanup runs) and checks the sleeper follows."""
import subprocess
import sys
import time

from cognitive_harness2 import sbx_session

child = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, preexec_fn=sbx_session._die_with_parent)
print(child.pid, flush=True)
time.sleep(300)
sys.exit(0)
