"""Fail-closed guard for unexpected/manual exchange positions.

Invariant
---------
Any real exchange position whose BGX ownership cannot be proven exactly is
external/manual and therefore read-only.

At startup, a surviving position may be reassociated only through the stronger
read-only restart proof: one unique durable BGX FILLED order, independently
confirmed by the active exchange with matching orderId/clientOid/symbol/side/base quantity,
and currently valid native protection. Symbol similarity alone is never enough.

External positions may be observed, protection-checked and counted toward
exposure, but must never be auto-adopted, have stops changed, be reduced or be
closed by BGX. An unprotected external position blocks new entries; a protected
external may coexist only under the existing capacity policy.
"""

from bot.conditional_stop_protection import conditional_stop_confirmed, _to_base_size, _instrument_info
from bot.restart_ownership_recovery import prove_restart_ownership


def install(TradingEngine, log, exchange_name: str = "kucoin"):
    if getattr(TradingEngine, "_pilot_external_position_guard_patched", False):
        return

    # Shadow position forensics currently contains KuCoin-specific transport
    # assumptions. The ownership boundary below is exchange-neutral and is
    # installed for Binance without importing those KuCoin hooks.
    if str(exchange_name).lower() == "kucoin":
        from bot import kucoin as _kucoin
        from bot import shadow_position_forensics as _shadow_position_forensics
        _shadow_position_forensics.install(_kucoin, log)

    original_guard = getattr(TradingEngine, "_guard_naked_positions", None)
    original_sync = getattr(TradingEngine, "_sync_positions", None)
    original_reconcile = getattr(TradingEngine, "_reconcile_exchange_positions", None)
    original_load = getattr(TradingEngine, "_load_existing_positions", None)

    def _real_exchange_mode(engine):
        return not getattr(engine, "paper_trade", False)

    async def _unexpected_positions(engine):
        try:
            rows = await engine.client.get_positions()
        except Exception as exc:
            log.critical(
                "[EXTERNAL_POSITION_IMMUTABLE] result=BLOCKED "
                "reason=position_read_failed error=%s action=no_mutation",
                type(exc).__name__,
            )
            setattr(engine, "_pilot_external_position_guard_blocked", True)
            return None

        local = set(getattr(engine, "positions", {}) or {})
        explicit_external = set(getattr(engine, "_external_position_symbols", set()) or set())
        unexpected = {}
        evidence = {}
        for row in rows or []:
            try:
                size = abs(float(row.get("size", 0) or 0))
            except (AttributeError, TypeError, ValueError):
                size = 0.0
            sym = str(row.get("symbol", "") or "") if isinstance(row, dict) else ""
            if size <= 0 or not sym:
                continue
            # Explicit EXTERNAL ownership always wins over local presence. This
            # closes the startup-sync hole that caused a manual position to be
            # adopted and emergency-closed in the historical P0 incident.
            if sym in local and sym not in explicit_external:
                owned = engine.positions[sym]
                base_size = _to_base_size(size, row.get("sizeUnit", "CONTRACTS"),
                                          _instrument_info(engine.client, sym))
                local_size = abs(float(getattr(owned, "qty", 0) or 0))
                exchange_side = str(row.get("side", "")).upper()
                exchange_direction = {"BUY": "LONG", "SELL": "SHORT"}.get(exchange_side, exchange_side)
                same_direction = exchange_direction == str(getattr(owned, "direction", "")).upper()
                # Legitimate partial exits can decrease size. An increase or
                # reversal has no ownership proof merely because symbol matches.
                if (base_size > 0 and local_size > 0 and same_direction
                        and base_size <= local_size + max(1e-12, local_size * 1e-9)):
                    continue
                explicit_external.add(sym)
                engine._external_position_symbols = explicit_external
                engine.positions.pop(sym, None)
                log.critical(
                    "[EXTERNAL_POSITION_IMMUTABLE] symbol=%s reason=ownership_quantity_or_side_changed "
                    "local_qty=%s exchange_base_qty=%s action=quarantine_read_only_no_mutation",
                    sym, local_size, base_size,
                )

            protected, source = await conditional_stop_confirmed(engine.client, row)
            unexpected[sym] = bool(unexpected.get(sym, False) or protected)
            if protected:
                evidence[sym] = source

        unprotected = sorted(sym for sym, protected in unexpected.items() if not protected)
        protected = sorted(sym for sym, is_protected in unexpected.items() if is_protected)

        unprotected_set = getattr(engine, "_unprotected_symbols", None)
        if isinstance(unprotected_set, set):
            unprotected_set.update(unprotected)
            for sym in protected:
                unprotected_set.discard(sym)

        blocked = bool(unprotected)
        setattr(engine, "_pilot_external_position_guard_blocked", blocked)

        if unprotected:
            log.critical(
                "[EXTERNAL_POSITION_IMMUTABLE] result=BLOCKED "
                "reason=unexpected_unprotected_position symbols=%s "
                "action=read_only_no_adopt_no_stop_change_no_reduce_no_close",
                ",".join(unprotected),
            )
        if protected:
            evidence_text = ",".join(
                f"{sym}:{evidence.get(sym, 'unknown')}" for sym in protected
            )
            log.warning(
                "[EXTERNAL_POSITION_IMMUTABLE] result=PASS_WITH_EXTERNAL_PROTECTED "
                "symbols=%s protection=%s "
                "action=read_only_no_adopt_no_stop_change_no_reduce_no_close "
                "capacity_effect=count_slot",
                ",".join(protected), evidence_text,
            )

        return {"all": sorted(unexpected), "protected": protected, "unprotected": unprotected}

    # P0 ownership boundary: startup exchange positions are EXTERNAL by default.
    # A symbol is allowed to survive the legacy loader only when the dedicated
    # restart proof independently establishes exact durable + exchange ownership.
    if original_load is not None:
        async def _load_existing_failclosed(self, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_load(self, *args, **kwargs)
            try:
                rows = await self.client.get_positions()
            except Exception as exc:
                self._external_position_symbols = set()
                self._recovered_position_symbols = set()
                self._pilot_external_position_guard_blocked = True
                log.critical(
                    "[EXTERNAL_POSITION_OWNERSHIP] result=BLOCKED reason=preload_read_failed "
                    "error=%s action=no_adopt_no_mutation",
                    type(exc).__name__,
                )
                return None

            live_rows = []
            row_counts = {}
            for row in rows or []:
                try:
                    size = abs(float(row.get("size", 0) or 0))
                    sym = str(row.get("symbol", "") or "")
                except (AttributeError, TypeError, ValueError):
                    continue
                if size <= 0 or not sym:
                    continue
                live_rows.append(row)
                row_counts[sym] = row_counts.get(sym, 0) + 1

            external = set()
            recovered = set()
            proofs = {}
            for row in live_rows:
                sym = str(row.get("symbol", "") or "")
                if row_counts.get(sym, 0) != 1:
                    external.add(sym)
                    log.critical(
                        "[RESTART_OWNERSHIP] symbol=%s result=REJECTED "
                        "reason=multiple_exchange_position_rows action=EXTERNAL_READ_ONLY",
                        sym,
                    )
                    continue

                proof = await prove_restart_ownership(self, row)
                proofs[sym] = proof
                if proof.recovered:
                    recovered.add(sym)
                    log.warning(
                        "[RESTART_OWNERSHIP] symbol=%s result=RECOVERED "
                        "reason=%s clientOid=%s orderId=%s qty=%s protection=%s "
                        "execution_effect=NONE",
                        sym,
                        proof.reason,
                        proof.client_oid[:16],
                        proof.order_id[:16],
                        proof.base_qty,
                        proof.protection,
                    )
                else:
                    external.add(sym)
                    log.warning(
                        "[RESTART_OWNERSHIP] symbol=%s result=REJECTED reason=%s "
                        "action=EXTERNAL_READ_ONLY execution_effect=NONE",
                        sym,
                        proof.reason,
                    )

            self._external_position_symbols = external
            self._recovered_position_symbols = recovered
            self._restart_ownership_proofs = proofs

            result = await original_load(self, *args, **kwargs)

            # The legacy loader performs a fresh exchange read. Verify its local
            # reconstruction still matches the quantity/side that was proven.
            # Any time-of-check/time-of-use divergence is demoted to EXTERNAL.
            for sym in list(recovered):
                proof = proofs.get(sym)
                local = self.positions.get(sym)
                try:
                    local_qty = abs(float(getattr(local, "qty", 0) or 0))
                    local_side = str(getattr(local, "direction", "") or "").upper()
                    expected_side = "LONG" if proof and proof.side == "Buy" else "SHORT"
                    qty_ok = bool(
                        proof
                        and abs(local_qty - proof.base_qty)
                        <= max(1e-12, proof.base_qty * 1e-9)
                    )
                    side_ok = bool(local and local_side == expected_side)
                except (TypeError, ValueError):
                    qty_ok = False
                    side_ok = False
                if not qty_ok or not side_ok:
                    recovered.discard(sym)
                    external.add(sym)
                    self.positions.pop(sym, None)
                    self._trade_ids.pop(sym, None)
                    log.critical(
                        "[RESTART_OWNERSHIP] symbol=%s result=DEMOTED_EXTERNAL "
                        "reason=postload_proof_divergence action=no_adopt_no_mutation",
                        sym,
                    )

            for sym in external:
                self.positions.pop(sym, None)
                self._trade_ids.pop(sym, None)

            self._external_position_symbols = external
            self._recovered_position_symbols = recovered

            if recovered:
                log.warning(
                    "[EXTERNAL_POSITION_OWNERSHIP] classified=BGX_RECOVERED symbols=%s "
                    "basis=exact_durable_exchange_proof",
                    ",".join(sorted(recovered)),
                )
            if external:
                log.critical(
                    "[EXTERNAL_POSITION_OWNERSHIP] classified=EXTERNAL symbols=%s "
                    "action=read_only_no_adopt_no_stop_change_no_reduce_no_close",
                    ",".join(sorted(external)),
                )
            return result
        TradingEngine._load_existing_positions = _load_existing_failclosed

    if original_guard is not None:
        async def _guard_failclosed(self, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_guard(self, *args, **kwargs)
            state = await _unexpected_positions(self)
            if state is None or (state["all"] and not self.positions):
                return None
            return await original_guard(self, *args, **kwargs)
        TradingEngine._guard_naked_positions = _guard_failclosed

    if original_sync is not None:
        async def _sync_failclosed(self, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_sync(self, *args, **kwargs)
            state = await _unexpected_positions(self)
            if state is None or (state["all"] and not self.positions):
                return None
            return await original_sync(self, *args, **kwargs)
        TradingEngine._sync_positions = _sync_failclosed

    if original_reconcile is not None:
        async def _reconcile_failclosed(self, only_symbol=None, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_reconcile(self, only_symbol=only_symbol, *args, **kwargs)

            # Symbol-scoped recovery is permitted only for a symbol that was not
            # classified external at startup. An external symbol can never use
            # this escape hatch.
            external = set(getattr(self, "_external_position_symbols", set()) or set())
            if only_symbol:
                if only_symbol in external:
                    log.critical(
                        "[EXTERNAL_POSITION_IMMUTABLE] symbol=%s scoped_reconcile=BLOCKED "
                        "action=no_adopt_no_mutation",
                        only_symbol,
                    )
                    return None
                return await original_reconcile(self, only_symbol=only_symbol, *args, **kwargs)

            state = await _unexpected_positions(self)
            if state is None:
                current = getattr(self, "_unprotected_symbols", set())
                return list(current) if isinstance(current, set) else []
            if state["all"]:
                return list(state["unprotected"])
            return await original_reconcile(self, only_symbol=only_symbol, *args, **kwargs)
        TradingEngine._reconcile_exchange_positions = _reconcile_failclosed

    TradingEngine._pilot_external_position_guard_patched = True
    log.warning(
        "[EXTERNAL_POSITION_IMMUTABLE] installed exchange=%s: startup positions "
        "default to EXTERNAL/read-only; only exact durable+exchange+protection "
        "proof may recover BGX ownership; no heuristic adoption; unprotected "
        "externals block",
        str(exchange_name).lower(),
    )
