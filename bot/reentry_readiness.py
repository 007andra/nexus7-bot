"""Observability-only governance for a future LIVE re-entry.

This module has no execution authority. It reads already-available runtime
state and reports whether the configured conservative re-entry profile is
currently satisfied. A READY result never authorizes an order and never
bypasses drawdown, recovery, ownership, reconciliation, market-data, sizing,
CROSS-stress, protection, or dispatch gates.
"""
from __future__ import annotations

import math
import os
import time

from bot.config import cfg

AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}

_TARGET_RISK_PCT = 0.0025
_TARGET_MARGIN_FRACTION = 0.25
_TARGET_MAX_POSITIONS = 1
_TARGET_MAX_SUBMISSIONS = 1
_EMIT_EVERY_S = 300.0
_LAST = {}


def _pending_orders(engine) -> int:
    orders = getattr(engine, "orders", None)
    fn = getattr(orders, "pending_orders", None)
    if not callable(fn):
        return 0
    try:
        return len(list(fn()))
    except Exception:
        return 0


def snapshot(engine) -> dict:
    from bot import drawdown_recovery, pilot
    from bot.final_sizing_invariants import operator_margin_fraction
    from bot.operator_runtime_policy import _risk_override_enabled

    risk = getattr(engine, "risk", None)
    drawdown = float(getattr(risk, "drawdown", float("nan")))
    peak = float(
        getattr(risk, "peak_equity", 0.0)
        or getattr(risk, "peak_balance", 0.0)
        or 0.0
    )
    equity = float(
        getattr(risk, "balance", 0.0)
        or getattr(engine, "_pilot_account_equity", 0.0)
        or 0.0
    )
    limit = float(cfg.MAX_DRAWDOWN)
    margin_fraction = float(operator_margin_fraction())
    recovery = drawdown_recovery.policy_from_env()
    override = bool(_risk_override_enabled())
    positions = len(getattr(engine, "positions", {}) or {})
    pending_orders = _pending_orders(engine)
    preflight_ready = bool(getattr(engine, "_pilot_live_prelive_ready", False))

    blockers = []
    if not math.isfinite(drawdown) or drawdown < 0:
        blockers.append("DRAWDOWN_UNREADABLE")
    elif drawdown >= limit:
        blockers.append("DRAWDOWN_ABOVE_LIMIT")
    if recovery.authorized:
        blockers.append("RECOVERY_AUTHORIZED")
    if override:
        blockers.append("RISK_OVERRIDE_ENABLED")
    if float(cfg.MAX_RISK_PCT) > _TARGET_RISK_PCT + 1e-12:
        blockers.append("RISK_PCT_ABOVE_REENTRY_CAP")
    if margin_fraction > _TARGET_MARGIN_FRACTION + 1e-12:
        blockers.append("MARGIN_FRACTION_ABOVE_REENTRY_CAP")
    if int(cfg.MAX_POSITIONS) > _TARGET_MAX_POSITIONS:
        blockers.append("MAX_POSITIONS_ABOVE_REENTRY_CAP")
    if int(pilot.PILOT_MAX_CONCURRENT_POSITIONS) > _TARGET_MAX_POSITIONS:
        blockers.append("PILOT_POSITIONS_ABOVE_REENTRY_CAP")
    if int(pilot.MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION) > _TARGET_MAX_SUBMISSIONS:
        blockers.append("PILOT_SUBMISSIONS_ABOVE_REENTRY_CAP")
    if positions:
        blockers.append("OPEN_POSITIONS")
    if pending_orders:
        blockers.append("PENDING_ORDERS")
    if not preflight_ready:
        blockers.append("PREFLIGHT_NOT_READY")

    required_equity = peak * (1.0 - limit) if peak > 0 and 0 < limit < 1 else None
    equity_gap = (
        max(0.0, required_equity - equity)
        if required_equity is not None and math.isfinite(equity)
        else None
    )

    return {
        "status": "READY" if not blockers else "BLOCKED",
        "blockers": tuple(blockers),
        "drawdown": drawdown,
        "configured_limit": limit,
        "equity": equity,
        "peak_equity": peak,
        "required_equity_for_limit": required_equity,
        "equity_gap_to_limit": equity_gap,
        "recovery_reason": recovery.reason,
        "recovery_authorized": recovery.authorized,
        "override": override,
        "max_risk_pct": float(cfg.MAX_RISK_PCT),
        "margin_fraction": margin_fraction,
        "max_positions": int(cfg.MAX_POSITIONS),
        "pilot_max_positions": int(pilot.PILOT_MAX_CONCURRENT_POSITIONS),
        "pilot_max_submissions": int(pilot.MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION),
        "positions": positions,
        "pending_orders": pending_orders,
        "preflight_ready": preflight_ready,
        **AUTHORITY,
    }


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, tuple):
        return ",".join(value) or "NONE"
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return f"{value:.12g}"
    return str(value).replace(" ", "_")


def emit(engine, log, *, force: bool = False) -> dict:
    row = snapshot(engine)
    signature = (
        row["status"], row["blockers"], row["recovery_reason"],
        row["preflight_ready"], round(row["drawdown"], 8)
        if math.isfinite(row["drawdown"]) else "nan",
    )
    now = time.monotonic()
    key = id(engine)
    previous = _LAST.get(key)
    if not force and previous and previous[0] == signature and now - previous[1] < _EMIT_EVERY_S:
        return row
    _LAST[key] = (signature, now)
    fields = " ".join(f"{k}={_fmt(v)}" for k, v in row.items())
    log.warning("[REENTRY_READINESS_V1] %s", fields)
    return row
