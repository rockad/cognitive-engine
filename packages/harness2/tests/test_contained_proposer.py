"""`ContainedProposer`: upstream's Proposer bound to contained sessions, driven by FAKE CLIs on the REAL rootless daemon.

No model is called and no real CLI runs: tests/fixtures/fake-proposer-cli plays opencode, claude and agy (it is
installed under each name) and does what `FAKE:<word>` directives in the mission tell it to. The pure parts run
without a daemon; the session tests do not skip (a daemon that is not rootless fails them, which is the signal).
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from h2_upstream import isolated_upstream

from cognitive_harness2 import channels, contained_proposer as cp, model_gate, session_runner as sr, upstream_egress
from cognitive_harness2.contained_proposer import (ContainedProposerError, SessionConfig, build_command, make_proposer,
                                                    make_selector_prop, session_env)

FIX = Path(__file__).resolve().parent / "fixtures"
FAKE = FIX / "fake-proposer-cli"
CANARY = "CANARY-do-not-leak-90210"
CAPS = dict(sessions=50, wall_s=600, cost_usd=1)

CHANNELS = {
    "local": dict(kind="opencode", provider="ollama", endpoint="http://127.0.0.1:11434", model="ollama/qwen3:30b",
                  retention="local", max_classification="restricted"),
    "claude": dict(kind="claude_cli", provider="anthropic", endpoints=["https://api.anthropic.com"], model="claude-x",
                   retention="unknown", max_classification="open"),
    "agy": dict(kind="agy_cli", provider="google", endpoints=["https://daily-cloudcode-pa.googleapis.com"],
                model="gemini-3-pro", retention="unknown", max_classification="open"),
}
RUN = {"local": ("strict", "restricted"), "claude": ("off", "open"), "agy": ("off", "open")}
KIND = {"local": "opencode", "claude": "claude_cli", "agy": "agy_cli"}
CLI = {"opencode": "opencode", "claude_cli": "claude", "agy_cli": "agy"}


@pytest.fixture(scope="module")
def up(tmp_path_factory):
    with isolated_upstream(tmp_path_factory.mktemp("runs")) as u:
        yield u


def docker(verified, *args) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("DOCKER_")}
    return subprocess.run([verified.path, "-H", verified.host, *args], capture_output=True, text=True, timeout=60, env=env)


def leftovers(verified) -> set[str]:
    out = docker(verified, "ps", "-a", "-q", "--filter", f"label={sr.LABEL}").stdout.split()
    out += docker(verified, "network", "ls", "-q", "--filter", f"label={sr.LABEL}").stdout.split()
    base = sr._runtime_base_dir()
    out += [n for n in os.listdir(base) if n.startswith(("h2s-", "h2bridge-"))]
    return set(out)


class World:
    def __init__(self, tmp_path, verified, image, up):
        self.up, self.verified, self.image, self.tmp = up, verified, image, tmp_path
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir()
        self.scratch = Path(os.path.realpath(self.scratch))
        self.bins = tmp_path / "bin"
        self.bins.mkdir()
        for name in ("opencode", "claude", "agy"):
            shutil.copyfile(FAKE, self.bins / name)
            (self.bins / name).chmod(0o755)
        raw = {"workspace_roots": [str(self.scratch)],
               "channels": {n: {**body, "approval": "per-sweep", "caps": dict(CAPS)} for n, body in CHANNELS.items()}}
        self.profile = channels.validate(raw, source="test")
        self.audit = tmp_path / "audit"
        self.context = tmp_path / "context"
        (self.context / "harness").mkdir(parents=True)
        (self.context / "harness" / "seed.md").write_text("SEED-CONTENT")

    def session(self, name="local", **kw) -> SessionConfig:
        zdr, cls = RUN.get(name, ("off", "open"))
        base = dict(verified=self.verified, image=self.image, profile=self.profile, channel=name, zdr=zdr,
                    classification=cls, scratch_root=str(self.scratch),
                    cli_paths={k: str(self.bins / v) for k, v in CLI.items()}, timeout_s=120)
        base.update(kw)
        return SessionConfig(**base)

    def prop(self, name="local", *, session_kw=None, **kw):
        return make_proposer(self.up, self.session(name, **(session_kw or {})), audit_dir=self.audit, **kw)

    def call(self, prop, directives, label="t.a1", **kw):
        return prop.call(f"Do the work.\n{directives}\n", self.context, label, harvest="context/harness", **kw)


@pytest.fixture
def world(tmp_path, verified, proposer_image, up):
    before = leftovers(verified)
    w = World(tmp_path, verified, proposer_image, up)
    yield w
    # Other agents may run sessions on the same daemon at the same time: anything NEW must go away within a while
    # (a leftover of ours never does); things that disappeared are theirs finishing.
    deadline = time.monotonic() + 30
    new = leftovers(verified) - before
    while new and time.monotonic() < deadline:
        time.sleep(1)
        new = leftovers(verified) - before
    assert not new, f"a session left a container, network or runtime directory behind: {sorted(new)}"
    assert not any(w.scratch.iterdir()), "a scratch workspace was left behind"


@pytest.fixture
def pure(tmp_path, verified, proposer_image, up):
    """A World for tests that never start a session (no leftovers check needed beyond the scratch)."""
    return World(tmp_path, verified, proposer_image, up)


def harvested(reply, name):
    return (Path(reply.workspace) / name).read_text()


# ── the argv this class builds passes the gate, for each kind ─────────────────────

@pytest.mark.parametrize("name", ["local", "claude", "agy"])
def test_the_argv_built_for_each_kind_passes_check_proposer(pure, name):
    zdr, cls = RUN[name]
    ch = pure.profile.channels[name]
    ws = pure.scratch / "w"
    ws.mkdir()
    cli = "/opt/cli/" + CLI[ch.kind]
    argv = build_command(ch.kind, cli_container=cli, model=ch.model, effort="high", max_budget_usd=2)
    env = session_env(ch.kind, model=ch.model, base_url="http://harness2-proxy:11434/v1")
    d = upstream_egress.check_proposer(
        upstream_egress.ProposerConfig(kind=ch.kind, model=ch.model, base_url=ch.endpoints[0], env=env, cwd=str(ws),
                                       command=argv),
        profile=pure.profile, channel=name, zdr=zdr, classification=cls)
    assert d.allowed, d.reasons
    shape = upstream_egress.COMMAND_SHAPES[ch.kind]
    assert argv[0] == cli and set(a.split("=", 1)[0] for a in argv[1:] if a.startswith("-")) <= {*shape.flags, *shape.joined}


@pytest.mark.parametrize("name", ["local", "claude", "agy"])
def test_the_argv_carries_no_endpoint_flag_and_no_credential(pure, name):
    ch = pure.profile.channels[name]
    argv = build_command(ch.kind, cli_container="/opt/cli/x", model=ch.model)
    text = " ".join(argv).lower()
    for word in ("proxy", "base-url", "api-base", "endpoint", "http://", "https://", "token", "key"):
        assert word not in text


# ── env(): nothing inherited, nothing Vertex ──────────────────────────────────────

@pytest.mark.parametrize("name", ["local", "claude", "agy"])
def test_env_never_reads_the_process_environment_and_forces_no_vertex(pure, monkeypatch, name):
    for k, v in (("ANTHROPIC_API_KEY", CANARY), ("OPENAI_API_KEY", CANARY), ("GOOGLE_API_KEY", CANARY),
                 ("CLAUDE_CODE_USE_VERTEX", "1"), ("GOOGLE_VERTEX_PROJECT", "p"), ("GOOGLE_CLOUD_PROJECT", "p"),
                 ("VERTEXAI_LOCATION", "global"), ("ANTHROPIC_VERTEX_PROJECT_ID", "p"), ("CLOUD_ML_REGION", "global"),
                 ("HOME", "/home/someone"), ("GITHUB_TOKEN", CANARY)):
        monkeypatch.setenv(k, v)
    e = pure.prop(name).env()
    text = json.dumps(e)
    assert CANARY not in text and "/home/someone" not in text
    assert not any(k.startswith(("GOOGLE_VERTEX_", "VERTEXAI_", "ANTHROPIC_VERTEX_")) or k in ("CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION",
                                                                                              "GOOGLE_CLOUD_PROJECT")
                   for k in e), e
    assert not (set(e) & {"HOME", "PATH"})                                         # the runner sets those
    for k in e:
        assert not upstream_egress._is_secret_name(k), k
    sr._check_env(e, [])                                                              # the runner accepts every name and value


def test_the_opencode_env_points_the_provider_at_the_runner_endpoint_only(pure):
    e = pure.prop("local").env()
    cfg = json.loads(e["OPENCODE_CONFIG_CONTENT"])
    (prov, body), = cfg["provider"].items()
    assert prov == "ollama" and body["options"]["baseURL"] == "http://harness2-proxy:11434/v1"
    assert e["OPENCODE_DISABLE_MODELS_FETCH"] == "1" and e["XDG_DATA_HOME"].startswith(sr.HOME)


# ── the class ─────────────────────────────────────────────────────────────────────

def test_it_subclasses_upstreams_proposer_and_the_overrides_are_applied(pure, up):
    p = pure.prop("local")
    assert isinstance(p, up.proposer.Proposer) and type(p).__name__ == "ContainedProposer"
    assert p.attempts == 1 and p.project == "" and p.region == ""
    assert up.recursion.STAGE_ATTEMPTS == 1 and up.recursion.TRANSPORT_ATTEMPTS == 1 and up.config.TRANSPORT_ATTEMPTS == 1
    assert cp.contained_proposer_class(up) is type(p)                      # built once


def test_an_agy_channel_is_carried_by_an_opencode_spec_and_says_what_it_really_is(pure):
    p = pure.prop("agy")
    assert p.spec.kind == "opencode" and p.real_kind == "agy_cli"
    assert pure.prop("claude").spec.kind == "claude_cli" and pure.prop("local").spec.kind == "opencode"


def test_misconfiguration_is_loud(pure, up):
    cls = cp.contained_proposer_class(up)
    ch = pure.profile.channels["agy"]
    with pytest.raises(ContainedProposerError, match="SessionConfig"):
        cls(up.spec.ChannelSpec("opencode", ch.model))
    with pytest.raises(ContainedProposerError, match="carried by"):
        cls(up.spec.ChannelSpec("claude_cli", ch.model), session=pure.session("agy"), real_kind="agy_cli")
    with pytest.raises(ContainedProposerError, match="real_kind"):
        cls(up.spec.ChannelSpec("opencode", ch.model), session=pure.session("agy"), real_kind="opencode")
    with pytest.raises(ContainedProposerError, match="model"):
        cls(up.spec.ChannelSpec("opencode", "other"), session=pure.session("agy"), real_kind="agy_cli")
    with pytest.raises(ContainedProposerError, match="not declared"):
        pure.session("nonexistent")
    with pytest.raises(ContainedProposerError, match="named 'agy'"):
        pure.session("agy", cli_paths={"agy_cli": str(pure.bins / "claude")})
    with pytest.raises(ContainedProposerError, match="workspace_roots"):
        pure.session("agy", scratch_root=str(pure.tmp))


def test_apply_overrides_names_a_constant_upstream_renamed(up):
    class Fake:
        commit = "f" * 40
        recursion = type("M", (), {"__name__": "x.recursion", "TRANSPORT_ATTEMPTS": 2})
        config = type("M", (), {"__name__": "x.config", "TRANSPORT_ATTEMPTS": 2})
    with pytest.raises(ContainedProposerError, match="STAGE_ATTEMPTS"):
        cp.apply_overrides(Fake)


def test_the_class_keeps_upstreams_call_signature_and_dispatch(up):
    sig = inspect.signature(cp.contained_proposer_class(up).call)
    assert list(sig.parameters) == ["self", "mission", "context_dir", "label", "system", "timeout", "max_attempts", "harvest"]


# ── a refused decision starts nothing ─────────────────────────────────────────────

def test_a_refused_channel_decision_starts_nothing(world, monkeypatch):
    started = []
    monkeypatch.setattr(sr.ProposerSessionRunner, "run", lambda self, spec: started.append(spec))
    p = world.prop("claude", session_kw=dict(classification="restricted"))     # unknown retention carries only `open`
    with pytest.raises(world.up.proposer.ProposerError, match="refused before anything started"):
        world.call(p, "FAKE:marker")
    assert started == [] and not any(world.scratch.iterdir())


def test_a_config_that_check_proposer_refuses_starts_nothing(world, monkeypatch):
    started = []
    monkeypatch.setattr(sr.ProposerSessionRunner, "run", lambda self, spec: started.append(spec))
    p = world.prop("agy")
    monkeypatch.setattr(cp, "CLI_NAMES", {**cp.CLI_NAMES})
    monkeypatch.setattr(upstream_egress, "COMMAND_SHAPES", {**upstream_egress.COMMAND_SHAPES,
                                                            "agy_cli": upstream_egress.CommandShape(("agy",), {}, {"agy": ()})})
    with pytest.raises(world.up.proposer.ProposerError, match="rule command-shape"):
        world.call(p, "FAKE:marker")
    assert started == []


def test_a_wrong_model_is_refused_by_the_gate_before_anything_starts(world, monkeypatch):
    started = []
    monkeypatch.setattr(sr.ProposerSessionRunner, "run", lambda self, spec: started.append(spec))
    p = world.prop("claude")
    p.spec = world.up.spec.ChannelSpec("claude_cli", "claude-other")           # drifted after construction
    with pytest.raises(world.up.proposer.ProposerError, match="model-mismatch"):
        world.call(p, "FAKE:marker")
    assert started == []


# ── real sessions, one per kind ───────────────────────────────────────────────────

@pytest.mark.requires_rootless_docker
@pytest.mark.parametrize("name,text,cost", [("local", "EVENT-ANSWER", 0.0), ("claude", "CLAUDE-ANSWER", 0.5),
                                            ("agy", "AGY-ANSWER", 0.0)])
def test_a_session_per_kind_returns_the_reply_and_the_harvest(world, monkeypatch, name, text, cost):
    for k, v in (("ANTHROPIC_API_KEY", CANARY), ("GITHUB_TOKEN", CANARY), ("CLAUDE_CODE_USE_VERTEX", "1")):
        monkeypatch.setenv(k, v)
    p = world.prop(name)
    r = world.call(p, "FAKE:record FAKE:edit")
    kind = KIND[name]
    assert r.ok and r.text.strip() == text and r.attempts == 1 and not r.cached
    assert r.cost_usd == cost
    assert r.channel == kind                                                  # the real kind, not the carrier's
    # the harvest path: what the session wrote under context/harness comes back in Reply.workspace
    assert r.workspace is not None and r.workspace.is_dir()
    assert harvested(r, "edited.md").strip() == f"edited by the fake {CLI[kind]}"
    assert harvested(r, "seed.md") == "SEED-CONTENT"                          # the evidence went in
    assert harvested(r, "who.txt").strip() == CLI[kind]
    # it ran in the scratch copy, at the same absolute path, and used the exact argv
    ws = harvested(r, "pwd.txt").strip()
    assert ws.startswith(str(world.scratch) + "/")
    argv = harvested(r, "argv.txt").splitlines()
    ch = world.profile.channels[name]
    if kind == "opencode":
        assert argv[:7] == ["run", "--standalone", "--auto", "--format", "json", "--model", ch.model]
    elif kind == "claude_cli":
        assert "-p" in argv and "--no-session-persistence" in argv and ch.model in argv
    else:
        assert argv[0].startswith("--print=") and f"--model={ch.model}" in argv
    # the container environment: closed, no Vertex, no host variable
    env = harvested(r, "env.txt")
    assert CANARY not in env and "VERTEX" not in env and "GOOGLE_CLOUD" not in env
    assert "HTTPS_PROXY=http://harness2-proxy:" in env and "HOME=/home/agent" in env
    if kind == "opencode":
        assert "http://harness2-proxy:11434/v1" in env
    # audit trail and cache: the second identical call is served from the audit directory
    again = world.call(p, "FAKE:record FAKE:edit")
    assert again.cached and again.channel == kind and again.text.strip() == text
    meta = json.loads(next(world.audit.glob("t.a1.*.meta.json")).read_text())
    assert meta["channel"] == kind and meta["model"] == ch.model


@pytest.mark.requires_rootless_docker
def test_opencode_prefers_output_answer_md_over_its_event_stream(world):
    r = world.call(world.prop("local"), "FAKE:file-answer")
    assert r.text == "FILE-ANSWER"


# ── failures are failures ─────────────────────────────────────────────────────────

@pytest.mark.requires_rootless_docker
@pytest.mark.parametrize("name,directive,needle", [
    ("local", "FAKE:no-reply", "empty reply"),
    ("local", "FAKE:fail", "rc=3"),
    ("agy", "FAKE:fail", "rc=3"),
    ("claude", "FAKE:cli-error", "CLI error"),
])
def test_a_session_without_a_usable_reply_is_a_proposer_error(world, name, directive, needle):
    with pytest.raises(world.up.proposer.ProposerError, match=needle) as e:
        world.call(world.prop(name), directive)
    assert e.value.attempts == 1                                               # attempts=1: nothing was retried


@pytest.mark.requires_rootless_docker
@pytest.mark.parametrize("directive,rule", [("FAKE:symlink", "symlink"), ("FAKE:exec", "executable"),
                                            ("FAKE:big", "file-too-large")])
def test_a_workspace_that_fails_inspection_fails_the_session_and_copies_nothing_back(world, directive, rule):
    with pytest.raises(world.up.proposer.ProposerError, match=rf"failed inspection \(.*{rule}"):
        world.call(world.prop("local"), directive + " FAKE:edit")
    assert not list(world.audit.glob("*.workspace"))                           # nothing was harvested


@pytest.mark.requires_rootless_docker
def test_a_timeout_kills_the_session_and_leaves_nothing_behind(world, verified):
    t0 = time.monotonic()
    with pytest.raises(world.up.proposer.ProposerError, match="timeout after 5s"):
        world.call(world.prop("local"), "FAKE:sleep", timeout=5)
    assert time.monotonic() - t0 < 60
    assert docker(verified, "ps", "-a", "-q", "--filter", f"label={sr.LABEL}").stdout.split() == []


@pytest.mark.requires_rootless_docker
def test_the_session_timeout_bounds_a_longer_requested_one(world):
    t0 = time.monotonic()
    with pytest.raises(world.up.proposer.ProposerError, match="timeout"):
        world.call(world.prop("local", session_kw=dict(timeout_s=5)), "FAKE:sleep", timeout=3600)
    assert time.monotonic() - t0 < 60


# ── the model gate ────────────────────────────────────────────────────────────────

@pytest.mark.requires_rootless_docker
def test_two_concurrent_calls_run_one_session_at_a_time(world):
    gate = model_gate.ModelGate()
    p = world.prop("local", session_kw=dict(gate=gate))
    replies, errors = {}, []

    def go(label):
        try:
            replies[label] = world.call(p, f"FAKE:stamp FAKE:edit {label}", label=label)
        except Exception as e:     # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(f"c{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(300)
    assert not errors, errors
    spans = sorted(tuple(int(x) for x in harvested(r, "stamp.txt").split()) for r in replies.values())
    assert len(spans) == 2
    assert spans[0][1] <= spans[1][0], f"the two model sessions overlapped: {spans}"      # one at a time
    assert not gate.locked()


@pytest.mark.requires_rootless_docker
def test_a_gate_that_stays_taken_fails_the_call_without_starting_a_container(world, monkeypatch):
    started = []
    real_run = sr.ProposerSessionRunner.run
    monkeypatch.setattr(sr.ProposerSessionRunner, "run", lambda self, spec: started.append(spec) or real_run(self, spec))
    gate = model_gate.ModelGate()
    p = world.prop("local", session_kw=dict(gate=gate, gate_wait_s=0.3))
    with gate.hold(label="solver:other"):
        with pytest.raises(world.up.proposer.ProposerError, match="GateTimeout.*solver:other"):
            world.call(p, "FAKE:marker")
    assert started == []


# ── the selector ──────────────────────────────────────────────────────────────────

def test_make_selector_prop_is_what_schedule_uses_and_a_plain_proposer_never_is(pure, up):
    p = pure.prop("local")
    sel = make_selector_prop(p)
    assert type(sel) is type(p) and sel.real_kind == "opencode" and sel.audit_dir == Path(p.audit_dir) / "selector"
    other = make_selector_prop(p, channel="agy")
    assert other.real_kind == "agy_cli" and other.spec.kind == "opencode" and other.session.channel == "agy"
    adapter = type("A", (), {"spec": type("S", (), {"channels": {"selector": up.spec.ChannelSpec("claude_cli", "claude-x")}})()})()
    assert up.schedule._selector_for(adapter, p, sel) is sel                  # passed explicitly: used as is
    plain = up.schedule._selector_for(adapter, p, None)                       # NOT passed: upstream builds a host Proposer
    assert type(plain) is up.proposer.Proposer and not isinstance(plain, type(p))


# ── the shared sweep budget and the audit observer reach every proposer session ───────────────

def test_the_session_config_carries_the_budget_and_observer_into_every_runner(pure):
    from cognitive_harness2.sweep_budget import SweepBudget
    b = SweepBudget(600, 120, 7, role_caps={"proposer": 3, "solver": 4})
    seen = []
    prop = pure.prop(session_kw=dict(budget=b, observer=lambda n, f: seen.append(n)))
    r = prop._runner(None)
    assert r.budget is b and r.observer is not None and r.role == "proposer"
    sel = make_selector_prop(prop)
    assert sel._runner(None).budget is b


def test_an_exhausted_budget_stops_a_proposer_session_before_anything_is_created(pure, monkeypatch):
    from cognitive_harness2.sweep_budget import SweepBudget
    t = [100.0]
    b = SweepBudget(10, 5, 3, clock=lambda: t[0])
    t[0] += 50                                                     # the sweep's wall clock is spent
    events = []
    prop = pure.prop(session_kw=dict(budget=b, observer=lambda n, f: events.append((n, dict(f)))))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("docker")))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("docker")))
    with pytest.raises(Exception) as ei:
        pure.call(prop, "FAKE:marker")
    assert "wall-clock" in str(ei.value) or "budget" in str(ei.value).lower()
    assert calls == [] and not any(pure.scratch.iterdir())
    assert [n for n, _ in events] == ["session_refused"] and events[0][1]["reason"] == "budget:deadline"
