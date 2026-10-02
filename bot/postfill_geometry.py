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

F-013A — late entry fills (same opening order, after the first confirmation):
INV-POSTFILL-RISK-CONTINUOUS-001  an exposure increase is re-proven (cumulative
                           VWAP from the exact order's ledger/status, active
                           stop, budget) before the position stays CONFIRMED.
INV-GEOMETRY-VERSION-001   a confirmation is valid for exactly one
                           (entry_qty, VWAP, active stop) version; a
                           non-terminal entry order keeps being rechecked
                           every LIVE cycle (WS is only a trigger).
INV-LATE-FILL-BUDGET-001   CONFIRMED => projected_loss(current qty, current
                           VWAP, active stop, cost) <= original F-003 budget.
An increase NOT explained by the same order's fills (manual/external) is never
absorbed as BGX exposure; quantity is never increased or auto-reduced here.
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


def fill_from_order_status(status, multiplier, *, allow_active=False):
    """Exchange-computed average (dealValue / base dealSize) of a TERMINAL order,
    or — with ``allow_active`` — the cumulative fill so far of a still-active
    order (F-013A: geometry is then confirmed only for that filled quantity)."""
    if not isinstance(status, dict) or status.get("_synthetic"):
        return None
    if status.get("isActive", False) and not allow_active:
        return None
    contracts = _pos(status.get("dealSizeContracts"))
    if contracts is None and status.get("contractMultiplier") is None:
        contracts = _pos(status.get("dealSize"))          # raw KuCoin: contracts
    value = _pos(status.get("dealValueQuote", status.get("dealValue")))
    mult = _pos(multiplier)
    if contracts is None or value is None or mult is None:
        return None
    qty = Decimal(str(contracts)) * Decimal(str(mult))
    source = "order_status_partial" if status.get("isActive", False) else "order_status"
    return Fill(float(Decimal(str(value)) / qty), float(qty), source)


def fill_from_position(row, expected_qty):
    """Position average entry, valid only when the position IS this fill."""
    if not isinstance(row, dict) or expected_qty is None:
        return None
    size, entry = _pos(row.get("size")), _pos(row.get("avgPrice", row.get("entryPrice")))
    if size is None or entry is None or not math.isclose(size, expected_qty, rel_tol=1e-9):
        return None
    return Fill(entry, size, "position_avg_entry")


async def authoritative_fill(client, symbol, order_id, status, multiplier, *, position_row=None,
                             window_s=120, allow_active=False, opened_ms=None):
    """1 fills ledger (exact orderId, unique tradeIds) > 2 order average >
    3 position average consistent with that fill > UNKNOWN. Never the ticker."""
    terminal = fill_from_order_status(status, multiplier, allow_active=allow_active)
    if order_id:
        try:
            from bot.accounting_fill_link import fills
            from bot.kucoin import to_kucoin
            now = int(time.time() * 1000)
            start = int(opened_ms) - window_s * 1000 if opened_ms else now - window_s * 1000
            rows = await fills(client, {"symbol": to_kucoin(symbol), "openTime": start,
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


def _same_level(a, b, tick):
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= max((_pos(tick) or 0.0) * 1.0001, abs(float(b)) * 1e-9)


# ── Reconciliation ───────────────────────────────────────────────────────────
def mark_unconfirmed(position, *, planned_entry=None, planned_sl=None, planned_tp=None,
                     order_id="", status=None, reason="pending", proven_entry=None, opened_ms=None,
                     reduced_qty=None, client_oid=""):
    position.initial_sl = None            # R-based exits fail closed until proven
    position._postfill_state = UNCONFIRMED
    position._postfill_version = None
    position._postfill = {"planned_entry": planned_entry, "planned_sl": planned_sl,
                          "planned_tp": planned_tp, "order_id": str(order_id or ""),
                          "client_oid": str(client_oid or ""),
                          "status": dict(status or {}), "proven_entry": proven_entry,
                          "opened_ms": int(opened_ms or time.time() * 1000),
                          # restart: BGX-proven reductions already applied to this entry
                          "reduced_qty": reduced_qty}
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


def geometry_lock(position):
    """Per-position lock shared by the post-fill recheck and the trailing stop."""
    return _lock(position)


def _lock(position):
    import asyncio
    lock = getattr(position, "_postfill_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = position._postfill_lock = asyncio.Lock()
    return lock


async def _entry_fill(client, symbol, ctx, multiplier, row):
    """Cumulative fill of THIS opening order, read fresh: (Fill, terminal) or (None, False).

    The fresh order status (cumulative dealValue/dealSize) and the fills
    ledger of the exact orderId are authoritative; a still-active order yields
    a fill valid only for the quantity filled so far (terminal=False)."""
    order_id = ctx.get("order_id")
    status = ctx.get("status") or {}
    getter = getattr(client, "get_order_status", None)
    if order_id and callable(getter):
        try:
            fresh = await getter(order_id)
            if isinstance(fresh, dict) and fresh and not fresh.get("_synthetic") \
                    and not fresh.get("_unknown"):
                status = fresh
        except Exception:
            pass
    terminal = isinstance(status, dict) and status.get("isActive", None) is False
    fill = await authoritative_fill(client, symbol, order_id, status, multiplier, position_row=row,
                                    allow_active=True, opened_ms=ctx.get("opened_ms"))
    if fill is None and _pos(ctx.get("proven_entry")):
        # Restart: the restart ownership proof matched this exact entry and
        # quantity against durable lineage + exchange position + fills.
        size = _pos(row.get("size"))
        if size and _same_level(row.get("avgPrice", row.get("entryPrice")), ctx["proven_entry"], None):
            fill, terminal = Fill(float(ctx["proven_entry"]), size, "restart_ownership_proof"), True
    return fill, terminal, status


async def _validate(engine, position, stage):
    """One coherent pass: fill state -> exposure explanation -> active stop ->
    risk -> repair (make-before-break) -> read-back -> versioned geometry."""
    symbol, direction = position.symbol, position.direction
    ctx = getattr(position, "_postfill", {}) or {}
    version = getattr(position, "_postfill_version", None)
    info = (getattr(engine, "instruments", None) or {}).get(symbol) or {}
    multiplier = _pos(info.get("multiplier"))
    tick = info.get("tickSize")
    client = engine.client
    if multiplier is None:
        return _invalidate(position, "multiplier_unavailable")
    rows = await client.get_positions()
    row = next((r for r in rows or [] if r.get("symbol") == symbol
                and (_pos(r.get("size")) or 0) > 0), None)
    if row is None:
        return _invalidate(position, "exchange_position_unread")
    fill, terminal, status = await _entry_fill(client, symbol, ctx, multiplier, row)
    if fill is None:
        return _invalidate(position, "fill_price_unproven")
    exposure = _pos(row.get("size")) or 0.0
    # INV-POSTFILL-RISK-CONTINUOUS-001: exposure must be explained by THIS
    # opening order's cumulative fills minus BGX reductions since confirmation.
    # A decrease (BGX partial exit / stop) only lowers risk; an INCREASE must be
    # explained by more fills of the same order — never by a manual/external add.
    if version:
        reduced = max(0.0, float(version["entry_qty"]) - float(position.qty))
    else:
        reduced = float(ctx.get("reduced_qty") or 0.0)
    expected = fill.qty - reduced
    if exposure > expected * (1 + 1e-9) + 1e-12:
        return _invalidate(position, "position_qty_not_explained_by_entry_fills",
                           exposure=exposure, expected=expected)
    late = (version is not None and fill.qty > float(version["entry_qty"]) * (1 + 1e-9)) \
        or ctx.get("reduced_qty") is not None
    log.info("[FILL_PRICE_AUTHORITY] symbol=%s opening_order_id=%s stage=%s fill_source=%s "
             "fill_price=%.10g entry_qty=%.12g terminal=%s late_fill=%s", symbol,
             ctx.get("order_id", "")[:16], stage, fill.source, fill.price, fill.qty, terminal, late)
    # NOVO-F013A-1: the ACTIVE stop/TP are only those of THIS opening lineage
    # (native st-orders legs or BGX stops owned for the lineage). Foreign,
    # stale or fallback-estimated levels never become the trade geometry.
    protection = await _lineage_protection(client, symbol, direction, ctx, status, tick, row)
    if not protection.readable:
        return _invalidate(position, "exchange_protection_unread")
    if protection.sl is None:
        return _invalidate(position, "no_active_stop_of_this_lineage")
    if protection.other_lineage_tighter is not None:
        log.critical("[PROTECTION_LINEAGE_REJECTED] symbol=%s current_opening_order_id=%s "
                     "other_lineage_bgx_stop=%s lineage_stop=%s action=preserved_not_authority "
                     "geometry=UNCONFIRMED", symbol, ctx.get("order_id", "")[:16],
                     protection.other_lineage_tighter, protection.sl)
    if protection.foreign_tighter is not None:
        return _invalidate(position, "foreign_tighter_stop_present",
                           foreign_stop=protection.foreign_tighter, lineage_stop=protection.sl)
    if not _pos(ctx.get("planned_sl")) and protection.native_sl:
        ctx["planned_sl"] = protection.native_sl        # restart: the order's own trigger
    if not _pos(ctx.get("planned_tp")) and protection.native_tp:
        ctx["planned_tp"] = protection.native_tp
    planned_sl = ctx.get("planned_sl")
    active, active_tp = protection.sl, protection.tp
    budget, cost = _budget(engine, symbol)
    if budget is None:
        return _invalidate(position, "risk_budget_unavailable")
    candidate = candidate_stop(direction, fill.price, exposure, cost, budget, tick,
                               planned_entry=ctx.get("planned_entry"), planned_sl=planned_sl)
    active_before, repaired = active, "NOOP"
    if candidate is not None:
        from bot.stop_monotonic import BETTER, FIRST_PROTECTION, decide_stop
        decision = decide_stop(direction, active, candidate, tick_size=tick)
        mark = _pos(row.get("markPrice"))
        valid_side = mark is not None and (candidate < mark if direction == "LONG" else candidate > mark)
        if decision.reason in (BETTER, FIRST_PROTECTION) and not valid_side:
            # Budget stop beyond the market (realized gap): best VALID improvement only.
            fallback = candidate_stop(direction, fill.price, exposure, cost, None, tick,
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
            after = await _lineage_protection(client, symbol, direction, ctx, status, tick, None)
            if not after.readable or after.sl is None:
                return _invalidate(position, "stop_readback_after_repair_failed")
            repaired = "CONFIRMED" if ok and _same_level(after.sl, candidate, tick) else "FAILED"
            active = after.sl                  # exchange truth, whatever happened
            active_tp = after.tp if after.tp is not None else active_tp
    loss = projected_loss(exposure, fill.price, active, cost)
    state = CONFIRMED if loss <= budget * (1 + 1e-9) else OVER_BUDGET
    tp = active_tp if active_tp is not None else ctx.get("planned_tp")
    if active_tp is None:
        # Never invented: the lineage TP is not on the exchange (local target =
        # the order's own planned/echoed TP when known, else unchanged).
        log.critical("[NATIVE_PROTECTION_RECOVERY] symbol=%s opening_order_id=%s tp=MISSING_ON_EXCHANGE "
                     "local_tp_source=%s", symbol, ctx.get("order_id", "")[:16],
                     "lineage_planned" if tp is not None else "unchanged_unproven")
    _apply(position, fill, exposure, active, tp, state, terminal,
           new_initial=(version is None or repaired == "CONFIRMED"))
    if late:
        await _record_late_fill(position, fill.qty)
    log.warning(
        "[POSTFILL_GEOMETRY_RECONCILE] symbol=%s opening_order_id=%s stage=%s state=%s fill_source=%s "
        "fill_price=%.10g planned_entry=%s planned_sl=%s active_sl_before=%s candidate_sl=%s "
        "stop_repair=%s active_sl_after=%s initial_sl=%s tp=%s qty=%.12g entry_qty=%.12g "
        "entry_terminal=%s late_fill=%s risk_budget=%.6f projected_loss=%.6f cost_fraction=%.5f "
        "geometry_version=%s",
        symbol, ctx.get("order_id", "")[:16], stage, state, fill.source, fill.price,
        ctx.get("planned_entry"), planned_sl, active_before, candidate, repaired, active,
        position.initial_sl, tp, exposure, fill.qty, terminal, late, budget, loss, cost,
        position._postfill_version["n"])
    (log.info if state == CONFIRMED else log.critical)(
        "[POSTFILL_RISK_RECHECK] symbol=%s stage=%s state=%s qty=%.12g vwap=%.10g stop=%.10g "
        "projected_loss=%.6f risk_budget=%.6f within_budget=%s",
        symbol, stage, state, exposure, fill.price, active, loss, budget, state == CONFIRMED)
    return state


async def _lineage_protection(client, symbol, direction, ctx, status, tick, row):
    from bot import conditional_stop_lifecycle as lifecycle
    from bot import native_protection
    if row is None:
        rows = await client.get_positions()
        row = next((r for r in rows or [] if r.get("symbol") == symbol
                    and (_pos(r.get("size")) or 0) > 0), None)
    side = "sell" if direction == "LONG" else "buy"
    # Strong lineage of THIS trade: its opening order (NOVO-02 identity).
    lineage = lifecycle.STRONG_PREFIX + str(ctx.get("order_id")) if ctx.get("order_id") else ""

    async def owned(order, kind):
        return await lifecycle.owned_for_strong_lineage(
            client, symbol, side, kind, lineage, str(order.get("clientOid") or ""),
            order_id=str(order.get("id") or order.get("orderId") or ""))
    return await native_protection.discover(
        client, symbol, direction, order_id=ctx.get("order_id", ""),
        client_oid=ctx.get("client_oid", ""), status=status, planned_sl=ctx.get("planned_sl"),
        planned_tp=ctx.get("planned_tp"), tick=tick, row=row, lineage_owned=owned)


async def _record_late_fill(position, entry_qty):
    try:
        from bot import exit_geometry_durability, trade_lifecycle
        oid = trade_lifecycle.lineage_id(position)
        if oid:
            await trade_lifecycle.record_entry_fill(oid, entry_qty, "late_entry_fill")
        await exit_geometry_durability.persist(position, "late_entry_fill")
    except Exception as exc:
        log.error("[POSTFILL_GEOMETRY_RECONCILE] symbol=%s late_fill_persist_failed=%s:%s",
                  position.symbol, type(exc).__name__, exc)


def _invalidate(position, reason, **fields):
    """No proof -> geometry is not confirmed (R exits fail closed); protection untouched."""
    position._postfill_state = UNCONFIRMED
    position.initial_sl = None
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    log.warning("[POSTFILL_GEOMETRY_UNCONFIRMED] symbol=%s reason=%s initial_sl=NONE "
                "r_exits=FAIL_CLOSED protection=UNCHANGED %s", position.symbol, reason, extra)
    return UNCONFIRMED


def _still_unconfirmed(position, reason):
    return _invalidate(position, reason)


def _apply(position, fill, exposure, stop, tp, state, terminal, *, new_initial):
    """Local geometry := exchange truth for one concrete version (qty, VWAP, stop)."""
    first = getattr(position, "_postfill_version", None) is None
    position.entry = fill.price
    position.qty = exposure
    position.qty_original = fill.qty
    position.sl = position.trailing_sl = stop
    if new_initial or position.initial_sl is None:
        position.initial_sl = stop
    if tp is not None:
        position.tp = tp
    if first and (getattr(position, "_postfill", {}) or {}).get("reduced_qty") is None:
        position.peak_price = fill.price
        position.current_price = fill.price
    else:
        # The favorable extreme is never behind the (new) average entry.
        peak = _pos(getattr(position, "peak_price", None)) or fill.price
        position.peak_price = max(peak, fill.price) if position.direction == "LONG" \
            else min(peak, fill.price)
    previous = getattr(position, "_postfill_version", None) or {}
    position._postfill_state = state
    # INV-GEOMETRY-VERSION-001: confirmation is valid for exactly this tuple.
    position._postfill_version = {"entry_qty": fill.qty, "vwap": fill.price, "stop": stop,
                                  "exposure": exposure, "terminal": bool(terminal),
                                  "n": int(previous.get("n", 0)) + 1}
    position._postfill_confirmed = {"fill_price": fill.price, "fill_source": fill.source,
                                    "fill_qty": fill.qty, "initial_sl": position.initial_sl}


async def reconcile(engine, position):
    """Confirm an UNCONFIRMED position; never raises."""
    if getattr(position, "_postfill_state", None) != UNCONFIRMED:
        return getattr(position, "_postfill_state", None)
    return await _guarded(engine, position, "initial")


async def revalidate(engine, position):
    """Re-prove a confirmed geometry (late entry fills / non-terminal entry)."""
    if getattr(position, "_postfill_version", None) is None:
        return await reconcile(engine, position)
    return await _guarded(engine, position, "revalidation")


async def _guarded(engine, position, stage):
    if position.symbol in (getattr(engine, "_external_position_symbols", set()) or set()):
        return getattr(position, "_postfill_state", UNCONFIRMED)   # NOVO-02: never BGX-reconciled
    async with _lock(position):
        try:
            position._postfill_dirty = False
            return await _validate(engine, position, stage)
        except Exception as exc:
            return _invalidate(position, f"error_{type(exc).__name__}")


async def late_fill_explains(engine, symbol, exchange_qty):
    """Pilot ownership guard hook: an exposure increase is BGX only when the
    SAME opening order's cumulative fills explain it (re-proven right now)."""
    position = (getattr(engine, "positions", {}) or {}).get(symbol)
    if position is None or not (getattr(position, "_postfill", {}) or {}).get("order_id"):
        return False
    state = await _guarded(engine, position, "ownership_guard")
    return state != UNCONFIRMED and math.isclose(float(position.qty), float(exchange_qty),
                                                 rel_tol=1e-9, abs_tol=1e-12)


async def on_exposure_increase(engine, position, exchange_qty):
    """Periodic reconcile hook (EXEC-03): an exposure increase invalidates the
    confirmed geometry until the same order's cumulative fills re-prove it.
    Positions without an opening-order context (orphans, legacy) only lose
    their R geometry: the increase is never confirmed without fill proof."""
    if not (getattr(position, "_postfill", {}) or {}).get("order_id"):
        return _invalidate(position, "exposure_increase_without_entry_lineage",
                           local_qty=getattr(position, "qty", None), exchange_qty=exchange_qty)
    state = await _guarded(engine, position, "exposure_increase")
    if state != UNCONFIRMED and not math.isclose(float(position.qty), float(exchange_qty),
                                                 rel_tol=1e-9, abs_tol=1e-12):
        return _invalidate(position, "exposure_changed_during_recheck",
                           local_qty=position.qty, exchange_qty=exchange_qty)
    return state


async def adopt_after_timeout(engine, position, *, fill_status, order_id, planned_entry,
                              planned_sl, planned_tp, client_oid=""):
    """A fill-timeout adoption of THIS order's position enters the pipeline
    (INV-TIMEOUT-GEOMETRY-001: same pipeline, same geometry as no timeout)."""
    if not order_id or position.symbol in (getattr(engine, "_external_position_symbols", set()) or set()):
        return getattr(position, "_postfill_state", None)
    return await reconcile_after_open(engine, position, fill_status=fill_status, order_id=order_id,
                                      planned_entry=planned_entry, planned_sl=planned_sl,
                                      planned_tp=planned_tp, client_oid=client_oid)


def request_revalidation(engine, order_id):
    """WS trigger: schedule a recheck of the position opened by ``order_id``.
    Detection never depends on it (the periodic pass re-reads REST truth)."""
    import asyncio
    for position in list((getattr(engine, "positions", {}) or {}).values()):
        ctx = getattr(position, "_postfill", {}) or {}
        if order_id and ctx.get("order_id") == str(order_id):
            position._postfill_dirty = True
            try:
                return asyncio.get_running_loop().create_task(revalidate(engine, position))
            except RuntimeError:
                return None
    return None


RECONCILE_TIMEOUT_S = 15.0


async def reconcile_after_open(engine, position, *, fill_status, order_id, planned_entry,
                               planned_sl, planned_tp, client_oid=""):
    """First attempt inside _open (bounded); the LIVE loop retries while UNCONFIRMED."""
    import asyncio
    mark_unconfirmed(position, planned_entry=planned_entry, planned_sl=planned_sl,
                     planned_tp=planned_tp, order_id=order_id, status=fill_status,
                     client_oid=client_oid)
    try:
        return await asyncio.wait_for(reconcile(engine, position), RECONCILE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _still_unconfirmed(position, "reconcile_timeout_retry_in_loop")


async def reconcile_pending(engine):
    """Periodic (every LIVE management cycle; independent of private WS)."""
    for symbol, position in list((getattr(engine, "positions", {}) or {}).items()):
        state = getattr(position, "_postfill_state", None)
        version = getattr(position, "_postfill_version", None)
        if state == UNCONFIRMED:
            await reconcile(engine, position)
        elif state in (CONFIRMED, OVER_BUDGET) and version is not None and (
                not version["terminal"] or getattr(position, "_postfill_dirty", False)):
            await revalidate(engine, position)
