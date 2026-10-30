"""The stage-1 runner: `plan` is read-only, `run` is gated, `selftest` is the whole pipeline on the real daemon.

No model is called and no real CLI runs. The "model" is a throwaway HTTP server on a free loopback port and the
"opencode" is `selftest_fake_opencode.sh`. The pure tests inject the few outside probes (docker gate, image, Ollama)
and a fake `run_task`; the end-to-end tests use the real rootless daemon and do not skip.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from h2_upstream import isolated_upstream

from cognitive_harness2 import channels, contained_proposer as cp, sandbox, session_runner as sr, stage1, upstream_egress
from cognitive_harness2.image import ImageError, ImageRef
from cognitive_harness2.stage1 import (Approval, Audit, FakeOllama, Probes, Stage1Error, build_parser, clean,
                                       confirm_on_terminal, confirmation_phrase, run_plan, run_run, run_selftest,
                                       selftest_problems)

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "harness2-channels.stage1.toml"
FAKE_SH = REPO / "src" / "cognitive_harness2" / "selftest_fake_opencode.sh"
SOCK = "unix:///run/user/1000/docker.sock"
VERIFIED = sandbox.VerifiedDocker("/usr/bin/docker", SOCK)
IMAGE = ImageRef("cognitive-harness2-proposer:0123456789ab", "sha256:" + "5" * 64)
SENTINELS = ("careful bookkeeper", "credits positive", "Read MISSION.md first")        # prompt text that must not be logged


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(scope="module")
def up(tmp_path_factory):
    with isolated_upstream(tmp_path_factory.mktemp("runs")) as u:
        yield u


class Rig:
    """A profile on disk, a fake CLI, a fake Ollama, and counters on every outside probe."""

    def __init__(self, tmp: Path, fake: FakeOllama):
        self.tmp, self.fake = tmp, fake
        self.ws = tmp / "workspaces"
        self.ws.mkdir()
        self.runs = tmp / "runs"
        bindir = tmp / "bin"
        bindir.mkdir()
        self.cli = bindir / "opencode"
        shutil.copyfile(FAKE_SH, self.cli)
        self.cli.chmod(0o755)
        text = EXAMPLE.read_text().replace("/home/rockad/h2-workspaces", str(self.ws)).replace("127.0.0.1:11434", f"127.0.0.1:{fake.port}")
        self.profile = tmp / "channels.toml"
        self.profile.write_text(text)
        self.calls: dict[str, int] = {"gate": 0, "resolve": 0, "ollama": 0}

    def probes(self, *, gate=None, resolve=None, ollama=None) -> Probes:
        def count(name, fn):
            def wrapped(*a):
                self.calls[name] += 1
                return fn(*a)
            return wrapped
        return Probes(gate=count("gate", gate or (lambda: VERIFIED)), resolve=count("resolve", resolve or (lambda v: IMAGE)),
                      ollama=count("ollama", ollama or stage1.probe_ollama), environ={}, which=lambda n: None)

    def args(self, *extra: str):
        return build_parser().parse_args(["--profile", str(self.profile), "--opencode", str(self.cli),
                                          "--runs-dir", str(self.runs), *extra])

    def parse(self, cmd: str, *extra: str):
        return build_parser().parse_args([cmd, "--profile", str(self.profile), "--opencode", str(self.cli),
                                          "--runs-dir", str(self.runs), *extra])


@pytest.fixture
def rig(tmp_path):
    with FakeOllama() as fake:
        yield Rig(tmp_path, fake)


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")} if root.exists() else set()


class Traps:
    """Anything that would start something fails the test: processes, sockets to anywhere but the fake Ollama, the bridge."""

    def __init__(self, monkeypatch, allowed_port: int):
        self.bridges = 0

        def no_proc(*a, **k):
            raise AssertionError(f"a process was started: {a[:1]}")
        monkeypatch.setattr(subprocess, "run", no_proc)
        monkeypatch.setattr(subprocess, "Popen", no_proc)

        class Bridge:
            def __init__(inner, *a, **k):
                self.bridges += 1
                raise AssertionError("the host bridge was started")
        monkeypatch.setattr(sr, "HostBridge", Bridge)
        real_connect = socket.socket.connect

        def connect(sock, addr, *a):
            if not (isinstance(addr, tuple) and addr[0] == "127.0.0.1" and addr[1] == allowed_port):
                raise AssertionError(f"a connection to {addr} was attempted")
            return real_connect(sock, addr, *a)
        monkeypatch.setattr(socket.socket, "connect", connect)


# ══ plan: side-effect free ════════════════════════════════════════════════════════

def test_plan_is_side_effect_free_and_prints_the_whole_summary_and_the_approve_command(rig, up, monkeypatch, capsys):
    home = rig.tmp / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    before = tree(rig.tmp) | {f"home:{p}" for p in tree(home)}
    Traps(monkeypatch, rig.fake.port)
    out = io.StringIO()
    code = run_plan(rig.parse("plan"), environ={"HOME": str(home)}, probes=rig.probes(), out=out)
    text = out.getvalue()
    assert code == 0, text
    assert tree(rig.tmp) | {f"home:{p}" for p in tree(home)} == before            # not one file or directory appeared
    assert rig.calls == {"gate": 1, "resolve": 1, "ollama": 1}
    for needle in ("local-ollama", "ollama/gemma4:12b", f"127.0.0.1:{rig.fake.port}/v1", "sessions 7", "2700 s", "45 min",
                   "cost $0", "P=3", "R=4", "600 s per session", "worst case 4200 s", "MORE than the 2700 s wall",
                   "leaves machine   nothing", "2 GB, 2 CPUs, 256 pids", "classification   open", "READY."):
        assert needle in text, needle
    cmd = next(line.strip() for line in text.splitlines() if "stage1 run --approve" in line)
    parsed = build_parser().parse_args(shlex.split(cmd)[5:])                 # after `uv run python -m cognitive_harness2.stage1`
    assert parsed.cmd == "run" and parsed.approve == "local-ollama" and parsed.profile == str(rig.profile)
    assert parsed.opencode == str(rig.cli) and parsed.runs_dir == str(rig.runs) and parsed.per_session_s == 600.0


def test_plan_is_the_default_command_and_never_writes_under_config(rig, up, monkeypatch, capsys):
    home = rig.tmp / "h"
    (home / ".config" / "cognitive-engine").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COGNITIVE_HARNESS2_CHANNELS_FILE", str(rig.profile))
    monkeypatch.setattr(stage1, "Probes", lambda **k: rig.probes())
    before = tree(home)
    code = stage1.main(["--opencode", str(rig.cli), "--runs-dir", str(rig.runs)])
    assert code == 0 and "STAGE 1" in capsys.readouterr().out and tree(home) == before and not rig.runs.exists()


def test_a_missing_profile_is_a_clear_refusal_not_a_fallback_to_the_default_file(rig, up):
    args = build_parser().parse_args(["plan", "--runs-dir", str(rig.runs)])
    with pytest.raises(Stage1Error, match="--profile FILE or set COGNITIVE_HARNESS2_CHANNELS_FILE"):
        run_plan(args, environ={}, probes=rig.probes(), out=io.StringIO())
    assert rig.calls == {"gate": 0, "resolve": 0, "ollama": 0}


def test_wall_s_can_only_lower_the_profiles_total(rig, up):
    out = io.StringIO()
    assert run_plan(rig.parse("plan", "--wall-s", "99999"), environ={}, probes=rig.probes(), out=out) == 0
    assert "2700 s" in out.getvalue() and "capped at 2700" in out.getvalue() and "--wall-s 2700" in out.getvalue() and "cost" not in out.getvalue().split("note:")[-1]
    out = io.StringIO()
    run_plan(rig.parse("plan", "--wall-s", "900"), environ={}, probes=rig.probes(), out=out)
    assert "900 s (15 min)" in out.getvalue() and "--wall-s 900" in out.getvalue()


@pytest.mark.parametrize("bad", ["0", "-5", "nan"])
def test_a_nonsense_time_is_refused(rig, up, bad):
    with pytest.raises(Stage1Error):
        run_plan(rig.parse("plan", "--per-session-s", bad), environ={}, probes=rig.probes(), out=io.StringIO())


# ══ preflight failures refuse before anything starts ══════════════════════════════

def failing_probes(rig, which):
    def boom_gate():
        raise sandbox.SandboxUnavailable("this Docker daemon is not rootless")

    def boom_image(v):
        raise ImageError("the proposer image is not present locally")
    return {
        "docker": rig.probes(gate=boom_gate),
        "image": rig.probes(resolve=boom_image),
        "ollama": rig.probes(ollama=lambda h, p, m: (False, "nothing usable on 127.0.0.1")),
    }[which]


@pytest.mark.parametrize("which,needle", [("docker", "not rootless"), ("image", "not present locally"),
                                          ("ollama", "nothing usable")])
def test_plan_reports_a_failing_preflight_as_not_ready_with_no_approve_command(rig, up, which, needle):
    out = io.StringIO()
    assert run_plan(rig.parse("plan"), environ={}, probes=failing_probes(rig, which), out=out) == 2
    text = out.getvalue()
    assert "NOT READY" in text and needle in text and "stage1 run --approve" not in text and "[FAIL]" in text


@pytest.mark.parametrize("which,needle", [("docker", "not rootless"), ("image", "not present locally"),
                                          ("ollama", "nothing usable")])
def test_a_failing_preflight_refuses_a_run_before_anything_is_created(rig, up, monkeypatch, capsys, which, needle):
    Traps(monkeypatch, rig.fake.port)
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=failing_probes(rig, which),
                   stdin=TTY("approve local-ollama anything\n"), stdout=TTY())
    err = capsys.readouterr().err
    assert code == 2 and needle in err and "nothing was started" in err
    assert not rig.runs.exists() and not any(rig.ws.iterdir())


def test_the_ollama_probe_against_nothing_and_against_the_wrong_model(rig):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    ok, why = stage1.probe_ollama("127.0.0.1", free, "gemma4:12b", timeout=1)
    assert not ok and "ollama serve" in why
    ok, why = stage1.probe_ollama("127.0.0.1", rig.fake.port, "qwen3:30b")
    assert not ok and "ollama pull qwen3:30b" in why and "gemma4:12b" in why
    ok, why = stage1.probe_ollama("127.0.0.1", rig.fake.port, "gemma4:12b")
    assert ok and "not loaded or called" in why


def test_a_workspace_root_that_is_missing_or_a_cli_that_is_missing_fails_the_preflight(rig, up, monkeypatch):
    monkeypatch.setenv("HOME", str(rig.tmp / "nohome"))
    shutil.rmtree(rig.ws)
    rig.cli.unlink()
    pre = stage1.preflight(stage1.Settings(profile=channels.load_profile({"COGNITIVE_HARNESS2_CHANNELS_FILE": str(rig.profile)}),
                                           cli=str(rig.cli), runs_dir=rig.runs), rig.probes())
    failing = {c.name: c.detail for c in pre.failing()}
    assert "does not exist" in failing["workspace_roots"] and "opencode not found" in failing["cli"] and not pre.ok


def test_a_channel_that_is_not_local_or_not_declared_fails_closed(rig, up):
    prof = channels.load_profile({"COGNITIVE_HARNESS2_CHANNELS_FILE": str(rig.profile)})
    pre = stage1.preflight(stage1.Settings(profile=prof, channel="nope", cli=str(rig.cli), runs_dir=rig.runs), rig.probes())
    assert not pre.ok and "not declared" in pre.checks[0].detail and rig.calls["gate"] == 0
    hosted = channels.validate({"workspace_roots": [str(rig.ws)], "channels": {"local-ollama": {
        "kind": "opencode", "provider": "openrouter", "endpoint": "https://openrouter.ai/api/v1", "model": "openrouter/x",
        "retention": "zdr-attested", "max_classification": "gated", "approval": "per-sweep",
        "caps": {"sessions": 7, "wall_s": 2700, "cost_usd": 1}}}}, source="t")
    pre = stage1.preflight(stage1.Settings(profile=hosted, cli=str(rig.cli), runs_dir=rig.runs), rig.probes())
    assert not pre.ok and "retention=local" in pre.checks[0].detail and rig.calls["gate"] == 0


# ══ run: the approval gate ════════════════════════════════════════════════════════

def test_run_without_approve_refuses_and_touches_nothing(rig, up, monkeypatch, capsys):
    Traps(monkeypatch, rig.fake.port)
    assert run_run(rig.parse("run"), environ={}, probes=rig.probes(), stdin=TTY(), stdout=TTY()) == 2
    assert "pass --approve local-ollama" in capsys.readouterr().err
    assert rig.calls == {"gate": 0, "resolve": 0, "ollama": 0} and not rig.runs.exists()


def test_run_with_the_wrong_channel_name_refuses(rig, up, monkeypatch, capsys):
    Traps(monkeypatch, rig.fake.port)
    assert run_run(rig.parse("run", "--approve", "openrouter"), environ={}, probes=rig.probes(), stdin=TTY(), stdout=TTY()) == 2
    assert "names 'openrouter'" in capsys.readouterr().err and rig.calls["gate"] == 0 and not rig.runs.exists()


def test_the_non_interactive_flag_is_not_accepted_by_run(rig, up, monkeypatch, capsys):
    Traps(monkeypatch, rig.fake.port)
    code = run_run(rig.parse("run", "--approve", "local-ollama", "--yes-i-am-the-user"), environ={}, probes=rig.probes(),
                   stdin=io.StringIO(), stdout=io.StringIO())
    assert code == 2 and "only for `selftest`" in capsys.readouterr().err and rig.calls["gate"] == 0 and not rig.runs.exists()


@pytest.mark.parametrize("stdin,stdout", [(io.StringIO("approve local-ollama x\n"), TTY()), (TTY(), io.StringIO())])
def test_run_needs_a_terminal_and_a_pipe_never_approves(rig, up, monkeypatch, capsys, stdin, stdout):
    Traps(monkeypatch, rig.fake.port)
    assert run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=rig.probes(), stdin=stdin, stdout=stdout) == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert rig.calls == {"gate": 0, "resolve": 0, "ollama": 0} and not rig.runs.exists()


def test_a_wrong_phrase_on_a_terminal_refuses_and_creates_nothing(rig, up, monkeypatch, capsys):
    Traps(monkeypatch, rig.fake.port)
    out = TTY()
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=rig.probes(),
                   stdin=TTY("yes\n"), stdout=out)
    assert code == 2 and "phrase was not typed exactly" in capsys.readouterr().err
    assert "approve local-ollama stage1-" in out.getvalue() and not rig.runs.exists() and not any(rig.ws.iterdir())


def test_the_phrase_binds_the_channel_and_the_sweep_and_must_be_exact():
    phrase = confirmation_phrase("local-ollama", "stage1-20261030T000000Z-ab12")
    assert phrase == "approve local-ollama stage1-20261030T000000Z-ab12"
    assert confirm_on_terminal(phrase, stdin=TTY(phrase + "\n"), stdout=TTY())
    assert confirm_on_terminal(phrase, stdin=TTY("  " + phrase + "  \n"), stdout=TTY())
    for typed in ("", "approve", phrase + " now", phrase.upper(), "approve local-ollama stage1-OTHER", phrase[:-1]):
        assert not confirm_on_terminal(phrase, stdin=TTY(typed + "\n"), stdout=TTY())
    assert not confirm_on_terminal(phrase, stdin=io.StringIO(phrase + "\n"), stdout=TTY())
    assert not confirm_on_terminal(phrase, stdin=TTY(phrase + "\n"), stdout=io.StringIO())


# ══ the sweep, with a fake run_task: grant, bridge ordering, teardown ═══════════════

def approved_run(rig, up, monkeypatch, run_task, *, sweep="stage1-20261030T000000Z-aaaa"):
    monkeypatch.setattr(stage1, "new_sweep_id", lambda: sweep)
    out = TTY()
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=rig.probes(),
                   stdin=TTY(confirmation_phrase("local-ollama", sweep) + "\n"), stdout=out, run_task=run_task)
    return code, rig.runs / sweep, out.getvalue()


def events(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "audit.jsonl").read_text().splitlines()]


def test_an_approved_run_builds_the_grant_before_the_sweep_and_audits_both(rig, up, monkeypatch):
    seen = {}

    def fake_run_task(task, k, **kw):
        seen["kw"] = kw
        return type("R", (), {"status": "ok", "modes": {}, "errors": []})()
    code, run_dir, _ = approved_run(rig, up, monkeypatch, fake_run_task)
    ev = [e["event"] for e in events(run_dir)]
    assert code == 1                                                            # no ledger: the fake produced nothing
    assert ev[:5] == ["plan", "approval", "grant", "sweep_start", "run_task_start"] and ev[-1] == "sweep_end"
    grant = next(e for e in events(run_dir) if e["event"] == "grant")
    assert grant["channel"] == "local-ollama" and grant["max_proposer_sessions"] == 3 and grant["max_solver_sessions"] == 4
    assert grant["max_wall_s"] == 2700 and grant["per_session_s"] == 600 and grant["approved_by"]
    kw = seen["kw"]
    assert kw["libraries"] is None and tuple(kw["modes"]) == ("agg",) and kw["selector_prop"] is not None
    assert kw["adapter"].substrate.name == "cognitive" and kw["prop"].attempts == 1
    assert up.recursion.STAGE_ATTEMPTS == 1 and up.recursion.TRANSPORT_ATTEMPTS == 1 and up.config.TRANSPORT_ATTEMPTS == 1
    assert json.loads((run_dir / "report.json").read_text())["real_model_call"] is True
    assert (run_dir / "audit.jsonl").stat().st_mode & 0o777 == 0o600 and (run_dir / "report.json").stat().st_mode & 0o777 == 0o600


def test_teardown_on_an_exception_removes_the_scratch_and_reports(rig, up, monkeypatch):
    made = {}

    def explode(task, k, *, prop, **kw):
        scratch = Path(prop.session.scratch_root)
        (scratch / "s-leftover").mkdir()
        (scratch / "s-leftover" / "f").write_text("x")
        made["scratch"] = scratch
        raise RuntimeError("the orchestration blew up")
    code, run_dir, _ = approved_run(rig, up, monkeypatch, explode)
    assert code == 1 and made["scratch"].parent.name == "stage1-20261030T000000Z-aaaa"
    assert not made["scratch"].parent.exists() and not any(rig.ws.iterdir())
    rep = json.loads((run_dir / "report.json").read_text())
    assert rep["status"] == "failed" and "blew up" in rep["errors"][0] and rep["teardown"]["scratch_left"] is False
    ev = events(run_dir)
    assert [e["event"] for e in ev][-3:] == ["error", "teardown", "sweep_end"] and ev[-3]["type"] == "RuntimeError"


def test_teardown_on_an_interrupt_and_the_exit_code(rig, up, monkeypatch):
    def interrupted(task, k, **kw):
        raise KeyboardInterrupt
    code, run_dir, _ = approved_run(rig, up, monkeypatch, interrupted)
    rep = json.loads((run_dir / "report.json").read_text())
    assert code == 130 and rep["status"] == "aborted" and not any(rig.ws.iterdir())


@pytest.mark.parametrize("edit,why", [
    (lambda g: dataclasses.replace(g, expires_at=time.time() - 5), "expired"),
    (lambda g: dataclasses.replace(g, channel="other-channel"), "other-channel"),
    (lambda g: dataclasses.replace(g, classification="restricted"), "different classification"),
    (lambda g: dataclasses.replace(g, max_solver_sessions=g.max_solver_sessions + 1), "larger than the approved plan"),
    (lambda g: dataclasses.replace(g, max_wall_s=g.max_wall_s + 1), "larger than the approved plan"),
])
def test_an_expired_or_wrong_or_oversized_grant_refuses_before_anything_is_created(rig, up, monkeypatch, edit, why):
    started = []
    ran = []
    monkeypatch.setattr(sr, "HostBridge", lambda *a, **k: started.append(1))
    s = stage1.Settings(profile=channels.load_profile({"COGNITIVE_HARNESS2_CHANNELS_FILE": str(rig.profile)}),
                        cli=str(rig.cli), runs_dir=rig.runs)
    pre = stage1.preflight(s, rig.probes())
    plan = stage1.build_plan(s, pre, stage1.load_surface())
    approval = Approval("tester", "tty", "p")
    grant = edit(stage1.make_grant(plan, sweep_id="stage1-x", approval=approval, per_session_s=600))
    run_dir = rig.runs / "stage1-x"
    code = stage1._approved(s, plan, pre, up, stage1.load_surface(), "stage1-x", run_dir, approval, real=False,
                            run_task=lambda *a, **k: ran.append(1), out=io.StringIO(), grant=grant)
    assert code == 1 and ran == [] and started == []
    assert why in json.loads((run_dir / "report.json").read_text())["errors"][0]
    assert not any(rig.ws.iterdir())


# ══ the command line and the solver's argv ════════════════════════════════════════

def test_the_fixed_opencode_argv_passes_check_proposer_for_the_proposer_and_the_solver(rig, up):
    prof = channels.load_profile({"COGNITIVE_HARNESS2_CHANNELS_FILE": str(rig.profile)})
    ch = prof.channels["local-ollama"]
    base = f"http://{sr.PROXY_ALIAS}:{rig.fake.port}/v1"
    env = cp.session_env("opencode", model=ch.model, base_url=base)
    prompt = cp.OPENCODE_PROMPT
    job = stage1.SolverJob("household/sum-1", "r1", stage1.ss.task_prompt_text("household/sum-1"), str(rig.ws / "w"),
                           ch.model, "/opt/cli/opencode")
    (rig.ws / "w").mkdir()
    for argv in (cp.build_command("opencode", cli_container="/opt/cli/opencode", model=ch.model, prompt=prompt),
                 stage1.solver_argv(job)):
        assert argv[:7] == ("/opt/cli/opencode", "run", "--standalone", "--auto", "--format", "json", "--model")
        assert argv[7] == "ollama/gemma4:12b"
        d = upstream_egress.check_proposer(
            upstream_egress.ProposerConfig(kind="opencode", model=ch.model, base_url=ch.endpoints[0], env=env,
                                           cwd=str(rig.ws / "w"), command=argv),
            profile=prof, channel="local-ollama", zdr="strict", classification="open")
        assert d.allowed, d.reasons


def test_the_solver_gets_a_provider_config_pointing_at_the_proxy_alias_and_no_secret_looking_names(rig, up):
    prof = channels.load_profile({"COGNITIVE_HARNESS2_CHANNELS_FILE": str(rig.profile)})
    runner = sr.ProposerSessionRunner(VERIFIED, IMAGE, prof.channels["local-ollama"], zdr="strict", classification="open",
                                      role="solver", workspace_roots=prof.workspace_roots)
    job = stage1.SolverJob("t", "r", "p", str(rig.ws), "ollama/gemma4:12b", "/opt/cli/opencode")
    env = stage1.solver_env_builder(runner)(job)
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["provider"]["ollama"]["options"]["baseURL"] == f"http://harness2-proxy:{rig.fake.port}/v1"
    assert not any(upstream_egress._is_secret_name(k) for k in env) and "OPENCODE_PERMISSION" not in env
    sr._check_env(env, [])                                                    # what the runner accepts


def test_a_missing_upstream_or_surface_is_not_the_planners_crash(rig, monkeypatch):
    monkeypatch.setattr(stage1, "load_upstream", lambda d: (_ for _ in ()).throw(Stage1Error("the pinned upstream cannot be imported")))
    out = io.StringIO()
    assert run_plan(rig.parse("plan"), environ={}, probes=rig.probes(), out=out) == 2
    assert "cannot be imported" in out.getvalue() and "stage1 run --approve" not in out.getvalue()


# ══ the audit trail ═══════════════════════════════════════════════════════════════

def test_clean_withholds_secret_named_and_prompt_named_fields_and_caps_sizes():
    out = clean({"prompt": "SECRET-PROMPT", "stdout": "x", "env": {"A": "b"}, "api_key": "k", "OPENROUTER_TOKEN": "t",
                 "note": "a" * 1000, "esc": "\x1b[31mred\x1b[0m\x07", "n": float("nan"), "deep": {"a": {"b": {"c": {"d": {"e": 1}}}}},
                 "list": list(range(500)), "ok": 3})
    assert out["prompt"] == out["stdout"] == out["env"] == out["api_key"] == out["OPENROUTER_TOKEN"] == "[withheld]"
    assert len(out["note"]) < 320 and out["esc"] == "red" and out["n"] is None and out["ok"] == 3
    assert len(out["list"]) == 60 and "[deep]" in json.dumps(out["deep"])


def test_the_audit_is_a_private_append_only_jsonl_with_ordered_sequence_numbers(tmp_path):
    a = Audit(tmp_path / "a.jsonl")
    a.emit("one", x=1)
    a.emit("two", prompt="never")
    a.close()
    a.emit("three")                                                            # after close: ignored, not an error
    rows = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert [r["seq"] for r in rows] == [1, 2] and [r["event"] for r in rows] == ["one", "two"] and rows[1]["prompt"] == "[withheld]"
    assert (tmp_path / "a.jsonl").stat().st_mode & 0o777 == 0o600


# ══ the end-to-end selftest, on the real rootless daemon ══════════════════════════

def runtime_entries() -> set[str]:
    return stage1.runtime_entries()


def settle(before: set[str]) -> set[str]:
    """Entries that appeared during a test and are still there after a while (other agents' sessions come and go)."""
    deadline = time.monotonic() + 30
    new = runtime_entries() - before
    while new and time.monotonic() < deadline:
        time.sleep(1)
        new = runtime_entries() - before
    return new


def labelled(verified, session_ids) -> list[str]:
    return stage1.session_leftovers(verified, session_ids)


class RecordingBridge:
    """Wraps the real HostBridge; notes what the audit trail held at the moment each bridge started."""

    def __init__(self, base_cls, runs_glob):
        self.base_cls, self.runs_glob, self.started_after = base_cls, runs_glob, []

    def make(self):
        outer = self

        class Recording(self.base_cls):
            def __enter__(inner):
                files = list(outer.runs_glob().glob("*/runs/*/audit.jsonl"))
                outer.started_after.append([json.loads(x)["event"] for x in files[0].read_text().splitlines()] if files else None)
                return super().__enter__()
        return Recording


@pytest.mark.requires_rootless_docker
def test_the_selftest_runs_the_whole_pipeline_with_clean_teardown(tmp_path, verified, proposer_image, up, monkeypatch):
    before = runtime_entries()
    rec = RecordingBridge(sr.HostBridge, lambda: tmp_path / "st")
    monkeypatch.setattr(sr, "HostBridge", rec.make())
    (tmp_path / "st").mkdir()
    for d in (tmp_path / "st",):
        d.chmod(0o755)
    outcome = run_selftest(base=tmp_path / "st", out=io.StringIO())
    r = outcome.report
    assert selftest_problems(outcome) == [], r
    assert (r["ledger"]["P"], r["ledger"]["R"]) == (3, 4) and r["budget_identity"]["ok"] and r["real_model_call"] is False
    assert r["sessions"]["counts"] == {"proposer": 3, "solver": 4, "total": 7}
    assert sorted(x["label"] for x in r["rollouts"]) == ["agg1", "base1", "dom1", "inv1"]
    assert all(x["passed"] is True and x["n_passed"] == 3 and x["timed_out"] is False for x in r["rollouts"])
    assert [s["label"] for s in r["stages"]] == ["inv1", "dom1", "agg1"] and not any(s["fell_back"] for s in r["stages"])
    assert r["_fake_endpoint_hits"] >= 1                                       # the preflight asked it for its models

    run_dir = Path(r["_run_dir"])
    ev = events(run_dir)
    names = [e["event"] for e in ev]
    assert names.index("approval") < names.index("grant") < names.index("sweep_start") < names.index("session_start")
    assert names.count("session_start") == names.count("session_end") == 7 and names.count("rollout_verdict") == 4
    assert names.count("session_reserved") == 7 and "session_refused" not in names
    assert all(e["exit_code"] == 0 and e["timed_out"] is False and e["leftovers"] == 0 for e in ev if e["event"] == "session_end")
    assert all(e["timeout_s"] <= 120 for e in ev if e["event"] == "session_start")
    # the host bridge: one per session, each started only after the approval and the grant were on record
    assert len(rec.started_after) == 7 and all(x and "approval" in x and "grant" in x for x in rec.started_after)
    # nothing sensitive in the audit trail or the report
    blob = (run_dir / "audit.jsonl").read_text() + (run_dir / "report.json").read_text()
    for s in SENTINELS:
        assert s not in blob
    assert all(k not in e for e in ev for k in ("prompt", "argv", "stdout", "mission"))
    # no leftovers on the daemon, in the runtime directory, or in the workspace root
    ids = [e["session"] for e in ev if e["event"] == "session_start"]
    assert len(set(ids)) == 7 and labelled(verified, ids) == []
    assert settle(before) == set()
    assert not list((tmp_path / "st").glob("*/workspaces/*"))


@pytest.mark.requires_rootless_docker
def test_a_session_timeout_kills_the_container_and_the_sweep_ends_clean(tmp_path, verified, proposer_image, up):
    before = runtime_entries()
    (tmp_path / "st").mkdir()
    t0 = time.monotonic()
    outcome = run_selftest(base=tmp_path / "st", out=io.StringIO(), cli_text="#!/bin/bash\nsleep 600\n", per_session_s=3)
    r = outcome.report
    assert time.monotonic() - t0 < 120
    ev = events(Path(r["_run_dir"]))
    ends = [e for e in ev if e["event"] == "session_end"]
    assert ends and ends[0]["timed_out"] is True and ends[0]["leftovers"] == 0 and ends[0]["duration_s"] < 30
    assert r["status"].startswith("base-failed") and not r["teardown"]["docker_leftovers"] and not r["teardown"]["scratch_left"]
    assert labelled(verified, [e["session"] for e in ev if e["event"] == "session_start"]) == []
    assert settle(before) == set() and not list((tmp_path / "st").glob("*/workspaces/*"))
    assert any(not x["ok"] for x in r["rollouts"])


@pytest.mark.requires_rootless_docker
def test_budget_exhaustion_stops_further_sessions(tmp_path, verified, proposer_image, up):
    before = runtime_entries()
    (tmp_path / "st").mkdir()
    outcome = run_selftest(base=tmp_path / "st", out=io.StringIO(),
                           grant_edit=lambda g: dataclasses.replace(g, max_solver_sessions=1))
    r = outcome.report
    ev = events(Path(r["_run_dir"]))
    assert r["sessions"]["counts"]["solver"] == 1 and r["sessions"]["counts"]["proposer"] == 3
    refused = [e for e in ev if e["event"] == "session_refused"]
    assert len(refused) == 3 and {e["reason"] for e in refused} <= {"budget:role-sessions", "budget:sessions"}      # the solver cap, then the total cap
    assert sum(1 for x in r["rollouts"] if x["ok"]) == 1 and sum(1 for e in ev if e["event"] == "session_start" and e["role"] == "solver") == 1
    assert labelled(verified, [e["session"] for e in ev if e["event"] == "session_start"]) == [] and settle(before) == set()


@pytest.mark.requires_rootless_docker
def test_a_model_endpoint_that_does_not_answer_fails_the_sessions_not_the_teardown(tmp_path, verified, proposer_image, up):
    before = runtime_entries()
    (tmp_path / "st").mkdir()
    # a CLI that reports the bridge dead: exits 3 with no events, like a real opencode that cannot reach its model
    outcome = run_selftest(base=tmp_path / "st", out=io.StringIO(), cli_text="#!/bin/bash\necho 'cannot reach the model' >&2\nexit 3\n")
    r = outcome.report
    assert r["status"].startswith("base-failed") and r["rollouts"][0]["ok"] is False
    assert r["teardown"]["docker_leftovers"] == [] and settle(before) == set()


@pytest.mark.requires_rootless_docker
def test_plan_with_the_real_daemon_starts_nothing(tmp_path, verified, proposer_image, up):
    with FakeOllama() as fake:
        rig = Rig(tmp_path, fake)
        before_c = subprocess.run([verified.path, "-H", verified.host, "ps", "-a", "-q", "--filter", f"label={sr.LABEL}"],
                                  capture_output=True, text=True, env=sandbox._pinned_env()).stdout.split()
        before_n = subprocess.run([verified.path, "-H", verified.host, "network", "ls", "-q", "--filter", f"label={sr.LABEL}"],
                                  capture_output=True, text=True, env=sandbox._pinned_env()).stdout.split()
        entries = runtime_entries()
        before_tree = tree(rig.tmp)
        out = io.StringIO()
        code = run_plan(rig.parse("plan"), environ={}, out=out)                  # the REAL docker gate and image check
        text = out.getvalue()
        assert code == 0 and "[ok] docker" in text and "rootless Docker verified" in text and "READY." in text
        assert tree(rig.tmp) == before_tree and not rig.runs.exists() and not any(rig.ws.iterdir())
        after_c = subprocess.run([verified.path, "-H", verified.host, "ps", "-a", "-q", "--filter", f"label={sr.LABEL}"],
                                 capture_output=True, text=True, env=sandbox._pinned_env()).stdout.split()
        after_n = subprocess.run([verified.path, "-H", verified.host, "network", "ls", "-q", "--filter", f"label={sr.LABEL}"],
                                 capture_output=True, text=True, env=sandbox._pinned_env()).stdout.split()
        assert not (set(after_c) - set(before_c)) and not (set(after_n) - set(before_n)) and settle(entries) == set()
        assert fake.hits == 1                                                   # exactly one GET /api/tags


def test_every_solver_command_line_is_vetted_by_check_proposer_before_it_can_start(rig, up):
    s = stage1.Settings(profile=channels.load_profile({"COGNITIVE_HARNESS2_CHANNELS_FILE": str(rig.profile)}), cli=str(rig.cli))
    runner = sr.ProposerSessionRunner(VERIFIED, IMAGE, s.ch, zdr="strict", classification="open", role="solver",
                                      workspace_roots=s.profile.workspace_roots)
    build = stage1.checked_solver_argv(s, stage1.solver_env_builder(runner))
    (rig.ws / "w").mkdir()
    job = stage1.SolverJob("household/sum-1", "r1", stage1.ss.task_prompt_text("household/sum-1"), str(rig.ws / "w"),
                           s.ch.model, "/opt/cli/opencode")
    argv = build(job)
    assert argv[-1] == " ".join(job.prompt.split()) and "\n" not in "".join(argv)
    with pytest.raises(Stage1Error, match="refused before anything started"):
        build(dataclasses.replace(job, prompt="x " * 4000))                  # too long for the gate
    with pytest.raises(Stage1Error, match="refused before anything started"):
        build(dataclasses.replace(job, workspace="/etc"))                    # not a workspace under a declared root


@pytest.mark.requires_rootless_docker
def test_the_selftest_subcommand_passes_on_the_real_daemon_and_removes_its_scratch(verified, proposer_image, up, capsys):
    import tempfile
    tmp = Path(tempfile.gettempdir())
    before = {p.name for p in tmp.glob("h2-stage1-selftest-*")}
    code = stage1.main(["selftest"])
    out = capsys.readouterr()
    assert code == 0 and "selftest passed: P=3 R=4, rollouts 4/4 correct, teardown clean: True" in out.out, out
    assert {p.name for p in tmp.glob("h2-stage1-selftest-*")} <= before


def test_the_plan_does_not_alarm_with_host_session_warnings_that_do_not_apply_to_a_container(rig, up):
    out = io.StringIO()
    assert run_plan(rig.parse("plan"), environ={}, probes=rig.probes(), out=out) == 0
    text = out.getvalue()
    assert "on this host in its working directory" not in text and "gcloud ADC" not in text
    assert "tmpfs HOME and no credential" in text and "max_cost_or_tokens" not in text
