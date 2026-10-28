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

import fnmatch
import posixpath
import re

MUTABLE = frozenset({"prompt", "tool_description", "context_assembly", "decomposition_strategy"})

PROTECTED_COMPONENTS = frozenset({
    "guardrail", "verification_gate", "egress_policy", "zdr_policy",
    "session_ceiling", "lifecycle_hook", "human_approval",
})

# Paths whose contents define the protected set. Matched after normalisation, so
# `./a/../hooks/x`, `HOOKS/x` and `hooks\x` all hit the same rule.
PROTECTED_PATH_PATTERNS = (
    "*/hooks/*", "hooks/*", "*hooks.json", "*/guard.*", "guard.*", "*/hook.*", "hook.*",
    "*egress*", "*zdr*", "*governance*", "*ceiling*", "*.claude/settings*", "*approval*",
    "*mcp_config.json", "*.mcp.json",
)


class InvariantViolation(PermissionError):
    """A proposed change touches something the optimizer is barred from modifying."""


def normalise_component(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def normalise_path(path: str) -> str:
    """Lower-cased, separator-normalised and collapsed (`a/../b` -> `b`).

    ⚠️ Deliberately does NOT strip leading dots or slashes: an earlier `.lstrip("./")`
    turned `../../escape` into `escape` and `/etc/passwd` into `etc/passwd`, hiding the
    very escapes `assert_path_unprotected` exists to catch.
    """
    return posixpath.normpath(path.replace("\\", "/").strip().lower())


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
    norm = normalise_path(path)
    if norm == ".." or norm.startswith("../") or posixpath.isabs(norm):
        raise InvariantViolation(f"{path!r} escapes the working tree")
    for pattern in PROTECTED_PATH_PATTERNS:
        if fnmatch.fnmatch(norm, pattern):
            raise InvariantViolation(f"{path!r} matches protected pattern {pattern!r}")
    return norm
