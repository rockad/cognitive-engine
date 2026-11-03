"""Daily quota pre-flight against OpenRouter `GET /api/v1/key` (RFC 004 §2.4).

Probing fans out many calls, so it checks the budget first and aborts gracefully when it
is exhausted or throttled instead of burning what is left. The gate fails closed: a
missing key, an unreachable endpoint or a response it cannot read is "not ok".

⚠️ Field names follow RFC 004 (`free_model_daily_requests`) with a fallback to
`limit_remaining`. They were NOT verified against the live API (no key was used while
writing this), so `evaluate` is a pure function with every shape under test, and the
first real run should confirm which fields OpenRouter returns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

KEY_URL = "https://openrouter.ai/api/v1/key"


@dataclass(frozen=True)
class QuotaVerdict:
    ok: bool
    reason: str
    detail: dict = field(default_factory=dict)


def _remaining(value: object) -> float | None:
    """Remaining requests from an int, or a {remaining} / {used, limit} mapping."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if isinstance(value.get("remaining"), (int, float)):
            return float(value["remaining"])
        if isinstance(value.get("used"), (int, float)) and isinstance(value.get("limit"), (int, float)):
            return float(value["limit"]) - float(value["used"])
    return None


def evaluate(status: int | None, payload: object) -> QuotaVerdict:
    if status == 429:
        return QuotaVerdict(False, "throttled (HTTP 429)")
    if status is None or status >= 400:
        return QuotaVerdict(False, f"quota unknown (HTTP {status})")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return QuotaVerdict(False, "quota unknown (unreadable response)")
    if "free_model_daily_requests" in data:
        left = _remaining(data["free_model_daily_requests"])
        if left is None:
            return QuotaVerdict(False, "quota unknown (unreadable free_model_daily_requests)")
        return (QuotaVerdict(True, f"{left:g} free-model requests left today", {"remaining": left})
                if left > 0 else QuotaVerdict(False, "daily free-model requests exhausted", {"remaining": left}))
    if "limit_remaining" in data:
        left = data["limit_remaining"]
        if left is None:
            return QuotaVerdict(True, "no spending limit set", {})
        if isinstance(left, (int, float)) and not isinstance(left, bool):
            return (QuotaVerdict(True, f"{left:g} credit remaining", {"remaining": left})
                    if left > 0 else QuotaVerdict(False, "credit exhausted", {"remaining": left}))
    return QuotaVerdict(False, "quota unknown (no recognised quota field)")


async def preflight(api_key: str | None, client: httpx.AsyncClient | None = None, url: str = KEY_URL) -> QuotaVerdict:
    if not api_key:
        return QuotaVerdict(False, "quota unknown (no OpenRouter API key configured)")
    own = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            body = resp.json()
        except ValueError:
            body = None
        return evaluate(resp.status_code, body)
    except httpx.HTTPError as e:
        return QuotaVerdict(False, f"quota unknown ({type(e).__name__})")
    finally:
        if own:
            await client.aclose()
