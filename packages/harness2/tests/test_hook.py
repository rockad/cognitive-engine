"""The advisory push hook (hooks/harness2-advise.sh): counter, threshold, reset, fail-open, isolation."""
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cognitive_harness2 import triggers

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "harness2-advise.sh"
FAIL = "PostToolUseFailure"


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


def run(state, payload, *, env_extra=None, path=None):
    stdin = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    env = {**os.environ, "COGNITIVE_HARNESS2_HOOK_STATE_DIR": str(state), **(env_extra or {})}
    if path is not None:
        env["PATH"] = path
    return subprocess.run(["bash", str(HOOK)] if path is None else [shutil.which("bash"), str(HOOK)],
                          input=stdin, capture_output=True, text=isinstance(stdin, str), env=env, timeout=30)


def ev(event, sid="s1", **extra):
    return {"hook_event_name": event, "session_id": sid, **extra}


def count(state, sid="s1"):
    f = state / f"{sid}.count"
    return f.stat().st_size if f.exists() else 0


def test_jq_and_bash_are_installed():
    assert shutil.which("jq") and shutil.which("bash"), "the hook needs bash and jq; install jq"


def test_the_script_limit_is_the_triggers_limit():
    assert int(re.search(r"^RETRY_LIMIT=(\d+)", HOOK.read_text(), re.M).group(1)) == triggers.RETRY_LIMIT


def test_the_script_is_executable():
    assert os.access(HOOK, os.X_OK)


def test_silent_below_the_threshold_and_advises_at_it(state):
    for i in range(1, triggers.RETRY_LIMIT):
        r = run(state, ev(FAIL, tool_name="Bash", error="boom"))
        assert r.returncode == 0 and r.stdout == "" and count(state) == i
    r = run(state, ev(FAIL))
    assert r.returncode == 0 and count(state) == triggers.RETRY_LIMIT
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == FAIL
    assert "harness2_trigger" in out["additionalContext"] and "advisory" in out["additionalContext"]


def test_above_the_threshold_repeats_only_at_multiples(state):
    said = [bool(run(state, ev(FAIL)).stdout) for _ in range(7)]
    assert said == [False, False, True, False, False, True, False]


def test_advice_never_blocks_or_decides(state):
    outs = [run(state, ev(FAIL)) for _ in range(6)] + [run(state, ev("Stop"))]
    for r in outs:
        assert r.returncode == 0
        if r.stdout:
            body = json.loads(r.stdout)
            assert not {"decision", "continue", "stopReason", "permissionDecision"} & set(body)
            assert "decision" not in body.get("hookSpecificOutput", {})


@pytest.mark.parametrize("event", ["PostToolUse", "UserPromptSubmit"])
def test_success_and_new_prompt_reset_the_counter(state, event):
    for _ in range(2):
        run(state, ev(FAIL))
    assert count(state) == 2
    r = run(state, ev(event))
    assert r.returncode == 0 and r.stdout == "" and count(state) == 0
    # a fresh streak needs the full limit again
    assert run(state, ev(FAIL)).stdout == "" and count(state) == 1


def test_a_reset_on_a_clean_session_is_a_no_op(state):
    assert run(state, ev("PostToolUse")).returncode == 0
    assert not state.exists()


def test_user_interrupts_are_not_failures(state):
    for _ in range(5):
        r = run(state, ev(FAIL, is_interrupt=True))
        assert r.returncode == 0 and r.stdout == ""
    assert count(state) == 0


def test_stop_suggests_to_the_user_only_after_the_limit(state):
    run(state, ev(FAIL))
    assert run(state, ev("Stop")).stdout == ""
    for _ in range(triggers.RETRY_LIMIT - 1):
        run(state, ev(FAIL))
    msg = json.loads(run(state, ev("Stop")).stdout)
    assert set(msg) == {"systemMessage"} and "harness2_trigger" in msg["systemMessage"]
    assert count(state) == triggers.RETRY_LIMIT        # Stop does not reset


def test_unrelated_events_are_ignored(state):
    for e in ("SessionStart", "PreToolUse", "SubagentStop", "Notification", "Nonsense"):
        r = run(state, ev(e, last_assistant_message="ok"))
        assert r.returncode == 0 and r.stdout == ""
    assert not state.exists()


def test_sessions_are_counted_independently(state):
    for _ in range(2):
        run(state, ev(FAIL, sid="a"))
    run(state, ev(FAIL, sid="b"))
    assert (count(state, "a"), count(state, "b")) == (2, 1)
    run(state, ev("PostToolUse", sid="a"))
    assert (count(state, "a"), count(state, "b")) == (0, 1)
    assert run(state, ev(FAIL, sid="b")).stdout == "" and run(state, ev(FAIL, sid="b")).stdout != ""


def test_concurrent_failures_in_one_session_are_all_counted(state):
    n = 16
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda _: run(state, ev(FAIL, sid="par")), range(n)))
    assert all(r.returncode == 0 for r in results)
    assert count(state, "par") == n
    assert sum(1 for r in results if r.stdout) >= 1       # the limit was crossed by someone


def test_concurrent_sessions_do_not_interfere(state):
    sids = [f"c{i}" for i in range(6)]
    jobs = [sid for sid in sids for _ in range(4)]
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda sid: run(state, ev(FAIL, sid=sid)), jobs))
    assert all(r.returncode == 0 for r in results)
    assert [count(state, s) for s in sids] == [4] * 6


# ── fail-open ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stdin", [
    "", "   ", "not json", "{", '{"hook_event_name":', "[]", "null", "42", '"str"',
    b"\xff\xfe\x00garbage", "{}", '{"hook_event_name": 7, "session_id": ["x"]}',
    '{"hook_event_name": "PostToolUseFailure"}',                       # no session id
    '{"hook_event_name": "PostToolUseFailure", "session_id": ""}',
    '{"hook_event_name": "PostToolUseFailure", "session_id": {"a": 1}}',
])
def test_malformed_stdin_is_a_silent_exit_zero_and_writes_nothing(state, stdin):
    r = run(state, stdin)
    assert r.returncode == 0 and not r.stdout
    assert not state.exists()


@pytest.mark.parametrize("sid", ["../escape", "a/b", "a b", "x" * 65, "..", "a\nb", "é", ";rm -rf"])
def test_unsafe_session_ids_are_ignored_and_never_leave_the_state_dir(state, tmp_path, sid):
    r = run(state, ev(FAIL, sid=sid))
    assert r.returncode == 0 and r.stdout == ""
    assert not state.exists()
    assert not (tmp_path / "escape.count").exists()


def test_an_oversized_payload_is_still_handled(state):
    big = json.dumps(ev(FAIL, error="e" * 2_000_000))
    assert run(state, big).returncode == 0


def test_an_unwritable_state_dir_fails_open(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        r = run(ro / "state", ev(FAIL))
        assert r.returncode == 0 and r.stdout == ""
    finally:
        ro.chmod(0o700)


def test_a_state_dir_that_is_a_file_fails_open(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    assert run(f, ev(FAIL)).returncode == 0


def test_a_corrupt_state_file_is_treated_as_a_fresh_count(state):
    (state / "s1.count").mkdir(parents=True)             # a directory where a file should be
    r = run(state, ev(FAIL))
    assert r.returncode == 0


def test_without_jq_it_says_so_on_stderr_and_still_exits_zero(state, tmp_path):
    empty = tmp_path / "bin"
    empty.mkdir()
    r = run(state, ev(FAIL), path=str(empty))
    assert r.returncode == 0 and r.stdout == "" and "jq" in r.stderr
    assert not state.exists()


# ── privacy ──────────────────────────────────────────────────────────────────────

def test_nothing_but_a_count_is_written_to_disk(state, tmp_path):
    secret = "sk-or-v1-SECRETSECRET"
    payload = ev(FAIL, tool_name="Bash", tool_input={"command": f"curl -H 'Authorization: {secret}'"},
                 error=f"401 {secret}", prompt="my private prompt")
    for _ in range(4):
        r = run(state, payload)
        assert secret not in r.stdout and "private prompt" not in r.stdout
    files = [p for p in state.rglob("*") if p.is_file()]
    assert [p.name for p in files] == ["s1.count"]
    assert set(files[0].read_bytes()) == {ord("x")}           # one byte per failure, no text
    assert oct(state.stat().st_mode & 0o777) == "0o700"
    leaked = subprocess.run(["rg", "-l", "-a", "SECRETSECRET|private prompt", str(tmp_path)], capture_output=True)
    assert leaked.stdout == b""


def test_stale_state_is_pruned_when_a_new_streak_starts(state):
    state.mkdir()
    old = state / "old.count"
    old.write_text("xx")
    os.utime(old, (1, 1))
    fresh = state / "fresh.count"
    fresh.write_text("x")
    run(state, ev(FAIL, sid="new"))
    assert not old.exists() and fresh.exists() and (state / "new.count").exists()
