"""BuildSlot: one conformance suite for every implementation, then what is specific to the flock one.

A daemon-backed implementation must pass the CONFORMANCE section unchanged: add a rig to `RIGS`. Nothing here touches
Docker or the shared Docker lock: every lock file lives under `tmp_path`.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest

from cognitive_harness2 import coordination
from cognitive_harness2.coordination import BuildSlot, FlockBuildSlot, HolderInfo, SlotTimeout, marker_name
from cognitive_harness2.model_gate import GATE

SLOT = "rootless-docker"

CHILD = """
import sys
from cognitive_harness2.coordination import FlockBuildSlot
lock, name, label = sys.argv[1:4]
with FlockBuildSlot({name: lock}).hold(name, label=label, ttl_hint=30):
    print("READY", flush=True)
    sys.stdin.readline()
"""


class Holder:
    """Another process that holds a slot until told to stop (or killed)."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def release(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.write("\n")
            self.proc.stdin.flush()
        self.proc.wait(timeout=10)

    def kill(self) -> None:
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)


class FlockRig:
    """What the conformance suite needs from an implementation: the slots object and a foreign holder."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.lock = tmp_path / "slot.lock"
        self.slots: BuildSlot = FlockBuildSlot({SLOT: self.lock}, state_dir=tmp_path / "state", poll_s=0.02)
        self.children: list[Holder] = []

    def start_holder(self, name: str = SLOT, label: str = "child:holder") -> Holder:
        lock = self.lock if name == SLOT else self.tmp / "state" / f"{name}.lock"
        proc = subprocess.Popen([sys.executable, "-c", CHILD, str(lock), name, label], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, env={k: v for k, v in os.environ.items() if not k.startswith("CE_BUILDSLOT_")})
        h = Holder(proc)
        self.children.append(h)
        assert proc.stdout.readline().strip() == "READY"
        return h

    def close(self) -> None:
        for h in self.children:
            if h.proc.poll() is None:
                h.proc.kill()
                h.proc.wait(timeout=10)


RIGS = [pytest.param(FlockRig, id="flock")]


@pytest.fixture(autouse=True)
def _no_marker(monkeypatch):
    for k in list(os.environ):
        if k.startswith("CE_BUILDSLOT_"):
            monkeypatch.delenv(k)


@pytest.fixture(params=RIGS)
def rig(request, tmp_path):
    r = request.param(tmp_path)
    yield r
    r.close()


# ══ CONFORMANCE: every implementation passes this unchanged ═════════════════════════════════════════════════════════

def test_the_implementation_satisfies_the_protocol(rig):
    assert isinstance(rig.slots, BuildSlot)


def test_a_free_slot_is_taken_and_given_back(rig):
    with rig.slots.hold(SLOT, timeout=1, label="t"):
        pass
    with rig.slots.hold(SLOT, timeout=1, label="t"):
        pass


def test_two_processes_exclude_each_other(rig):
    child = rig.start_holder()
    with pytest.raises(SlotTimeout):
        with rig.slots.hold(SLOT, timeout=0.3):
            pytest.fail("entered a slot another process holds")
    child.release()
    with rig.slots.hold(SLOT, timeout=5):
        pass


def test_timeout_and_deadline_the_earlier_wins(rig):
    rig.start_holder()
    t0 = time.monotonic()
    with pytest.raises(SlotTimeout):
        with rig.slots.hold(SLOT, timeout=30, deadline=time.monotonic() + 0.3):
            pass
    assert time.monotonic() - t0 < 5
    t0 = time.monotonic()
    with pytest.raises(SlotTimeout):
        with rig.slots.hold(SLOT, timeout=0.3, deadline=time.monotonic() + 30):
            pass
    assert time.monotonic() - t0 < 5
    with pytest.raises(SlotTimeout):                                  # a deadline already in the past
        with rig.slots.hold(SLOT, deadline=time.monotonic() - 1):
            pass


def test_the_timeout_names_the_holder_when_it_is_known(rig):
    rig.start_holder(label="suite:full")
    with pytest.raises(SlotTimeout) as ei:
        with rig.slots.hold(SLOT, timeout=0.2):
            pass
    assert ei.value.slot == SLOT and isinstance(ei.value, TimeoutError)
    assert ei.value.holder is not None and ei.value.holder.label == "suite:full" and ei.value.holder.pid
    assert "suite:full" in str(ei.value)


def test_the_slot_is_released_on_normal_exit_on_exception_and_on_sigkill(rig):
    with rig.slots.hold(SLOT, timeout=1):
        pass
    with rig.slots.hold(SLOT, timeout=1):          # normal exit released it
        pass
    with pytest.raises(RuntimeError):
        with rig.slots.hold(SLOT, timeout=1):
            raise RuntimeError("boom")
    with rig.slots.hold(SLOT, timeout=1):          # an exception released it
        pass
    child = rig.start_holder()
    child.kill()
    with rig.slots.hold(SLOT, timeout=5):          # the holder died without cleaning up
        assert rig.slots.holder(SLOT) is None or rig.slots.holder(SLOT).pid == os.getpid()


def test_the_slot_is_not_reentrant(rig):
    with rig.slots.hold(SLOT, timeout=1):
        with pytest.raises(SlotTimeout):
            with rig.slots.hold(SLOT, timeout=0.2):
                pytest.fail("a thread entered a slot it already holds")


def test_different_slots_do_not_exclude_each_other(rig):
    rig.start_holder()
    with rig.slots.hold("gpu-model", timeout=1):
        pass


def test_a_stale_holder_is_not_reported(rig):
    child = rig.start_holder(label="will-die")
    child.kill()
    assert rig.slots.holder(SLOT) is None


def test_the_holder_of_a_free_slot_is_none(rig):
    assert rig.slots.holder(SLOT) is None
    with rig.slots.hold(SLOT, timeout=1, label="x"):
        pass
    assert rig.slots.holder(SLOT) is None


def test_the_documented_lock_order_gate_then_slot_never_deadlocks(rig):
    """Two threads of this process and one other process, all using the order GATE, then the slot, released in reverse."""
    child = rig.start_holder()
    order: list[str] = []
    errors: list[BaseException] = []

    def work(tag: str) -> None:
        try:
            with GATE.hold(timeout=20, label=f"gate:{tag}"):
                with rig.slots.hold(SLOT, timeout=20, label=f"slot:{tag}"):
                    order.append(tag)
                    time.sleep(0.05)
        except BaseException as e:                                  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    time.sleep(0.3)
    child.release()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    assert not errors and sorted(order) == ["a", "b"] and not GATE.locked()
    with rig.slots.hold(SLOT, timeout=1):
        pass


async def test_ahold_does_not_block_the_event_loop(rig):
    rig.start_holder()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    with pytest.raises(SlotTimeout):
        async with rig.slots.ahold(SLOT, timeout=0.4):
            pytest.fail("entered a held slot")
    t.cancel()
    assert ticks >= 10


async def test_a_cancelled_ahold_does_not_leak_the_slot(rig):
    child = rig.start_holder()

    async def waiter():
        async with rig.slots.ahold(SLOT):               # no timeout: waits until told otherwise
            pass

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    child.release()
    await asyncio.sleep(0.3)                              # the abandoned worker must not take the slot after the holder leaves
    with rig.slots.hold(SLOT, timeout=2):
        pass


async def test_ahold_takes_and_releases_the_slot(rig):
    async with rig.slots.ahold(SLOT, timeout=1, label="async"):
        with pytest.raises(SlotTimeout):
            with rig.slots.hold(SLOT, timeout=0.1):
                pass
    with rig.slots.hold(SLOT, timeout=1):
        pass


@pytest.mark.parametrize("bad", ["", "A", "../x", "a/b", "a b", "-a", "x" * 65, ".a"])
def test_a_bad_slot_name_is_refused_before_anything_is_touched(rig, bad):
    with pytest.raises(ValueError):
        with rig.slots.hold(bad, timeout=0.1):
            pass
    with pytest.raises(ValueError):
        rig.slots.holder(bad)


@pytest.mark.parametrize("kw,exc", [({"timeout": -1}, ValueError), ({"timeout": "5"}, TypeError), ({"timeout": True}, TypeError),
                                    ({"deadline": "x"}, TypeError), ({"timeout": float("nan")}, ValueError)])
def test_a_bad_timeout_or_deadline_is_refused(rig, kw, exc):
    with pytest.raises(exc):
        with rig.slots.hold(SLOT, **kw):
            pass


# ══ FLOCK: what is specific to this implementation ══════════════════════════════════════════════════════════════════

@pytest.fixture
def flock_rig(tmp_path):
    r = FlockRig(tmp_path)
    yield r
    r.close()


def sidecar(rig: FlockRig):
    return rig.lock.with_name(rig.lock.name + ".holder")


def test_the_kernel_call_is_fcntl_flock_never_lockf():
    src = open(coordination.__file__, encoding="utf-8").read()
    assert "fcntl.flock(" in src and "fcntl.lockf(" not in src


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) is not installed")
def test_it_excludes_against_flock_1_in_both_directions(flock_rig):
    flock_rig.lock.touch()
    outer = subprocess.Popen(["flock", str(flock_rig.lock), "sleep", "2"])
    try:
        time.sleep(0.3)
        with pytest.raises(SlotTimeout) as ei:
            with flock_rig.slots.hold(SLOT, timeout=0.3):
                pass
        assert ei.value.holder is None                      # the shell wrapper leaves no sidecar: unknown, not guessed
    finally:
        outer.kill()
        outer.wait()
    with flock_rig.slots.hold(SLOT, timeout=2):
        rc = subprocess.run(["flock", "-n", str(flock_rig.lock), "true"]).returncode
        assert rc == 1                                      # flock(1) cannot take what Python holds


def test_the_sidecar_is_written_while_held_and_removed_after(flock_rig):
    assert not sidecar(flock_rig).exists()
    with flock_rig.slots.hold(SLOT, timeout=1, label="suite:full", ttl_hint=120):
        data = json.loads(sidecar(flock_rig).read_text())
        assert data["label"] == "suite:full" and data["pid"] == os.getpid() and data["ttl_hint_s"] == 120
        assert set(data) == {"label", "pid", "host", "acquired_at", "ttl_hint_s"}
        assert flock_rig.lock.read_text() == ""             # the lock file itself stays empty
        info = flock_rig.slots.holder(SLOT)
        assert info == HolderInfo("suite:full", os.getpid(), data["host"], data["acquired_at"], 120.0)
    assert not sidecar(flock_rig).exists()


def test_a_sidecar_left_by_a_killed_holder_is_replaced_by_the_next_holder_and_gone_after(flock_rig):
    child = flock_rig.start_holder(label="old")
    assert sidecar(flock_rig).exists()
    child.kill()
    assert sidecar(flock_rig).exists()                      # the dead holder could not clean up
    with flock_rig.slots.hold(SLOT, timeout=5, label="new"):
        assert json.loads(sidecar(flock_rig).read_text())["label"] == "new"
    assert not sidecar(flock_rig).exists()


def test_a_sidecar_whose_lock_is_free_is_not_reported_even_if_its_pid_is_alive(flock_rig):
    flock_rig.lock.touch()
    sidecar(flock_rig).write_text(json.dumps({"label": "x", "pid": os.getpid(), "host": flock_rig.slots._host, "acquired_at": 1.0}))
    assert flock_rig.slots.holder(SLOT) is None


@pytest.mark.parametrize("text", ["not json", "[]", "null", '{"pid": "x"}', "x" * 5000, "{" * 50000])
def test_an_unreadable_or_oversized_sidecar_is_ignored(flock_rig, text):
    flock_rig.start_holder()
    sidecar(flock_rig).write_text(text)
    assert flock_rig.slots.holder(SLOT) is None
    with pytest.raises(SlotTimeout) as ei:
        with flock_rig.slots.hold(SLOT, timeout=0.2):
            pass
    assert ei.value.holder is None


def test_a_forged_sidecar_cannot_inject_control_characters_or_huge_text(flock_rig):
    flock_rig.start_holder()
    sidecar(flock_rig).write_text(json.dumps({"label": "ok\r\nFORGED line\x1b[31m" + "y" * 500, "pid": 1.5, "host": "h\n" * 10,
                                              "acquired_at": -3, "ttl_hint_s": True}))
    info = flock_rig.slots.holder(SLOT)
    assert info is not None and "\n" not in (info.label or "") and "\x1b" not in (info.label or "") and len(info.label or "") <= 120
    assert info.pid is None and info.acquired_at is None and info.ttl_hint_s is None and "\n" not in (info.host or "")
    with pytest.raises(SlotTimeout) as ei:
        with flock_rig.slots.hold(SLOT, timeout=0.2):
            pass
    assert "\n" not in str(ei.value) and "\x1b" not in str(ei.value)


def test_a_holder_on_this_host_with_a_dead_pid_is_not_reported_but_one_on_another_host_is_trusted_while_locked(flock_rig):
    flock_rig.start_holder()
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    sidecar(flock_rig).write_text(json.dumps({"label": "x", "pid": gone.pid, "host": flock_rig.slots._host, "acquired_at": 1.0}))
    assert flock_rig.slots.holder(SLOT) is None
    sidecar(flock_rig).write_text(json.dumps({"label": "far", "pid": gone.pid, "host": "other-host", "acquired_at": 1.0}))
    assert flock_rig.slots.holder(SLOT) == HolderInfo("far", gone.pid, "other-host", 1.0, None)


def test_a_failed_sidecar_write_does_not_fail_the_hold(flock_rig, monkeypatch):
    monkeypatch.setattr(coordination.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with flock_rig.slots.hold(SLOT, timeout=1, label="x"):
        assert not sidecar(flock_rig).exists() and flock_rig.slots.holder(SLOT) is None
    assert not list(flock_rig.tmp.glob("*.tmp"))


def test_the_marker_name_maps_the_slot_name():
    assert marker_name("rootless-docker") == "CE_BUILDSLOT_HELD_ROOTLESS_DOCKER"
    assert marker_name("gpu.model_2") == "CE_BUILDSLOT_HELD_GPU_MODEL_2"
    with pytest.raises(ValueError):
        marker_name("Bad Name")


def test_the_nesting_marker_for_this_slot_skips_the_lock_and_writes_no_sidecar(flock_rig, monkeypatch):
    flock_rig.start_holder()                                   # somebody else really holds it
    monkeypatch.setenv("CE_BUILDSLOT_HELD_ROOTLESS_DOCKER", "1")
    t0 = time.monotonic()
    with flock_rig.slots.hold(SLOT, timeout=0.2):
        assert time.monotonic() - t0 < 0.15                     # returned at once, did not wait for the holder
        assert json.loads(sidecar(flock_rig).read_text())["label"] == "child:holder"    # and did not touch it


async def test_the_nesting_marker_applies_to_ahold_too(flock_rig, monkeypatch):
    flock_rig.start_holder()
    monkeypatch.setenv("CE_BUILDSLOT_HELD_ROOTLESS_DOCKER", "1")
    async with flock_rig.slots.ahold(SLOT, timeout=0.2):
        pass


def test_a_marker_for_another_slot_does_not_bypass_the_lock(flock_rig, monkeypatch):
    flock_rig.start_holder()
    monkeypatch.setenv("CE_BUILDSLOT_HELD_OTHER", "1")
    monkeypatch.setenv("CE_BUILDSLOT_HELD_GPU_MODEL", "1")
    with pytest.raises(SlotTimeout):
        with flock_rig.slots.hold(SLOT, timeout=0.2):
            pass


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "11", " 1", "1 "])
def test_only_the_exact_value_1_counts_as_the_marker(flock_rig, monkeypatch, value):
    flock_rig.start_holder()
    monkeypatch.setenv("CE_BUILDSLOT_HELD_ROOTLESS_DOCKER", value)
    with pytest.raises(SlotTimeout):
        with flock_rig.slots.hold(SLOT, timeout=0.2):
            pass


def test_a_child_started_under_the_wrapper_marker_does_not_deadlock_against_its_parent(flock_rig):
    """The shell wrapper holds the lock (flock(1)) and exports the marker; the Python inside must not wait for it."""
    if shutil.which("flock") is None:
        pytest.skip("flock(1) is not installed")
    code = ("import sys\nfrom cognitive_harness2.coordination import FlockBuildSlot\n"
            "with FlockBuildSlot({'rootless-docker': sys.argv[1]}).hold('rootless-docker', timeout=2):\n    print('INSIDE')\n")
    env = {**os.environ, "CE_BUILDSLOT_HELD_ROOTLESS_DOCKER": "1"}
    out = subprocess.run(["flock", str(flock_rig.lock), sys.executable, "-c", code, str(flock_rig.lock)], env=env, capture_output=True,
                         text=True, timeout=20)
    assert out.returncode == 0 and out.stdout.strip() == "INSIDE", out.stderr
    env.pop("CE_BUILDSLOT_HELD_ROOTLESS_DOCKER")             # without the marker the same nesting is the self-deadlock the marker exists for
    out = subprocess.run(["flock", str(flock_rig.lock), sys.executable, "-c", code.replace("timeout=2", "timeout=0.5"), str(flock_rig.lock)],
                         env=env, capture_output=True, text=True, timeout=20)
    assert out.returncode != 0 and "SlotTimeout" in out.stderr


def test_the_rootless_docker_slot_defaults_to_the_shell_wrappers_lock_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    slots = FlockBuildSlot(state_dir=tmp_path / "s")
    assert slots.lock_path("rootless-docker") == tmp_path / ".agents" / "cognitive-engine-docker-tests.lock"
    assert slots.lock_path("gpu-model") == tmp_path / "s" / "gpu-model.lock"
    assert (tmp_path / "s").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("poll", [0, -1, 6, "x", True, float("nan")])
def test_a_bad_poll_interval_is_refused(tmp_path, poll):
    with pytest.raises(ValueError):
        FlockBuildSlot(state_dir=tmp_path, poll_s=poll)

