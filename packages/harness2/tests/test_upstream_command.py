"""Seam 2, command line: the argv must be exactly the expected shape for the channel kind.

Upstream builds the command itself, and a flag can outrank the environment (`--api-base`,
`--proxy`, `--endpoint`). So the command is ALLOW-LISTED, not scanned for bad words: the
executable plus a fixed set of flags per kind, each at most once, with values checked; any
other flag, any joined `--x=v` (except the joined flags of the one CLI that is invoked that way),
any unicode dash, `--`, response file (`@file`) or positional outside the expected shape is refused.

The shapes are the CLIs this package really drives: opencode v2 (`opencode run --standalone --auto
--format json --model M PROMPT`), claude 2.1 headless (`claude -p ...`, prompt on stdin) and agy
(`agy --print=PROMPT --dangerously-skip-permissions --model=M [--effort=low]`).
"""
from __future__ import annotations

import pytest
from h2_profiles import LOCAL, PROFILE, WORK

from cognitive_harness2 import upstream_egress
from cognitive_harness2.upstream_egress import COMMAND_SHAPES, check_proposer

SENTINEL = "SENTINEL-cmd-4d2b"
ENV = {"PATH": "/usr/bin", "HOME": "/tmp/h"}
OC = ("opencode", "run", "--standalone", "--auto", "--format", "json", "--model", "ollama/qwen3:30b",
      "Read MISSION.md first. Do what it asks.")
OC_BARE = ("opencode", "run", "Read MISSION.md first.")
CL = ("claude", "-p", "--output-format", "json", "--permission-mode", "bypassPermissions", "--no-session-persistence",
      "--model", "claude-x", "--effort", "high", "--append-system-prompt", "Follow the rules in the mission.",
      "--max-budget-usd", "2.5")
AG = ("agy", "--print=Read MISSION.md first. Do what it asks.", "--dangerously-skip-permissions",
      "--model=gemini-3-pro", "--effort=low")


def oc(command, **over):
    c = dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=ENV, cwd=WORK, command=tuple(command))
    c.update(over)
    return check_proposer(c, profile=PROFILE, channel="local", classification="open")


def cl(command, **over):
    c = dict(kind="claude_cli", model="claude-x", base_url="https://api.anthropic.com", env=ENV, command=tuple(command))
    c.update(over)
    return check_proposer(c, profile=PROFILE, channel="claude", classification="open", zdr="off")


def ag(command, **over):
    c = dict(kind="agy_cli", model="gemini-3-pro", base_url="https://generativelanguage.googleapis.com", env=ENV,
             command=tuple(command))
    c.update(over)
    return check_proposer(c, profile=PROFILE, channel="agy", classification="open", zdr="off")


MAKE = {"opencode": (oc, OC), "claude": (cl, CL), "agy": (ag, AG)}


def shaped(d):
    return [r for r in d.reasons if "rule command-shape" in r]


def with_extra(base, *extra):
    return (*base, *extra)


def check_for(argv):
    return {"opencode": oc, "claude": cl, "agy": ag}[argv[0].rsplit("/", 1)[-1]]


# ── the expected shapes pass ──────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [OC, OC_BARE, ("/opt/cli/opencode", *OC[1:]),
                                  ("opencode", "run", "--model", "ollama/qwen3:30b", "--auto", "--standalone", "go"),
                                  ("opencode", "run", "--format", "json", "go")])
def test_the_expected_opencode_shapes_are_allowed(argv):
    d = oc(argv)
    assert d.allowed, d.reasons


@pytest.mark.parametrize("argv", [CL, ("claude",), ("claude", "-p"), ("/usr/local/bin/claude", *CL[1:]),
                                  ("claude", "--append-system-prompt", "See https://example.com/rules for the rules"),
                                  ("claude", "-p", "--tools", "Bash,Edit,Write,Read"),
                                  ("claude", "-p", "--allowedTools", "Bash(git:*)"),
                                  ("claude", "-p", "--disallowedTools", "WebFetch"),
                                  ("claude", "-p", "--permission-mode", "acceptEdits")])
def test_the_expected_claude_shapes_are_allowed(argv):
    d = cl(argv)
    assert d.allowed, d.reasons


@pytest.mark.parametrize("argv", [AG, ("agy", "--print=go", "--model=gemini-3-pro"),
                                  ("/opt/cli/agy", *AG[1:]), ("agy", "--model=gemini-3-pro", "--print=Compare with https://example.com/x")])
def test_the_expected_agy_shapes_are_allowed(argv):
    d = ag(argv)
    assert d.allowed, d.reasons


def test_the_flag_allow_lists_are_data_next_to_the_kinds():
    assert set(COMMAND_SHAPES) == set(upstream_egress.CHECKED_KINDS)
    assert "--model" in COMMAND_SHAPES["opencode"].flags and "-p" in COMMAND_SHAPES["claude_cli"].flags
    assert set(COMMAND_SHAPES["agy_cli"].joined) == {"--print", "--model", "--effort"}
    for shape in COMMAND_SHAPES.values():
        names = [*shape.flags, *shape.joined]
        assert not any(w in f for f in names for w in ("base", "proxy", "endpoint", "url", "host", "gateway"))


def test_opencode_v1_flags_are_gone():
    for flag in ("--dir", "--variant", "--cwd", "--conditions=browser"):
        assert flag not in COMMAND_SHAPES["opencode"].flags
    assert not oc(("bun", "run", "src/index.ts", "run", "go", "--dir", WORK)).allowed
    for argv in (with_extra(OC[:-1], "--dir", WORK, OC[-1]), with_extra(OC[:-1], "--variant", "high", OC[-1]),
                 with_extra(OC[:-1], "--conditions=browser", OC[-1])):
        assert shaped(oc(argv)), argv


def test_the_standard_continuations_of_the_other_clis_are_not_opencode_flags():
    for flag in ("-p", "--print=x", "--permission-mode", "--no-session-persistence", "--dangerously-skip-permissions"):
        assert shaped(oc(with_extra(OC[:-1], flag, OC[-1]))), flag


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
@pytest.mark.parametrize("which", list(MAKE))
def test_an_endpoint_flag_in_any_style_is_refused(which, flag, style):
    make, base = MAKE[which]
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
@pytest.mark.parametrize("which", list(MAKE))
def test_short_flags_outside_the_list_are_refused(which, extra):
    make, base = MAKE[which]
    d = make(with_extra(base, *extra))
    assert shaped(d), (which, extra)


@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "―", "−", "﹣", "－",
                                  "֊", "᠆", "⸺"])
def test_unicode_dashes_are_refused(dash):
    for argv in (with_extra(OC, f"{dash}{dash}proxy", URLISH), with_extra(OC, f"{dash}H", URLISH),
                 with_extra(OC, f"{dash}{dash}model", "ollama/qwen3:30b"),    # even a lookalike of an ALLOWED flag
                 with_extra(CL, f"{dash}{dash}api-base={URLISH}"),
                 with_extra(AG, f"{dash}{dash}print=x"),
                 with_extra(AG, f"{dash}{dash}api-base={URLISH}")):
        d = check_for(argv)(argv)
        assert not d.allowed and shaped(d), argv


def test_a_leading_space_does_not_hide_a_flag():
    assert shaped(oc(with_extra(OC, " --proxy", URLISH)))
    assert shaped(oc(with_extra(OC, "\t--proxy=x")))
    assert shaped(ag(with_extra(AG, " --proxy=x")))


def test_repeated_flags_are_refused_even_when_allowed_once():
    assert shaped(oc(with_extra(OC, "--model", "ollama/qwen3:30b")))
    assert shaped(oc(with_extra(OC, "--format", "json")))
    assert shaped(oc(with_extra(OC, "--auto")))
    assert shaped(cl(with_extra(CL, "-p")))
    assert shaped(cl(with_extra(CL, "--effort", "low")))
    assert shaped(ag(with_extra(AG, "--model=gemini-3-pro")))
    assert shaped(ag(with_extra(AG, "--print=again")))
    assert shaped(ag(with_extra(AG, "--dangerously-skip-permissions")))


@pytest.mark.parametrize("tail", [
    ("--", "--base-url=http://evil.example"), ("--", "--proxy", URLISH), ("--",), ("--", "run", "x"),
    ("---",), ("--=",), ("-",), ("-=",),
])
@pytest.mark.parametrize("which", list(MAKE))
def test_end_of_options_and_bare_dashes_are_refused(which, tail):
    make, base = MAKE[which]
    assert shaped(make(with_extra(base, *tail)))


def test_a_flag_smuggled_after_double_dash_in_the_middle_is_refused():
    argv = list(OC)
    argv[7:7] = ["--", "--api-base=http://evil.example"]
    assert shaped(oc(argv))
    argv = list(AG)
    argv[2:2] = ["--", "--api-base=http://evil.example"]
    assert shaped(ag(argv))


@pytest.mark.parametrize("token", ["@/etc/passwd", "@args.txt", "@", "@@x", "@http://evil.example/cfg"])
def test_response_files_are_refused(token):
    for argv in (with_extra(OC, token), with_extra(CL, token), with_extra(OC[:-1], "--model", token, OC[-1]),
                 with_extra(AG, token), (*AG[:1], f"--print={token}", *AG[2:])):
        d = check_for(argv)(argv)
        assert not d.allowed and shaped(d), argv
    assert shaped(cl((*CL[:11], token, *CL[12:])))             # as the system prompt value


# ── allowed flags with a bad value ────────────────────────────────────────────────

BAD_VALUES = {
    "model elsewhere": ("--model", "ollama/other"),
    "model flaglike": ("--model", "--proxy"),
    "model url": ("--model", URLISH),
    "format xml": ("--format", "xml"),
    "format url": ("--format", URLISH),
    "format default": ("--format", "default"),
}


@pytest.mark.parametrize("name", list(BAD_VALUES))
def test_an_allowed_flag_with_an_unacceptable_value_is_refused(name):
    flag, value = BAD_VALUES[name]
    argv = list(OC)
    argv[argv.index(flag) + 1] = value
    d = oc(argv)
    assert shaped(d), d.reasons
    assert "evil.example" not in " ".join(d.reasons)


OC_PROMPT_BAD = {"url": URLISH, "setting": "OPENAI_BASE_URL=x", "hostport": "evil.example:11434", "dash": "-",
                 "nul": "go\x00", "response file": "@x", "huge": "x" * 5000}


@pytest.mark.parametrize("name", list(OC_PROMPT_BAD))
def test_a_bad_opencode_prompt_is_refused(name):
    assert shaped(oc((*OC[:-1], OC_PROMPT_BAD[name])))


CL_BAD_VALUES = {
    "output-format url": ("--output-format", URLISH),
    "output-format text": ("--output-format", "text"),
    "effort url": ("--effort", "http://evil"),
    "effort spaces": ("--effort", "very high"),
    "model elsewhere": ("--model", "claude-other"),
    "model url": ("--model", URLISH),
    "permission-mode default": ("--permission-mode", "default"),
    "permission-mode url": ("--permission-mode", URLISH),
    "permission-mode dash": ("--permission-mode", "--proxy"),
    "budget text": ("--max-budget-usd", "lots"),
    "budget url": ("--max-budget-usd", URLISH),
    "budget negative": ("--max-budget-usd", "-1"),
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


@pytest.mark.parametrize("flag", ["--tools", "--allowedTools", "--disallowedTools"])
@pytest.mark.parametrize("value", [URLISH, "--proxy", "-p", "a\nb", "x" * 300, "", "@list", "http://evil"])
def test_a_bad_claude_tool_list_is_refused(flag, value):
    assert shaped(cl(("claude", "-p", flag, value)))


AG_BAD = {
    "model elsewhere": "--model=gemini-other",
    "model url": f"--model={URLISH}",
    "model empty": "--model=",
    "effort high": "--effort=high",
    "effort url": f"--effort={URLISH}",
    "print dash": "--print=-p",
    "print url": f"--print={URLISH}",
    "print setting": "--print=OPENAI_BASE_URL=x",
    "print hostport": "--print=evil.example:8080",
    "print control": "--print=a\x00b",
    "print huge": "--print=" + "x" * 5000,
}


@pytest.mark.parametrize("name", list(AG_BAD))
def test_a_bad_agy_value_is_refused(name):
    bad = AG_BAD[name]
    argv = [a for a in AG if not a.startswith(bad.split("=", 1)[0] + "=")] + [bad]
    d = ag(argv)
    assert shaped(d), d.reasons
    assert "evil.example" not in " ".join(d.reasons)


def test_agy_flags_written_as_two_tokens_are_refused():
    for argv in (("agy", "--print", "go"), ("agy", "--model", "gemini-3-pro"), ("agy", "--effort", "low"),
                 ("agy", "--print=go", "--model", "gemini-3-pro")):
        assert shaped(ag(argv)), argv


def test_a_joined_flag_that_is_not_agys_is_refused_for_the_others():
    assert shaped(oc(with_extra(OC[:-1], "--model=ollama/qwen3:30b", OC[-1])))
    assert shaped(oc(with_extra(OC[:-1], "--format=json", OC[-1])))
    assert shaped(cl(with_extra(CL, "--model=claude-x")))
    assert shaped(cl(("claude", "--output-format=json")))


def test_a_flag_missing_its_value_is_refused():
    d = oc(OC[:-2])                                # ends with `--model` and no value, no prompt
    assert shaped(d) and any("missing its value" in r for r in d.reasons)
    assert shaped(cl((*CL[:-1],)))                 # --max-budget-usd with no value
    assert shaped(cl(("claude", "--effort")))


def test_a_value_given_to_a_flag_that_takes_none_is_a_stray_positional():
    assert shaped(cl(("claude", "-p", "extra")))
    assert shaped(cl(("claude", "--no-session-persistence", "yes")))
    assert shaped(oc(("opencode", "run", "--auto", "yes", "go")))
    assert shaped(ag(("agy", "--dangerously-skip-permissions", "yes")))


# ── positionals ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ("opencode", "run"),                                          # missing prompt
    ("opencode", "run", "go", "extra"),                           # extra positional
    ("opencode", "serve", "go"),
    ("opencode", "run", "go", "more"),
    ("opencode",),
    ("opencode", "go"),
    ("opencode", "run", "http://evil.example/v1"),                # the prompt is itself a URL
    ("opencode", "run", "proxy=http://evil.example"),
    ("opencode", "run", "OPENAI_BASE_URL=x"),
    ("opencode", "run", "evil.example:11434"),
    ("opencode", "run", "-"),
    ("opencode", "run", "go\x00"),
    ("bun", "run", "src/index.ts", "run", "go"),                  # the old way in: bun is no longer a recognised executable
    ("bun", "run", "--cwd", "/opt/oc", "src/index.ts", "run", "go"),
])
def test_unexpected_positionals_are_refused(argv):
    assert shaped(oc(argv))


def test_a_prompt_with_a_url_inside_prose_is_allowed():
    assert oc(("opencode", "run", "Compare with https://example.com/doc please")).allowed
    assert ag(("agy", "--print=Compare with https://example.com/doc please")).allowed


def test_claude_and_agy_take_no_positionals():
    assert shaped(cl(("claude", "-p", "do the thing")))
    assert shaped(cl(("claude", "do the thing")))
    assert shaped(ag(("agy", "do the thing")))
    assert shaped(ag((*AG, "do the thing")))


# ── the executable and the kind ───────────────────────────────────────────────────

def test_the_shape_is_the_one_for_the_declared_kind():
    d = check_proposer(dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=ENV, cwd=WORK,
                            command=CL), profile=PROFILE, channel="local", classification="open")
    assert not d.allowed
    assert any("does not match" in r for r in d.reasons)
    d = check_proposer(dict(kind="agy_cli", model="gemini-3-pro", base_url="https://generativelanguage.googleapis.com",
                            env=ENV, command=OC), profile=PROFILE, channel="agy", classification="open", zdr="off")
    assert any("does not match" in r for r in d.reasons)


def test_bun_is_not_a_recognised_executable_any_more():
    d = oc(("bun", "run", "src/index.ts", "run", "go"))
    assert not d.allowed and any("not a CLI this gate recognises" in r for r in d.reasons)


def test_no_command_at_all_is_not_a_shape_problem():
    d = check_proposer(dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=ENV, cwd=WORK),
                       profile=PROFILE, channel="local", classification="open")
    assert d.allowed and not shaped(d)


def test_agy_is_checked_like_the_others_and_its_google_host_is_not_a_vertex_route():
    d = ag(AG)
    assert d.allowed, d.reasons
    assert d.view["kind"] == "agy_cli" and d.view["provider"] == "google"
    assert any("Google AI subscription" in w for w in d.warnings)
    vertex = ag(AG, base_url="https://aiplatform.googleapis.com")
    assert not vertex.allowed and any("rule vertex" in r for r in vertex.reasons)


def test_an_agy_model_must_be_a_bare_id():
    d = check_proposer(dict(kind="agy_cli", model="google/gemini", base_url="https://generativelanguage.googleapis.com",
                            env=ENV), profile=PROFILE, channel="agy", classification="open", zdr="off")
    assert not d.allowed and any("rule provider" in r for r in d.reasons)


# ── redaction ─────────────────────────────────────────────────────────────────────

def test_no_argument_value_reaches_a_reason_or_the_view():
    d = oc((*OC, f"--api-base={SENTINEL}", "--proxy", SENTINEL, f"@{SENTINEL}", "--", SENTINEL))
    assert not d.allowed
    text = repr(d) + " ".join(d.reasons) + repr(d.to_dict())
    assert SENTINEL not in text
    d = ag((*AG, f"--api-base={SENTINEL}", f"--print={SENTINEL}", f"@{SENTINEL}"))
    assert not d.allowed
    assert SENTINEL not in repr(d) + " ".join(d.reasons) + repr(d.to_dict())


def test_hostile_flag_names_are_escaped_and_truncated_in_reasons():
    d = oc((*OC, "--\x1b[31m" + "z" * 200 + "=1"))
    text = " ".join(d.reasons)
    assert "\x1b" not in text and "z" * 60 not in text
    assert "\x1b" not in repr(d.view["command"]) and "z" * 60 not in repr(d.view["command"])


# ── opencode --title (measured against the real v2.0.25: with it, no separate title-generation model call) ─────

def test_the_opencode_title_flag_is_allowed_with_a_plain_word_only():
    assert oc(with_extra(OC[:-1], "--title", "adapt", OC[-1])).allowed
    for bad in ("http://evil.example", "../etc", "two words", "A" * 40, "", "x=y", "-x"):
        assert shaped(oc(with_extra(OC[:-1], "--title", bad, OC[-1]))), bad
