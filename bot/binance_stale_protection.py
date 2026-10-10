"""P1-OPEN-1 — stale Binance protective algo orders after a BGX trade closes.

Binance does not document that a ``closePosition`` STOP_MARKET /
TAKE_PROFIT_MARKET algo order is cancelled when the position goes flat. When
trade A's SL fills, its sibling TP stays ``NEW`` on ``/fapi/v1/openAlgoOrders``
and can close trade B (same symbol) at A's level. This module retires such
leftovers under the rule:

  POSITION CONFIRMED FLAT -> enumerate active algo orders of the symbol
  -> ownership proven ONLY by the durable record "clientAlgoId -> opening
     lineage" written before the algo POST (``binance_protection_registry``)
  -> cancel each owned order of a CLOSED lineage individually
     (DELETE /fapi/v1/algoOrder) -> read back /fapi/v1/openAlgoOrders
  -> VERIFIED only when no closed-lineage order is still active and nothing
     unmapped / external remains.

The ``bgx7-`` prefix only means "possibly BGX": an unmapped bgx7 order is never
cancelled and never yields VERIFIED (UNRESOLVED_UNMAPPED_BGX_PROTECTION). A
non-BGX (manual / external) order is never cancelled and never yields VERIFIED
on a flat symbol (UNRESOLVED_EXTERNAL_PROTECTION). An unreadable inventory is
UNRESOLVED_INVENTORY_UNKNOWN with zero cancels. Symbol, side, type and trigger
price are never used to select an order for cancellation.
"""
from __future__ import annotations

from bot import binance_protection_registry as registry
from bot.logger import log

BGX_PREFIXES = ("bgx7-", "bgx-stop-")

VERIFIED = "VERIFIED"
UNRESOLVED_INVENTORY_UNKNOWN = "UNRESOLVED_INVENTORY_UNKNOWN"
UNRESOLVED_CURRENT_LINEAGE_UNKNOWN = "UNRESOLVED_CURRENT_LINEAGE_UNKNOWN"
UNRESOLVED_UNMAPPED_BGX_PROTECTION = "UNRESOLVED_UNMAPPED_BGX_PROTECTION"
UNRESOLVED_EXTERNAL_PROTECTION = "UNRESOLVED_EXTERNAL_PROTECTION"
CANCEL_UNCONFIRMED = "CANCEL_UNCONFIRMED"


def possibly_bgx(order) -> bool:
    """Prefix recognition only — never proof of ownership."""
    return isinstance(order, dict) and str(order.get("clientOid") or "").startswith(BGX_PREFIXES)


def _symbol(value) -> str:
    return str(value or "").upper()


def _active_rows(rows, symbol: str):
    return [row for row in rows if isinstance(row, dict)
            and _symbol(row.get("symbol")) == _symbol(symbol)
            and row.get("isActive") is True]


def current_lineage(client, symbol: str) -> dict:
    return {
        "opening_order_id": str((getattr(client, "_protection_opening_order", {}) or {}).get(symbol) or ""),
        "opening_client_oid": str((getattr(client, "_protection_lineage", {}) or {}).get(symbol) or ""),
    }


def owned_by(record: dict, lineage: dict) -> bool:
    """Strong lineage: the exchange opening orderId decides whenever both sides
    know it (a clientOid can repeat across trades); clientOid only as fallback."""
    if not isinstance(record, dict) or not lineage:
        return False
    rec_oid, cur_oid = str(record.get("opening_order_id") or ""), lineage.get("opening_order_id", "")
    if rec_oid and cur_oid:
        return rec_oid == cur_oid
    rec_coid, cur_coid = str(record.get("opening_client_oid") or ""), lineage.get("opening_client_oid", "")
    return bool(rec_coid and cur_coid) and rec_coid == cur_coid


async def _read(client, symbol: str):
    try:
        rows = await client.get_stop_orders(symbol)
    except Exception as exc:
        log.warning("[BINANCE_STALE_PROTECTION] symbol=%s inventory=UNKNOWN error=%s",
                    symbol, type(exc).__name__)
        return None
    if not isinstance(rows, list):
        log.warning("[BINANCE_STALE_PROTECTION] symbol=%s inventory=UNKNOWN error=malformed", symbol)
        return None
    return rows


def _log_order(tag, symbol, row, record, *, previous_status, cancel_result, final_status, strong):
    log.warning(
        "[%s] symbol=%s opening_order_id=%s algo_id=%s client_oid=%s type=%s "
        "previous_status=%s cancel_result=%s final_status=%s strong_lineage_match=%s",
        tag, symbol, (record or {}).get("opening_order_id", "") or "-",
        row.get("orderId", "") or "-", row.get("clientOid", "") or "-",
        row.get("type", "") or "-", previous_status, cancel_result, final_status,
        str(bool(strong)).lower(),
    )


async def reconcile_symbol(client, symbol: str, *, flat_proven: bool) -> str:
    """Retire owned protection of closed lineages on ``symbol``.

    ``flat_proven=True``: the caller holds authoritative proof that the symbol
    has no position; every mapped lineage is closed. ``flat_proven=False``: a
    position is open and only orders of a lineage different from the CURRENT,
    known one are stale; with no known current lineage nothing is cancelled.
    """
    if flat_proven:
        live = {}
    else:
        live = current_lineage(client, symbol)
        if not any(live.values()):
            log.warning("[FLAT_PROTECTION_CLEANUP] symbol=%s status=%s cancels=0",
                        symbol, UNRESOLVED_CURRENT_LINEAGE_UNKNOWN)
            return UNRESOLVED_CURRENT_LINEAGE_UNKNOWN

    rows = await _read(client, symbol)
    if rows is None:
        log.warning("[FLAT_PROTECTION_CLEANUP] symbol=%s status=%s cancels=0",
                    symbol, UNRESOLVED_INVENTORY_UNKNOWN)
        return UNRESOLVED_INVENTORY_UNKNOWN
    active = _active_rows(rows, symbol)
    if any(possibly_bgx(row) for row in active):
        await registry.load(client)        # unreadable -> unmapped -> unresolved

    stale, kept, unmapped, external = [], [], [], []
    for row in active:
        if not possibly_bgx(row):
            external.append(row)
            _log_order("BINANCE_STALE_PROTECTION", symbol, row, None, previous_status="ACTIVE",
                       cancel_result="NOT_ATTEMPTED_EXTERNAL", final_status="PRESERVED", strong=False)
            continue
        record = registry.lookup(client, str(row.get("clientOid") or ""))
        if (record is None or _symbol(record.get("symbol")) != _symbol(symbol)
                or not registry.lineage_token(record)):
            unmapped.append(row)
            _log_order("BINANCE_STALE_PROTECTION", symbol, row, None, previous_status="ACTIVE",
                       cancel_result="NOT_ATTEMPTED_UNMAPPED", final_status="UNRESOLVED", strong=False)
            continue
        if owned_by(record, live):
            kept.append(row)
            continue
        stale.append((row, record))
        _log_order("BINANCE_STALE_PROTECTION", symbol, row, record, previous_status="ACTIVE",
                   cancel_result="PENDING", final_status="STALE_CLOSED_LINEAGE", strong=True)

    results = {}
    for row, record in stale:
        results[str(row.get("clientOid"))] = await client.cancel_algo_order(
            symbol, algo_id=str(row.get("orderId") or ""), client_algo_id=str(row.get("clientOid") or ""))

    remaining = []
    if stale:
        after = await _read(client, symbol)
        if after is None:
            log.warning("[FLAT_PROTECTION_CLEANUP] symbol=%s status=%s stale=%s readback=UNKNOWN",
                        symbol, CANCEL_UNCONFIRMED, len(stale))
            return CANCEL_UNCONFIRMED
        still = {str(row.get("clientOid") or "") for row in _active_rows(after, symbol)}
        for row, record in stale:
            cid = str(row.get("clientOid") or "")
            if cid in still:
                remaining.append(cid)
            _log_order("BINANCE_ALGO_CANCEL_READBACK", symbol, row, record, previous_status="ACTIVE",
                       cancel_result=results.get(cid, "-"),
                       final_status="STILL_ACTIVE" if cid in still else "NOT_ACTIVE", strong=True)
        await registry.forget(client, [str(row.get("clientOid")) for row, _ in stale
                                       if str(row.get("clientOid")) not in still])

    if remaining:
        status = CANCEL_UNCONFIRMED
    elif unmapped:
        status = UNRESOLVED_UNMAPPED_BGX_PROTECTION
    elif external and flat_proven:
        status = UNRESOLVED_EXTERNAL_PROTECTION
    else:
        status = VERIFIED
    if status == VERIFIED and flat_proven:
        # Flat and clean: every record of this symbol belongs to a closed
        # lineage whose orders are no longer active -> bound the durable map.
        active_ids = {str(row.get("clientOid") or "") for row in active}
        done = [cid for cid, rec in (getattr(client, "_algo_registry", {}) or {}).items()
                if isinstance(rec, dict) and _symbol(rec.get("symbol")) == _symbol(symbol)
                and cid not in active_ids]
        if done:
            await registry.forget(client, done)
    log.warning(
        "[FLAT_PROTECTION_CLEANUP] symbol=%s flat_proven=%s stale=%s cancelled=%s "
        "still_active=%s kept_current=%s unmapped=%s external_preserved=%s status=%s",
        symbol, str(bool(flat_proven)).lower(), len(stale), len(stale) - len(remaining),
        len(remaining), len(kept), len(unmapped), len(external), status,
    )
    return status


async def assert_clean_before_entry(client, symbol: str) -> None:
    """Pre-dispatch gate: a new opening order may only reach Binance when the
    symbol is proven flat AND no owned protection of a closed lineage remains."""
    position = await client._active_position_for_symbol(symbol)   # raises -> blocked
    if position:
        return                                  # not an opening on a flat symbol
    status = await reconcile_symbol(client, symbol, flat_proven=True)
    if status != VERIFIED:
        raise RuntimeError(
            f"READY_FOR_NEW_ENTRIES=false: stale_protection_cleanup={status} symbol={symbol}")
