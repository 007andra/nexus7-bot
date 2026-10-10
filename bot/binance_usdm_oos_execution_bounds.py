"""Conservative hypothetical marketable execution bounds for #609.

This is a counterfactual cost model, NOT proof an order filled. Inputs must
come from independently timestamped market data at or after decision time.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _positive(x):
    try:
        d = Decimal(str(x))
        return d if d.is_finite() and d > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def estimate_market_entry(*, side: str, decision_ms: int, quote: dict,
                          quantity: str, slippage_bps: str,
                          max_quote_age_ms: int = 1000) -> dict:
    blockers = []
    if side not in ("SHORT", "LONG"):
        blockers.append("INVALID_SIDE")
    if not isinstance(decision_ms, int):
        blockers.append("INVALID_DECISION_TIME")
    ts = quote.get("timestamp_ms") if isinstance(quote, dict) else None
    if not isinstance(ts, int) or not isinstance(decision_ms, int):
        blockers.append("QUOTE_TIME_MISSING")
    elif ts < decision_ms or ts - decision_ms > max_quote_age_ms:
        blockers.append("STALE_OR_PREDECISION_QUOTE")
    bid = _positive(quote.get("bid")) if isinstance(quote, dict) else None
    ask = _positive(quote.get("ask")) if isinstance(quote, dict) else None
    qty = _positive(quantity)
    slip = _positive(slippage_bps)
    if bid is None or ask is None or bid >= ask:
        blockers.append("INVALID_BBO")
    if qty is None:
        blockers.append("INVALID_QUANTITY")
    if slip is None:
        blockers.append("POSITIVE_SLIPPAGE_REQUIRED")
    if not isinstance(quote, dict) or quote.get("independent_source_verified") is not True:
        blockers.append("INDEPENDENT_QUOTE_NOT_VERIFIED")
    if blockers:
        return {"status": "NET_PROOF_MISSING", "blockers": blockers,
                "live_allowed": False, "execution_effect": "NONE"}
    # A marketable SHORT sells into bid; LONG buys at ask.
    # Adverse slippage applies beyond the observed touch price.
    touch = bid if side == "SHORT" else ask
    factor = Decimal("1") - slip / Decimal("10000") if side == "SHORT" else Decimal("1") + slip / Decimal("10000")
    return {"status": "HYPOTHETICAL_ENTRY_BOUND",
            "price": str(touch * factor), "quantity": str(qty),
            "quote_timestamp_ms": ts, "hypothetical_fill_proven": False,
            "live_allowed": False, "promotion_allowed": False,
            "execution_effect": "NONE"}
