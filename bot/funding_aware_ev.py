"""Funding-aware expected-value diagnostics for Binance USD-M research."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True)
class ExpectedFunding:
    timestamp: float
    rate: float


def funding_fraction_for_hold(
    *,
    side: str,
    opened_at: float,
    expected_hold_seconds: float,
    events: Iterable[ExpectedFunding],
) -> float:
    """Return signed PnL fraction from funding over the expected holding window.

    Positive result improves PnL; negative result is a cost.
    """
    position_side = side.upper()
    if position_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if expected_hold_seconds < 0:
        raise ValueError("expected_hold_seconds cannot be negative")

    end = float(opened_at) + float(expected_hold_seconds)
    total = 0.0
    for event in events:
        ts = float(event.timestamp)
        rate = float(event.rate)
        if not math.isfinite(ts) or not math.isfinite(rate):
            raise ValueError("non-finite funding event")
        if float(opened_at) < ts <= end:
            total += -rate if position_side == "LONG" else rate
    return total


def adjust_ev_for_funding(
    *,
    base_ev_pct: float,
    funding_pnl_fraction: float,
) -> dict[str, float | str]:
    if not math.isfinite(float(base_ev_pct)) or not math.isfinite(
        float(funding_pnl_fraction)
    ):
        raise ValueError("non-finite EV input")
    adjusted = float(base_ev_pct) + float(funding_pnl_fraction) * 100.0
    return {
        "base_ev_pct": float(base_ev_pct),
        "funding_pnl_pct": float(funding_pnl_fraction) * 100.0,
        "funding_adjusted_ev_pct": adjusted,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
