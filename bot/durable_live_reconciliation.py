"""Bounded authoritative reconciliation for non-terminal durable LIVE orders.

Continuous runtime and startup reconciliation share ``apply_exchange_order_truth``.
The evaluator never resubmits an order and never infers terminal execution from
position state.
"""
from __future__ import annotations

import math
import time

from bot import durable_execution as durable
from bot.logger import log
from bot.order_state import OrderState


_MIN_INTERVAL_S = 10.0
_TERMINAL_STATUS = {"done", "filled", "cancelled", "canceled", "rejected", "reject"}


def _canon_symbol(value: str) -> str:
    symbol = str(value or "").upper()
    if symbol == "XBTUSDTM":
        return "BTCUSDT"
    if symbol.endswith("USDTM"):
        return f"{symbol[:-5]}USDT"
    return symbol


async def _owner_valid(engine) -> bool:
    if getattr(engine, "paper_trade", False):
        return False
    if not bool(getattr(engine, "_execution_ownership_valid", False)):
        return False
    client = getattr(engine, "client", None)
    raw_client = getattr(client, "_client", client)
    ownership = getattr(raw_client, "_execution_ownership", None)
    if ownership is None:
        ownership = getattr(client, "_execution_ownership", None)
    if ownership is None:
        return False
    try:
        from bot.execution_ownership import validate_execution_ownership
        await validate_execution_ownership(ownership)
        return True
    except Exception as exc:
        log.warning(
            "[DURABLE_LIVE_RECONCILE] ownership_valid=false error=%s",
            type(exc).__name__,
        )
        return False


def _cancel_terminal(order, *, order_id: str, filled: float, source: str) -> bool:
    if order.is_terminal:
        return False
    if order.state == OrderState.CREATED:
        order.transition(OrderState.FAILED, source=source)
        return True
    if order.state == OrderState.SUBMITTING:
        order.transition(OrderState.SUBMITTED, order_id=order_id, source=source)
    order.transition(
        OrderState.CANCELLED,
        order_id=order_id,
        filled_qty=filled,
        source=source,
    )
    return True


def _reject_terminal(order, *, order_id: str, source: str) -> bool:
    if order.is_terminal:
        return False
    if order.state == OrderState.CREATED:
        order.transition(OrderState.FAILED, source=source)
        return True
    if order.state == OrderState.PARTIALLY_FILLED:
        order.transition(OrderState.CANCELLED, order_id=order_id, source=source)
        return True
    order.transition(OrderState.REJECTED, order_id=order_id, source=source)
    return True


def apply_exchange_order_truth(
    engine, order, data: dict, *, source: str = "LIVE_REST"
) -> tuple[bool, bool]:
    """Apply one authoritative KuCoin order snapshot to a ManagedOrder.

    Returns ``(changed, terminal)``. Durable ``qty`` and KuCoin
    ``filledSize``/``dealSize`` are contract quantities. Position state is not
    consulted. A partial execution followed by cancellation is terminalized as
    CANCELLED, never falsely promoted to a full FILLED.
    """
    if not isinstance(data, dict) or not data:
        return False, False

    remote_client_oid = str(data.get("clientOid") or "")
    if remote_client_oid and remote_client_oid != order.client_oid:
        raise ValueError("durable/exchange clientOid mismatch")

    remote_symbol = str(data.get("symbol") or "")
    if remote_symbol and _canon_symbol(remote_symbol) != _canon_symbol(order.symbol):
        raise ValueError("durable/exchange symbol mismatch")

    remote_order_id = str(data.get("orderId") or data.get("id") or "")
    if order.order_id and remote_order_id and order.order_id != remote_order_id:
        raise ValueError("durable/exchange orderId mismatch")
    if remote_order_id:
        engine.orders.index_order_id(remote_order_id, order.client_oid)

    requested = float(order.qty)
    filled = float(data.get("filledSize", data.get("dealSize", 0)) or 0)
    if not all(math.isfinite(value) and value >= 0 for value in (requested, filled)):
        raise ValueError("non-finite durable order quantity")
    if requested <= 0:
        raise ValueError("invalid durable requested quantity")
    if data.get("size") not in (None, ""):
        # F-011 INV-ORDER-FILL-001: exchange cumulative fill bounded by the
        # exchange order size, which must belong to this order.
        from bot.order_event_identity import check_fill
        instruments = getattr(engine, "instruments", None) or {}
        check_fill(order, filled=filled, size=data.get("size"),
                   info=instruments.get(order.symbol) if isinstance(instruments, dict) else None)

    has_active_flag = "isActive" in data
    active = bool(data.get("isActive")) if has_active_flag else None
    status = str(data.get("status") or "").strip().lower()
    cancel_exists = bool(data.get("cancelExist", False))
    terminal_truth = (
        (has_active_flag and active is False)
        or status in _TERMINAL_STATUS
        or cancel_exists
    )
    tolerance = max(1e-9, requested * 1e-9)
    changed = False

    if active is True:
        if order.state == OrderState.CREATED:
            order.transition(OrderState.SUBMITTING, source=source)
            changed = True
        if order.state == OrderState.SUBMITTING:
            order.transition(
                OrderState.SUBMITTED,
                order_id=remote_order_id,
                source=source,
            )
            changed = True
        if filled > 0 and order.state == OrderState.SUBMITTED:
            order.transition(
                OrderState.PARTIALLY_FILLED,
                order_id=remote_order_id,
                filled_qty=filled,
                source=source,
            )
            changed = True
        elif filled > 0 and order.state == OrderState.PARTIALLY_FILLED:
            before = float(order.filled_qty or 0)
            if filled > before + tolerance:
                order.transition(
                    OrderState.PARTIALLY_FILLED,
                    order_id=remote_order_id,
                    filled_qty=filled,
                    source=source,
                )
                changed = True
        return changed, order.is_terminal

    if not terminal_truth:
        return False, False

    if filled + tolerance >= requested:
        if order.state == OrderState.CREATED:
            order.transition(OrderState.SUBMITTING, source=source)
            changed = True
        if order.state == OrderState.SUBMITTING:
            order.transition(
                OrderState.SUBMITTED,
                order_id=remote_order_id,
                source=source,
            )
            changed = True
        if not order.is_terminal:
            order.transition(
                OrderState.FILLED,
                order_id=remote_order_id,
                filled_qty=filled,
                source=source,
            )
            changed = True
        return changed, order.is_terminal

    if status in {"rejected", "reject"}:
        changed = _reject_terminal(
            order,
            order_id=remote_order_id,
            source=source,
        ) or changed
        return changed, order.is_terminal

    if (
        cancel_exists
        or status in {"cancelled", "canceled", "done", "filled"}
        or active is False
    ):
        changed = _cancel_terminal(
            order,
            order_id=remote_order_id,
            filled=filled,
            source=source,
        ) or changed
        return changed, order.is_terminal

    return changed, order.is_terminal


async def reconcile_pending(engine, *, min_interval_s: float = _MIN_INTERVAL_S) -> bool:
    """Continuously converge only non-terminal ManagedOrders from exchange truth.

    At most one byClientOid lookup per currently pending order per cadence is
    made by the execution owner. Terminal history is never polled. This path has
    no submission, fill-accounting, PnL, or trade-row side effects.
    """
    registry = getattr(engine, "orders", None)
    pending_reader = getattr(registry, "pending_orders", None)
    if not callable(pending_reader):
        return False
    pending = list(pending_reader() or [])
    if not pending:
        # No non-terminal intent remains: the unresolved-order cause is proven
        # resolved. Persistence/restore/protection reasons are not ours.
        durable._clear(engine, durable.ORDERS_UNRESOLVED, source="reconcile_pending")
        return True
    if not getattr(engine, "connected", False):
        return False
    if not await _owner_valid(engine):
        return False

    now = time.monotonic()
    last = float(getattr(engine, "_durable_live_reconcile_last", 0.0) or 0.0)
    if now - last < max(1.0, float(min_interval_s)):
        return False
    engine._durable_live_reconcile_last = now

    lookup = getattr(getattr(engine, "client", None), "get_order_by_client_oid", None)
    if not callable(lookup):
        durable._block(engine, durable.ORDERS_UNRESOLVED, source="reconcile_pending")
        return False

    changed = 0
    unresolved = []
    for order in list(pending):
        if order.is_terminal:
            continue
        try:
            data = await lookup(order.client_oid)
            if not data:
                unresolved.append(order.client_oid)
                continue
            before = order.state
            did_change, terminal = apply_exchange_order_truth(
                engine, order, data, source="LIVE_REST"
            )
            changed += int(did_change)
            if not terminal:
                unresolved.append(order.client_oid)
            log.info(
                "[DURABLE_LIVE_RECONCILE] client_oid=%s symbol=%s before=%s after=%s "
                "terminal=%s mutation=STATE_ONLY resubmit=false accounting_side_effect=false",
                order.client_oid,
                order.symbol,
                before.value,
                order.state.value,
                str(order.is_terminal).lower(),
            )
        except Exception as exc:
            unresolved.append(order.client_oid)
            log.error(
                "[DURABLE_LIVE_RECONCILE] client_oid=%s failed=%s",
                order.client_oid,
                type(exc).__name__,
            )

    if changed:
        if not await durable.persist_orders(
            engine, "continuous_exchange_truth_reconcile", strict=True
        ):
            durable._block(engine, durable.ORDERS_UNRESOLVED, source="reconcile_pending")
            return False

    remaining = list(pending_reader() or [])
    if remaining or unresolved:
        durable._block(engine, durable.ORDERS_UNRESOLVED, source="reconcile_pending")
        return False

    durable._clear(engine, durable.ORDERS_UNRESOLVED, source="reconcile_pending")
    return True
