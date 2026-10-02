"""Operator emergency flatten (close-all) — fail-safe and idempotent.

Order of operations (F-001):
  1. FREEZE NEW RISK — pause entries on engine and exchange client (the
     transport refuses every non-reduce POST while paused). The run loop keeps
     running, so stops, reconciliation and event processing continue.
  2. DISCOVER — exchange positions are the authority (local state is never
     taken as proof of flatness). An unreadable payload means UNKNOWN exposure.
  3. CLOSE — one reduce-only order per open position, opposite side, quantity
     from ``emergency_close_quantity`` (explicit units, never above the open
     contracts). Orders go through the normal ``place_order`` chain, so the
     ownership fence applies; there is no emergency bypass.
  4. VERIFY — positions are re-read; success is reported only when the
     exchange shows no remaining exposure for the requested symbols.
  5. The engine is NOT stopped. Entries stay paused until the operator
     resumes, and any remaining exposure stays under normal management.

INV-EMERGENCY-001: a failed flatten never leaves open positions unmanaged.
INV-EMERGENCY-002: flatten(flatten(state)) == flatten(state) for exposure —
reduce-only orders, deterministic clientOid per minute, a per-engine lock and
exchange re-reads before every order.
"""
from __future__ import annotations

import asyncio
import time

from bot.logger import log

_TERMINAL_OK = ("FLAT", "ALREADY_FLAT")


def _summary(status, reason, results, remaining):
    return {
        "status": status,
        "reason": reason,
        "positions_found": len(results),
        "positions_closed": sum(1 for r in results if r["result"] == "CLOSED"),
        "positions_failed": sum(1 for r in results if r["result"] in ("FAILED", "UNKNOWN")),
        "remaining_positions": remaining,
        "results": results,
        "entries_paused": True,
        "position_management": "RUNNING",
    }


def _open_rows(rows):
    if not isinstance(rows, list):
        raise ValueError("positions payload is not a list")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("symbol"):
            raise ValueError("position row malformed")
        size = row.get("size")
        try:
            if float(size) == 0:
                continue
        except (TypeError, ValueError):
            pass  # unparseable size is kept and fails closed downstream
        out.append(row)
    return out


async def close_all_positions(engine, *, reason="operator_close_all",
                              lock_timeout_s=30.0, fill_timeout_s=8.0,
                              verify_attempts=3, verify_delay_s=0.5):
    from bot.conditional_stop_protection import _instrument_info
    from bot.prelive_protection_failclosed import emergency_close_quantity

    engine.pause_entries()
    engine.active = False
    log.critical("[EMERGENCY_FLATTEN_START] reason=%s entries_paused=true "
                 "position_management=RUNNING", reason)

    if getattr(engine, "paper_trade", False):
        result = _summary("FAILED", "paper_mode_exchange_flatten_unavailable", [], [])
        log.critical("[EMERGENCY_FLATTEN_INCOMPLETE] reason=paper_mode exchange_orders=NONE")
        return result

    lock = getattr(engine, "_emergency_flatten_lock", None)
    if lock is None:
        lock = engine._emergency_flatten_lock = asyncio.Lock()
    async with lock:
        pos_lock = getattr(engine, "_pos_lock", None)
        acquired = False
        if pos_lock is not None:
            try:
                await asyncio.wait_for(pos_lock.acquire(), lock_timeout_s)
                acquired = True
            except asyncio.TimeoutError:
                log.critical("[EMERGENCY_FLATTEN_INCOMPLETE] reason=position_lock_busy")
                return _summary("FAILED", "position_lock_busy", [], ["UNKNOWN"])
        try:
            return await _flatten(engine, _instrument_info, emergency_close_quantity,
                                  fill_timeout_s, verify_attempts, verify_delay_s)
        finally:
            if acquired:
                pos_lock.release()


async def _flatten(engine, instrument_info, close_qty, fill_timeout_s,
                   verify_attempts, verify_delay_s):
    client = engine.client
    try:
        rows = _open_rows(await client.get_positions())
    except Exception as exc:
        log.critical("[EMERGENCY_FLATTEN_INCOMPLETE] stage=discover error=%s "
                     "exposure=UNKNOWN", type(exc).__name__)
        return _summary("FAILED", "positions_unconfirmed", [], ["UNKNOWN"])

    if not rows:
        log.critical("[EMERGENCY_FLATTEN_VERIFY] status=ALREADY_FLAT remaining=0")
        return _summary("ALREADY_FLAT", "no_open_positions", [], [])

    counts = {}
    for row in rows:
        counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1

    results, requested = [], []
    for row in rows:
        sym = str(row["symbol"])
        side = str(row.get("side", ""))
        entry = {"symbol": sym, "side": side, "open_qty": row.get("size"),
                 "request_contracts": None, "order_id": "", "client_oid": "",
                 "result": "FAILED", "detail": ""}
        results.append(entry)
        if counts[sym] > 1:
            entry["detail"] = "ambiguous_duplicate_symbol"
            continue
        if side not in ("Buy", "Sell"):
            entry["detail"] = "invalid_side"
            continue
        try:
            info = instrument_info(client, sym) or (getattr(engine, "instruments", None) or {}).get(sym)
            close = close_qty(row, info)
        except Exception as exc:
            entry["detail"] = f"quantity_unresolved:{type(exc).__name__}"
            log.critical("[EMERGENCY_FLATTEN_RESULT] symbol=%s result=FAILED reason=%s "
                         "exchange_dispatch=NONE", sym, entry["detail"])
            continue
        close_side = "Sell" if side == "Buy" else "Buy"
        entry["request_contracts"] = close["request_contracts"]
        idem = (f"emergency_flatten_{sym}_{close_side}_{close['open_contracts']}_"
                f"{int(time.time() // 60)}")
        try:
            entry["client_oid"] = client.build_client_oid(sym, close_side, close["base_qty"], idem)
        except Exception:
            entry["client_oid"] = ""
        log.critical(
            "[EMERGENCY_FLATTEN_ORDER] symbol=%s position_side=%s close_side=%s "
            "open_contracts=%s request_contracts=%s base_qty=%s reduceOnly=true clientOid=%s",
            sym, side, close_side, close["open_contracts"], close["request_contracts"],
            close["base_qty"], entry["client_oid"] or "?",
        )
        requested.append(sym)
        try:
            res = await client.place_order(
                symbol=sym, side=close_side, qty=close["base_qty"], sl=0, tp=0,
                instruments=getattr(engine, "instruments", None), reduce_only=True,
                idem_key=idem, single_submission=True,
            )
        except Exception as exc:
            entry["result"], entry["detail"] = "UNKNOWN", f"submit_error:{type(exc).__name__}"
            continue
        order_id = str((res or {}).get("orderId") or "") if isinstance(res, dict) else ""
        entry["order_id"] = order_id
        if not order_id:
            entry["result"], entry["detail"] = "UNKNOWN", "submission_unconfirmed"
            continue
        entry["result"], entry["detail"] = "SUBMITTED", ""
        try:
            await client.wait_for_fill(order_id, timeout_s=fill_timeout_s)
        except Exception as exc:
            entry["detail"] = f"fill_check_error:{type(exc).__name__}"

    # VERIFY against exchange truth; never infer flatness from submissions.
    remaining = None
    for attempt in range(max(1, verify_attempts)):
        if attempt:
            await asyncio.sleep(verify_delay_s)
        try:
            after = _open_rows(await client.get_positions())
        except Exception as exc:
            log.critical("[EMERGENCY_FLATTEN_INCOMPLETE] stage=verify error=%s exposure=UNKNOWN",
                         type(exc).__name__)
            remaining = None
            continue
        remaining = {str(r["symbol"]): r.get("size") for r in after}
        if not any(s in remaining for s in requested):
            break

    for entry in results:
        sym = entry["symbol"]
        if remaining is None:
            if entry["result"] == "SUBMITTED":
                entry["result"] = "UNKNOWN"
                entry["detail"] = "verify_unavailable"
        elif sym not in remaining:
            entry["result"] = "CLOSED" if sym in requested else entry["result"]
            if entry["result"] == "FAILED" and sym not in requested:
                entry["result"], entry["detail"] = "ALREADY_FLAT", "absent_on_verify"
        else:
            if entry["result"] in ("SUBMITTED", "UNKNOWN"):
                entry["result"] = "FAILED"
                entry["detail"] = entry["detail"] or "still_open_after_close"
        log.critical(
            "[EMERGENCY_FLATTEN_RESULT] symbol=%s side=%s open_qty=%s request_contracts=%s "
            "orderId=%s clientOid=%s result=%s detail=%s",
            sym, entry["side"], entry["open_qty"], entry["request_contracts"],
            entry["order_id"] or "NONE", entry["client_oid"] or "NONE",
            entry["result"], entry["detail"] or "-",
        )

    if remaining is None:
        status, left = "FAILED", ["UNKNOWN"]
    else:
        left = sorted(remaining)
        if not left:
            status = "FLAT"
        elif any(e["result"] == "CLOSED" for e in results):
            status = "PARTIAL_FAILURE"
        else:
            status = "FAILED"
    log.critical("[EMERGENCY_FLATTEN_VERIFY] status=%s remaining=%s", status, left or 0)
    if status not in _TERMINAL_OK:
        log.critical("[EMERGENCY_FLATTEN_INCOMPLETE] status=%s remaining=%s "
                     "entries_paused=true position_management=RUNNING", status, left)
    return _summary(status, "verified" if remaining is not None else "verify_unavailable",
                    results, left)
