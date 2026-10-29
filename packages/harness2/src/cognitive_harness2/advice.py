"""The read-only recommendation behind `harness2_trigger` (RFC 004 §2.3, Decision 5).

Decides whether a System 2 probe is worth suggesting, and whether it could actually
start. It is advisory and pull-only: it starts no sandbox, calls no model, writes no
file and never creates a run. The only checks it makes are the ones `harness2_status`
already makes (constructing the sandbox runner validates the local runtime; it does not
run anything) plus, only when the caller asks and the endpoint is OpenRouter, the same
`GET /api/v1/key` the quota pre-flight uses.

Two separate answers, never folded into one:

  * `advise`: a trigger fired (`decision.probe`). It says nothing about readiness.
  * `ready`: tri-state over the four readiness checks (sandbox, egress, quota, model).
      - `false`: at least one check is known-bad. `blockers` lists each one.
      - `true`:  every check is positively ok.
      - `null`:  none is known-bad but at least one is unchecked or unknown (quota is
                 unchecked unless the caller passes `check_quota=true` on OpenRouter).
    A known-bad check always wins over an unchecked one.

A fired trigger with `ready: false` still has `advise: true`; the caller reads `blockers`
to see why the probe would refuse.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from . import egress, triggers
from .engine import Engine
from .sandbox import DockerRunner, SandboxUnavailable, SbxRunner


async def _sandbox(eng: Engine) -> dict:
    def build() -> dict:
        try:
            runner = eng.sandbox_factory()  # validates the runtime; runs nothing
        except SandboxUnavailable as e:
            return {"ok": False, "runtime": "unavailable", "detail": f"fail closed: {e}"}
        except Exception as e:  # noqa: BLE001 - a broken probe of the host must not break the advice
            return {"ok": False, "runtime": "unavailable", "detail": f"could not check ({type(e).__name__})"}
        name = ("sbx" if isinstance(runner, SbxRunner)
                else "rootless-docker" if isinstance(runner, DockerRunner) else type(runner).__name__)
        return {"ok": True, "runtime": name, "detail": "usable; nothing was started"}
    return await asyncio.to_thread(build)


def _egress(eng: Engine, zdr: str, classification: str) -> dict:
    try:
        return {"ok": True, "detail": egress.check_endpoint(
            zdr=zdr, base_url=eng.base_url, model=eng.model or "", classification=classification,
            zdr_endpoints=eng.zdr_endpoints)}
    except egress.EgressDenied as e:
        return {"ok": False, "detail": f"egress refused: {e}"}


async def _quota(eng: Engine, check: bool) -> dict:
    if (urlparse(eng.base_url).hostname or "") != "openrouter.ai":
        return {"ok": True, "status": "not_applicable", "detail": "endpoint is not OpenRouter; no pre-flight runs"}
    if not check:
        return {"ok": None, "status": "not_checked", "detail": "pass check_quota=true to pre-flight (one GET, no model call)"}
    v = await eng.quota_check()
    return {"ok": v.ok, "status": "checked", "detail": v.reason}


def cost_class(eng: Engine, fired: bool) -> str:
    """none | local (loopback endpoint) | free_quota (free-tier model) | metered (anything else)."""
    if not fired:
        return "none"
    if (urlparse(eng.base_url).hostname or "").lower() in egress.LOOPBACK:
        return "local"
    return "free_quota" if egress._is_free_tier(eng.model or "") else "metered"


def tri_state_ready(readiness: dict[str, dict]) -> bool | None:
    """False if any check is known-bad; True only if all are positively ok; else None (unknown)."""
    oks = [v["ok"] for v in readiness.values()]
    if any(ok is False for ok in oks):
        return False
    if oks and all(ok is True for ok in oks):
        return True
    return None


async def advise(eng: Engine, decision: triggers.Decision, *, zdr: str, classification: str,
                 check_quota: bool) -> dict:
    sandbox, eg, q = await _sandbox(eng), _egress(eng, zdr, classification), await _quota(eng, check_quota)
    model = ({"ok": True, "detail": f"model {eng.model!r}"} if eng.model
             else {"ok": False, "detail": "no generator model configured (set COGNITIVE_HARNESS2_MODEL)"})
    readiness = {"sandbox": sandbox, "egress": eg, "quota": q, "model": model}
    blockers = [f"{k}: {v['detail']}" for k, v in readiness.items() if v["ok"] is False]
    ready = tri_state_ready(readiness)
    nxt = None
    if decision.probe and ready is not False:
        nxt = ("call harness2_propose (dry_run first), then harness2_probe and harness2_compose; "
               "the composed patch needs human approval")
        if ready is None:
            nxt += "; readiness is not fully checked (see readiness for what is unknown)"
    return {
        "advise": decision.probe,
        "trigger": decision.trigger,
        "reason": decision.reason,
        "ready": ready,
        "blockers": blockers,
        "readiness": readiness,
        "cost_class": cost_class(eng, decision.probe),
        "requires_human_approval": True,
        "read_only": True,
        "next": nxt,
    }
