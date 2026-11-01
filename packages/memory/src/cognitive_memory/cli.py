"""`cognitive-memory`: launch upstream basic-memory natively, under validated settings.

    cognitive-memory serve            streamable HTTP on 127.0.0.1:8105/mcp
    cognitive-memory serve --stdio    stdio, for a CLI harness that spawns it
    cognitive-memory env              print the environment `serve` would use

`serve` `exec`s upstream rather than spawning it: no wrapper process sits in the
stdio pipe, signals reach the daemon directly, and the only coupling between this
package and upstream is the environment and the command line — the process
boundary RFC 001 Decision 23 relies on to keep AGPL-3.0 inside this package.
Nothing here is printed on stdout under `--stdio`; that stream is the protocol.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import settings


def _upstream() -> str:
    """The `basic-memory` console script from this interpreter's environment."""
    beside = Path(sys.executable).with_name("basic-memory")
    found = str(beside) if beside.is_file() else shutil.which("basic-memory")
    if not found:
        sys.exit("cognitive-memory: the `basic-memory` executable is not installed "
                 "in this environment; run `uv sync`.")
    return found


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cognitive-memory", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run the daemon")
    p.add_argument("--stdio", action="store_true",
                   help="serve over stdio instead of loopback HTTP")
    p.add_argument("--port", type=int, help=f"HTTP port (default {settings.PORT})")
    sub.add_parser("env", help="print the resolved environment as JSON")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    environ = dict(os.environ)
    if getattr(a, "port", None):
        environ[settings.ENV_PORT] = str(a.port)
    try:
        s = settings.from_env(environ)
    except settings.SettingsError as e:
        sys.exit(f"cognitive-memory: {e}")

    env = settings.basic_memory_env(s)
    if a.cmd == "env":
        print(json.dumps({"root": str(s.root), "env": env}, indent=2, sort_keys=True))
        return 0

    s.config_dir.mkdir(parents=True, exist_ok=True)
    s.notes_dir.mkdir(parents=True, exist_ok=True)
    argv_up = settings.serve_argv(s, "stdio" if a.stdio else "http")
    exe = _upstream()
    os.execve(exe, [exe, *argv_up], {**environ, **env})
    return 0  # pragma: no cover — execve does not return


if __name__ == "__main__":
    raise SystemExit(main())
