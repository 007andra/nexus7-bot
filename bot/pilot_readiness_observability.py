"""Non-sensitive startup observability for controlled LIVE pilot readiness.

This module is diagnostic only. It does not change PAPER/LIVE selection,
release authorization, exchange connectivity, risk limits, or order routing.
It intentionally reports booleans only and never logs release-token values or
credentials.
"""
from __future__ import annotations

def snapshot() -> dict:
    from bot import pilot

    paper = bool(pilot._paper_trade_enabled())
    configured = bool(pilot.PILOT_ENABLED)
    release_approved = bool(pilot._release_approved())
    from bot import runtime_release_contract
    contract = runtime_release_contract.current()
    return {
        "configured": configured,
        "enabled": configured and not paper,
        "paper_trade": paper,
        "release_approved": release_approved,
        "release_authorized": bool(contract.release_authorized),
        "validation_lock": bool(contract.validation_lock),
    }


def install(log) -> None:
    state = snapshot()
    log.info(
        "[PILOT_READINESS] configured=%s enabled=%s paper_trade=%s release_approved=%s "
        "release_authorized=%s validation_lock=%s",
        str(state["configured"]).lower(),
        str(state["enabled"]).lower(),
        str(state["paper_trade"]).lower(),
        str(state["release_approved"]).lower(),
        str(state["release_authorized"]).lower(),
        str(state["validation_lock"]).lower(),
    )
