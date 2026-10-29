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
    c5    sub_agents/<name>/AGENT.md                 decomposition_strategy
    c6    skills/<name>/SKILL.md                     (undecided: denied)
    c7    scripts/<name>.py, <skill>/scripts/*       (executable code: denied)
    c8    plugin/<name>.ts, hooks_file               (lifecycle hooks: protected)

⚠️ Upstream is inconsistent about numbering: its README orders the components system prompt,
guardrails, memory, tool guidance, subagents, skills, scripts, plugins (so guardrails is c2),
while its prompt tables label `guardrails.md` as c6 and have no c2. This module binds to the
PATHS, which agree everywhere, and uses the README numbering. Codex folds c1/c3/c4 and the
guardrails into one `AGENTS.md`; it cannot be split, so it is denied whole.

The mapping c3 -> context_assembly, c5 -> decomposition_strategy and c6 -> denied is a policy
choice RFC 004 does not spell out. It lives in `SLOT_COMPONENT` as data: widening it is a
deliberate edit there, never an inference. `invariants.assert_mutable` is consulted on every
call, so removing a component from `MUTABLE` removes its slot.

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
from .sandbox import _stage_name

SLOTS = ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8")

# slot -> our component. `None` is a slot we deny. Edit deliberately.
SLOT_COMPONENT: Mapping[str, str | None] = MappingProxyType({
    "c1": "prompt",
    "c2": None,
    "c3": "context_assembly",
    "c4": "tool_description",
    "c5": "decomposition_strategy",
    "c6": None,
    "c7": None,
    "c8": None,
})

SLOT_DENIAL: Mapping[str, str] = MappingProxyType({
    "c2": "guardrails are a protected component (invariants.PROTECTED_COMPONENTS)",
    "c6": "skills are not placed in any of the four mutable components by RFC 004 (undecided, denied)",
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
    if "->" in raw or raw.endswith(("@", "|", "=")) or any(seg.endswith("@") for seg in raw.split("/")):
        raise _refuse(f"{raw!r} looks like a symlink (a listing marker or arrow in the name)")
    try:
        _stage_name(raw)  # clean relative path, no control chars, and not a protected path
    except ValueError as e:
        raise _refuse(str(e)) from e
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
    invariants.assert_path_unprotected(rel)

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
    return slot, _component_if_mutable(slot)


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
