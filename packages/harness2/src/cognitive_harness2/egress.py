"""Where candidate-generation prompts may be sent (RFC 004 §3.2; RFC 001 egress rules).

Probe prompts carry the user's task and code. Under `zdr: strict` they go only to a
local endpoint or to an endpoint the operator has explicitly listed as zero-retention,
and never to a free-tier model. Unclassified or unknown content is `restricted`, as in
RFC 001: classification can only tighten, never default to open.

`check_endpoint` runs BEFORE any request is built, so a denied call sends nothing.
"""
from __future__ import annotations

from urllib.parse import urlparse

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
CLASSIFICATIONS = ("open", "gated", "unstated", "restricted")


class EgressDenied(PermissionError):
    """The request would send content somewhere the policy forbids."""


def _is_free_tier(model: str) -> bool:
    m = model.strip().lower()
    return m.startswith("openrouter/free") or m.endswith(":free")


def effective_classification(value: str | None) -> str:
    """Unknown or missing classification is restricted, never open."""
    return value if value in ("open", "gated") else "restricted"


def check_endpoint(
    *,
    zdr: str,
    base_url: str,
    model: str,
    classification: str | None,
    zdr_endpoints: frozenset[str] = frozenset(),
) -> str:
    """Return a one-line reason the call is allowed, or raise EgressDenied."""
    host = (urlparse(base_url).hostname or "").lower()
    cls = effective_classification(classification)
    local = host in LOOPBACK
    listed = host in {h.lower() for h in zdr_endpoints}

    if not host:
        raise EgressDenied(f"cannot determine the host of {base_url!r}")
    if _is_free_tier(model) and (zdr == "strict" or cls != "open"):
        raise EgressDenied(f"free-tier model {model!r} is barred for {cls} content under zdr={zdr}")
    if zdr == "strict" and not (local or listed):
        raise EgressDenied(
            f"zdr=strict allows only a local endpoint or a listed zero-retention host; {host!r} is neither")
    if cls != "open" and not (local or listed):
        raise EgressDenied(f"{cls} content may only go to a local or listed zero-retention host, not {host!r}")
    return f"{'local' if local else 'listed zero-retention'} endpoint {host}, zdr={zdr}, {cls} content"
