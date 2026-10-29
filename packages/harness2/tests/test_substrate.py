"""Seam 3: `CognitiveSubstrate`, upstream's Substrate Protocol with execution switched off.

`surface`, `materialise` and `preflight` work; `execute` and `read_trajectory` raise. A fake `docker`
answers the gate here (the real daemon is exercised at the bottom, marked requires_rootless_docker).
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cognitive_harness2 import substrate as sub
from cognitive_harness2.invariants import InvariantViolation
from cognitive_harness2.quota import QuotaVerdict
from cognitive_harness2.sandbox import DEFAULT_IMAGE, SandboxUnavailable
from cognitive_harness2.substrate import CognitiveSubstrate, PreflightError, StagedInstall

SOCK = "unix:///run/user/1000/docker.sock"
ROOTLESS = ["name=seccomp,profile=builtin", "name=rootless", "name=cgroupns"]
ROOTFUL = ["name=seccomp,profile=builtin", "name=cgroupns"]
BENCH = SimpleNamespace(install_mode="workspace_agents_md")

BASE_TREE = {
    "systemprompt.md": "You are careful.\n",
    "guardrails.md": "Never delete.\n",
    "LongTermMEMORY.md": "facts\n",
    "tool_descriptions/tool_guidance.md": "use tools\n",
    "skills/review/SKILL.md": "---\nname: review\ndescription: d\n---\nbody\n",
    "scripts/helper.py": "print('x')\n",
    "sub_agents/helper/AGENT.md": "---\nmode: subagent\ndescription: d\n---\n",
}


@pytest.fixture(scope="module")
def opencode():
    from harness2.substrate.opencode import OPENCODE_SURFACE
    return OPENCODE_SURFACE


@pytest.fixture(scope="module")
def codex():
    from harness2.substrate.codex import CODEX_SURFACE
    return CODEX_SURFACE


@pytest.fixture(autouse=True)
def private_tmp(tmp_path, monkeypatch):
    """Stage under a private temp dir (never under $HOME, which staging refuses)."""
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def tree(tmp_path):
    return write_tree(tmp_path / "tree", BASE_TREE)


def fake_docker(*, options=ROOTLESS, info_rc=0, image_rc=0, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((list(argv), dict(kw)))
        sub_ = argv[1:]
        if sub_[:2] == ["context", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, SOCK + "\n", "")
        assert sub_[0] == "-H", f"docker call not pinned to the verified host: {argv}"
        verb = sub_[2:]
        if verb[0] == "info":
            return subprocess.CompletedProcess(argv, info_rc, json.dumps(options), "daemon says no" if info_rc else "")
        if verb[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, image_rc, "sha256:abc\n", "")
        raise AssertionError(f"unexpected docker call {argv}")
    return run


def make(spec, **kw):
    kw.setdefault("which", lambda _: "/usr/bin/docker")
    kw.setdefault("environ", {"DOCKER_HOST": SOCK})
    return CognitiveSubstrate(spec, **kw)


# ── the Protocol shape ────────────────────────────────────────────────────────────

def test_it_satisfies_upstreams_substrate_protocol(opencode):
    from harness2.substrate import Substrate
    assert isinstance(make(opencode), Substrate)


def test_method_signatures_match_the_protocol(opencode):
    from harness2.substrate import Substrate
    for name in ("surface", "materialise", "execute", "read_trajectory", "preflight"):
        want = inspect.signature(getattr(Substrate, name))
        got = inspect.signature(getattr(CognitiveSubstrate, name))
        assert list(got.parameters)[: len(want.parameters)] == list(want.parameters), name


def test_the_install_it_returns_has_upstreams_install_fields():
    from harness2.substrate import Install
    assert [f.name for f in dataclasses.fields(StagedInstall)] == [f.name for f in dataclasses.fields(Install)]


def test_install_modes_are_upstreams():
    from harness2.core.spec import INSTALL_MODES
    assert sub.INSTALL_MODES == INSTALL_MODES


def test_surface_returns_the_spec_and_the_allowed_slice(opencode):
    s = make(opencode)
    assert s.surface() is opencode
    assert s.allowed_surface().slots == {"c1", "c3", "c4", "c5"}
    assert s.name == "cognitive"


@pytest.mark.parametrize("bad", [None, object(), SimpleNamespace(name="x")])
def test_an_unrecognised_surface_is_refused_at_construction(bad):
    with pytest.raises(InvariantViolation):
        CognitiveSubstrate(bad)


# ── execute and read_trajectory are not enabled ───────────────────────────────────

def test_execute_refuses_and_starts_nothing(opencode):
    calls: list = []
    s = make(opencode, run=fake_docker(calls=calls))
    with pytest.raises(NotImplementedError, match="^slice 3b, not enabled$"):
        s.execute("task-1", StagedInstall(Path("/nonexistent"), "f", {}), ["r1"], 60)
    assert calls == []


def test_read_trajectory_refuses(opencode):
    with pytest.raises(NotImplementedError, match="^slice 3b, not enabled$"):
        make(opencode).read_trajectory(Path("/nonexistent"), obs_trunc=100, max_chars=1000)


def test_there_is_no_way_to_enable_execution_by_argument(opencode):
    with pytest.raises(TypeError):
        CognitiveSubstrate(opencode, enable_execution=True)  # type: ignore[call-arg]
    with pytest.raises(NotImplementedError):
        make(opencode).execute("t", StagedInstall(Path("/x"), "f", {}), [], 1)


# ── materialise ───────────────────────────────────────────────────────────────────

def staged_files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def test_materialise_stages_the_tree_read_only(opencode, tree, private_tmp):
    s = make(opencode)
    inst = s.materialise(tree, BENCH, {})
    try:
        assert isinstance(inst, StagedInstall)
        assert inst.staging_dir.parent == private_tmp and inst.staging_dir.name.startswith("harness2-stage-")
        assert staged_files(inst.staging_dir) == BASE_TREE
        assert inst.extras == {"install_mode": "workspace_agents_md", "read_only": "true"}
        assert len(inst.fingerprint) == 16
        for p in inst.staging_dir.rglob("*"):
            mode = stat.S_IMODE(p.stat().st_mode)
            assert mode == (0o555 if p.is_dir() else 0o444), (p, oct(mode))
        assert stat.S_IMODE(inst.staging_dir.stat().st_mode) == 0o555
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                (inst.staging_dir / "systemprompt.md").write_text("x")
            with pytest.raises(PermissionError):
                (inst.staging_dir / "new.md").write_text("x")
    finally:
        s.release(inst)
    assert not inst.staging_dir.exists()


def test_the_fingerprint_digests_the_source_tree(opencode, tree, tmp_path):
    s = make(opencode)
    a = s.materialise(tree, BENCH, {})
    b = s.materialise(tree, BENCH, {"file:extra.md": "ctx file"})  # ctx files ride along, not in the digest
    (tree / "systemprompt.md").write_text("changed\n")
    c = s.materialise(tree, BENCH, {})
    try:
        assert a.fingerprint == b.fingerprint != c.fingerprint
    finally:
        for i in (a, b, c):
            s.release(i)


def test_ctx_files_are_staged_and_other_ctx_keys_are_ignored(opencode, tree):
    s = make(opencode)
    inst = s.materialise(tree, BENCH, {"file:docs/note.md": "hello", "token": "v"})
    try:
        assert (inst.staging_dir / "docs/note.md").read_text() == "hello"
        assert not (inst.staging_dir / "token").exists()
    finally:
        s.release(inst)


def stage_dirs(private_tmp):
    return sorted(p.name for p in private_tmp.iterdir() if p.name.startswith("harness2-stage-"))


@pytest.mark.parametrize("ctx", [
    {"file:../escape.md": "x"},
    {"file:/abs.md": "x"},
    {"file:./dot.md": "x"},
    {"file:a//b.md": "x"},
    {"file:a\\b.md": "x"},
    {"file:": "x"},
    {"file:hooks/x.md": "x"},
    {"file:guard/x.md": "x"},
    {"file:x\x00y.md": "x"},
    {"file:systemprompt.md": "overwrite"},   # collides with the tree
    {"file:ok.md": 5},
    {5: "x"},
])
def test_bad_ctx_files_are_refused_and_stage_nothing(opencode, tree, private_tmp, ctx):
    with pytest.raises((ValueError, InvariantViolation, TypeError)):
        make(opencode).materialise(tree, BENCH, ctx)
    assert stage_dirs(private_tmp) == []


def test_a_protected_path_in_the_tree_is_refused_by_the_staging_key_rules(opencode, tree, private_tmp):
    write_tree(tree, {"hooks/pre.sh": "x"})
    with pytest.raises(InvariantViolation, match="hooks"):
        make(opencode).materialise(tree, BENCH, {})
    assert stage_dirs(private_tmp) == []


def test_a_codex_tree_with_hooks_is_refused(codex, tmp_path, private_tmp):
    t = write_tree(tmp_path / "cx", {"AGENTS.md": "a", ".codex/hooks.json": "{}"})
    with pytest.raises(InvariantViolation):
        make(codex).materialise(t, SimpleNamespace(install_mode="codex_root"), {})
    assert stage_dirs(private_tmp) == []


def test_symlinks_are_refused_wherever_they_point(opencode, tree, tmp_path, private_tmp):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (tree / "link.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        make(opencode).materialise(tree, BENCH, {})
    (tree / "link.md").unlink()
    (tree / "dirlink").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        make(opencode).materialise(tree, BENCH, {})
    (tree / "dirlink").unlink()
    (tree / "dangling").symlink_to(tmp_path / "nope")
    with pytest.raises(ValueError, match="symlink"):
        make(opencode).materialise(tree, BENCH, {})
    assert stage_dirs(private_tmp) == []


def test_a_symlinked_tree_dir_is_refused(opencode, tree, tmp_path):
    link = tmp_path / "treelink"
    link.symlink_to(tree)
    with pytest.raises(ValueError, match="symlink"):
        make(opencode).materialise(link, BENCH, {})


def test_special_files_and_non_utf8_files_are_refused(opencode, tree, private_tmp):
    (tree / "bin.dat").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ValueError, match="UTF-8"):
        make(opencode).materialise(tree, BENCH, {})
    (tree / "bin.dat").unlink()
    os.mkfifo(tree / "pipe")
    with pytest.raises(ValueError, match="regular"):
        make(opencode).materialise(tree, BENCH, {})
    assert stage_dirs(private_tmp) == []


def test_size_and_count_caps(opencode, tree, monkeypatch):
    monkeypatch.setattr(sub, "MAX_FILES", 3)
    with pytest.raises(ValueError, match="more than 3 files"):
        make(opencode).materialise(tree, BENCH, {})
    monkeypatch.setattr(sub, "MAX_FILES", 100)
    monkeypatch.setattr(sub, "MAX_FILE_BYTES", 4)
    with pytest.raises(ValueError, match="larger than"):
        make(opencode).materialise(tree, BENCH, {})


@pytest.mark.parametrize("mode", ["nope", "", None, "codex_root_x"])
def test_an_unknown_install_mode_is_refused(opencode, tree, mode):
    with pytest.raises(ValueError, match="install_mode"):
        make(opencode).materialise(tree, SimpleNamespace(install_mode=mode), {})


def test_a_spec_without_install_mode_is_refused(opencode, tree):
    with pytest.raises(ValueError, match="install_mode"):
        make(opencode).materialise(tree, SimpleNamespace(), {})


def test_a_missing_or_foreign_directory_is_refused(opencode, tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        make(opencode).materialise(tmp_path / "nope", BENCH, {})
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="harness tree"):
        make(opencode).materialise(empty, BENCH, {})


def test_changed_paths_outside_the_mutable_surface_are_refused_before_anything_is_read(opencode, tree, private_tmp):
    s = make(opencode)
    for bad in (["guardrails.md"], ["plugin/gate.ts"], ["scripts/helper.py"], ["../x"], ["nope.md"]):
        with pytest.raises(InvariantViolation):
            s.materialise(tree, BENCH, {}, changed=bad)
    assert stage_dirs(private_tmp) == []
    inst = s.materialise(tree, BENCH, {}, changed=["systemprompt.md", "sub_agents/helper/AGENT.md"])
    s.release(inst)


def test_release_only_removes_what_this_substrate_staged(opencode, tree, tmp_path):
    s = make(opencode)
    foreign = tmp_path / "harness2-stage-foreign"
    foreign.mkdir()
    with pytest.raises(ValueError, match="not staged"):
        s.release(StagedInstall(foreign, "f", {}))
    assert foreign.exists()
    with pytest.raises(ValueError, match="not staged"):
        s.release(StagedInstall(Path("/"), "f", {}))
    inst = s.materialise(tree, BENCH, {})
    s.release(inst)
    with pytest.raises(ValueError, match="not staged"):
        s.release(inst)  # already gone


def test_materialise_runs_no_subprocess(opencode, tree, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("materialise must not start a process")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    s = make(opencode, run=boom)
    s.release(s.materialise(tree, BENCH, {}))


def test_the_install_mode_must_belong_to_the_surface(opencode, codex, tree, tmp_path):
    with pytest.raises(ValueError, match="does not belong"):
        make(opencode).materialise(tree, SimpleNamespace(install_mode="codex_root"), {})
    cx = write_tree(tmp_path / "cx", {"AGENTS.md": "a"})
    with pytest.raises(ValueError, match="does not belong"):
        make(codex).materialise(cx, SimpleNamespace(install_mode="home_dir_jinja"), {})
    s = make(codex)
    s.release(s.materialise(cx, SimpleNamespace(install_mode="codex_root"), {}))


# ── preflight ─────────────────────────────────────────────────────────────────────

def test_preflight_passes_on_a_rootless_daemon_and_only_asks_it_two_questions(opencode):
    calls: list = []
    make(opencode, run=fake_docker(calls=calls)).preflight()
    verbs = [argv[3:5] if argv[3] == "image" else argv[3:4] for argv, _ in calls]
    assert verbs == [["info"], ["image", "inspect"]]
    assert all(argv[1:3] == ["-H", SOCK] for argv, _ in calls)
    assert all("DOCKER_HOST" not in kw["env"] and "DOCKER_CONTEXT" not in kw["env"] for _, kw in calls)


def test_preflight_never_creates_runs_or_pulls(opencode):
    calls: list = []
    make(opencode, run=fake_docker(calls=calls)).preflight()
    flat = {w for argv, _ in calls for w in argv}
    assert not flat & {"run", "create", "start", "pull", "exec", "rm", "kill", "build", "load"}


REFUSALS = {
    "no docker": dict(which=lambda _: None, run=fake_docker(), needle="not installed"),
    "rootful": dict(run=fake_docker(options=ROOTFUL), needle="rootless"),
    "unreachable": dict(run=fake_docker(info_rc=1), needle="unreachable"),
    "image missing": dict(run=fake_docker(image_rc=1), needle="pinned sandbox image"),
    "remote daemon": dict(environ={"DOCKER_HOST": "tcp://10.0.0.1:2375"}, run=fake_docker(), needle="local unix socket"),
}


@pytest.mark.parametrize("case", REFUSALS.values(), ids=REFUSALS.keys())
def test_preflight_raises_a_remediation_message(opencode, case):
    case = dict(case)
    needle = case.pop("needle")
    with pytest.raises(PreflightError) as ei:
        make(opencode, **case).preflight()
    msg = str(ei.value)
    assert needle in msg
    assert "dockerd-rootless-setuptool.sh install" in msg and "docker pull alpine:3" in msg
    assert "Nothing was started" in msg
    assert isinstance(ei.value, RuntimeError)  # upstream's contract: RuntimeError with the remediation
    assert isinstance(ei.value.__cause__, SandboxUnavailable)


def test_quota_is_not_consulted_unless_the_caller_enables_it(opencode):
    s = make(opencode, run=fake_docker())
    s.preflight()  # no quota_check: nothing else to call, and nothing tries
    seen: list = []
    make(opencode, run=fake_docker(), quota_check=lambda: seen.append(1) or QuotaVerdict(True, "ok")).preflight()
    assert seen == [1]


def test_a_refused_quota_raises_with_a_remediation(opencode):
    with pytest.raises(PreflightError, match="daily free-model requests exhausted"):
        make(opencode, run=fake_docker(),
             quota_check=lambda: QuotaVerdict(False, "daily free-model requests exhausted")).preflight()


def test_a_quota_check_that_crashes_is_a_refusal_without_its_message(opencode):
    def boom():
        raise RuntimeError("secret-in-message")
    with pytest.raises(PreflightError) as ei:
        make(opencode, run=fake_docker(), quota_check=boom).preflight()
    assert "secret-in-message" not in str(ei.value) and "RuntimeError" in str(ei.value)


def test_a_quota_check_that_returns_the_wrong_thing_is_a_refusal(opencode):
    with pytest.raises(PreflightError, match="unreadable"):
        make(opencode, run=fake_docker(), quota_check=lambda: True).preflight()  # type: ignore[arg-type,return-value]


def test_docker_is_checked_before_quota(opencode):
    seen: list = []
    with pytest.raises(PreflightError):
        make(opencode, run=fake_docker(options=ROOTFUL), quota_check=lambda: seen.append(1)).preflight()
    assert seen == []


async def test_the_openrouter_quota_helper_is_a_sync_callable_that_works_inside_a_loop():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openrouter.ai" and request.url.path == "/api/v1/key"
        return httpx.Response(200, json={"data": {"free_model_daily_requests": {"remaining": 5}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    check = sub.openrouter_quota_check("k", client=client)
    v = check()  # called from inside a running loop: must not blow up
    assert v.ok and v.detail == {"remaining": 5.0}
    await client.aclose()


def test_the_openrouter_quota_helper_without_a_key_is_a_refusal():
    v = sub.openrouter_quota_check(None)()
    assert v.ok is False and "no OpenRouter API key" in v.reason


# ── the real daemon (never skipped) ───────────────────────────────────────────────

@pytest.mark.requires_rootless_docker
def test_preflight_against_the_real_rootless_daemon_asks_only_pinned_read_only_questions(opencode, monkeypatch):
    def containers() -> list[str]:
        return subprocess.run(["docker", "ps", "-a", "-q"], capture_output=True, text=True, timeout=30).stdout.split()

    before = containers()
    seen: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv, *a, **kw):
        seen.append(list(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", recording_run)
    CognitiveSubstrate(opencode).preflight()
    monkeypatch.undo()

    assert seen, "preflight made no docker call at all"
    for argv in seen:
        assert Path(argv[0]).name == "docker"
        if argv[1:3] == ["context", "inspect"]:
            continue  # resolving the endpoint when DOCKER_HOST is unset; read-only and not yet pinned
        assert argv[1] == "-H" and argv[2].startswith("unix://"), argv
        assert argv[3:5] in (["info", "--format"], ["image", "inspect"]), argv
    assert containers() == before
