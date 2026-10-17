"""PILOT_BUDGET_STUDY_V1.

Research-only absolute-loss-budget study for a future segregated pilot.
It derives fixed R-multiple budget references from the current RiskManagerV3
risk unit (equity * max_risk_pct). No row is a recommendation or LIVE limit.

This module cannot alter HWM, drawdown, risk, sizing, leverage, thresholds,
recovery/override, funds, orders, dispatch, or LIVE authority.
"""
from __future__ import annotations

import math

R_MULTIPLES = (3, 5, 8, 10)
SHADOW_REFERENCE_R = 5

AUTHORITY = {
    "authority": "RESEARCH_ONLY",
    "research_only": True,
    "shadow_reference_only": True,
    "not_capital_recommendation": True,
    "historical_hwm_preserved": True,
    "lifetime_drawdown_preserved": True,
    "current_hard_gate_unchanged": True,
    "risk_policy_unchanged": True,
    "sizing_unchanged": True,
    "leverage_unchanged": True,
    "thresholds_unchanged": True,
    "fund_movement_authorized": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def evaluate(readiness: dict, oos: dict | None = None) -> dict:
    equity = _finite(readiness.get("equity"))
    risk_pct = _finite(readiness.get("max_risk_pct"))
    valid = (
        equity is not None
        and risk_pct is not None
        and equity > 0.0
        and 0.0 < risk_pct <= 1.0
    )
    risk_unit = equity * risk_pct if valid else None

    rows = []
    if risk_unit is not None:
        for multiple in R_MULTIPLES:
            budget = risk_unit * multiple
            rows.append({
                "r_multiple": multiple,
                "absolute_loss_budget_usdt": budget,
                "budget_pct_of_equity": budget / equity,
                "full_stop_units": multiple,
                "live_limit": False,
                "recommendation": False,
            })

    reference = next(
        (row for row in rows if row["r_multiple"] == SHADOW_REFERENCE_R),
        None,
    )
    return {
        **AUTHORITY,
        "status": "AVAILABLE_RESEARCH_ONLY" if valid else "INPUT_UNAVAILABLE",
        "equity_snapshot": equity,
        "max_risk_pct_snapshot": risk_pct,
        "risk_unit_usdt": risk_unit,
        "budget_rows": tuple(rows),
        "shadow_reference_r": SHADOW_REFERENCE_R,
        "shadow_reference_budget_usdt": (
            reference["absolute_loss_budget_usdt"] if reference else None
        ),
        "shadow_reference_budget_pct_of_equity": (
            reference["budget_pct_of_equity"] if reference else None
        ),
        "oos_status": str((oos or {}).get("status") or "UNAVAILABLE"),
        "interpretation_guard": (
            "FIXED_R_MULTIPLES_FOR_SHADOW_STRESS_ONLY_NOT_A_LIVE_LOSS_LIMIT"
        ),
    }


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, (tuple, list)):
        return ",".join(map(str, value)) or "NONE"
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value).replace(" ", "_")


def format_log(row: dict) -> str:
    levels = "|".join(
        f"{item['r_multiple']}R:{item['absolute_loss_budget_usdt']:.8f}"
        for item in row.get("budget_rows") or ()
    ) or "NONE"
    return (
        "[PILOT_BUDGET_STUDY_V1] "
        f"status={_fmt(row.get('status'))} "
        f"equity={_fmt(row.get('equity_snapshot'))} "
        f"max_risk_pct={_fmt(row.get('max_risk_pct_snapshot'))} "
        f"risk_unit_usdt={_fmt(row.get('risk_unit_usdt'))} "
        f"levels={levels} "
        f"shadow_reference_r={_fmt(row.get('shadow_reference_r'))} "
        f"shadow_reference_budget_usdt={_fmt(row.get('shadow_reference_budget_usdt'))} "
        "not_capital_recommendation=true shadow_reference_only=true "
        "historical_hwm_preserved=true lifetime_drawdown_preserved=true "
        "current_hard_gate_unchanged=true promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )
