import threading
import time

from ollama_bench import perf, scoring as sc
from ollama_bench.system import GpuSampler, quiet, wait_for_quiet, load_state


def approx_tokens(messages, tools):
    return (sum(len(str(m)) for m in messages) + len(repr(tools))) / perf.CHARS_PER_TOKEN


def test_prompt_sizes_are_near_target_and_ordered():
    for target in (1000, 4000):
        msgs, tools = perf.build_perf_request(target, "n1")
        assert 0.85 * target <= approx_tokens(msgs, tools) <= 1.35 * target, target
        assert len(tools) == 6
    m1, t1 = perf.build_perf_request(1000, "n")
    m4, t4 = perf.build_perf_request(4000, "n")
    assert approx_tokens(m4, t4) > 2.5 * approx_tokens(m1, t1)


def test_nonce_leads_the_prompt_so_prefix_cache_cannot_hit():
    a, _ = perf.build_perf_request(1000, "run-A")
    b, _ = perf.build_perf_request(1000, "run-B")
    assert a[0]["content"].startswith("[run run-A]") and b[0]["content"].startswith("[run run-B]")
    assert a[0]["content"].split("\n", 1)[1] == b[0]["content"].split("\n", 1)[1]


def test_4k_step_is_opencode_like_with_a_tool_round_trip():
    msgs, tools = perf.build_perf_request(4000, "n")
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert "AGENTS.md" in msgs[0]["content"]
    assert {t["function"]["name"] for t in tools} == {"read_file", "write_file", "edit_file", "list_dir", "bash", "todo"}


def test_wait_for_quiet_returns_immediately_when_quiet():
    r = wait_for_quiet(read=lambda: {"load1": 1.0, "load5": 1, "load15": 1, "mem_available_gb": 8.0},
                       sleep=lambda s: (_ for _ in ()).throw(AssertionError("must not sleep")))
    assert r.waited_s == 0 and r.settled and r.polls == []


def test_wait_for_quiet_waits_then_settles():
    states = iter([{"load1": 9.0, "mem_available_gb": 8.0}, {"load1": 7.0, "mem_available_gb": 8.0},
                   {"load1": 2.0, "mem_available_gb": 8.0}])
    now = [0.0]

    def sleep(s):
        now[0] += s
    r = wait_for_quiet(read=lambda: next(states), sleep=sleep, clock=lambda: now[0], max_wait_s=300, poll_s=15)
    assert r.settled and r.waited_s == 30.0 and len(r.polls) == 2


def test_wait_for_quiet_gives_up_after_the_maximum_and_says_so():
    now = [0.0]

    def sleep(s):
        now[0] += s
    r = wait_for_quiet(read=lambda: {"load1": 1.0, "mem_available_gb": 1.5}, sleep=sleep, clock=lambda: now[0])
    assert not r.settled and r.waited_s >= sc.NOISE_WAIT_MAX_S


def test_quiet_thresholds():
    assert quiet({"load1": 4.0, "mem_available_gb": 3.0})
    assert not quiet({"load1": 4.1, "mem_available_gb": 8})
    assert not quiet({"load1": 1, "mem_available_gb": 2.9})


def test_load_state_reads_proc():
    s = load_state()
    assert s["load1"] >= 0 and s["mem_available_gb"] > 0


def test_gpu_sampler_keeps_peak_per_phase():
    vals = iter([100, 300, 200, 500, 400] + [400] * 1000)
    s = GpuSampler(read=lambda: next(vals), interval_s=0.01)
    s.start()
    s.mark("a")
    time.sleep(0.06)
    s.mark("b")
    time.sleep(0.06)
    out = s.stop()
    assert out["peak_mib"] == 500 and out["n_samples"] >= 3 and "a" in out["phase_peaks_mib"]


def test_gpu_sampler_tolerates_missing_nvidia_smi():
    s = GpuSampler(read=lambda: None, interval_s=0.01)
    s.start()
    time.sleep(0.03)
    assert s.stop()["peak_mib"] is None
