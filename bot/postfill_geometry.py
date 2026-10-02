"""F-013 — one post-fill geometry: authoritative fill -> active exchange
protection -> risk recheck -> Position / initial risk -> durable geometry.

Stop semantics (proven intent, applied end to end):
  The strategy builds the stop as a DISTANCE from the entry (ATR x mult) and
  the historical post-fill code tried to preserve that distance locally. The
  native stop, however, is sent as an absolute level. The single policy is:

      effective stop = the MOST PROTECTIVE of
        * the technical level sent natively (never loosened — Q-01C),
        * fill -/+ planned distance (distance never grows with slippage),
        * the F-003 budget stop (loss at stop <= equity x MAX_RISK_PCT),
      quantized to the protective side of the tick.

  A tighter stop is applied ON THE EXCHANGE first (make-before-break through
  ``set_position_stops``) and the local geometry is then taken from the stop
  read back from the exchange. Local never leads the exchange. TP is the
  native level actually sent (price level); it is read back, never shifted.

States:
  CONFIRMED    fill authoritative, our stop read back, loss <= budget
  OVER_BUDGET  fill and stop real, but loss > budget and not repairable now
               (realized gap/slippage, invalid trigger, failed replacement)
  UNCONFIRMED  fill or active stop unproven: initial_sl stays None, so every
               R-based exit is fail-closed; protection is never touched.

INV-POSTFILL-GEOMETRY-001  after reconciliation Position.sl == the active
                           exchange stop (tick tolerance) and initial_sl is the
                           initial stop actually active for this trade.
INV-POSTFILL-RISK-001      CONFIRMED => qty x (|fill - stop| + fill x cost)
                           <= risk budget.
INV-FILL-AUTHORITY-001     a ticker is never a fill price.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from bot.logger import log

CONFIRMED = "CONFIRMED"
OVER_BUDGET = "OVER_BUDGET"
UNCONFIRMED = "UNCONFIRMED"


@dataclass(frozen=True)
class Fill:
    price: float
    qty: float           # base asset
    source: str


def _pos(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


# ── Fill authority ───────────────────────────────────────────────────────────
def fill_from_fills(rows, order_id, multiplier):
    """VWAP of the exact order's fills (duplicate tradeIds counted once)."""
    seen, notional, contracts = set(), Decimal(0), Decimal(0)
    for row in rows or []:
        if str(row.get("orderId")) != str(order_id):
            continue
        token = str(row.get("tradeId") or "")
        if not token or token in seen:
            continue
        price, size = _pos(row.get("price")), _pos(row.get("size"))
        if price is None or size is None:
            return None
        seen.add(token)
        notional += Decimal(str(price)) * Decimal(str(size))
        contracts += Decimal(str(size))
    if contracts <= 0:
        return None
    return Fill(float(notional / contracts), float(contracts * Decimal(str(multiplier))),
                "fills_ledger")


def fill_from_order_status(status, multiplier):
    """Exchange-computed average of a TERMINAL order (dealValue / base dealSize)."""
    if not isinstance(status, dict) or status.get("_synthetic") or status.get("isActive", False):
        return None
    contracts = _pos(status.get("dealSizeContracts"))
    if contracts is None and status.get("contractMultiplier") is None:
        contracts = _pos(status.get("dealSize"))          # raw KuCoin: contracts
    value = _pos(status.get("dealValueQuote", status.get("dealValue")))
    mult = _pos(multiplier)
    if contracts is None or value is None or mult is None:
        return None
    qty = Decimal(str(contracts)) * Decimal(str(mult))
    return Fill(float(Decimal(str(value)) / qty), float(qty), "order_status")


def fill_from_position(row, expected_qty):
    """Position average entry, valid only when the position IS this fill."""
    if not isinstance(row, dict) or expected_qty is None:
        return None
    size, entry = _pos(row.get("size")), _pos(row.get("avgPrice", row.get("entryPrice")))
    if size is None or entry is None or not math.isclose(size, expected_qty, rel_tol=1e-9):
        return None
    return Fill(entry, size, "position_avg_entry")


async def authoritative_fill(client, symbol, order_id, status, multiplier, *, position_row=None,
                             window_s=120):
    """1 fills ledger (exact orderId) > 2 terminal order average > 3 position
    average consistent with that fill > UNKNOWN. Never the ticker."""
    terminal = fill_from_order_status(status, multiplier)
    if order_id:
        try:
            from bot.accounting_fill_link import fills
            from bot.kucoin import to_kucoin
            now = int(time.time() * 1000)
            rows = await fills(client, {"symbol": to_kucoin(symbol), "openTime": now - window_s * 1000,
                                        "closeTime": now})
            found = fill_from_fills(rows, order_id, multiplier)
            if found is not None and (terminal is None
                                      or math.isclose(found.qty, terminal.qty, rel_tol=1e-9)):
                return found
            if found is not None:
                log.warning("[FILL_PRICE_AUTHORITY] symbol=%s source=fills_ledger rejected=incomplete "
                            "ledger_qty=%s order_qty=%s", symbol, found.qty, terminal.qty)
        except Exception as exc:
            log.info("[FILL_PRICE_AUTHORITY] symbol=%s source=fills_ledger unavailable=%s",
                     symbol, type(exc).__name__)
    if terminal is not None:
        return terminal
    status_qty = None
    if isinstance(status, dict):
        contracts = _pos(status.get("dealSizeContracts") or status.get("filledSize"))
        status_qty = contracts * multiplier if contracts else None
    return fill_from_position(position_row, status_qty)


# ── Geometry arithmetic ──────────────────────────────────────────────────────
def quantize_protective(price, tick, direction):
    """LONG rounds UP (tighter), SHORT rounds DOWN (tighter)."""
    tick = _pos(tick)
    if tick is None:
        return float(price)
    raw = Decimal(str(price)) / Decimal(str(tick))
    nearest = raw.to_integral_value()
    if abs(raw - nearest) < Decimal("1e-6"):      # float noise on an exact tick
        steps = nearest
    else:
        steps = raw.to_integral_value(rounding=ROUND_CEILING if direction == "LONG" else ROUND_FLOOR)
    return float(steps * Decimal(str(tick)))


def projected_loss(qty, fill, stop, cost):
    return float(Decimal(str(qty)) * (abs(Decimal(str(fill)) - Decimal(str(stop)))
                                      + Decimal(str(fill)) * Decimal(str(cost))))


def candidate_stop(direction, fill, qty, cost, budget, tick, *, planned_entry=None, planned_sl=None):
    """Most protective of technical level, preserved distance and budget stop."""
    long = direction == "LONG"
    options = []
    if _pos(planned_entry) and _pos(planned_sl):
        distance = abs(planned_entry - planned_sl)
        options += [planned_sl, fill - distance if long else fill + distance]
    if budget is not None:
        allowed = budget / qty - fill * cost
        if allowed <= 0:
            return None
        options.append(fill - allowed if long else fill + allowed)
    if not options:
        return None
    raw = max(options) if long else min(options)
    return quantize_protective(raw, tick, direction)


# ── Exchange reads ───────────────────────────────────────────────────────────
async def _exchange_view(client, symbol, direction):
    """(position_row, active_stop, active_tp, active_stop_order) from one read."""
    from bot.conditional_stop_protection import _instrument_info, _protective_order, read_stop_orders
    rows = await client.get_positions()
    row = next((r for r in rows or [] if r.get("symbol") == symbol
                and (_pos(r.get("size")) or 0) > 0), None)
    if row is None:
        return None, None, None, None
    orders = await read_stop_orders(client, symbol)
    if orders is None:
        return row, None, None, None
    info = _instrument_info(client, symbol)
    stops, tps = [], []
    inline = _pos(row.get("stopLoss"))
    if inline:
        stops.append((inline, None))
    mark = _pos(row.get("markPrice")) or _pos(row.get("avgPrice", row.get("entryPrice")))
    close_side = "sell" if direction == "LONG" else "buy"
    for order in orders:
        qualifies, full, amount = _protective_order(order, row, symbol, info)
        price = _pos(order.get("stopPrice"))
        if qualifies and price and (full or amount > 0):
            stops.append((price, order))
        elif price and mark and str(order.get("side", "")).lower() == close_side \
                and (order.get("closeOrder") is True or order.get("reduceOnly") is True) \
                and ((direction == "LONG" and price > mark) or (direction == "SHORT" and price < mark)):
            tps.append(price)
    best = (max(stops, key=lambda x: x[0]) if direction == "LONG"
            else min(stops, key=lambda x: x[0])) if stops else (None, None)
    tp = (min(tps) if direction == "LONG" else max(tps)) if tps else None
    return row, best[0], tp, best[1]


async def _is_this_trades_stop(client, symbol, direction, row, active, order, native, planned_sl, tick):
    """The level sent natively, or a BGX stop owned by THIS lineage (F-001A/Q-01C)."""
    if native is None:
        return True                     # restart: no planned level; exchange truth only
    if _same_level(active, native, tick) or _same_level(active, planned_sl, tick):
        return True
    from bot import conditional_stop_lifecycle as lifecycle
    if isinstance(order, dict) and lifecycle.is_bgx_owned(order):
        lineage = lifecycle.position_lineage(client, symbol, row)
        side = "sell" if direction == "LONG" else "buy"
        return await lifecycle.owned_for_lineage(client, symbol, side, "SL", lineage,
                                                 str(order.get("clientOid") or ""))
    return False


def _same_level(a, b, tick):
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= max((_pos(tick) or 0.0) * 1.0001, abs(float(b)) * 1e-9)


# ── Reconciliation ───────────────────────────────────────────────────────────
def mark_unconfirmed(position, *, planned_entry=None, planned_sl=None, planned_tp=None,
                     order_id="", status=None, reason="pending", proven_entry=None):
    position.initial_sl = None            # R-based exits fail closed until proven
    position._postfill_state = UNCONFIRMED
    position._postfill = {"planned_entry": planned_entry, "planned_sl": planned_sl,
                          "planned_tp": planned_tp, "order_id": str(order_id or ""),
                          "status": dict(status or {}), "proven_entry": proven_entry}
    log.warning(
        "[POSTFILL_GEOMETRY_UNCONFIRMED] symbol=%s opening_order_id=%s reason=%s initial_sl=NONE "
        "r_exits=FAIL_CLOSED protection=UNCHANGED", position.symbol, str(order_id or "")[:16] or "NONE",
        reason)


def _budget(engine, symbol):
    from bot import risk_budget
    auth = risk_budget.current_authorization()
    if auth is not None and auth.symbol == symbol:
        return float(auth.risk_budget), float(auth.cost_fraction)
    reserved = None
    position = (getattr(engine, "positions", {}) or {}).get(symbol)
    if position is not None:
        reserved = _pos(getattr(position, "_risk_reserved_usdt", None))
    if reserved is None:
        from bot.config import cfg
        snapshot = getattr(getattr(engine, "risk", None), "professional_snapshot", None)
        equity = _pos(getattr(getattr(snapshot, "capital", None), "equity", None))
        if equity is None:
            return None, risk_budget.cost_fraction(symbol)
        reserved = equity * float(cfg.MAX_RISK_PCT)
    return reserved, risk_budget.cost_fraction(symbol)


async def reconcile(engine, position):
    """Advance an UNCONFIRMED position; returns the resulting state. Never raises."""
    if getattr(position, "_postfill_state", None) != UNCONFIRMED:
        return getattr(position, "_postfill_state", None)
    symbol, direction = position.symbol, position.direction
    if symbol in (getattr(engine, "_external_position_symbols", set()) or set()):
        return UNCONFIRMED                      # NOVO-02: never reconciled as BGX
    ctx = getattr(position, "_postfill", {}) or {}
    try:
        info = (getattr(engine, "instruments", None) or {}).get(symbol) or {}
        multiplier = _pos(info.get("multiplier"))
        tick = info.get("tickSize")
        client = engine.client
        row, active, active_tp, active_order = await _exchange_view(client, symbol, direction)
        if row is None or active is None:
            return _still_unconfirmed(position, "exchange_position_or_stop_unread")
        fill = await authoritative_fill(client, symbol, ctx.get("order_id"), ctx.get("status"),
                                        multiplier or 0.0, position_row=row) if multiplier else None
        if fill is None and _pos(ctx.get("proven_entry")):
            # Restart: the restart ownership proof matched this exact entry and
            # quantity against durable lineage + exchange position + fills.
            size = _pos(row.get("size"))
            if size and _same_level(row.get("avgPrice", row.get("entryPrice")), ctx["proven_entry"], None):
                fill = Fill(float(ctx["proven_entry"]), size, "restart_ownership_proof")
        if fill is None:
            return _still_unconfirmed(position, "fill_price_unproven")
        log.info("[FILL_PRICE_AUTHORITY] symbol=%s opening_order_id=%s fill_source=%s fill_price=%.10g "
                 "qty=%.12g planned_entry=%s", symbol, ctx.get("order_id", "")[:16], fill.source,
                 fill.price, fill.qty, ctx.get("planned_entry"))
        planned_sl = ctx.get("planned_sl")
        # Our stop: the level sent natively. Anything else being the active,
        # most protective stop is foreign (stale lineage) -> not our initial stop.
        native = quantize_protective(planned_sl, tick, direction) if _pos(planned_sl) else None
        if not await _is_this_trades_stop(client, symbol, direction, row, active, active_order,
                                          native, planned_sl, tick):
            return _still_unconfirmed(position, "active_stop_not_this_trade")
        budget, cost = _budget(engine, symbol)
        if budget is None:
            return _still_unconfirmed(position, "risk_budget_unavailable")
        candidate = candidate_stop(direction, fill.price, fill.qty, cost, budget, tick,
                                   planned_entry=ctx.get("planned_entry"), planned_sl=planned_sl)
        active_before = active
        repaired = "NOOP"
        if candidate is not None:
            from bot.stop_monotonic import BETTER, FIRST_PROTECTION, decide_stop
            decision = decide_stop(direction, active, candidate, tick_size=tick)
            mark = _pos(row.get("markPrice"))
            valid_side = mark is not None and (candidate < mark if direction == "LONG" else candidate > mark)
            if decision.reason in (BETTER, FIRST_PROTECTION) and not valid_side:
                # Budget stop sits beyond the market (realized gap): still apply
                # the best VALID improvement (distance / technical), never loosen.
                fallback = candidate_stop(direction, fill.price, fill.qty, cost, None, tick,
                                          planned_entry=ctx.get("planned_entry"), planned_sl=planned_sl)
                fallback_ok = fallback is not None and mark is not None and (
                    fallback < mark if direction == "LONG" else fallback > mark)
                if fallback_ok and decide_stop(direction, active, fallback, tick_size=tick).reason in (
                        BETTER, FIRST_PROTECTION):
                    candidate, valid_side = fallback, True
                else:
                    repaired = "INVALID_TRIGGER_SIDE"
            if decision.reason in (BETTER, FIRST_PROTECTION) and valid_side:
                ok = await client.set_position_stops(symbol, sl=candidate)
                _, after, after_tp, _ = await _exchange_view(client, symbol, direction)
                if after is None:
                    return _still_unconfirmed(position, "stop_readback_after_repair_failed")
                repaired = "CONFIRMED" if ok and _same_level(after, candidate, tick) else "FAILED"
                active = after                  # exchange truth, whatever happened
                active_tp = after_tp if after_tp is not None else active_tp
        loss = projected_loss(fill.qty, fill.price, active, cost)
        state = CONFIRMED if loss <= budget * (1 + 1e-9) else OVER_BUDGET
        tp = active_tp if active_tp is not None else ctx.get("planned_tp")
        _apply(position, fill, active, tp, state)
        log.warning(
            "[POSTFILL_GEOMETRY_RECONCILE] symbol=%s opening_order_id=%s state=%s fill_source=%s "
            "fill_price=%.10g planned_entry=%s planned_sl=%s active_sl_before=%s candidate_sl=%s "
            "stop_repair=%s active_sl_after=%s initial_sl=%s tp=%s tp_source=%s qty=%.12g "
            "risk_budget=%.6f projected_loss=%.6f cost_fraction=%.5f",
            symbol, ctx.get("order_id", "")[:16], state, fill.source, fill.price,
            ctx.get("planned_entry"), planned_sl, active_before, candidate, repaired, active,
            position.initial_sl, tp, "exchange" if active_tp is not None else "planned_native_level",
            fill.qty, budget, loss, cost)
        (log.info if state == CONFIRMED else log.critical)(
            "[POSTFILL_RISK_RECHECK] symbol=%s state=%s qty=%.12g fill=%.10g stop=%.10g "
            "projected_loss=%.6f risk_budget=%.6f within_budget=%s",
            symbol, state, fill.qty, fill.price, active, loss, budget, state == CONFIRMED)
        return state
    except Exception as exc:
        return _still_unconfirmed(position, f"error_{type(exc).__name__}")


def _still_unconfirmed(position, reason):
    log.warning("[POSTFILL_GEOMETRY_UNCONFIRMED] symbol=%s reason=%s initial_sl=NONE "
                "r_exits=FAIL_CLOSED protection=UNCHANGED", position.symbol, reason)
    return UNCONFIRMED


def _apply(position, fill, stop, tp, state):
    """Local geometry := exchange truth; initial_sl written once, never again."""
    position.entry = fill.price
    position.qty = fill.qty
    position.qty_original = fill.qty
    position.sl = position.trailing_sl = stop
    position.initial_sl = stop
    if tp is not None:
        position.tp = tp
    position.peak_price = fill.price
    position.current_price = fill.price
    position._postfill_state = state
    position._postfill_confirmed = {"fill_price": fill.price, "fill_source": fill.source,
                                    "fill_qty": fill.qty, "initial_sl": stop}


RECONCILE_TIMEOUT_S = 15.0


async def reconcile_after_open(engine, position, *, fill_status, order_id, planned_entry,
                               planned_sl, planned_tp):
    """First attempt inside _open (bounded); the LIVE loop retries while UNCONFIRMED."""
    import asyncio
    mark_unconfirmed(position, planned_entry=planned_entry, planned_sl=planned_sl,
                     planned_tp=planned_tp, order_id=order_id, status=fill_status)
    try:
        return await asyncio.wait_for(reconcile(engine, position), RECONCILE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _still_unconfirmed(position, "reconcile_timeout_retry_in_loop")


async def reconcile_pending(engine):
    for symbol, position in list((getattr(engine, "positions", {}) or {}).items()):
        if getattr(position, "_postfill_state", None) == UNCONFIRMED:
            await reconcile(engine, position)
