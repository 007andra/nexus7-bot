"""NOVO-F013A-1 (Binance USD-M port) — orphan adoption never invents a geometry.

``_reconcile_exchange_positions`` adopts an exchange position that is not
tracked locally. Binance ``positionRisk`` rows carry no ``stopLoss`` field, so
the legacy loader ALWAYS concluded "no stop" and sent an ATR/liquidation
estimate (SL entry -/+ 1.05xATR and TP entry +/- 2.1xATR) to the exchange,
even when the native STOP_MARKET/TAKE_PROFIT_MARKET algo orders of the very
order that had just timed out were already active. A timeout became a
strategy event (R 2.0 -> 1.05, target 4R -> 2.1R).

Policy:
  * Timeout adoption of a KNOWN opening order: its own planned SL/TP are the
    geometry (INV-TIMEOUT-GEOMETRY-001). Active protection at exactly those
    levels is reused; a missing level is restored at the ORIGINAL trigger only
    (never an estimate). If the original SL cannot be placed (market beyond
    it) the position stays unprotected/blocked — no estimate is sent.
  * Unknown origin: an already active conditional stop is never replaced by an
    estimate (INV-NO-FALLBACK-STRATEGY-001). Only a position with NO
    protection at all keeps the legacy safety stop.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _same(a, b, tick):
    try:
        tolerance = Decimal(str(tick or 0)) / 2
        return abs(Decimal(str(a)) - Decimal(str(b))) <= tolerance
    except (InvalidOperation, TypeError):
        return False


def _close_side(direction):
    return "sell" if direction == "LONG" else "buy"


def active_protection(orders, direction):
    """(stop_levels, tp_levels) of active closePosition algo orders on the close side."""
    stops, tps = [], []
    for row in orders or []:
        if not isinstance(row, dict) or row.get("isActive") is not True:
            continue
        if row.get("closeOrder") is not True and row.get("reduceOnly") is not True:
            continue
        if str(row.get("side", "")).lower() != _close_side(direction):
            continue
        price = _num(row.get("stopPrice"))
        kind = str(row.get("type", "")).upper()
        if price is None:
            continue
        if kind == "STOP_MARKET":
            stops.append(price)
        elif kind == "TAKE_PROFIT_MARKET":
            tps.append(price)
    return stops, tps


def most_protective(stops, direction):
    if not stops:
        return None
    return max(stops) if direction == "LONG" else min(stops)


async def read_orders(client, symbol):
    try:
        return await client.get_stop_orders(symbol)
    except Exception:
        return None


async def adopt_known_lineage(client, symbol, direction, row, ctx, tick, log):
    """Return (sl, tp, protected) for a timeout adoption of THIS opening order."""
    planned_sl, planned_tp = _num(ctx.get("planned_sl")), _num(ctx.get("planned_tp"))
    orders = await read_orders(client, symbol)
    if orders is None or planned_sl is None or planned_tp is None:
        log.critical("[TIMEOUT_ADOPTION] symbol=%s opening_order_id=%s result=UNPROTECTED "
                     "reason=%s fallback_estimate=NOT_USED", symbol, str(ctx.get("order_id", ""))[:16],
                     "stop_orders_unreadable" if orders is None else "planned_levels_missing")
        return planned_sl, planned_tp, False
    stops, tps = active_protection(orders, direction)
    has_sl = any(_same(level, planned_sl, tick) for level in stops)
    has_tp = any(_same(level, planned_tp, tick) for level in tps)
    if not (has_sl and has_tp):
        mark = _num(row.get("markPrice")) or _num(row.get("entryPrice"))
        sl_valid = mark is not None and (planned_sl < mark if direction == "LONG" else planned_sl > mark)
        restore_sl = planned_sl if not has_sl and sl_valid else 0
        restore_tp = planned_tp if not has_tp else 0
        ok = False
        if restore_sl or restore_tp:
            try:
                ok = bool(await client.set_position_stops(symbol, sl=restore_sl, tp=restore_tp))
            except Exception as exc:
                log.critical("[TIMEOUT_ADOPTION] symbol=%s restore_error=%s", symbol, type(exc).__name__)
        orders = await read_orders(client, symbol) if ok else orders
        stops, tps = active_protection(orders or [], direction)
        has_sl = any(_same(level, planned_sl, tick) for level in stops)
        log.critical("[TIMEOUT_ADOPTION] symbol=%s opening_order_id=%s original_levels_restored=%s "
                     "sl_trigger_valid=%s fallback_estimate=NOT_USED", symbol,
                     str(ctx.get("order_id", ""))[:16], ok, sl_valid)
    # Local geometry = the lineage's own levels. A tighter active stop that is
    # not this lineage's level is never adopted as geometry (Binance algo stops
    # carry no lineage); it is reported because it would exit first.
    current = most_protective(stops, direction)
    sl = planned_sl
    if current is not None and not _same(current, planned_sl, tick) and (
            current > planned_sl if direction == "LONG" else current < planned_sl):
        log.critical("[TIMEOUT_ADOPTION] symbol=%s tighter_unattributed_stop=%s lineage_sl=%s "
                     "geometry_source=LINEAGE action=preserved_not_adopted", symbol, current, planned_sl)
    protected = has_sl
    log.warning("[TIMEOUT_ADOPTION] symbol=%s opening_order_id=%s direction=%s planned_sl=%s "
                "planned_tp=%s active_stops=%s active_tps=%s local_sl=%s local_tp=%s protected=%s "
                "fallback_estimate=NOT_USED", symbol, str(ctx.get("order_id", ""))[:16], direction,
                planned_sl, planned_tp, stops, tps, sl, planned_tp, protected)
    return sl, planned_tp, protected


async def existing_conditional_stop(client, symbol, direction):
    """Most protective active conditional stop of an unknown-origin position, or None."""
    orders = await read_orders(client, symbol)
    if orders is None:
        return None
    stops, _ = active_protection(orders, direction)
    return most_protective(stops, direction)
