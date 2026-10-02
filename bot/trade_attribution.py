"""Deterministic post-trade attribution.

Separates market outcome from execution drag without changing any runtime policy.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradeAttributionInput:
    side: str
    qty: float
    decision_entry: float
    actual_entry: float
    decision_exit: float
    actual_exit: float
    fees: float = 0.0
    funding: float = 0.0
    regime: str = "UNKNOWN"
    exit_reason: str = "UNKNOWN"

    def __post_init__(self) -> None:
        if self.side.upper() not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if self.qty <= 0:
            raise ValueError("qty must be positive")
        if min(self.decision_entry, self.actual_entry, self.decision_exit, self.actual_exit) <= 0:
            raise ValueError("prices must be positive")
        if self.fees < 0:
            raise ValueError("fees cannot be negative")


def _signed_move(side: str, start: float, end: float, qty: float) -> float:
    mult = 1.0 if side.upper() == "LONG" else -1.0
    return mult * (end - start) * qty


def attribute_trade(item: TradeAttributionInput) -> dict:
    """Return additive PnL attribution in quote currency."""
    market = _signed_move(item.side, item.decision_entry, item.decision_exit, item.qty)
    entry_effect = _signed_move(item.side, item.actual_entry, item.decision_entry, item.qty)
    exit_effect = _signed_move(item.side, item.decision_exit, item.actual_exit, item.qty)
    gross = market + entry_effect + exit_effect
    net = gross - item.fees + item.funding
    execution_effect = entry_effect + exit_effect - item.fees
    return {
        "side": item.side.upper(),
        "regime": item.regime,
        "exit_reason": item.exit_reason,
        "market_pnl_at_decision_prices": market,
        "entry_execution_effect": entry_effect,
        "exit_execution_effect": exit_effect,
        "fees_effect": -item.fees,
        "funding_effect": item.funding,
        "gross_pnl": gross,
        "net_pnl": net,
        "execution_effect_total": execution_effect,
        "reconciles": abs(net - (market + entry_effect + exit_effect - item.fees + item.funding)) < 1e-12,
    }
