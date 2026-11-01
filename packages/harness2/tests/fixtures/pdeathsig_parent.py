"""Test helper: start the REAL host egress proxy the way `SbxSessionRunner` starts it (`sbx_session._spawn_proxy`: own session, no
preexec_fn, a lifeline pipe), from a WORKER THREAD that then exits, print its pid, then wait to be killed. The test kills THIS
process with SIGKILL (no cleanup runs) and checks the proxy follows; it also checks the proxy was still alive after the spawning
thread was gone (`PR_SET_PDEATHSIG` is tied to the thread that spawned the child, which is one of the reasons it is not used)."""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from cognitive_harness2 import egress_proxy, sbx_session

base = Path(tempfile.mkdtemp(prefix="h2pdeath"))
port, forward = sbx_session._free_ports(2)
cfg = base / "proxy.json"
cfg.write_text(json.dumps({"listen_host": "127.0.0.1", "listen_port": forward, "allow": []}))
out: dict = {}


def spawn() -> None:
    with open(base / "proxy.log", "wb") as log:
        out["proc"], out["lifeline"] = sbx_session._spawn_proxy(sys.executable, egress_proxy.__file__, str(cfg), log)


t = threading.Thread(target=spawn)
t.start()
t.join()                                                  # the spawning thread is gone from here on
for _ in range(200):
    if egress_proxy.READY_LINE in (base / "proxy.log").read_text(errors="replace"):
        break
    time.sleep(0.05)
print(out["proc"].pid, flush=True)
time.sleep(300)
sys.exit(0)
