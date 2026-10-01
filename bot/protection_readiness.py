"""Canonical protection-system readiness derived from exchange state.

Readiness remains fail-closed and exchange-truth-derived. Lifecycle hardening
may converge durable order state and selectively retire obsolete BGX-owned
conditional protection only after independent exposure evidence is clean.
"""
from __future__ import annotations

import time

from bot.conditional_stop_protection import conditional_stop_confirmed
from bot.logger import log


_FLAT_GC_RETRY_S = 60.0


def _positive(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _inline_stop_equivalent(position) -> bool:
    stop = _positive(position.get("stopLoss", position.get("stop_loss", 0)))
    if stop <= 0:
        return False
    reference = 0.0
    for key in ("markPrice", "mark_price", "entryPrice", "avgEntryPrice"):
        reference = _positive(position.get(key))
        if reference > 0:
            break
    if reference <= 0:
        return False
    side = str(position.get("side", "") or "").strip().lower()
    if side in ("buy", "long"):
        return stop < reference
    if side in ("sell", "short"):
        return stop > reference
    return False


def _live_positions(rows):
    live = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            size = abs(float(row.get("size", 0) or 0))
        except (TypeError, ValueError):
            return None
        if size > 0:
            live.append(row)
    return live


def _configured_symbols(engine, extras=()):
    symbols = {str(symbol) for symbol in extras if str(symbol)}
    viable = getattr(engine, "viable_symbols", None)
    if isinstance(viable, (list, tuple, set)):
        symbols.update(str(symbol) for symbol in viable if str(symbol))
    instruments = getattr(engine, "instruments", None)
    if isinstance(instruments, dict):
        symbols.update(str(symbol) for symbol in instruments if str(symbol))
    return sorted(symbols)


async def _reconcile_global_flat_bgx_protection(engine, symbols) -> bool:
    """Bounded one-shot sweep of configured symbols for legacy BGX stop debt.

    Successful completion is cached for the current global-flat epoch. A failed
    sweep retries no faster than once per minute. Any later live position resets
    the epoch. Per-symbol cleanup still performs owner/fence checks and preserves
    external orders.
    """
    if bool(getattr(engine, "_flat_protection_gc_complete", False)):
        return True

    now = time.monotonic()
    last = float(getattr(engine, "_flat_protection_gc_last", 0.0) or 0.0)
    if last and now - last < _FLAT_GC_RETRY_S:
        return False
    engine._flat_protection_gc_last = now

    from bot.conditional_stop_lifecycle import cleanup_flat_symbol

    checked = 0
    for symbol in symbols:
        checked += 1
        if not await cleanup_flat_symbol(
            engine,
            symbol,
            exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ):
            engine._flat_protection_gc_complete = False
            log.warning(
                "[PROTECTION_FLAT_SWEEP] result=UNCONFIRMED symbol=%s "
                "checked=%s retry_after_s=%s",
                symbol,
                checked,
                int(_FLAT_GC_RETRY_S),
            )
            return False

    engine._flat_protection_gc_complete = True
    log.info(
        "[PROTECTION_FLAT_SWEEP] result=VERIFIED symbols=%s cadence=ON_FLAT_EPOCH",
        checked,
    )
    return True


async def refresh_protection_readiness(engine) -> bool:
    """Refresh canonical readiness and return the derived boolean."""
    engine._protection_system_ready = False
    engine._protection_readiness_evidence = {
        "positions": None,
        "unprotected_positions": None,
        "verified": {},
        "reason": "not_evaluated",
    }

    if not bool(getattr(engine, "connected", False)):
        engine._protection_readiness_evidence["reason"] = "exchange_not_connected"
        return False

    client = getattr(engine, "client", None)
    if client is None:
        engine._protection_readiness_evidence["reason"] = "exchange_client_unavailable"
        return False

    try:
        from bot.durable_live_reconciliation import reconcile_pending
        await reconcile_pending(engine)
    except Exception as exc:
        engine._protection_readiness_evidence["reason"] = (
            "durable_live_reconciliation_failed"
        )
        log.critical(
            "[PROTECTION_READINESS] ready=false stage=durable_live_reconcile error=%s",
            type(exc).__name__,
        )
        return False

    try:
        rows = await client.get_positions()
    except Exception as exc:
        engine._protection_readiness_evidence["reason"] = "positions_read_failed"
        log.critical(
            "[PROTECTION_READINESS] ready=false stage=positions_read error=%s",
            type(exc).__name__,
        )
        return False

    if not isinstance(rows, list):
        engine._protection_readiness_evidence["reason"] = "positions_payload_malformed"
        return False

    live = _live_positions(rows)
    if live is None:
        engine._protection_readiness_evidence["reason"] = "position_size_malformed"
        return False

    if live:
        # A future transition back to global flat must inventory protection debt
        # again exactly once for that new flat epoch.
        engine._flat_protection_gc_complete = False
        engine._flat_protection_gc_last = 0.0

    unprotected = set(getattr(engine, "_unprotected_symbols", set()) or set())
    engine._protection_readiness_evidence["positions"] = len(live)
    engine._protection_readiness_evidence["unprotected_positions"] = len(unprotected)

    def _canon(value):
        symbol = str(value or "")
        return symbol[:-1] if symbol.endswith("M") else symbol

    registry = getattr(engine, "orders", None)
    unresolved_reader = getattr(registry, "unreconciled_filled_orders", None)
    mark_reconciled = getattr(registry, "mark_filled_exposure_reconciled", None)
    if not callable(unresolved_reader) or not callable(mark_reconciled):
        engine._protection_readiness_evidence["reason"] = (
            "durable_fill_reconciliation_authority_unavailable"
        )
        return False

    live_qty = {}
    for row in live:
        canon = _canon(row.get("symbol"))
        live_qty[canon] = live_qty.get(canon, 0.0) + abs(float(row.get("size", 0) or 0))

    unresolved_fills = list(unresolved_reader() or [])
    entry_unresolved = [
        order for order in unresolved_fills
        if str(getattr(order, "exposure_intent", "INCREASE")) == "INCREASE"
    ]
    reduce_unresolved = [
        order for order in unresolved_fills
        if str(getattr(order, "exposure_intent", "INCREASE")) == "REDUCE"
    ]

    reconciled_now = 0
    for order in entry_unresolved:
        if live_qty.get(_canon(getattr(order, "symbol", "")), 0.0) > 0:
            reconciled_now += mark_reconciled(order.symbol)
    for order in reduce_unresolved:
        previous = getattr(order, "previous_position_qty", None)
        current = live_qty.get(_canon(getattr(order, "symbol", "")), 0.0)
        filled = max(0.0, float(getattr(order, "filled_qty", 0.0) or 0.0))
        if previous is not None and filled > 0 and current > 0:
            expected = max(0.0, float(previous) - filled)
            tolerance = max(1e-9, abs(expected) * 1e-6)
            if abs(current - expected) <= tolerance:
                reconciled_now += mark_reconciled(order.symbol)

    if reconciled_now:
        from bot import durable_execution as durable
        if not await durable.persist_orders(
            engine, "exposure_reconciled_from_position_truth", strict=True
        ):
            engine._protection_readiness_evidence["reason"] = (
                "durable_fill_reconciliation_persist_failed"
            )
            return False

    unresolved_fills = list(unresolved_reader() or [])
    unresolved_fill_symbols = {
        _canon(getattr(order, "symbol", "")) for order in unresolved_fills
    }

    if not live:
        pending_reader = getattr(registry, "pending_orders", None)
        if not callable(pending_reader):
            engine._protection_readiness_evidence["reason"] = "flat_pending_orders_unconfirmed"
            return False
        try:
            pending = list(pending_reader() or [])
        except Exception as exc:
            engine._protection_readiness_evidence["reason"] = "flat_pending_orders_read_failed"
            log.critical(
                "[PROTECTION_STATE_RECONCILIATION] decision=KEEP_BLOCKED "
                "reason=pending_orders_read_failed error=%s",
                type(exc).__name__,
            )
            return False

        if getattr(engine, "paper_trade", False):
            # PAPER orders are durable simulator state and never exist on the
            # exchange. Private active-order reads would incorrectly make
            # credentials a prerequisite for PAPER readiness.
            active = []
        else:
            open_orders = getattr(client, "get_open_orders", None)
            try:
                if callable(open_orders):
                    payload = await open_orders()
                else:
                    raw_get = getattr(client, "_get", None)
                    if not callable(raw_get):
                        engine._protection_readiness_evidence["reason"] = (
                            "flat_active_orders_unconfirmed"
                        )
                        return False
                    payload = await raw_get(
                        "/api/v1/orders", {"status": "active"}, auth=True
                    )
            except Exception as exc:
                engine._protection_readiness_evidence["reason"] = (
                    "flat_active_orders_read_failed"
                )
                log.critical(
                    "[PROTECTION_STATE_RECONCILIATION] decision=KEEP_BLOCKED "
                    "reason=active_orders_read_failed error=%s",
                    type(exc).__name__,
                )
                return False
            if isinstance(payload, dict):
                active = payload.get("items")
                if active is None and isinstance(payload.get("data"), list):
                    active = payload.get("data")
            elif isinstance(payload, list):
                active = payload
            else:
                active = None
        if not isinstance(active, list):
            engine._protection_readiness_evidence["reason"] = "flat_active_orders_payload_malformed"
            return False

        pending_symbols = {_canon(getattr(order, "symbol", "")) for order in pending}
        active_entry_symbols = {
            _canon(row.get("symbol"))
            for row in active
            if isinstance(row, dict)
            and row.get("reduceOnly") is not True
            and row.get("closeOrder") is not True
        }
        local_position_symbols = {
            _canon(symbol) for symbol in (getattr(engine, "positions", {}) or {}).keys()
        }

        close_reconciled = 0
        flat_unresolved = list(unresolved_reader() or [])
        for order in flat_unresolved:
            canon = _canon(getattr(order, "symbol", ""))
            if str(getattr(order, "exposure_intent", "INCREASE")) != "REDUCE":
                continue
            previous = getattr(order, "previous_position_qty", None)
            filled = max(0.0, float(getattr(order, "filled_qty", 0.0) or 0.0))
            requested = max(0.0, float(getattr(order, "qty", 0.0) or 0.0))
            known_full_close = (
                previous is not None
                and filled + 1e-9 >= float(previous)
            )

            # Compatibility repair for Binance REDUCE records written before
            # reduce lineage captured previous_position_qty.  This does not
            # infer a position from the order itself.  It is permitted only
            # under stronger exchange truth: the whole account is already
            # confirmed flat (this branch), this is the sole unresolved fill
            # for the symbol, the REDUCE order itself is fully FILLED, and no
            # durable/local/active-entry exposure remains.
            same_symbol_unresolved = [
                candidate
                for candidate in flat_unresolved
                if _canon(getattr(candidate, "symbol", "")) == canon
            ]
            legacy_flat_repair = (
                previous is None
                and requested > 0
                and filled + 1e-9 >= requested
                and len(same_symbol_unresolved) == 1
            )
            if (
                (known_full_close or legacy_flat_repair)
                and canon not in pending_symbols
                and canon not in active_entry_symbols
                and canon not in local_position_symbols
            ):
                if legacy_flat_repair:
                    log.warning(
                        "[PROTECTION_STATE_RECONCILIATION] symbol=%s "
                        "decision=REPAIR_LEGACY_REDUCE_LINEAGE "
                        "exchange_global_flat=true filled_qty=%s requested_qty=%s "
                        "previous_position_qty=UNKNOWN execution_effect=NONE",
                        order.symbol,
                        filled,
                        requested,
                    )
                close_reconciled += mark_reconciled(order.symbol)
        if close_reconciled:
            from bot import durable_execution as durable
            if not await durable.persist_orders(
                engine, "full_close_exposure_reconciled_flat", strict=True
            ):
                engine._protection_readiness_evidence["reason"] = (
                    "durable_fill_reconciliation_persist_failed"
                )
                return False

        unresolved_fills = list(unresolved_reader() or [])
        unresolved_fill_symbols = {
            _canon(getattr(order, "symbol", "")) for order in unresolved_fills
        }

        before = set(unprotected)
        clearable = {
            symbol for symbol in before
            if _canon(symbol) not in pending_symbols
            and _canon(symbol) not in active_entry_symbols
            and _canon(symbol) not in local_position_symbols
            and _canon(symbol) not in unresolved_fill_symbols
        }

        # Legacy stop debt may exist after restart even when no local incident is
        # marked. Sweep configured symbols only after GLOBAL flatness is proven by
        # all independent exposure authorities. This is not a readiness bypass.
        global_flat_for_gc = (
            not pending
            and not active_entry_symbols
            and not local_position_symbols
            and not unresolved_fill_symbols
        )
        global_gc_verified = False
        if global_flat_for_gc:
            sweep_symbols = _configured_symbols(engine, extras=before)
            global_gc_verified = await _reconcile_global_flat_bgx_protection(
                engine, sweep_symbols
            )
            if not global_gc_verified:
                engine._protection_readiness_evidence["reason"] = (
                    "flat_bgx_protection_inventory_unconfirmed"
                )
                return False

        # When the global sweep is not yet eligible (for example another symbol
        # has a pending intent), preserve the older per-symbol convergence for
        # independently clearable stale incidents.
        gc_verified = set()
        if clearable and not global_gc_verified:
            from bot.conditional_stop_lifecycle import cleanup_flat_symbol
            for symbol in sorted(clearable):
                if await cleanup_flat_symbol(
                    engine,
                    symbol,
                    exchange_position_qty=0.0,
                    active_entry_confirmed_absent=True,
                ):
                    gc_verified.add(symbol)
                else:
                    log.warning(
                        "[PROTECTION_STATE_RECONCILIATION] symbol=%s "
                        "decision=KEEP_BLOCKED "
                        "reason=obsolete_bgx_protection_cleanup_unconfirmed",
                        symbol,
                    )
        elif global_gc_verified:
            gc_verified = set(clearable)
        clearable = gc_verified

        if clearable:
            engine._unprotected_symbols = before - clearable
            unprotected = set(engine._unprotected_symbols)

        for symbol in sorted(before):
            canon = _canon(symbol)
            if symbol in clearable:
                decision, reason = (
                    "CLEAR_STALE",
                    "confirmed_symbol_flat_and_bgx_cleanup",
                )
            elif canon in pending_symbols:
                decision, reason = "KEEP_BLOCKED", "pending_durable_intent"
            elif canon in active_entry_symbols:
                decision, reason = "KEEP_BLOCKED", "active_exchange_entry"
            elif canon in unresolved_fill_symbols:
                decision, reason = (
                    "KEEP_BLOCKED",
                    "durable_fill_exposure_not_reconciled",
                )
            elif canon not in local_position_symbols:
                decision, reason = (
                    "KEEP_BLOCKED",
                    "obsolete_bgx_protection_cleanup_unconfirmed",
                )
            else:
                decision, reason = (
                    "KEEP_BLOCKED",
                    "local_filled_position_awaiting_exchange_convergence",
                )
            log.warning(
                "[PROTECTION_STATE_RECONCILIATION] symbol=%s position_qty=0 "
                "exchange_position_qty=0 active_entry_orders=%s durable_pending=%s "
                "local_materialized_position=%s durable_fill_unreconciled=%s "
                "exposure_reconciliation_complete=%s unprotected_before=true "
                "unprotected_after=%s decision=%s reason=%s",
                symbol,
                int(canon in active_entry_symbols),
                int(canon in pending_symbols),
                int(canon in local_position_symbols),
                int(canon in unresolved_fill_symbols),
                str(canon not in unresolved_fill_symbols).lower(),
                str(symbol in unprotected).lower(),
                decision,
                reason,
            )

        ready = len(unprotected) == 0
        engine._protection_system_ready = ready
        engine._protection_readiness_evidence["unprotected_positions"] = len(unprotected)
        engine._protection_readiness_evidence["reason"] = (
            "confirmed_flat_no_pending_exposure_and_bgx_cleanup"
            if ready else "flat_with_unresolved_exposure_evidence"
        )
        return ready

    verified = {}
    for row in live:
        symbol = str(row.get("symbol", "") or "")
        if not symbol:
            engine._protection_readiness_evidence["reason"] = "position_symbol_missing"
            return False

        try:
            protected, evidence = await conditional_stop_confirmed(client, row)
        except Exception as exc:
            engine._protection_readiness_evidence["reason"] = "protection_readback_failed"
            log.critical(
                "[PROTECTION_READINESS] ready=false symbol=%s stage=readback error=%s",
                symbol,
                type(exc).__name__,
            )
            return False

        if protected and evidence == "inline_stop":
            protected = _inline_stop_equivalent(row)
            if not protected:
                evidence = "inline_stop_mismatch"

        verified[symbol] = {
            "protected": bool(protected),
            "evidence": str(evidence),
        }
        if not protected:
            engine._protection_readiness_evidence["verified"] = verified
            engine._protection_readiness_evidence["reason"] = (
                f"protection_unverified:{symbol}:{evidence}"
            )
            log.critical(
                "[PROTECTION_READINESS] ready=false symbol=%s evidence=%s "
                "http_success_is_not_readback=true",
                symbol,
                evidence,
            )
            return False

    live_symbols = {str(row.get("symbol", "") or "") for row in live}
    unresolved_live = sorted(live_symbols.intersection(unprotected))
    if unresolved_live:
        engine._protection_readiness_evidence["verified"] = verified
        engine._protection_readiness_evidence["reason"] = "unresolved_unprotected_symbols"
        log.critical(
            "[PROTECTION_READINESS] ready=false unresolved=%s",
            ",".join(unresolved_live),
        )
        return False

    engine._protection_readiness_evidence["verified"] = verified
    engine._protection_readiness_evidence["reason"] = "all_live_positions_readback_verified"
    engine._protection_system_ready = True
    log.info(
        "[PROTECTION_READINESS] ready=true positions=%s unprotected_positions=%s "
        "basis=EXCHANGE_READBACK "
        "invariant=PROTECTION_READINESS_MUST_BE_DERIVED_FROM_EXCHANGE_STATE",
        len(live),
        len(unprotected),
    )
    return True
