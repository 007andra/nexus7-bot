"""Fail-closed post-entry protection for Binance USD-M LIVE positions.

PAPER is untouched. In real controlled-pilot mode, only BGX-owned local
positions may be repaired or safety-closed. External/manual positions remain
read-only under pilot_external_position_guard.
"""
from __future__ import annotations

from bot.conditional_stop_protection import conditional_stop_confirmed


def install(TradingEngine, log) -> None:
    if getattr(TradingEngine, "_binance_protection_failclosed_patched", False):
        return

    original_open = TradingEngine._open
    original_guard = TradingEngine._guard_naked_positions

    def _external_symbols(engine) -> set[str]:
        return set(getattr(engine, "_external_position_symbols", set()) or set())

    def _owned_locally(engine, symbol: str) -> bool:
        return bool(
            symbol
            and symbol in (getattr(engine, "positions", {}) or {})
            and symbol not in _external_symbols(engine)
        )

    def _mark_unprotected(engine, symbol: str) -> None:
        current = set(getattr(engine, "_unprotected_symbols", set()) or set())
        current.add(symbol)
        engine._unprotected_symbols = current

    def _clear_unprotected(engine, symbol: str) -> None:
        current = set(getattr(engine, "_unprotected_symbols", set()) or set())
        current.discard(symbol)
        engine._unprotected_symbols = current

    def _durable_block(engine, symbol: str, reason: str) -> None:
        _mark_unprotected(engine, symbol)
        try:
            from bot import durable_execution as durable

            if getattr(engine, "_durable_state_enforced", False):
                durable._block(engine, "orders")
        except Exception as exc:
            log.error(
                "[BINANCE_PROTECTION] symbol=%s durable_block_failed=%s "
                "reason=%s",
                symbol,
                type(exc).__name__,
                reason,
            )

    async def _live_position(engine, symbol: str):
        rows = await engine.client.get_positions()
        return next(
            (
                row
                for row in rows
                if row.get("symbol") == symbol
                and abs(float(row.get("size", 0) or 0)) > 0
            ),
            None,
        )

    async def _confirm(engine, position: dict) -> tuple[bool, str]:
        return await conditional_stop_confirmed(engine.client, position)

    async def _repair(
        engine,
        symbol: str,
        *,
        sl: float,
        tp: float,
    ) -> tuple[bool, str]:
        if sl <= 0:
            return False, "missing_local_stop_target"
        try:
            submitted = await engine.client.set_position_stops(
                symbol,
                sl=float(sl),
                tp=max(0.0, float(tp or 0)),
            )
        except Exception as exc:
            return False, f"repair_submit_{type(exc).__name__}"
        if not submitted:
            return False, "repair_submit_unconfirmed"

        try:
            live = await _live_position(engine, symbol)
        except Exception as exc:
            return False, f"repair_read_{type(exc).__name__}"
        if live is None:
            return True, "position_flat_after_repair"
        protected, evidence = await _confirm(engine, live)
        if protected:
            return True, f"repair_confirmed:{evidence}"
        return False, f"repair_not_confirmed:{evidence}"

    async def _safety_close(engine, position: dict) -> tuple[bool, str]:
        symbol = str(position.get("symbol") or "")
        if not _owned_locally(engine, symbol):
            return False, "ownership_unconfirmed_no_mutation"

        side = str(position.get("side") or "")
        try:
            qty = abs(float(position.get("size", 0) or 0))
        except (TypeError, ValueError):
            return False, "invalid_exchange_quantity"
        if qty <= 0 or side not in {"Buy", "Sell"}:
            return False, "invalid_exchange_position"

        try:
            result = await engine.client.place_order(
                symbol=symbol,
                side="Sell" if side == "Buy" else "Buy",
                qty=qty,
                sl=0,
                tp=0,
                instruments=engine.instruments,
                reduce_only=True,
                single_submission=True,
            )
        except Exception as exc:
            return False, f"close_submit_{type(exc).__name__}"

        order_id = str(
            result.get("orderId") if isinstance(result, dict) else ""
        )
        if not order_id:
            return False, "close_order_id_missing"

        try:
            fill = await engine.client.wait_for_fill(order_id, timeout_s=8.0)
        except Exception as exc:
            return False, f"close_fill_{type(exc).__name__}"

        try:
            live = await _live_position(engine, symbol)
        except Exception as exc:
            return False, f"close_flat_read_{type(exc).__name__}"

        if bool(fill.get("filled")) and live is None:
            engine.positions.pop(symbol, None)
            _clear_unprotected(engine, symbol)
            log.critical(
                "[BINANCE_PROTECTION_CLOSE] symbol=%s orderId=%s "
                "fill_confirmed=true exchange_flat=true result=PASS",
                symbol,
                order_id,
            )
            return True, "fill_confirmed_exchange_flat"

        return False, (
            "close_unconfirmed:"
            f"filled={bool(fill.get('filled'))}:flat={live is None}"
        )

    async def _enforce(
        engine,
        symbol: str,
        *,
        preferred_sl: float = 0,
        preferred_tp: float = 0,
        allow_close: bool = True,
    ) -> bool:
        if not _owned_locally(engine, symbol):
            return False

        try:
            live = await _live_position(engine, symbol)
        except Exception as exc:
            _durable_block(engine, symbol, "position_read_unconfirmed")
            log.critical(
                "[BINANCE_PROTECTION] symbol=%s result=BLOCK "
                "stage=POSITION_READ error=%s mutation=false",
                symbol,
                type(exc).__name__,
            )
            return False

        if live is None:
            _clear_unprotected(engine, symbol)
            return True

        protected, evidence = await _confirm(engine, live)
        if protected:
            _clear_unprotected(engine, symbol)
            log.info(
                "[BINANCE_PROTECTION] symbol=%s result=PASS evidence=%s",
                symbol,
                evidence,
            )
            return True

        local = (getattr(engine, "positions", {}) or {}).get(symbol)
        sl = float(preferred_sl or getattr(local, "sl", 0) or 0)
        tp = float(preferred_tp or getattr(local, "tp", 0) or 0)
        repaired, repair_evidence = await _repair(
            engine, symbol, sl=sl, tp=tp
        )
        if repaired:
            _clear_unprotected(engine, symbol)
            log.warning(
                "[BINANCE_PROTECTION] symbol=%s result=REPAIRED "
                "evidence=%s",
                symbol,
                repair_evidence,
            )
            return True

        if allow_close:
            closed, close_evidence = await _safety_close(engine, live)
            if closed:
                return True
        else:
            close_evidence = "close_disabled"

        _durable_block(engine, symbol, "protection_unconfirmed")
        log.critical(
            "[BINANCE_PROTECTION] symbol=%s result=BLOCK "
            "initial=%s repair=%s close=%s "
            "entries_blocked=true",
            symbol,
            evidence,
            repair_evidence,
            close_evidence,
        )
        return False

    async def _open_with_protection(self, sig, *args, **kwargs):
        result = await original_open(self, sig, *args, **kwargs)
        if getattr(self, "paper_trade", False):
            return result

        pilot = getattr(self, "pilot", None)
        if pilot is None or not getattr(pilot, "enabled", False):
            return result
        if getattr(self, "_validation_safety_lock_active", False):
            return result

        symbol = str(getattr(sig, "symbol", "") or "")
        if symbol in _external_symbols(self):
            _durable_block(self, symbol, "post_open_external_conflict")
            log.critical(
                "[BINANCE_PROTECTION_POSTOPEN] symbol=%s result=BLOCK "
                "reason=external_ownership_conflict mutation=false",
                symbol,
            )
            return result

        if not _owned_locally(self, symbol):
            # A real position may exist even if local creation failed (for
            # example an ambiguous submit that Binance filled). Only the exact
            # durable BGX lineage proof may adopt and protect it.
            from bot import ambiguous_entry_recovery

            await ambiguous_entry_recovery.recover_unadopted_entries(
                self, symbol=symbol
            )
            if _owned_locally(self, symbol):
                return result
            try:
                live = await _live_position(self, symbol)
            except Exception:
                live = None
            if live is not None:
                _durable_block(self, symbol, "post_open_local_state_missing")
                log.critical(
                    "[BINANCE_PROTECTION_POSTOPEN] symbol=%s result=BLOCK "
                    "reason=local_ownership_state_missing mutation=false",
                    symbol,
                )
                if not ambiguous_entry_recovery._unresolved_entry_intents(self, symbol):
                    await ambiguous_entry_recovery.alert_unattributed_exposure(
                        self, symbol, "post_open_local_state_missing"
                    )
            return result

        await _enforce(
            self,
            symbol,
            preferred_sl=float(getattr(sig, "sl", 0) or 0),
            preferred_tp=float(getattr(sig, "tp", 0) or 0),
            allow_close=True,
        )
        return result

    async def _guard_with_conditional_protection(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_guard(self, *args, **kwargs)

        try:
            rows = await self.client.get_positions()
        except Exception as exc:
            log.warning(
                "[BINANCE_PROTECTION_GUARD] result=UNCONFIRMED "
                "error=%s mutation=false",
                type(exc).__name__,
            )
            return None

        external = _external_symbols(self)
        for position in rows or []:
            symbol = str(position.get("symbol") or "")
            try:
                size = abs(float(position.get("size", 0) or 0))
            except (TypeError, ValueError):
                continue
            if size <= 0 or not symbol:
                continue
            if symbol in external or symbol not in self.positions:
                continue
            await _enforce(self, symbol, allow_close=True)
        return None

    TradingEngine._open = _open_with_protection
    TradingEngine._guard_naked_positions = _guard_with_conditional_protection
    # Shared enforcement authority for exposure adopted with exact BGX lineage
    # (ambiguous_entry_recovery); same repair / confirmed-close policy.
    TradingEngine._bgx_enforce_owned_protection = staticmethod(_enforce)
    TradingEngine._binance_protection_failclosed_patched = True
    log.warning(
        "[BINANCE_PROTECTION_FAIL_CLOSED] installed=true "
        "conditional_stop_readback=true repair=true "
        "emergency_close_requires_fill_and_flat=true "
        "external_positions_immutable=true"
    )
