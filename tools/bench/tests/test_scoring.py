import re
from pathlib import Path

from ollama_bench import scoring as sc

PREREG = Path(__file__).resolve().parents[1] / "PREREGISTRATION.md"


def metrics(name, **kw):
    base = dict(gpu_fraction=1.0, agent_success=0.5, tools_t0=0.5, tools_t06=0.5, agent_median_s=30.0)
    base.update(kw)
    return sc.ModelMetrics(name=name, **base)


def test_weights_sum_to_one_and_match_prereg_document():
    assert abs(sum(sc.WEIGHTS.values()) - 1.0) < 1e-9
    text = PREREG.read_text()
    assert "`Score = 100 * (0.50*A + 0.25*T + 0.15*S + 0.10*R)`" in text
    assert sc.WEIGHTS == {"agent": 0.50, "tools": 0.25, "speed": 0.15, "robustness": 0.10}
    assert "within 3.0 points" in text and sc.TIE_POINTS == 3.0
    for phase, frac in sc.PHASE_CUM_FRACTION.items():
        assert f"{int(frac * 100)}%" in text, phase
    assert "2048 for a tool-suite call, 4096 for a mini-agent step, 160" in text
    assert sc.NUM_PREDICT == {"tools": 2048, "agent": 4096, "perf": 160}
    assert re.search(r"Per-request timeout 240 s", text) and sc.REQUEST_TIMEOUT_S == 240
    assert "max 12 steps, 180 s per task" in text and sc.MAX_STEPS == 12 and sc.TASK_TIMEOUT_S == 180


def test_perfect_fastest_model_scores_100():
    s = sc.score_all([metrics("a", agent_success=1, tools_t0=1, tools_t06=1)])
    assert s["a"]["score"] == 100.0 and s["a"]["eligible"]


def test_speed_normalised_to_fastest_and_zero_without_success():
    s = sc.score_all([metrics("fast", agent_median_s=10), metrics("slow", agent_median_s=40),
                      metrics("quick-fail", agent_success=0.0, agent_median_s=1)])
    assert abs(s["fast"]["components"]["speed"] - 1 / 10) < 1e-9
    # quick-fail has the smallest median (1 s) so everyone is normalised to it, but it earns no speed points
    assert s["quick-fail"]["components"]["speed"] == 0.0
    assert abs(s["slow"]["components"]["speed"] - 1 / 40) < 1e-9


def test_robustness_capped_and_zero_when_base_zero():
    assert sc.robustness_component(0.9, 0.6) == 1.0
    assert abs(sc.robustness_component(0.3, 0.6) - 0.5) < 1e-9
    assert sc.robustness_component(0.5, 0.0) == 0.0


def test_gates():
    assert sc.gate_failures(metrics("a")) == []
    assert sc.gate_failures(metrics("a", gpu_fraction=0.8)) == ["gpu_split"]
    assert sc.gate_failures(metrics("a", gpu_fraction=None)) == ["gpu_split"]
    assert sc.gate_failures(metrics("a", load_failure=True, crash=True)) == ["load_failure", "crash"]
    assert sc.gpu_fraction({"size": 100, "size_vram": 100}) == 1.0
    assert sc.gpu_fraction({"size": 100, "size_vram": 80}) == 0.8
    assert sc.gpu_fraction(None) is None


def test_tie_prefers_faster_and_flags_tie():
    ms = [metrics("slowish", agent_success=0.9, tools_t0=0.9, tools_t06=0.9, agent_median_s=60),
          metrics("fastish", agent_success=0.9, tools_t0=0.9, tools_t06=0.9, agent_median_s=20)]
    scored = sc.score_all(ms)
    rec = sc.recommend(scored)
    assert rec["recommended"] == "fastish" and rec["runner_up"] == "slowish"
    # scores differ by speed points only; 0.15*(1-1/3)*100 = 10 points: not a tie
    assert rec["tied"] is False
    ms[0].agent_median_s = 21
    rec = sc.recommend(sc.score_all(ms))
    assert rec["tied"] is True and rec["recommended"] == "fastish"


def test_tie_band_picks_faster_even_with_slightly_lower_score():
    scored = {"hi": {"score": 80.0, "agent_median_s": 50.0, "eligible": True},
              "lo": {"score": 77.5, "agent_median_s": 20.0, "eligible": True}}
    assert sc.recommend(scored)["recommended"] == "lo"
    scored["lo"]["score"] = 76.9
    assert sc.recommend(scored)["recommended"] == "hi"


def test_ineligible_never_recommended_and_none_eligible_reported():
    ms = [metrics("split", gpu_fraction=0.7, agent_success=1, tools_t0=1, tools_t06=1),
          metrics("ok", agent_success=0.2, tools_t0=0.2, tools_t06=0.2)]
    rec = sc.recommend(sc.score_all(ms))
    assert rec["recommended"] == "ok"
    rec = sc.recommend(sc.score_all([metrics("split", gpu_fraction=0.7)]))
    assert rec["recommended"] is None and rec["best_by_score_among_gate_failures"] == "split"


def test_thinking_verdict():
    m = metrics("a", agent_success=0.6, tools_t0=0.8, agent_success_nothink=0.6, tools_nothink=0.76,
                agent_median_nothink_s=10)
    v = sc.thinking_verdict(m)
    assert v["disable_thinking"] is True and v["faster"] is True
    m.agent_success_nothink = 0.5
    assert sc.thinking_verdict(m)["disable_thinking"] is False
    m.agent_success_nothink = None
    assert sc.thinking_verdict(m)["disable_thinking"] is None


def test_phase_deadline_cumulative():
    assert sc.phase_deadline(100.0, 1000.0, "perf") == 200.0
    assert sc.phase_deadline(100.0, 1000.0, "agent_nothink") == 1100.0
