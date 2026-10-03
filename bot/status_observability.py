"""Read-only status enrichment for NEXUS-7 API responses.

Keeps observability metrics outside ``TradingEngine.get_status`` so runtime
bootstrap no longer needs to monkey-patch the engine status method. This module
never changes strategy decisions, risk state, exchange state, or execution
permission.
"""
from __future__ import annotations

import os

from bot.logger import log


def _drawdown_blocked(engine) -> tuple[bool | None, float, float]:
    """Return observational drawdown-block state without mutating risk policy.

    ``None`` means the state could not be read. Callers must report it as a
    blocker rather than as "not blocked" (observability must not over-claim
    that LIVE entries are available).
    """
    try:
        from bot.config import cfg

        drawdown = float(getattr(getattr(engine, "risk", None), "drawdown", 0.0) or 0.0)
        limit = float(getattr(cfg, "MAX_DRAWDOWN", 0.0) or 0.0)
        if drawdown != drawdown or limit != limit:
            return None, 0.0, 0.0
        override = str(os.environ.get("LIVE_RISK_OVERRIDE_APPROVED", "")).strip().lower() == "true"
        blocked = bool(limit > 0.0 and drawdown >= limit and not override)
        return blocked, drawdown, limit
    except Exception as exc:  # noqa: BLE001 - observability only; reported as unknown
        log.debug("[STATUS_OBSERVABILITY] drawdown_state_unreadable=%s", type(exc).__name__)
        return None, 0.0, 0.0


def execution_observability(engine) -> dict:
    """Describe the effective execution mode without granting permission.

    ``paper_trade=False`` alone is not enough to claim that exchange mutations
    are possible: VALIDATION_LOCK intentionally keeps the runtime in read-only
    SHADOW LIVE, the controlled pilot has an explicit release gate, and runtime
    risk gates may block new entries even while the process remains online.
    This helper is observational only and defaults to the safe/blocked side
    whenever release state cannot be proven.
    """
    if getattr(engine, "paper_trade", False):
        return {
            "effective_execution_mode": "PAPER",
            "orders_sent_to_exchange": False,
            "new_entries_allowed": False,
            "execution_blockers": ("PAPER_MODE",),
            "execution_effect": "SIMULATED",
        }

    if getattr(engine, "_validation_safety_lock_active", False):
        return {
            "effective_execution_mode": "SHADOW_LIVE",
            "orders_sent_to_exchange": False,
            "new_entries_allowed": False,
            "execution_blockers": ("VALIDATION_LOCK",),
            "execution_effect": "NONE",
        }

    pilot = getattr(engine, "pilot", None)
    if pilot is not None and getattr(pilot, "enabled", False):
        try:
            status = pilot.status(engine=engine, client=getattr(engine, "client", None))
        except Exception:
            return {
                "effective_execution_mode": "LIVE_LOCKED",
                "orders_sent_to_exchange": False,
                "new_entries_allowed": False,
                "execution_blockers": ("PILOT_STATUS_UNAVAILABLE",),
                "execution_effect": "NONE",
            }
        if not bool(status.get("release_approved", False)):
            return {
                "effective_execution_mode": "LIVE_LOCKED",
                "orders_sent_to_exchange": False,
                "new_entries_allowed": False,
                "execution_blockers": ("PILOT_RELEASE_NOT_APPROVED",),
                "execution_effect": "NONE",
            }

    blockers: list[str] = []
    if not bool(getattr(engine, "connected", True)):
        blockers.append("ENGINE_DISCONNECTED")
    if not bool(getattr(engine, "active", True)):
        blockers.append("ENGINE_INACTIVE")

    drawdown_blocked, drawdown, drawdown_limit = _drawdown_blocked(engine)
    if drawdown_blocked is None:
        blockers.append("DRAWDOWN_STATE_UNKNOWN")
    elif drawdown_blocked:
        blockers.append("DRAWDOWN_HARD_GATE")

    if blockers:
        return {
            "effective_execution_mode": "LIVE_BLOCKED",
            "orders_sent_to_exchange": False,
            "new_entries_allowed": False,
            "execution_blockers": tuple(blockers),
            "drawdown_pct": round(drawdown * 100.0, 2),
            "drawdown_limit_pct": round(drawdown_limit * 100.0, 2),
            "execution_effect": "BLOCK_NEW_ENTRIES",
        }

    return {
        "effective_execution_mode": "LIVE",
        "orders_sent_to_exchange": True,
        "new_entries_allowed": True,
        "execution_blockers": (),
        "execution_effect": "REAL",
    }


def enrich_status(engine, base_status):
    """Return a copy of ``base_status`` enriched with read-only diagnostics.

    Best-effort by design: an observability failure leaves the canonical engine
    status intact and can never change execution behavior.
    """
    if not isinstance(base_status, dict):
        return base_status

    out = dict(base_status)
    try:
        from bot import funnel_metrics as fm
        from bot import mtf_shadow as ms
        from bot import nexus_decision_dedupe
        from bot import nexus_persistence as np

        out["nexus_persistent_metrics"] = np.get_cached_metrics()
        out["funnel_metrics"] = fm.get_funnel_metrics()
        out["mtf_shadow_metrics"] = ms.snapshot()
        out["nexus_dedupe_metrics"] = nexus_decision_dedupe.snapshot()
        out.update(execution_observability(engine))

        if getattr(engine, "paper_trade", False):
            out["paper_wallet"] = {
                "balance": round(
                    float(
                        getattr(
                            engine,
                            "_paper_balance",
                            engine.risk.balance,
                        ) or 0.0
                    ),
                    4,
                ),
                "drawdown_pct": round(float(engine.risk.drawdown) * 100.0, 2),
                "isolated_from_exchange": True,
            }
    except Exception as exc:
        log.debug(
            "[STATUS_OBSERVABILITY] best_effort_failed error=%s "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )
        try:
            out.update(execution_observability(engine))
        except Exception:
            out.update(
                {
                    "effective_execution_mode": "UNKNOWN_LOCKED",
                    "orders_sent_to_exchange": False,
                    "new_entries_allowed": False,
                    "execution_blockers": ("STATUS_UNAVAILABLE",),
                    "execution_effect": "NONE",
                }
            )
    return out
