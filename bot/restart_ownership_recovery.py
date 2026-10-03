"""Read-only proof of BGX position ownership after a process restart.

A live exchange position is managed again only when one unique durable BGX
FILLED order explains the current exposure and the exchange independently
confirms the same order identity/fill. Symbol/side/size similarity alone is
never sufficient. Any missing, ambiguous or conflicting evidence fails closed
and leaves the position EXTERNAL/read-only.

This module contains no exchange mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from bot.conditional_stop_protection import conditional_stop_confirmed
from bot.logger import log
from bot.quantity import contracts_to_base, quantity_rules


@dataclass(frozen=True)
class OwnershipProof:
    recovered: bool
    reason: str
    symbol: str = ""
    client_oid: str = ""
    order_id: str = ""
    side: str = ""
    base_qty: float = 0.0
    protection: str = ""


def _normalized_symbol(raw: str) -> str:
    sym = str(raw or "").upper()
    if sym == "XBTUSDTM":
        return "BTCUSDT"
    if sym.endswith("USDTM"):
        return f"{sym[:-5]}USDT"
    return sym


def _normalized_side(raw: str) -> str:
    side = str(raw or "").strip().lower()
    if side in {"buy", "long"}:
        return "Buy"
    if side in {"sell", "short"}:
        return "Sell"
    return ""


def _instrument_info(client, symbol: str):
    standard = _normalized_symbol(symbol)
    getter = getattr(client, "get_instruments", None)
    if callable(getter):
        try:
            instruments = getter() or {}
        except Exception:
            instruments = {}
        if isinstance(instruments, dict):
            info = instruments.get(standard)
            if isinstance(info, dict):
                return info
    instruments = getattr(client, "_instruments", None)
    if isinstance(instruments, dict):
        info = instruments.get(standard)
        if isinstance(info, dict):
            return info
    return None


def _base_lot(info) -> float:
    try:
        multiplier, lot, _, _ = quantity_rules(info)
        return float(multiplier * lot)
    except (KeyError, TypeError, ValueError):
        return 0.0


def _same_base_qty(left: float, right: float, info) -> bool:
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(a) and math.isfinite(b)) or a <= 0 or b <= 0:
        return False
    lot = _base_lot(info)
    if lot <= 0:
        return False
    tolerance = max(1e-12, lot * 1e-9)
    return abs(a - b) <= tolerance


def _position_base_qty(position: dict, info) -> float:
    if not isinstance(position, dict):
        return 0.0
    try:
        raw = abs(float(position.get("size", 0) or 0))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(raw) or raw <= 0:
        return 0.0
    if str(position.get("sizeUnit", "") or "").upper() == "BASE_ASSET":
        return raw
    try:
        return float(contracts_to_base(raw, info))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _filled_base_qty(status: dict, info) -> float:
    if not isinstance(status, dict):
        return 0.0
    raw = status.get("filledSize", status.get("dealSize", 0))
    try:
        return float(contracts_to_base(raw, info))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _durable_fill_matches_position(record: dict, position_qty: float, info) -> bool:
    """Validate durable filled_qty across the historic KuCoin unit boundary.

    ManagedOrder.qty has always represented engine/base-asset quantity. Older
    KuCoin REST fill transitions, however, persisted ``filledSize`` directly,
    which is a contract count. Modern/test records may already contain base
    quantity. Accept either representation only when it resolves *exactly* to
    the same base quantity already proven by record.qty and the live position.

    This is unit reconciliation, not heuristic ownership adoption: a mismatch,
    invalid value, or missing instrument metadata still fails closed.
    """
    if not isinstance(record, dict):
        return False
    try:
        raw = float(record.get("filled_qty", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(raw) or raw < 0:
        return False
    if raw == 0:
        return True

    # Base-unit durable records remain valid without conversion.
    if _same_base_qty(raw, position_qty, info):
        return True

    # Backward compatibility for KuCoin records that stored filledSize in
    # contracts. Conversion must land on the exact same base quantity.
    try:
        normalized = float(contracts_to_base(raw, info))
    except (KeyError, TypeError, ValueError):
        return False
    return _same_base_qty(normalized, position_qty, info)


def _durable_fill_is_partial(record: dict, requested_qty: float, info) -> bool:
    """Durable filled qty (modern base units only) strictly between 0 and the
    requested qty. Historic contract-unit records keep the exact conversion
    rule: they are never reinterpreted as partial fills."""
    try:
        raw = float(record.get("filled_qty", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(raw) or raw <= 0:
        return False
    return raw < requested_qty - max(1e-12, _base_lot(info) * 1e-9)


def _reject(reason: str, *, symbol: str = "") -> OwnershipProof:
    log.warning("[RESTART_OWNERSHIP_REJECTED] symbol=%s reason=%s action=EXTERNAL_READ_ONLY",
                symbol or "?", reason)
    return OwnershipProof(False, reason, symbol=symbol)


async def _fills_continuity(client, symbol, side, opening_order_id, opened_at_ms, position_qty, info):
    """Replay the exchange fill ledger of ``symbol`` since the lineage opened.

    Authoritative continuity proof: only the opening order may add exposure;
    the running balance must never return to zero (that would be a close),
    and the final balance must equal the present position. Returns a
    rejection reason or ``None``. Unreadable/incomplete ledger -> reject.
    """
    import time

    from bot.accounting_fill_link import fills
    from bot.kucoin import to_kucoin
    now_ms = int(time.time() * 1000)
    # opened_at_ms is stamped after the fill was confirmed; widen the window and
    # start the replay at the first fill of the opening order itself.
    start = int(opened_at_ms) - 120_000
    if not 0 <= now_ms - start <= 7 * 86400000:
        return "fills_window_unsupported"
    try:
        ledger = await fills(client, {"symbol": to_kucoin(symbol), "openTime": start, "closeTime": now_ms})
    except Exception:
        return "fills_ledger_unconfirmed"
    direction = "buy" if side == "Buy" else "sell"
    first = next((i for i, f in enumerate(ledger) if str(f.get("orderId")) == str(opening_order_id)), None)
    if first is None:
        return "opening_fill_missing_in_ledger"
    balance, opened = 0.0, False
    for fill in ledger[first:]:
        try:
            size = float(contracts_to_base(fill["size"], info))
        except (KeyError, TypeError, ValueError):
            return "fills_ledger_malformed"
        if fill.get("side") == direction:
            if str(fill.get("orderId")) != str(opening_order_id):
                return "exposure_added_after_open"
            balance += size
            opened = True
        elif fill.get("side") in ("buy", "sell"):
            if not opened:
                return "fills_ledger_order_invalid"
            balance -= size
            if balance <= max(1e-12, position_qty * 1e-9):
                return "lineage_flat_in_fills"
        else:
            return "fills_ledger_malformed"
    if not opened:
        return "opening_fill_missing_in_ledger"
    if not _same_base_qty(balance, position_qty, info):
        return "fills_residual_mismatch"
    return None


async def prove_restart_ownership(engine, position: dict) -> OwnershipProof:
    """Prove one startup position belongs to BGX using only read-only evidence."""
    if not isinstance(position, dict):
        return _reject("invalid_position")

    symbol = _normalized_symbol(position.get("symbol"))
    side = _normalized_side(position.get("side"))
    if not symbol or not side:
        return _reject("invalid_position_identity", symbol=symbol)

    info = _instrument_info(getattr(engine, "client", None), symbol)
    if not isinstance(info, dict) or _base_lot(info) <= 0:
        return _reject("instrument_metadata_unconfirmed", symbol=symbol)

    position_qty = _position_base_qty(position, info)
    if position_qty <= 0:
        return _reject("position_quantity_unconfirmed", symbol=symbol)

    registry = getattr(engine, "orders", None)
    snapshot_fn = getattr(registry, "snapshot", None)
    if not callable(snapshot_fn):
        return _reject("durable_registry_unavailable", symbol=symbol)
    try:
        records = snapshot_fn() or []
    except Exception:
        return _reject("durable_registry_unreadable", symbol=symbol)

    # NOVO-02 ownership equation. A FILLED BGX opening record only proves that
    # BGX opened a trade in the past. The present position is that trade only
    # when its durable lifecycle is OPEN and the exchange quantity equals the
    # opening fill minus the reductions proven to come from BGX orders
    # (INV-OWNERSHIP-OPEN-001, INV-OWNERSHIP-QTY-001). A missing, corrupt or
    # CLOSED lifecycle never grants ownership.
    from bot import trade_lifecycle
    candidates = []
    cumulative = []          # F-013A: lineages whose exposure needs cumulative-fill proof
    rejections = []
    for record in records:
        if not isinstance(record, dict):
            continue
        client_oid = str(record.get("client_oid") or "")
        order_id = str(record.get("order_id") or "")
        if not client_oid.startswith("bgx7-") or not order_id:
            continue
        if str(record.get("state") or "") != "FILLED":
            continue
        if _normalized_symbol(record.get("symbol")) != symbol:
            continue
        if _normalized_side(record.get("side")) != side:
            continue
        try:
            durable_qty = float(record.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(durable_qty) or durable_qty <= 0:
            continue
        partial = False
        if not _durable_fill_matches_position(record, durable_qty, info):
            # F-013A: a durable fill BELOW the requested qty (partial so far)
            # can only be proven by the cumulative-fill proof below.
            if not _durable_fill_is_partial(record, durable_qty, info):
                continue
            partial = True
        try:
            lifecycle = await trade_lifecycle.load(order_id)
        except Exception:
            rejections.append("trade_lifecycle_unreadable")
            continue
        if lifecycle is None:
            rejections.append("trade_lifecycle_missing")
            continue
        if lifecycle["status"] != trade_lifecycle.OPEN:
            rejections.append("trade_lifecycle_closed")
            continue
        if (_normalized_symbol(lifecycle["symbol"]) != symbol
                or ("Buy" if lifecycle["direction"] == "LONG" else "Sell") != side):
            rejections.append("trade_lifecycle_lineage_mismatch")
            continue
        if partial:
            cumulative.append((record, lifecycle, "trade_lifecycle_lineage_mismatch"))
            rejections.append("trade_lifecycle_lineage_mismatch")
            continue
        if not _same_base_qty(lifecycle["opening_qty"], durable_qty, info):
            # F-013A: a partially filled opening order (opening_qty below the
            # requested qty) can only be proven from its CUMULATIVE fills.
            if float(lifecycle["opening_qty"]) < durable_qty:
                cumulative.append((record, lifecycle, "trade_lifecycle_lineage_mismatch"))
            rejections.append("trade_lifecycle_lineage_mismatch")
            continue
        # Rejection-only continuity evidence: a reopened position has its own
        # average entry and a newer exchange opening time. Neither can PROVE
        # ownership (price can coincide); both can disprove it.
        try:
            live_entry = float(position.get("entryPrice", position.get("avgPrice", 0)) or 0)
        except (TypeError, ValueError):
            live_entry = 0.0
        expected = trade_lifecycle.expected_remaining(lifecycle)
        if not live_entry or abs(live_entry - lifecycle["entry"]) > lifecycle["entry"] * 1e-3:
            if position_qty > expected:     # late fills change the average entry
                cumulative.append((record, lifecycle, "trade_entry_mismatch"))
            rejections.append("trade_entry_mismatch")
            continue
        try:
            opened_on_exchange = float(position.get("openingTimestamp") or 0)
        except (TypeError, ValueError):
            opened_on_exchange = 0.0
        if opened_on_exchange and lifecycle.get("opened_at_ms") and \
                opened_on_exchange > float(lifecycle["opened_at_ms"]) + 60_000:
            rejections.append("position_reopened_after_lineage")
            continue
        if not _same_base_qty(expected, position_qty, info):
            if position_qty > expected:
                cumulative.append((record, lifecycle, "trade_residual_mismatch"))
            rejections.append("trade_residual_mismatch")
            log.warning(
                "[TRADE_RESIDUAL_MISMATCH] symbol=%s side=%s opening_order_id=%s trade_status=OPEN "
                "opening_qty=%s confirmed_reduced_qty=%s expected_remaining=%s exchange_qty=%s",
                symbol, side, order_id[:16], lifecycle["opening_qty"],
                lifecycle["confirmed_reduced_qty"], expected, position_qty,
            )
            continue
        candidates.append(record)

    expected_fill_qty = float(candidates[0].get("qty", 0) or 0) if len(candidates) == 1 else 0.0
    reason = "exact_durable_exchange_proof"

    if not candidates and len(cumulative) == 1:
        return await _cumulative_fill_proof(engine, symbol, side, position, position_qty, info,
                                            *cumulative[0])
    if not candidates:
        return _reject(rejections[0] if len(set(rejections)) == 1 else
                       ("no_exact_durable_fill" if not rejections else "no_open_lineage_match"),
                       symbol=symbol)
    if len(candidates) != 1:
        return _reject("ambiguous_durable_fills", symbol=symbol)

    candidate = candidates[0]
    client_oid = str(candidate["client_oid"])
    order_id = str(candidate["order_id"])
    lifecycle = await trade_lifecycle.load(order_id)
    continuity = await _fills_continuity(
        getattr(engine, "client", None), symbol, side, order_id,
        lifecycle.get("opened_at_ms") or 0, position_qty, info)
    if continuity:
        return _reject(continuity, symbol=symbol)
    getter = getattr(getattr(engine, "client", None), "get_order_status", None)
    if not callable(getter):
        return _reject("exchange_order_reader_unavailable", symbol=symbol)
    try:
        status = await getter(order_id)
    except Exception:
        return _reject("exchange_order_read_failed", symbol=symbol)
    if not isinstance(status, dict) or status.get("_unknown") or status.get("_synthetic"):
        return _reject("exchange_order_unconfirmed", symbol=symbol)

    status_order_id = str(status.get("orderId") or status.get("id") or "")
    status_client_oid = str(status.get("clientOid") or "")
    if status_order_id != order_id:
        return _reject("exchange_order_id_mismatch", symbol=symbol)
    if status_client_oid != client_oid:
        return _reject("exchange_client_oid_mismatch", symbol=symbol)
    if _normalized_symbol(status.get("symbol")) != symbol:
        return _reject("exchange_symbol_mismatch", symbol=symbol)
    if _normalized_side(status.get("side")) != side:
        return _reject("exchange_side_mismatch", symbol=symbol)
    if status.get("isActive") is not False:
        return _reject("exchange_order_not_terminal", symbol=symbol)
    if status.get("cancelExist") is True:
        return _reject("exchange_order_cancelled", symbol=symbol)

    filled_qty = _filled_base_qty(status, info)
    if not _same_base_qty(filled_qty, expected_fill_qty, info):
        return _reject("exchange_fill_quantity_mismatch", symbol=symbol)

    protected, protection = await conditional_stop_confirmed(engine.client, position)
    if not protected:
        return _reject("protection_unconfirmed", symbol=symbol)

    log.warning(
        "[RESTART_OWNERSHIP_ACCEPTED] symbol=%s side=%s opening_order_id=%s trade_status=OPEN "
        "exchange_qty=%s reason=%s", symbol, side, order_id[:16], position_qty, reason,
    )
    return OwnershipProof(
        True,
        reason,
        symbol=symbol,
        client_oid=client_oid,
        order_id=order_id,
        side=side,
        base_qty=position_qty,
        protection=protection,
    )


async def _cumulative_fill_proof(engine, symbol, side, position, position_qty, info, record, lifecycle,
                                 exact_reason):
    """F-013A: exposure explained by the CUMULATIVE fills of the same opening
    order (late fills during downtime, or a partial fill of a larger request).

    Same identity evidence as the exact proof (orderId, clientOid, symbol,
    side, OPEN lifecycle, fills continuity, confirmed protection), plus:
      exchange cumulative filled qty - BGX-proven reductions == exchange qty,
      opening_qty <= cumulative filled qty <= requested qty,
      VWAP of the opening order's ledger fills == exchange average entry.
    The ownership is proven; the GEOMETRY is not: the durable exit geometry
    confirmed for a smaller entry is not restored as CONFIRMED (Q-01B compare)
    and the post-fill reconciliation re-proves VWAP/stop/risk from the ledger.
    Read-only: no exchange or durable mutation here."""
    from bot.accounting_fill_link import fills
    from bot.kucoin import to_kucoin
    import time
    client = getattr(engine, "client", None)
    client_oid, order_id = str(record["client_oid"]), str(record["order_id"])
    getter = getattr(client, "get_order_status", None)
    if not callable(getter):
        return _reject("exchange_order_reader_unavailable", symbol=symbol)
    try:
        status = await getter(order_id)
    except Exception:
        return _reject("exchange_order_read_failed", symbol=symbol)
    if not isinstance(status, dict) or status.get("_unknown") or status.get("_synthetic"):
        return _reject("exchange_order_unconfirmed", symbol=symbol)
    if str(status.get("orderId") or status.get("id") or "") != order_id:
        return _reject("exchange_order_id_mismatch", symbol=symbol)
    if str(status.get("clientOid") or "") != client_oid:
        return _reject("exchange_client_oid_mismatch", symbol=symbol)
    if _normalized_symbol(status.get("symbol")) != symbol:
        return _reject("exchange_symbol_mismatch", symbol=symbol)
    if _normalized_side(status.get("side")) != side:
        return _reject("exchange_side_mismatch", symbol=symbol)
    filled = _filled_base_qty(status, info)
    requested = float(record.get("qty", 0) or 0)
    reduced = float(lifecycle["confirmed_reduced_qty"])
    tolerance = max(1e-12, _base_lot(info) * 1e-9)
    if not (filled > 0 and float(lifecycle["opening_qty"]) <= filled + tolerance
            and filled <= requested + tolerance
            and _same_base_qty(filled - reduced, position_qty, info)):
        log.warning("[TRADE_RESIDUAL_MISMATCH] symbol=%s side=%s opening_order_id=%s "
                    "cumulative_filled=%s requested=%s confirmed_reduced_qty=%s exchange_qty=%s "
                    "proof=cumulative_fill", symbol, side, order_id[:16], filled, requested,
                    reduced, position_qty)
        return _reject(exact_reason, symbol=symbol)
    continuity = await _fills_continuity(client, symbol, side, order_id,
                                         lifecycle.get("opened_at_ms") or 0, position_qty, info)
    if continuity:
        return _reject(continuity, symbol=symbol)
    try:
        now_ms = int(time.time() * 1000)
        ledger = await fills(client, {"symbol": to_kucoin(symbol),
                                      "openTime": int(lifecycle.get("opened_at_ms") or 0) - 120_000,
                                      "closeTime": now_ms})
    except Exception:
        return _reject("fills_ledger_unconfirmed", symbol=symbol)
    seen, notional, contracts = set(), 0.0, 0.0
    for fill in ledger or []:
        token = str(fill.get("tradeId") or "")
        if str(fill.get("orderId")) != order_id or not token or token in seen:
            continue
        seen.add(token)
        try:
            notional += float(fill["price"]) * float(fill["size"])
            contracts += float(fill["size"])
        except (KeyError, TypeError, ValueError):
            return _reject("fills_ledger_malformed", symbol=symbol)
    try:
        ledger_qty = float(contracts_to_base(contracts, info)) if contracts > 0 else 0.0
        live_entry = float(position.get("entryPrice", position.get("avgPrice", 0)) or 0)
    except (KeyError, TypeError, ValueError):
        return _reject("fills_ledger_malformed", symbol=symbol)
    if not _same_base_qty(ledger_qty, filled, info):
        return _reject("fills_ledger_incomplete_for_opening_order", symbol=symbol)
    vwap = notional / contracts
    if not live_entry or abs(live_entry - vwap) > vwap * 1e-3:
        return _reject("trade_entry_mismatch", symbol=symbol)
    protected, protection = await conditional_stop_confirmed(engine.client, position)
    if not protected:
        return _reject("protection_unconfirmed", symbol=symbol)
    log.warning(
        "[RESTART_OWNERSHIP_ACCEPTED] symbol=%s side=%s opening_order_id=%s trade_status=OPEN "
        "exchange_qty=%s reason=cumulative_entry_fill_proof opening_qty=%s cumulative_filled=%s "
        "vwap=%.10g geometry=REQUIRES_POSTFILL_RECHECK", symbol, side, order_id[:16], position_qty,
        lifecycle["opening_qty"], filled, vwap)
    return OwnershipProof(True, "cumulative_entry_fill_proof", symbol=symbol, client_oid=client_oid,
                          order_id=order_id, side=side, base_qty=position_qty, protection=protection)
