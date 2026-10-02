"""Repair KuCoin Futures protection with verified conditional closing orders.

The desired SL/TP is still supplied by the existing trading policy. This module
changes only lifecycle mechanics: BGX-owned protection is deterministic across
retries, a replacement is verified before same-lineage superseded BGX stops are
retired, and external/user-created orders are never adopted or cancelled.
"""
import asyncio
import math

from bot.conditional_stop_protection import (
    _instrument_info,
    _normalized_symbol,
    _order_active,
    _to_base_size,
    read_stop_orders,
)
from bot import conditional_stop_lifecycle as lifecycle


def _number(value):
    value = float(value or 0)
    if not math.isfinite(value):
        raise ValueError("nonfinite protection input")
    return value


def _same_trigger_price(readback, requested, instrument_info=None):
    try:
        left, right = _number(readback), _number(requested)
        tick = 0.0
        if isinstance(instrument_info, dict):
            tick = _number(instrument_info.get("tickSize", 0))
        tolerance = (
            tick + max(1e-12, abs(right) * 1e-12)
            if tick > 0 else max(1e-12, abs(right) * 1e-12)
        )
        return abs(left - right) <= tolerance
    except (ValueError, TypeError, AttributeError):
        return False


def _matches(order, body, position=None, instrument_info=None):
    try:
        semantic_match = (
            _order_active(order)
            and _normalized_symbol(order.get("symbol", ""))
            == _normalized_symbol(body["symbol"])
            and str(order.get("side", "")).lower() == body["side"]
            and (order.get("closeOrder") is True or order.get("reduceOnly") is True)
            and str(order.get("stop", "")).lower() == body["stop"]
            and str(order.get("stopPriceType") or body["stopPriceType"]).upper()
            == body["stopPriceType"]
            and _same_trigger_price(
                order.get("stopPrice"), body["stopPrice"], instrument_info
            )
        )
        if not semantic_match or position is None:
            return False
        if order.get("closeOrder") is True:
            return True
        position_qty = abs(_number(position.get("size")))
        if position_qty <= 0:
            return False
        position_base = _to_base_size(
            position_qty,
            position.get("sizeUnit", "CONTRACTS"),
            instrument_info,
        )
        covered = _to_base_size(
            order.get("size", order.get("qty", 0)),
            order.get("sizeUnit", "CONTRACTS"),
            instrument_info,
        )
        return (
            position_base > 0
            and covered + max(1e-12, position_base * 1e-9) >= position_base
        )
    except (ValueError, TypeError, AttributeError):
        return False


def _exact_matches(orders, body, position, instrument_info):
    return sorted(
        [
            row for row in (orders or [])
            if _matches(row, body, position, instrument_info)
        ],
        key=lambda row: float(
            row.get("updatedAt", row.get("createdAt", 0)) or 0
        ),
        reverse=True,
    )


def _covers_position(order, position, instrument_info):
    if order.get("closeOrder") is True:
        return True
    position_base = _to_base_size(abs(_number(position.get("size"))),
                                  position.get("sizeUnit", "CONTRACTS"), instrument_info)
    covered = _to_base_size(order.get("size", order.get("qty", 0)),
                            order.get("sizeUnit", "CONTRACTS"), instrument_info)
    return position_base > 0 and covered + max(1e-12, position_base * 1e-9) >= position_base


async def _owned_stop_floor(client, symbol, position_side, order_side, lineage, orders,
                            position, instrument_info, long):
    """Most protective active BGX SL of this exact lineage covering the position."""
    best = None
    for row in orders or []:
        try:
            if not (lifecycle._active(row) and lifecycle.is_bgx_owned(row)
                    and lifecycle.protection_kind(row, position_side) == "SL"
                    and _covers_position(row, position, instrument_info)):
                continue
            trigger = _number(row.get("stopPrice"))
            if trigger <= 0 or not await lifecycle.owned_for_lineage(
                    client, symbol, order_side, "SL", lineage, str(row.get("clientOid") or "")):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        if best is None or (trigger > best if long else trigger < best):
            best = trigger
    return best


async def set_stops(client, symbol, sl, tp, kucoin_mod, log):
    if kucoin_mod.PAPER_TRADE or not kucoin_mod.API_KEY:
        return False
    try:
        sl, tp = _number(sl), _number(tp)
        if sl < 0 or tp < 0 or not (sl > 0 or tp > 0):
            return False
        # NOVO-01: protecting a validly identified position is risk reducing;
        # another symbol's malformed row must not block it. An UNKNOWN target
        # symbol is never mutated.
        from bot.position_snapshot import read_for_risk_reduction
        view = await read_for_risk_reduction(client, source="native_stop_repair")
        if symbol in view.unknown_symbols:
            log.critical("[RISK_REDUCTION_UNKNOWN_SKIPPED] stage=stop_repair symbol=%s "
                         "reason=row_rejected mutation=NONE", symbol)
            return False
        pos = next(
            (
                p for p in view.rows
                if p.get("symbol") == symbol
                and abs(_number(p.get("size"))) > 0
            ),
            None,
        )
        if pos is None:
            return False
        if not view.authoritative:
            log.critical("[RISK_REDUCTION_VALID_ROW] stage=stop_repair symbol=%s action=set_stop "
                         "snapshot_authority=%s", symbol, view.state)
        side = str(pos.get("side", "")).lower()
        if side not in ("buy", "sell", "long", "short"):
            return False
        long = side in ("buy", "long")
        reference = _number(pos.get("markPrice") or pos.get("entryPrice"))
        if reference <= 0:
            return False
        orders = await read_stop_orders(client, symbol)
        instrument_info = _instrument_info(client, symbol)
        if orders is None:
            return False
        lineage = lifecycle.position_lineage(client, symbol, pos)

        for kind, price in (("SL", sl), ("TP", tp)):
            if price <= 0:
                continue
            rounded = client._round_price(price, symbol)
            below = long if kind == "SL" else not long
            trigger = _number(rounded)
            if kind == "SL":
                invalid = (long and trigger >= reference) or (
                    (not long) and trigger <= reference
                )
            else:
                invalid = (long and trigger <= reference) or (
                    (not long) and trigger >= reference
                )
            if invalid:
                log.error(
                    "[NATIVE_STOP_REPAIR] symbol=%s kind=%s invalid_trigger_side",
                    symbol,
                    kind,
                )
                return False

            if kind == "SL":
                # Q-01C INV-STOP-MONOTONIC-001 at the exchange boundary: a more
                # protective owned stop already verified active for this lineage
                # is never replaced by a looser one (stale local BE, restart,
                # repair). No new order, no cancellation.
                from bot.stop_monotonic import WORSE, decide_stop, log_decision
                floor = await _owned_stop_floor(
                    client, symbol, side, "sell" if long else "buy", lineage, orders,
                    pos, instrument_info, long)
                if floor is not None:
                    direction = "LONG" if long else "SHORT"
                    decision = decide_stop(
                        direction, floor, trigger,
                        tick_size=(instrument_info or {}).get("tickSize"))
                    if decision.reason == WORSE:
                        log_decision(symbol, direction, floor, trigger, decision,
                                     "native_stop_repair_exchange_floor")
                        continue

            body = dict(
                symbol=kucoin_mod.to_kucoin(symbol),
                side="sell" if long else "buy",
                type="market",
                stop="down" if below else "up",
                stopPrice=rounded,
                stopPriceType="MP",
                closeOrder=True,
                reduceOnly=True,
            )

            exact_rows = _exact_matches(orders, body, pos, instrument_info)

            # An external exact stop may already provide valid exchange
            # protection (notably across restart). Respect it without adopting,
            # persisting, renaming, or cancelling it. This preserves the legacy
            # no-duplicate behavior while keeping ownership boundaries strict.
            external_exact = next(
                (row for row in exact_rows if not lifecycle.is_bgx_owned(row)),
                None,
            )
            if external_exact is not None:
                log.info(
                    "[PROTECTION_READBACK] symbol=%s kind=%s "
                    "canonical_client_oid=EXTERNAL_UNOWNED superseded_bgx=0 "
                    "external=1 desired_trigger=%s verified_trigger=%s "
                    "cleanup_status=NOT_OWNED",
                    symbol,
                    kind,
                    rounded,
                    external_exact.get("stopPrice"),
                )
                continue

            # Price equivalence alone does not authorize BGX ownership/lineage.
            # A BGX stop is canonical only when its clientOid was persisted in
            # the slot for this exact live lineage.
            exact = None
            for row in exact_rows:
                if not lifecycle.is_bgx_owned(row):
                    continue
                oid = str(row.get("clientOid") or "")
                if await lifecycle.owned_for_lineage(
                    client, symbol, body["side"], kind, lineage, oid
                ):
                    exact = row
                    break
            if exact is not None:
                canonical_oid = str(exact.get("clientOid") or "")
                cleanup_ok, superseded, external = (
                    await lifecycle.cleanup_superseded(
                        client,
                        symbol,
                        side,
                        kind,
                        canonical_oid,
                        read_stop_orders,
                    )
                )
                log.info(
                    "[PROTECTION_READBACK] symbol=%s kind=%s "
                    "canonical_client_oid=%s superseded_bgx=%s external=%s "
                    "desired_trigger=%s verified_trigger=%s cleanup_status=%s",
                    symbol,
                    kind,
                    canonical_oid,
                    superseded,
                    external,
                    rounded,
                    exact.get("stopPrice"),
                    "VERIFIED" if cleanup_ok else "UNCONFIRMED",
                )
                if not cleanup_ok:
                    return False
                orders = await read_stop_orders(client, symbol)
                if orders is None:
                    return False
                continue

            candidate = await lifecycle.prepare_candidate(
                client,
                symbol,
                side,
                body["side"],
                kind,
                lineage,
                str(rounded),
            )

            if (
                not candidate.get("post_allowed")
                and candidate.get("reason")
                == "capacity_blocked_reconcile_required"
                and isinstance(orders, list)
                and len(orders) < 50
            ):
                if await lifecycle.clear_capacity_if_recovered(
                    client, candidate["slot_key"], len(orders)
                ):
                    candidate = await lifecycle.prepare_candidate(
                        client,
                        symbol,
                        side,
                        body["side"],
                        kind,
                        lineage,
                        str(rounded),
                    )

            body["clientOid"] = candidate["client_oid"]
            if not candidate.get("post_allowed"):
                log.error(
                    "[NATIVE_STOP_REPAIR] symbol=%s kind=%s "
                    "readback_unconfirmed candidate=%s reason=%s resubmit=false",
                    symbol,
                    kind,
                    body["clientOid"],
                    candidate.get("reason"),
                )
                return False

            result = None
            capacity_error = False
            try:
                result = await client._post(
                    "/api/v1/orders", body, single_attempt=True
                )
            except Exception as exc:
                capacity_error = "300004" in str(exc)
                if capacity_error:
                    await lifecycle.mark_capacity_blocked(
                        client, candidate["slot_key"], len(orders)
                    )
                    log.error(
                        "[NATIVE_STOP_CAPACITY] symbol=%s code=300004 stops=%s "
                        "action=RECONCILE_NO_RESUBMIT",
                        symbol,
                        len(orders),
                    )
                else:
                    log.warning(
                        "[NATIVE_STOP_REPAIR] symbol=%s post_unconfirmed=%s",
                        symbol,
                        type(exc).__name__,
                    )

            confirmed = None
            for attempt in range(4):
                if attempt:
                    await asyncio.sleep(0.25 * attempt)
                orders = await read_stop_orders(client, symbol)
                if orders is None:
                    continue
                confirmed = next(
                    (
                        row for row in orders
                        if str(row.get("clientOid") or "")
                        == body["clientOid"]
                        and _matches(row, body, pos, instrument_info)
                    ),
                    None,
                )
                if confirmed is not None:
                    break

            if confirmed is None:
                if (
                    isinstance(orders, list)
                    and len(orders) >= 50
                    and (capacity_error or not result)
                ):
                    await lifecycle.mark_capacity_blocked(
                        client, candidate["slot_key"], len(orders)
                    )
                    log.error(
                        "[NATIVE_STOP_CAPACITY] symbol=%s code=300004 stops=%s "
                        "action=RECONCILE_NO_RESUBMIT",
                        symbol,
                        len(orders),
                    )
                log.error(
                    "[NATIVE_STOP_REPAIR] symbol=%s kind=%s "
                    "readback_unconfirmed candidate=%s resubmit=false",
                    symbol,
                    kind,
                    body["clientOid"],
                )
                return False

            await lifecycle.mark_verified(
                client,
                candidate["slot_key"],
                str(confirmed.get("id") or confirmed.get("orderId") or ""),
            )
            cleanup_ok, superseded, external = (
                await lifecycle.cleanup_superseded(
                    client,
                    symbol,
                    side,
                    kind,
                    body["clientOid"],
                    read_stop_orders,
                )
            )
            log.info(
                "[PROTECTION_READBACK] symbol=%s kind=%s "
                "canonical_client_oid=%s superseded_bgx=%s external=%s "
                "desired_trigger=%s verified_trigger=%s cleanup_status=%s",
                symbol,
                kind,
                body["clientOid"],
                superseded,
                external,
                rounded,
                confirmed.get("stopPrice"),
                "VERIFIED" if cleanup_ok else "UNCONFIRMED",
            )
            if not cleanup_ok:
                return False
            orders = await read_stop_orders(client, symbol)
            if orders is None:
                return False

            log.info(
                "[NATIVE_STOP_REPAIR] symbol=%s kind=%s trigger=%s "
                "closeOrder=true confirmed=true canonical_client_oid=%s "
                "superseded_cleanup=verified",
                symbol,
                kind,
                rounded,
                body["clientOid"],
            )
        return True
    except Exception as exc:
        log.error(
            "[NATIVE_STOP_REPAIR] symbol=%s failed=%s",
            symbol,
            type(exc).__name__,
        )
        return False
