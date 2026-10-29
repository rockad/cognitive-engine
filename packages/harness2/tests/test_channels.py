"""The channel profile: schema, loud validation, the pinned reader, and the policy matrix.

Nothing here touches the network. Files are written only under pytest's tmp_path.
"""
from __future__ import annotations

import copy
import math
import os
import textwrap
from types import MappingProxyType

import pytest

from cognitive_harness2 import channels, settings
from cognitive_harness2.channels import ChannelProfileError, load_profile, validate

GOOD_TOML = textwrap.dedent('''
    workspace_roots = ["/srv/ws"]

    [channels.local-ollama]
    kind = "opencode"
    provider = "ollama"
    endpoint = "http://127.0.0.1:11434/v1"
    model = "ollama/qwen3:30b"
    retention = "local"
    max_classification = "restricted"
    approval = "per-sweep"
    [channels.local-ollama.caps]
    sessions = 30
    wall_s = 3600
    cost_usd = 1.0

    [channels.openrouter]
    kind = "opencode"
    provider = "openrouter"
    endpoint = "https://openrouter.ai/api/v1"
    model = "openrouter/anthropic/claude-sonnet-4"
    retention = "zdr-attested"
    max_classification = "gated"
    approval = "per-sweep"
    [channels.openrouter.caps]
    sessions = 20
    wall_s = 1800
    cost_usd = 5.0

    [channels.claude-harness]
    kind = "claude_cli"
    provider = "anthropic"
    endpoints = ["https://api.anthropic.com"]
    model = "claude-sonnet-5-5"
    retention = "unknown"
    max_classification = "open"
    approval = "per-sweep"
    [channels.claude-harness.caps]
    sessions = 10
    wall_s = 1200
    cost_usd = 2.5

    [channels.antigravity]
    kind = "agy_cli"
    provider = "google"
    endpoints = ["https://generativelanguage.googleapis.com", "https://cloudcode-pa.googleapis.com:443"]
    model = "gemini-3-pro"
    retention = "unknown"
    max_classification = "open"
    approval = "per-sweep"
    [channels.antigravity.caps]
    sessions = 10
    wall_s = 1200
    cost_usd = 2.5
''')


def good_raw() -> dict:
    import tomllib
    return tomllib.loads(GOOD_TOML)


def problems(raw) -> str:
    with pytest.raises(ChannelProfileError) as ei:
        validate(raw, source="/p/channels.toml")
    assert ei.value.path == "/p/channels.toml"
    assert "/p/channels.toml" in str(ei.value)
    return " | ".join(ei.value.problems)


def mutated(path: str, value=..., *, delete: bool = False) -> dict:
    """good_raw() with the dotted `path` set to `value` or removed."""
    raw = good_raw()
    node = raw
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[k]
    if delete:
        del node[keys[-1]]
    else:
        node[keys[-1]] = value
    return raw


# ── the worked example is valid ───────────────────────────────────────────────────

def test_the_worked_example_validates():
    p = validate(good_raw(), source="x")
    assert set(p.channels) == {"local-ollama", "openrouter", "claude-harness", "antigravity"}
    assert p.workspace_roots == ("/srv/ws",)
    c = p.channels["local-ollama"]
    assert (c.kind, c.provider, c.model, c.retention, c.max_classification) == \
        ("opencode", "ollama", "ollama/qwen3:30b", "local", "restricted")
    assert c.endpoints == ("http://127.0.0.1:11434",)
    assert c.caps == channels.Caps(30, 3600.0, 1.0)
    assert p.channels["antigravity"].endpoints[1] == "https://cloudcode-pa.googleapis.com:443"
    assert p.channels["claude-harness"].endpoints == ("https://api.anthropic.com:443",)


def test_a_channel_and_a_profile_are_immutable():
    p = validate(good_raw(), source="x")
    with pytest.raises(AttributeError):
        p.channels["local-ollama"].retention = "local"  # type: ignore[misc]
    with pytest.raises(TypeError):
        p.channels["new"] = p.channels["openrouter"]  # type: ignore[index]
    assert isinstance(p.channels, MappingProxyType)


# ── malformed rows: each rule, with the file path and the exact problem ───────────

BAD = {
    # structure
    "top-level not a table": ([1, 2], "top level must be a table"),
    "top-level none": (None, "top level must be a table"),
    "no channels table": ({}, "no [channels] table"),
    "channels is a list": ({"channels": []}, "channels must be a table"),
    "channels is a string": ({"channels": "x"}, "channels must be a table"),
    "channel body not a table": ({"channels": {"a": 5}}, "channels.a must be a table"),
    "unknown top-level key": (lambda: {**good_raw(), "mode": "on"}, "unknown key 'mode'"),
    "unknown channel key": (lambda: mutated("channels.openrouter.color", "red"), "unknown key channels.openrouter.'color'"),
    "unknown caps key": (lambda: mutated("channels.openrouter.caps.tokens", 5), "unknown key channels.openrouter.caps.'tokens'"),
    "channel name with a space": ({"channels": {"a b": {}}}, "channel name"),
    "channel name with a slash": ({"channels": {"a/b": {}}}, "channel name"),
    "channel name leading dash": ({"channels": {"-a": {}}}, "channel name"),
    "channel name too long": ({"channels": {"a" * 65: {}}}, "channel name"),
    # enums
    "bad kind": (lambda: mutated("channels.openrouter.kind", "gemini_cli"), "channels.openrouter.kind must be one of"),
    "kind not a string": (lambda: mutated("channels.openrouter.kind", 3), "channels.openrouter.kind must be one of"),
    "bad retention": (lambda: mutated("channels.openrouter.retention", "forever"), "channels.openrouter.retention must be one of"),
    "retention uppercase": (lambda: mutated("channels.openrouter.retention", "LOCAL"), "retention must be one of"),
    "bad classification": (lambda: mutated("channels.openrouter.max_classification", "secret"), "max_classification must be one of"),
    "classification unstated": (lambda: mutated("channels.openrouter.max_classification", "unstated"), "max_classification must be one of"),
    "bad approval": (lambda: mutated("channels.openrouter.approval", "never"), "approval must be one of"),
    "approval auto": (lambda: mutated("channels.openrouter.approval", "auto"), "approval must be one of"),
    # types
    "provider int": (lambda: mutated("channels.openrouter.provider", 5), "provider must be a string"),
    "provider uppercase": (lambda: mutated("channels.openrouter.provider", "OpenRouter"), "provider must be lowercase"),
    "model int": (lambda: mutated("channels.openrouter.model", 5), "model must be a string"),
    "endpoint int": (lambda: mutated("channels.openrouter.endpoint", 5), "endpoint must be a string"),
    "endpoint list on opencode": (lambda: mutated("channels.openrouter.endpoint", ["https://x.example"]), "endpoint must be a string"),
    "endpoints string on cli": (lambda: mutated("channels.antigravity.endpoints", "https://x.example"), "endpoints must be a non-empty array"),
    "endpoints empty": (lambda: mutated("channels.antigravity.endpoints", []), "endpoints must be a non-empty array"),
    "endpoints item int": (lambda: mutated("channels.antigravity.endpoints", [3]), "endpoints[0] must be a string"),
    "caps not a table": (lambda: mutated("channels.openrouter.caps", 5), "caps must be a table"),
    "caps list": (lambda: mutated("channels.openrouter.caps", [1, 2, 3]), "caps must be a table"),
    "workspace_roots string": (lambda: {**good_raw(), "workspace_roots": "/srv/ws"}, "workspace_roots must be an array"),
    # missing required fields
    **{f"missing {k}": ((lambda k=k: mutated(f"channels.openrouter.{k}", delete=True)), f"channels.openrouter.{k} is missing")
       for k in ("kind", "provider", "endpoint", "model", "retention", "max_classification", "approval", "caps")},
    "missing endpoints on a cli": (lambda: mutated("channels.antigravity.endpoints", delete=True), "channels.antigravity.endpoints is missing"),
    **{f"missing caps.{k}": ((lambda k=k: mutated(f"channels.openrouter.caps.{k}", delete=True)), f"caps.{k} is missing")
       for k in ("sessions", "wall_s", "cost_usd")},
    "empty caps table": (lambda: mutated("channels.openrouter.caps", {}), "caps.sessions is missing"),
    # endpoint form confusion
    "endpoints on opencode": (lambda: mutated("channels.openrouter.endpoints", ["https://x.example"]), "takes a single `endpoint`"),
    "endpoint on a cli": (lambda: mutated("channels.antigravity.endpoint", "https://x.example"), "takes a list `endpoints`"),
    # endpoint shapes
    "endpoint ftp": (lambda: mutated("channels.openrouter.endpoint", "ftp://openrouter.ai"), "http or https"),
    "endpoint no scheme": (lambda: mutated("channels.openrouter.endpoint", "openrouter.ai"), "http or https"),
    "endpoint no host": (lambda: mutated("channels.openrouter.endpoint", "https:///v1"), "no host"),
    "endpoint userinfo": (lambda: mutated("channels.openrouter.endpoint", "https://u:p@openrouter.ai"), "credentials"),
    "endpoint query": (lambda: mutated("channels.openrouter.endpoint", "https://openrouter.ai/?k=v"), "credentials"),
    "endpoint fragment": (lambda: mutated("channels.openrouter.endpoint", "https://openrouter.ai/#x"), "credentials"),
    "endpoint bad port": (lambda: mutated("channels.openrouter.endpoint", "https://openrouter.ai:99999"), "not a valid URL"),
    "endpoint with a newline": (lambda: mutated("channels.openrouter.endpoint", "https://openrouter.ai\n/x"), "control"),
    "endpoint empty": (lambda: mutated("channels.openrouter.endpoint", ""), "non-empty"),
    # model ids
    "model with a newline": (lambda: mutated("channels.openrouter.model", "a\nb"), "model may contain only"),
    "model with a space": (lambda: mutated("channels.openrouter.model", "a b"), "model may contain only"),
    "model with a semicolon": (lambda: mutated("channels.openrouter.model", "a;rm"), "model may contain only"),
    "model with a dollar": (lambda: mutated("channels.openrouter.model", "$(x)"), "model may contain only"),
    "model leading dash": (lambda: mutated("channels.openrouter.model", "-x"), "model may contain only"),
    "model empty": (lambda: mutated("channels.openrouter.model", ""), "must not be empty"),
    "model too long": (lambda: mutated("channels.openrouter.model", "a" * 129), "longer than 128"),
    "cli model too long": (lambda: mutated("channels.claude-harness.model", "a" * 65), "longer than 64"),
    # caps
    "sessions zero": (lambda: mutated("channels.openrouter.caps.sessions", 0), "sessions must be a positive integer"),
    "sessions negative": (lambda: mutated("channels.openrouter.caps.sessions", -1), "sessions must be a positive integer"),
    "sessions bool": (lambda: mutated("channels.openrouter.caps.sessions", True), "sessions must be a positive integer"),
    "sessions float": (lambda: mutated("channels.openrouter.caps.sessions", 1.5), "sessions must be a positive integer"),
    "sessions string": (lambda: mutated("channels.openrouter.caps.sessions", "5"), "sessions must be a positive integer"),
    "sessions oversized": (lambda: mutated("channels.openrouter.caps.sessions", 10_001), "above the sanity ceiling"),
    "sessions huge": (lambda: mutated("channels.openrouter.caps.sessions", 10**400), "above the sanity ceiling"),
    "wall zero": (lambda: mutated("channels.openrouter.caps.wall_s", 0), "greater than zero"),
    "wall negative": (lambda: mutated("channels.openrouter.caps.wall_s", -5), "greater than zero"),
    "wall bool": (lambda: mutated("channels.openrouter.caps.wall_s", True), "wall_s must be a positive finite number"),
    "wall nan": (lambda: mutated("channels.openrouter.caps.wall_s", math.nan), "finite"),
    "wall inf": (lambda: mutated("channels.openrouter.caps.wall_s", math.inf), "finite"),
    "wall -inf": (lambda: mutated("channels.openrouter.caps.wall_s", -math.inf), "finite"),
    "wall string": (lambda: mutated("channels.openrouter.caps.wall_s", "60"), "wall_s must be a positive finite number"),
    "wall oversized": (lambda: mutated("channels.openrouter.caps.wall_s", 10**9), "above the sanity ceiling"),
    "wall huge": (lambda: mutated("channels.openrouter.caps.wall_s", 10**400), "above the sanity ceiling"),
    "cost zero": (lambda: mutated("channels.openrouter.caps.cost_usd", 0), "greater than zero"),
    "cost negative": (lambda: mutated("channels.openrouter.caps.cost_usd", -0.01), "greater than zero"),
    "cost nan": (lambda: mutated("channels.openrouter.caps.cost_usd", math.nan), "finite"),
    "cost inf": (lambda: mutated("channels.openrouter.caps.cost_usd", math.inf), "finite"),
    "cost bool": (lambda: mutated("channels.openrouter.caps.cost_usd", False), "cost_usd must be a positive finite number"),
    "cost oversized": (lambda: mutated("channels.openrouter.caps.cost_usd", 1000.01), "above the sanity ceiling"),
    # cross-field rules
    "unknown retention above open": (lambda: mutated("channels.claude-harness.max_classification", "gated"),
                                     "retention=unknown with max_classification=gated"),
    "unknown retention restricted": (lambda: mutated("channels.antigravity.max_classification", "restricted"),
                                     "cannot exceed open"),
    "local with a remote endpoint": (lambda: mutated("channels.local-ollama.endpoint", "https://ollama.example"),
                                     "retention=local requires a loopback endpoint"),
    "local with a lookalike host": (lambda: mutated("channels.local-ollama.endpoint", "http://127.0.0.1.nip.io:11434"),
                                    "requires a loopback endpoint"),
    "local with a lan host": (lambda: mutated("channels.local-ollama.endpoint", "http://192.168.1.5:11434"),
                              "requires a loopback endpoint"),
    "local cli with one remote endpoint": (
        lambda: {**mutated("channels.claude-harness.retention", "local"), },
        "retention=local requires a loopback endpoint"),
    # workspace roots
    "root relative": (lambda: {**good_raw(), "workspace_roots": ["srv/ws"]}, "not an absolute path"),
    "root slash": (lambda: {**good_raw(), "workspace_roots": ["/"]}, "filesystem root"),
    "root not normalised": (lambda: {**good_raw(), "workspace_roots": ["/srv/../ws"]}, "normalised"),
    "root trailing slash": (lambda: {**good_raw(), "workspace_roots": ["/srv/ws/"]}, "normalised"),
    "root with a control char": (lambda: {**good_raw(), "workspace_roots": ["/srv/w\x07s"]}, "control"),
    "root inside .git": (lambda: {**good_raw(), "workspace_roots": ["/srv/ws/.git"]}, "protected"),
    "root inside hooks": (lambda: {**good_raw(), "workspace_roots": ["/srv/ws/hooks/x"]}, "protected"),
    "root is the home": (lambda: {**good_raw(), "workspace_roots": [os.path.expanduser("~")]}, "home directory"),
    "root not a string": (lambda: {**good_raw(), "workspace_roots": [5]}, "workspace_roots[0] must be a string"),
    "too many roots": (lambda: {**good_raw(), "workspace_roots": [f"/srv/w{i}" for i in range(17)]}, "more than 16"),
}


@pytest.mark.parametrize("name", list(BAD))
def test_each_malformed_profile_is_refused_loudly(name):
    raw, needle = BAD[name]
    raw = raw() if callable(raw) else raw
    text = problems(raw)
    assert needle in text, f"{needle!r} not in {text!r}"


def test_every_problem_is_reported_at_once_not_just_the_first():
    raw = good_raw()
    raw["channels"]["openrouter"]["retention"] = "forever"
    raw["channels"]["openrouter"]["caps"]["sessions"] = 0
    del raw["channels"]["antigravity"]["model"]
    raw["bogus"] = 1
    with pytest.raises(ChannelProfileError) as ei:
        validate(raw, source="/f")
    assert len(ei.value.problems) >= 4
    assert "4 problems" in str(ei.value) or "problems" in str(ei.value)


def test_a_valid_channel_next_to_a_bad_one_is_not_used_either():
    raw = good_raw()
    raw["channels"]["openrouter"]["retention"] = "forever"
    with pytest.raises(ChannelProfileError):
        validate(raw, source="/f")           # never a partial profile


# ── typos get a did-you-mean hint ─────────────────────────────────────────────────

TYPOS = [
    # (where, wrong key, intended)
    ("", "channel", "channels"),                      # channels vs channel
    ("", "Channels", "channels"),
    ("", "workspace_root", "workspace_roots"),
    ("", "chanels", "channels"),
    ("channels.openrouter", "retenion", "retention"),   # dropped letter
    ("channels.openrouter", "retnetion", "retention"),  # adjacent swap = 1
    ("channels.openrouter", "Retention", "retention"),
    ("channels.openrouter", "max_clasification", "max_classification"),
    ("channels.openrouter", "classification", "max_classification"),
    ("channels.openrouter", "endpiont", "endpoint"),
    ("channels.openrouter", "endpoint_s", "endpoints"),
    ("channels.openrouter", "modle", "model"),
    ("channels.openrouter", "knd", "kind"),
    ("channels.openrouter", "provder", "provider"),
    ("channels.openrouter", "aproval", "approval"),
    ("channels.openrouter", "cap", "caps"),
    ("channels.openrouter.caps", "session", "sessions"),
    ("channels.openrouter.caps", "wall", "wall_s"),
    ("channels.openrouter.caps", "cost_usdd", "cost_usd"),
    ("channels.openrouter.caps", "usd", "cost_usd"),
]


@pytest.mark.parametrize(("where", "wrong", "intended"), TYPOS)
def test_a_typoed_key_is_flagged_with_a_hint(where, wrong, intended):
    raw = good_raw()
    node = raw
    for k in [x for x in where.split(".") if x]:
        node = node[k]
    if wrong != intended and intended in node and where:
        node[wrong] = node.pop(intended)          # the typo replaces the real key
    else:
        node[wrong] = node.pop(intended) if intended in node else 1
    text = problems(raw)
    assert f"did you mean '{intended}'?" in text, text


def test_short_names_use_the_shorter_distance_limit():
    # `kind` is short (<= 5 chars): distance 1 hints, distance 2 does not
    raw = good_raw()
    raw["channels"]["openrouter"]["kid"] = 1
    assert "did you mean 'kind'" in problems(raw)
    raw = good_raw()
    raw["channels"]["openrouter"]["xxkind"] = 1       # distance 2 from a 4-letter key: no hint
    text = problems(raw)
    assert "xxkind" in text and "did you mean 'kind'" not in text


def test_an_unrelated_unknown_key_is_still_flagged_without_a_hint():
    raw = good_raw()
    raw["channels"]["openrouter"]["zzzzzzzz"] = 1
    text = problems(raw)
    assert "unknown key" in text and "did you mean" not in text and "allowed:" in text


def test_near_miss_rule_directly():
    nm = channels.near_miss
    assert nm("retention", ("retention",)) is None            # exact
    assert nm("retenion", ("retention",)) == "retention"
    assert nm("rtention", ("retention",)) == "retention"
    assert nm("reetntion", ("retention",)) == "retention"      # swap + ... distance 2
    assert nm("rxxxntion", ("retention",)) is None             # distance 3
    assert nm("caps", ("kind", "model")) is None


def test_a_hostile_key_is_shown_escaped_and_truncated():
    raw = good_raw()
    raw["channels"]["openrouter"]["a\x1b[31m" + "z" * 100] = 1
    text = problems(raw)
    assert "\x1b" not in text and "z" * 60 not in text


# ── duplicates ────────────────────────────────────────────────────────────────────

def test_a_case_variant_channel_name_is_a_duplicate():
    raw = good_raw()
    raw["channels"]["OpenRouter"] = copy.deepcopy(raw["channels"]["openrouter"])
    assert "duplicate channel name" in problems(raw)


def test_a_fullwidth_variant_channel_name_is_refused():
    raw = good_raw()
    raw["channels"]["ｏpenrouter"] = copy.deepcopy(raw["channels"]["openrouter"])
    assert "channel name" in problems(raw)


# ── warnings ──────────────────────────────────────────────────────────────────────

def test_zdr_attested_on_loopback_is_allowed_but_warns():
    raw = mutated("channels.local-ollama.retention", "zdr-attested")
    p = validate(raw, source="x")
    assert any("zdr-attested" in w and "loopback" in w for w in p.warnings)
    assert p.channels["local-ollama"].warnings


def test_a_remote_zdr_attested_channel_does_not_warn():
    assert not validate(good_raw(), source="x").warnings


def test_an_empty_channels_table_is_valid_but_says_everything_is_denied():
    p = validate({"channels": {}}, source="x")
    assert p.channels == {} and any("denied" in w for w in p.warnings)


# ── TOML text and the file reader ─────────────────────────────────────────────────

@pytest.fixture
def prof(tmp_path_factory):
    d = tmp_path_factory.mktemp("p")

    def write(text: str | bytes, name: str = "ch.toml"):
        f = d / name
        f.write_bytes(text if isinstance(text, bytes) else text.encode())
        return f
    write.dir = d
    return write


def env_for(path) -> dict:
    return {settings.ENV_CHANNELS_FILE: str(path)}


def test_a_good_file_loads(prof):
    p = load_profile(env_for(prof(GOOD_TOML)))
    assert p.declared and len(p.channels) == 4 and p.source.endswith("ch.toml")


def test_invalid_toml_is_malformed_and_names_the_file(prof):
    f = prof("[channels\nkind = ")
    with pytest.raises(ChannelProfileError, match="invalid TOML") as ei:
        load_profile(env_for(f))
    assert str(f) in str(ei.value)


def test_a_channel_declared_twice_in_toml_is_malformed(prof):
    f = prof(GOOD_TOML + "\n[channels.openrouter]\nkind = \"opencode\"\n")
    with pytest.raises(ChannelProfileError, match="declared twice"):
        load_profile(env_for(f))


def test_non_utf8_is_malformed(prof):
    with pytest.raises(ChannelProfileError, match="UTF-8"):
        load_profile(env_for(prof(b"\xff\xfe[channels]\n")))


def test_a_file_over_one_megabyte_is_malformed(prof):
    f = prof("# " + "x" * (1024 * 1024) + "\n")
    with pytest.raises(ChannelProfileError, match="over the 1 MB limit"):
        load_profile(env_for(f))


def test_a_file_exactly_at_the_cap_is_read(prof):
    body = GOOD_TOML + "\n# "
    f = prof(body + "x" * (1024 * 1024 - len(body.encode())))
    assert f.stat().st_size == 1024 * 1024
    assert load_profile(env_for(f)).declared


def test_the_cap_is_a_parameter_of_the_reader(prof):
    f = prof(GOOD_TOML)
    with pytest.raises(ChannelProfileError, match="over"):
        channels._read_capped(str(f), 10)


def test_a_file_that_grows_after_the_stat_is_caught_by_the_read_loop(prof, monkeypatch):
    f = prof(GOOD_TOML)
    real_stat = os.stat
    small = real_stat(f)

    class Lie:
        st_mode, st_size = small.st_mode, 1          # claims to be tiny

    monkeypatch.setattr(os, "stat", lambda p, *a, **k: Lie)
    with pytest.raises(ChannelProfileError, match="over"):
        channels._read_capped(str(f), 100)


def test_a_symlink_to_nowhere_is_malformed_not_missing(prof):
    link = prof.dir / "dangling.toml"
    link.symlink_to(prof.dir / "nope.toml")
    with pytest.raises(ChannelProfileError, match="broken symlink"):
        load_profile(env_for(link))
    # even when it is the DEFAULT path and no env var is set
    with pytest.raises(ChannelProfileError, match="broken symlink"):
        channels._read_capped(str(link))


def test_a_good_symlink_is_followed(prof):
    f = prof(GOOD_TOML)
    link = prof.dir / "link.toml"
    link.symlink_to(f)
    assert load_profile(env_for(link)).declared


def test_a_symlink_loop_is_malformed(prof):
    a, b = prof.dir / "a.toml", prof.dir / "b.toml"
    a.symlink_to(b)
    b.symlink_to(a)
    with pytest.raises(ChannelProfileError, match="could not be read"):
        load_profile(env_for(a))


def test_a_directory_is_not_a_regular_file(prof):
    with pytest.raises(ChannelProfileError, match="not a regular file"):
        load_profile(env_for(prof.dir))


def test_a_fifo_is_refused_without_blocking(prof):
    fifo = prof.dir / "fifo.toml"
    os.mkfifo(fifo)
    with pytest.raises(ChannelProfileError, match="not a regular file"):
        load_profile(env_for(fifo))


def test_the_env_var_set_to_a_missing_file_is_malformed(prof):
    with pytest.raises(ChannelProfileError, match=f"{settings.ENV_CHANNELS_FILE} points at a file that does not exist"):
        load_profile(env_for(prof.dir / "missing.toml"))


@pytest.mark.parametrize("value", ["", "   "])
def test_the_env_var_set_but_empty_is_malformed(value):
    with pytest.raises(ChannelProfileError, match="set but empty"):
        load_profile({settings.ENV_CHANNELS_FILE: value})


def test_a_relative_env_path_is_malformed():
    with pytest.raises(ChannelProfileError, match="absolute"):
        load_profile({settings.ENV_CHANNELS_FILE: "channels.toml"})


def test_no_env_var_and_no_default_file_means_nothing_declared(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("h")
    monkeypatch.setenv("HOME", str(home))
    p = load_profile({})
    assert p.declared is False and p.channels == {}
    assert str(home) in p.source and p.source.endswith("harness2-channels.toml")
    assert any("every channel is denied" in w and "[channels.<name>]" in w for w in p.warnings)


def test_the_default_file_is_used_when_the_env_var_is_unset(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("h")
    monkeypatch.setenv("HOME", str(home))
    cfg = home / ".config" / "cognitive-engine"
    cfg.mkdir(parents=True)
    (cfg / "harness2-channels.toml").write_text(GOOD_TOML)
    assert load_profile({}).declared


def test_a_malformed_default_file_is_still_malformed(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("h")
    monkeypatch.setenv("HOME", str(home))
    cfg = home / ".config" / "cognitive-engine"
    cfg.mkdir(parents=True)
    (cfg / "harness2-channels.toml").write_text("channels = 5\n")
    with pytest.raises(ChannelProfileError, match="channels must be a table"):
        load_profile({})


def test_the_reader_is_the_only_file_access(prof, monkeypatch):
    f = prof(GOOD_TOML)
    seen = []
    real = channels._read_capped
    monkeypatch.setattr(channels, "_read_capped", lambda p, *a: (seen.append(p), real(p, *a))[1])
    load_profile(env_for(f))
    assert seen == [str(f)]


# ── classification and the policy matrix ──────────────────────────────────────────

@pytest.mark.parametrize(("sources", "want"), [
    (["open"], "open"), (["open", "gated"], "gated"), (["gated", "open", "restricted"], "restricted"),
    (["open", None], "restricted"), ([None], "restricted"), ([], "restricted"), (None, "restricted"),
    (["open", "unstated"], "restricted"), (["OPEN"], "restricted"), (["open", 3], "restricted"),
    ("open", "restricted"), (iter(["open", "open"]), "open"),
])
def test_the_sweep_classification_is_the_strictest_source(sources, want):
    assert channels.sweep_classification(sources) == want


def make_channel(retention, ceiling, model="ollama/q", name="c") -> channels.Channel:
    return channels.Channel(name=name, kind="opencode", provider="ollama", endpoints=("http://127.0.0.1:11434",),
                            model=model, retention=retention, max_classification=ceiling,
                            caps=channels.Caps(1, 1.0, 1.0))


def expected_allowed(retention, zdr, cls, ceiling) -> bool:
    if zdr == "strict" and retention == "unknown":
        return False
    if retention == "unknown" and cls != "open":
        return False
    order = ("open", "gated", "restricted")
    return order.index(cls) <= order.index(ceiling)


MATRIX = [(r, z, c, m) for r in channels.RETENTIONS for z in ("strict", "off")
          for c in ("open", "gated", "restricted", None, "unstated") for m in channels.CLASSIFICATIONS]


@pytest.mark.parametrize(("retention", "zdr", "cls", "ceiling"), MATRIX)
def test_the_policy_matrix(retention, zdr, cls, ceiling):
    ch = make_channel(retention, ceiling)
    v = channels.decide(ch, zdr=zdr, classification=cls)
    eff = channels.effective_classification(cls)
    assert v.allowed is expected_allowed(retention, zdr, eff, ceiling)
    assert (not v.allowed) == bool(v.rules) == bool(v.reasons)
    assert all("channel 'c'" in r and "rule " in r for r in v.reasons)


def test_each_denial_names_the_rule_that_fired():
    d = channels.decide
    assert d(make_channel("unknown", "open"), zdr="strict", classification="open").rules == ("retention-vs-zdr",)
    assert d(make_channel("unknown", "open"), zdr="off", classification="gated").rules == (
        "unknown-retention-open-only", "classification-ceiling")
    assert d(make_channel("local", "open"), zdr="strict", classification="gated").rules == ("classification-ceiling",)
    assert d(make_channel("zdr-attested", "gated"), zdr="strict", classification=None).rules == ("classification-ceiling",)
    assert d(make_channel("local", "restricted", model="openrouter/free"), zdr="strict",
             classification="open").rules == ("free-tier",)
    assert d(make_channel("local", "restricted", model="x:free"), zdr="off", classification="open").allowed


@pytest.mark.parametrize("zdr", ["STRICT", "", None, "lax", 1, "strict "])
def test_an_unknown_zdr_mode_is_denied(zdr):
    v = channels.decide(make_channel("local", "restricted"), zdr=zdr, classification="open")
    assert not v.allowed and "zdr-mode" in v.rules


def test_unknown_retention_is_only_ever_usable_for_open_content_with_zdr_off():
    ok = [(z, c) for z in ("strict", "off") for c in ("open", "gated", "restricted")
          if channels.decide(make_channel("unknown", "open"), zdr=z, classification=c).allowed]
    assert ok == [("off", "open")]
