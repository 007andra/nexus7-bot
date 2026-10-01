"""Recovery of BGX entry exposure whose local Position was never created.

Incident class (audit F-01): a LIVE MARKET entry is accepted and filled by
Binance, but the POST response is ambiguous (timeout / HTTP 5xx / -1007) and
the immediate ``newClientOrderId`` lookup is inconclusive. ``engine._open``
then persists ``ambiguous_dispatch`` and returns without a local Position, so
the post-open protection wrapper and the periodic naked-position guard both
skip the symbol and the exposure stays without a stop.

This module is the single authority that closes that gap. It never submits an
opening order and never adopts exposure by symbol similarity. Each durable BGX
entry intent ends in exactly one of:

* ``NOT_ACCEPTED``      exchange proves no execution (REJECTED/FAILED/CANCELLED
                        with zero fill); nothing to adopt;
* ``SUBMIT_UNKNOWN``    no exchange truth yet (RECONCILE_REQUIRED); the intent
                        stays pending, ``durable_execution`` keeps new entries
                        blocked, no second dispatch can happen;
* ``ACCEPTED_OPEN``     acknowledged, no fill yet; stays pending;
* ``ADOPTED_PROTECTED`` / ``ADOPTED_UNPROTECTED``
                        BGX fill (full or partial) proven with strong lineage,
                        local Position created from the durable protective plan
                        and the existing Binance protection enforcement run;
* ``LINEAGE_REJECTED``  a fill exists but exchange exposure does not match the
                        intent exactly; the symbol stays read-only and blocked.

Exchange order truth comes from the shared ``apply_exchange_order_truth``
(also used by continuous and startup reconciliation); protection is installed
or confirmed only through ``binance_protection_failclosed``'s enforcement, so
the existing repair / confirmed-close policy applies unchanged.
"""
from __future__ import annotations

import asyncio
import math

from bot import durable_execution as durable
from bot.logger import log
from bot.order_state import OrderState


BGX_PREFIX = "bgx7-"
SOURCE = "AMBIGUOUS_ENTRY_RECOVERY"
PENDING_ATTR = "_ambiguous_recovery_symbols"
# Dedicated durable block reason: ``persist_orders`` clears the generic
# "orders" reason on every successful write, which must never release an
# unresolved ambiguous entry. Only this module clears this reason, and only
# after a full scan proves every BGX entry intent resolved or materialized.
BLOCK_REASON = "ambiguous_entry"
_ALERTS_ATTR = "_ambiguous_entry_alerts"

NOT_ACCEPTED = "NOT_ACCEPTED"
SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
ACCEPTED_OPEN = "ACCEPTED_OPEN"
ADOPTED_PROTECTED = "ADOPTED_PROTECTED"
ADOPTED_UNPROTECTED = "ADOPTED_UNPROTECTED"
LINEAGE_REJECTED = "LINEAGE_REJECTED"
DEFERRED = "DEFERRED"


def _enabled(engine) -> bool:
    if getattr(engine, "paper_trade", False):
        return False
    if getattr(engine, "_validation_safety_lock_active", False):
        return False
    pilot = getattr(engine, "pilot", None)
    return bool(getattr(pilot, "enabled", False))


def _side_of(raw) -> str:
    value = str(raw or "").strip().lower()
    if value in {"buy", "long"}:
        return "Buy"
    if value in {"sell", "short"}:
        return "Sell"
    return ""


def _filled(order) -> float:
    try:
        value = float(getattr(order, "filled_qty", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _not_accepted(order) -> bool:
    return order.state in (OrderState.REJECTED, OrderState.FAILED) or (
        order.state == OrderState.CANCELLED and _filled(order) <= 0
    )


def _is_entry_intent(order) -> bool:
    return bool(
        str(getattr(order, "client_oid", "") or "").startswith(BGX_PREFIX)
        and str(getattr(order, "exposure_intent", "INCREASE")) == "INCREASE"
        and not bool(getattr(order, "reduce_only", False))
        and getattr(order, "protection_plan", None)
    )


def _orders(engine) -> list:
    registry = getattr(engine, "orders", None)
    reader = getattr(registry, "all_orders", None)
    if not callable(reader):
        return []
    try:
        return list(reader() or [])
    except Exception:
        return []


def _unresolved_entry_intents(engine, symbol: str | None = None) -> list:
    """Durable BGX entry intents that never materialized a local Position."""
    result = []
    for order in _orders(engine):
        if not _is_entry_intent(order):
            continue
        if bool((order.protection_plan or {}).get("materialized", False)):
            continue
        if symbol is not None and order.symbol != symbol:
            continue
        if bool(getattr(order, "exposure_reconciliation_complete", False)):
            continue
        if _not_accepted(order):
            continue
        result.append(order)
    return result


def pending_recovery_symbols(engine) -> set[str]:
    """Symbols where a durable BGX entry intent may still own exposure.

    Used by the startup ownership classifier: such a symbol is neither EXTERNAL
    nor recovered until this module resolves the intent with exchange truth.
    """
    return {
        order.symbol for order in _unresolved_entry_intents(engine)
        if order.symbol not in (getattr(engine, "positions", {}) or {})
    }


def _mark_unprotected(engine, symbol: str) -> None:
    current = set(getattr(engine, "_unprotected_symbols", set()) or set())
    current.add(symbol)
    engine._unprotected_symbols = current


def _block_entries(engine, symbol: str | None = None) -> None:
    if symbol:
        _mark_unprotected(engine, symbol)
    durable._block(engine, BLOCK_REASON)


async def _alert_once(engine, kind: str, order, detail: str) -> None:
    """CRITICAL log every time; one Telegram alert per (kind, intent)."""
    client_oid = str(getattr(order, "client_oid", "") or "")
    symbol = str(getattr(order, "symbol", "") or "")
    log.critical(
        "[AMBIGUOUS_ENTRY_RECOVERY] kind=%s symbol=%s side=%s qty=%s "
        "client_oid=%s state=%s detail=%s entries_blocked=true",
        kind, symbol, getattr(order, "side", "?"), getattr(order, "qty", "?"),
        client_oid[:16], getattr(getattr(order, "state", None), "value", "?"), detail,
    )
    sent = getattr(engine, _ALERTS_ATTR, None)
    if not isinstance(sent, set):
        sent = set()
        setattr(engine, _ALERTS_ATTR, sent)
    key = (kind, client_oid)
    if key in sent:
        return
    sent.add(key)
    titles = {
        SUBMIT_UNKNOWN: "BGX: resultado de envio de ordem AMBÍGUO",
        "PROTECTION_UNCONFIRMED": "BGX: exposição confirmada SEM proteção confirmada",
        LINEAGE_REJECTED: "BGX: fill confirmado sem lineage exata da posição",
        "EXCHANGE_FLAT_AFTER_FILL": "BGX: fill confirmado, exchange sem posição",
        "UNATTRIBUTED_EXPOSURE": "BGX: posição viva sem posse local comprovada",
    }
    text = (
        f"🆘 *{titles.get(kind, 'BGX: incidente de entrada')}*\n"
        f"`{symbol}` {getattr(order, 'side', '?')} qty=`{getattr(order, 'qty', '?')}`\n"
        f"intent=`{client_oid[:16]}` estado=`{getattr(getattr(order, 'state', None), 'value', '?')}`\n"
        f"motivo=`{detail[:80]}`\n"
        f"_Novas entradas bloqueadas; nenhum reenvio automático._"
    )
    try:
        from bot.notifier import notify
        await asyncio.wait_for(notify(text), timeout=15)
    except Exception as exc:  # noqa: BLE001 - alert delivery never decides safety
        log.error(
            "[AMBIGUOUS_ENTRY_RECOVERY] alert_delivery_failed kind=%s error=%s",
            kind, type(exc).__name__,
        )


async def alert_unattributed_exposure(engine, symbol: str, reason: str) -> None:
    """Operator alert for live exposure the post-open path cannot attribute."""
    probe = type("Probe", (), {})()
    probe.client_oid = f"symbol:{symbol}"
    probe.symbol = symbol
    probe.side = "?"
    probe.qty = "?"
    probe.state = None
    await _alert_once(engine, "UNATTRIBUTED_EXPOSURE", probe, reason)


async def _persist(engine, reason: str) -> bool:
    try:
        return bool(await durable.persist_orders(engine, reason, strict=True))
    except Exception as exc:
        log.critical(
            "[AMBIGUOUS_ENTRY_RECOVERY] persist_failed reason=%s error=%s",
            reason, type(exc).__name__,
        )
        return False


async def _refresh_from_exchange(engine, order) -> str:
    """Converge the intent from authoritative clientOid truth (read-only)."""
    lookup = getattr(getattr(engine, "client", None), "get_order_by_client_oid", None)
    if not callable(lookup):
        return "lookup_unavailable"
    try:
        data = await lookup(order.client_oid)
    except Exception as exc:
        return f"lookup_{type(exc).__name__}"
    if not data:
        return "lookup_inconclusive"
    remote_side = _side_of(data.get("side")) if isinstance(data, dict) else ""
    if remote_side and remote_side != order.side:
        raise ValueError("durable/exchange side mismatch")
    from bot.durable_live_reconciliation import apply_exchange_order_truth

    changed, _terminal = apply_exchange_order_truth(engine, order, data, source=SOURCE)
    if changed and not await _persist(engine, "ambiguous_entry_exchange_truth"):
        return "persist_failed"
    return "applied"


def _instrument(engine, symbol: str):
    info = (getattr(engine, "instruments", {}) or {}).get(symbol)
    if isinstance(info, dict) and info:
        return info
    from bot.restart_ownership_recovery import _instrument_info

    return _instrument_info(getattr(engine, "client", None), symbol)


def _lineage(engine, order, rows: list) -> tuple[dict | None, str]:
    """Return the single live row proven to be this intent's exposure."""
    from bot.restart_ownership_recovery import _position_base_qty, _same_base_qty

    symbol = order.symbol
    if symbol in set(getattr(engine, "_external_position_symbols", set()) or set()):
        if symbol not in set(getattr(engine, PENDING_ATTR, set()) or set()):
            return None, "symbol_classified_external"
    if not order.order_id:
        return None, "exchange_order_id_unproven"
    others = [
        other for other in _unresolved_entry_intents(engine, symbol)
        if other is not order
    ]
    if others:
        return None, "multiple_unresolved_bgx_intents"

    live = []
    for row in rows or []:
        if not isinstance(row, dict) or str(row.get("symbol") or "") != symbol:
            continue
        try:
            size = abs(float(row.get("size", 0) or 0))
        except (TypeError, ValueError):
            return None, "position_size_unreadable"
        if size > 0:
            live.append(row)
    if not live:
        return None, "exchange_flat"
    if len(live) != 1:
        return None, "multiple_exchange_position_rows"
    row = live[0]

    if _side_of(row.get("side")) != order.side:
        return None, "side_mismatch"
    plan = order.protection_plan or {}
    expected_direction = "LONG" if order.side == "Buy" else "SHORT"
    if plan.get("direction") != expected_direction:
        return None, "plan_direction_mismatch"

    info = _instrument(engine, symbol)
    if not isinstance(info, dict):
        return None, "instrument_metadata_unconfirmed"
    exchange_qty = _position_base_qty(row, info)
    if not _same_base_qty(exchange_qty, _filled(order), info):
        return None, "quantity_mismatch"
    if _filled(order) > float(order.qty) + max(1e-12, float(order.qty) * 1e-9):
        return None, "fill_exceeds_intent"
    return row, "exact_bgx_lineage"


def _position_from_plan(order, row: dict):
    from bot.engine import Position
    from bot.strategy import Signal

    plan = order.protection_plan
    entry = 0.0
    for key in ("entryPrice", "avgPrice"):
        try:
            entry = float(row.get(key, 0) or 0)
        except (TypeError, ValueError):
            entry = 0.0
        if entry > 0:
            break
    if not (math.isfinite(entry) and entry > 0):
        entry = float(getattr(order, "avg_price", 0.0) or 0.0)
    if not (math.isfinite(entry) and entry > 0):
        raise ValueError("fill price unproven")
    # Same geometry rule as engine._open after a fill: shift the planned
    # SL/TP by the fill delta so the reviewed risk distance is preserved.
    delta = entry - float(plan["entry"])
    sl = float(plan["sl"]) + delta
    tp = float(plan["tp"]) + delta if float(plan["tp"]) > 0 else 0.0
    direction = plan["direction"]
    if direction == "LONG" and not 0 < sl < entry:
        raise ValueError("recovered stop geometry invalid")
    if direction == "SHORT" and not sl > entry:
        raise ValueError("recovered stop geometry invalid")
    sig = Signal(order.symbol, direction, entry, sl, tp, 0.75, "Ambiguous entry recovery", 75)
    position = Position(sig, _filled(order))
    try:
        mark = float(row.get("markPrice", entry) or entry)
        position.update_pnl(mark if mark > 0 else entry)
    except (TypeError, ValueError):
        pass
    return position


def _mark_exposure_absorbed(engine, order) -> None:
    if order.state == OrderState.FILLED:
        engine.orders.mark_filled_exposure_reconciled(order.symbol)
        return
    order.exposure_reconciliation_complete = True
    order.history.append((
        order.updated_at, order.state.value, order.state.value,
        {"source": SOURCE, "exposure_reconciled": True},
    ))


async def _adopt_and_protect(engine, order, row: dict) -> str:
    from bot.durable_live_reconciliation import _owner_valid

    symbol = order.symbol
    if not await _owner_valid(engine):
        _block_entries(engine, symbol)
        await _alert_once(engine, "PROTECTION_UNCONFIRMED", order, "execution_ownership_invalid")
        return DEFERRED
    try:
        position = _position_from_plan(order, row)
    except Exception as exc:
        _block_entries(engine, symbol)
        await _alert_once(engine, "PROTECTION_UNCONFIRMED", order, f"plan_{type(exc).__name__}")
        return ADOPTED_UNPROTECTED

    engine.positions[symbol] = position
    order.protection_plan["materialized"] = True
    pending = set(getattr(engine, PENDING_ATTR, set()) or set())
    pending.discard(symbol)
    setattr(engine, PENDING_ATTR, pending)
    external = set(getattr(engine, "_external_position_symbols", set()) or set())
    if symbol in external:
        external.discard(symbol)
        engine._external_position_symbols = external
    _mark_unprotected(engine, symbol)
    pilot = getattr(engine, "pilot", None)
    if callable(getattr(pilot, "register_position_opened", None)):
        pilot.register_position_opened(symbol)
    log.critical(
        "[AMBIGUOUS_ENTRY_RECOVERY] symbol=%s result=ADOPTED client_oid=%s order_id=%s "
        "side=%s qty=%s entry=%.8f sl=%.8f tp=%.8f basis=exact_bgx_lineage resubmit=false",
        symbol, order.client_oid[:16], order.order_id, order.side, position.qty,
        position.entry, position.sl, position.tp,
    )

    enforce = getattr(type(engine), "_bgx_enforce_owned_protection", None)
    protected = False
    if callable(enforce):
        try:
            protected = bool(await enforce(
                engine, symbol,
                preferred_sl=position.sl, preferred_tp=position.tp,
                allow_close=True,
            ))
        except Exception as exc:
            log.critical(
                "[AMBIGUOUS_ENTRY_RECOVERY] symbol=%s protection_error=%s",
                symbol, type(exc).__name__,
            )
            protected = False
    if protected and symbol not in set(getattr(engine, "_unprotected_symbols", set()) or set()):
        _mark_exposure_absorbed(engine, order)
        if not await _persist(engine, "ambiguous_entry_adopted_protected"):
            _block_entries(engine)
            return ADOPTED_UNPROTECTED
        log.critical(
            "[AMBIGUOUS_ENTRY_RECOVERY] symbol=%s result=ADOPTED_PROTECTED client_oid=%s "
            "incident=resolved",
            symbol, order.client_oid[:16],
        )
        return ADOPTED_PROTECTED

    # Position stays local, unprotected and blocking: the periodic guard keeps
    # retrying protection under the existing Binance enforcement policy.
    _block_entries(engine, symbol)
    await _alert_once(engine, "PROTECTION_UNCONFIRMED", order, "stop_not_confirmed")
    return ADOPTED_UNPROTECTED


async def _recover_one(engine, order) -> str:
    symbol = order.symbol
    if not order.is_terminal:
        try:
            outcome = await _refresh_from_exchange(engine, order)
        except ValueError as exc:
            _block_entries(engine, symbol)
            await _alert_once(engine, LINEAGE_REJECTED, order, f"exchange_truth_{exc}")
            return LINEAGE_REJECTED
    else:
        outcome = "terminal"

    if _not_accepted(order):
        pending = set(getattr(engine, PENDING_ATTR, set()) or set())
        if symbol in pending:
            # The only BGX claim on this startup exposure is proven void: the
            # exposure is not BGX and becomes EXTERNAL/read-only.
            pending.discard(symbol)
            setattr(engine, PENDING_ATTR, pending)
            external = set(getattr(engine, "_external_position_symbols", set()) or set())
            external.add(symbol)
            engine._external_position_symbols = external
        log.warning(
            "[AMBIGUOUS_ENTRY_RECOVERY] symbol=%s client_oid=%s result=NOT_ACCEPTED "
            "state=%s adopt=false",
            symbol, order.client_oid[:16], order.state.value,
        )
        return NOT_ACCEPTED

    if _filled(order) <= 0:
        _block_entries(engine)
        if order.state == OrderState.SUBMITTING:
            await _alert_once(engine, SUBMIT_UNKNOWN, order, outcome)
            return SUBMIT_UNKNOWN
        return ACCEPTED_OPEN

    try:
        rows = await engine.client.get_positions()
    except Exception as exc:
        _block_entries(engine, symbol)
        log.critical(
            "[AMBIGUOUS_ENTRY_RECOVERY] symbol=%s positions_read_failed=%s",
            symbol, type(exc).__name__,
        )
        return DEFERRED

    row, reason = _lineage(engine, order, rows if isinstance(rows, list) else [])
    if row is None:
        if reason == "exchange_flat" and order.is_terminal:
            _block_entries(engine)
            await _alert_once(engine, "EXCHANGE_FLAT_AFTER_FILL", order, reason)
            return LINEAGE_REJECTED
        _block_entries(engine, symbol)
        await _alert_once(engine, LINEAGE_REJECTED, order, reason)
        return LINEAGE_REJECTED
    return await _adopt_and_protect(engine, order, row)


async def recover_unadopted_entries(engine, *, symbol: str | None = None) -> dict:
    """Resolve every unresolved BGX entry intent without a local Position."""
    if not _enabled(engine):
        return {}
    results = {}
    local = getattr(engine, "positions", {}) or {}
    materialized = 0
    for order in _unresolved_entry_intents(engine, symbol):
        if order.symbol in local and order.state != OrderState.SUBMITTING:
            # Exposure owned by a local Position (normal entry path): this
            # intent is no longer an ambiguous-recovery candidate, even after
            # that position later closes.
            order.protection_plan["materialized"] = True
            materialized += 1
    if materialized:
        await durable.persist_orders(engine, "entry_intent_materialized", strict=False)
    for order in _unresolved_entry_intents(engine, symbol):
        if order.symbol in local:
            continue
        try:
            results[order.client_oid] = await _recover_one(engine, order)
        except Exception as exc:  # noqa: BLE001 - recovery failure is fail-closed
            _block_entries(engine, order.symbol)
            log.critical(
                "[AMBIGUOUS_ENTRY_RECOVERY] symbol=%s client_oid=%s result=ERROR "
                "error=%s entries_blocked=true",
                order.symbol, order.client_oid[:16], type(exc).__name__,
            )
            results[order.client_oid] = DEFERRED
    if symbol is None:
        remaining = [
            order for order in _unresolved_entry_intents(engine)
            if order.symbol not in (getattr(engine, "positions", {}) or {})
        ]
        if remaining:
            durable._block(engine, BLOCK_REASON)
        else:
            durable._clear(engine, BLOCK_REASON)
    return results
