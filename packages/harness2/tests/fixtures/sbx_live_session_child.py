"""Live-test helper (COGNITIVE_HARNESS2_SBX_LIVE=1): run ONE sbx model session whose "CLI" just sleeps, as a separate process that the
test can SIGTERM or SIGKILL while the microVM is up. usage: sbx_live_session_child.py ROOT SBX_PATH

No model is called: the profile's endpoint is a throwaway local socket and the in-VM "CLI" is `sleep`. The process exits with
whatever the runner raised, which for SIGTERM is the same KeyboardInterrupt path as Ctrl-C."""
import os
import socket
import sys
from pathlib import Path

from cognitive_harness2 import sandbox, sbx_session as ss, stage1
from cognitive_harness2 import session_runner as sr

root, sbx = Path(sys.argv[1]).resolve(), sys.argv[2]
ws = root / "ws"
ws.mkdir()
cli = root / "h2sleep"
cli.write_text("#!/bin/sh\nsleep 300\n")
cli.chmod(0o755)
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(1)
profile = stage1.selftest_profile(root, listener.getsockname()[1])
version = sandbox.real_sbx_probes(dict(os.environ)).version(sbx)
runner = ss.SbxSessionRunner(sbx, profile.channels[stage1.CHANNEL], zdr="strict", classification="restricted", role="solver",
                             workspace_roots=profile.workspace_roots, verified=frozenset({version}), environ=dict(os.environ),
                             create_timeout_s=900)
spec = sr.SessionSpec(argv=(sr.container_cli_path(str(cli)),), workspace=str(ws), cli=str(cli), timeout_s=280,
                      env={"H2_MODEL_URL": f"http://{sr.PROXY_ALIAS}:{listener.getsockname()[1]}/v1", "H2_MODEL": "none"})
print("starting", flush=True)
runner.run(spec)
