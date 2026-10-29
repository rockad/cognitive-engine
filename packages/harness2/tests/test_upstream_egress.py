"""Seam 2: the egress gate on an upstream Proposer configuration, BEFORE any session exists.

Nothing here starts a process, opens a socket or calls a model: `check_proposer` only reads
the configuration it is given. Every secret in these fixtures is a unique sentinel so the
redaction tests can search the whole decision for it.
"""
from __future__ import annotations

import json

import pytest

from cognitive_harness2 import settings, upstream_egress
from cognitive_harness2.upstream_egress import (
    Decision,
    ProposerConfig,
    check_proposer,
    from_upstream,
    parse_config,
)

LOCAL = "http://127.0.0.1:11434/v1"
SENTINEL = "SENTINEL-do-not-leak-7f3a"
CLEAN_ENV = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": "/tmp/h"}
ARGV = ("bun", "run", "--cwd", "/x", "src/index.ts", "run", f"PROMPT {SENTINEL}", "--dir", "/tmp/work",
        "--model", "ollama/qwen3:30b", "--format", "json")


def cfg(**over) -> ProposerConfig:
    base = dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=dict(CLEAN_ENV), cwd="/tmp/work")
    base.update(over)
    return ProposerConfig(**base)


@pytest.fixture
def st(tmp_path):
    def make(*, zdr="", claude=None):
        env = {settings.ENV_EXPERIENCE_ROOT: str(tmp_path / "exp")}
        if zdr:
            env[settings.ENV_ZDR_ENDPOINTS] = zdr
        if claude is not None:
            env[settings.ENV_ALLOW_CLAUDE_PROPOSER] = claude
        return settings.from_env(env)
    return make


def dump(d: Decision) -> str:
    return json.dumps(d.to_dict(), default=str) + repr(d) + " ".join(d.reasons) + " ".join(d.warnings)


# ── the allowed case ──────────────────────────────────────────────────────────────

def test_a_local_model_on_loopback_is_allowed(st):
    d = check_proposer(cfg(), st())
    assert d.allowed is True
    assert any("local endpoint 127.0.0.1" in r for r in d.reasons)
    assert d.view["endpoint"] == "http://127.0.0.1:11434"
    assert d.view["kind"] == "opencode" and d.view["provider"] == "ollama"


@pytest.mark.parametrize("url", ["http://localhost:1234/v1", "http://[::1]:8080", "https://LOCALHOST/v1"])
def test_every_loopback_spelling_is_allowed_for_a_local_provider(st, url):
    assert check_proposer(cfg(base_url=url), st()).allowed


def test_an_allowed_decision_still_warns_that_the_session_runs_on_the_host(st):
    d = check_proposer(cfg(), st())
    assert d.allowed
    assert any("host" in w for w in d.warnings)
    assert any("HOME" in w for w in d.warnings)  # file-based credentials are invisible to this gate


def test_a_listed_zero_retention_host_is_allowed(st):
    c = cfg(model="ollama/qwen3", base_url="https://zdr.example/v1")
    assert check_proposer(c, st(zdr="zdr.example")).allowed
    d = check_proposer(c, st())
    assert not d.allowed and any("zdr.example" in r and "neither" in r for r in d.reasons)


def test_a_hosted_provider_on_a_listed_zdr_host_is_allowed(st):
    c = cfg(model="anthropic/claude-x", base_url="https://zdr.example/v1")
    assert check_proposer(c, st(zdr="zdr.example")).allowed


# ── refusals, one per reason ──────────────────────────────────────────────────────

def refused(d: Decision, *needles: str) -> None:
    assert d.allowed is False and d.reasons
    text = " | ".join(d.reasons)
    for n in needles:
        assert n in text, f"{n!r} not in {text!r}"


VERTEX = {
    "claude env flag": dict(kind="claude_cli", model="claude-x", env={**CLEAN_ENV, "CLAUDE_CODE_USE_VERTEX": "1"}),
    "provider prefix": dict(model="google-vertex/gemini-x"),
    "provider prefix anthropic": dict(model="google-vertex-anthropic/claude-x"),
    "vertex project env": dict(env={**CLEAN_ENV, "GOOGLE_VERTEX_PROJECT": "p"}),
    "anthropic vertex env": dict(env={**CLEAN_ENV, "ANTHROPIC_VERTEX_PROJECT_ID": "p"}),
    "vertexai location env": dict(env={**CLEAN_ENV, "VERTEXAI_LOCATION": "global"}),
    "cloud ml region env": dict(env={**CLEAN_ENV, "CLOUD_ML_REGION": "global"}),
    "googleapis host": dict(base_url="https://us-central1-aiplatform.googleapis.com/v1"),
    "project configured": dict(project="my-gcp-project"),
    "bedrock flag": dict(env={**CLEAN_ENV, "CLAUDE_CODE_USE_BEDROCK": "1"}),
}


@pytest.mark.parametrize("over", VERTEX.values(), ids=VERTEX.keys())
def test_vertex_is_refused(st, over):
    refused(check_proposer(cfg(**over), st(claude="1")), "Vertex")


def test_a_non_loopback_unlisted_endpoint_is_refused(st):
    refused(check_proposer(cfg(base_url="https://api.openai.com/v1", model="ollama/x"), st()), "api.openai.com")


@pytest.mark.parametrize("url", ["https://openrouter.ai/api/v1", "http://192.168.1.5:11434", "http://10.0.0.2",
                                 "http://127.0.0.1.nip.io", "http://localhost.evil.example", "http://0.0.0.0:80",
                                 "http://127.0.0.2:1"])
def test_lookalike_and_lan_hosts_are_not_loopback(st, url):
    assert not check_proposer(cfg(base_url=url), st()).allowed


def test_an_unknown_endpoint_is_refused(st):
    d = check_proposer(cfg(base_url=None), st())
    refused(d, "cannot be determined")
    assert d.view["endpoint"] is None


@pytest.mark.parametrize("url", ["", "not a url", "http://", "://x", "/v1", "localhost:11434", "ftp://127.0.0.1/x",
                                 "file:///etc/passwd", "http:///v1", f"http://user:{SENTINEL}@127.0.0.1/v1",
                                 f"http://127.0.0.1/v1?key={SENTINEL}", 5, ["http://127.0.0.1"]])
def test_an_unusable_base_url_is_refused_without_echoing_it(st, url):
    d = check_proposer(cfg(base_url=url), st())
    assert d.allowed is False
    assert SENTINEL not in dump(d)


def test_credentials_in_the_url_are_refused_and_never_shown(st):
    d = check_proposer(cfg(base_url=f"http://user:{SENTINEL}@127.0.0.1:11434/v1"), st())
    refused(d, "credentials")
    assert SENTINEL not in dump(d) and "user" not in json.dumps(d.to_dict()["view"])


def test_a_hosted_provider_behind_a_loopback_address_is_refused(st):
    refused(check_proposer(cfg(model="anthropic/claude-x", base_url=LOCAL), st()), "hosted")
    refused(check_proposer(cfg(model="openai/gpt-x", base_url=LOCAL), st()), "hosted")
    refused(check_proposer(cfg(model="mystery/model", base_url=LOCAL), st()), "hosted")


def test_a_free_tier_model_is_refused(st):
    refused(check_proposer(cfg(model="openrouter/free", base_url=LOCAL), st()), "free-tier")
    refused(check_proposer(cfg(model="ollama/x:free", base_url=LOCAL), st()), "free-tier")


def test_a_provider_less_opencode_model_is_refused(st):
    refused(check_proposer(cfg(model="qwen3"), st()), "provider")


def test_the_policy_arguments_reach_the_egress_check(st):
    c = cfg(model="ollama/x", base_url="https://zdr.example")
    assert check_proposer(c, st(zdr="zdr.example"), zdr="strict").allowed
    assert not check_proposer(c, st(), zdr="strict").allowed
    # zdr=off still bars restricted content from an unlisted host: classification is never open by default
    assert not check_proposer(cfg(base_url="https://other.example"), st(), zdr="off").allowed


# ── credentials in the environment ────────────────────────────────────────────────

SECRET_ENVS = {
    "openrouter key": {"OPENROUTER_API_KEY": f"sk-{SENTINEL}"},
    "anthropic key": {"ANTHROPIC_API_KEY": SENTINEL},
    "anthropic token": {"ANTHROPIC_AUTH_TOKEN": SENTINEL},
    "aws": {"AWS_SECRET_ACCESS_KEY": SENTINEL, "AWS_ACCESS_KEY_ID": SENTINEL},
    "github": {"GITHUB_TOKEN": SENTINEL},
    "gitea": {"GITEA_ACCESS_TOKEN": SENTINEL},
    "google creds file": {"GOOGLE_APPLICATION_CREDENTIALS": "/home/u/key.json"},
    "generic key": {"MY_SERVICE_KEY": SENTINEL},
    "password": {"DB_PASSWORD": SENTINEL},
    "secret": {"CLIENT_SECRET": SENTINEL},
    "ssh agent": {"SSH_AUTH_SOCK": "/tmp/agent.sock"},
    "jev": {"JEV_OPENROUTER_API_KEY": SENTINEL},
    "lowercase": {"openai_api_key": SENTINEL},
    "cookie": {"SESSION_COOKIE": SENTINEL},
}


@pytest.mark.parametrize("extra", SECRET_ENVS.values(), ids=SECRET_ENVS.keys())
def test_inherited_credentials_are_refused_by_name_only(st, extra):
    d = check_proposer(cfg(env={**CLEAN_ENV, **extra}), st())
    refused(d, "credential")
    for name in extra:
        assert name in " ".join(d.reasons)
    assert SENTINEL not in dump(d)


def test_every_secret_name_is_listed_in_one_reason(st):
    d = check_proposer(cfg(env={**CLEAN_ENV, "A_TOKEN": "1", "B_SECRET": "2", "C_API_KEY": "3"}), st())
    reason = next(r for r in d.reasons if "credential" in r)
    assert all(n in reason for n in ("A_TOKEN", "B_SECRET", "C_API_KEY"))
    assert "PATH" not in reason


def test_explicitly_allowed_credentials_pass_and_only_those(st):
    env = {**CLEAN_ENV, "OPENROUTER_API_KEY": SENTINEL, "OTHER_TOKEN": SENTINEL}
    assert not check_proposer(cfg(env=env), st(), allowed_env=frozenset({"OPENROUTER_API_KEY"})).allowed
    d = check_proposer(cfg(env=env), st(), allowed_env=frozenset({"OPENROUTER_API_KEY", "OTHER_TOKEN"}))
    assert d.allowed and SENTINEL not in dump(d)
    assert set(d.view["env"]["allowed"]) == {"OPENROUTER_API_KEY", "OTHER_TOKEN"}


def test_non_secret_names_that_look_like_config_pass(st):
    env = {**CLEAN_ENV, "ANTHROPIC_MODEL": "m", "OPENAI_BASE_URL": LOCAL, "GOOGLE_CLOUD_PROJECT_ID": "x",
           "TOKENIZERS_PARALLELISM": "false", "KEYBOARD_LAYOUT": "us", "MONKEY": "1"}
    d = check_proposer(cfg(env=env), st())
    assert d.allowed, d.reasons


def test_an_unspecified_env_means_the_whole_process_environment(st):
    d = check_proposer(cfg(env=None), st(), environ={**CLEAN_ENV, "OPENROUTER_API_KEY": SENTINEL})
    refused(d, "OPENROUTER_API_KEY", "inherit")
    assert SENTINEL not in dump(d)
    assert check_proposer(cfg(env=None), st(), environ=dict(CLEAN_ENV)).allowed


def test_endpoint_variables_in_the_environment_must_pass_the_same_gate(st):
    for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "OLLAMA_HOST_ENDPOINT", "MY_API_BASE"):
        d = check_proposer(cfg(env={**CLEAN_ENV, name: f"https://evil.example/{SENTINEL}"}), st(),
                           allowed_env=frozenset({name}))
        refused(d, name)
        assert SENTINEL not in dump(d) and "evil.example" not in dump(d)
    assert check_proposer(cfg(env={**CLEAN_ENV, "OPENAI_BASE_URL": LOCAL}), st()).allowed


@pytest.mark.parametrize("name", ["HTTP_PROXY", "https_proxy", "ALL_PROXY"])
def test_a_proxy_that_is_not_loopback_is_refused(st, name):
    d = check_proposer(cfg(env={**CLEAN_ENV, name: f"http://u:{SENTINEL}@proxy.corp:3128"}), st())
    refused(d, name)
    assert SENTINEL not in dump(d)
    assert check_proposer(cfg(env={**CLEAN_ENV, name: "http://127.0.0.1:3128"}), st()).allowed


@pytest.mark.parametrize("value", [3, ["PATH"], {"PATH": 1}, {1: "x"}, "PATH=/usr/bin"])
def test_an_env_that_is_not_a_string_mapping_is_unparseable(st, value):
    d = check_proposer({"kind": "opencode", "model": "ollama/x", "base_url": LOCAL, "env": value}, st())
    refused(d, "unparseable")


def test_an_env_left_out_is_reported_as_the_process_environment(st):
    d = check_proposer({"kind": "opencode", "model": "ollama/x", "base_url": LOCAL}, st(), environ=dict(CLEAN_ENV))
    assert d.view["env"]["source"] == "process environment"


# ── the claude CLI ────────────────────────────────────────────────────────────────

CLAUDE = dict(kind="claude_cli", model="claude-x", base_url="https://zdr.example/v1")


def test_a_claude_cli_proposer_is_refused_without_the_named_flag(st):
    d = check_proposer(cfg(**CLAUDE), st(zdr="zdr.example"))
    refused(d, "Claude subscription", settings.ENV_ALLOW_CLAUDE_PROPOSER)


def test_the_flag_alone_does_not_make_it_pass(st):
    refused(check_proposer(cfg(**CLAUDE), st(claude="1")), "neither")  # endpoint still not approved
    refused(check_proposer(cfg(**{**CLAUDE, "env": {**CLEAN_ENV, "CLAUDE_CODE_USE_VERTEX": "1"}}),
                           st(zdr="zdr.example", claude="1")), "Vertex")


def test_the_flag_plus_an_approved_endpoint_and_clean_env_passes(st):
    d = check_proposer(cfg(**CLAUDE), st(zdr="zdr.example", claude="true"))
    assert d.allowed
    assert any("Claude" in w for w in d.warnings)  # still says it can spend subscription quota


def test_claude_is_not_treated_as_local_even_on_loopback(st):
    refused(check_proposer(cfg(kind="claude_cli", model="claude-x", base_url=LOCAL), st(claude="1")), "hosted")


@pytest.mark.parametrize("kind", ["claude", "Claude_CLI", " claude_cli"])
def test_claude_aliases_are_still_claude(st, kind):
    refused(check_proposer(cfg(**{**CLAUDE, "kind": kind}), st(zdr="zdr.example")), "Claude subscription")


def test_a_claude_executable_is_claude_whatever_the_kind_says(st):
    d = check_proposer(cfg(command=("/usr/local/bin/claude", "-p"), base_url=LOCAL), st())
    refused(d, "Claude subscription")
    d = check_proposer(cfg(command=("claude",), base_url=LOCAL), st())
    refused(d, "Claude subscription")


def test_a_command_that_disagrees_with_its_kind_is_refused(st):
    d = check_proposer(cfg(**CLAUDE, command=("bun", "run", "src/index.ts")), st(zdr="zdr.example", claude="1"))
    refused(d, "does not match")


# ── command line and working directory ────────────────────────────────────────────

def test_the_view_shows_the_executable_and_flag_names_never_arguments(st):
    d = check_proposer(cfg(command=ARGV), st())
    assert d.allowed
    assert d.view["command"] == {"executable": "bun", "flags": ["--cwd", "--dir", "--format", "--model"]}
    assert SENTINEL not in dump(d)


@pytest.mark.parametrize("argv", [("bun", "--api-key", SENTINEL), ("bun", f"--token={SENTINEL}"),
                                  ("bun", "--password", SENTINEL)])
def test_a_credential_flag_on_the_command_line_is_refused_without_its_value(st, argv):
    d = check_proposer(cfg(command=argv), st())
    refused(d, "command line")
    assert SENTINEL not in dump(d)


@pytest.mark.parametrize("cwd", ["work", "./work", "../x", "", "/", "~", "/tmp/a\x00b", "/tmp/a\nb"])
def test_a_bad_working_directory_is_refused(st, cwd):
    assert not check_proposer(cfg(cwd=cwd), st()).allowed


def test_home_and_its_ancestors_are_not_a_working_directory(st, monkeypatch, tmp_path):
    home = tmp_path / "u" / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for bad in (home, home.parent, tmp_path):
        refused(check_proposer(cfg(cwd=str(bad)), st()), "working directory")
    assert check_proposer(cfg(cwd=str(home / "work")), st()).allowed


def test_no_working_directory_means_the_fresh_temp_dir_upstream_makes(st):
    d = check_proposer(cfg(cwd=None), st())
    assert d.allowed and d.view["cwd"] == "fresh temporary directory (upstream default)"


# ── unparseable configuration ─────────────────────────────────────────────────────

BAD_RAW = {
    "none": None,
    "int": 3,
    "string": "opencode",
    "empty mapping": {},
    "kind int": {"kind": 5, "model": "ollama/x"},
    "unknown kind": {"kind": "gemini_cli", "model": "x", "base_url": LOCAL},
    "kind missing": {"model": "ollama/x", "base_url": LOCAL},
    "model missing": {"kind": "opencode", "base_url": LOCAL},
    "model empty": {"kind": "opencode", "model": "  ", "base_url": LOCAL},
    "model int": {"kind": "opencode", "model": 7, "base_url": LOCAL},
    "command string": {"kind": "opencode", "model": "ollama/x", "base_url": LOCAL, "command": "bun run"},
    "command items": {"kind": "opencode", "model": "ollama/x", "base_url": LOCAL, "command": ["bun", 3]},
    "empty command": {"kind": "opencode", "model": "ollama/x", "base_url": LOCAL, "command": []},
    "cwd int": {"kind": "opencode", "model": "ollama/x", "base_url": LOCAL, "cwd": 3},
    "unknown key": {"kind": "opencode", "model": "ollama/x", "base_url": LOCAL, "api_key": SENTINEL},
}


@pytest.mark.parametrize("raw", BAD_RAW.values(), ids=BAD_RAW.keys())
def test_unparseable_configuration_is_a_refusal_not_an_exception(st, raw):
    d = check_proposer(raw, st())
    assert d.allowed is False
    assert any("unparseable" in r for r in d.reasons)
    assert SENTINEL not in dump(d)


def test_parse_config_raises_where_check_proposer_refuses():
    with pytest.raises(upstream_egress.ProposerConfigError):
        parse_config({"kind": "nope", "model": "x"})
    assert parse_config({"kind": "opencode", "model": "ollama/x", "base_url": LOCAL}).kind == "opencode"


def test_a_mapping_is_accepted_like_a_config(st):
    d = check_proposer({"kind": "opencode", "model": "ollama/q", "base_url": LOCAL, "env": CLEAN_ENV,
                        "cwd": "/tmp/work", "command": list(ARGV)}, st())
    assert d.allowed


def test_a_surprise_exception_inside_the_gate_is_a_refusal(st, monkeypatch):
    def boom(**_):
        raise RuntimeError(f"boom {SENTINEL}")
    monkeypatch.setattr(upstream_egress.egress, "check_endpoint", boom)
    d = check_proposer(cfg(), st())
    assert d.allowed is False and any("internal error" in r for r in d.reasons)
    assert SENTINEL not in dump(d)


# ── redaction ─────────────────────────────────────────────────────────────────────

def test_no_secret_value_appears_anywhere_in_any_decision(st):
    hostile = cfg(
        base_url=f"http://user:{SENTINEL}@evil.example/v1?token={SENTINEL}#{SENTINEL}",
        env={**CLEAN_ENV, "OPENROUTER_API_KEY": SENTINEL, "HTTPS_PROXY": f"http://u:{SENTINEL}@p.example",
             "OPENAI_BASE_URL": f"https://evil.example/{SENTINEL}", "ANTHROPIC_AUTH_TOKEN": SENTINEL},
        command=("claude", "--api-key", SENTINEL, f"--token={SENTINEL}", f"prompt {SENTINEL}"),
        cwd="/tmp/work",
        project="p",
    )
    d = check_proposer(hostile, st())
    assert d.allowed is False
    assert SENTINEL not in dump(d)
    assert SENTINEL not in json.dumps(d.to_dict()["view"])


def test_the_view_lists_secret_like_env_by_name_and_counts_the_rest(st):
    d = check_proposer(cfg(env={**CLEAN_ENV, "FOO_TOKEN": SENTINEL}), st())
    assert d.view["env"]["secret_like"] == ["FOO_TOKEN"]
    assert d.view["env"]["other"] == len(CLEAN_ENV)
    assert "PATH" not in json.dumps(d.view["env"])


def test_the_decision_is_immutable(st):
    d = check_proposer(cfg(), st())
    with pytest.raises(AttributeError):
        d.allowed = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        d.view["kind"] = "x"  # type: ignore[index]


def test_a_refusal_collects_every_reason_not_just_the_first(st):
    d = check_proposer(cfg(kind="claude_cli", model="claude-x", base_url="https://openrouter.ai/api",
                           env={**CLEAN_ENV, "CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_API_KEY": SENTINEL},
                           cwd="relative"), st())
    text = " | ".join(d.reasons)
    for needle in ("Claude subscription", "Vertex", "openrouter.ai", "credential", "working directory"):
        assert needle in text, needle


# ── settings flag ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "want"), [(None, False), ("", False), ("0", False), ("false", False),
                                           ("1", True), ("true", True), ("TRUE", True)])
def test_the_claude_flag_parses(tmp_path, raw, want):
    env = {settings.ENV_EXPERIENCE_ROOT: str(tmp_path / "e")}
    if raw is not None:
        env[settings.ENV_ALLOW_CLAUDE_PROPOSER] = raw
    assert settings.from_env(env).allow_claude_cli_proposer is want


@pytest.mark.parametrize("raw", ["yes", "on", "2", "enabled"])
def test_an_unclear_claude_flag_is_an_error_not_a_default(tmp_path, raw):
    with pytest.raises(settings.SettingsError):
        settings.from_env({settings.ENV_EXPERIENCE_ROOT: str(tmp_path / "e"), settings.ENV_ALLOW_CLAUDE_PROPOSER: raw})


def test_the_default_settings_do_not_allow_claude(tmp_path):
    assert settings.from_env({settings.ENV_EXPERIENCE_ROOT: str(tmp_path / "e")}).allow_claude_cli_proposer is False


# ── binding to the real upstream (dev group) ──────────────────────────────────────

def test_our_kinds_are_upstreams_channels():
    from harness2.core.spec import CHANNELS
    assert set(upstream_egress.KINDS) == set(CHANNELS)


def test_a_real_upstream_proposer_is_read_without_starting_a_session(st):
    from harness2.core.proposer import Proposer
    from harness2.core.spec import ChannelSpec

    p = Proposer(ChannelSpec("opencode", "ollama/qwen3"), project="a-project", region="global")
    c = from_upstream(p, base_url=LOCAL, cwd="/tmp/work")
    assert (c.kind, c.model, c.project) == ("opencode", "ollama/qwen3", "a-project")
    d = check_proposer(p, st(), environ={})
    # upstream's own env() adds GOOGLE_VERTEX_PROJECT whatever the model: refused until the caller scrubs it
    refused(d, "Vertex")
    assert d.view["endpoint"] is None


def test_a_real_claude_proposer_is_refused_for_quota_and_for_vertex(st):
    from harness2.core.proposer import Proposer
    from harness2.core.spec import ChannelSpec

    p = Proposer(ChannelSpec("claude_cli", "claude-x"), project="p", region="global")
    d = check_proposer(from_upstream(p, base_url=LOCAL), st())
    refused(d, "Claude subscription", "Vertex")


def test_from_upstream_takes_the_env_the_caller_will_really_pass(st):
    from harness2.core.proposer import Proposer
    from harness2.core.spec import ChannelSpec

    p = Proposer(ChannelSpec("opencode", "ollama/qwen3"), project="", region="global")
    c = from_upstream(p, base_url=LOCAL, cwd="/tmp/work", env=dict(CLEAN_ENV))
    assert check_proposer(c, st()).allowed


def test_from_upstream_refuses_what_it_cannot_read():
    with pytest.raises(upstream_egress.ProposerConfigError):
        from_upstream(object())
