"""F-013 (Binance USD-M port) — one post-fill geometry.

Before this module the engine shifted the LOCAL signal by the fill delta
(entry/SL/TP += fill - planned) after Binance had already installed the native
STOP_MARKET/TAKE_PROFIT_MARKET algo orders at the PLANNED levels, and it fell
back to the cached ticker when no fill was reported. Local geometry and
exchange protection diverged: on an adverse fill the real stop distance (and
the real loss) was larger than every local R calculation believed.

Single policy (same as the approved F-013 semantics):

  fill        = exchange order average (cumQuote / executedQty). A ticker is
                never a fill (INV-FILL-AUTHORITY-001). Unknown -> nothing is
                shifted and the planned geometry is kept as the exchange truth.
  stop        = the most protective of
                  * the technical level already installed on the exchange,
                  * fill -/+ planned distance (distance never grows with slippage),
                  * the RiskManagerV3 monetary budget stop at the real fill,
                quantized to the protective side of the tick. A tighter stop is
                created ON THE EXCHANGE and read back before local adopts it
                (make-before-break: the original stop stays until replaced).
  take profit = the level installed on the exchange (never shifted).

Budget authority: ``engine.risk.validate_fresh_executable_risk`` (RiskManagerV3),
the same authority used at fresh predispatch. No second risk formula.

INV-POSTFILL-GEOMETRY-001  local SL/TP == protection actually on the exchange.
INV-POSTFILL-RISK-001      CONFIRMED => projected loss at the real fill <= V3 budget.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

CONFIRMED = "CONFIRMED"
OVER_BUDGET = "OVER_BUDGET"
UNCONFIRMED = "UNCONFIRMED"


@dataclass
class Geometry:
    state: str
    entry: float
    sl: float
    tp: float
    fill_source: str
    repaired: str = "NOOP"
    projected_loss: float = float("nan")
    risk_budget: float = float("nan")


def _pos(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def fill_price(status) -> float | None:
    """Exchange-computed average of THIS order, or None. Never the ticker."""
    if not isinstance(status, dict) or status.get("_synthetic") or status.get("_unknown"):
        return None
    if status.get("isActive", False):
        return None
    size, value = _pos(status.get("dealSize")), _pos(status.get("dealValue"))
    if size is None or value is None:
        return None
    try:
        return float(Decimal(str(value)) / Decimal(str(size)))
    except (InvalidOperation, ZeroDivisionError):
        return None


def quantize_protective(price, tick, direction):
    """LONG rounds UP (tighter), SHORT rounds DOWN (tighter); exact ticks kept."""
    tick = _pos(tick)
    if tick is None:
        return float(price)
    raw = Decimal(str(price)) / Decimal(str(tick))
    nearest = raw.to_integral_value()
    if abs(raw - nearest) < Decimal("1e-6"):
        steps = nearest
    else:
        steps = raw.to_integral_value(rounding=ROUND_CEILING if direction == "LONG" else ROUND_FLOOR)
    return float(steps * Decimal(str(tick)))


def candidate_stop(direction, fill, qty, planned_entry, planned_sl, metrics, tick):
    """Most protective of technical level, preserved distance and V3 budget stop."""
    long = direction == "LONG"
    options = [planned_sl]
    distance = abs(planned_entry - planned_sl)
    options.append(fill - distance if long else fill + distance)
    budget = _pos(metrics.get("risk_budget"))
    fee = metrics.get("fee_rate_per_side")
    slip = metrics.get("slippage_pct")
    if budget is not None and qty > 0 and fee is not None and slip is not None:
        allowed = budget / qty - fill * (2.0 * float(fee) + float(slip))
        if allowed <= 0:
            return None
        options.append(fill - allowed if long else fill + allowed)
    raw = max(options) if long else min(options)
    return quantize_protective(raw, tick, direction)


def _better(direction, new, old):
    return new > old if direction == "LONG" else new < old


async def _active_stop(client, symbol, level, tick):
    """Read back: an active BGX closePosition STOP_MARKET at ``level``."""
    try:
        orders = await client.get_stop_orders(symbol)
    except Exception:
        return False
    tolerance = (_pos(tick) or 0.0) * 0.5 + abs(level) * 1e-12
    for row in orders or []:
        if not isinstance(row, dict) or row.get("isActive") is not True:
            continue
        if str(row.get("type", "")).upper() != "STOP_MARKET" or row.get("closeOrder") is not True:
            continue
        if not str(row.get("clientOid", "")).startswith("bgx7-"):
            continue
        price = _pos(row.get("stopPrice"))
        if price is not None and abs(price - level) <= tolerance:
            return True
    return False


async def reconcile(engine, sig, qty, fill_status, log) -> Geometry:
    """Post-fill geometry for a freshly opened LIVE position. Never raises."""
    symbol, direction = sig.symbol, sig.direction
    planned_entry, planned_sl, planned_tp = float(sig.entry), float(sig.sl), float(sig.tp)
    fill = fill_price(fill_status)
    if fill is None:
        log.critical(
            "[POSTFILL_GEOMETRY_UNCONFIRMED] symbol=%s reason=fill_price_unproven "
            "ticker_used=false local_geometry=PLANNED(exchange levels) shifted=false", symbol)
        return Geometry(UNCONFIRMED, planned_entry, planned_sl, planned_tp, "none")
    info = (getattr(engine, "instruments", None) or {}).get(symbol) or {}
    tick = info.get("tickSize")
    try:
        allowed, metrics = engine.risk.validate_fresh_executable_risk(symbol, fill, float(qty))
    except Exception as exc:
        log.critical(
            "[POSTFILL_GEOMETRY_UNCONFIRMED] symbol=%s reason=risk_authority_%s fill=%.10g "
            "local_geometry=EXCHANGE_LEVELS", symbol, type(exc).__name__, fill)
        return Geometry(UNCONFIRMED, fill, planned_sl, planned_tp, "order_status")
    stop, repaired = planned_sl, "NOOP"
    candidate = candidate_stop(direction, fill, float(qty), planned_entry, planned_sl, metrics, tick)
    if candidate is not None and _better(direction, candidate, planned_sl):
        mark = None
        try:
            rows = await engine.client.get_positions()
            row = next((r for r in rows or [] if r.get("symbol") == symbol), None)
            mark = _pos((row or {}).get("markPrice"))
        except Exception:
            mark = None
        valid = mark is not None and (candidate < mark if direction == "LONG" else candidate > mark)
        if not valid:
            repaired = "INVALID_TRIGGER_SIDE"
        else:
            try:
                ok = bool(await engine.client.set_position_stops(symbol, sl=candidate))
            except Exception as exc:
                ok = False
                log.critical("[POSTFILL_GEOMETRY_RECONCILE] symbol=%s stop_repair_error=%s",
                             symbol, type(exc).__name__)
            if ok and await _active_stop(engine.client, symbol, candidate, tick):
                stop, repaired = candidate, "CONFIRMED"
            else:
                repaired = "FAILED"            # exchange truth stays the original stop
    loss_per_unit = abs(fill - stop) + fill * (
        2.0 * float(metrics.get("fee_rate_per_side", 0.0)) + float(metrics.get("slippage_pct", 0.0)))
    projected = float(qty) * loss_per_unit
    budget = float(metrics.get("risk_budget", float("nan")))
    within = math.isfinite(budget) and projected <= budget + max(1e-12, budget * 1e-6)
    state = CONFIRMED if within else OVER_BUDGET
    (log.warning if state == CONFIRMED else log.critical)(
        "[POSTFILL_GEOMETRY_RECONCILE] symbol=%s state=%s fill_source=order_status fill=%.10g "
        "planned_entry=%.10g planned_sl=%.10g candidate_sl=%s stop_repair=%s active_sl=%.10g "
        "tp=%.10g qty=%.12g projected_loss=%.6f risk_budget=%.6f ticker_used=false shifted=false",
        symbol, state, fill, planned_entry, planned_sl, candidate, repaired, stop, planned_tp,
        float(qty), projected, budget)
    return Geometry(state, fill, stop, planned_tp, "order_status", repaired, projected, budget)
