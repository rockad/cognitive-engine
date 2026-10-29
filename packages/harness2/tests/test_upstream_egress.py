"""Seam 2: the egress gate on an upstream Proposer configuration, BEFORE any session exists.

Nothing here starts a process, opens a socket or calls a model: `check_proposer` only reads
the configuration it is given and the channel profile. Every secret in these fixtures is a
unique sentinel so the redaction tests can search the whole decision for it.
"""
from __future__ import annotations

import json

import pytest
from h2_profiles import CAPS, LOCAL, PROFILE, WORK, make_profile

from cognitive_harness2 import channels, upstream_egress
from cognitive_harness2.upstream_egress import (
    Decision,
    ProposerConfig,
    check_proposer,
    from_upstream,
    parse_config,
)

SENTINEL = "SENTINEL-do-not-leak-7f3a"
CLEAN_ENV = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": "/tmp/h"}
ARGV = ("bun", "run", "--cwd", "/x", "src/index.ts", "run", f"PROMPT {SENTINEL}", "--dir", WORK,
        "--model", "ollama/qwen3:30b", "--format", "json")


def cfg(**over) -> ProposerConfig:
    base = dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=dict(CLEAN_ENV), cwd=WORK)
    base.update(over)
    return ProposerConfig(**base)


def chk(config, channel="local", *, profile=PROFILE, **kw) -> Decision:
    return check_proposer(config, profile=profile, channel=channel, **kw)


def dump(d: Decision) -> str:
    return json.dumps(d.to_dict(), default=str) + repr(d) + " ".join(d.reasons) + " ".join(d.warnings)


def refused(d: Decision, *needles: str) -> None:
    assert d.allowed is False and d.reasons
    text = " | ".join(d.reasons)
    for n in needles:
        assert n in text, f"{n!r} not in {text!r}"


# ── the allowed case ──────────────────────────────────────────────────────────────

def test_a_local_model_on_its_declared_channel_is_allowed():
    d = chk(cfg())
    assert d.allowed is True
    assert any("channel 'local' allowed" in r and "retention=local" in r for r in d.reasons)
    assert d.view["endpoint"] == "http://127.0.0.1:11434"
    assert d.view["kind"] == "opencode" and d.view["provider"] == "ollama"
    assert d.view["channel"] == "local" and d.view["retention"] == "local"
    assert d.view["classification"] == "restricted" and d.view["zdr"] == "strict"
    assert d.view["caps"] == CAPS


@pytest.mark.parametrize("url", ["http://localhost:1234/v1", "http://[::1]:8080", "https://LOCALHOST/v1"])
def test_every_loopback_spelling_is_allowed_when_the_channel_declares_it(url):
    p = make_profile(local=dict(endpoint=url))
    assert chk(cfg(base_url=url), profile=p).allowed


def test_an_allowed_decision_still_warns_that_the_session_runs_on_the_host():
    d = chk(cfg())
    assert any("host" in w for w in d.warnings)
    assert any("HOME" in w for w in d.warnings)


def test_a_zdr_attested_remote_channel_is_allowed_under_strict():
    d = chk(cfg(model="ollama/x", base_url="https://zdr.example/v1"), "zdr-host")
    assert d.allowed and "zdr-attested" in " ".join(d.reasons)


def test_a_hosted_provider_on_a_zdr_attested_host_is_allowed():
    c = cfg(model="openrouter/anthropic/claude-x", base_url="https://openrouter.ai/api/v1")
    assert chk(c, "openrouter", classification="gated").allowed


def test_a_default_port_matches_the_declared_one():
    p = make_profile(**{"zdr-host": dict(endpoint="https://zdr.example:443/v1")})
    assert chk(cfg(model="ollama/x", base_url="https://zdr.example"), "zdr-host", profile=p).allowed


# ── the declaration: no channel, no session ───────────────────────────────────────

def test_an_undeclared_channel_is_denied_and_the_message_points_at_the_profile():
    d = chk(cfg(), "nope")
    refused(d, "rule undeclared-channel", "'nope'", "/p/channels.toml", "declared: ")
    assert "[channels.<name>]" in " ".join(d.reasons)


@pytest.mark.parametrize("name", [None, "", 5, ["local"], "LOCAL", "local ", "loc\nal"])
def test_a_missing_or_misspelt_channel_name_is_denied(name):
    refused(chk(cfg(), name), "rule undeclared-channel")


def test_no_profile_at_all_denies_everything_and_says_how_to_create_one():
    empty = channels.ChannelProfile(source="/home/u/.config/cognitive-engine/harness2-channels.toml", declared=False)
    d = chk(cfg(), "local", profile=empty)
    refused(d, "no channel profile exists at /home/u/.config/cognitive-engine/harness2-channels.toml", "[channels.<name>]")


@pytest.mark.parametrize("profile", [None, {}, "profile", PROFILE.channels])
def test_something_that_is_not_a_profile_is_denied(profile):
    refused(chk(cfg(), profile=profile), "rule undeclared-channel")


def test_a_declared_channel_does_not_unlock_the_others():
    # the claude channel is declared, but the config asks for the local one's endpoint and model
    refused(chk(cfg(), "claude", zdr="off", classification="open"), "kind-mismatch")


MISMATCH = {
    "kind": (dict(kind="claude_cli", model="claude-x", base_url=LOCAL), "local", "kind-mismatch"),
    "provider": (dict(model="mistral/qwen3:30b"), "local", "provider-mismatch"),
    "endpoint host": (dict(base_url="http://127.0.0.2:11434/v1"), "local", "endpoint-mismatch"),
    "endpoint port": (dict(base_url="http://127.0.0.1:11435/v1"), "local", "endpoint-mismatch"),
    "endpoint scheme default port": (dict(base_url="https://127.0.0.1:11434/v1"), "local", None),   # same host:port
    "endpoint other host": (dict(base_url="https://elsewhere.example/v1"), "local", "endpoint-mismatch"),
    "model": (dict(model="ollama/qwen3:8b"), "local", "model-mismatch"),
    "model case": (dict(model="ollama/QWEN3:30b"), "local", "model-mismatch"),
}


@pytest.mark.parametrize("name", list(MISMATCH))
def test_the_configuration_must_match_the_declared_channel_exactly(name):
    over, channel, rule = MISMATCH[name]
    d = chk(cfg(**over), channel)
    if rule is None:
        assert d.allowed
    else:
        refused(d, f"channel '{channel}'", f"rule {rule}")


def test_a_profile_whose_endpoint_differs_from_the_proposer_config_is_denied():
    p = make_profile(local=dict(endpoint="http://127.0.0.1:9999/v1"))
    d = chk(cfg(), profile=p)
    refused(d, "rule endpoint-mismatch", "127.0.0.1:11434", "127.0.0.1:9999")


def test_a_hosted_provider_behind_a_loopback_address_is_refused():
    p = make_profile(local=dict(provider="anthropic", model="anthropic/claude-x"))
    refused(chk(cfg(model="anthropic/claude-x"), profile=p), "hosted", "rule local-provider")
    p = make_profile(local=dict(provider="mystery", model="mystery/model"))
    refused(chk(cfg(model="mystery/model"), profile=p), "hosted")


def test_a_zdr_attested_loopback_gateway_to_a_hosted_model_is_allowed_with_a_warning():
    p = make_profile(local=dict(provider="anthropic", model="anthropic/claude-x", retention="zdr-attested"))
    d = chk(cfg(model="anthropic/claude-x"), profile=p)
    assert d.allowed
    assert any("loopback gateway" in w for w in d.warnings)


# ── retention, zdr mode, classification: the policy through the seam ──────────────

CLS = [None, "open", "gated", "restricted", "unstated"]


def _expected(retention, zdr, cls, ceiling):
    eff = channels.effective_classification(cls)
    if zdr == "strict" and retention == "unknown":
        return False
    if retention == "unknown" and eff != "open":
        return False
    return ("open", "gated", "restricted").index(eff) <= ("open", "gated", "restricted").index(ceiling)


@pytest.mark.parametrize("zdr", ["strict", "off"])
@pytest.mark.parametrize("cls", CLS)
@pytest.mark.parametrize("name", ["local", "zdr-host", "openrouter", "claude"])
def test_the_policy_matrix_through_check_proposer(name, cls, zdr):
    body = PROFILE.channels[name]
    if name == "claude":
        c = cfg(kind="claude_cli", model="claude-x", base_url="https://api.anthropic.com")
    elif name == "openrouter":
        c = cfg(model=body.model, base_url="https://openrouter.ai/api/v1")
    elif name == "zdr-host":
        c = cfg(model=body.model, base_url="https://zdr.example/v1")
    else:
        c = cfg()
    d = chk(c, name, zdr=zdr, classification=cls)
    assert d.allowed is _expected(body.retention, zdr, cls, body.max_classification), d.reasons
    if not d.allowed:
        assert all("rule " in r for r in d.reasons)


def test_subscription_content_rules():
    c = cfg(kind="claude_cli", model="claude-x", base_url="https://api.anthropic.com")
    # strict: unknown retention is refused outright, whatever the content
    refused(chk(c, "claude", zdr="strict", classification="open"), "rule retention-vs-zdr")
    # off: open content only
    assert chk(c, "claude", zdr="off", classification="open").allowed
    refused(chk(c, "claude", zdr="off", classification="gated"), "rule unknown-retention-open-only")
    refused(chk(c, "claude", zdr="off", classification=None), "rule unknown-retention-open-only")
    assert any("Claude subscription" in w for w in chk(c, "claude", zdr="off", classification="open").warnings)


def test_a_missing_classification_is_restricted():
    d = chk(cfg(model="openrouter/anthropic/claude-x", base_url="https://openrouter.ai/api/v1"), "openrouter")
    refused(d, "rule classification-ceiling", "restricted")
    assert d.view["classification"] == "restricted"


@pytest.mark.parametrize("zdr", ["STRICT", "", None, "lax", 1])
def test_an_unknown_zdr_mode_is_denied(zdr):
    refused(chk(cfg(), zdr=zdr, classification="open"), "rule zdr-mode")


def test_a_free_tier_model_is_refused():
    c = cfg(model="openrouter/free", base_url="https://openrouter.ai/api/v1")
    refused(chk(c, "free", classification="open"), "rule free-tier")
    p = make_profile(free=dict(model="ollama/x:free", provider="ollama"))
    refused(chk(cfg(model="ollama/x:free", base_url="https://openrouter.ai/api/v1"), "free", profile=p,
                classification="gated", zdr="off"), "rule free-tier")


# ── refusals, one per reason ──────────────────────────────────────────────────────

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
def test_vertex_is_refused(over):
    refused(chk(cfg(**over), zdr="off", classification="open"), "Vertex")


def test_a_vertex_environment_is_refused_even_on_an_otherwise_allowed_channel():
    refused(chk(cfg(env={**CLEAN_ENV, "GOOGLE_VERTEX_PROJECT": "p"})), "rule vertex")


def test_a_non_loopback_undeclared_endpoint_is_refused():
    refused(chk(cfg(base_url="https://api.openai.com/v1", model="ollama/x")), "api.openai.com")


@pytest.mark.parametrize("url", ["https://openrouter.ai/api/v1", "http://192.168.1.5:11434", "http://10.0.0.2",
                                 "http://127.0.0.1.nip.io", "http://localhost.evil.example", "http://0.0.0.0:80",
                                 "http://127.0.0.2:1"])
def test_lookalike_and_lan_hosts_are_not_the_declared_endpoint(url):
    assert not chk(cfg(base_url=url)).allowed


def test_an_unknown_endpoint_is_refused():
    d = chk(cfg(base_url=None))
    refused(d, "cannot be determined")
    assert d.view["endpoint"] is None


@pytest.mark.parametrize("url", ["", "not a url", "http://", "://x", "/v1", "localhost:11434", "ftp://127.0.0.1/x",
                                 "file:///etc/passwd", "http:///v1", f"http://user:{SENTINEL}@127.0.0.1/v1",
                                 f"http://127.0.0.1/v1?key={SENTINEL}", 5, ["http://127.0.0.1"]])
def test_an_unusable_base_url_is_refused_without_echoing_it(url):
    d = chk(cfg(base_url=url))
    assert d.allowed is False
    assert SENTINEL not in dump(d)


def test_credentials_in_the_url_are_refused_and_never_shown():
    d = chk(cfg(base_url=f"http://user:{SENTINEL}@127.0.0.1:11434/v1"))
    refused(d, "credentials")
    assert SENTINEL not in dump(d) and "user" not in json.dumps(d.to_dict()["view"])


def test_a_provider_less_opencode_model_is_refused():
    refused(chk(cfg(model="qwen3")), "provider")


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
def test_inherited_credentials_are_refused_by_name_only(extra):
    d = chk(cfg(env={**CLEAN_ENV, **extra}))
    refused(d, "credential")
    for name in extra:
        assert name in " ".join(d.reasons)
    assert SENTINEL not in dump(d)


def test_every_secret_name_is_listed_in_one_reason():
    d = chk(cfg(env={**CLEAN_ENV, "A_TOKEN": "1", "B_SECRET": "2", "C_API_KEY": "3"}))
    reason = next(r for r in d.reasons if "credential" in r)
    assert all(n in reason for n in ("A_TOKEN", "B_SECRET", "C_API_KEY"))
    assert "PATH" not in reason


def test_explicitly_allowed_credentials_pass_and_only_those():
    env = {**CLEAN_ENV, "OPENROUTER_API_KEY": SENTINEL, "OTHER_TOKEN": SENTINEL}
    assert not chk(cfg(env=env), allowed_env=frozenset({"OPENROUTER_API_KEY"})).allowed
    d = chk(cfg(env=env), allowed_env=frozenset({"OPENROUTER_API_KEY", "OTHER_TOKEN"}))
    assert d.allowed and SENTINEL not in dump(d)
    assert set(d.view["env"]["allowed"]) == {"OPENROUTER_API_KEY", "OTHER_TOKEN"}


def test_non_secret_names_that_look_like_config_pass():
    env = {**CLEAN_ENV, "ANTHROPIC_MODEL": "m", "OPENAI_BASE_URL": LOCAL, "GOOGLE_CLOUD_PROJECT_ID": "x",
           "TOKENIZERS_PARALLELISM": "false", "KEYBOARD_LAYOUT": "us", "MONKEY": "1"}
    d = chk(cfg(env=env))
    assert d.allowed, d.reasons


def test_an_unspecified_env_means_the_whole_process_environment():
    d = chk(cfg(env=None), environ={**CLEAN_ENV, "OPENROUTER_API_KEY": SENTINEL})
    refused(d, "OPENROUTER_API_KEY", "inherit")
    assert SENTINEL not in dump(d)
    assert chk(cfg(env=None), environ=dict(CLEAN_ENV)).allowed


def test_endpoint_variables_in_the_environment_must_be_a_declared_endpoint():
    for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "OLLAMA_HOST_ENDPOINT", "MY_API_BASE"):
        d = chk(cfg(env={**CLEAN_ENV, name: f"https://evil.example/{SENTINEL}"}), allowed_env=frozenset({name}))
        refused(d, name)
        assert SENTINEL not in dump(d) and "evil.example" not in dump(d)
    assert chk(cfg(env={**CLEAN_ENV, "OPENAI_BASE_URL": LOCAL})).allowed


@pytest.mark.parametrize("name", ["HTTP_PROXY", "https_proxy", "ALL_PROXY"])
def test_a_proxy_that_is_not_a_declared_endpoint_is_refused(name):
    d = chk(cfg(env={**CLEAN_ENV, name: f"http://u:{SENTINEL}@proxy.corp:3128"}))
    refused(d, name)
    assert SENTINEL not in dump(d)


@pytest.mark.parametrize("value", [3, ["PATH"], {"PATH": 1}, {1: "x"}, "PATH=/usr/bin"])
def test_an_env_that_is_not_a_string_mapping_is_unparseable(value):
    d = chk({"kind": "opencode", "model": "ollama/x", "base_url": LOCAL, "env": value})
    refused(d, "unparseable")


def test_an_env_left_out_is_reported_as_the_process_environment():
    d = chk({"kind": "opencode", "model": "ollama/qwen3:30b", "base_url": LOCAL}, environ=dict(CLEAN_ENV))
    assert d.view["env"]["source"] == "process environment"


# ── the claude CLI ────────────────────────────────────────────────────────────────

CLAUDE = dict(kind="claude_cli", model="claude-x", base_url="https://api.anthropic.com")


def test_a_claude_cli_proposer_needs_its_declared_channel():
    refused(chk(cfg(**CLAUDE), "local", zdr="off", classification="open"), "kind-mismatch")
    assert chk(cfg(**CLAUDE), "claude", zdr="off", classification="open").allowed


def test_claude_is_not_treated_as_local_even_on_loopback():
    p = make_profile(cl=dict(kind="claude_cli", provider="anthropic", endpoints=[LOCAL], model="claude-x",
                             retention="local", max_classification="restricted"))
    refused(chk(cfg(kind="claude_cli", model="claude-x", base_url=LOCAL), "cl", profile=p), "hosted")


@pytest.mark.parametrize("kind", ["claude", "Claude_CLI", " claude_cli"])
def test_claude_aliases_are_still_claude(kind):
    assert chk(cfg(**{**CLAUDE, "kind": kind}), "claude", zdr="off", classification="open").allowed
    refused(chk(cfg(**{**CLAUDE, "kind": kind}), "local"), "kind-mismatch")


def test_a_claude_executable_is_claude_whatever_the_kind_says():
    d = chk(cfg(command=("/usr/local/bin/claude", "-p"), base_url=LOCAL))
    refused(d, "does not match")
    assert any("Claude subscription" in w for w in d.warnings)
    refused(chk(cfg(command=("claude",), base_url=LOCAL)), "does not match")


def test_a_command_that_disagrees_with_its_kind_is_refused():
    d = chk(cfg(**CLAUDE, command=("bun", "run", "src/index.ts")), "claude", zdr="off", classification="open")
    refused(d, "does not match")


# ── command line and working directory ────────────────────────────────────────────

def test_the_view_shows_the_executable_and_flag_names_never_arguments():
    d = chk(cfg(command=ARGV))
    assert d.allowed
    assert d.view["command"] == {"executable": "bun", "flags": ["--cwd", "--dir", "--format", "--model"]}
    assert SENTINEL not in dump(d)


@pytest.mark.parametrize("argv", [("bun", "--api-key", SENTINEL), ("bun", f"--token={SENTINEL}"),
                                  ("bun", "--password", SENTINEL)])
def test_a_credential_flag_on_the_command_line_is_refused_without_its_value(argv):
    d = chk(cfg(command=argv))
    refused(d, "command line")
    assert SENTINEL not in dump(d)


@pytest.mark.parametrize("cwd", ["work", "./work", "../x", "", "/", "~", "/tmp/a\x00b", "/tmp/a\nb"])
def test_a_bad_working_directory_is_refused(cwd):
    assert not chk(cfg(cwd=cwd)).allowed


def test_home_and_its_ancestors_are_not_a_working_directory(monkeypatch, tmp_path):
    home = tmp_path / "u" / "home"
    (home / "ws" / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    p = make_profile(roots=[str(home / "ws")])
    for bad in (home, home.parent, tmp_path):
        refused(chk(cfg(cwd=str(bad)), profile=p), "working directory")
    assert chk(cfg(cwd=str(home / "ws" / "work")), profile=p).allowed


def test_no_working_directory_means_the_fresh_temp_dir_upstream_makes():
    d = chk(cfg(cwd=None))
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
def test_unparseable_configuration_is_a_refusal_not_an_exception(raw):
    d = chk(raw)
    assert d.allowed is False
    assert any("unparseable" in r for r in d.reasons)
    assert SENTINEL not in dump(d)


def test_parse_config_raises_where_check_proposer_refuses():
    with pytest.raises(upstream_egress.ProposerConfigError):
        parse_config({"kind": "nope", "model": "x"})
    assert parse_config({"kind": "opencode", "model": "ollama/x", "base_url": LOCAL}).kind == "opencode"


def test_a_mapping_is_accepted_like_a_config():
    d = chk({"kind": "opencode", "model": "ollama/qwen3:30b", "base_url": LOCAL, "env": CLEAN_ENV,
             "cwd": WORK, "command": list(ARGV)})
    assert d.allowed


def test_a_surprise_exception_inside_the_gate_is_a_refusal(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"boom {SENTINEL}")
    monkeypatch.setattr(upstream_egress.channels, "decide", boom)
    d = chk(cfg())
    assert d.allowed is False and any("internal error" in r for r in d.reasons)
    assert SENTINEL not in dump(d)


# ── redaction ─────────────────────────────────────────────────────────────────────

def test_no_secret_value_appears_anywhere_in_any_decision():
    hostile = cfg(
        base_url=f"http://user:{SENTINEL}@evil.example/v1?token={SENTINEL}#{SENTINEL}",
        env={**CLEAN_ENV, "OPENROUTER_API_KEY": SENTINEL, "HTTPS_PROXY": f"http://u:{SENTINEL}@p.example",
             "OPENAI_BASE_URL": f"https://evil.example/{SENTINEL}", "ANTHROPIC_AUTH_TOKEN": SENTINEL},
        command=("claude", "--api-key", SENTINEL, f"--token={SENTINEL}", f"prompt {SENTINEL}"),
        cwd=WORK,
        project="p",
    )
    d = chk(hostile)
    assert d.allowed is False
    assert SENTINEL not in dump(d)
    assert SENTINEL not in json.dumps(d.to_dict()["view"])


def test_the_view_lists_secret_like_env_by_name_and_counts_the_rest():
    d = chk(cfg(env={**CLEAN_ENV, "FOO_TOKEN": SENTINEL}))
    assert d.view["env"]["secret_like"] == ["FOO_TOKEN"]
    assert d.view["env"]["other"] == len(CLEAN_ENV)
    assert "PATH" not in json.dumps(d.view["env"])


def test_the_decision_is_immutable():
    d = chk(cfg())
    with pytest.raises(AttributeError):
        d.allowed = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        d.view["kind"] = "x"  # type: ignore[index]


def test_a_refusal_collects_every_reason_not_just_the_first():
    d = chk(cfg(kind="claude_cli", model="claude-x", base_url="https://openrouter.ai/api",
                env={**CLEAN_ENV, "CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_API_KEY": SENTINEL},
                cwd="relative"), "claude", zdr="off", classification="gated")
    text = " | ".join(d.reasons)
    for needle in ("rule unknown-retention-open-only", "Vertex", "openrouter.ai", "credential", "working directory",
                   "rule endpoint-mismatch"):
        assert needle in text, needle


def test_every_reason_names_the_channel_and_the_rule():
    d = chk(cfg(base_url="http://127.0.0.1:1/v1", model="ollama/other"))
    for r in d.reasons:
        assert "channel 'local'" in r and "rule " in r, r


# ── binding to the real upstream (dev group) ──────────────────────────────────────

def test_our_kinds_are_upstreams_channels():
    from harness2.core.spec import CHANNELS
    assert set(upstream_egress.KINDS) == set(CHANNELS)


def test_a_real_upstream_proposer_is_read_without_starting_a_session():
    from harness2.core.proposer import Proposer
    from harness2.core.spec import ChannelSpec

    p = Proposer(ChannelSpec("opencode", "ollama/qwen3:30b"), project="a-project", region="global")
    c = from_upstream(p, base_url=LOCAL, cwd=WORK)
    assert (c.kind, c.model, c.project) == ("opencode", "ollama/qwen3:30b", "a-project")
    d = chk(p, environ={})
    # upstream's own env() adds GOOGLE_VERTEX_PROJECT whatever the model: refused until the caller scrubs it
    refused(d, "Vertex")
    assert d.view["endpoint"] is None


def test_a_real_claude_proposer_is_refused_for_vertex():
    from harness2.core.proposer import Proposer
    from harness2.core.spec import ChannelSpec

    p = Proposer(ChannelSpec("claude_cli", "claude-x"), project="p", region="global")
    d = chk(from_upstream(p, base_url="https://api.anthropic.com"), "claude", zdr="off", classification="open")
    refused(d, "Vertex")


def test_from_upstream_takes_the_env_the_caller_will_really_pass():
    from harness2.core.proposer import Proposer
    from harness2.core.spec import ChannelSpec

    p = Proposer(ChannelSpec("opencode", "ollama/qwen3:30b"), project="", region="global")
    c = from_upstream(p, base_url=LOCAL, cwd=WORK, env=dict(CLEAN_ENV))
    assert chk(c).allowed


def test_from_upstream_refuses_what_it_cannot_read():
    with pytest.raises(upstream_egress.ProposerConfigError):
        from_upstream(object())
