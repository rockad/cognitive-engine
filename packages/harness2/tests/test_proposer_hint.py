"""The proposer's ENVIRONMENT RULES block: derived from the slot table, prepended for proposer sessions only, audited.

Real runs (2026-11-01, hard set): the INV proposer wrote the answer into a script (slot c7, refused twice) and the qwen
proposers made no edit at all. The block tells a proposer which harness files are kept and which are discarded. Its file
lists come from the slot table and the surface in code, so these tests hold the TEXT to the TABLE: whatever the block names
as mutable must classify as mutable, and whatever it names as read-only must classify as denied.

No Docker, no model: a proposer's `_contained` is replaced by a recorder.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from h2_upstream import isolated_upstream

from cognitive_harness2 import channels, proposer_hint as ph, sandbox, stage1, upstream_surface as us
from cognitive_harness2.contained_proposer import SessionConfig, make_proposer, make_selector_prop
from cognitive_harness2.image import ImageRef
from cognitive_harness2.invariants import InvariantViolation

VERIFIED = sandbox.VerifiedDocker("/usr/bin/docker", "unix:///run/user/1000/docker.sock")
IMAGE = ImageRef("cognitive-harness2-proposer:0123456789ab", "sha256:" + "5" * 64)


@pytest.fixture(scope="module")
def up(tmp_path_factory):
    with isolated_upstream(tmp_path_factory.mktemp("runs")) as u:
        yield u


@pytest.fixture(scope="module")
def surface():
    from harness2.substrate.opencode import OPENCODE_SURFACE
    return OPENCODE_SURFACE


def spec(**kw):
    """A SurfaceSpec by duck typing (the slot table reads it that way)."""
    base = dict(name="demo", always_on=("systemprompt.md", "guardrails.md"), skill_dir="skills", plugin_dir="plugin",
                hooks_file=None)
    return SimpleNamespace(**{**base, **kw})


def concrete(where: str) -> str:
    return where.replace("<name>", "demo")


# ══ the lists come from the slot table, not from text ═════════════════════════════════

def test_every_file_named_as_mutable_classifies_as_mutable_and_every_read_only_one_as_denied(surface):
    hint = ph.build_hint(surface)
    assert hint.mutable and hint.readonly
    for where in hint.mutable:
        slot, component = us.classify_path(surface, concrete(where))
        assert component is not None, (where, slot)
    for where in hint.readonly:
        try:
            slot, component = us.classify_path(surface, concrete(where))
        except InvariantViolation:                      # not on the surface at all is denied too
            continue
        assert component is None, (where, slot)


def test_the_opencode_surface_gives_the_four_mutable_paths_and_the_three_denied_ones(surface):
    hint = ph.build_hint(surface)
    assert set(hint.mutable) == {"systemprompt.md", "LongTermMEMORY.md", "tool_descriptions/tool_guidance.md",
                                 "sub_agents/<name>/AGENT.md", "skills/<name>/SKILL.md"}
    assert set(hint.readonly) == {"guardrails.md", "scripts/<name>.py", "plugin/<name>.ts"}


def test_the_text_names_each_listed_path_under_the_harness_directory_and_no_other_file(surface):
    hint = ph.build_hint(surface)
    for where in (*hint.mutable, *hint.readonly):
        assert f"{ph.HARNESS_DIR}/{where}" in hint.text, where
    named = set(re.findall(r"context/harness/(\S+)", hint.text))
    assert named == {*hint.mutable, *hint.readonly}


def test_the_lists_follow_the_surface_so_a_changed_surface_changes_the_block():
    default = ph.build_hint(spec())
    assert "skills/<name>/SKILL.md" in default.mutable and "plugin/<name>.ts" in default.readonly
    other_spec = spec(skill_dir="playbooks", plugin_dir=None, always_on=("systemprompt.md", "guardrails.md", "AGENTS.md"))
    other = ph.build_hint(other_spec)
    assert "playbooks/<name>/SKILL.md" in other.mutable and "skills/<name>/SKILL.md" not in other.mutable
    assert "AGENTS.md" in other.readonly                      # codex's composite file is denied whole
    assert not any(w.startswith("plugin/") for w in other.readonly)
    assert other.sha != default.sha and other.text != default.text
    assert us.classify_path(other_spec, "AGENTS.md")[1] is None
    assert us.classify_path(other_spec, "playbooks/demo/SKILL.md")[1] is not None


def test_a_slot_that_leaves_MUTABLE_leaves_the_block(surface, monkeypatch):
    from cognitive_harness2 import invariants
    monkeypatch.setattr(invariants, "MUTABLE", invariants.MUTABLE - {"tool_description"})
    hint = ph.build_hint(surface)
    assert "tool_descriptions/tool_guidance.md" not in hint.mutable and "tool_descriptions/tool_guidance.md" in hint.readonly
    assert "tool_descriptions/tool_guidance.md" in hint.text.split("Read-only")[1]


def test_an_unrecognised_surface_is_refused_not_guessed():
    with pytest.raises(InvariantViolation):
        ph.build_hint(SimpleNamespace(name="x"))


def test_the_harness_directory_is_the_one_upstream_harvests_from(up):
    from harness2.core import recursion
    assert f'harvest="{ph.HARNESS_DIR}"' in inspect.getsource(recursion._run_stage)


# ══ the block itself ══════════════════════════════════════════════════════════════════

def test_the_block_is_short_names_the_rule_and_says_the_whole_proposal_is_discarded(surface):
    hint = ph.build_hint(surface)
    assert hint.text.startswith("ENVIRONMENT RULES") and len(hint.text) < 1600 and hint.text.endswith("\n")
    low = hint.text.lower()
    for needle in ("added by the runner", "read-only", "whole proposal discarded", "scripts", "plugins", "guardrails.md",
                   "answer", "frontmatter"):
        assert needle in low, needle
    assert "body only" in low                       # the c5/c6 templates keep their frontmatter byte-identical


def test_the_block_names_no_task_content_and_no_secret(surface):
    low = ph.build_hint(surface).text.lower()
    for needle in ("sum-signed", "household", "parenthes", "total:", "token", "password", "ollama"):
        assert needle not in low, needle


def test_the_hash_is_a_short_stable_digest_of_the_exact_text(surface):
    a, b = ph.build_hint(surface), ph.build_hint(surface)
    assert a == b and re.fullmatch(r"[0-9a-f]{12}", a.sha)
    import hashlib
    assert a.sha == hashlib.sha256(a.text.encode("utf-8")).hexdigest()[:12]


# ══ prepended for proposer sessions only, audited ═════════════════════════════════════

def make_session(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    cli = tmp_path / "opencode"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    raw = {"workspace_roots": [str(scratch.resolve())],
           "channels": {"local": dict(kind="opencode", provider="ollama", endpoint="http://127.0.0.1:11434/v1",
                                      model="ollama/qwen3:30b", retention="local", max_classification="restricted",
                                      approval="per-sweep", caps=dict(sessions=50, wall_s=600, cost_usd=0))}}
    return SessionConfig(verified=VERIFIED, image=IMAGE, profile=channels.validate(raw, source="test"), channel="local",
                         zdr="strict", classification="open", scratch_root=str(scratch.resolve()), cli_paths={"opencode": str(cli)},
                         timeout_s=120)


class Recorder:
    def __init__(self):
        self.systems: list[str] = []

    def __call__(self, cwd, mission, system, wall):
        self.systems.append(system)
        return "an answer", 0.0, ""


def audit_file(d: Path, label: str, kind: str) -> Path:
    """`<label>.<cache key>.<kind>`: upstream puts the cache key into every audit file name."""
    found = sorted(d.glob(f"{label}.*.{kind}"))
    assert len(found) == 1, (label, kind, sorted(p.name for p in d.iterdir()))
    return found[0]


def run_call(up, tmp_path, hint, *, system="UPSTREAM SYSTEM", label="inv1.a1", factory=make_proposer):
    prop = factory(up, make_session(tmp_path), audit_dir=tmp_path / "audit", **({"hint": hint} if hint is not None else {}))
    rec = Recorder()
    prop._contained = rec
    ctx = tmp_path / "ctx"
    (ctx / "harness").mkdir(parents=True, exist_ok=True)
    prop.call("MISSION TEXT", ctx, label, system=system, harvest="context/harness")
    return prop, rec


def test_a_hinted_proposer_gets_the_block_in_front_of_upstreams_system_text(up, tmp_path, surface):
    hint = ph.build_hint(surface)
    _prop, rec = run_call(up, tmp_path, hint)
    assert rec.systems == [hint.text + "\n" + "UPSTREAM SYSTEM"]
    assert rec.systems[0].startswith("ENVIRONMENT RULES")


def test_the_block_goes_in_even_when_upstream_sends_no_system_text(up, tmp_path, surface):
    hint = ph.build_hint(surface)
    _prop, rec = run_call(up, tmp_path, hint, system="")
    assert rec.systems == [hint.text]


def test_without_a_hint_the_system_text_is_upstreams_unchanged(up, tmp_path):
    _prop, rec = run_call(up, tmp_path, None)
    assert rec.systems == ["UPSTREAM SYSTEM"]


def test_the_audit_records_the_block_verbatim_its_hash_and_hint_true(up, tmp_path, surface):
    hint = ph.build_hint(surface)
    run_call(up, tmp_path, hint)
    meta = json.loads(audit_file(tmp_path / "audit", "inv1.a1", "meta.json").read_text())
    assert meta["hint"] is True and meta["hint_sha"] == hint.sha and meta["hint_block"] == hint.text
    assert audit_file(tmp_path / "audit", "inv1.a1", "system.md").read_text() == hint.text + "\nUPSTREAM SYSTEM"   # what the session was given
    assert audit_file(tmp_path / "audit", "inv1.a1", "mission.md").read_text() == "MISSION TEXT"                  # the mission is untouched


def test_the_audit_says_hint_false_and_holds_no_block_when_it_is_off(up, tmp_path):
    run_call(up, tmp_path, None)
    meta = json.loads(audit_file(tmp_path / "audit", "inv1.a1", "meta.json").read_text())
    assert meta["hint"] is False and "hint_sha" not in meta and "hint_block" not in meta
    assert audit_file(tmp_path / "audit", "inv1.a1", "system.md").read_text() == "UPSTREAM SYSTEM"
    assert meta["label"] == "inv1.a1" and meta["ok"] is True                     # upstream's own fields are still there


def test_the_selector_proposer_never_gets_the_block(up, tmp_path, surface):
    prop = make_proposer(up, make_session(tmp_path), audit_dir=tmp_path / "audit", hint=ph.build_hint(surface))
    sel = make_selector_prop(prop)
    assert prop.hint_block and sel.hint_block == ""
    rec = Recorder()
    sel._contained = rec
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    sel.call("pick one", ctx, "select.agg1", system="SELECTOR SYSTEM")
    assert rec.systems == ["SELECTOR SYSTEM"]
    meta = json.loads(audit_file(tmp_path / "audit" / "selector", "select.agg1", "meta.json").read_text())
    assert meta["hint"] is False


def test_a_cached_reply_is_not_served_across_a_hint_change(up, tmp_path, surface):
    """upstream caches on (label, mission, system, context): the block is part of `system`, so flipping the flag re-asks."""
    run_call(up, tmp_path, None)
    prop, rec = run_call(up, tmp_path, ph.build_hint(surface))
    assert len(rec.systems) == 1 and rec.systems[0].startswith("ENVIRONMENT RULES")


# ══ the stage-1 flag ══════════════════════════════════════════════════════════════════

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "harness2-channels.stage1.toml"


def profile(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    f = tmp_path / "channels.toml"
    f.write_text(EXAMPLE.read_text().replace("/home/rockad/h2-workspaces", str(ws)))
    return channels.load_profile({stage1.ENV_CHANNELS: str(f)})


@pytest.mark.parametrize("cmd", ["plan", "run", "selftest"])
def test_the_flag_takes_on_or_off_and_has_no_value_of_its_own_by_default(cmd, capsys):
    assert stage1.build_parser().parse_args([cmd]).proposer_hint is None
    assert stage1.build_parser().parse_args([cmd, "--proposer-hint", "on"]).proposer_hint == "on"
    assert stage1.build_parser().parse_args([cmd, "--proposer-hint", "off"]).proposer_hint == "off"
    with pytest.raises(SystemExit):
        stage1.build_parser().parse_args([cmd, "--proposer-hint", "maybe"])
    assert "invalid choice" in capsys.readouterr().err


def test_the_default_is_on_for_the_hard_set_and_off_for_the_easy_one_and_an_explicit_value_wins(tmp_path):
    p = profile(tmp_path)
    assert stage1.Settings(profile=p).hint_on is False
    assert stage1.Settings(profile=p, task_set="hard").hint_on is True
    assert stage1.Settings(profile=p, task_set="hard", proposer_hint="off").hint_on is False
    assert stage1.Settings(profile=p, proposer_hint="on").hint_on is True
    assert stage1.Settings(profile=p, task_set="hard").hint_source == "default for the hard set"
    assert stage1.Settings(profile=p).hint_source == "default for the easy set"
    assert stage1.Settings(profile=p, proposer_hint="off").hint_source == "--proposer-hint"
    with pytest.raises(stage1.Stage1Error):
        stage1.Settings(profile=p, proposer_hint="yes")
