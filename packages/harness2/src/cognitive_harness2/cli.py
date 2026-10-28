"""`cognitive-harness2`: run the MCP server, and the human side of experience sign-off.

    cognitive-harness2 serve [--stdio] [--port N]
    cognitive-harness2 experience pending
    cognitive-harness2 experience approve ID --by NAME

`approve` is intentionally a CLI step and not an MCP tool: an agent can stage a lesson
(`harness2_reflect`) but cannot commit one.
"""
from __future__ import annotations

import argparse
import sys

from . import settings
from .experience import ExperienceError, ExperienceStore, SignoffRequired


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cognitive-harness2", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run the MCP server (loopback HTTP, or --stdio)")
    p.add_argument("--stdio", action="store_true")
    p.add_argument("--port", type=int)
    e = sub.add_parser("experience", help="review and approve staged lessons").add_subparsers(dest="action", required=True)
    e.add_parser("pending", help="list staged lessons")
    a = e.add_parser("approve", help="commit a staged lesson (needs a named human)")
    a.add_argument("id")
    a.add_argument("--by", dest="by", required=True, help="who is approving")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        s = settings.from_env()
    except settings.SettingsError as e:
        sys.exit(f"cognitive-harness2: {e}")
    if a.cmd == "serve":
        from .server import serve
        serve(stdio=a.stdio, port=a.port)
        return 0
    store = ExperienceStore(s.experience_root)
    if a.action == "pending":
        for p in store.pending():
            print(f"{p.id}  [{p.domain}]  {p.lesson.splitlines()[0][:80]}")
        return 0
    try:
        print(f"committed to {store.approve(a.id, approved_by=a.by)}")
    except (SignoffRequired, ExperienceError) as e:
        sys.exit(f"cognitive-harness2: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
