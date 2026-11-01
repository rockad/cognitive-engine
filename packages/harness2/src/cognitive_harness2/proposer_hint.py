"""The proposer's ENVIRONMENT RULES block (stage 1c): which harness files a proposer may change, said once, up front.

Why. In the real hard-set runs (2026-11-01) the INV proposer wrote the answer into a script (slot c7, refused by the
slot table, twice) and the qwen proposers made no edit at all. Nothing in upstream's prompts says which files the
runner keeps: a proposer finds out only when its whole proposal is thrown away. This block says it before the session.

It is OUR runner's text, not upstream's. It is prepended to a proposer session's system text by `ContainedProposer.call`
(never to a solver rollout, never to the selector), written into the proposer audit with its hash, and switched by
`stage1 --proposer-hint {on,off}`.

The file lists are DERIVED, never typed: `upstream_surface.layout` reads the surface the slot table reads, so a file is
named as mutable only if its slot maps onto `invariants.MUTABLE`, and as read-only only if it does not. A test holds the
text to the table (every named mutable path classifies as mutable, every named read-only path as denied). The only
fixed words are the rules' prose and `HARNESS_DIR`, the directory upstream harvests the edited tree from
(`harvest="context/harness"` in `harness2.core.recursion._run_stage`; a test pins it).

Nothing here imports upstream: a surface is read by duck typing, as in `upstream_surface`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from . import upstream_surface

HARNESS_DIR = "context/harness"          # where the proposer finds the harness; upstream harvests this subpath back
SHA_LEN = 12

INTRO = ("ENVIRONMENT RULES (added by the runner; they are not part of the task)\n"
         f"The harness you edit is the directory {HARNESS_DIR} . When you finish, the runner checks every file you "
         "changed against a fixed table of what may change.\n")
MUTABLE_HEAD = "Mutable (a change here is kept when it passes the check):\n"
READONLY_HEAD = "Read-only (a change to any of these gets the whole proposal discarded, and the harness stays as it was):\n"
OUTRO = ("Scripts, plugins and guardrails.md are never edited, whatever they are for. Put what the solver should do into a "
         "mutable file as plain instructions in words. Do not write the answer to a task into any file, and do not start a "
         "file with a `---` frontmatter block.\n")
BODY_ONLY = " (body only: its leading frontmatter stays byte-identical)"


@dataclass(frozen=True)
class ProposerHint:
    text: str                         # the exact block, ending in a newline
    sha: str                          # first 12 hex digits of sha256(text)
    mutable: tuple[str, ...]          # exact paths and `<name>` templates the slot table lets a proposer change
    readonly: tuple[str, ...]         # the ones it refuses (present on the surface but denied)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:SHA_LEN]


def _line(where: str, *, note: str = "") -> str:
    return f"- {HARNESS_DIR}/{where}{note}\n"


def build_hint(surface: Any) -> ProposerHint:
    """The block for `surface` (an upstream `SurfaceSpec`). Raises `InvariantViolation` for a shape it does not recognise."""
    mutable, readonly = upstream_surface.layout(surface)
    text = (INTRO
            + MUTABLE_HEAD + "".join(_line(e.where, note=BODY_ONLY if e.is_template else "") for e in mutable)
            + READONLY_HEAD + "".join(_line(e.where) for e in readonly)
            + OUTRO)
    return ProposerHint(text=text, sha=digest(text), mutable=tuple(e.where for e in mutable),
                        readonly=tuple(e.where for e in readonly))
