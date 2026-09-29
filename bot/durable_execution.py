"""Durable execution state for PAPER positions and order idempotency.

This module never submits, cancels or changes an order. It stores the state
needed to make restart recovery fail closed. Trading decisions, configured
leverage, sizing thresholds and PAPER/LIVE selection remain untouched.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone

from bot import database as db
from bot.logger import log
from bot.order_state import InvalidTransition, OrderState

_ORDER_KEY = "order_registry_state_v1"
_PAPER_KEY = "paper_runtime_state_v1"
PAPER_STATE_KEY = _PAPER_KEY


def _block(engine, reason: str):
    errors = getattr(engine, "_durable_state_errors", None)
    if errors is None:
        errors = set()
        engine._durable_state_errors = errors
    errors.add(str(reason))
    engine._durable_state_ok = False


def _clear(engine, reason: str):
    errors = getattr(engine, "_durable_state_errors", set())
    errors.discard(str(reason))
    engine._durable_state_ok = not errors


def can_open(engine) -> bool:
    if getattr(engine, 'entries_paused', False) or getattr(engine, '_pending_partial_symbols', set()):
        return False
    return bool(getattr(engine, "_durable_state_ok", False)) and getattr(engine, '_daily_pnl_ok', True) is True


async def record_binance_margin_rejection(engine, order, exc) -> bool:
    """Terminalize only the exact order rejected by a structured HTTP 400/-2019.

    No regex classification, absent-order inference, retries, or historical
    intent cleanup. Unknown transport outcomes keep their pending state.
    """
    from bot.binance import BinanceAPIError

    if order is None or not isinstance(exc, BinanceAPIError):
        return False
    if (exc.method, exc.endpoint, exc.status, exc.code) != (
        "POST", "/fapi/v1/order", 400, -2019
    ):
        return False
    if (not exc.client_oid or order.client_oid != exc.client_oid
            or order.symbol != exc.symbol or str(order.side).upper() != exc.side.upper()
            or order.state != OrderState.SUBMITTING or order.order_id
            or order.filled_qty > 0):
        return False
    try:
        quantity = float(exc.quantity)
        if not math.isfinite(quantity) or quantity <= 0 or quantity != float(order.qty):
            return False
    except (TypeError, ValueError):
        return False
    order.transition(OrderState.REJECTED, source="BINANCE_HTTP_REJECTION", code=-2019)
    saved = await persist_orders(engine, "binance_margin_rejected", strict=True)
    # persist_orders clears its write fault; other unresolved orders still block.
    if engine.orders.pending_orders():
        _block(engine, "orders")
    log.warning(
        "[BINANCE_ORDER_REJECTED] clientOid=%s symbol=%s code=-2019 "
        "state=REJECTED persisted=%s resubmit=false",
        order.client_oid, order.symbol, str(saved).lower(),
    )
    return saved


def _positive(value, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"invalid {field}")
    return number


def _nonnegative(value, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid {field}")
    return number


def _position_record(position) -> dict:
    return {
        "symbol": position.symbol,
        "direction": position.direction,
        "entry": position.entry,
        "sl": position.sl,
        "tp": position.tp,
        "score": position.score,
        "qty": position.qty,
        "qty_original": position.qty_original,
        "opened_at": position.opened_at.isoformat(),
        "peak_pnl": position.peak_pnl,
        "trailing_sl": position.trailing_sl,
        "trailing_active": position.trailing_active,
        "trailing_milestone": position.trailing_milestone,
        "min_hold_until": position.min_hold_until,
        "expected_pnl": position.expected_pnl,
        "total_fees_pct": position.total_fees_pct,
        "tp1": position.tp1,
        "tp2": position.tp2,
        "tp1_hit": position.tp1_hit,
        "rr1": position.rr1,
        "rr2": position.rr2,
        "entry_type": getattr(position, "entry_type", "PULLBACK"),
        "regime": getattr(position, "regime", "RANGING"),
    }


def _restore_position(record: dict):
    from bot.engine import Position

    if not isinstance(record, dict):
        raise ValueError("position record must be a mapping")
    symbol = str(record.get("symbol", ""))
    direction = str(record.get("direction", ""))
    if not symbol or direction not in ("LONG", "SHORT"):
        raise ValueError("invalid PAPER position identity")
    entry = _positive(record.get("entry"), "entry")
    sl = _positive(record.get("sl"), "sl")
    tp = _positive(record.get("tp"), "tp")
    qty = _positive(record.get("qty"), "qty")
    tp1 = _positive(record.get("tp1", tp), "tp1")
    tp2 = _positive(record.get("tp2", tp), "tp2")
    if direction == "LONG" and not (tp > entry and 0 < sl < tp):
        raise ValueError("invalid managed LONG boundaries")
    if direction == "SHORT" and not (tp < entry and sl > tp):
        raise ValueError("invalid managed SHORT boundaries")
    if direction == "LONG" and not (tp1 > entry and tp2 > entry):
        raise ValueError("invalid managed LONG targets")
    if direction == "SHORT" and not (tp1 < entry and tp2 < entry):
        raise ValueError("invalid managed SHORT targets")

    # A managed position is not a new Signal: after TP1 its stop can be at
    # break-even or beyond entry. Re-running Signal's entry validation would
    # incorrectly reject that safe lifecycle state.
    position = object.__new__(Position)
    position.symbol = symbol
    position.direction = direction
    position.entry = entry
    position.sl = sl
    position.tp = tp
    position.score = int(record.get("score", 0))
    position.qty = qty
    position.pnl = 0.0
    position.current_price = entry
    position.expected_pnl = float(record.get("expected_pnl", 0) or 0)
    position.total_fees_pct = float(record.get("total_fees_pct", 0) or 0)
    position.tp1 = tp1
    position.tp2 = tp2
    position.rr1 = _nonnegative(record.get("rr1", 0), "rr1")
    position.rr2 = _nonnegative(record.get("rr2", 0), "rr2")
    position.qty_original = _positive(record.get("qty_original", qty), "qty_original")
    if position.qty_original < qty:
        raise ValueError("PAPER qty exceeds original quantity")
    opened_at = datetime.fromisoformat(str(record.get("opened_at")))
    if opened_at.tzinfo is not None:
        opened_at = opened_at.astimezone(timezone.utc).replace(tzinfo=None)
    position.opened_at = opened_at
    position.peak_pnl = _nonnegative(record.get("peak_pnl", 0), "peak_pnl")
    position.trailing_sl = _positive(record.get("trailing_sl", sl), "trailing_sl")
    position.trailing_active = bool(record.get("trailing_active", False))
    position.trailing_milestone = int(record.get("trailing_milestone", 0))
    position.min_hold_until = _nonnegative(
        record.get("min_hold_until", time.time()), "min_hold_until"
    )
    position.tp1_hit = bool(record.get("tp1_hit", False))
    position.entry_type = str(record.get("entry_type", "PULLBACK"))
    position.regime = str(record.get("regime", "RANGING"))
    return position


async def persist_orders(engine, reason: str, *, strict: bool = False) -> bool:
    try:
        async with engine._durable_order_lock:
            engine.orders.gc(max_age=7 * 86400)
            payload = json.dumps({
                "version": 1,
                "reason": str(reason)[:160],
                "orders": engine.orders.snapshot(),
            }, separators=(",", ":"), sort_keys=True)
            await db.save_key_value(_ORDER_KEY, payload, strict=True)
        _clear(engine, "orders")
        return True
    except Exception as exc:
        _block(engine, "orders")
        log.critical(
            "[DURABLE_ORDER] persistence failed; new entries blocked: %s: %s",
            type(exc).__name__, exc,
        )
        if strict:
            return False
        return False


def build_paper_runtime_payload(
    engine, reason: str, *, balance_override=None, peak_override=None,
    exclude_symbols=(), cooldown_override=None,
) -> str:
    balance = _nonnegative(
        (getattr(engine, "_paper_balance", engine.risk.balance)
         if balance_override is None else balance_override),
        "balance",
    )
    peak = _nonnegative(
        (getattr(engine.risk, "peak_balance", balance)
         if peak_override is None else peak_override),
        "peak",
    )
    if peak < balance:
        raise ValueError("PAPER balance exceeds peak")
    excluded = set(exclude_symbols)
    cooldown = dict(engine._cooldown)
    if cooldown_override:
        cooldown.update(cooldown_override)
    return json.dumps({
            "version": 1,
            "reason": str(reason)[:160],
            "balance": balance,
            "peak_balance": peak,
            "positions": [
                _position_record(p)
                for _, p in sorted(engine.positions.items())
                if p.symbol not in excluded
            ],
            "trade_ids": {
                str(symbol): int(trade_id)
                for symbol, trade_id in engine._trade_ids.items()
                if int(trade_id) > 0 and symbol not in excluded
            },
            "cooldown": {
                str(symbol): float(until)
                for symbol, until in cooldown.items()
                if float(until) > time.time()
            },
        }, separators=(",", ":"), sort_keys=True)


async def persist_paper_runtime(engine, reason: str, *, strict: bool = False) -> bool:
    if not getattr(engine, "paper_trade", False):
        return True
    try:
        payload = build_paper_runtime_payload(engine, reason)
        if payload == getattr(engine, "_paper_last_snapshot", None):
            return True
        async with engine._durable_paper_lock:
            await db.save_key_value(_PAPER_KEY, payload, strict=True)
            engine._paper_last_snapshot = payload
        _clear(engine, "paper")
        return True
    except Exception as exc:
        _block(engine, "paper")
        log.critical(
            "[PAPER_STATE] persistence failed; new entries blocked: %s: %s",
            type(exc).__name__, exc,
        )
        if strict:
            return False
        return False


async def restore_engine_state(engine) -> bool:
    """Restore validated snapshots before any scan can open a position."""
    engine._durable_state_errors = set()
    engine._durable_state_ok = True
    engine._durable_order_lock = asyncio.Lock()
    engine._durable_paper_lock = asyncio.Lock()
    engine._paper_last_snapshot = None

    async def _registry_callback(_order):
        await persist_orders(engine, "private_ws_transition")

    engine.orders.persist_callback = _registry_callback

    if db.configured_postgres_unavailable():
        _block(engine, "database")
        log.critical(
            "[DURABLE_STATE] configured PostgreSQL is unavailable; "
            "fallback storage cannot authorize new entries"
        )
    else:
        _clear(engine, "database")

    try:
        raw_orders = await db.load_key_value(_ORDER_KEY, strict=True)
        if raw_orders:
            state = json.loads(raw_orders)
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("unsupported order registry schema")
            engine.orders.restore(state.get("orders"))
            identity_hook = getattr(
                getattr(engine, "client", None),
                "rehydrate_order_identity_maps",
                None,
            )
            if callable(identity_hook):
                identity_hook(engine.orders.snapshot())
            log.info(
                "[DURABLE_ORDER] restored orders=%s pending=%s",
                len(engine.orders), len(engine.orders.pending_orders()),
            )
        _clear(engine, "orders")
    except Exception as exc:
        _block(engine, "orders")
        log.critical("[DURABLE_ORDER] restore failed; new entries blocked: %s", exc)

    if not getattr(engine, "paper_trade", False):
        return can_open(engine)

    try:
        raw_paper = await db.load_key_value(_PAPER_KEY, strict=True)
        if not raw_paper:
            _clear(engine, "paper")
            return can_open(engine)
        state = json.loads(raw_paper)
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("unsupported PAPER runtime schema")
        balance = _nonnegative(state.get("balance"), "balance")
        peak = _nonnegative(state.get("peak_balance"), "peak")
        if peak < balance:
            raise ValueError("PAPER balance exceeds peak")
        records = state.get("positions")
        if not isinstance(records, list):
            raise ValueError("PAPER positions must be a list")
        restored = {}
        for record in records:
            position = _restore_position(record)
            if position.symbol in restored:
                raise ValueError("duplicate PAPER position symbol")
            restored[position.symbol] = position
        trade_ids = state.get("trade_ids", {})
        cooldown = state.get("cooldown", {})
        if not isinstance(trade_ids, dict) or not isinstance(cooldown, dict):
            raise ValueError("invalid PAPER metadata")
        engine.positions = restored
        engine._trade_ids = {
            str(symbol): int(trade_id)
            for symbol, trade_id in trade_ids.items()
            if str(symbol) in restored and int(trade_id) > 0
        }
        if set(engine._trade_ids) != set(restored):
            raise ValueError("every PAPER position requires a durable trade id")
        engine._cooldown = {
            str(symbol): float(until)
            for symbol, until in cooldown.items()
            if math.isfinite(float(until)) and float(until) > time.time()
        }
        engine._paper_balance = balance
        engine.risk.balance = balance
        engine.risk.peak_balance = peak
        engine.risk.drawdown = ((peak - balance) / peak) if peak else 0.0
        engine.risk.balance_confirmed = balance > 0
        engine._paper_last_snapshot = raw_paper
        _clear(engine, "paper")
        log.info(
            "[PAPER_STATE] restored balance=$%.4f peak=$%.4f positions=%s",
            balance, peak, len(restored),
        )
    except Exception as exc:
        _block(engine, "paper")
        log.critical("[PAPER_STATE] restore failed; new entries blocked: %s", exc)
    return can_open(engine)


def _advance(order, state: OrderState, **info):
    if order.state == state:
        return
    if state == OrderState.FILLED and order.state == OrderState.SUBMITTING:
        order.transition(OrderState.SUBMITTED, **info)
    order.transition(state, **info)


async def reconcile_orders(engine) -> bool:
    """Resolve restored non-terminal intents without ever resubmitting them.

    LIVE startup uses the exact same authoritative exchange-order evaluator as
    continuous runtime reconciliation. Position presence/flatness is never a
    substitute for order truth.
    """
    pending = list(engine.orders.pending_orders())
    if not pending:
        _clear(engine, "orders")
        return True

    unresolved = []
    for order in pending:
        try:
            if getattr(engine, "paper_trade", False):
                terminal = (
                    OrderState.FAILED
                    if order.state in (OrderState.CREATED, OrderState.SUBMITTING)
                    else OrderState.CANCELLED
                )
                _advance(order, terminal, source="PAPER_RESTART")
                continue
            if not getattr(engine, "connected", False):
                unresolved.append(order.client_oid)
                continue
            data = await engine.client.get_order_by_client_oid(order.client_oid)
            if not data:
                unresolved.append(order.client_oid)
                continue
            # Local import avoids a module-import cycle while making startup and
            # continuous LIVE reconciliation share one authoritative state
            # transition implementation.
            from bot.durable_live_reconciliation import apply_exchange_order_truth
            _, terminal = apply_exchange_order_truth(
                engine, order, data, source="STARTUP_REST"
            )
            if not terminal:
                unresolved.append(order.client_oid)
        except Exception as exc:
            log.error("[DURABLE_ORDER] reconcile %s failed: %s", order.client_oid, exc)
            unresolved.append(order.client_oid)

    saved = await persist_orders(engine, "startup_reconcile", strict=True)
    if unresolved:
        _block(engine, "orders")
        log.critical(
            "[DURABLE_ORDER] unresolved intents=%s; new entries blocked; no retry sent",
            len(unresolved),
        )
        return False
    return saved
