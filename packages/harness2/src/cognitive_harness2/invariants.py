"""The protected invariant set: what the optimizer may never change (RFC 004 §3.1).

Default-deny. A mutation is refused unless its component is in `MUTABLE`, and refused
regardless if it touches a protected path. An unknown component name is an error, not
a pass: the optimizer must not be able to widen its own remit by inventing a name.

⚠️ RFC 001 says Harness² can mutate 8 harness components; RFC 004 names four (prompts,
tool descriptions, context assembly, decomposition strategies). Only those four are
allowed here. Adding the other four is a deliberate edit to `MUTABLE`, never an
inference.
"""
from __future__ import annotations

import posixpath
import re
import unicodedata

_DRIVE = re.compile(r"^[a-z]:")

MUTABLE = frozenset({"prompt", "tool_description", "context_assembly", "decomposition_strategy"})

PROTECTED_COMPONENTS = frozenset({
    "guardrail", "verification_gate", "egress_policy", "zdr_policy",
    "session_ceiling", "lifecycle_hook", "human_approval",
})

# Paths whose contents define the protected set. Matching is per path SEGMENT, after
# normalisation (so `./a/../hooks/x`, `HOOKS/x`, `hooks\x` and fullwidth `ｈｏｏｋｓ` hit the
# same rule), and a protected segment protects the whole subtree below it. Matching on a
# whole-path glob missed the directories themselves and every directory spelled without a
# dot (`src/guard/index.ts`, bare `hooks`), because the globs needed a `.` or a child.
#
# A segment is protected when its stem (name up to the first dot, leading dots ignored) is one of
# these. `hooks`, `hooks.json`, `hooks.bak` and `.hooks` all have the stem `hooks`; `hookup` does not.
PROTECTED_STEMS = frozenset({"hooks", "hook", "guard"})
# Git control paths, matched as WHOLE segments (so `.gitignore`, `gitutils.py` and `github/` stay
# ordinary): `.git` (a directory, or a file in a worktree) decides core.hooksPath and filter drivers,
# `.gitmodules` and `.gitattributes` redirect what git runs, `.github` holds CI. Each protects the whole
# subtree below it. `git~1` style Windows 8.3 short names are the same entries under another spelling.
PROTECTED_GIT_SEGMENTS = frozenset({".git", ".gitmodules", ".gitattributes", ".github"})
_GIT_SHORT_NAME = re.compile(r"^\.?git[a-z]*~\d+$")
# ...or when the segment name ends with one of these (`my-hooks.json`, `x.mcp.json`).
PROTECTED_SEGMENT_SUFFIXES = ("hooks.json", "mcp_config.json", ".mcp.json")
# ...or when this text appears anywhere in the path.
PROTECTED_SUBSTRINGS = ("egress", "zdr", "governance", "ceiling", "approval")
# ...or when a `.claude` segment is followed by a `settings*` segment.
PROTECTED_CLAUDE_DIR, PROTECTED_CLAUDE_CHILD = ".claude", "settings"


class InvariantViolation(PermissionError):
    """A proposed change touches something the optimizer is barred from modifying."""


def normalise_component(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def normalise_path(path: str) -> str:
    """NFKC-folded, case-folded, separator-normalised and collapsed (`a/../b` -> `b`).

    ⚠️ Deliberately does NOT strip leading dots or slashes: an earlier `.lstrip("./")`
    turned `../../escape` into `escape` and `/etc/passwd` into `etc/passwd`, hiding the
    very escapes `assert_path_unprotected` exists to catch.
    """
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", path).casefold())
    return posixpath.normpath(folded.replace("\\", "/").strip())


def _segment_protected(seg: str) -> bool:
    seg = seg.rstrip(". ")  # `hooks.` and `hooks ` name the same entry on some filesystems
    if not seg:
        return False
    if seg in PROTECTED_GIT_SEGMENTS or _GIT_SHORT_NAME.match(seg):
        return True
    if seg.lstrip(".").split(".", 1)[0] in PROTECTED_STEMS:
        return True
    return seg.endswith(PROTECTED_SEGMENT_SUFFIXES)


def assert_mutable(component: str) -> str:
    """Return the normalised component name, or raise."""
    norm = normalise_component(component)
    if norm in PROTECTED_COMPONENTS:
        raise InvariantViolation(f"{component!r} is a protected component and cannot be mutated")
    if norm not in MUTABLE:
        raise InvariantViolation(
            f"{component!r} is not a mutable component (allowed: {', '.join(sorted(MUTABLE))})")
    return norm


def assert_path_unprotected(path: str) -> str:
    """Return the normalised path, or raise. Default-deny: anything odd is refused.

    Refused: control or invisible characters (checked on the raw text, before any trimming),
    absolute and drive-letter paths, `..` escapes, the tree root, and any protected file, any
    protected directory itself, and anything under one (see `PROTECTED_*`).
    """
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in path):
        raise InvariantViolation(f"{path!r} contains control or invisible characters")
    norm = normalise_path(path)
    if norm in ("", "."):
        raise InvariantViolation(f"{path!r} names the working tree itself")
    if norm == ".." or norm.startswith("../") or posixpath.isabs(norm) or _DRIVE.match(norm):
        raise InvariantViolation(f"{path!r} escapes the working tree")
    segments = [s for s in norm.split("/") if s]
    for i, seg in enumerate(segments):
        if _segment_protected(seg):
            raise InvariantViolation(f"{path!r} is, or is under, protected path segment {seg!r}")
        if seg.endswith(PROTECTED_CLAUDE_DIR) and i + 1 < len(segments) \
                and segments[i + 1].startswith(PROTECTED_CLAUDE_CHILD):
            raise InvariantViolation(f"{path!r} is, or is under, protected {PROTECTED_CLAUDE_DIR}/settings")
    for text in PROTECTED_SUBSTRINGS:
        if text in norm:
            raise InvariantViolation(f"{path!r} contains protected name {text!r}")
    return norm
