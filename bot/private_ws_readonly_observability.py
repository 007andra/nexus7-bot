"""Read-only private/account readiness observability for SHADOW LIVE.

This module never submits/cancels orders, changes leverage/stops, or grants
execution permission. It performs authenticated read-only exchange checks for
private WS subscription, live positions, active orders, and existing protective
stops, then stores only diagnostic state on the client instance.

A protected external/manual position is compatible with SHADOW pre-live
readiness when its protection is confirmed read-only and there are no active
normal orders. It still consumes pilot concurrency elsewhere. Any unknown or
unprotected exposure remains fail-closed.
"""

import asyncio


def _first(order, *keys, default=""):
    """Return the first non-empty value across exchange field-name variants."""
    for key in keys:
        value = order.get(key)
        if value not in (None, ""):
            return value
    return default


def _log_active_order_forensics(orders, log):
    """Emit normalized, non-secret details for active orders in SHADOW only."""
    for idx, order in enumerate(orders or [], start=1):
        if not isinstance(order, dict):
            log.warning(
                "[PRELIVE_ACTIVE_ORDER] index=%s result=UNPARSEABLE execution_effect=NONE",
                idx,
            )
            continue
        log.warning(
            "[PRELIVE_ACTIVE_ORDER] index=%s symbol=%s side=%s type=%s status=%s "
            "price=%s size=%s filled=%s orderId=%s clientOid=%s createdAt=%s "
            "source=exchange read_only=true execution_effect=NONE",
            idx,
            _first(order, "symbol", "contract", default="?"),
            _first(order, "side", default="?"),
            _first(order, "type", "orderType", default="?"),
            _first(order, "status", default="active"),
            _first(order, "price", "orderPrice", default="0"),
            _first(order, "size", "orderSize", "qty", default="0"),
            _first(order, "filledSize", "dealSize", "filledQty", default="0"),
            _first(order, "orderId", "id", default="?"),
            _first(order, "clientOid", "clientOrderId", default="?"),
            _first(order, "createdAt", "createdTime", "ts", default="?"),
        )


async def refresh_account_exposure(client, log) -> bool:
    """Refresh SHADOW pre-live exposure from current exchange read-only state.

    This is intentionally safe to call again after startup. It allows a manual
    stop added later to be recognized, and it also fails closed again if a
    protective stop disappears or account exposure becomes unverifiable.
    """
    from bot.prelive_readonly_probe import _active_orders
    from bot.conditional_stop_protection import conditional_stop_confirmed

    try:
        positions_raw = await client.get_positions()
        positions = []
        for p in positions_raw or []:
            try:
                active = abs(float(p.get("size", 0) or 0)) > 0
            except (AttributeError, TypeError, ValueError):
                raise ValueError("unparseable_position_size")
            if active:
                positions.append(p)

        open_orders = getattr(client, "get_open_orders", None)
        if callable(open_orders):
            orders_raw = await open_orders()
        else:
            orders_raw = await client._get(
                "/api/v1/orders", {"status": "active"}, auth=True
            )
        orders = _active_orders(orders_raw)

        protected = []
        unprotected = []
        for position in positions:
            confirmed, evidence = await conditional_stop_confirmed(client, position)
            item = (position, evidence)
            if confirmed is True:
                protected.append(item)
            else:
                unprotected.append(item)

        exposure_clear = not orders and not unprotected
        setattr(client, "_prelive_active_positions", len(positions))
        setattr(client, "_prelive_active_orders", len(orders))
        setattr(client, "_prelive_protected_positions", len(protected))
        setattr(client, "_prelive_unprotected_positions", len(unprotected))
        setattr(client, "_prelive_account_exposure_verified", True)
        setattr(client, "_prelive_account_exposure_clear", exposure_clear)
        log.warning(
            "[PRELIVE_ACCOUNT_EXPOSURE] result=%s positions=%s active_orders=%s "
            "protected_positions=%s unprotected_positions=%s execution_effect=NONE",
            "PASS" if exposure_clear else "BLOCKED",
            len(positions),
            len(orders),
            len(protected),
            len(unprotected),
        )
        for position, evidence in protected:
            log.info(
                "[PRELIVE_PROTECTED_POSITION] symbol=%s evidence=%s read_only=true "
                "capacity_effect=count_slot execution_effect=NONE",
                _first(position, "symbol", default="?"),
                evidence,
            )
        if orders:
            _log_active_order_forensics(orders, log)
        return exposure_clear
    except Exception as exc:
        setattr(client, "_prelive_active_positions", None)
        setattr(client, "_prelive_active_orders", None)
        setattr(client, "_prelive_protected_positions", None)
        setattr(client, "_prelive_unprotected_positions", None)
        setattr(client, "_prelive_account_exposure_verified", False)
        setattr(client, "_prelive_account_exposure_clear", False)
        log.warning(
            "[PRELIVE_ACCOUNT_EXPOSURE] result=FAIL reason=%s execution_effect=NONE",
            type(exc).__name__,
        )
        return False


async def run(client, instruments, log) -> bool:
    from bot.prelive_readonly_probe import _private_ws_probe

    symbol = "ETHUSDT" if "ETHUSDT" in instruments else next(iter(instruments), "")
    if not symbol:
        setattr(client, "_prelive_private_ws_probe_ok", False)
        setattr(client, "_prelive_account_exposure_verified", False)
        setattr(client, "_prelive_account_exposure_clear", False)
        log.warning(
            "[PRIVATE_WS_READONLY_PROBE] result=FAIL reason=no_instrument execution_effect=NONE"
        )
        return False

    exposure_clear = await refresh_account_exposure(client, log)

    try:
        ws_ok = bool(
            await asyncio.wait_for(_private_ws_probe(client, symbol), timeout=20.0)
        )
    except Exception as exc:
        setattr(client, "_prelive_private_ws_probe_ok", False)
        log.warning(
            "[PRIVATE_WS_READONLY_PROBE] result=FAIL symbol=%s reason=%s execution_effect=NONE",
            symbol,
            type(exc).__name__,
        )
        return False

    setattr(client, "_prelive_private_ws_probe_ok", ws_ok)
    log.info(
        "[PRIVATE_WS_READONLY_PROBE] result=%s symbol=%s transport_probe=%s "
        "scope=handshake_and_ping_only event_delivery_authority=private_stream_health "
        "execution_effect=NONE",
        "PASS" if ws_ok else "FAIL",
        symbol,
        str(ws_ok).lower(),
    )
    return bool(ws_ok and exposure_clear)
