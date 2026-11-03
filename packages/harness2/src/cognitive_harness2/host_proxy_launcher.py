"""Run the egress proxy as a HOST process that stops when its parent dies (stdlib only; started as `python -I <this file>`).

    python -I host_proxy_launcher.py --parent-fd N -- /path/egress_proxy.py --config proxy.json

The parent keeps the write end of a pipe and the launcher inherits the read end as descriptor N. When the parent dies, however it
dies, the kernel closes the write end, the launcher reads EOF and sends itself SIGTERM, which the proxy handles as its orderly stop
(it prints its final ACCESS-SUMMARY). This replaces `preexec_fn` + `PR_SET_PDEATHSIG`: the first can deadlock in a multithreaded
parent, the second fires when the SPAWNING THREAD exits, not the process. The proxy source is run unchanged (it is baked into the
Docker image by content hash, so it must not grow host-only options)."""
from __future__ import annotations

import os
import runpy
import signal
import sys
import threading


def main(argv: list[str]) -> int:
    if len(argv) < 5 or argv[0] != "--parent-fd" or argv[2] != "--":
        print("usage: host_proxy_launcher.py --parent-fd N -- SCRIPT [ARGS...]", file=sys.stderr, flush=True)
        return 2
    try:
        fd = int(argv[1])
        os.fstat(fd)
    except (ValueError, OSError):
        print(f"host-proxy-launcher: refusing to start: --parent-fd {argv[1]!r} is not an open descriptor", file=sys.stderr, flush=True)
        return 2

    def watch() -> None:
        try:
            while os.read(fd, 1):
                pass
        except OSError:
            pass
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
    script, rest = argv[3], argv[4:]
    sys.argv = [script, *rest]
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
