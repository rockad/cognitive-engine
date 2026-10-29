"""Seam 1: upstream's editable surface (c1..c8) mapped onto our protected set, default-deny.

Upstream's `SurfaceSpec` (harness2.core.tree) declares WHERE a framework's editable files live
(`always_on`, `skill_dir`, `plugin_dir`, `hooks_file`); the eight components c1..c8 are a
naming convention over those paths, not fields of the spec. Upstream's own `is_allowed` is
permissive by design (it lets every component through, scripts and plugins included). This
module is the stricter, default-deny reading: a path or a slot is mutable only if it maps onto
one of the four `invariants.MUTABLE` components, and everything else is refused.

    slot  upstream path                              our component
    c1    systemprompt.md                            prompt
    c2    guardrails.md                              (guardrail: protected)
    c3    LongTermMEMORY.md                          context_assembly
    c4    tool_descriptions/tool_guidance.md         tool_description
    c5    sub_agents/<name>/AGENT.md                 decomposition_strategy (BODY ONLY)
    c6    skills/<name>/SKILL.md                     context_assembly (BODY ONLY)
    c7    scripts/<name>.py, <skill>/scripts/*       (executable code: denied)
    c8    plugin/<name>.ts, hooks_file               (lifecycle hooks: protected)

⚠️ Upstream is inconsistent about numbering: its README orders the components system prompt,
guardrails, memory, tool guidance, subagents, skills, scripts, plugins (so guardrails is c2),
while its prompt tables label `guardrails.md` as c6 and have no c2. This module binds to the
PATHS, which agree everywhere, and uses the README numbering. Codex folds c1/c3/c4 and the
guardrails into one `AGENTS.md`; it cannot be split, so it is denied whole.

The mapping c3 -> context_assembly, c5 -> decomposition_strategy and c6 -> context_assembly is
a policy choice RFC 004 / Slice 3b defines: skills are on-demand instructions assembled into
context, while `MUTABLE` stays strictly the four components. Frontmatter in c5 and c6 is frozen.
It lives in `SLOT_COMPONENT` as data: widening it is a deliberate edit there, never an
inference. `invariants.assert_mutable` is consulted on every call, so removing a component from
`MUTABLE` removes its slot.

Nothing here imports upstream: a spec is read by duck typing, so this module imports (and its
refusals work) without the `upstream` extra. Importing `harness2.substrate` would run
`harness2.config`, which reads `$CWD/.env` into `os.environ`.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from . import invariants
from .invariants import InvariantViolation

SLOTS = ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8")

# slot -> our component. `None` is a slot we deny. Edit deliberately.
SLOT_COMPONENT: Mapping[str, str | None] = MappingProxyType({
    "c1": "prompt",
    "c2": None,
    "c3": "context_assembly",
    "c4": "tool_description",
    "c5": "decomposition_strategy",
    "c6": "context_assembly",
    "c7": None,
    "c8": None,
})

SLOT_DENIAL: Mapping[str, str] = MappingProxyType({
    "c2": "guardrails are a protected component (invariants.PROTECTED_COMPONENTS)",
    "c7": "scripts are executable code, not one of the four mutable components",
    "c8": "plugins and lifecycle hooks act inside the tool loop and are protected (lifecycle_hook)",
    "composite": "AGENTS.md folds the prompt, memory, tool prose and guardrails into one file; "
                 "it cannot be split, so it is denied whole",
})

# Upstream's always-on file names -> slot. Exact, case-sensitive.
_ALWAYS_ON_SLOT = {
    "systemprompt.md": "c1",
    "guardrails.md": "c2",
    "LongTermMEMORY.md": "c3",
    "tool_descriptions/tool_guidance.md": "c4",
    "AGENTS.md": "composite",
}

# Upstream's name rule for skills, subagents and plugins (harness2.core.tree._NAME).
_NAME = r"[a-z0-9][a-z0-9_\-]*"
_SUBAGENT = re.compile(rf"sub_agents/{_NAME}/AGENT\.md")
_SCRIPT = re.compile(r"scripts/[a-z0-9_][a-z0-9_]*\.py")
_SKILL_SCRIPT = r"{sd}/" + _NAME + r"/scripts/[A-Za-z0-9_.\-]+"
_SKILL = r"{sd}/" + _NAME + r"/SKILL\.md"
_PLUGIN = r"{pd}/" + _NAME + r"\.ts"

_PATTERNS = {
    "c5": "sub_agents/<name>/AGENT.md",
}


@dataclass(frozen=True)
class AllowedSurface:
    """What may be mutated on one surface. `denied` maps each slot present but refused to why."""
    surface: str
    slots: frozenset[str]
    components: frozenset[str]
    files: frozenset[str]          # exact paths that are mutable
    patterns: tuple[str, ...]      # path templates that are mutable
    denied: Mapping[str, str]


def _refuse(msg: str) -> InvariantViolation:
    return InvariantViolation(msg)


def _require_spec(spec: Any) -> None:
    ok = (
        isinstance(getattr(spec, "name", None), str)
        and isinstance(getattr(spec, "always_on", None), (tuple, list))
        and all(isinstance(a, str) for a in getattr(spec, "always_on", ()))
        and isinstance(getattr(spec, "skill_dir", None), str) and bool(spec.skill_dir)
        and isinstance(getattr(spec, "plugin_dir", ""), (str, type(None)))
        and hasattr(spec, "plugin_dir")
        and isinstance(getattr(spec, "hooks_file", ""), (str, type(None)))
        and hasattr(spec, "hooks_file")
    )
    if not ok:
        raise _refuse(
            "unrecognised surface spec: expected an upstream SurfaceSpec (name, always_on, skill_dir, "
            "plugin_dir, hooks_file); refusing by default")


def _component_if_mutable(slot: str) -> str | None:
    """The mutable component behind `slot`, or None. Re-checked against `MUTABLE` on every call."""
    comp = SLOT_COMPONENT.get(slot)
    if comp is None:
        return None
    try:
        return invariants.assert_mutable(comp)
    except InvariantViolation:
        return None


def _present(spec: Any) -> dict[str, str]:
    """slot -> the path or template it occupies on THIS surface (denied slots included)."""
    present: dict[str, str] = {}
    for name in spec.always_on:
        slot = _ALWAYS_ON_SLOT.get(name)
        present[slot or f"unknown:{name}"] = name
    present["c5"] = _PATTERNS["c5"]
    present["c6"] = f"{spec.skill_dir}/<name>/SKILL.md"
    present["c7"] = "scripts/<name>.py"
    if spec.plugin_dir is not None or spec.hooks_file is not None:
        present["c8"] = spec.hooks_file or f"{spec.plugin_dir}/<name>.ts"
    return present


def _denial(slot: str, where: str) -> str:
    if slot.startswith("unknown:"):
        return f"{where}: not a slot this module recognises (unknown slot, denied by default)"
    return f"{where}: {SLOT_DENIAL.get(slot) or 'not mutable under invariants.MUTABLE'}"


def _partition(spec: Any) -> tuple[dict[str, str], dict[str, str]]:
    allowed: dict[str, str] = {}
    denied: dict[str, str] = {}
    for slot, where in _present(spec).items():
        if _component_if_mutable(slot) is not None:
            allowed[slot] = where
        else:
            denied[slot] = _denial(slot, where)
    return allowed, denied


def _check_name(raw: Any) -> str:
    """The key rules of the hardened staging, plus the extra ones this seam needs."""
    if not isinstance(raw, str):
        raise _refuse(f"path {raw!r} is not a string")
    if not raw:
        raise _refuse("invalid staged file name '': must be a non-empty string")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in raw):
        raise _refuse(f"invalid staged file name {raw!r}: control or invisible characters")
    if "\\" in raw:
        raise _refuse(f"invalid staged file name {raw!r}: backslashes are not allowed")
    if raw.startswith("/") or raw.endswith("/"):
        raise _refuse(f"invalid staged file name {raw!r}: must be a relative file path, not absolute or a directory")
    for seg in raw.split("/"):
        if seg in ("", ".", "..") or seg != seg.strip():
            raise _refuse(f"invalid staged file name {raw!r}: not a clean relative path (segment {seg!r})")
    if "->" in raw or raw.endswith(("@", "|", "=")) or any(seg.endswith("@") for seg in raw.split("/")):
        raise _refuse(f"{raw!r} looks like a symlink (a listing marker or arrow in the name)")
    if unicodedata.normalize("NFKC", raw) != raw:
        raise _refuse(f"{raw!r} is not NFKC-normal (look-alike characters are refused)")
    if ":" in raw or "~" in raw or "*" in raw or "?" in raw:
        raise _refuse(f"{raw!r} contains a stream, drive, home or glob character")
    if raw != raw.rstrip(". "):
        raise _refuse(f"{raw!r} ends in a dot or space")
    return raw


def classify_path(spec: Any, path: str) -> tuple[str, str | None]:
    """(slot, component) for one path on this surface. `component` is None when the slot is denied.

    Raises InvariantViolation for a path that fails the key rules, or that is not on the surface at all.
    """
    _require_spec(spec)
    rel = _check_name(path)

    slot = _ALWAYS_ON_SLOT.get(rel) if rel in spec.always_on else None
    if slot is None and rel in spec.always_on:
        slot = f"unknown:{rel}"
    if slot is None and spec.hooks_file is not None and rel == spec.hooks_file:
        slot = "c8"
    if slot is None:
        sd, pd = re.escape(spec.skill_dir), re.escape(spec.plugin_dir or "")
        if _SUBAGENT.fullmatch(rel):
            slot = "c5"
        elif re.fullmatch(_SKILL.format(sd=sd), rel):
            slot = "c6"
        elif re.fullmatch(_SKILL_SCRIPT.format(sd=sd), rel) or _SCRIPT.fullmatch(rel):
            slot = "c7"
        elif spec.plugin_dir is not None and re.fullmatch(_PLUGIN.format(pd=pd), rel):
            slot = "c8"
    if slot is None:
        hint = next((n for n in (*spec.always_on, *_ALWAYS_ON_SLOT) if n.lower() == rel.lower() and n != rel), None)
        why = f" (differs only by case from {hint!r})" if hint else ""
        raise _refuse(f"{path!r} is not on the {spec.name} surface (unknown slot, denied by default){why}")

    component = _component_if_mutable(slot)
    if component is not None:
        invariants.assert_path_unprotected(rel)
    return slot, component


def check_surface(spec: Any, *, request: Iterable[str] | None = None,
                  tree: Iterable[str] | Mapping[str, Any] | None = None) -> AllowedSurface:
    """The mutable part of `spec`, or raise InvariantViolation.

    * `spec`: an upstream SurfaceSpec (read by duck typing). A shape it does not recognise is refused.
    * `request`: slot names (`"c1"`..`"c8"`, exact) the optimizer wants to edit. Each must be known,
      present on this surface and mutable, or the whole request is refused. Unknown slots are denied.
    * `tree`: paths (or a mapping keyed by path) the optimizer wants to write. Each must pass the staging
      key rules and `assert_path_unprotected` and classify into a mutable slot, or the whole tree is refused.

    With neither, returns what is mutable and what is denied (and why), without refusing anything.
    """
    _require_spec(spec)
    allowed, denied = _partition(spec)

    if request is not None:
        if isinstance(request, (str, bytes)):
            raise _refuse(f"request must be a collection of slot names, not {type(request).__name__}")
        for slot in request:
            if not isinstance(slot, str) or slot not in SLOTS:
                raise _refuse(f"{slot!r} is an unknown slot (known: {', '.join(SLOTS)}); denied by default")
            if slot in allowed:
                continue
            if slot in denied:
                raise _refuse(f"{slot} is not mutable on {spec.name}: {denied[slot]}")
            if SLOT_COMPONENT.get(slot) is not None and _component_if_mutable(slot) is None:
                raise _refuse(f"{slot} is not mutable: component {SLOT_COMPONENT[slot]!r} is not in MUTABLE")
            raise _refuse(f"{slot} is not on this surface ({spec.name} has no {slot} file); denied")

    if tree is not None:
        if isinstance(tree, (str, bytes)):
            raise _refuse(f"tree must be a collection of paths, not {type(tree).__name__}")
        for path in tree:
            slot, component = classify_path(spec, path)
            if component is None:
                raise _refuse(f"{path!r} is in slot {slot}, which is not mutable: "
                              f"{SLOT_DENIAL.get(slot, 'unknown slot, denied by default')}")

    components = frozenset(c for s in allowed if (c := _component_if_mutable(s)) is not None)
    files = frozenset(w for s, w in allowed.items() if "<" not in w)
    patterns = tuple(sorted(w for w in allowed.values() if "<" in w))
    return AllowedSurface(
        surface=spec.name,
        slots=frozenset(allowed),
        components=components,
        files=files,
        patterns=patterns,
        denied=MappingProxyType(dict(denied)),
    )


# ── Slot table (3b): Frontmatter freezing and mutation validation ──────────────────

MAX_MUTATION_BYTES = 512 * 1024  # 512 KiB per editable file cap

_FRONTMATTER_FENCE = "---"
_SENSITIVE_KEYS = (
    "model:", "tools:", "allowed-tools:", "permissions:", "permission-mode:",
    "agent:", "effort:", "role:",
)
_YAML_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+\s*:\s*\S")


def _to_clean_text(val: str | bytes, label: str) -> str:
    if isinstance(val, bytes):
        if len(val) > MAX_MUTATION_BYTES:
            raise InvariantViolation(
                f"{label} exceeds maximum allowed size ({len(val)} > {MAX_MUTATION_BYTES} bytes)")
        try:
            text = val.decode("utf-8")
        except UnicodeDecodeError as e:
            raise InvariantViolation(f"{label} is not valid UTF-8: {e}") from e
    elif isinstance(val, str):
        raw_bytes = val.encode("utf-8")
        if len(raw_bytes) > MAX_MUTATION_BYTES:
            raise InvariantViolation(
                f"{label} exceeds maximum allowed size ({len(raw_bytes)} > {MAX_MUTATION_BYTES} bytes)")
        text = val
    else:
        raise InvariantViolation(f"{label} must be str or bytes, got {type(val).__name__}")

    if "\x00" in text:
        raise InvariantViolation(f"{label} contains NUL character")

    for c in text:
        if c in ("\n", "\r", "\t"):
            continue
        cat = unicodedata.category(c)
        if cat in ("Cc", "Cf", "Cs"):
            raise InvariantViolation(
                f"{label} contains disallowed control or invisible character U+{ord(c):04X} ({cat})")

    return text


def _extract_leading_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_block_including_fences, remaining_body).

    Leading frontmatter begins on the first line (or after initial blank lines)
    with `---` and ends with a closing `---` or `...` line.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return None, text

    first_non_blank_idx = None
    for idx, line in enumerate(lines):
        if line.strip():
            first_non_blank_idx = idx
            break

    if first_non_blank_idx is None:
        return None, text

    first_line_clean = lines[first_non_blank_idx].rstrip("\r\n")
    if first_line_clean != _FRONTMATTER_FENCE:
        return None, text

    closing_idx = None
    for idx in range(first_non_blank_idx + 1, len(lines)):
        line_clean = lines[idx].rstrip("\r\n")
        if line_clean in (_FRONTMATTER_FENCE, "..."):
            closing_idx = idx
            break

    if closing_idx is None:
        raise InvariantViolation("unclosed frontmatter fence (missing closing '---')")

    frontmatter_block = "".join(lines[:closing_idx + 1])
    body = "".join(lines[closing_idx + 1:])
    return frontmatter_block, body


def _check_no_secondary_frontmatter(body: str) -> None:
    """Reject any second frontmatter-looking block or stray frontmatter keys after `---`."""
    lines = body.splitlines()
    in_code_block = False
    fence_line_idx = None

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        if stripped == _FRONTMATTER_FENCE:
            if fence_line_idx is None:
                fence_line_idx = idx
            else:
                between = lines[fence_line_idx + 1:idx]
                if any(_YAML_KEY_PATTERN.match(l.strip()) for l in between):
                    raise InvariantViolation("mutation body contains a secondary frontmatter-looking block")
                fence_line_idx = idx
            continue

        if fence_line_idx is not None and (idx - fence_line_idx) <= 10:
            lower = stripped.lower()
            if any(lower.startswith(k) for k in _SENSITIVE_KEYS):
                raise InvariantViolation(
                    f"mutation body contains frontmatter key {stripped!r} following a '---' fence")


def check_mutation(slot: str, path: str, old_text: str | bytes, new_text: str | bytes) -> None:
    """Validate that a proposed file mutation is permitted under the slot table and invariants.

    Enforces RFC 004 §6.5:
    1. `slot` must be a known, mutable slot (c1, c3, c4, c5, c6).
    2. `path` must be an unprotected path (refusing guardrails, hooks, git, escapes).
    3. Content must be valid UTF-8, within size limits (512 KiB), free of NUL and control characters.
    4. In c5 (AGENT.md) and c6 (SKILL.md), the leading frontmatter block is FROZEN:
       - If old has frontmatter, new must have an exact byte-identical leading frontmatter block.
       - If old has no frontmatter, new must not introduce one.
       - The body must not introduce a secondary frontmatter-looking block or stray frontmatter keys.
    """
    if not isinstance(slot, str) or slot not in SLOTS:
        raise InvariantViolation(f"{slot!r} is an unknown slot (known: {', '.join(SLOTS)})")
    comp = _component_if_mutable(slot)
    if comp is None:
        raise InvariantViolation(f"slot {slot} is not mutable: {SLOT_DENIAL.get(slot, 'denied by default')}")

    invariants.assert_path_unprotected(path)

    old_clean = _to_clean_text(old_text, "original text")
    new_clean = _to_clean_text(new_text, "new text")

    old_fm, _ = _extract_leading_frontmatter(old_clean)
    new_fm, new_body = _extract_leading_frontmatter(new_clean)

    if old_fm is None:
        if new_fm is not None:
            raise InvariantViolation("mutation introduces a frontmatter block where none existed")
    else:
        if new_fm is None:
            raise InvariantViolation("mutation removed the frozen leading frontmatter block")
        if old_fm.encode("utf-8") != new_fm.encode("utf-8"):
            raise InvariantViolation("leading frontmatter is frozen and must be byte-identical")

    _check_no_secondary_frontmatter(new_body)

