"""Fail-closed research feasibility summary for Binance USDM #609.

Does not authenticate exchangeInfo itself or imply an executable order.
All external inputs must be separately evidenced and time-bound.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _decimal(x):
    try:
        value = Decimal(str(x))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def assess_candidate_feasibility(*, candidate_id: str, depth: dict,
                                 filters: dict, costs: dict,
                                 source_proof: dict) -> dict:
    blockers = []
    if not candidate_id:
        blockers.append("CANDIDATE_ID_MISSING")
    if not isinstance(source_proof, dict) or source_proof.get("verified") is not True:
        blockers.append("SOURCE_PROOF_MISSING")
    if not isinstance(depth, dict) or depth.get("status") != "DISPLAYED_DEPTH_BOUND":
        blockers.append("DEPTH_NOT_PROVEN")
    if not isinstance(filters, dict) or filters.get("exchange_info_verified") is not True:
        blockers.append("EXCHANGE_FILTERS_UNVERIFIED")
    if not isinstance(costs, dict) or costs.get("status") != "HYPOTHETICAL_NET_CALCULATED":
        blockers.append("NET_COSTS_UNPROVEN")
    qty = _decimal(depth.get("quantity")) if isinstance(depth, dict) else None
    price = _decimal(depth.get("weighted_price")) if isinstance(depth, dict) else None
    min_qty = _decimal(filters.get("min_qty")) if isinstance(filters, dict) else None
    step = _decimal(filters.get("step_size")) if isinstance(filters, dict) else None
    min_notional = _decimal(filters.get("min_notional")) if isinstance(filters, dict) else None
    if any(v is None or v <= 0 for v in (qty, price, min_qty, step, min_notional)):
        blockers.append("INVALID_FILTER_OR_DEPTH_VALUES")
    else:
        if qty < min_qty:
            blockers.append("BELOW_MIN_QTY")
        if (qty / step) != (qty / step).to_integral_value():
            blockers.append("INVALID_STEP_SIZE")
        if qty * price < min_notional:
            blockers.append("BELOW_MIN_NOTIONAL")
    if blockers:
        return {"candidate_id": candidate_id, "status": "FEASIBILITY_UNPROVEN",
                "blockers": blockers, "promotion_allowed": False,
                "live_allowed": False, "execution_effect": "NONE"}
    return {"candidate_id": candidate_id, "status": "CONDITIONAL_HYPOTHETICAL_FEASIBILITY",
            "base_net_usdt": costs["base_net_usdt"],
            "stressed_net_usdt": costs["stressed_net_usdt"],
            "displayed_depth_weighted_price": depth["weighted_price"],
            "fill_proven": False, "promotion_allowed": False,
            "live_allowed": False, "execution_effect": "NONE"}
