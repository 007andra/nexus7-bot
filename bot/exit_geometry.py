"""Exit geometry in PRICE / R units, independent of remaining quantity (Q-01).

A partial exit reduces size; it does not change the path price travelled.
  * favorable excursion = peak_price - entry (LONG) / entry - trough (SHORT),
    tracked by ``Position.update_pnl`` as ``peak_price`` (best price seen);
  * initial risk per unit = |entry - initial_sl|, fixed at entry
    (INV-INITIAL-RISK-001); the current stop (BE, trailing) is never the
    denominator of 1R/2R;
  * R multiple = excursion / initial risk (INV-EXIT-RMULT-001).

Positions rebuilt from exchange rows (restart/orphan/external sync) carry
``initial_sl = None``: their real initial risk is unknown, so discretionary
R exits fail closed while existing protection and trailing stay in place.
"""
from __future__ import annotations

import math

from bot.logger import log


def _finite_positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def initial_risk_per_unit(position):
    """|entry - initial_sl|, or None when the initial stop is unknown/invalid."""
    entry = _finite_positive(getattr(position, "entry", None))
    stop = _finite_positive(getattr(position, "initial_sl", None))
    if entry is None or stop is None:
        return None
    direction = getattr(position, "direction", "")
    if direction == "LONG" and stop < entry:
        return entry - stop
    if direction == "SHORT" and stop > entry:
        return stop - entry
    return None


def favorable_move(position, price):
    entry = float(position.entry)
    return price - entry if position.direction == "LONG" else entry - price


def peak_excursion(position):
    """Best favorable price excursion of the trade (price units, >= 0).

    Legacy snapshots without ``peak_price`` fall back to
    ``peak_pnl / qty_original``: peak_pnl was accumulated on a quantity never
    above qty_original, so this never overstates the excursion.
    """
    peak = _finite_positive(getattr(position, "peak_price", None))
    if peak is not None:
        return max(0.0, favorable_move(position, peak))
    size = _finite_positive(getattr(position, "qty_original", None)) or \
        _finite_positive(getattr(position, "qty", None))
    peak_pnl = float(getattr(position, "peak_pnl", 0.0) or 0.0)
    if size is None or not math.isfinite(peak_pnl):
        return 0.0
    return max(0.0, peak_pnl / size)


def r_multiple(position, price):
    risk = initial_risk_per_unit(position)
    if risk is None:
        return None
    return favorable_move(position, float(price)) / risk


def stop_on_valid_side(direction, stop, price):
    """INV-TRAILING-VALIDITY-001: LONG stop below price, SHORT stop above."""
    if stop is None or not math.isfinite(stop) or not math.isfinite(price) or price <= 0:
        return False
    return stop < price if direction == "LONG" else stop > price


def log_geometry(position, tag, **extra):
    price = float(getattr(position, "current_price", 0.0) or 0.0)
    log.info(
        "[%s] symbol=%s entry=%s initial_sl=%s initial_risk=%s remaining_qty=%s "
        "peak_price=%s r_multiple=%s current_price=%s %s",
        tag, getattr(position, "symbol", "?"), getattr(position, "entry", "?"),
        getattr(position, "initial_sl", None), initial_risk_per_unit(position),
        getattr(position, "qty", "?"), getattr(position, "peak_price", None),
        r_multiple(position, price) if price > 0 else None, price,
        " ".join(f"{k}={v}" for k, v in extra.items()),
    )
