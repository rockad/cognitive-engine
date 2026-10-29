"""Seam 2, command line: the argv must be exactly the expected shape for the channel kind.

Upstream builds the command itself, and a flag can outrank the environment (`--api-base`,
`--proxy`, `--endpoint`). So the command is ALLOW-LISTED, not scanned for bad words: the
executable plus a fixed set of flags per kind, each at most once, with values checked; any
other flag, any joined `--x=v`, any unicode dash, `--`, response file (`@file`) or positional
outside the expected shape is refused.
"""
from __future__ import annotations

import pytest
from h2_profiles import LOCAL, PROFILE, WORK

from cognitive_harness2 import upstream_egress
from cognitive_harness2.upstream_egress import COMMAND_SHAPES, check_proposer

SENTINEL = "SENTINEL-cmd-4d2b"
ENV = {"PATH": "/usr/bin", "HOME": "/tmp/h"}
OC = ("bun", "run", "--cwd", "/opt/oc/packages/opencode", "--conditions=browser", "src/index.ts", "run",
      "Read MISSION.md first. Do what it asks.", "--dir", WORK, "--model", "ollama/qwen3:30b",
      "--format", "json")
OC_BARE = ("opencode", "run", "Read MISSION.md first.", "--model", "ollama/qwen3:30b")
CL = ("claude", "--dangerously-skip-permissions", "-p", "--output-format", "json", "--effort", "high",
      "--append-system-prompt", "Follow the rules in the mission.")


def oc(command, **over):
    c = dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=ENV, cwd=WORK, command=tuple(command))
    c.update(over)
    return check_proposer(c, profile=PROFILE, channel="local", classification="open")


def cl(command, **over):
    c = dict(kind="claude_cli", model="claude-x", base_url="https://api.anthropic.com", env=ENV, command=tuple(command))
    c.update(over)
    return check_proposer(c, profile=PROFILE, channel="claude", classification="open", zdr="off")


def shaped(d):
    return [r for r in d.reasons if "rule command-shape" in r]


def with_extra(base, *extra):
    return (*base, *extra)


# ── the expected shapes pass ──────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [OC, OC_BARE, (*OC, "--variant", "high"),
                                  ("bun", "run", "src/index.ts", "run", "go", "--model", "ollama/qwen3:30b")])
def test_the_expected_opencode_shapes_are_allowed(argv):
    d = oc(argv)
    assert d.allowed, d.reasons


@pytest.mark.parametrize("argv", [CL, ("claude",), ("claude", "-p"), ("/usr/local/bin/claude", *CL[1:]),
                                  ("claude", "--append-system-prompt", "See https://example.com/rules for the rules")])
def test_the_expected_claude_shapes_are_allowed(argv):
    d = cl(argv)
    assert d.allowed, d.reasons


def test_the_flag_allow_lists_are_data_next_to_the_kinds():
    assert set(COMMAND_SHAPES) == set(upstream_egress.KINDS)
    assert "--model" in COMMAND_SHAPES["opencode"].flags and "-p" in COMMAND_SHAPES["claude_cli"].flags
    for shape in COMMAND_SHAPES.values():
        assert not any(w in f for f in shape.flags for w in ("base", "proxy", "endpoint", "url", "host", "gateway"))


# ── every endpoint-style flag, in every style ─────────────────────────────────────

URLISH = "http://evil.example:8080/v1"
STYLES = {
    "joined": lambda f, v: (f"{f}={v}",),
    "separate": lambda f, v: (f, v),
    "joined-no-value": lambda f, v: (f,),
}
FLAGS = ["--api-base", "--base-url", "--baseurl", "--proxy", "--endpoint", "--gateway", "--server", "--host", "--url",
         "--openai-base-url", "--API-BASE", "--Base-Url", "--api_base", "--http-proxy", "--https-proxy", "--no-proxy"]


@pytest.mark.parametrize("style", list(STYLES))
@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("make", [oc, cl], ids=["opencode", "claude"])
def test_an_endpoint_flag_in_any_style_is_refused(make, flag, style):
    base = OC if make is oc else CL
    d = make(with_extra(base, *STYLES[style](flag, URLISH)))
    assert not d.allowed and shaped(d), d.reasons
    assert "evil.example" not in " ".join(d.reasons) + " ".join(d.warnings)


@pytest.mark.parametrize("flag", FLAGS)
def test_an_endpoint_flag_in_front_of_the_prompt_is_refused(flag):
    argv = list(OC)
    argv[2:2] = [flag, URLISH]
    assert shaped(oc(argv))


@pytest.mark.parametrize("extra", [
    ("-H", "Authorization: x"), ("-H",), ("-x", "http://evil"), ("-u", "user"), ("-e", "A=B"), ("-X", "POST"),
    ("-pp",), ("-p=1",), ("-h",), ("-v",), ("-vvv",), ("-c", "/etc/x"),
])
def test_short_flags_outside_the_list_are_refused(extra):
    assert shaped(oc(with_extra(OC, *extra)))
    assert shaped(cl(with_extra(CL, *extra)))


@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "―", "−", "﹣", "－",
                                  "֊", "᠆", "⸺"])
def test_unicode_dashes_are_refused(dash):
    for argv in (with_extra(OC, f"{dash}{dash}proxy", URLISH), with_extra(OC, f"{dash}H", URLISH),
                 with_extra(OC, f"{dash}{dash}model", "ollama/qwen3:30b"),    # even a lookalike of an ALLOWED flag
                 with_extra(CL, f"{dash}{dash}api-base={URLISH}")):
        d = (oc if argv[0] == "bun" else cl)(argv)
        assert not d.allowed and shaped(d), argv


def test_a_leading_space_does_not_hide_a_flag():
    assert shaped(oc(with_extra(OC, " --proxy", URLISH)))
    assert shaped(oc(with_extra(OC, " --proxy", URLISH)))
    assert shaped(oc(with_extra(OC, "\t--proxy=x")))


def test_repeated_flags_are_refused_even_when_allowed_once():
    assert shaped(oc(with_extra(OC, "--model", "ollama/qwen3:30b")))
    assert shaped(oc(with_extra(OC, "--format", "json")))
    assert shaped(cl(with_extra(CL, "-p")))
    assert shaped(cl(with_extra(CL, "--effort", "low")))


@pytest.mark.parametrize("tail", [
    ("--", "--base-url=http://evil.example"), ("--", "--proxy", URLISH), ("--",), ("--", "run", "x"),
    ("---",), ("--=",), ("-",), ("-=",),
])
def test_end_of_options_and_bare_dashes_are_refused(tail):
    assert shaped(oc(with_extra(OC, *tail)))
    assert shaped(cl(with_extra(CL, *tail)))


def test_a_flag_smuggled_after_double_dash_in_the_middle_is_refused():
    argv = list(OC)
    argv[7:7] = ["--", "--api-base=http://evil.example"]
    assert shaped(oc(argv))


@pytest.mark.parametrize("token", ["@/etc/passwd", "@args.txt", "@", "@@x", "@http://evil.example/cfg"])
def test_response_files_are_refused(token):
    for argv in (with_extra(OC, token), with_extra(CL, token), with_extra(OC, "--model", token)):
        d = (oc if argv[0] == "bun" else cl)(argv)
        assert not d.allowed and shaped(d)
    assert shaped(cl((*CL[:-1], token)))                       # as the system prompt value


# ── allowed flags with a bad value ────────────────────────────────────────────────

BAD_VALUES = {
    "cwd url": ("--cwd", "http://evil.example/x"),
    "cwd relative": ("--cwd", "opencode"),
    "cwd control": ("--cwd", "/opt/x\n--proxy"),
    "dir elsewhere": ("--dir", "/tmp/other"),
    "dir url": ("--dir", URLISH),
    "model elsewhere": ("--model", "ollama/other"),
    "model flaglike": ("--model", "--proxy"),
    "format xml": ("--format", "xml"),
    "format url": ("--format", URLISH),
    "variant spaces": ("--variant", "a b"),
    "variant url": ("--variant", "http://evil"),
    "variant upper": ("--variant", "HIGH"),
    "variant long": ("--variant", "x" * 40),
}


@pytest.mark.parametrize("name", list(BAD_VALUES))
def test_an_allowed_flag_with_an_unacceptable_value_is_refused(name):
    flag, value = BAD_VALUES[name]
    argv = list(OC)
    if flag in argv:
        argv[argv.index(flag) + 1] = value
    else:
        argv += [flag, value]
    d = oc(argv)
    assert shaped(d), d.reasons
    assert "evil.example" not in " ".join(d.reasons)


CL_BAD_VALUES = {
    "output-format url": ("--output-format", URLISH),
    "output-format text": ("--output-format", "text"),
    "effort url": ("--effort", "http://evil"),
    "effort spaces": ("--effort", "very high"),
    "system flaglike": ("--append-system-prompt", "--api-base=http://evil.example"),
    "system dash": ("--append-system-prompt", "-p"),
    "system url": ("--append-system-prompt", "https://evil.example/system"),
    "system setting": ("--append-system-prompt", "ANTHROPIC_BASE_URL=http://evil.example"),
    "system hostport": ("--append-system-prompt", "evil.example:8080"),
    "system control": ("--append-system-prompt", "a\x00b"),
    "system huge": ("--append-system-prompt", "x" * 5000),
}


@pytest.mark.parametrize("name", list(CL_BAD_VALUES))
def test_a_bad_claude_value_is_refused(name):
    flag, value = CL_BAD_VALUES[name]
    argv = list(CL)
    argv[argv.index(flag) + 1] = value
    assert shaped(cl(argv))


def test_a_flag_missing_its_value_is_refused():
    assert shaped(oc((*OC[:-1],)))                 # --format with no value
    assert shaped(cl((*CL[:-1],)))                 # --append-system-prompt with no value
    assert shaped(cl(("claude", "--effort")))


def test_a_value_given_to_a_flag_that_takes_none_is_a_stray_positional():
    assert shaped(cl(("claude", "-p", "extra")))
    assert shaped(cl(("claude", "--dangerously-skip-permissions", "yes")))


# ── positionals ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ("bun", "run", "src/index.ts", "run"),                              # missing prompt
    ("bun", "run", "src/index.ts", "run", "go", "extra"),               # extra positional
    ("bun", "run", "evil.ts", "run", "go"),                             # another entry point
    ("bun", "x", "src/index.ts", "run", "go"),                          # not `run`
    ("bun", "src/index.ts", "run", "go"),
    ("bun",),
    ("opencode", "run"),
    ("opencode", "run", "go", "more"),
    ("opencode", "serve", "go"),
    ("bun", "run", "src/index.ts", "run", "http://evil.example/v1"),    # the prompt is itself a URL
    ("bun", "run", "src/index.ts", "run", "proxy=http://evil.example"),
    ("bun", "run", "src/index.ts", "run", "OPENAI_BASE_URL=x"),
    ("bun", "run", "src/index.ts", "run", "evil.example:11434"),
    ("bun", "run", "src/index.ts", "run", "-"),
    ("bun", "run", "src/index.ts", "run", "go\x00"),
])
def test_unexpected_positionals_are_refused(argv):
    assert shaped(oc(argv))


def test_a_prompt_with_a_url_inside_prose_is_allowed():
    assert oc(("bun", "run", "src/index.ts", "run", "Compare with https://example.com/doc please")).allowed


def test_claude_takes_no_positionals_the_prompt_arrives_on_stdin():
    assert shaped(cl(("claude", "-p", "do the thing")))
    assert shaped(cl(("claude", "do the thing")))


# ── the executable and the kind ───────────────────────────────────────────────────

def test_the_shape_is_the_one_for_the_declared_kind():
    d = check_proposer(dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=ENV, cwd=WORK,
                            command=CL), profile=PROFILE, channel="local", classification="open")
    assert not d.allowed
    assert any("does not match" in r for r in d.reasons)


def test_no_command_at_all_is_not_a_shape_problem():
    d = check_proposer(dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=ENV, cwd=WORK),
                       profile=PROFILE, channel="local", classification="open")
    assert d.allowed and not shaped(d)


# ── redaction ─────────────────────────────────────────────────────────────────────

def test_no_argument_value_reaches_a_reason_or_the_view():
    d = oc((*OC, f"--api-base={SENTINEL}", "--proxy", SENTINEL, f"@{SENTINEL}", "--", SENTINEL))
    assert not d.allowed
    text = repr(d) + " ".join(d.reasons) + repr(d.to_dict())
    assert SENTINEL not in text


def test_hostile_flag_names_are_escaped_and_truncated_in_reasons():
    d = oc((*OC, "--\x1b[31m" + "z" * 200 + "=1"))
    text = " ".join(d.reasons)
    assert "\x1b" not in text and "z" * 60 not in text
    assert "\x1b" not in repr(d.view["command"]) and "z" * 60 not in repr(d.view["command"])
