"""Which runtime model sessions use. Docker is the default and is chosen without consulting sbx at all; the sbx route needs
COGNITIVE_HARNESS2_MODEL_SANDBOX=sbx written out AND a verified, usable sbx, and when it was asked for and is not usable
the answer is a refusal with reasons, never Docker. Probes are injected: no process, no daemon."""
from __future__ import annotations

import pytest

from cognitive_harness2 import sandbox, sbx_session as ss

VERIFIED = frozenset({"0.50.1"})
OFF = {"ssh.agentForwardingEnabled": False, "browser.enabled": False}


def boom(*_a, **_k):
    raise AssertionError("sbx must not be consulted")


def probes(*, kvm=None, daemon=True, version="0.50.1", policy=None, settings=OFF) -> sandbox.SbxProbes:
    return sandbox.SbxProbes(lambda: kvm, lambda: daemon, lambda _p: version, lambda _p: policy, lambda _p: dict(settings))


NEVER = sandbox.SbxProbes(boom, boom, boom, boom, boom)


def choose(environ, *, which=lambda _n: "/usr/bin/sbx", pr=None, verified=VERIFIED):
    return ss.choose_model_runtime(environ, which=which, probes=pr if pr is not None else probes(), verified=verified)


SBX = {ss.ENV_MODEL_SANDBOX: "sbx"}


@pytest.mark.parametrize("environ", [{}, {ss.ENV_MODEL_SANDBOX: ""}, {ss.ENV_MODEL_SANDBOX: "docker"},
                                     {"COGNITIVE_HARNESS2_SANDBOX": "sbx"}])
def test_docker_is_the_default_and_sbx_is_not_consulted(environ):
    rt = ss.choose_model_runtime(environ, which=boom, probes=NEVER, verified=VERIFIED)
    assert (rt.kind, rt.ok, rt.reasons, rt.sbx_path) == ("docker", True, (), None)
    assert "rootless Docker" in rt.line and "model sessions run in" in rt.line


def test_explicit_sbx_with_a_verified_usable_sbx_is_selected():
    rt = choose(SBX)
    assert (rt.kind, rt.ok, rt.version, rt.sbx_path, rt.reasons) == ("sbx", True, "0.50.1", "/usr/bin/sbx", ())
    assert "sbx microVM" in rt.line and "0.50.1" in rt.line and "COGNITIVE_HARNESS2_MODEL_SANDBOX=sbx" in rt.line


REFUSALS = [
    pytest.param(dict(which=lambda _n: None), "not-installed", id="sbx is not installed"),
    pytest.param(dict(pr=probes(daemon=False)), "daemon-down", id="the daemon is down"),
    pytest.param(dict(pr=probes(kvm="/dev/kvm: permission denied")), "kvm-unusable", id="no kvm"),
    pytest.param(dict(pr=probes(version="0.51.0")), "unverified-version", id="a version nobody verified"),
    pytest.param(dict(verified=frozenset()), "no-verified-version", id="nothing is verified yet"),
    pytest.param(dict(pr=probes(policy="a network allow rule exists ('x'): not deny-all")), "policy-not-deny-all",
                 id="a global allow rule"),
    pytest.param(dict(pr=probes(settings={**OFF, "browser.enabled": True})), "unsafe-setting", id="browser.enabled is on"),
    pytest.param(dict(pr=probes(settings={**OFF, "ssh.agentForwardingEnabled": True})), "unsafe-setting",
                 id="ssh agent forwarding is on"),
]


@pytest.mark.parametrize("kw, code", REFUSALS)
def test_explicit_sbx_that_is_not_usable_is_a_refusal_with_reasons_never_docker(kw, code):
    rt = choose(SBX, **kw)
    assert rt.ok is False and rt.kind == "sbx"
    assert code in rt.codes and rt.reasons
    assert "REFUSED" in rt.line and "no fallback to Docker" in rt.line
    for reason in rt.reasons:
        assert reason in rt.line


@pytest.mark.parametrize("value", ["auto", "1", "true", "sbx,docker", "microvm", "sbxx"])
def test_a_value_that_is_not_docker_or_sbx_is_refused_not_defaulted(value):
    rt = ss.choose_model_runtime({ss.ENV_MODEL_SANDBOX: value}, which=boom, probes=NEVER, verified=VERIFIED)
    assert rt.ok is False and rt.kind == "refused" and "not recognised" in " ".join(rt.reasons)
    assert "REFUSED" in rt.line


def test_the_generic_sandbox_setting_neither_selects_nor_blocks_the_model_route():
    rt = choose({**SBX, "COGNITIVE_HARNESS2_SANDBOX": "docker"})
    assert rt.ok and rt.kind == "sbx"


def test_no_version_is_verified_for_the_model_route_by_default_so_a_plain_call_refuses():
    """Until the live test has passed on a version, that version is not in `VERIFIED_SBX_MODEL_VERSIONS`; with it empty a
    call that does not inject a verified set must refuse, before any sbx process is asked."""
    rt = ss.choose_model_runtime(SBX, which=lambda _n: "/usr/bin/sbx", probes=NEVER, verified=frozenset())
    assert rt.ok is False and "no-verified-version" in rt.codes


def test_the_model_policy_probe_refuses_a_policy_that_is_not_initialised(monkeypatch):
    """`sbx policy ls` on a fresh install says the policy has not been initialised: that is a reason, not a crash."""
    class P:
        returncode, stdout, stderr = 1, "", "error: network policy has not been initialized"

    monkeypatch.setattr(sandbox, "_run_readonly", lambda *a, **k: P())
    pr = ss.model_probes({})
    assert "not initialised" in pr.policy_problem("/usr/bin/sbx")
