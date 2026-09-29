"""Startup reconciliation hardening for durable LIVE order intents.

This module never submits, cancels, amends or retries exchange orders. It only
uses authenticated read evidence to resolve already-persisted order intents.
"""
from __future__ import annotations

import time
import math
import os

STALE_SUBMITTING_AGE_S = 300.0

# Operator-authorized incident recovery, NOT a general absence->rejection rule.
# Railway e430206e-c76f-416d-94b4-ed17e83c8ae6 / SHA 67cf44a32db87ec6c39e360d128e4bbe1c2317fd
# 2026-09-29T07:45:21.852384687Z: POST /fapi/v1/order HTTP 400 code=-2019,
# side=Buy qty=19.34 LINKUSDT. No engine retry (1/1). Exact id reconstructed
# with the deployed build_client_oid algorithm from LINKUSDT_Buy_19.34_29844465
# and matched to restored SUBMITTING record on deployment 8f801e05... .
_LINK_REJECTED_OID = "bgx7-3c132f2a2fe68e76ba376fdfbedb70"


async def _recover_audited_link_rejection(engine, order, log) -> bool:
    from bot.binance import BinanceAPIError, BinanceClient
    from bot.order_state import OrderState
    from bot.durable_live_reconciliation import _owner_valid

    if order.client_oid != _LINK_REJECTED_OID:
        return False
    client = getattr(engine.client, "_client", engine.client)
    if (getattr(engine, "paper_trade", True) or not isinstance(client, BinanceClient)
            or os.getenv("RAILWAY_PROJECT_ID") != "1443ec46-186f-497b-9e61-4b69446d35c3"
            or os.getenv("RAILWAY_ENVIRONMENT_ID") != "e07566a4-170b-418d-9c91-b19604db8b3e"
            or order.symbol != "LINKUSDT" or order.side != "Buy" or order.qty != 19.34
            or order.state != OrderState.SUBMITTING or order.order_id
            or order.filled_qty != 0 or order.avg_price != 0
            or order.reduce_only or order.exposure_intent != "INCREASE"
            or not 1790667900 <= order.created_at <= 1790667922
            or getattr(engine, "positions", {})):
        return False
    try:
        if not await _owner_valid(engine):
            return False
        # Bypass the convenience lookup that collapses arbitrary failures to {}.
        try:
            await client._get("/fapi/v1/order", {
                "symbol": "LINKUSDT", "origClientOrderId": order.client_oid,
            }, auth=True)
        except BinanceAPIError as exc:
            if (exc.method, exc.endpoint, exc.status, exc.code) != (
                "GET", "/fapi/v1/order", 400, -2013
            ):
                raise
        else:
            return False
        positions = await client._get("/fapi/v3/positionRisk", auth=True)
        if not isinstance(positions, list):
            return False
        for row in positions:
            if not isinstance(row, dict) or "positionAmt" not in row:
                return False
            qty = float(row["positionAmt"])
            if not math.isfinite(qty) or qty != 0:
                return False
        for endpoint in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
            rows = await client._get(endpoint, auth=True)
            if endpoint.endswith("openAlgoOrders") and isinstance(rows, dict):
                rows = rows.get("orders")
            if not isinstance(rows, list) or rows:
                return False
        # Read actual leverage for the incident symbol, never change it here.
        config = await client.get_symbol_config("LINKUSDT")
        if (config.get("symbol") != "LINKUSDT"
                or str(config.get("marginType", "")).upper() not in {"CROSS", "CROSSED"}
                or not 1 <= int(config.get("leverage", 0)) <= 125):
            return False
        if not await _owner_valid(engine):
            return False
        # Recheck in-memory evidence after asynchronous reads (private WS races).
        if (order.state != OrderState.SUBMITTING or order.order_id
                or order.filled_qty != 0 or order.avg_price != 0
                or getattr(engine, "positions", {})):
            return False
        order.transition(OrderState.REJECTED, source="AUDITED_BINANCE_2019_20260929",
                         code=-2019, evidence_deployment="e430206e-c76f-416d-94b4-ed17e83c8ae6",
                         evidence_timestamp="2026-09-29T07:45:21.852384687Z")
        log.warning("[AUDITED_LINK_REJECTION] clientOid=%s state=REJECTED "
                    "exchange_leverage=%s persistence=PENDING resubmit=false exchange_mutation=false",
                    order.client_oid, config["leverage"])
        return True
    except Exception as exc:
        log.warning("[AUDITED_LINK_REJECTION] result=BLOCK error_type=%s", type(exc).__name__)
        return False


def _active_items(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("data") or payload.get("orders") or []
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    return None


async def _prove_absent_and_flat(engine, order, log) -> bool:
    """Prove only that a stale intent is not active *now*.

    This diagnostic evidence is deliberately not dispatch provenance. Current
    absence, account flatness and zero active orders cannot prove that an order
    was never historically dispatched.
    """
    try:
        by_oid = await engine.client.get_order_by_client_oid(order.client_oid)
        if by_oid:
            return False
        positions = await engine.client.get_positions()
        if not isinstance(positions, list):
            return False
        if any(abs(float((p or {}).get("size", 0) or 0)) > 0 for p in positions if isinstance(p, dict)):
            return False
        open_orders = getattr(engine.client, "get_open_orders", None)
        if callable(open_orders):
            active = await open_orders()
        else:
            active = await engine.client._get("/api/v1/orders", {"status": "active"}, auth=True)
        items = _active_items(active)
        if items is None or items:
            return False
        return True
    except Exception as exc:
        log.error(
            "[DURABLE_RECONCILE] current-absence proof failed clientOid=%s: %s",
            order.client_oid, exc,
        )
        return False


def _proven_not_dispatched(order) -> tuple[bool, str]:
    """Consume the canonical BGX-PREDISPATCH-001 durable provenance."""
    from bot import pilot_submission_counter as provenance

    attempted, abort_reason = provenance._provenance(order)
    return attempted is False and bool(abort_reason), abort_reason


def install(durable_module, order_state_module, log) -> None:
    if getattr(durable_module, "_startup_reconcile_hardening_installed", False):
        return

    original = durable_module.reconcile_orders
    OrderState = order_state_module.OrderState

    async def reconcile_orders_hardened(engine) -> bool:
        ok = await original(engine)
        if ok:
            return True

        pending = list(engine.orders.pending_orders())
        if not pending:
            durable_module._clear(engine, "orders")
            return await durable_module.persist_orders(
                engine, "startup_reconcile_hardened_empty", strict=True
            )

        changed = False
        for order in pending:
            if await _recover_audited_link_rejection(engine, order, log):
                changed = True
                continue
            age_s = max(0.0, time.time() - float(order.created_at or time.time()))
            log.warning(
                "[DURABLE_RECONCILE_DETAIL] clientOid=%s symbol=%s state=%s "
                "orderId=%s age_s=%.1f",
                order.client_oid, order.symbol, order.state.value,
                order.order_id or "NONE", age_s,
            )

            proven_not_dispatched, abort_reason = _proven_not_dispatched(order)
            if (
                proven_not_dispatched
                and order.state in (OrderState.CREATED, OrderState.SUBMITTING)
            ):
                order.transition(
                    OrderState.FAILED,
                    source="STARTUP_PROVEN_NOT_DISPATCHED",
                    reason=abort_reason,
                )
                changed = True
                log.warning(
                    "[DURABLE_RECONCILE] terminalized proven pre-dispatch intent "
                    "clientOid=%s symbol=%s state=FAILED reason=%s execution_effect=NONE",
                    order.client_oid, order.symbol, abort_reason,
                )
                continue

            if order.order_id and order.state in (
                OrderState.SUBMITTING,
                OrderState.SUBMITTED,
                OrderState.PARTIALLY_FILLED,
            ):
                try:
                    status = await engine.client.get_order_status(order.order_id)
                except Exception as exc:
                    log.error(
                        "[DURABLE_RECONCILE] orderId lookup failed orderId=%s: %s",
                        order.order_id, exc,
                    )
                    status = {}

                if isinstance(status, dict) and status and not status.get("_unknown"):
                    try:
                        filled = float(status.get("filledSize", status.get("dealSize", 0)) or 0)
                    except (TypeError, ValueError):
                        filled = 0.0
                    active = bool(status.get("isActive", True))
                    cancelled = bool(status.get("cancelExist", False))

                    if filled > 0 and not active and not cancelled:
                        durable_module._advance(
                            order, OrderState.FILLED,
                            order_id=order.order_id,
                            filled_qty=filled,
                            source="STARTUP_ORDER_ID",
                        )
                        changed = True
                        continue

                    if not active and filled <= 0:
                        target = (
                            OrderState.REJECTED
                            if order.state == OrderState.SUBMITTING
                            else OrderState.CANCELLED
                        )
                        order.transition(
                            target,
                            source="STARTUP_ORDER_ID",
                            order_id=order.order_id,
                        )
                        changed = True
                        continue

            # Legacy CREATED/SUBMITTING records can predate durable dispatch
            # provenance. Current absence, flatness, zero active orders and age
            # are not historical proof that POST was never crossed. Preserve
            # ambiguity as non-terminal so startup remains fail-closed.
            if (
                order.state in (OrderState.CREATED, OrderState.SUBMITTING)
                and not order.order_id
                and age_s >= STALE_SUBMITTING_AGE_S
            ):
                not_active_now = await _prove_absent_and_flat(engine, order, log)
                log.warning(
                    "[DURABLE_RECONCILE] legacy dispatch ambiguity preserved "
                    "clientOid=%s symbol=%s state=%s age_s=%.1f not_active_now=%s "
                    "historical_not_dispatched_proven=false execution_effect=NONE",
                    order.client_oid, order.symbol, order.state.value, age_s,
                    str(not_active_now).lower(),
                )

        if changed:
            saved = await durable_module.persist_orders(
                engine, "startup_reconcile_hardened", strict=True
            )
            if not saved:
                return False

        remaining = list(engine.orders.pending_orders())
        if remaining:
            durable_module._block(engine, "orders")
            log.critical(
                "[DURABLE_RECONCILE] remaining_unresolved=%s; fail_closed=true; no retry sent",
                len(remaining),
            )
            return False

        durable_module._clear(engine, "orders")
        log.warning(
            "[DURABLE_RECONCILE] all restored intents resolved; new entries may "
            "proceed subject to normal gates execution_effect=NONE"
        )
        return True

    durable_module.reconcile_orders = reconcile_orders_hardened
    durable_module._startup_reconcile_hardening_installed = True
    log.info(
        "[DURABLE_RECONCILE] startup hardening installed: canonical pre-dispatch "
        "provenance + orderId recovery + fail-closed legacy ambiguity; no exchange mutations"
    )
