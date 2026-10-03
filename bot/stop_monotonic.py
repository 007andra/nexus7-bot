"""Monotonic protective-stop decisions (Q-01C, INV-STOP-MONOTONIC-001).

A protective stop may only tighten: LONG stops only move up, SHORT stops only
move down, compared after tick quantization. Break-even is a floor, never an
order to overwrite a better (e.g. trailed) stop. The decision is a pure
function so every caller (break-even, trailing, stop repair) shares one rule.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Optional

from bot.logger import log

BETTER = "BETTER"
FIRST_PROTECTION = "FIRST_PROTECTION"
WORSE = "WORSE"
EQUAL_AFTER_ROUNDING = "EQUAL_AFTER_ROUNDING"
INVALID_TRIGGER_SIDE = "INVALID_TRIGGER_SIDE"
INVALID_CANDIDATE = "INVALID_CANDIDATE"
UNKNOWN_CURRENT_PROTECTION = "UNKNOWN_CURRENT_PROTECTION"


class StopDecision(NamedTuple):
    selected: Optional[float]     # stop in force after the decision
    replace: bool                 # send the candidate to the exchange
    reason: str
    normalized_candidate: Optional[float]


def _finite_positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _ticks(price, tick):
    return round(price / tick) if tick else price


def decide_stop(direction, current_sl, candidate_sl, *, tick_size=None,
                market_price=None, current_known=True) -> StopDecision:
    """Return the more protective stop and whether the candidate must be sent.

    ``current_sl=None`` (with ``current_known=True``) means no protection
    exists, so a valid candidate installs the first stop. ``current_known``
    False means the current protection cannot be proven: never assume the
    candidate is an improvement.
    """
    tick = _finite_positive(tick_size)
    candidate = _finite_positive(candidate_sl)
    current = _finite_positive(current_sl)
    if candidate is None or direction not in ("LONG", "SHORT"):
        return StopDecision(current, False, INVALID_CANDIDATE, None)
    normalized = round(_ticks(candidate, tick) * tick, 12) if tick else candidate
    price = _finite_positive(market_price)
    if price is not None and (
        (direction == "LONG" and normalized >= price)
        or (direction == "SHORT" and normalized <= price)
    ):
        return StopDecision(current, False, INVALID_TRIGGER_SIDE, normalized)
    if not current_known:
        return StopDecision(current, False, UNKNOWN_CURRENT_PROTECTION, normalized)
    if current is None:
        return StopDecision(normalized, True, FIRST_PROTECTION, normalized)
    a, b = _ticks(normalized, tick), _ticks(current, tick)
    if a == b or (not tick and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)):
        return StopDecision(current, False, EQUAL_AFTER_ROUNDING, normalized)
    better = a > b if direction == "LONG" else a < b
    if better:
        return StopDecision(normalized, True, BETTER, normalized)
    return StopDecision(current, False, WORSE, normalized)


def current_stop(position):
    """Most protective of the locally known stop fields of a Position."""
    direction = getattr(position, "direction", "")
    values = [v for v in (_finite_positive(getattr(position, "sl", None)),
                          _finite_positive(getattr(position, "trailing_sl", None))) if v]
    if not values:
        return None
    return max(values) if direction == "LONG" else min(values)


def log_decision(symbol, direction, current_sl, candidate_sl, decision, source, *,
                 quiet_skip=False):
    tag = "STOP_IMPROVEMENT_ACCEPTED" if decision.replace else "STOP_IMPROVEMENT_SKIPPED"
    emit = log.info if decision.replace else (log.debug if quiet_skip else log.warning)
    emit(
        "[%s] symbol=%s side=%s current_sl=%s candidate_sl=%s normalized_candidate=%s "
        "reason=%s source=%s",
        tag, symbol, direction, current_sl, candidate_sl, decision.normalized_candidate,
        decision.reason, source,
    )
