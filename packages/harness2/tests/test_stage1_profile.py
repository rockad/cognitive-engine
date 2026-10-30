"""The committed stage-1 channel profile is valid under the REAL validator and says what stage 1 is."""
from __future__ import annotations

from pathlib import Path

from cognitive_harness2 import channels

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "harness2-channels.stage1.toml"


def load() -> channels.ChannelProfile:
    return channels.parse_text(EXAMPLE.read_text(encoding="utf-8"), source=str(EXAMPLE))


def test_the_stage_one_profile_is_valid_and_declares_exactly_one_local_channel():
    p = load()
    assert p.declared and set(p.channels) == {"local-ollama"}
    c = p.channels["local-ollama"]
    assert (c.kind, c.provider, c.model) == ("opencode", "ollama", "ollama/gemma4:12b")
    assert c.endpoints == ("http://127.0.0.1:11434",)          # canonical scheme://host:port
    assert c.retention == "local" and c.max_classification == "restricted" and c.approval == "per-sweep"
    assert c.caps.to_dict() == {"sessions": 7, "wall_s": 2700.0, "cost_usd": 0.0}
    assert p.workspace_roots and all(r.startswith("/") for r in p.workspace_roots)
    assert not p.warnings and not c.warnings


def test_the_stage_one_channel_is_allowed_for_the_synthetic_task_under_strict_zdr():
    c = load().channels["local-ollama"]
    for cls in ("open", "gated", "restricted"):
        assert channels.decide(c, zdr="strict", classification=cls).allowed
