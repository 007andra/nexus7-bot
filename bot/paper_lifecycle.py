"""PAPER-only position lifecycle.

The live engine reconciles positions against the exchange. In PAPER mode that is
incorrect because simulated positions deliberately do not exist at KuCoin.
This patch keeps PAPER positions internal, marks them to current market prices,
simulates SL/TP exits, and keeps the live integrity/exchange path untouched.
"""
from __future__ import annotations

import time
from datetime import datetime


def install(log):
    import bot.engine as engine_module
    from bot.engine import TradingEngine, Trade
    from bot.config import cfg
    from bot.exchange import TAKER_FEE
    from bot import database as db
    from bot import durable_execution as durable
    from bot.notifier import notify, close_msg
    from bot.integrity import IntegrityGuard, IntegrityState, Severity

    if getattr(TradingEngine, "_paper_lifecycle_patched", False):
        return

    original_sync = TradingEngine._sync_positions
    original_init = TradingEngine.__init__
    original_assess = IntegrityGuard.assess
    original_exit_check = TradingEngine._check_stagnation_and_invalidation

    def init_with_paper_stop_sim(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if not self.paper_trade:
            return

        client = self.client
        if getattr(client, "_paper_stop_sim_patched", False):
            return

        client._real_set_sl = getattr(client, "set_sl", None)
        client._real_set_position_stops = getattr(client, "set_position_stops", None)

        async def _paper_set_sl(symbol: str, sl: float):
            log.info("[PAPER] set_sl simulated OK: %s -> %.6f", symbol, sl)
            return True

        async def _paper_set_position_stops(symbol: str, sl: float = 0, tp: float = 0):
            log.info(
                "[PAPER] set_position_stops simulated OK: %s SL=%.6f TP=%.6f",
                symbol, sl, tp,
            )
            return True

        client.set_sl = _paper_set_sl
        client.set_position_stops = _paper_set_position_stops
        client._paper_stop_sim_patched = True
        log.info("🧪 PAPER stops: acknowledgements simulados; nenhuma mutação na exchange")

    async def assess_with_paper_semantics(self, client, engine):
        if not getattr(engine, "paper_trade", False):
            return await original_assess(self, client, engine)

        # PAPER capital is the durable virtual wallet. Integrity must not
        # require authenticated exchange balance reads merely to validate a
        # simulator that cannot mutate exchange state.
        original_log_state = self._log_state
        original_get_balance = getattr(client, "get_balance", None)
        paper_balance = float(
            getattr(engine, "_paper_balance", getattr(engine.risk, "balance", 0.0))
            or 0.0
        )

        async def _paper_balance():
            return paper_balance

        if callable(original_get_balance):
            client.get_balance = _paper_balance
        self._log_state = lambda: None
        try:
            state = await original_assess(self, client, engine)
        finally:
            self._log_state = original_log_state
            if callable(original_get_balance):
                client.get_balance = original_get_balance

        kept = []
        removed = []
        paper_balance = float(
            getattr(engine, "_paper_balance", getattr(engine.risk, "balance", 0.0)) or 0.0
        )
        for issue in state.issues:
            # In PAPER the virtual wallet, not the mutable KuCoin available
            # balance, is the capital source. A zero exchange balance must not
            # block simulated entries when the virtual wallet is confirmed.
            if issue.code == "BALANCE_ZERO" and paper_balance > 0:
                removed.append(issue)
                continue
            if issue.code == "STATE_DIVERGENCE" and "INEXISTENTE na exchange" in issue.detail:
                removed.append(issue)
                continue
            kept.append(issue)

        if removed:
            if any(i.severity == Severity.BLOCKED for i in kept):
                sev = Severity.BLOCKED
            elif any(i.severity == Severity.DEGRADED for i in kept):
                sev = Severity.DEGRADED
            else:
                sev = Severity.OK

            self.state = IntegrityState(
                severity=sev,
                issues=kept,
                checked_at=state.checked_at,
                exchange_known=state.exchange_known,
            )
            removed_codes = ",".join(sorted({i.code for i in removed}))
            log.debug(
                "[PAPER] integrity: removidas divergências esperadas (%s)",
                removed_codes,
            )
        else:
            self.state = state

        self._log_state()
        return self.state

    async def _finish_paper_position(self, sym, pos, exit_px: float, reason: str):
        if sym not in self.positions:
            return

        # Idempotency guard: a PAPER close may be requested from more than one
        # management path in the same event-loop turn. Only one settlement is
        # allowed to reach persistence/wallet mutation for a symbol at a time.
        closing = getattr(self, "_paper_closing_symbols", None)
        if closing is None:
            closing = set()
            self._paper_closing_symbols = closing
        if sym in closing:
            log.debug("[PAPER_EXIT] duplicate close suppressed: %s reason=%s", sym, reason)
            return
        closing.add(sym)

        try:
            pos.update_pnl(exit_px)
            pnl_gross = pos.pnl
            fee_open = pos.qty * pos.entry * TAKER_FEE
            fee_close = pos.qty * exit_px * TAKER_FEE
            total_fee = fee_open + fee_close
            pnl_net = pnl_gross - total_fee

            trade = Trade(
                sym, pos.direction, pos.entry, exit_px,
                pos.qty, pnl_gross, pos.opened_at,
                fee_open=fee_open, fee_close=fee_close,
            )

            tid = self._trade_ids.get(sym, 0)
            if not tid:
                durable._block(self, "trade")
                log.critical(
                    "[PAPER_STATE] %s close blocked: durable trade id missing", sym
                )
                return
            current_balance = float(
                getattr(self, "_paper_balance", self.risk.balance) or 0.0
            )
            next_balance = max(0.0, current_balance + pnl_net)
            next_peak = max(
                next_balance,
                float(getattr(self.risk, "peak_balance", next_balance) or next_balance),
            )
            cooldown_until = time.time() + 1800
            try:
                post_close_state = durable.build_paper_runtime_payload(
                    self,
                    f"position_closed:{sym}:{reason}",
                    balance_override=next_balance,
                    peak_override=next_peak,
                    exclude_symbols={sym},
                    cooldown_override={sym: cooldown_until},
                )
                await db.save_paper_close_atomic(
                    tid, exit_px, pnl_net, total_fee,
                    (datetime.utcnow() - pos.opened_at).total_seconds() / 60,
                    f"PAPER_{reason}", durable.PAPER_STATE_KEY, post_close_state,
                )
            except (db.PersistenceError, TypeError, ValueError) as exc:
                durable._block(self, "trade")
                log.critical(
                    "[PAPER_STATE] %s close persistence failed; position retained: %s",
                    sym, exc,
                )
                return
            self.stats.add(trade)
            self._trade_ids.pop(sym, None)
            durable._clear(self, "trade")

            del self.positions[sym]
            self._cooldown[sym] = cooldown_until
            await self._record_trade_result(sym, pnl_net)
            self.daily_tracker.add_pnl(
                pnl_net,
                symbol=sym,
                entry_type=getattr(pos, "entry_type", ""),
                regime=getattr(pos, "regime", ""),
                session=self._get_market_session(),
                rr_achieved=round(
                    abs(pnl_net / max(abs(pos.entry - pos.sl), 0.0001) / pos.qty)
                    if pos.sl and pos.qty else 0,
                    2,
                ),
            )

            try:
                consecutive = await db.update_consecutive_losses(pnl_net)
                if consecutive >= 3:
                    await db.save_risk_event(
                        "CONSECUTIVE_LOSSES",
                        f"{consecutive} perdas consecutivas",
                        pnl_net,
                    )
            except Exception as exc:
                log.debug("PAPER close loss-counter: %s", exc)

            # Settle the simulated result into the isolated PAPER wallet. This is
            # intentionally absent in LIVE and cannot mutate the KuCoin balance.
            try:
                apply_wallet = getattr(self, "_paper_apply_virtual_balance", None)
                if callable(apply_wallet):
                    apply_wallet(next_balance, f"close:{sym}:{reason}:{pnl_net:+.4f}")
            except Exception as exc:
                log.error("[PAPER_WALLET] close settlement failed %s: %s", sym, exc)

            if getattr(self, "_durable_state_enforced", False):
                await durable.persist_paper_runtime(
                    self, f"position_closed:{sym}:{reason}", strict=True
                )

            log.info(
                "📭 [PAPER] %s fechado por %s | exit=%.6f Bruto=$%+.4f Taxas=-$%.4f Líquido=$%+.4f",
                sym, reason, exit_px, pnl_gross, total_fee, pnl_net,
            )
            try:
                bal = float(getattr(self, "_paper_balance", self.risk.balance) or 0.0)
                await notify(await close_msg(
                    sym, pos.direction, pnl_net, pos.pnl_pct(), exit_px,
                    bal, bal * cfg.LEVERAGE,
                ))
            except Exception as exc:
                log.debug("PAPER close notify: %s", exc)
        finally:
            closing.discard(sym)

    async def _paper_exit_check(self):
        """PAPER equivalent of the live signal-exit manager.

        The production method closes through ``client.place_order``. In PAPER,
        that call intentionally returns only a synthetic acknowledgement and
        therefore cannot remove/settle an internal simulated position. That
        caused the same INVALIDATION close request to repeat every scan until
        SL/TP eventually fired. Here the trigger logic is preserved, but full
        exits settle through the PAPER lifecycle atomically and exactly once.
        """
        if not self.paper_trade:
            return await original_exit_check(self)

        from bot.indicators import atr as calc_atr
        from bot.strategy import detect_regime

        for sym, pos in list(self.positions.items()):
            try:
                k15 = self.client.get_cached_klines(sym, "15", limit=100) or []
                if len(k15) < 20:
                    continue

                closes = [float(k["c"]) for k in k15[:-1]]
                highs = [float(k["h"]) for k in k15[:-1]]
                lows = [float(k["l"]) for k in k15[:-1]]
                cur = pos.current_price or closes[-1]

                atr_val = float(calc_atr(highs, lows, closes, 14)[-1])
                if atr_val <= 0:
                    continue

                # Keep the exact production thresholds; only the execution
                # target changes from exchange reduceOnly to PAPER settlement.
                STAGNATION_BARS = 16
                STAGNATION_MULT = 0.5
                movement = abs(cur - pos.entry)
                bars_open = len(k15)
                if (
                    bars_open >= STAGNATION_BARS
                    and movement < atr_val * STAGNATION_MULT
                ):
                    log.info(
                        "⏱️  [%s] Saída por TEMPO: %s candles aberto, movimento=%.4f < %.4f "
                        "(0.5×ATR) → fechando PAPER",
                        sym, bars_open, movement, atr_val * STAGNATION_MULT,
                    )
                    await _finish_paper_position(self, sym, pos, cur, "TIME")
                    continue

                if len(closes) >= 10:
                    recent_c = closes[-10:]
                    recent_h = highs[-10:]
                    recent_l = lows[-10:]
                    choch_bear = (
                        recent_h[-1] < recent_h[-3]
                        and recent_l[-1] < recent_l[-3]
                        and recent_c[-1] < recent_c[-3]
                    )
                    choch_bull = (
                        recent_l[-1] > recent_l[-3]
                        and recent_h[-1] > recent_h[-3]
                        and recent_c[-1] > recent_c[-3]
                    )
                    invalidated = (
                        (pos.direction == "LONG" and choch_bear)
                        or (pos.direction == "SHORT" and choch_bull)
                    )
                    if invalidated and not pos.tp1_hit:
                        log.info(
                            "❌ [%s] Saída por INVALIDAÇÃO: CHoCH oposto detectado após entrada %s "
                            "→ fechando PAPER antes do SL",
                            sym, pos.direction,
                        )
                        await _finish_paper_position(
                            self, sym, pos, cur, "INVALIDATION"
                        )
                        continue

                k4h = self.client.get_cached_klines(sym, "240", limit=120) or []
                if len(k4h) >= 20:
                    c4h = [float(k["c"]) for k in k4h[:-1]]
                    h4h = [float(k["h"]) for k in k4h[:-1]]
                    l4h = [float(k["l"]) for k in k4h[:-1]]
                    atr4h = float(calc_atr(h4h, l4h, c4h, 14)[-1])
                    regime_now = detect_regime(c4h, h4h, l4h, atr4h)
                    if (
                        regime_now in ("RANGING", "COMPRESSED", "CHOPPY")
                        and not pos.tp1_hit
                    ):
                        log.info(
                            "🔄 [%s] Saída por REGIME: mercado mudou para %s → fechando PAPER",
                            sym, regime_now,
                        )
                        await _finish_paper_position(
                            self, sym, pos, cur, "REGIME"
                        )
            except Exception as exc:
                log.error("[PAPER_EXIT] %s: %s", sym, exc)

    async def paper_sync(self):
        if not self.paper_trade:
            return await original_sync(self)

        for sym, pos in list(self.positions.items()):
            try:
                tk = self.client.get_cached_ticker(sym) or {}
                price = float(tk.get("lastPrice", 0) or 0)
                if price <= 0:
                    fresh = await self.client.get_ticker(sym)
                    price = float((fresh or {}).get("lastPrice", 0) or 0)
                if price <= 0:
                    log.warning("[PAPER] %s sem preço atual — posição mantida", sym)
                    continue

                pos.update_pnl(price)

                sl = float(getattr(pos, "trailing_sl", 0) or getattr(pos, "sl", 0) or 0)
                tp = float(getattr(pos, "tp", 0) or 0)
                hit_sl = (
                    (pos.direction == "LONG" and sl > 0 and price <= sl)
                    or (pos.direction == "SHORT" and sl > 0 and price >= sl)
                )
                hit_tp = (
                    (pos.direction == "LONG" and tp > 0 and price >= tp)
                    or (pos.direction == "SHORT" and tp > 0 and price <= tp)
                )

                if hit_sl:
                    await _finish_paper_position(self, sym, pos, price, "SL")
                elif hit_tp:
                    await _finish_paper_position(self, sym, pos, price, "TP")
                else:
                    last = getattr(pos, "_paper_heartbeat", 0.0)
                    if time.time() - last >= 60:
                        pos._paper_heartbeat = time.time()
                        log.info(
                            "🧪 [PAPER_POS] %s %s entry=%.6f price=%.6f pnl=$%+.4f sl=%.6f tp=%.6f",
                            sym, pos.direction, pos.entry, price, pos.pnl, sl, tp,
                        )
            except Exception as exc:
                log.error("[PAPER] _sync_positions %s: %s", sym, exc)

        if getattr(self, "_durable_state_enforced", False) and self.positions:
            await durable.persist_paper_runtime(self, "position_mark", strict=False)

    TradingEngine.__init__ = init_with_paper_stop_sim
    TradingEngine._sync_positions = paper_sync
    TradingEngine._check_stagnation_and_invalidation = _paper_exit_check
    IntegrityGuard.assess = assess_with_paper_semantics
    TradingEngine._paper_lifecycle_patched = True
    log.info(
        "🧪 PAPER lifecycle: posições/stops/integridade/saídas por sinal simulados internamente; LIVE inalterado"
    )
