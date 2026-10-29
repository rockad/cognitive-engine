"""Seam 1: upstream `SurfaceSpec` (c1..c8) mapped onto our invariants, default-deny.

The tables use the REAL upstream surfaces (`OPENCODE_SURFACE`, `CODEX_SURFACE`, from the dev
group's pinned upstream) and a hand-built stand-in, so the module's own logic is also tested
without any upstream import.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognitive_harness2 import invariants, upstream_surface
from cognitive_harness2.invariants import InvariantViolation
from cognitive_harness2.upstream_surface import SLOT_COMPONENT, SLOTS, check_surface


@pytest.fixture(scope="module")
def opencode():
    from harness2.substrate.opencode import OPENCODE_SURFACE
    return OPENCODE_SURFACE


@pytest.fixture(scope="module")
def codex():
    from harness2.substrate.codex import CODEX_SURFACE
    return CODEX_SURFACE


def stand_in(**over):
    base = dict(name="stand-in", always_on=("systemprompt.md", "guardrails.md", "LongTermMEMORY.md",
                                            "tool_descriptions/tool_guidance.md"),
                skill_dir="skills", plugin_dir="plugin", hooks_file=None)
    base.update(over)
    return SimpleNamespace(**base)


# ── the slot table ────────────────────────────────────────────────────────────────

def test_slots_are_exactly_c1_to_c8():
    assert SLOTS == ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8")
    assert set(SLOT_COMPONENT) == set(SLOTS)


def test_every_mapped_component_is_one_of_our_four_mutable_components():
    mapped = {c for c in SLOT_COMPONENT.values() if c is not None}
    assert mapped == set(invariants.MUTABLE)  # one slot per mutable component, none invented


def test_the_other_four_slots_are_denied_with_a_reason():
    assert {s for s, c in SLOT_COMPONENT.items() if c is None} == {"c2", "c6", "c7", "c8"}
    assert all(upstream_surface.SLOT_DENIAL[s] for s in ("c2", "c6", "c7", "c8"))


# ── what each real surface may mutate ─────────────────────────────────────────────

def test_opencode_allows_c1_c3_c4_c5(opencode):
    got = check_surface(opencode)
    assert got.slots == {"c1", "c3", "c4", "c5"}
    assert got.components == invariants.MUTABLE
    assert got.surface == "opencode"
    assert set(got.denied) >= {"c2", "c6", "c7", "c8"}


def test_codex_allows_only_c5(codex):
    got = check_surface(codex)
    assert got.slots == {"c5"}
    assert got.components == {"decomposition_strategy"}
    # codex folds c1/c3/c4/c6 into one AGENTS.md, which also carries the guardrails: denied whole
    assert "composite" in got.denied


REQUEST_CASES = [
    ("c1", True), ("c2", False), ("c3", True), ("c4", True),
    ("c5", True), ("c6", False), ("c7", False), ("c8", False),
]


@pytest.mark.parametrize(("slot", "ok"), REQUEST_CASES)
def test_requesting_each_slot_on_opencode(opencode, slot, ok):
    if ok:
        assert slot in check_surface(opencode, request=[slot]).slots
    else:
        with pytest.raises(InvariantViolation):
            check_surface(opencode, request=[slot])


@pytest.mark.parametrize("slot", ["c0", "c9", "c10", "C1", " c1", "c1 ", "", "c", "1", "all", "*", "systemprompt",
                                  "c1,c2", "c١", "ｃ１", None, 1, b"c1"])
def test_unknown_slots_are_denied(opencode, slot):
    with pytest.raises(InvariantViolation, match="unknown slot"):
        check_surface(opencode, request=[slot])


def test_one_bad_slot_in_a_request_refuses_the_whole_request(opencode):
    with pytest.raises(InvariantViolation):
        check_surface(opencode, request=["c1", "c4", "c8"])


def test_a_slot_that_is_mutable_but_absent_from_the_surface_is_denied(codex):
    for slot in ("c1", "c3", "c4"):  # mutable in principle, but codex has no such file
        with pytest.raises(InvariantViolation, match="not on this surface"):
            check_surface(codex, request=[slot])


def test_a_string_is_not_an_iterable_of_slots(opencode):
    with pytest.raises(InvariantViolation):
        check_surface(opencode, request="c1")


# ── paths: the allowed ones, one per mutable slot ─────────────────────────────────

ALLOWED_OPENCODE = [
    ("systemprompt.md", "c1"),
    ("LongTermMEMORY.md", "c3"),
    ("tool_descriptions/tool_guidance.md", "c4"),
    ("sub_agents/helper/AGENT.md", "c5"),
    ("sub_agents/a-b_c9/AGENT.md", "c5"),
]


@pytest.mark.parametrize(("path", "slot"), ALLOWED_OPENCODE)
def test_allowed_paths_classify_to_their_slot(opencode, path, slot):
    assert upstream_surface.classify_path(opencode, path) == (slot, SLOT_COMPONENT[slot])
    assert check_surface(opencode, tree=[path]).slots  # does not raise


def test_a_tree_can_be_a_mapping_or_any_iterable_of_paths(opencode):
    check_surface(opencode, tree={"systemprompt.md": "x", "LongTermMEMORY.md": "y"})
    check_surface(opencode, tree=iter(["systemprompt.md"]))
    check_surface(opencode, tree=("systemprompt.md",))


DENIED_BY_SLOT = [
    ("guardrails.md", "c2"),
    ("skills/review/SKILL.md", "c6"),
    ("skills/review/scripts/run.sh", "c7"),
    ("scripts/helper.py", "c7"),
    ("plugin/gate.ts", "c8"),
]


@pytest.mark.parametrize(("path", "slot"), DENIED_BY_SLOT)
def test_paths_in_a_denied_slot_are_refused_and_name_the_slot(opencode, path, slot):
    assert upstream_surface.classify_path(opencode, path)[1] is None
    with pytest.raises(InvariantViolation, match=slot):
        check_surface(opencode, tree=[path])


@pytest.mark.parametrize(("path", "slot"), [
    ("AGENTS.md", "composite"),
    (".codex/hooks.json", "c8"),
    (".agents/skills/review/SKILL.md", "c6"),
    ("scripts/helper.py", "c7"),
])
def test_denied_paths_on_codex(codex, path, slot):
    with pytest.raises(InvariantViolation):
        check_surface(codex, tree=[path])


def test_subagent_paths_are_allowed_on_codex(codex):
    assert check_surface(codex, tree=["sub_agents/helper/AGENT.md"]).slots == {"c5"}


def test_one_bad_path_refuses_the_whole_tree(opencode):
    with pytest.raises(InvariantViolation):
        check_surface(opencode, tree=["systemprompt.md", "plugin/gate.ts"])


# ── paths: every way of being odd is a refusal ────────────────────────────────────

BAD_PATHS = {
    "empty": "",
    "dot": ".",
    "tree root slash": "/",
    "parent": "..",
    "traversal": "../systemprompt.md",
    "deep traversal": "sub_agents/x/../../../etc/passwd",
    "traversal back to an allowed file": "sub_agents/../systemprompt.md",
    "absolute": "/etc/passwd",
    "absolute allowed name": "/systemprompt.md",
    "windows drive": "C:/systemprompt.md",
    "windows drive backslash": "C:\\systemprompt.md",
    "backslash": "sub_agents\\x\\AGENT.md",
    "dot slash": "./systemprompt.md",
    "double slash": "sub_agents//x/AGENT.md",
    "inner dot": "sub_agents/./x/AGENT.md",
    "trailing slash": "sub_agents/x/AGENT.md/",
    "trailing slash on a file": "systemprompt.md/",
    "space padded": " systemprompt.md",
    "space padded end": "systemprompt.md ",
    "trailing dot": "systemprompt.md.",
    "wrong case": "SYSTEMPROMPT.md",
    "wrong case memory": "longtermmemory.md",
    "wrong case agent file": "sub_agents/x/agent.md",
    "title case": "Systemprompt.md",
    "fullwidth letters": "ｓｙｓｔｅｍｐｒｏｍｐｔ.md",
    "fullwidth subagent": "sub_agents/ｘ/AGENT.md",
    "NUL": "systemprompt.md\x00",
    "newline": "systemprompt.md\n",
    "tab": "system\tprompt.md",
    "zero width": "system\u200bprompt.md",
    "RTL override": "sub_agents/x/AGENT\u202e.md",
    "BOM": "\ufeffsystemprompt.md",
    "symlink arrow": "systemprompt.md -> /etc/passwd",
    "symlink arrow tight": "systemprompt.md->/etc/passwd",
    "symlink marker @": "systemprompt.md@",
    "symlink marker @ dir": "sub_agents@/x/AGENT.md",
    "alternate data stream": "systemprompt.md:stream",
    "tilde home": "~/systemprompt.md",
    "tilde short name": "SYSTEM~1.MD",
    "glob": "sub_agents/*/AGENT.md",
    "protected hooks dir": "hooks/x.md",
    "protected hooks upper": "HOOKS/x.md",
    "protected hooks file": "hooks.json",
    "protected guard dir": "guard/index.ts",
    "protected guard in subagent name": "sub_agents/guard/AGENT.md",
    "protected hook in subagent name": "sub_agents/hook/AGENT.md",
    "protected egress": "sub_agents/egress-policy/AGENT.md",
    "protected zdr": "sub_agents/zdr/AGENT.md",
    "protected approval": "sub_agents/approval-bot/AGENT.md",
    "protected governance": "sub_agents/governance/AGENT.md",
    "protected ceiling": "sub_agents/ceiling/AGENT.md",
    "claude settings": ".claude/settings.json",
    "claude settings local": "sub_agents/x/.claude/settings.local.json",
    "mcp config": ".mcp.json",
    "unknown file": "notes.md",
    "unknown nested": "sub_agents/x/notes.md",
    "subagent name with dot": "sub_agents/x.y/AGENT.md",
    "subagent name uppercase": "sub_agents/X/AGENT.md",
    "subagent name with space": "sub_agents/x y/AGENT.md",
    "subagent name leading dash": "sub_agents/-x/AGENT.md",
    "subagent two levels": "sub_agents/a/b/AGENT.md",
    "subagent dir only": "sub_agents/x",
    "subagent readme stub": "sub_agents/README.md",
    "scripts readme stub": "scripts/README.md",
    "git dir": ".git/config",
    "env file": ".env",
}


@pytest.mark.parametrize("path", BAD_PATHS.values(), ids=BAD_PATHS.keys())
def test_odd_paths_are_refused(opencode, path):
    with pytest.raises(InvariantViolation):
        check_surface(opencode, tree=[path])


@pytest.mark.parametrize("bad", [None, 3, b"systemprompt.md", ("systemprompt.md",), 1.5])
def test_a_non_string_path_is_refused(opencode, bad):
    with pytest.raises(InvariantViolation):
        check_surface(opencode, tree=[bad])


def test_a_case_variant_says_so(opencode):
    with pytest.raises(InvariantViolation, match="case"):
        check_surface(opencode, tree=["SystemPrompt.md"])


def test_a_stored_symlink_name_is_refused_even_if_the_tail_is_an_allowed_file(opencode):
    with pytest.raises(InvariantViolation, match="symlink"):
        check_surface(opencode, tree=["sub_agents/x/AGENT.md -> systemprompt.md"])


# ── default-deny against a surface the module has never seen ──────────────────────

def test_an_unrecognised_always_on_file_is_denied_not_allowed():
    spec = stand_in(always_on=("systemprompt.md", "brand_new.md"))
    got = check_surface(spec)
    assert got.slots == {"c1", "c5"}
    assert any("brand_new.md" in why for why in got.denied.values())
    with pytest.raises(InvariantViolation):
        check_surface(spec, tree=["brand_new.md"])


def test_a_hooks_file_is_c8_and_denied():
    spec = stand_in(hooks_file="config/lifecycle.json")  # a name the path rules do not already catch
    assert check_surface(spec).denied["c8"].startswith("config/lifecycle.json")
    with pytest.raises(InvariantViolation, match="c8"):
        check_surface(spec, tree=["config/lifecycle.json"])
    with pytest.raises(InvariantViolation):
        check_surface(spec, request=["c8"])


def test_the_skill_dir_comes_from_the_spec():
    spec = stand_in(skill_dir=".agents/skills")
    with pytest.raises(InvariantViolation, match="c6"):
        check_surface(spec, tree=[".agents/skills/x/SKILL.md"])
    with pytest.raises(InvariantViolation, match="unknown slot"):
        check_surface(spec, tree=["skills/x/SKILL.md"])  # not THIS surface's skill dir


@pytest.mark.parametrize("spec", [
    None,
    object(),
    SimpleNamespace(),
    SimpleNamespace(name="x"),
    SimpleNamespace(name="x", always_on="systemprompt.md", skill_dir="s", plugin_dir=None, hooks_file=None),
    SimpleNamespace(name="x", always_on=(1,), skill_dir="s", plugin_dir=None, hooks_file=None),
    SimpleNamespace(name="x", always_on=(), skill_dir=None, plugin_dir=None, hooks_file=None),
    SimpleNamespace(name="x", always_on=(), skill_dir="s", plugin_dir=3, hooks_file=None),
    {"name": "x"},
])
def test_an_unrecognised_spec_shape_is_refused(spec):
    with pytest.raises(InvariantViolation, match="surface spec"):
        check_surface(spec)


def test_editing_mutable_propagates(monkeypatch, opencode):
    """The slot table never widens `MUTABLE`: removing a component from it removes the slot."""
    monkeypatch.setattr(invariants, "MUTABLE", frozenset(invariants.MUTABLE - {"prompt"}))
    got = check_surface(opencode)
    assert "c1" not in got.slots
    with pytest.raises(InvariantViolation):
        check_surface(opencode, request=["c1"])
    with pytest.raises(InvariantViolation):
        check_surface(opencode, tree=["systemprompt.md"])


def test_the_result_is_immutable(opencode):
    got = check_surface(opencode)
    with pytest.raises(AttributeError):
        got.slots = frozenset({"c8"})  # type: ignore[misc]
    with pytest.raises(TypeError):
        got.denied["c1"] = "x"  # type: ignore[index]
