"""The channel profile: which proposer channels may carry content, and under what ceiling (RFC 005).

Every channel a Proposer session could use is DECLARED in one TOML file, under one policy:

    kind               opencode | claude_cli | agy_cli
    provider           the model provider (opencode: the model id's prefix; claude_cli: anthropic)
    endpoint(s)        opencode takes one `endpoint`; the vendor CLIs take a list `endpoints`
    model              the exact model id
    retention          local | zdr-attested | unknown
    max_classification open < gated < restricted: the most sensitive content the channel may carry
    caps               {sessions, wall_s, cost_usd}: hard per-sweep caps, all three required, above zero;
                       cost_usd = 0 ("this sweep must spend nothing") is accepted for retention=local only
    approval           per-sweep (a human approves each sweep; the only value)

A channel is allowed only if its retention satisfies the zdr mode (`strict`: only `local` or
`zdr-attested`) AND the sweep's classification is within the channel's ceiling. An undeclared channel is
denied. A subscription channel (`retention = "unknown"`) may only carry `open` content.

⚠️ ZDR is the user's DECLARATION. Nothing here contacts a provider or verifies a retention claim.

The profile is configuration that grants egress, so it is read like the Jev config: a malformed file is
refused LOUDLY (`ChannelProfileError`, with the file path and every problem), never repaired and never
partly used. The reader is size-capped, regular-file-only and symlink-aware; it is the only file access
in this module, and this module opens no socket and starts no process.

Unset env var + missing default file means "no channels declared": everything is denied, which is not an
error. The env var set to a missing file IS an error.
"""
from __future__ import annotations

import errno
import math
import os
import re
import stat
import tomllib
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from . import egress, invariants
from .settings import DEFAULT_CHANNELS_FILE, ENV_CHANNELS_FILE

MAX_PROFILE_BYTES = 1024 * 1024

KINDS = ("opencode", "claude_cli", "agy_cli")
RETENTIONS = ("local", "zdr-attested", "unknown")
CLASSIFICATIONS = ("open", "gated", "restricted")        # ordered, least to most sensitive
APPROVALS = ("per-sweep",)
ZDR_MODES = ("strict", "off")                             # server.py's Literal; anything else is denied
_RANK = {c: i for i, c in enumerate(CLASSIFICATIONS)}

# Sanity ceilings: "effectively unbounded" is malformed. Raising one is a deliberate edit here.
MAX_SESSIONS_CEILING = 1_000
MAX_WALL_SECONDS_CEILING = 7 * 24 * 3600
MAX_USD_CEILING = 1_000.0
MAX_TOKENS_CEILING = 1_000_000_000
MAX_CHANNELS = 64
MAX_ROOTS = 16

# Model ids: a strict character set, no leading dash, no whitespace, bounded, per channel kind.
_MODEL_CHARS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
MODEL_ID_MAX = {"opencode": 128, "claude_cli": 64, "agy_cli": 64}

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")

_TOP_KEYS = ("channels", "workspace_roots")
_TOP_ALIASES = {"channel": "channels", "workspace_root": "workspace_roots", "workspaces": "workspace_roots",
                "roots": "workspace_roots"}
_CHANNEL_KEYS = ("kind", "provider", "endpoint", "endpoints", "model", "retention", "max_classification", "caps",
                 "approval")
_CHANNEL_ALIASES = {"classification": "max_classification", "max_class": "max_classification",
                    "zdr": "retention", "cap": "caps"}
_CAP_KEYS = ("sessions", "wall_s", "cost_usd")
_CAP_ALIASES = {"session": "sessions", "wall": "wall_s", "wall_seconds": "wall_s", "seconds": "wall_s",
                "usd": "cost_usd", "cost": "cost_usd"}

HOWTO = ("create it with one [channels.<name>] table per channel (kind, provider, endpoint, model, retention, "
         "max_classification, approval and a [channels.<name>.caps] table); see the 'Channel profile' section "
         "of packages/harness2/README.md")


class ChannelProfileError(ValueError):
    """The profile cannot be trusted. Carries the path and every problem; the message names both."""

    def __init__(self, path: str, problems: Iterable[str]):
        self.path = path
        self.problems = tuple(problems)
        shown = "; ".join(self.problems)
        super().__init__(f"channel profile {path} is malformed ({len(self.problems)} problem"
                         f"{'s' if len(self.problems) != 1 else ''}): {shown}")


@dataclass(frozen=True)
class Caps:
    sessions: int
    wall_s: float
    cost_usd: float

    def to_dict(self) -> dict:
        return {"sessions": self.sessions, "wall_s": self.wall_s, "cost_usd": self.cost_usd}


@dataclass(frozen=True)
class Channel:
    name: str
    kind: str
    provider: str
    endpoints: tuple[str, ...]          # canonical `scheme://host:port`
    model: str
    retention: str
    max_classification: str
    caps: Caps
    approval: str = "per-sweep"
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def endpoint_keys(self) -> frozenset[tuple[str, int]]:
        return frozenset(endpoint_key(e) for e in self.endpoints)  # type: ignore[misc]


@dataclass(frozen=True)
class ChannelProfile:
    source: str                          # the file path (or a label for a dict)
    channels: Mapping[str, Channel] = field(default_factory=lambda: MappingProxyType({}))
    workspace_roots: tuple[str, ...] = ()
    declared: bool = True                # False: no profile file at all (everything denied)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rules: tuple[str, ...]               # the rule ids that fired (empty when allowed)
    reasons: tuple[str, ...]


# ── pure helpers ──────────────────────────────────────────────────────────────────

def effective_classification(value: Any) -> str:
    """open, gated or restricted. Anything else (None, 'unstated', junk) is restricted: it can only tighten."""
    return value if isinstance(value, str) and value in _RANK else "restricted"


def sweep_classification(sources: Iterable[Any] | None) -> str:
    """The strictest classification of a sweep's sources (RFC 005 inheritance). Unclassified is restricted,
    and a sweep with no sources at all is unclassified."""
    if sources is None or isinstance(sources, (str, bytes)):
        return "restricted"
    worst = None
    for s in sources:
        c = effective_classification(s)
        if worst is None or _RANK[c] > _RANK[worst]:
            worst = c
    return worst or "restricted"


def exceeds(classification: str, ceiling: str) -> bool:
    return _RANK[effective_classification(classification)] > _RANK[ceiling]


def endpoint_key(url: str) -> tuple[str, int] | None:
    """(host, effective port) of an http(s) URL, or None when it cannot be read."""
    try:
        p = urlparse(url.strip())
        host, port = p.hostname, p.port
    except (ValueError, AttributeError):
        return None
    if p.scheme not in ("http", "https") or not host:
        return None
    return host.lower(), port or (443 if p.scheme == "https" else 80)


def model_id_problem(kind: str, model: Any) -> str | None:
    """Why `model` is not an acceptable model id for this kind of channel, or None. Never echoes the value."""
    if not isinstance(model, str):
        return "model must be a string"
    limit = MODEL_ID_MAX.get(kind, 64)
    if not model:
        return "model must not be empty"
    if len(model) > limit:
        return f"model is longer than {limit} characters"
    if not _MODEL_CHARS.fullmatch(model):
        return ("model may contain only letters, digits and . - _ : / (no whitespace, control or shell "
                "characters, and no leading dash)")
    return None


def bad_directory(path: str) -> str | None:
    """Why `path` cannot be a workspace directory (pure text rules), or None."""
    if not path.strip():
        return "is empty"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in path):
        return "contains control or invisible characters"
    if not os.path.isabs(path):
        return "is not an absolute path"
    from pathlib import PurePosixPath
    p = PurePosixPath(os.path.normpath(path))
    home = PurePosixPath(os.path.normpath(os.path.expanduser("~")))
    if p == PurePosixPath("/") or p == home or p in home.parents:
        return "is the filesystem root, the home directory or one of its ancestors"
    try:
        invariants.assert_path_unprotected(os.path.normpath(path).lstrip("/"))
    except invariants.InvariantViolation as e:
        return f"is in a protected location ({e})"
    return None


def decide(channel: Channel, *, zdr: Any, classification: Any) -> Verdict:
    """Apply the policy to one declared channel. Collects every rule that fires; allowed only if none does.

    Rules (ids appear in the reasons):
      zdr-mode                  the zdr mode is not one this policy knows
      retention-vs-zdr          zdr=strict permits only retention local or zdr-attested
      unknown-retention-open-only  retention unknown carries only `open` content
      classification-ceiling    the sweep's classification is above the channel's max_classification
      free-tier                 a free-tier model never carries non-open content, or anything under zdr=strict
    """
    cls = effective_classification(classification)
    fired: list[tuple[str, str]] = []
    n = channel.name
    if zdr not in ZDR_MODES:
        fired.append(("zdr-mode", f"zdr mode {str(zdr)[:20]!r} is not one of {', '.join(ZDR_MODES)}"))
    if zdr == "strict" and channel.retention not in ("local", "zdr-attested"):
        fired.append(("retention-vs-zdr", f"retention={channel.retention} is not local or zdr-attested, "
                      "which zdr=strict requires"))
    if channel.retention == "unknown" and cls != "open":
        fired.append(("unknown-retention-open-only",
                      f"retention=unknown may only carry open content, and this sweep is {cls}"))
    if exceeds(cls, channel.max_classification):
        fired.append(("classification-ceiling",
                      f"the sweep is {cls}, above this channel's max_classification={channel.max_classification}"))
    if egress._is_free_tier(channel.model) and (zdr == "strict" or cls != "open"):
        fired.append(("free-tier", f"free-tier model {channel.model!r} is barred for {cls} content under zdr={zdr}"))
    return Verdict(not fired, tuple(r for r, _ in fired),
                   tuple(f"channel {n!r}: rule {r}: {t}" for r, t in fired))


# ── validation ────────────────────────────────────────────────────────────────────

def _edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance: insert, delete, substitute and adjacent swap each cost 1."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def near_miss(key: str, known: Iterable[str], aliases: Mapping[str, str] = MappingProxyType({})) -> str | None:
    """The known key a misspelled one was probably meant to be. Not an exact key; compared case-insensitively;
    an explicit alias, or edit distance <= 2 (<= 1 when the known key has 5 characters or fewer)."""
    known = tuple(known)
    if key in known:
        return None
    low = key.lower()
    if low in aliases:
        return aliases[low]
    best: tuple[int, str] | None = None
    for k in known:
        dist = _edit_distance(low, k)
        if dist <= (1 if len(k) <= 5 else 2) and (best is None or dist < best[0]):
            best = (dist, k)
    return best[1] if best else None


def _show(value: Any, limit: int = 40) -> str:
    return ascii(str(value)[:limit])


def _describe(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "a boolean"
    if isinstance(v, (int, float)):
        return "a number"
    if isinstance(v, str):
        return "a string"
    if isinstance(v, (list, tuple)):
        return "an array"
    if isinstance(v, Mapping):
        return "a table"
    return type(v).__name__


class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, text: str) -> None:
        self.items.append(text)

    def unknown_keys(self, at: str, table: Mapping[Any, Any], known: tuple[str, ...],
                     aliases: Mapping[str, str], skip: frozenset[str] = frozenset()) -> None:
        for key in table:
            if key in known or key in skip:
                continue
            if not isinstance(key, str):
                self.add(f"{at}: key {_show(key)} is not a string")
                continue
            hint = near_miss(key, known, aliases)
            where = f"{at}." if at else ""
            if hint:
                self.add(f"unknown key {where}{_show(key)}; did you mean '{hint}'?")
            else:
                self.add(f"unknown key {where}{_show(key)} (allowed: {', '.join(known)})")


def _text(p: _Problems, at: str, v: Any, *, maxlen: int = 256) -> str | None:
    if not isinstance(v, str):
        p.add(f"{at} must be a string, found {_describe(v)}")
        return None
    if not v or v != v.strip():
        p.add(f"{at} must be a non-empty string without leading or trailing space")
        return None
    if len(v) > maxlen:
        p.add(f"{at} is longer than {maxlen} characters")
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in v):
        p.add(f"{at} contains control or invisible characters")
        return None
    return v


def _enum(p: _Problems, at: str, v: Any, allowed: tuple[str, ...]) -> str | None:
    if not isinstance(v, str):
        p.add(f"{at} must be one of {', '.join(allowed)}, found {_describe(v)}")
        return None
    if v not in allowed:
        p.add(f"{at} must be one of {', '.join(allowed)}, found {_show(v)}")
        return None
    return v


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _caps(p: _Problems, at: str, raw: Any, *, zero_usd: bool = False) -> Caps | None:
    """`zero_usd`: a cost_usd of exactly 0 is a real cap ("spend nothing"), allowed only where the model is local."""
    if not isinstance(raw, Mapping):
        p.add(f"{at} must be a table with sessions, wall_s and cost_usd, found {_describe(raw)}")
        return None
    before = len(p.items)
    p.unknown_keys(at, raw, _CAP_KEYS, _CAP_ALIASES)
    sessions = wall = usd = None
    for key in _CAP_KEYS:
        if key not in raw:
            p.add(f"{at}.{key} is missing: a hard cap is required")
    v = raw.get("sessions")
    if "sessions" in raw:
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            p.add(f"{at}.sessions must be a positive integer (zero, negative, fractional, boolean and non-numeric "
                  "are refused)")
        elif v > MAX_SESSIONS_CEILING:
            p.add(f"{at}.sessions is above the sanity ceiling {MAX_SESSIONS_CEILING}")
        else:
            sessions = v
    for key, ceiling, unit in (("wall_s", MAX_WALL_SECONDS_CEILING, "seconds"), ("cost_usd", MAX_USD_CEILING, "usd")):
        if key not in raw:
            continue
        v = raw[key]
        if not _number(v):
            p.add(f"{at}.{key} must be a positive finite number, found {_describe(v)}")
        elif isinstance(v, float) and not math.isfinite(v):
            p.add(f"{at}.{key} must be finite (NaN and infinity are refused)")
        elif v < 0 or (v == 0 and not (key == "cost_usd" and zero_usd)):
            p.add(f"{at}.{key} must be greater than zero (zero and negative are refused"
                  + ("; a zero cost_usd is accepted only for retention=local" if key == "cost_usd" else "") + ")")
        elif v > ceiling:
            p.add(f"{at}.{key} is above the sanity ceiling {ceiling:g} {unit}")
        elif key == "wall_s":
            wall = float(v)
        else:
            usd = float(v)
    if len(p.items) == before and sessions is not None and wall is not None and usd is not None:
        return Caps(sessions, wall, usd)
    return None


def _endpoint(p: _Problems, at: str, v: Any) -> str | None:
    s = _text(p, at, v)
    if s is None:
        return None
    try:
        parsed = urlparse(s)
        host, port = parsed.hostname, parsed.port
        creds = parsed.username is not None or parsed.password is not None or bool(parsed.query) or bool(parsed.fragment)
    except ValueError:
        p.add(f"{at} is not a valid URL")
        return None
    if parsed.scheme not in ("http", "https"):
        p.add(f"{at} must be an http or https URL")
        return None
    if not host:
        p.add(f"{at} has no host")
        return None
    if creds:
        p.add(f"{at} must not embed credentials (userinfo, a query or a fragment)")
        return None
    if any(c.isspace() for c in s):
        p.add(f"{at} must not contain whitespace")
        return None
    shown = f"[{host.lower()}]" if ":" in host else host.lower()
    return f"{parsed.scheme}://{shown}:{port or (443 if parsed.scheme == 'https' else 80)}"


def _channel(p: _Problems, name: str, raw: Any) -> Channel | None:
    at = f"channels.{name}"
    if not isinstance(raw, Mapping):
        p.add(f"{at} must be a table, found {_describe(raw)}")
        return None
    before = len(p.items)
    p.unknown_keys(at, raw, _CHANNEL_KEYS, _CHANNEL_ALIASES)

    kind = _enum(p, f"{at}.kind", raw["kind"], KINDS) if "kind" in raw else None
    if "kind" not in raw:
        p.add(f"{at}.kind is missing (one of {', '.join(KINDS)})")

    # endpoint / endpoints: the form depends on the kind, and confusing them is the common mistake
    single = kind == "opencode"
    vendor = kind in ("claude_cli", "agy_cli")
    endpoints: list[str] = []
    if single and "endpoints" in raw:
        p.add(f"{at}: an opencode channel takes a single `endpoint`, not `endpoints`")
    if vendor and "endpoint" in raw:
        p.add(f"{at}: a {kind} channel takes a list `endpoints`, not a single `endpoint`")
    if "endpoint" in raw and not vendor:
        e = _endpoint(p, f"{at}.endpoint", raw["endpoint"])
        if e:
            endpoints.append(e)
    if "endpoints" in raw and not single:
        lst = raw["endpoints"]
        if not isinstance(lst, (list, tuple)) or not lst:
            p.add(f"{at}.endpoints must be a non-empty array of URLs, found {_describe(lst)}")
        elif len(lst) > 16:
            p.add(f"{at}.endpoints has more than 16 entries")
        else:
            for i, item in enumerate(lst):
                e = _endpoint(p, f"{at}.endpoints[{i}]", item)
                if e and e not in endpoints:
                    endpoints.append(e)
    if not endpoints and not ("endpoint" in raw or "endpoints" in raw):
        p.add(f"{at}.{'endpoint' if single else 'endpoints'} is missing" if kind else f"{at}.endpoint is missing")

    provider = None
    if "provider" in raw:
        pv = _text(p, f"{at}.provider", raw["provider"], maxlen=32)
        if pv is not None and not _PROVIDER.fullmatch(pv):
            p.add(f"{at}.provider must be lowercase letters, digits, . - _ (starting with a letter or digit)")
        elif pv is not None:
            provider = pv
    else:
        p.add(f"{at}.provider is missing")

    model = None
    if "model" in raw:
        if isinstance(raw["model"], str):
            why = model_id_problem(kind or "opencode", raw["model"])
            if why:
                p.add(f"{at}.{why}")
            else:
                model = raw["model"]
        else:
            p.add(f"{at}.model must be a string, found {_describe(raw['model'])}")
    else:
        p.add(f"{at}.model is missing")

    retention = _enum(p, f"{at}.retention", raw["retention"], RETENTIONS) if "retention" in raw else None
    if "retention" not in raw:
        p.add(f"{at}.retention is missing (one of {', '.join(RETENTIONS)})")
    ceiling = _enum(p, f"{at}.max_classification", raw["max_classification"], CLASSIFICATIONS) \
        if "max_classification" in raw else None
    if "max_classification" not in raw:
        p.add(f"{at}.max_classification is missing (one of {', '.join(CLASSIFICATIONS)})")
    approval = _enum(p, f"{at}.approval", raw["approval"], APPROVALS) if "approval" in raw else None
    if "approval" not in raw:
        p.add(f"{at}.approval is missing (one of {', '.join(APPROVALS)})")
    caps = _caps(p, f"{at}.caps", raw["caps"], zero_usd=retention == "local") if "caps" in raw else None
    if "caps" not in raw:
        p.add(f"{at}.caps is missing: hard sessions, wall_s and cost_usd caps are required")

    warnings: list[str] = []
    if retention == "unknown" and ceiling is not None and ceiling != "open":
        p.add(f"{at}: retention=unknown with max_classification={ceiling}: a channel whose retention is unknown "
              "cannot exceed open")
    if retention == "local":
        for e in endpoints:
            k = endpoint_key(e)
            if k is None or k[0] not in egress.LOOPBACK:
                p.add(f"{at}: retention=local requires a loopback endpoint, but {e} is not loopback")
    if retention == "zdr-attested":
        for e in endpoints:
            k = endpoint_key(e)
            if k is not None and k[0] in egress.LOOPBACK:
                warnings.append(f"channel {name!r}: retention=zdr-attested on the loopback endpoint {e}: a "
                                "loopback gateway forwards content off this machine; ZDR here is only your declaration")

    if len(p.items) != before or None in (kind, provider, model, retention, ceiling, approval, caps) or not endpoints:
        return None
    assert kind and provider and model and retention and ceiling and approval and caps
    return Channel(name=name, kind=kind, provider=provider, endpoints=tuple(endpoints), model=model,
                   retention=retention, max_classification=ceiling, caps=caps, approval=approval,
                   warnings=tuple(warnings))


def _roots(p: _Problems, raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        p.add(f"workspace_roots must be an array of absolute paths, found {_describe(raw)}")
        return ()
    if len(raw) > MAX_ROOTS:
        p.add(f"workspace_roots has more than {MAX_ROOTS} entries")
        return ()
    out: list[str] = []
    for i, r in enumerate(raw):
        at = f"workspace_roots[{i}]"
        s = _text(p, at, r, maxlen=512)
        if s is None:
            continue
        if os.path.normpath(s) != s:
            p.add(f"{at} must be a normalised absolute path (no '..', '//', '.' or trailing slash)")
            continue
        why = bad_directory(s)
        if why:
            p.add(f"{at} {why}")
            continue
        if s not in out:
            out.append(s)
    return tuple(out)


def validate(raw: Any, *, source: str = "<profile>") -> ChannelProfile:
    """The profile for `raw` (the parsed TOML), or `ChannelProfileError` listing every problem. Pure."""
    p = _Problems()
    if not isinstance(raw, Mapping):
        raise ChannelProfileError(source, [f"the top level must be a table, found {_describe(raw)}"])
    p.unknown_keys("", raw, _TOP_KEYS, _TOP_ALIASES)

    channels: dict[str, Channel] = {}
    warnings: list[str] = []
    seen: dict[str, str] = {}
    table = raw.get("channels")
    if "channels" not in raw:
        p.add("no [channels] table: declare at least one channel as [channels.<name>]")
    elif not isinstance(table, Mapping):
        p.add(f"channels must be a table of channel tables, found {_describe(table)}")
    else:
        if len(table) > MAX_CHANNELS:
            p.add(f"more than {MAX_CHANNELS} channels declared")
        for name, body in table.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                p.add(f"channel name {_show(name)} must be letters, digits, _ or - (at most 64, not starting with _ or -)")
                continue
            folded = unicodedata.normalize("NFKC", name).casefold()
            if folded in seen:
                p.add(f"duplicate channel name: {_show(name)} repeats {_show(seen[folded])} (names are case-insensitive)")
                continue
            seen[folded] = name
            ch = _channel(p, name, body)
            if ch is not None:
                channels[name] = ch
                warnings.extend(ch.warnings)
        if not table:
            warnings.append("the profile declares no channels, so every channel is denied")
    roots = _roots(p, raw["workspace_roots"]) if "workspace_roots" in raw else ()

    if p.items:
        raise ChannelProfileError(source, p.items)
    return ChannelProfile(source=source, channels=MappingProxyType(dict(channels)), workspace_roots=roots,
                          declared=True, warnings=tuple(warnings))


# ── the loader and its pinned reader ──────────────────────────────────────────────

def _read_capped(path: str, limit: int = MAX_PROFILE_BYTES) -> bytes | None:
    """The one place this module touches a file. Returns None only when there is no entry at all.

    lstat first (so a dangling symlink is not mistaken for 'no file'), a regular-file check on what the path
    resolves to, a size check before the read, a non-blocking open (a FIFO is never waited on), a regular-file
    check on the open descriptor, and a size check on what was actually read. Raises ChannelProfileError.
    """
    def fail(msg: str) -> ChannelProfileError:
        return ChannelProfileError(path, [msg])

    try:
        is_link = stat.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise fail(f"the file could not be read ({errno.errorcode.get(e.errno or 0, 'OSError')})") from None
    try:
        st = os.stat(path)
    except FileNotFoundError:
        if is_link:
            raise fail("broken symlink: the target does not exist") from None
        return None
    except OSError as e:
        raise fail(f"the file could not be read ({errno.errorcode.get(e.errno or 0, 'OSError')})") from None
    if not stat.S_ISREG(st.st_mode):
        raise fail("the file could not be read (not a regular file)")
    if st.st_size > limit:
        raise fail(f"the file is over the {limit // (1024 * 1024)} MB limit")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        raise fail(f"the file could not be read ({errno.errorcode.get(e.errno or 0, 'OSError')})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise fail("the file could not be read (not a regular file)")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise fail(f"the file is over the {limit // (1024 * 1024)} MB limit")
            chunks.append(chunk)
    except OSError as e:
        raise fail(f"the file could not be read ({errno.errorcode.get(e.errno or 0, 'OSError')})") from None
    finally:
        os.close(fd)
    return b"".join(chunks)


def profile_path(environ: Mapping[str, str] | None = None) -> tuple[str, bool]:
    """(path, explicit). Explicit means the env var named it, so a missing file is an error."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_CHANNELS_FILE)
    if raw is None:
        return str(Path(DEFAULT_CHANNELS_FILE).expanduser()), False
    if not raw.strip():
        raise ChannelProfileError(f"{ENV_CHANNELS_FILE}=''", [f"{ENV_CHANNELS_FILE} is set but empty"])
    path = str(Path(raw).expanduser())
    if not os.path.isabs(path):
        raise ChannelProfileError(raw[:200], [f"{ENV_CHANNELS_FILE} must be an absolute path"])
    return path, True


def parse_text(text: str, *, source: str) -> ChannelProfile:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        msg = str(e)
        hint = " (a channel or key is declared twice)" if "twice" in msg or "already" in msg else ""
        raise ChannelProfileError(source, [f"invalid TOML: {msg[:200]}{hint}"]) from None
    return validate(raw, source=source)


def load_profile(environ: Mapping[str, str] | None = None) -> ChannelProfile:
    """Read and validate the profile. Raises `ChannelProfileError` when it is there but cannot be trusted, or
    when the env var names a file that is not there. No file and no env var: an empty, undeclared profile."""
    path, explicit = profile_path(environ)
    data = _read_capped(path)
    if data is None:
        if explicit:
            raise ChannelProfileError(path, [f"{ENV_CHANNELS_FILE} points at a file that does not exist"])
        return ChannelProfile(source=path, declared=False,
                              warnings=(f"no channel profile at {path}: no channel is declared, so every channel "
                                        f"is denied; {HOWTO}",))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ChannelProfileError(path, ["the file is not valid UTF-8"]) from None
    return parse_text(text, source=path)
