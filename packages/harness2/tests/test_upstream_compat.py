"""Pins every coupling `contained_proposer.py` has to upstream's PRIVATE or fragile surface.

The binding subclasses `harness2.core.proposer.Proposer` and overrides names upstream never promised to keep
(`_claude_cli`, `_opencode`, `_cached`, `_write_audit`), relies on the `(text, cost, err)` tuple, the harvest path,
the closed `CHANNELS` tuple, `schedule._selector_for` and two retry constants. Each is pinned here against the real
upstream at the pinned commit, and each failure names WHAT drifted and the commit, so a bump of the pin in
pyproject.toml (`uv lock`) turns into a short, specific list of places to re-audit instead of a silent change in
what a session is.

Nothing here starts a container or a process: upstream's real `Proposer.call` is exercised with a fake session method.
"""
from __future__ import annotations

import dataclasses
import inspect
import re
from pathlib import Path

import pytest
from h2_upstream import isolated_upstream

from cognitive_harness2 import contained_proposer as cp, upstream_egress
from cognitive_harness2.upstream_import import PINNED_COMMIT

WHERE = f"upstream {PINNED_COMMIT[:12]} (google-research/harness2)"


def drift(what: str) -> None:
    pytest.fail(f"UPSTREAM DRIFT at {WHERE}: {what}. cognitive_harness2/contained_proposer.py binds to this; "
                "re-audit it (and the README 'Proposer binding' section) before moving the pin.", pytrace=False)


def pin(condition: object, what: str) -> None:
    if not condition:
        drift(what)


def params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters)


@pytest.fixture(scope="module")
def up(tmp_path_factory):
    with isolated_upstream(tmp_path_factory.mktemp("runs")) as u:
        yield u


@pytest.fixture
def P(up):
    return up.proposer.Proposer


# ── the dispatch targets and their tuple ──────────────────────────────────────────

def test_the_two_private_session_methods_exist_with_their_signature_and_tuple(P):
    for name in ("_claude_cli", "_opencode"):
        pin(hasattr(P, name), f"Proposer.{name} no longer exists (we override it as the session entry point)")
        pin(params(getattr(P, name)) == ["self", "cwd", "mission", "system", "wall"],
            f"Proposer.{name} now takes {params(getattr(P, name))}, not (self, cwd, mission, system, wall)")
        ret = inspect.signature(getattr(P, name)).return_annotation
        pin(ret in ("tuple[str, float, str]", tuple[str, float, str]),
            f"Proposer.{name} returns {ret!r}, not the (text, cost, err) tuple")


def test_call_still_dispatches_on_spec_kind_to_those_two_names(P):
    src = inspect.getsource(P.call)
    pin(re.search(r'self\._claude_cli\s+if self\.spec\.kind == "claude_cli"\s+else self\._opencode', src),
        "Proposer.call no longer picks `self._claude_cli if self.spec.kind == \"claude_cli\" else self._opencode`")
    pin("text, cost, err" in src and "spent_usd += cost" in src and "not text.strip()" in src,
        "Proposer.call no longer unpacks `text, cost, err`, adds the cost and treats an empty text as a failed attempt")
    pin("self._harvest(tmp, harvest, label, cache_key)" in src and "shutil.copytree(context_dir, tmp / \"context\"" in src,
        "Proposer.call no longer copies context_dir to tmp/context and harvests tmp/<harvest> through self._harvest")


def test_call_signature_and_defaults(P):
    sig = inspect.signature(P.call)
    pin(list(sig.parameters) == ["self", "mission", "context_dir", "label", "system", "timeout", "max_attempts", "harvest"],
        f"Proposer.call now takes {list(sig.parameters)}")
    pin(sig.parameters["system"].default == "" and all(sig.parameters[n].default is None
                                                      for n in ("timeout", "max_attempts", "harvest")),
        "a default of Proposer.call changed")


def test_the_fields_and_the_overridden_hooks(P, up):
    names = [f.name for f in dataclasses.fields(P)]
    pin(names == ["spec", "audit_dir", "attempts", "project", "region", "_calls"], f"Proposer fields are now {names}")
    pin(next(f for f in dataclasses.fields(P) if f.name == "attempts").default == 2,
        "Proposer.attempts no longer defaults to 2 (our attempts=1 is a deviation from THAT default)")
    pin(params(P._write_audit) == ["self", "r", "mission", "system", "cache_key"], f"Proposer._write_audit is {params(P._write_audit)}")
    pin(params(P._cached) == ["self", "label", "cache_key", "require_workspace"], f"Proposer._cached is {params(P._cached)}")
    pin(params(P._harvest) == ["self", "tmp", "sub", "label", "cache_key"], f"Proposer._harvest is {params(P._harvest)}")
    pin(params(P.env) == ["self"], "Proposer.env takes arguments now")
    reply_fields = [f.name for f in dataclasses.fields(up.proposer.Reply)]
    pin(reply_fields == ["label", "text", "ok", "cost_usd", "wall_s", "attempts", "cached", "channel", "error", "audit",
                         "workspace"], f"Reply fields are now {reply_fields}")
    pin(issubclass(up.proposer.ProposerError, RuntimeError) and "attempts" in inspect.signature(up.proposer.ProposerError).parameters
        and "cost_usd" in inspect.signature(up.proposer.ProposerError).parameters, "ProposerError lost attempts/cost_usd")


def test_the_parsers_we_reuse_still_parse_what_the_clis_print(up):
    P = up.proposer
    for name in ("_parse_json", "_opencode_stdout", "_opencode_cost", "_strip_oc_banner"):
        pin(callable(getattr(P, name, None)), f"{name} no longer exists in core.proposer (we parse stdout with it)")
    events = "\n".join([
        '{"type":"step_start","part":{"type":"step-start"}}',
        '{"type":"text","part":{"type":"text","text":"HELLO"}}',
        '{"type":"error","error":{"data":{"message":"E1"}}}',
        '{"type":"step_finish","part":{"type":"step-finish","cost":0.25}}'])
    pin(P._opencode_stdout(events) == ("HELLO", "E1"), f"_opencode_stdout gives {P._opencode_stdout(events)!r}")
    pin(P._opencode_cost(events) == 0.25, f"_opencode_cost gives {P._opencode_cost(events)!r}")
    pin(P._parse_json('{"result":"R","total_cost_usd":1.5}') == {"result": "R", "total_cost_usd": 1.5}, "_parse_json changed")
    pin(P._parse_json("banner\n" + '{"result":"R"}') == {"result": "R"}, "_parse_json no longer tolerates a banner")


# ── the real call path, with a fake session method ────────────────────────────────

def _fake(P, up, text_cost_err, on_session=None):
    class Fake(P):
        def _opencode(self, cwd, mission, system, wall):
            if on_session:
                on_session(cwd, mission, system, wall)
            return text_cost_err

        _claude_cli = _opencode
    return Fake


def test_the_tuple_cost_and_error_reach_the_reply(up, tmp_path):
    P = up.proposer.Proposer
    spec = up.spec.ChannelSpec("opencode", "p/m")
    ok = _fake(P, up, ("answer", 0.25, ""))(spec, attempts=1, project="", region="").call("m", None, "x")
    pin(ok.ok and ok.text == "answer" and ok.cost_usd == 0.25 and ok.attempts == 1, "the (text, cost, err) tuple no longer feeds the Reply")
    with pytest.raises(up.proposer.ProposerError) as e:
        _fake(P, up, ("", 0.5, "boom"))(spec, attempts=1, project="", region="").call("m", None, "y")
    pin("boom" in str(e.value) and e.value.attempts == 1 and e.value.cost_usd == 0.5,
        "an empty text with an err no longer becomes ProposerError(err, attempts, cost_usd)")


def test_the_harvest_path_comes_back_in_reply_workspace(up, tmp_path):
    P = up.proposer.Proposer
    context = tmp_path / "ctx"
    (context / "harness").mkdir(parents=True)
    (context / "harness" / "seed.md").write_text("S")
    seen = {}

    def session(cwd, mission, system, wall):
        seen["wall"] = wall
        (cwd / "context" / "harness" / "edited.md").write_text("E")
        seen["seed"] = (cwd / "context" / "harness" / "seed.md").read_text()

    fake = _fake(P, up, ("done", 0.0, ""), session)(up.spec.ChannelSpec("opencode", "p/m", timeout=77), audit_dir=tmp_path / "audit",
                                                    attempts=1, project="", region="")
    r = fake.call("m", context, "lbl", harvest="context/harness")
    pin(seen["seed"] == "S", "Proposer.call no longer copies context_dir to <tmp>/context")
    pin(seen["wall"] == 77, "Proposer.call no longer passes spec.timeout as the wall clock")
    pin(r.workspace is not None and (r.workspace / "edited.md").read_text() == "E" and (r.workspace / "seed.md").is_file(),
        "harvest='context/harness' no longer returns <tmp>/context/harness as Reply.workspace")


# ── the closed tuple, the selector, the constants ─────────────────────────────────

def test_channels_is_the_closed_tuple_that_forces_a_carrier_kind(up):
    pin(up.spec.CHANNELS == ("claude_cli", "opencode"), f"CHANNELS is now {up.spec.CHANNELS!r}")
    pin(set(upstream_egress.KINDS) == set(up.spec.CHANNELS), "upstream_egress.KINDS no longer equals upstream's CHANNELS")
    try:
        up.spec.ChannelSpec("agy_cli", "m")
    except ValueError:
        pass
    else:
        drift("ChannelSpec now accepts a third kind: the carrier-kind workaround in contained_proposer can be dropped")
    for kind, carrier in cp.CARRIER_KIND.items():
        pin(carrier in up.spec.CHANNELS, f"the carrier kind {carrier!r} for {kind} is not an upstream channel")


def test_selector_for_still_builds_a_plain_proposer_unless_given_one(up):
    fn = up.schedule._selector_for
    pin(params(fn) == ["adapter", "prop", "selector_prop"], f"_selector_for takes {params(fn)}")
    src = inspect.getsource(fn)
    pin("if selector_prop is not None" in src and "return selector_prop" in src, "_selector_for no longer prefers selector_prop")
    pin("Proposer(" in src, "_selector_for no longer builds a plain Proposer (the hazard make_selector_prop avoids)")
    for name in ("run_task", "run_sweep"):
        ps = params(getattr(up.schedule, name))
        pin("selector_prop" in ps and "modes" in ps and "libraries" in (params(up.schedule.run_task)),
            f"schedule.{name} lost selector_prop/modes/libraries")


def test_the_retry_constants_exist_under_their_names_and_take_the_override(up):
    for modname, name in cp.ATTEMPT_OVERRIDES:
        mod = getattr(up, modname)
        pin(isinstance(getattr(mod, name, None), int), f"{mod.__name__}.{name} is gone or not an int")
    src = inspect.getsource(up.recursion)
    pin("range(1, STAGE_ATTEMPTS + 1)" in src and "max_attempts=TRANSPORT_ATTEMPTS" in src,
        "recursion no longer reads STAGE_ATTEMPTS / TRANSPORT_ATTEMPTS at call time, so the override may have no effect")
    applied = cp.apply_overrides(up, stage_attempts=1, transport_attempts=1)
    pin(applied == {"recursion.STAGE_ATTEMPTS": 1, "recursion.TRANSPORT_ATTEMPTS": 1, "config.TRANSPORT_ATTEMPTS": 1},
        f"apply_overrides reported {applied}")


def test_upstreams_own_semaphore_is_still_at_module_level(up):
    pin(hasattr(up.proposer, "_SEM") and hasattr(up.proposer, "MAX_CONCURRENCY"),
        "core.proposer lost _SEM / MAX_CONCURRENCY: our model gate sits INSIDE that semaphore, re-check the ordering")


def test_a_drift_failure_names_what_drifted_and_the_commit():
    with pytest.raises(pytest.fail.Exception) as e:
        pin(False, "Proposer._opencode now takes (self, x)")
    text = str(e.value)
    assert "UPSTREAM DRIFT" in text and PINNED_COMMIT[:12] in text and "Proposer._opencode now takes (self, x)" in text
    pin(True, "never shown")
