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
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import h2_isolation as iso
import pytest
from h2_upstream import isolated_upstream

from cognitive_harness2 import channels, contained_proposer as cp, sandbox, sbx_session, session_runner as sr, stage1, upstream_egress
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


class HumanTTY(io.StringIO):
    """A controlling terminal with a person at it: it types back exactly what the prompt shows (the phrase and its fresh
    nonce), or `typed` when given."""

    def __init__(self, typed: str | None = None):
        super().__init__()
        self.typed = typed

    def isatty(self) -> bool:
        return True

    def readline(self, *a) -> str:
        if self.typed is not None:
            return self.typed + "\n"
        shown = [ln.strip() for ln in self.getvalue().splitlines() if ln.startswith("  approve ")]
        return shown[-1] + "\n"


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

    def probes(self, *, gate=None, resolve=None, ollama=None, model_runtime=None, environ=None) -> Probes:
        def count(name, fn):
            def wrapped(*a):
                self.calls[name] += 1
                return fn(*a)
            return wrapped
        return Probes(gate=count("gate", gate or (lambda: VERIFIED)), resolve=count("resolve", resolve or (lambda v: IMAGE)),
                      ollama=count("ollama", ollama or stage1.probe_ollama), environ=dict(environ or {}), which=lambda n: None,
                      **({"model_runtime": model_runtime} if model_runtime else {}))

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


def test_a_run_sweeps_stale_sbx_sandboxes_before_the_preflight_can_refuse_it(rig, up, monkeypatch, capsys):
    """A sandbox leaked by a hard-killed run holds a running VM and a read-write mount. Whatever the model runtime is, and
    even when this run's preflight refuses, the next run removes it first."""
    Traps(monkeypatch, rig.fake.port)
    order: list[str] = []

    def boom_gate():
        order.append("preflight")
        raise sandbox.SandboxUnavailable("not rootless")

    probes = rig.probes(gate=boom_gate)
    probes.sweep_stale = lambda environ, which: order.append("sweep") or ["sandbox h2m-0-1-aabbccddeeff"]
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=probes,
                   stdin=TTY("approve local-ollama anything\n"), stdout=TTY())
    err = capsys.readouterr().err
    assert code == 2 and order == ["sweep", "preflight"]
    assert "removed stale sbx leftover(s): sandbox h2m-0-1-aabbccddeeff" in err
    assert not rig.runs.exists()


def test_a_sweep_that_breaks_does_not_stop_a_run(rig, up, monkeypatch, capsys):
    Traps(monkeypatch, rig.fake.port)

    def broken(environ, which):
        raise RuntimeError("sbx is on fire")

    probes = rig.probes(gate=lambda: (_ for _ in ()).throw(sandbox.SandboxUnavailable("not rootless")))
    probes.sweep_stale = broken
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=probes,
                   stdin=TTY("approve local-ollama anything\n"), stdout=TTY())
    assert code == 2 and "not rootless" in capsys.readouterr().err        # the preflight still ran and refused for its own reason


def test_the_plan_and_the_selftest_remove_nothing(rig):
    calls: list[int] = []
    probes = rig.probes()
    probes.sweep_stale = lambda environ, which: calls.append(1) or []
    run_plan(rig.parse("plan"), environ={}, probes=probes, out=io.StringIO())
    assert calls == []                                                    # `plan` is read-only


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
    term = HumanTTY("yes")
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=rig.probes(),
                   stdin=TTY(), stdout=TTY(), tty=term)
    assert code == 2 and "phrase was not typed exactly" in capsys.readouterr().err
    assert "approve local-ollama stage1-" in term.getvalue() and not rig.runs.exists() and not any(rig.ws.iterdir())


def test_the_phrase_binds_the_channel_and_the_sweep_and_must_be_exact():
    phrase = confirmation_phrase("local-ollama", "stage1-20261030T000000Z-ab12")
    assert phrase == "approve local-ollama stage1-20261030T000000Z-ab12"
    n = "amber birch cobalt delta"
    ask = lambda typed, **kw: confirm_on_terminal(phrase, stdin=TTY(), stdout=TTY(), environ={}, nonce=n,   # noqa: E731
                                                  tty=HumanTTY(typed), **kw)
    assert ask(f"{phrase} {n}") and ask(f"  {phrase} {n}  ")
    for typed in ("", "approve", phrase, phrase + " now", f"{phrase} {n} now", f"{phrase} amber birch cobalt", f"{phrase} {n.upper()}",
                  f"{phrase} amber birch cobalt echo", f"{phrase.upper()} {n}", f"approve local-ollama stage1-OTHER {n}",
                  f"{phrase} {n}"[:-1]):
        assert not ask(typed), typed
    assert not confirm_on_terminal(phrase, stdin=io.StringIO("x\n"), stdout=TTY(), environ={}, tty=HumanTTY(f"{phrase} {n}"), nonce=n)
    assert not confirm_on_terminal(phrase, stdin=TTY(), stdout=io.StringIO(), environ={}, tty=HumanTTY(f"{phrase} {n}"), nonce=n)


def test_the_nonce_is_four_words_shown_on_the_terminal_and_fresh_each_time():
    phrase = confirmation_phrase("local-ollama", "stage1-x")
    shown = []
    for _ in range(6):
        t = HumanTTY("nothing")
        assert not confirm_on_terminal(phrase, stdin=TTY(), stdout=TTY(), environ={}, tty=t)
        line = next(ln.strip() for ln in t.getvalue().splitlines() if ln.startswith("  approve "))
        assert line.startswith(phrase + " ")
        words = line[len(phrase):].split()
        assert len(words) == 4 and all(w in stage1.NONCE_WORDS for w in words)
        shown.append(tuple(words))
    assert len(set(shown)) > 1                                                       # not a constant


def test_the_phrase_without_the_nonce_never_approves_even_when_the_stored_phrase_is_known():
    phrase = confirmation_phrase("local-ollama", "stage1-x")
    assert not confirm_on_terminal(phrase, stdin=TTY(), stdout=TTY(), environ={}, tty=HumanTTY(phrase))


def _assert_hint(text: str, name: str) -> None:
    """A user-set marker (CODEX_SANDBOX*, ANTIGRAVITY*, ...) gets the `env -u NAME` hint; a Claude Code runtime variable never does
    (that command would be a ready-made bypass for the very agent that triggered the refusal): it is told to use its own terminal."""
    if name in stage1.CLAUDE_RUNTIME_ENV:
        assert "env -u" not in text and f"-u {name}" not in text and "own terminal" in text
    else:
        assert f"env -u {name} " in text                                          # how to run it from a plain terminal


@pytest.mark.parametrize("name", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_JOB_DIR",
                                  "ANTIGRAVITY_AGENT", "GEMINI_CLI",
                                  "GEMINI_CLI_HOME", "AGY_SESSION", "OPENCODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"])
def test_an_agent_harness_marker_refuses_the_approval_and_nothing_overrides_it(rig, up, monkeypatch, capsys, name):
    phrase = confirmation_phrase("local-ollama", "stage1-x")
    with pytest.raises(stage1.ApprovalRefused) as ei:
        confirm_on_terminal(phrase, stdin=TTY(), stdout=TTY(), environ={name: "1"}, tty=HumanTTY())
    assert name in str(ei.value) and "no flag" in str(ei.value)
    _assert_hint(str(ei.value), name)
    Traps(monkeypatch, rig.fake.port)
    term = HumanTTY()
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ={name: "1"}, probes=rig.probes(),
                   stdin=TTY(), stdout=TTY(), tty=term)
    err = capsys.readouterr().err
    assert code == 2 and name in err and "agent-harness environment" in err and "nothing was started" in err
    _assert_hint(err, name)
    assert term.getvalue() == "" and rig.calls == {"gate": 0, "resolve": 0, "ollama": 0} and not rig.runs.exists()


@pytest.mark.parametrize("name", ["OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_API_KEY", "OPENCODE_DISABLE_AUTOUPDATE", "CODEX_HOME",
                                  "CODEX_API_KEY", "CLAUDE_CONFIG_DIR", "GEMINI_API_KEY", "AGYX", "ANTIGRAV"])
def test_a_variable_a_person_sets_in_their_own_profile_is_not_an_agent_marker(name):
    """The review's input: `CODEX_HOME=~/.codex stage1 run ...` from a plain shell was refused for good. These are settings
    the tools READ (their config directory, a key, a switch), not markers they SET in the processes they spawn."""
    assert stage1.agent_markers({name: "1"}) == []
    phrase = confirmation_phrase("local-ollama", "stage1-x")
    assert confirm_on_terminal(phrase, stdin=TTY(), stdout=TTY(), environ={name: "1"}, tty=HumanTTY())


def test_the_refusal_names_every_offending_variable_and_the_env_u_command_only_for_the_user_set_ones():
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": "s", "HOME": "/h", "OPENCODE_CONFIG": "/x",
           "CODEX_SANDBOX": "1", "ANTIGRAVITY_AGENT": "1"}
    with pytest.raises(stage1.ApprovalRefused) as ei:
        stage1.refuse_in_agent_environment(env)
    text = str(ei.value)
    for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CODEX_SANDBOX", "ANTIGRAVITY_AGENT"):
        assert name in text
    assert "OPENCODE_CONFIG" not in text and "HOME" not in text and "/x" not in text            # a value is never shown
    assert "env -u ANTIGRAVITY_AGENT -u CODEX_SANDBOX stage1 run" in text                       # only the user-set ones
    assert "-u CLAUDE" not in text and "own terminal" in text and "no flag" in text


def test_the_refusal_prints_no_env_u_for_the_claude_code_runtime_variables_at_all():
    env = {n: "1" for n in stage1.CLAUDE_RUNTIME_ENV}
    with pytest.raises(stage1.ApprovalRefused) as ei:
        stage1.refuse_in_agent_environment(env)
    text = str(ei.value)
    assert "env -u" not in text and " -u " not in text and "own terminal" in text and "no flag" in text
    assert all(n in text or "more" in text for n in stage1.CLAUDE_RUNTIME_ENV)


def test_claude_pid_and_claude_job_dir_are_agent_markers_and_the_assumption_is_documented():
    assert {"CLAUDE_PID", "CLAUDE_JOB_DIR"} <= set(stage1.AGENT_ENV_EXACT) and {"CLAUDE_PID", "CLAUDE_JOB_DIR"} <= set(stage1.CLAUDE_RUNTIME_ENV)
    assert stage1.agent_markers({"CLAUDE_PID": "1", "CLAUDE_JOB_DIR": "/x", "HOME": "/h"}) == ["CLAUDE_JOB_DIR", "CLAUDE_PID"]
    assert set(stage1.CLAUDE_RUNTIME_ENV) <= set(stage1.AGENT_ENV_EXACT) and "OPENCODE" not in stage1.CLAUDE_RUNTIME_ENV
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert "CLAUDE_PID" in readme and "CLAUDE_JOB_DIR" in readme and "assum" in readme
    many = {f"CODEX_SANDBOX_V{i:02d}": "1" for i in range(40)}
    with pytest.raises(stage1.ApprovalRefused) as ei:
        stage1.refuse_in_agent_environment(many)
    assert "and 28 more" in str(ei.value) and len(str(ei.value)) < 1200                     # bounded


def test_the_marker_lists_are_exact_names_and_the_prefixes_the_repo_can_justify():
    """The CLAUDE_CODE_ runtime variables were observed in a child of Claude Code (CLAUDECODE, CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_SESSION_ID, ...); the
    rest are kept as prefixes only because they cannot be verified here. OPENCODE_* and CODEX_* (config knobs) are not prefixes."""
    assert "CLAUDECODE" in stage1.AGENT_ENV_EXACT and "OPENCODE" in stage1.AGENT_ENV_EXACT
    assert "OPENCODE_" not in stage1.AGENT_ENV_PREFIXES and "OPENCODE" not in stage1.AGENT_ENV_PREFIXES
    assert "CODEX" not in stage1.AGENT_ENV_PREFIXES and "CODEX_" not in stage1.AGENT_ENV_PREFIXES
    assert "CLAUDE_CODE_" not in stage1.AGENT_ENV_PREFIXES and "CLAUDE_CODE_ENTRYPOINT" in stage1.AGENT_ENV_EXACT   # not the namespace of user settings
    for user_setting in ("CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_BEDROCK"):
        assert stage1.agent_markers({user_setting: "1", "PATH": "/bin"}) == []              # a person's own setting never refuses a real terminal


def test_the_audit_records_what_was_typed_and_which_agent_markers_were_checked_and_found(rig, up, monkeypatch):
    sweep = "stage1-20261030T000000Z-bbbb"
    code, run_dir, _ = approved_run(rig, up, monkeypatch, lambda task, k, **kw: type("R", (), {"status": "ok", "modes": {}, "errors": []})(),
                                    sweep=sweep)
    ap = next(e for e in events(run_dir) if e["event"] == "approval")
    phrase = confirmation_phrase("local-ollama", sweep)
    assert ap["typed"].startswith(phrase + " ") and len(ap["typed"][len(phrase):].split()) == 4        # the nonce-bearing string
    assert all(w in stage1.NONCE_WORDS for w in ap["typed"][len(phrase):].split())
    assert ap["agent_env"] == {"checked_exact": list(stage1.AGENT_ENV_EXACT), "checked_prefixes": list(stage1.AGENT_ENV_PREFIXES),
                               "present": []}


def test_the_agent_env_record_lists_the_names_present_and_never_a_value():
    rec = stage1.agent_env_record({"CLAUDECODE": "secret-value", "CLAUDE_CODE_SESSION_ID": "x", "PATH": "/bin"})
    assert rec["present"] == ["CLAUDECODE", "CLAUDE_CODE_SESSION_ID"] and "secret-value" not in json.dumps(rec)
    assert stage1.agent_env_record({})["present"] == []


def test_no_flag_can_override_the_agent_marker_refusal():
    help_text = build_parser().format_help() + "".join(a.format_help() for a in
                                                       [sp for sp in build_parser()._subparsers._group_actions[0].choices.values()])
    assert not re.search(r"--(force|allow-agent|override|i-am-human|ignore-env)", help_text)


def test_a_process_with_no_controlling_terminal_refuses_and_stdin_is_never_read(tmp_path):
    class Stdin(TTY):
        def readline(self, *a):
            raise AssertionError("the confirmation must not be read from stdin")
    with pytest.raises(stage1.ApprovalRefused, match="controlling terminal"):
        confirm_on_terminal("approve a b", stdin=Stdin("approve a b\n"), stdout=TTY(), environ={}, tty_path=str(tmp_path / "no-tty"))


def test_a_controlling_terminal_that_is_not_a_terminal_refuses(tmp_path):
    notty = tmp_path / "file"
    notty.write_text("")
    with pytest.raises(stage1.ApprovalRefused, match="not a terminal"):
        confirm_on_terminal("approve a b", stdin=TTY(), stdout=TTY(), environ={}, tty_path=str(notty))


def test_the_confirmation_is_read_from_the_terminal_stream_not_from_stdin():
    class Stdin(TTY):
        def readline(self, *a):
            raise AssertionError("read stdin")
    phrase = "approve local-ollama stage1-x"
    assert confirm_on_terminal(phrase, stdin=Stdin(), stdout=TTY(), environ={}, tty=HumanTTY())


# ══ the sweep, with a fake run_task: grant, bridge ordering, teardown ═══════════════

def approved_run(rig, up, monkeypatch, run_task, *, sweep="stage1-20261030T000000Z-aaaa"):
    monkeypatch.setattr(stage1, "new_sweep_id", lambda: sweep)
    out = TTY()
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=rig.probes(),
                   stdin=TTY(), stdout=out, tty=HumanTTY(), run_task=run_task)
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
    rep = json.loads((run_dir / "report.json").read_text())
    # approved for a real model, but this fake run_task started no session: nothing reached a model, and the report says so
    assert rep["approved_for_real_model"] is True and rep["real_model_call"] is False
    assert rep["cli_sessions_started"] == 0 and rep["model_sessions_reached"] == 0 and rep["sessions_summary"]["started"] == 0
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
    assert cfg["provider"]["ollama"]["options"]["baseURL"] == f"http://harness2-proxy.internal:{rig.fake.port}/v1"
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
    """Entries of THIS run (stage1.runtime_entries is scoped by the run id) that appeared during a test and are still there after a while."""
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
    outcome = run_selftest(base=tmp_path / "st", out=(printed := io.StringIO()))
    r = outcome.report
    assert selftest_problems(outcome) == [], r
    assert r["approved_for_real_model"] is False and r["real_model_call"] is False                # a fake endpoint is not a real model
    assert (r["cli_sessions_started"], r["model_sessions_reached"]) == (7, 7)                     # all seven reached the (fake) endpoint
    assert r["sessions_summary"]["exit_codes"] == {"0": 7} and r["sessions_summary"]["denied_hosts"] == {}
    assert "outcome   7 sessions started, 7 reached the model, all exited rc=0" in printed.getvalue()
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
        before_res = iso.run_resources(verified)                                # THIS run's containers and networks
        entries = runtime_entries()
        before_tree = tree(rig.tmp)
        out = io.StringIO()
        code = run_plan(rig.parse("plan"), environ={}, out=out)                  # the REAL docker gate and image check
        text = out.getvalue()
        assert code == 0 and "[ok] docker" in text and "rootless Docker verified" in text and "READY." in text
        assert tree(rig.tmp) == before_tree and not rig.runs.exists() and not any(rig.ws.iterdir())
        assert not (iso.run_resources(verified) - before_res) and settle(entries) == set()
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
def test_the_selftest_subcommand_passes_on_the_real_daemon_and_removes_its_scratch(verified, proposer_image, up, capsys, tmp_path, monkeypatch):
    # The selftest makes its scratch under the temp directory. Point that at a private one, so the "nothing is left" check
    # cannot be tripped by another process (a second test run, a real sweep) making its own scratch in the shared /tmp.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    code = stage1.main(["selftest"])
    out = capsys.readouterr()
    assert code == 0 and "selftest passed: P=3 R=4, rollouts 4/4 correct, teardown clean: True" in out.out, out
    assert list(tmp_path.glob("h2-stage1-selftest-*")) == []


def test_the_plan_does_not_alarm_with_host_session_warnings_that_do_not_apply_to_a_container(rig, up):
    out = io.StringIO()
    assert run_plan(rig.parse("plan"), environ={}, probes=rig.probes(), out=out) == 0
    text = out.getvalue()
    assert "on this host in its working directory" not in text and "gcloud ADC" not in text
    assert "tmpfs HOME and no credential" in text and "max_cost_or_tokens" not in text


@pytest.mark.requires_rootless_docker
def test_the_selftest_catches_a_runner_that_leaves_no_proxy_empty(tmp_path, verified, proposer_image, up, monkeypatch):
    """The first real run (2026-10-30) died because opencode's own loopback server was sent to the proxy. The fake
    opencode now does what the real one does, so the pre-fix runner (empty NO_PROXY) fails the selftest."""
    before = runtime_entries()
    (tmp_path / "st").mkdir()
    monkeypatch.setattr(sr, "SESSION_NO_PROXY", "")
    outcome = run_selftest(base=tmp_path / "st", out=io.StringIO())
    r = outcome.report
    assert selftest_problems(outcome) and r["rollouts_passed"] == 0
    first = next(e for e in events(Path(r["_run_dir"])) if e["event"] == "session_end")
    assert first["exit_code"] == 3
    # the report says what happened: sessions started, none reached the model, the proxy denied the CLI's own loopback
    sv = r["sessions_summary"]
    assert sv["started"] >= 1 and sv["reached_model"] == 0 and sv["exit_codes"] == {"3": sv["started"]}
    assert sv["denied_hosts"] and all(h.startswith("127.0.0.1:") for h in sv["denied_hosts"]) and sv["denied_reasons"] == ["not-allow-listed"]
    assert r["real_model_call"] is False and r["model_sessions_reached"] == 0 and r["cli_sessions_started"] == sv["started"]
    assert first["model_requests"] == 0 and first["denied_hosts"] and first["denied_hosts"][0] in sv["denied_hosts"]
    assert r["teardown"]["docker_leftovers"] == [] and settle(before) == set()


# ── what the report says about the sessions (first real run: "real_model_call": true although no model was reached) ──

def _end(model_requests=0, exit_code=0, denied=(), reasons=(), timed_out=False):
    return {"event": "session_end", "exit_code": exit_code, "timed_out": timed_out, "model_requests": model_requests,
            "denied_hosts": list(denied), "denied_reasons": list(reasons), "egress_denied": len(denied)}


FIRST_RUN_ENDS = [_end(0, 1, ["127.0.0.1:34175"], ["not-allow-listed"]) for _ in range(7)]


def test_sessions_view_of_the_first_real_run_says_seven_started_none_reached_the_model():
    v = stage1.sessions_view([{"event": "session_start"}] * 7, FIRST_RUN_ENDS,
                             rollout_errors=["UnexpectedStatus: 403"], stage_errors=["[inv1.a1] rc=1: empty reply"])
    assert v["started"] == 7 and v["reached_model"] == 0 and v["exit_codes"] == {"1": 7} and v["timed_out"] == 0
    assert v["denied_hosts"] == {"127.0.0.1:34175": 7} and v["denied_reasons"] == ["not-allow-listed"]
    assert v["first_error"] == "UnexpectedStatus: 403"
    ok = stage1.sessions_view([{"event": "session_start"}] * 2, [_end(3), _end(1)], rollout_errors=[], stage_errors=[])
    assert ok["reached_model"] == 2 and ok["exit_codes"] == {"0": 2} and ok["denied_hosts"] == {} and ok["first_error"] is None
    assert stage1.sessions_view([], [], rollout_errors=[], stage_errors=["x"])["first_error"] == "x"


def test_the_summary_line_names_what_happened_in_plain_words():
    v = stage1.sessions_view([{"event": "session_start"}] * 7, FIRST_RUN_ENDS, rollout_errors=["UnexpectedStatus: 403"], stage_errors=[])
    text = "\n".join(stage1.sessions_lines({"sessions_summary": v}))
    assert ("7 sessions started, 0 reached the model: 7 exited rc=1; first error: UnexpectedStatus: 403; "
            "proxy denied: 127.0.0.1:34175 (7 sessions, not-allow-listed)") in text.replace("outcome   ", "")
    assert "no session reached the model" in text
    good = stage1.sessions_view([{"event": "session_start"}] * 7, [_end(2)] * 7, rollout_errors=[], stage_errors=[])
    line = "\n".join(stage1.sessions_lines({"sessions_summary": good}))
    assert line.strip() == "outcome   7 sessions started, 7 reached the model, all exited rc=0"


def test_the_summary_caps_how_many_denied_hosts_it_prints_and_never_prints_a_url_or_body():
    ends = [_end(1, 0, [f"h{i}.example:443" for i in range(10)], ["not-allow-listed"])]
    v = stage1.sessions_view([{"event": "session_start"}], ends, rollout_errors=[], stage_errors=[])
    text = "\n".join(stage1.sessions_lines({"sessions_summary": v}))
    assert "h0.example:443" in text and "+5 more" in text and "http" not in text
    mixed = stage1.sessions_view([{"event": "session_start"}] * 3, [_end(1, 0), _end(0, None, timed_out=True), _end(1, 2)],
                                 rollout_errors=[], stage_errors=[])
    assert mixed["timed_out"] == 1 and mixed["exit_codes"] == {"0": 1, "2": 1, "none": 1}
    assert "1 timed out" in "\n".join(stage1.sessions_lines({"sessions_summary": mixed}))


# ── second real run: every session reached ollama, ollama answered 403 (its Host guard), the proxy denied nothing ──

SECOND_RUN_ERR = "provider.auth: Provider request failed with HTTP 403"


def test_the_summary_tells_a_model_endpoint_refusal_from_a_proxy_denial_at_a_glance():
    v = stage1.sessions_view([{"event": "session_start"}] * 7, [_end(2, 1) for _ in range(7)], rollout_errors=[SECOND_RUN_ERR], stage_errors=[])
    assert v["reached_model"] == 7 and v["denied_hosts"] == {} and v["first_error_http"] == 403
    text = "\n".join(stage1.sessions_lines({"sessions_summary": v}))
    assert ("7 sessions started, 7 reached the model: 7 exited rc=1; first error: " + SECOND_RUN_ERR +
            "; the proxy denied nothing, the model endpoint itself refused (HTTP 403)") in text.replace("outcome   ", "")
    assert "no session reached the model" not in text and "proxy denied:" not in text
    assert "Host" in text and "Origin" in text                      # for ollama a 403 is its Host/Origin guard: say where to look


def test_a_401_is_a_model_endpoint_refusal_too_and_other_errors_are_not_labelled():
    v = stage1.sessions_view([{"event": "session_start"}], [_end(1, 1)], rollout_errors=["provider.auth: HTTP 401 Unauthorized"], stage_errors=[])
    assert v["first_error_http"] == 401 and "model endpoint itself refused (HTTP 401)" in "\n".join(stage1.sessions_lines({"sessions_summary": v}))
    for err in ("provider.no-route: Unsupported package", "UnexpectedStatus: 403", None):
        w = stage1.sessions_view([{"event": "session_start"}], [_end(1, 1)], rollout_errors=[err] if err else [], stage_errors=[])
        assert w["first_error_http"] is None and "refused" not in "\n".join(stage1.sessions_lines({"sessions_summary": w}))
    h500 = stage1.sessions_view([{"event": "session_start"}], [_end(1, 1)], rollout_errors=["Provider request failed with HTTP 500"], stage_errors=[])
    assert "refused" not in "\n".join(stage1.sessions_lines({"sessions_summary": h500}))


def test_a_403_beside_a_proxy_denial_is_not_blamed_on_the_endpoint():
    v = stage1.sessions_view([{"event": "session_start"}] * 2, [_end(1, 1, ["evil.example:443"], ["not-allow-listed"]), _end(1, 1)],
                             rollout_errors=[SECOND_RUN_ERR], stage_errors=[])
    text = "\n".join(stage1.sessions_lines({"sessions_summary": v}))
    assert "proxy denied: evil.example:443" in text and "the proxy denied nothing" not in text and "itself refused" not in text
    none_reached = stage1.sessions_view([{"event": "session_start"}], [_end(0, 1)], rollout_errors=[SECOND_RUN_ERR], stage_errors=[])
    assert "itself refused" not in "\n".join(stage1.sessions_lines({"sessions_summary": none_reached}))


def test_the_fake_endpoint_refuses_what_real_ollama_refuses_so_the_selftest_would_catch_a_bad_host_name(rig):
    import http.client

    def get(host, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", rig.fake.port, timeout=5)
        c.putrequest("GET", "/api/tags", skip_host=True)
        c.putheader("Host", host)
        if origin:
            c.putheader("Origin", origin)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body
    assert get(f"127.0.0.1:{rig.fake.port}")[0] == 200 and get(f"{sr.PROXY_ALIAS}:{rig.fake.port}")[0] == 200
    assert get("harness2-proxy:11434") == (403, b"") and get("evil.example.com") == (403, b"")
    assert get("127.0.0.1", "http://evil.example.com")[0] == 403
    assert rig.fake.hits == 2 and [s for s, _ in rig.fake.rejected] == [403, 403, 403]


# ══ task sets (stage 1b): `--task-set {easy,hard}` for plan, run and selftest ═══════════

def plan_text(rig, *extra: str) -> str:
    out = io.StringIO()
    assert run_plan(rig.parse("plan", *extra), environ={}, probes=rig.probes(), out=out) == 0, out.getvalue()
    return out.getvalue()


def test_the_default_task_set_is_easy_and_each_set_names_its_own_task(rig):
    s = stage1._settings_from(rig.parse("plan"), {})
    assert (s.task_set, s.task) == ("easy", "household/sum-1") and s.ts.spec_name == "sum-signed"
    h = stage1._settings_from(rig.parse("plan", "--task-set", "hard"), {})
    assert (h.task_set, h.task) == ("hard", "household/hard-1") and h.ts.spec_name == "sum-signed-hard"
    prof = channels.load_profile({"COGNITIVE_HARNESS2_CHANNELS_FILE": str(rig.profile)})
    assert stage1.Settings(profile=prof).task_set == "easy" and stage1.Settings(profile=prof).task == "household/sum-1"
    for bad in (dict(task_set="medium"), dict(task_set="hard", task="household/sum-1"), dict(task="household/hard-1"),
                dict(task_set="hard", task="nope/hard-1")):
        with pytest.raises(Stage1Error):
            stage1.Settings(profile=prof, **bad)


@pytest.mark.parametrize("cmd", ["plan", "run", "selftest"])
def test_the_parser_takes_a_known_task_set_only_and_defaults_to_easy(cmd, capsys):
    assert build_parser().parse_args([cmd]).task_set == "easy"
    assert build_parser().parse_args([cmd, "--task-set", "hard"]).task_set == "hard"
    assert build_parser().parse_args([cmd, "--task-set", "easy"]).task_set == "easy"
    with pytest.raises(SystemExit):
        build_parser().parse_args([cmd, "--task-set", "medium"])
    assert "invalid choice" in capsys.readouterr().err


def test_the_easy_plan_names_its_set_and_its_approve_command_is_unchanged(rig, up):
    text = plan_text(rig)
    assert "task set         easy  (sum-signed:" in text and "task             household/sum-1, k=1" in text
    cmd = next(line.strip() for line in text.splitlines() if "stage1 run --approve" in line)
    assert "--task-set" not in cmd                                           # an old command means what it did
    assert build_parser().parse_args(shlex.split(cmd)[5:]).task_set == "easy"


def test_the_hard_plan_shows_the_set_keeps_the_budgets_and_puts_it_in_the_approve_command(rig, up):
    easy, hard = plan_text(rig), plan_text(rig, "--task-set", "hard")
    for needle in ("task set         hard  (sum-signed-hard: 12 amounts per instance", "task             household/hard-1, k=1",
                   "P=3", "R=4", "sessions 7", "2700 s", "600 s per session", "cost $0", "classification   open", "READY."):
        assert needle in hard, needle
    cmd = next(line.strip() for line in hard.splitlines() if "stage1 run --approve" in line)
    parsed = build_parser().parse_args(shlex.split(cmd)[5:])
    assert parsed.task_set == "hard" and parsed.approve == "local-ollama" and parsed.per_session_s == 600.0
    # nothing else differs: the budgets, caps and checks are the same lines
    only_easy = [x for x in easy.splitlines() if x not in hard.splitlines()]
    only_hard = [x for x in hard.splitlines() if x not in easy.splitlines()]
    assert all(("task" in x or "classification" in x or "proposer hint" in x or "stage1 run --approve" in x) for x in only_easy + only_hard), (only_easy, only_hard)


def test_the_hard_plan_is_as_side_effect_free_as_the_easy_one(rig, up, monkeypatch):
    before = tree(rig.tmp)
    Traps(monkeypatch, rig.fake.port)
    out = io.StringIO()
    assert run_plan(rig.parse("plan", "--task-set", "hard"), environ={}, probes=rig.probes(), out=out) == 0
    assert tree(rig.tmp) == before


def test_a_hard_run_hands_upstream_the_hard_adapter_and_records_the_set(rig, up, monkeypatch):
    seen = {}

    def fake_run_task(task, k, **kw):
        seen.update(task=task, adapter=kw["adapter"])
        return type("R", (), {"status": "ok", "modes": {}, "errors": [], "selections": {}})()
    sweep = "stage1-20261030T000000Z-bbbb"
    monkeypatch.setattr(stage1, "new_sweep_id", lambda: sweep)
    code = run_run(rig.parse("run", "--approve", "local-ollama", "--task-set", "hard"), environ={}, probes=rig.probes(),
                   stdin=TTY(), stdout=TTY(), tty=HumanTTY(), run_task=fake_run_task)
    assert code == 1                                                           # no ledger: the fake started nothing
    assert seen["task"] == "household/hard-1"
    ad = seen["adapter"]
    assert ad.ts.name == "hard" and ad.spec.name == "sum-signed-hard" and ad.tasks()[0] == "household/hard-1"
    assert (ad.base_harness() / "systemprompt.md").read_text() == stage1.ss_hard.BASE_SYSTEM_PROMPT
    run_dir = rig.runs / sweep
    rep = json.loads((run_dir / "report.json").read_text())
    start = next(e for e in events(run_dir) if e["event"] == "sweep_start")
    assert rep["task_set"] == start["task_set"] == "hard" and rep["task"] == start["task"] == "household/hard-1"
    assert rep["budget_identity"]["expected_P"] == 3 and rep["budget_identity"]["expected_R"] == 4
    assert rep["grant"]["max_proposer_sessions"] == 3 and rep["grant"]["max_solver_sessions"] == 4 and rep["k"] == 1
    assert rep["selection"] is None or rep["selection"]["adopted"] is False        # no mode ran


def test_an_easy_run_records_the_set_too_and_its_adapter_is_the_easy_one(rig, up, monkeypatch):
    seen = {}

    def fake_run_task(task, k, **kw):
        seen.update(task=task, adapter=kw["adapter"])
        return type("R", (), {"status": "ok", "modes": {}, "errors": [], "selections": {}})()
    sweep = "stage1-20261030T000000Z-cccc"
    monkeypatch.setattr(stage1, "new_sweep_id", lambda: sweep)
    run_run(rig.parse("run", "--approve", "local-ollama"), environ={}, probes=rig.probes(),
            stdin=TTY(), stdout=TTY(), tty=HumanTTY(), run_task=fake_run_task)
    assert seen["task"] == "household/sum-1" and seen["adapter"].ts is stage1.ss.EASY and seen["adapter"].spec.name == "sum-signed"
    assert json.loads((rig.runs / sweep / "report.json").read_text())["task_set"] == "easy"


# ── the proposer hint (stage 1c): `--proposer-hint {on,off}` ──

def test_the_plan_shows_the_hint_flag_on_for_hard_off_for_easy_and_the_approve_command_spells_it_only_when_given(rig, up):
    easy, hard = plan_text(rig), plan_text(rig, "--task-set", "hard")
    assert "proposer hint    off  (default for the easy set;" in easy and "--proposer-hint on flips it" in easy
    assert "proposer hint    on  (default for the hard set;" in hard and "--proposer-hint off flips it" in hard
    for text in (easy, hard):
        cmd = next(line.strip() for line in text.splitlines() if "stage1 run --approve" in line)
        assert "--proposer-hint" not in cmd                                   # a default is not spelled out
    forced = plan_text(rig, "--task-set", "hard", "--proposer-hint", "off")
    assert "proposer hint    off  (--proposer-hint;" in forced
    cmd = next(line.strip() for line in forced.splitlines() if "stage1 run --approve" in line)
    assert build_parser().parse_args(shlex.split(cmd)[5:]).proposer_hint == "off"


def hint_run(rig, up, monkeypatch, sweep, *extra):
    seen = {}

    def fake_run_task(task, k, **kw):
        seen["prop"], seen["selector"] = kw["prop"], kw["selector_prop"]
        return type("R", (), {"status": "ok", "modes": {}, "errors": [], "selections": {}})()
    monkeypatch.setattr(stage1, "new_sweep_id", lambda: sweep)
    run_run(rig.parse("run", "--approve", "local-ollama", *extra), environ={}, probes=rig.probes(),
            stdin=TTY(), stdout=TTY(), tty=HumanTTY(), run_task=fake_run_task)
    run_dir = rig.runs / sweep
    return seen, json.loads((run_dir / "report.json").read_text()), next(e for e in events(run_dir) if e["event"] == "sweep_start")


def test_a_hard_run_gives_the_proposer_the_block_but_not_the_selector_and_records_it(rig, up, monkeypatch):
    seen, rep, start = hint_run(rig, up, monkeypatch, "stage1-20261030T000000Z-dd01", "--task-set", "hard")
    assert seen["prop"].hint_block.startswith("ENVIRONMENT RULES") and seen["selector"].hint_block == ""
    sha = stage1.proposer_hint.digest(seen["prop"].hint_block)
    assert rep["proposer_hint"] == start["proposer_hint"] == {"on": True, "source": "default for the hard set", "sha": sha}


def test_the_easy_run_has_no_block_by_default_and_off_reproduces_run_4_on_the_hard_set(rig, up, monkeypatch):
    seen, rep, _ = hint_run(rig, up, monkeypatch, "stage1-20261030T000000Z-dd02")
    assert seen["prop"].hint_block == "" and rep["proposer_hint"] == {"on": False, "source": "default for the easy set", "sha": None}
    seen, rep, _ = hint_run(rig, up, monkeypatch, "stage1-20261030T000000Z-dd03", "--task-set", "hard", "--proposer-hint", "off")
    assert seen["prop"].hint_block == "" and rep["proposer_hint"] == {"on": False, "source": "--proposer-hint", "sha": None}
    seen, rep, _ = hint_run(rig, up, monkeypatch, "stage1-20261030T000000Z-dd04", "--proposer-hint", "on")
    assert seen["prop"].hint_block and rep["proposer_hint"]["on"] is True and rep["proposer_hint"]["source"] == "--proposer-hint"


# ── what the proxy refused by method and path (stage-1b: 5-10 unix-route-path-denied per session, unidentified) ──

def _end_dr(denied_requests, **kw):
    e = _end(1, 0, ["harness2-proxy.internal:11434"], ["unix-route-path-denied"], **kw)
    e["denied_requests"] = dict(denied_requests)
    return e


def test_sessions_view_adds_denied_requests_summed_over_sessions_and_omits_it_when_no_session_has_any():
    ends = [_end_dr({"GET /api/version": 5, "HEAD /api/tags": 1}), _end_dr({"GET /api/version": 7}), _end(1, 0)]
    v = stage1.sessions_view([{"event": "session_start"}] * 3, ends, rollout_errors=[], stage_errors=[])
    assert v["denied_requests"] == {"GET /api/version": 12, "HEAD /api/tags": 1}
    assert list(v["denied_requests"]) == ["GET /api/version", "HEAD /api/tags"]                 # most frequent first
    old = stage1.sessions_view([{"event": "session_start"}] * 2, FIRST_RUN_ENDS[:2], rollout_errors=[], stage_errors=[])
    assert "denied_requests" not in old
    junk = stage1.sessions_view([{"event": "session_start"}], [{**_end(1, 0), "denied_requests": "x"}, {**_end(1, 0), "denied_requests": {"a b": "z"}}],
                                rollout_errors=[], stage_errors=[])
    assert "denied_requests" not in junk


def test_sessions_view_keeps_at_most_20_denied_request_keys_and_sums_the_rest_into_other():
    ends = [_end_dr({f"GET /p{i}": 1 for i in range(15)}), _end_dr({f"GET /q{i}": 2 for i in range(15)} | {"other": 4})]
    d = stage1.sessions_view([{"event": "session_start"}] * 2, ends, rollout_errors=[], stage_errors=[])["denied_requests"]
    assert len(d) == 21 and sum(d.values()) == 15 + 30 + 4
    assert set(list(d)[:15]) == {f"GET /q{i}" for i in range(15)} and list(d)[-1] == "other"
    assert d["other"] == 4 + 10                                                           # 10 singles beyond the 20th key
    assert all(" " in k for k in d if k != "other")


def test_the_summary_prints_the_most_frequent_denied_requests_and_how_many_more():
    ends = [_end_dr({"GET /api/version": 9, "HEAD /api/tags": 2, **{f"GET /z{i}": 1 for i in range(6)}})]
    v = stage1.sessions_view([{"event": "session_start"}], ends, rollout_errors=[], stage_errors=[])
    text = "\n".join(stage1.sessions_lines({"sessions_summary": v}))
    assert "denied requests: GET /api/version x9, HEAD /api/tags x2, GET /z0 x1, GET /z1 x1, GET /z2 x1, +3 more" in text
    assert "denied requests" not in "\n".join(stage1.sessions_lines({"sessions_summary": stage1.sessions_view(
        [{"event": "session_start"}], FIRST_RUN_ENDS[:1], rollout_errors=[], stage_errors=[])}))


def test_the_audit_event_keeps_denied_requests_keys_whole(tmp_path):
    a = stage1.Audit(tmp_path / "a.jsonl")
    key = "POST /v1/chat/completions" + "/x" * 25 + "?<12>"                    # method + target at the proxy's 80 character cap
    assert len(key) > 60
    a.emit("session_end", denied_requests={key: 3, "GET /api/version": 1})
    a.close()
    row = json.loads((tmp_path / "a.jsonl").read_text())
    assert row["denied_requests"] == {key: 3, "GET /api/version": 1}


# ══ which runtime model sessions use (docker by default; sbx only when asked for AND usable) ═══════════════════════

SBX_OK = sbx_session.ModelRuntime("sbx", True, "/usr/bin/sbx", "0.50.1")
SBX_BAD = sbx_session.ModelRuntime("sbx", False, "/usr/bin/sbx", None, ("sbx daemon is not reachable (`sbx daemon start`)",), ("daemon-down",))
ASK_SBX = {sbx_session.ENV_MODEL_SANDBOX: "sbx"}


def test_plan_names_the_runtime_and_it_is_docker_by_default(rig, up):
    out = io.StringIO()
    assert run_plan(rig.parse("plan"), environ={}, probes=rig.probes(), out=out) == 0
    text = out.getvalue()
    assert "runtime          model sessions run in: rootless Docker (the default" in text
    assert re.search(r"\[ok\] model_runtime\s+model sessions run in: rootless Docker", text)
    assert "container        2 GB, 2 CPUs, 256 pids" in text and "no egress network" in text


def test_plan_with_sbx_asked_for_and_usable_says_which_runtime_and_describes_a_vm(rig, up):
    out = io.StringIO()
    code = run_plan(rig.parse("plan"), environ=ASK_SBX, probes=rig.probes(model_runtime=lambda env: SBX_OK, environ=ASK_SBX), out=out)
    text = out.getvalue()
    assert code == 0 and "READY." in text
    assert "runtime          model sessions run in: sbx microVM 0.50.1 at /usr/bin/sbx" in text
    assert "microVM          sbx, per session: 2 GB, 2 CPUs" in text and "container        2 GB" not in text
    assert "one host port (our egress proxy)" in text and "five sandbox-scoped" in text
    assert re.search(r"\[ok\] model_runtime\s+model sessions run in: sbx microVM", text)


def test_plan_with_sbx_asked_for_and_not_usable_is_not_ready_and_never_falls_back_to_docker(rig, up):
    out = io.StringIO()
    code = run_plan(rig.parse("plan"), environ=ASK_SBX, probes=rig.probes(model_runtime=lambda env: SBX_BAD, environ=ASK_SBX), out=out)
    text = out.getvalue()
    assert code == 2 and "NOT READY" in text and "stage1 run --approve" not in text
    assert "[FAIL] model_runtime" in text and "sbx daemon is not reachable" in text and "no fallback to Docker" in text
    assert "rootless Docker (the default" not in text


@pytest.mark.parametrize("value", ["auto", "1", "microvm"])
def test_a_value_that_is_not_docker_or_sbx_is_not_ready(rig, up, value):
    out = io.StringIO()
    code = run_plan(rig.parse("plan"), environ={sbx_session.ENV_MODEL_SANDBOX: value}, probes=rig.probes(environ={sbx_session.ENV_MODEL_SANDBOX: value}), out=out)
    assert code == 2 and "not recognised" in out.getvalue() and "[FAIL] model_runtime" in out.getvalue()


def test_the_runtime_check_is_what_the_probe_returns_for_the_environment_it_was_given(rig, up):
    seen = []
    run_plan(rig.parse("plan"), environ=ASK_SBX, probes=rig.probes(model_runtime=lambda env: seen.append(dict(env)) or SBX_OK, environ=ASK_SBX), out=io.StringIO())
    assert seen == [ASK_SBX]


def test_a_run_with_sbx_asked_for_and_not_usable_refuses_before_anything_is_created(rig, up, monkeypatch, capsys):
    Traps(monkeypatch, rig.fake.port)
    code = run_run(rig.parse("run", "--approve", "local-ollama"), environ=ASK_SBX, probes=rig.probes(model_runtime=lambda env: SBX_BAD, environ=ASK_SBX),
                   stdin=TTY("approve local-ollama anything\n"), stdout=TTY())
    err = capsys.readouterr().err
    assert code == 2 and "model_runtime" in err and "no fallback to Docker" in err and "nothing was started" in err
    assert not rig.runs.exists() and not any(rig.ws.iterdir())


def test_the_sweep_context_defaults_to_docker_and_carries_the_runtime():
    fields = {f.name: f for f in dataclasses.fields(stage1.SweepContext)}
    assert "runtime" in fields and fields["runtime"].default_factory().kind == "docker"       # type: ignore[misc]


def test_the_sbx_leftover_check_finds_and_removes_this_sweeps_sandboxes_only(tmp_path, monkeypatch):
    fake = REPO / "tests" / "fixtures" / "fake-sbx-model" / "sbx"
    state = tmp_path / "fake"
    state.mkdir()
    (state / "sandboxes").write_text("\n".join([sbx_session.sandbox_name("00000000000a", pid=os.getpid()),
                                                sbx_session.sandbox_name("00000000000b", pid=os.getpid()), "somebody-elses"]) + "\n")
    monkeypatch.setattr(sandbox, "_SBX_ENV_ALLOW_EXTRA", ("FAKE_SBX_",))
    env = {"PATH": os.environ["PATH"], "FAKE_SBX_DIR": str(state), "FAKE_SBX_POLICY_FILE": str(REPO / "tests" / "fixtures" / "sbx_policy_ls_deny_all.json")}
    left = sbx_session.leftover_sandboxes(str(fake), ["00000000000a"], environ=env)
    assert left == [f"sbx sandbox {sbx_session.sandbox_name('00000000000a', pid=os.getpid())}"]
    sbx_session.remove_leftover_sandboxes(str(fake), left, environ=env)
    assert sbx_session.leftover_sandboxes(str(fake), ["00000000000a"], environ=env) == []
    assert set((state / "sandboxes").read_text().split()) == {sbx_session.sandbox_name("00000000000b", pid=os.getpid()), "somebody-elses"}
    assert sbx_session.leftover_sandboxes(str(fake), [], environ=env) == []
