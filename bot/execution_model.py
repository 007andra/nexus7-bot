"""Venue-neutral execution-cost model for BGX research, shadow and audit paths.

Non-authorizing: it never places, changes or closes orders. Deterministic
helpers for market-fill slippage, taker fees and discrete funding
settlements, so backtests and counterfactuals do not invent continuous costs.
Moved unchanged from bot.kucoin_execution_model, including its defaults.
"""
from __future__ import annotations

import math
import os
from typing import Callable, Iterable

DEFAULT_TAKER_FEE = 0.0006
DEFAULT_SLIPPAGE = 0.0005


def _finite(value, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError, OverflowError):
        return default


def configured_taker_fee() -> float:
    rate = _finite(os.environ.get("TAKER_FEE", DEFAULT_TAKER_FEE), DEFAULT_TAKER_FEE)
    return rate if 0 <= rate < 0.02 else DEFAULT_TAKER_FEE


def slippage_rate_for_symbol(symbol: str) -> float:
    """Return the deterministic one-way adverse-slippage research assumption."""
    base = _finite(os.environ.get("BACKTEST_SLIPPAGE", DEFAULT_SLIPPAGE), DEFAULT_SLIPPAGE)
    if base < 0 or base >= 0.05:
        base = DEFAULT_SLIPPAGE
    sym = str(symbol or "").upper()
    # Preserve the historical BGX assumption: less-liquid alts use 2x base slip.
    return base if any(major in sym for major in ("BTC", "ETH", "SOL")) else base * 2.0


def adverse_fill(price: float, direction: str, *, is_entry: bool, slippage_rate: float) -> float:
    """Apply adverse market-order slippage to a price.

    LONG entry / SHORT exit are buys (price worsens upward). SHORT entry / LONG
    exit are sells (price worsens downward).
    """
    px = _finite(price)
    slip = max(0.0, _finite(slippage_rate))
    if px <= 0:
        return 0.0
    is_long = str(direction).upper() in ("LONG", "BUY")
    is_buy = is_long if is_entry else not is_long
    return px * (1.0 + slip if is_buy else 1.0 - slip)


def fee_return_fraction(entry_fill: float, exits: Iterable[tuple[float, float]], fee_rate: float) -> float:
    """Trading fees as a fraction of initial entry notional.

    Entry is assumed to be a market fill for 100% size. ``exits`` contains
    ``(fill_price, position_weight)`` pairs whose weights should sum to one.
    """
    entry = _finite(entry_fill)
    fee = max(0.0, _finite(fee_rate))
    if entry <= 0:
        return 0.0
    total = fee  # entry leg, full size
    for exit_price, weight in exits:
        px = _finite(exit_price)
        w = max(0.0, _finite(weight))
        if px > 0 and w > 0:
            total += fee * (px / entry) * w
    return total


def funding_return_fraction(
    events: list[dict],
    direction: str,
    entry_ts_ms: int,
    exit_ts_ms: int,
    entry_fill: float,
    *,
    price_at_ts: Callable[[int], float] | None = None,
    partial_after_ts_ms: int | None = None,
) -> tuple[float, int]:
    """Return signed funding PnL as a fraction of initial entry notional.

    Funding is charged only at actual KuCoin settlement ``timepoint`` values.
    Positive rates mean LONG pays / SHORT receives; negative rates reverse that.
    A 50% partial exit can be represented with ``partial_after_ts_ms``.
    ``price_at_ts`` may supply a historical mark-price proxy; otherwise entry
    price is used, producing a rate-only approximation.
    """
    entry = _finite(entry_fill)
    if entry <= 0 or exit_ts_ms <= entry_ts_ms:
        return 0.0, 0
    side_sign = -1.0 if str(direction).upper() in ("LONG", "BUY") else 1.0
    total = 0.0
    count = 0
    for event in events or []:
        try:
            tp = int(event.get("timepoint", event.get("ts", 0)) or 0)
            rate = _finite(event.get("fundingRate"))
        except (AttributeError, TypeError, ValueError):
            continue
        if not (entry_ts_ms < tp <= exit_ts_ms):
            continue
        mark = _finite(price_at_ts(tp), entry) if price_at_ts else entry
        if mark <= 0:
            mark = entry
        position_weight = 0.5 if partial_after_ts_ms is not None and tp > partial_after_ts_ms else 1.0
        total += side_sign * rate * (mark / entry) * position_weight
        count += 1
    return total, count


def estimated_round_trip_cost_pct(symbol: str, taker_fee_rate: float | None = None) -> float:
    """Approximate full-size market round trip in percentage points."""
    fee = configured_taker_fee() if taker_fee_rate is None else max(0.0, _finite(taker_fee_rate))
    slip = slippage_rate_for_symbol(symbol)
    return (2.0 * fee + 2.0 * slip) * 100.0
