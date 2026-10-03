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
F-001A: after verified flatness, close/reduce-only stop orders left on flat
symbols are cancelled and re-verified (step 4b); leftovers keep resume blocked.
NOVO-01: a malformed row never freezes the reduction of validly identified
positions. Valid rows are closed, UNKNOWN rows are never touched, a finite
second pass closes rows that became valid, and the status stays non-success
(UNKNOWN_REMAINS / PARTIAL_FAILURE) while any UNKNOWN exposure remains.
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
        "stale_protections_found": 0,
        "stale_protections_cancelled": 0,
        "stale_protections_failed": 0,
        "remaining_stale_protections": [],
        "stale_protection_scan_skipped": [],
    }


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


async def _terminalize_lineages(engine, rows, view=None):
    try:
        from bot import trade_lifecycle
        positions = getattr(engine, "positions", {}) or {}
        if view is not None:
            # NOVO-01: only lineages whose symbol is PROVEN flat; an UNKNOWN or
            # unidentified row keeps every possibly-matching lineage OPEN.
            positions = {s: p for s, p in positions.items() if view.flat_proven(s)}
        await trade_lifecycle.terminalize_positions(positions, rows, "emergency_flatten")
    except Exception as exc:
        log.error("[TRADE_LINEAGE_TERMINAL_FAILED] stage=emergency_flatten error=%s", type(exc).__name__)


async def _read(client, stage):
    from bot.position_snapshot import read_for_risk_reduction
    view = await read_for_risk_reduction(client, source=f"emergency_flatten.{stage}")
    if not view.readable:
        log.critical("[EMERGENCY_FLATTEN_INCOMPLETE] stage=%s error=positions_unreadable "
                     "exposure=UNKNOWN", stage)
        return None
    for symbol in view.unknown_symbols:
        log.critical("[RISK_REDUCTION_UNKNOWN_SKIPPED] stage=%s symbol=%s reason=row_rejected "
                     "mutation=NONE assumed=POSSIBLY_OPEN", stage, symbol)
    if view.unidentified_rows:
        log.critical("[RISK_REDUCTION_UNKNOWN_SKIPPED] stage=%s symbol=UNIDENTIFIED rows=%s "
                     "mutation=NONE flat_proof=NONE", stage, view.unidentified_rows)
    return view


async def _close_rows(engine, rows, instrument_info, close_qty, fill_timeout_s,
                      results, requested, partial):
    client = engine.client
    counts = {}
    for row in rows:
        counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1
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
        if partial:
            log.critical("[RISK_REDUCTION_VALID_ROW] symbol=%s side=%s size=%s action=reduce_only_close "
                         "snapshot_authority=PARTIAL_INVALID", sym, side, row.get("size"))
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


async def _verify(client, requested, attempts, delay_s):
    """Re-read until requested symbols are gone and the read is authoritative."""
    view = None
    for attempt in range(max(1, attempts)):
        if attempt:
            await asyncio.sleep(delay_s)
        view = await _read(client, "verify")
        if view is None:
            continue
        if not any(s in view.symbols() or s in view.unknown_symbols for s in requested) \
                and view.authoritative:
            break
    return view


async def _flatten(engine, instrument_info, close_qty, fill_timeout_s,
                   verify_attempts, verify_delay_s, max_passes=2):
    client = engine.client
    view = await _read(client, "discover")
    if view is None:
        return _summary("FAILED", "positions_unconfirmed", [], ["UNKNOWN"])

    if view.authoritative and not view.rows:
        log.critical("[EMERGENCY_FLATTEN_VERIFY] status=ALREADY_FLAT remaining=0")
        await _terminalize_lineages(engine, [])
        summary = _summary("ALREADY_FLAT", "no_open_positions", [], [])
        await _retire_stale_protections(engine, summary, set())
        return summary

    results, requested = [], []
    after = view
    for pass_no in range(max(1, max_passes)):
        handled = {e["symbol"] for e in results}
        todo = [r for r in view.rows if r["symbol"] not in handled]
        if not todo and pass_no:
            break
        await _close_rows(engine, todo, instrument_info, close_qty, fill_timeout_s,
                          results, requested, not view.authoritative)
        # VERIFY against exchange truth; never infer flatness from submissions.
        after = await _verify(client, requested, verify_attempts, verify_delay_s)
        if after is None:
            break
        # Second pass (finite): a row that was UNKNOWN may now be valid.
        view = after
        if not [r for r in after.rows if r["symbol"] not in {e["symbol"] for e in results}]:
            break
        log.critical("[EMERGENCY_FLATTEN_PARTIAL_AUTHORITY] pass=%s newly_valid=%s action=second_pass",
                     pass_no + 1, ",".join(sorted(after.symbols() - {e["symbol"] for e in results})))

    if after is not None:
        await _terminalize_lineages(engine, after.rows, after)   # NOVO-02 + NOVO-01

    still = set(after.symbols()) if after is not None else set()
    unknown = set(after.unknown_symbols) if after is not None else set()
    for entry in results:
        sym = entry["symbol"]
        if after is None:
            if entry["result"] == "SUBMITTED":
                entry["result"] = "UNKNOWN"
                entry["detail"] = "verify_unavailable"
        elif sym in unknown or (sym not in still and not after.flat_proven(sym)):
            if entry["result"] in ("SUBMITTED", "FAILED"):
                entry["result"] = "UNKNOWN"
            entry["detail"] = entry["detail"] or "flat_unproven_partial_snapshot"
        elif sym not in still:
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

    unidentified = after.unidentified_rows if after is not None else 0
    if after is None:
        status, left = "FAILED", ["UNKNOWN"]
    else:
        left = sorted(still | unknown) + (["UNIDENTIFIED"] if unidentified else [])
        if not left:
            status = "FLAT"
        elif still and any(e["result"] == "CLOSED" for e in results):
            status = "PARTIAL_FAILURE"
        elif still:
            status = "FAILED"
        else:
            status = "UNKNOWN_REMAINS"    # valid exposure reduced; UNKNOWN != FLAT
    if after is not None and not after.authoritative:
        log.critical(
            "[EMERGENCY_FLATTEN_PARTIAL_AUTHORITY] valid_symbols=%s unknown_symbols=%s "
            "unidentified_rows=%s actions_sent=%s remaining_unknown=%s status=%s",
            ",".join(sorted({e["symbol"] for e in results})) or "NONE",
            ",".join(sorted(unknown)) or "NONE", unidentified, len(requested),
            ",".join(sorted(unknown) + (["UNIDENTIFIED"] if unidentified else [])) or "NONE",
            status,
        )
    log.critical("[EMERGENCY_FLATTEN_VERIFY] status=%s remaining=%s", status, left or 0)
    summary = _summary(status, "verified" if after is not None else "verify_unavailable",
                       results, left)
    summary.update({
        "snapshot_state": after.state if after is not None else "READ_FAILED",
        "unknown_symbols": sorted(unknown),
        "unidentified_rows": unidentified,
    })
    if after is not None:
        if unidentified:
            # No symbol is provably flat: never retire protection (F-001A).
            summary["stale_protection_scan_skipped"] = ["UNIDENTIFIED_POSITION_ROWS"]
            log.critical("[EMERGENCY_PROTECTION_SCAN] skipped=unidentified_position_rows")
        else:
            await _retire_stale_protections(engine, summary, still | unknown)
    if summary["status"] not in _TERMINAL_OK:
        log.critical("[EMERGENCY_FLATTEN_INCOMPLETE] status=%s remaining=%s "
                     "remaining_stale_protections=%s entries_paused=true "
                     "position_management=RUNNING", summary["status"], left,
                     summary.get("remaining_stale_protections"))
    return summary


def _closer(order) -> bool:
    return isinstance(order, dict) and (
        order.get("closeOrder") is True or order.get("reduceOnly") is True
    )


async def _retire_stale_protections(engine, summary, open_symbols):
    """Cancel close/reduce-only stops left on symbols VERIFIED flat (F-001A).

    INV-FLAT-STATE-001 / INV-PROTECTION-OWNERSHIP-001: a closeOrder stop on a
    flat symbol protects nothing and would act on the next position of that
    symbol. Runs only after exchange flatness is verified, never while the
    symbol has a position or a non-terminal intent; open positions keep their
    protection. Cancels go through the owner-validated lifecycle path.
    """
    from bot.conditional_stop_lifecycle import _active, _cancel_order
    from bot.conditional_stop_protection import read_stop_orders

    symbols = set(getattr(engine, "instruments", None) or {})
    symbols.update(r["symbol"] for r in summary["results"])
    registry = getattr(engine, "orders", None)
    pending = {getattr(o, "symbol", "") for o in (registry.pending_orders() if registry else [])}
    found = cancelled = failed = 0
    stale_left, skipped = [], []
    for sym in sorted(symbols):
        if sym in open_symbols:
            continue            # still open: its protection must stay
        if sym in pending:
            skipped.append(sym)
            continue
        before = await read_stop_orders(engine.client, sym)
        if before is None:
            stale_left.append(f"{sym}:UNREADABLE")
            continue
        stale = [o for o in before if _active(o) and _closer(o)]
        found += len(stale)
        for order in stale:
            oid = str(order.get("id") or order.get("orderId") or "")
            ok = await _cancel_order(engine.client, oid)
            log.critical(
                "[EMERGENCY_PROTECTION_CANCEL] symbol=%s orderId=%s clientOid=%s side=%s "
                "stop=%s stopPrice=%s closeOrder=%s result=%s",
                sym, oid or "NONE", order.get("clientOid") or "NONE", order.get("side"),
                order.get("stop"), order.get("stopPrice"), order.get("closeOrder") is True,
                "REQUESTED" if ok else "UNCONFIRMED",
            )
        if not stale:
            continue
        after = await read_stop_orders(engine.client, sym)
        if after is None:
            stale_left.append(f"{sym}:UNREADABLE")
            failed += len(stale)
            continue
        left = {str(o.get("id") or o.get("orderId") or "") for o in after if _active(o) and _closer(o)}
        for order in stale:
            oid = str(order.get("id") or order.get("orderId") or "")
            if oid in left:
                failed += 1
                stale_left.append(f"{sym}:{oid}")
            else:
                cancelled += 1
        log.critical("[EMERGENCY_PROTECTION_VERIFY] symbol=%s stale_before=%s stale_after=%s",
                     sym, len(stale), len(left))
    summary.update({
        "stale_protections_found": found,
        "stale_protections_cancelled": cancelled,
        "stale_protections_failed": failed,
        "remaining_stale_protections": stale_left,
        "stale_protection_scan_skipped": skipped,
    })
    pending_cleanup = {s.split(":", 1)[0] for s in stale_left} | set(skipped)
    engine._stale_protection_pending = pending_cleanup
    log.critical("[EMERGENCY_PROTECTION_SCAN] found=%s cancelled=%s failed=%s remaining=%s "
                 "skipped=%s", found, cancelled, failed, stale_left, skipped)
    if pending_cleanup:
        log.critical("[EMERGENCY_PROTECTION_STALE] symbols=%s entries_paused=true "
                     "resume_blocked=true", sorted(pending_cleanup))
        if summary["status"] in _TERMINAL_OK:
            summary["status"] = "FLAT_BUT_PROTECTION_CLEANUP_FAILED"
