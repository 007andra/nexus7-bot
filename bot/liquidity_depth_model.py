"""Depth-based capacity diagnostics for NEXUS research.

Uses actual order-book levels to derive VWAP impact and a quantity capacity
under a chosen impact ceiling. No sizing mutation occurs here.
"""
from __future__ import annotations

from dataclasses import asdict
import math

from bot.binance_usdm_simulator import FillResult, vwap_market_fill


def depth_capacity(
    orderbook: dict,
    *,
    order_side: str,
    max_impact_bps: float,
) -> dict[str, float | bool | None]:
    if max_impact_bps < 0:
        raise ValueError("max_impact_bps cannot be negative")
    key = "a" if order_side.upper() == "BUY" else "b"
    raw = orderbook.get(key, []) if isinstance(orderbook, dict) else []

    cumulative_qty = 0.0
    accepted: FillResult | None = None
    for row in raw:
        try:
            level_qty = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not math.isfinite(level_qty) or level_qty <= 0:
            continue
        trial_qty = cumulative_qty + level_qty
        fill = vwap_market_fill(orderbook, order_side, trial_qty)
        if fill.average_price is None or fill.impact_bps is None:
            break
        if fill.impact_bps > max_impact_bps:
            break
        cumulative_qty = trial_qty
        accepted = fill

    if accepted is None:
        return {
            "capacity_qty": 0.0,
            "capacity_notional": 0.0,
            "vwap": None,
            "impact_bps": None,
            "full_depth_within_cap": False,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        }

    return {
        "capacity_qty": accepted.filled_qty,
        "capacity_notional": accepted.filled_qty * float(accepted.average_price),
        "vwap": accepted.average_price,
        "impact_bps": accepted.impact_bps,
        "full_depth_within_cap": not accepted.partial,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def liquidity_capped_qty(
    *,
    risk_budget_qty: float,
    margin_cap_qty: float,
    depth_cap_qty: float,
) -> dict[str, float | str]:
    values = {
        "risk_budget": float(risk_budget_qty),
        "margin_cap": float(margin_cap_qty),
        "liquidity_cap": float(depth_cap_qty),
    }
    if any(not math.isfinite(v) or v < 0 for v in values.values()):
        raise ValueError("invalid quantity cap")
    binding = min(values, key=values.get)
    return {
        "qty": values[binding],
        "binding": binding,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def fill_snapshot(orderbook: dict, *, order_side: str, qty: float) -> dict:
    fill = vwap_market_fill(orderbook, order_side, qty)
    return {
        **asdict(fill),
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
