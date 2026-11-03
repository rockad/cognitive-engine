"""Pure policy: protected invariants, probe triggers, egress/ZDR, settings, licensing."""
import tomllib
from pathlib import Path

import pytest

from cognitive_harness2 import egress, invariants, settings, triggers

PKG = Path(__file__).resolve().parents[1]


# ── protected invariants ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(invariants.MUTABLE))
def test_the_four_named_components_are_mutable(name):
    assert invariants.assert_mutable(name) == name


@pytest.mark.parametrize("name", ["egress_policy", "Egress-Policy", " ZDR_policy ", "session_ceiling",
                                  "lifecycle_hook", "human_approval", "guardrail", "verification_gate"])
def test_protected_components_are_refused_however_spelled(name):
    with pytest.raises(invariants.InvariantViolation, match="protected"):
        invariants.assert_mutable(name)


@pytest.mark.parametrize("name", ["", "memory", "model", "sampling", "prompt2", "PreToolUse"])
def test_unknown_components_are_refused_not_allowed(name):
    with pytest.raises(invariants.InvariantViolation, match="not a mutable component"):
        invariants.assert_mutable(name)


@pytest.mark.parametrize("path", [
    "hooks/hooks.json", "./a/../hooks/x.json", "HOOKS/Guard.ts", "adapters/claude/hooks/hooks.json",
    "packages/jev/src/guard.ts", "packages/jev/src/hook.ts", "src/egress_policy.py", "cfg/ZDR.toml",
    "src/core/governance.ts", "x/.claude/settings.json", "a/approval.py", "..\\outside", "/etc/passwd",
    "../../escape", "mcp_config.json",
])
def test_protected_or_escaping_paths_are_refused(path):
    with pytest.raises(invariants.InvariantViolation):
        invariants.assert_path_unprotected(path)


@pytest.mark.parametrize("path", ["docs/prompt.md", "prompts/system.txt", "src/decompose.py"])
def test_ordinary_paths_are_allowed(path):
    assert invariants.assert_path_unprotected(path)


# ── probe triggers ───────────────────────────────────────────────────────────────

def test_failed_verification_triggers():
    assert triggers.should_probe(judge_passed=False).probe
    assert triggers.should_probe(judge_score=0.49).probe


def test_threshold_is_strictly_below_point_five():
    assert not triggers.should_probe(judge_score=0.50).probe
    assert not triggers.should_probe(judge_passed=True, judge_score=0.9).probe


def test_retry_exhaustion_triggers_at_three():
    assert not triggers.should_probe(consecutive_failures=2).probe
    assert triggers.should_probe(consecutive_failures=3).probe


def test_on_demand_always_triggers():
    assert triggers.should_probe(on_demand=True).probe


def test_routing_confidence_never_triggers_and_never_suppresses():
    assert not triggers.should_probe(routing_confidence=0.0).probe
    assert triggers.should_probe(judge_passed=False, routing_confidence=1.0).probe


def test_nothing_known_means_no_probe():
    assert not triggers.should_probe().probe


# ── egress and ZDR ───────────────────────────────────────────────────────────────

LOCAL = "http://127.0.0.1:11434/v1"
OR = "https://openrouter.ai/api/v1"


def test_strict_zdr_allows_a_local_endpoint_even_for_restricted_content():
    assert "local" in egress.check_endpoint(zdr="strict", base_url=LOCAL, model="llama3", classification="restricted")


def test_strict_zdr_refuses_openrouter():
    with pytest.raises(egress.EgressDenied, match="zdr=strict"):
        egress.check_endpoint(zdr="strict", base_url=OR, model="anthropic/claude", classification="open")


def test_strict_zdr_allows_an_explicitly_listed_zero_retention_host():
    ok = egress.check_endpoint(zdr="strict", base_url="https://zdr.example.com/v1", model="m",
                               classification="gated", zdr_endpoints=frozenset({"ZDR.example.com"}))
    assert "listed" in ok


@pytest.mark.parametrize("model", ["openrouter/free", "google/gemma:free"])
def test_free_tier_models_are_refused_under_strict_zdr_even_locally(model):
    with pytest.raises(egress.EgressDenied, match="free-tier"):
        egress.check_endpoint(zdr="strict", base_url=LOCAL, model=model, classification="open")


def test_open_content_may_use_a_paid_remote_model_when_zdr_is_off():
    assert egress.check_endpoint(zdr="off", base_url=OR, model="anthropic/claude", classification="open")


@pytest.mark.parametrize("cls", ["gated", "unstated", "restricted", None, "nonsense"])
def test_anything_not_open_stays_off_remote_hosts(cls):
    with pytest.raises(egress.EgressDenied):
        egress.check_endpoint(zdr="off", base_url=OR, model="anthropic/claude", classification=cls)


def test_free_tier_is_refused_for_non_open_content_even_with_zdr_off():
    with pytest.raises(egress.EgressDenied, match="free-tier"):
        egress.check_endpoint(zdr="off", base_url=OR, model="openrouter/free", classification="gated")


def test_unknown_classification_is_restricted():
    assert egress.effective_classification(None) == "restricted"
    assert egress.effective_classification("unstated") == "restricted"
    assert egress.effective_classification("open") == "open"


def test_a_url_without_a_host_is_refused():
    with pytest.raises(egress.EgressDenied, match="host"):
        egress.check_endpoint(zdr="off", base_url="not-a-url", model="m", classification="open")


# ── settings ─────────────────────────────────────────────────────────────────────

def test_defaults(tmp_path):
    s = settings.from_env({settings.ENV_EXPERIENCE_ROOT: str(tmp_path / "x")})
    assert s.port == 8120 and s.base_url == settings.DEFAULT_BASE_URL and s.model is None


def test_experience_root_inside_the_checkout_is_refused():
    repo = Path(__file__).resolve().parents[3]
    with pytest.raises(settings.SettingsError, match="inside the git checkout"):
        settings.from_env({settings.ENV_EXPERIENCE_ROOT: str(repo / "packages/harness2/state")})


@pytest.mark.parametrize("raw", ["80", "99999", "x"])
def test_bad_ports_are_refused(raw, tmp_path):
    with pytest.raises(settings.SettingsError):
        settings.from_env({settings.ENV_PORT: raw, settings.ENV_EXPERIENCE_ROOT: str(tmp_path)})


def test_zdr_endpoints_are_parsed_lowercased(tmp_path):
    s = settings.from_env({settings.ENV_ZDR_ENDPOINTS: " A.example, b.example ,", settings.ENV_EXPERIENCE_ROOT: str(tmp_path)})
    assert s.zdr_endpoints == {"a.example", "b.example"}


# ── licensing ────────────────────────────────────────────────────────────────────

def test_apache_licence_and_notice_are_present():
    lic = (PKG / "LICENSE").read_text()
    assert "Apache License" in lic and "Version 2.0" in lic
    notice = (PKG / "NOTICE").read_text()
    assert "Apache License, Version 2.0" in notice
    assert 'license = { text = "Apache-2.0" }' in (PKG / "pyproject.toml").read_text()


def test_notice_cites_upstream_as_primary_and_the_literature_as_related_work_only():
    notice = (PKG / "NOTICE").read_text()
    primary, _, related = notice.partition("RELATED WORK")
    # Upstream is primary: Google, Apache-2.0, the paper, and the exact pinned commit.
    for needle in ("github.com/google-research/harness2", "Google Research / Google LLC", "Apache-2.0",
                   "Xu et al., 2026", "arxiv.org/abs/2610.14224"):
        assert needle in primary
    pin = tomllib.loads((PKG / "pyproject.toml").read_text())["tool"]["uv"]["sources"]["harness2"]["rev"]
    assert pin in primary
    # Promptbreeder and Tool Makers are related work only, never in the primary section.
    assert "Promptbreeder" in related and "Tool Makers" in related and "2309.16797" in related
    assert "Promptbreeder" not in primary and "Tool Makers" not in primary
    # Claims this package must not make.
    for banned in ("clean-room", "clean room", "DeepMind"):
        assert banned.lower() not in notice.lower()
