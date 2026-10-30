import json

from fake_ollama import FakeOllama, chat_response
from ollama_bench import runner as rn, scoring as sc
from ollama_bench.client import Ollama
from ollama_bench.report import deploy_commands, render, variant_name, write_report
from ollama_bench.system import GpuSampler, NoiseReport
from pathlib import Path


def quiet_noise():
    return NoiseReport(before={"load1": 0.5, "load5": 0.5, "load15": 0.5, "mem_available_gb": 9.0})


def make_runner(f, tmp_path, models=("m1", "m2"), **kw):
    return rn.Runner(Ollama(f.url), tmp_path, models=list(models), reps=1, seed=7, limit_cases=4, limit_tasks=2,
                     sleep=lambda s: None, gpu_read=lambda: 3000, noise_gate=quiet_noise,
                     sampler_factory=lambda: GpuSampler(lambda: 3500, interval_s=0.01), log=lambda s: None, **kw)


def chatty(body, fake):
    """A model that never calls tools: answers every request in prose."""
    return chat_response("I am not sure.", thinking="hmm" * 10)


def test_end_to_end_with_a_fake_server(tmp_path):
    with FakeOllama(chatty) as f:
        r = make_runner(f, tmp_path)
        summary = r.run()
        assert set(summary["models"]) == {"m1", "m2"}
        assert sorted(summary["meta"]["order"]) == ["m1", "m2"]
        m = summary["models"]["m1"]
        assert m["perf"]["gpu_fraction"] == 1.0 and m["perf"]["ps"]["context_length"] == 16384
        assert m["perf"]["timed"]["1000"]["gen_tps"]["median"] == 20.0
        assert len(m["perf"]["cold_loads"]) == 3 and all(x["unload_confirmed"] for x in m["perf"]["cold_loads"])
        assert m["unload_confirmed"] is True and f.loaded == []
        assert m["gpu"]["peak_mib"] == 3500 and m["gpu_baseline_mib"] == 3000
        for phase in sc.PHASE_ORDER[1:]:
            assert m["phases"][phase]["attempted"] > 0, phase
        assert m["phases"]["tools_t0"]["attempted"] == 4
        assert m["phases"]["agent_t0"]["failure_categories"] == {"no_tool_call": 2}
        assert m["score"]["eligible"] and m["score"]["components"]["agent"] == 0.0
        lines = [json.loads(x) for x in (tmp_path / "results.jsonl").read_text().splitlines()]
        kinds = {x["kind"] for x in lines}
        assert {"run_start", "chat", "case", "task", "model_end"} <= kinds
        chats = [x for x in lines if x["kind"] == "chat"]
        assert all("raw" in c and "stats" in c for c in chats)
        # every request carried the identical num_ctx; t=0.6 pass carries top_p
        assert all(rq["options"]["num_ctx"] == 16384 for rq in f.requests)
        t06 = [c for c in chats if c["phase"] == "tools_t06"]
        assert t06 and all(c["options"]["temperature"] == 0.6 and c["options"]["top_p"] == 0.95 for c in t06)
        nothink = [c for c in chats if c["phase"] in ("tools_nothink", "agent_nothink")]
        assert nothink and all(c["think"] is False for c in nothink)
        default = [c for c in chats if c["phase"] in ("tools_t0", "agent_t0")]
        assert default and all(c["think"] is None for c in default)
        # report renders and starts with the pre-registered rules
        path = write_report(tmp_path, summary, Path(__file__).resolve().parents[3])
        text = path.read_text()
        assert text.index("Pre-registered rules") < text.index("## Run conditions") < text.index("## Scores")
        assert (tmp_path / "summary.json").is_file()


def test_gpu_split_fails_the_gate(tmp_path):
    with FakeOllama(chatty, size=4_000_000_000, size_vram=3_000_000_000) as f:
        s = make_runner(f, tmp_path, models=("m1",)).run()
        assert s["models"]["m1"]["score"]["gate_failures"] == ["gpu_split"]
        assert s["recommendation"]["recommended"] is None


def test_three_errors_in_a_row_is_a_load_failure_and_stops_the_model(tmp_path):
    with FakeOllama(lambda b, f: ("error", 500, "llama runner process has terminated")) as f:
        s = make_runner(f, tmp_path, models=("m1",)).run()
        m = s["models"]["m1"]
        assert m["load_failure"] and not m["crash"]
        assert "tools_t0" not in m["phases"]
        assert len(f.requests) == 3                     # no retries beyond the streak
        assert "gpu_split" in m["score"]["gate_failures"] and "load_failure" in m["score"]["gate_failures"]


def test_errors_after_success_are_a_crash(tmp_path):
    state = {"n": 0}

    def flaky(body, fake):
        state["n"] += 1
        if state["n"] <= 10:
            return chat_response("ok")
        return ("error", 500, "boom")
    with FakeOllama(flaky) as f:
        s = make_runner(f, tmp_path, models=("m1",)).run()
        assert s["models"]["m1"]["crash"] and not s["models"]["m1"]["load_failure"]


def test_malformed_tool_call_errors_do_not_trip_the_streak(tmp_path):
    state = {"n": 0}

    def resp(body, fake):
        state["n"] += 1
        if state["n"] <= 10:
            return chat_response("ok")
        return ("error", 500, "error parsing tool call: raw='{oops'")
    with FakeOllama(resp) as f:
        s = make_runner(f, tmp_path, models=("m1",)).run()
        m = s["models"]["m1"]
        assert not m["crash"] and not m["load_failure"]
        assert m["phases"]["tools_t0"]["failure_reasons"].get("malformed tool call") == 4


def test_phase_deadline_skips_later_items_and_marks_incomplete(tmp_path):
    now = [0.0]
    with FakeOllama(chatty) as f:
        r = rn.Runner(Ollama(f.url), tmp_path, models=["m1"], reps=1, seed=1, limit_cases=4, limit_tasks=2,
                      budget_s=sc.REPORT_RESERVE_S + 100, clock=lambda: now[0], sleep=lambda s: None,
                      gpu_read=lambda: 1, noise_gate=quiet_noise,
                      sampler_factory=lambda: GpuSampler(lambda: 1, interval_s=0.05), log=lambda s: None)
        orig = r._chat

        def slow(*a, **k):
            now[0] += 30.0                      # every request takes 30 'seconds'
            return orig(*a, **k)
        r._chat = slow
        s = r.run()
        m = s["models"]["m1"]
        assert m["incomplete"] and sum(m["skipped"].values()) > 0


def test_order_is_seeded_and_recorded(tmp_path):
    with FakeOllama(chatty) as f:
        a = rn.Runner(Ollama(f.url), tmp_path / "a", models=["x", "y", "z"], seed=3, noise_gate=quiet_noise)
        b = rn.Runner(Ollama(f.url), tmp_path / "b", models=["x", "y", "z"], seed=3, noise_gate=quiet_noise)
        import random
        o = ["x", "y", "z"]
        random.Random(3).shuffle(o)
        for r in (a, b):
            r.phases = ()
            r.noise_gate = quiet_noise
            r.gpu_read = lambda: 1
            r.sampler_factory = lambda: GpuSampler(lambda: 1, interval_s=0.05)
            r.log = lambda s: None
            assert r.run()["meta"]["order"] == o


def test_options_helper():
    assert rn.make_options("tools_t0", "tools", 1) == {"num_ctx": 16384, "temperature": 0.0, "seed": 43, "num_predict": 2048}
    assert rn.make_options("tools_t06", "tools", 0)["top_p"] == 0.95
    assert rn.make_options("agent_nothink", "agent")["num_predict"] == 4096


def test_deploy_commands():
    assert "ollama create gemma4-e4b-16k" in deploy_commands("gemma4:e4b")
    assert "FROM gemma4:e4b" in deploy_commands("gemma4:e4b") and "PARAMETER num_ctx 16384" in deploy_commands("gemma4:e4b")
    assert 'model = "ollama/nemotron-3-nano-4b-16k"' in deploy_commands("nemotron-3-nano:4b")
    q = deploy_commands("qwen3-4b-16k")
    assert "ollama create" not in q and 'model = "ollama/qwen3-4b-16k"' in q
    assert variant_name("qwen3-4b-16k") == "qwen3-4b-16k"
