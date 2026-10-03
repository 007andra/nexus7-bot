"""Canonical identity/fill validation for external order events (F-011).

Single source of truth used BEFORE any ManagedOrder mutation by the private
WebSocket handler and the REST reconciliation evaluator.

Identity rules (INV-ORDER-IDENTITY-001/002, INV-ORDER-LINEAGE-001):
* symbol is mandatory and compared canonically (XBTUSDTM == BTCUSDT);
* a present clientOid must equal the order's clientOid;
* a present orderId must equal the order's bound orderId (if bound);
* at least one strong identifier must positively match;
* a present side must equal the order side (buy/sell, case-insensitive only);
* an event flagged reduceOnly/closeOrder never updates an entry order.

Fill rules (INV-ORDER-FILL-001): KuCoin ``filledSize`` is CUMULATIVE and in
CONTRACTS; ``size`` is the order size in CONTRACTS. ManagedOrder.qty may be
base asset (engine entries) or contracts (client-created reduce orders), so the
exchange ``size`` must match one of those interpretations and bounds the fill.
Any violation raises ``OrderEventRejected``; callers must not mutate.
"""
from __future__ import annotations

import math

from bot.logger import log


class OrderEventRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def canonical_symbol(raw) -> str:
    symbol = str(raw or "").strip().upper()
    if symbol.endswith("USDTM"):
        from bot.kucoin import to_standard
        return to_standard(symbol)
    return symbol


def canonical_side(raw):
    return {"buy": "Buy", "sell": "Sell"}.get(str(raw or "").strip().lower())


def check_identity(order, *, symbol, order_id="", client_oid="", side=None,
                   reduce_only=None, close_order=None) -> None:
    if not symbol:
        raise OrderEventRejected("symbol_missing")
    if canonical_symbol(symbol) != canonical_symbol(order.symbol):
        raise OrderEventRejected("symbol_mismatch")
    if client_oid and client_oid != order.client_oid:
        raise OrderEventRejected("client_oid_mismatch")
    if order_id and order.order_id and order_id != order.order_id:
        raise OrderEventRejected("order_id_mismatch")
    strong = bool(client_oid and client_oid == order.client_oid) or bool(
        order_id and order.order_id and order_id == order.order_id
    )
    if not strong:
        raise OrderEventRejected("no_strong_identifier_match")
    if side not in (None, ""):
        normalized = canonical_side(side)
        if normalized is None:
            raise OrderEventRejected("side_unrecognized")
        if normalized != canonical_side(order.side):
            raise OrderEventRejected("side_mismatch")
    if (reduce_only is True or close_order is True) and not bool(getattr(order, "reduce_only", False)):
        raise OrderEventRejected("lineage_child_event_for_entry")


def _number(value, reason):
    if isinstance(value, bool):
        raise OrderEventRejected(reason)
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise OrderEventRejected(reason)
    if not math.isfinite(out) or out < 0:
        raise OrderEventRejected(reason)
    return out


def check_fill(order, *, filled, size, info=None) -> float:
    """Validate a cumulative contract fill against the exchange order size."""
    filled_c = _number(filled, "filled_invalid")
    if size in (None, ""):
        raise OrderEventRejected("order_size_missing")
    size_c = _number(size, "order_size_invalid")
    if size_c <= 0:
        raise OrderEventRejected("order_size_invalid")
    candidates = {float(order.qty)}
    if isinstance(info, dict):
        try:
            from bot.quantity import base_to_contracts
            candidates.add(float(base_to_contracts(order.qty, info)))
        except Exception:
            pass
    if not any(abs(size_c - c) <= max(1e-9, c * 1e-9) for c in candidates):
        raise OrderEventRejected("order_size_mismatch")
    if filled_c > size_c + max(1e-9, size_c * 1e-9):
        raise OrderEventRejected("overfill")
    return filled_c


def log_rejection(event_kind, reason, order, *, symbol="", order_id="", client_oid="", filled=None):
    tag = {"overfill": "[ORDER_EVENT_OVERFILL_REJECTED]"}.get(reason, "[ORDER_EVENT_IDENTITY_REJECTED]")
    log.warning(
        "%s source=%s reason=%s event_symbol=%s event_orderId=%s event_clientOid=%s "
        "event_filled=%s candidate_symbol=%s candidate_orderId=%s candidate_clientOid=%s "
        "mutation=NONE action=RECONCILE",
        tag, event_kind, reason, symbol or "?", order_id or "?", client_oid or "?",
        filled if filled is not None else "?",
        getattr(order, "symbol", "?"), getattr(order, "order_id", None) or "?",
        getattr(order, "client_oid", "?"),
    )
