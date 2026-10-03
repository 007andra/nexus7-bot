"""Runtime hardening that does not alter live strategy thresholds or trading mode.

Fixes:
1) deterministic/idempotent DB schema bootstrap (tables -> migrations -> indexes),
2) Telegram transport with plain-text fallback and background 429 retry,
3) PAPER isolation from real/manual exchange positions,
4) stagnation age based on position opened_at instead of cache length.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime
from typing import Set



def install_database_schema_fix(log):
    from bot import database as db

    if getattr(db, "_schema_hardening_patched", False):
        return

    table_ddl = [
        """CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            timestamp TEXT, strategy TEXT, side TEXT,
            symbol TEXT, entry_price REAL, exit_price REAL,
            size REAL, leverage INTEGER, pnl REAL, fees REAL,
            duration_minutes REAL, score_entrada INTEGER,
            status TEXT DEFAULT 'open', score_features TEXT,
            risk_amount REAL, r_multiple REAL, direction TEXT, exit_reason TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY,
            timestamp TEXT, strategy TEXT, direction TEXT,
            symbol TEXT, score_total INTEGER,
            score_tecnico INTEGER, score_orderflow INTEGER,
            score_macro INTEGER, score_news INTEGER,
            entrou INTEGER DEFAULT 0, motivo_rejeicao TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS risk_events (
            id SERIAL PRIMARY KEY, timestamp TEXT, tipo_evento TEXT,
            descricao TEXT, pnl_acumulado REAL
        )""",
        """CREATE TABLE IF NOT EXISTS news_events (
            id SERIAL PRIMARY KEY, timestamp TEXT, titulo TEXT, fonte TEXT,
            classificacao TEXT, score_confianca REAL, impacto_no_score REAL
        )""",
        """CREATE TABLE IF NOT EXISTS market_snapshots (
            id SERIAL PRIMARY KEY, timestamp TEXT, symbol TEXT,
            open_interest REAL, funding_rate REAL, cvd REAL,
            btc_dominance REAL, fear_greed_index REAL
        )""",
        """CREATE TABLE IF NOT EXISTS performance (
            id SERIAL PRIMARY KEY, periodo TEXT, strategy TEXT,
            win_rate REAL, profit_factor REAL, sharpe_ratio REAL,
            sortino_ratio REAL, max_drawdown REAL, expectancy_por_trade REAL,
            total_trades INTEGER, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS consecutive_losses (
            id SERIAL PRIMARY KEY, count INTEGER DEFAULT 0, last_loss TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS key_value (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS decisions (
            id SERIAL PRIMARY KEY, timestamp TEXT, symbol TEXT,
            type TEXT, score INTEGER, reason TEXT
        )""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)",
        "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)",
        "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_decisions_id ON decisions(id)",
    ]
    trade_columns = {
        "score_features": "TEXT",
        "risk_amount": "REAL",
        "r_multiple": "REAL",
        "direction": "TEXT",
        "exit_reason": "TEXT",
    }

    async def _execute(stmt: str):
        if db._is_pg:
            await db._conn.execute(stmt)
        else:
            stmt = stmt.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            await db._conn.execute(stmt)
            await db._conn.commit()

    async def _trade_columns_present() -> Set[str]:
        if db._is_pg:
            rows = await db._conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='trades'"
            )
            return {str(r["column_name"]) for r in rows}
        async with db._conn.execute("PRAGMA table_info(trades)") as cur:
            rows = await cur.fetchall()
        return {str(r[1]) for r in rows}

    async def _create_tables_hardened():
        if not db._conn:
            return
        for stmt in table_ddl:
            await _execute(stmt)

        present = await _trade_columns_present()
        for column, sql_type in trade_columns.items():
            if column not in present:
                await _execute(f"ALTER TABLE trades ADD COLUMN {column} {sql_type}")

        for stmt in indexes:
            await _execute(stmt)

        backend = "PostgreSQL" if db._is_pg else "SQLite"
        log.info("✅ DB schema verificado (%s): tabelas/migrações/índices OK", backend)

    db._create_tables = _create_tables_hardened
    db._schema_hardening_patched = True


def install_telegram_fix(log):
    from bot import notifier, telegram_transport
    from bot.config import cfg
    import bot.engine as engine_module

    if getattr(notifier, "_transport_hardening_patched", False):
        return

    lock = asyncio.Lock()
    last_send = [0.0]
    seen = {}
    min_interval = 3.0
    dedup_window = 60.0

    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]

    async def robust_notify(text: str):
        if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT:
            return telegram_transport.DeliveryResult(False, "DISABLED", skipped="NO_CREDENTIALS")

        # A candidate exists before the mandatory NEXUS gate. Do not label it
        # as an opened/sent order. This prevents operators from assuming that
        # a Telegram candidate implies execution.
        if "*SINAL DETECTADO*" in text:
            text = text.replace(
                "*SINAL DETECTADO*",
                "*CANDIDATO DETECTADO — AGUARDANDO NEXUS AI*",
            )
            text += "\n\n⏳ _Nenhuma ordem foi enviada ainda. O NEXUS AI precisa aprovar._"

        key = _key(text)
        now = time.monotonic()
        if now - seen.get(key, 0.0) < dedup_window:
            # Identical text inside the window is never replayed, including
            # after an ambiguous failure (the first copy may have arrived).
            return telegram_transport.DeliveryResult(False, "DEDUP", skipped="DEDUP")
        seen[key] = now
        if len(seen) > 1000:
            cutoff = now - dedup_window * 2
            for k, ts in list(seen.items()):
                if ts < cutoff:
                    seen.pop(k, None)

        async with lock:
            delay = min_interval - (time.monotonic() - last_send[0])
            if 0 < delay <= min_interval:
                await asyncio.sleep(delay)
            # Canonical transport: classified errors, bounded retry with
            # backoff+jitter, Telegram-only circuit breaker, Markdown→plain
            # content fallback. Never raises; truthiness == Telegram ok:true.
            result = await telegram_transport.deliver(text, parse_mode="Markdown", source="notify")
            if result.sent:
                last_send[0] = time.monotonic()
            return result

    notifier.notify = robust_notify
    engine_module.notify = robust_notify
    notifier._transport_hardening_patched = True


def install_paper_execution_fix(log):
    """Keep PAPER simulation independent from manual/real exchange positions.

    In PAPER mode, real positions must not be imported into the paper engine,
    otherwise a manual position without exchange SL can trigger the global
    integrity gate and block every simulated candidate. Live mode is untouched.

    Also fixes the stagnation timer: cache length is historical data, not trade
    age. A newly opened position with a 100-candle cache was incorrectly treated
    as 100 candles old and immediately closed.
    """
    import bot.engine as engine_module
    from bot.engine import TradingEngine
    from bot.config import cfg
    from bot.exchange import TAKER_FEE

    if getattr(TradingEngine, "_paper_execution_hardening_patched", False):
        return

    orig_init = TradingEngine.__init__
    orig_reconcile = TradingEngine._reconcile_exchange_positions

    def init_with_paper_isolation(self, client):
        orig_init(self, client)
        if self.paper_trade and not getattr(client, "_paper_positions_isolated", False):
            original_get_positions = client.get_positions
            client._real_get_positions = original_get_positions

            async def _paper_get_positions():
                return []

            client.get_positions = _paper_get_positions
            client._paper_positions_isolated = True
            log.info("🟡 PAPER isolation: posições reais/manuais não contaminam o simulador")

    async def reconcile_with_paper_guard(self, only_symbol=None):
        if self.paper_trade:
            self._unprotected_symbols.clear()
            return []
        return await orig_reconcile(self, only_symbol=only_symbol)

    async def stagnation_with_real_age(self):
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

                STAGNATION_BARS = 16
                STAGNATION_MULT = 0.5
                movement = abs(cur - pos.entry)
                opened = getattr(pos, "opened_at", datetime.utcnow())
                bars_open = max(0, int((datetime.utcnow() - opened).total_seconds() // (15 * 60)))

                if bars_open >= STAGNATION_BARS and movement < atr_val * STAGNATION_MULT:
                    log.info(
                        f"⏱️  [{sym}] Saída por TEMPO: {bars_open} candles aberto, "
                        f"movimento={movement:.4f} < {atr_val*STAGNATION_MULT:.4f} "
                        f"(0.5×ATR) → fechando para evitar funding acumulado"
                    )
                    close_side = "Sell" if pos.direction == "LONG" else "Buy"
                    await self.client.place_order(
                        symbol=sym, side=close_side, qty=pos.qty, sl=0, tp=0,
                        instruments=self.instruments, reduce_only=True,
                    )
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
                            f"❌ [{sym}] Saída por INVALIDAÇÃO: CHoCH oposto detectado "
                            f"após entrada {pos.direction} → fechando antes do SL"
                        )
                        close_side = "Sell" if pos.direction == "LONG" else "Buy"
                        await self.client.place_order(
                            symbol=sym, side=close_side, qty=pos.qty, sl=0, tp=0,
                            instruments=self.instruments, reduce_only=True,
                        )
                        continue

                k4h = self.client.get_cached_klines(sym, "240", limit=120) or []
                if len(k4h) >= 20:
                    c4h = [float(k["c"]) for k in k4h[:-1]]
                    h4h = [float(k["h"]) for k in k4h[:-1]]
                    l4h = [float(k["l"]) for k in k4h[:-1]]
                    atr4h = float(calc_atr(h4h, l4h, c4h, 14)[-1])
                    regime_now = detect_regime(c4h, h4h, l4h, atr4h)
                    if regime_now in ("RANGING", "COMPRESSED", "CHOPPY") and not pos.tp1_hit:
                        log.info(
                            f"🔄 [{sym}] Saída por REGIME: mercado mudou para "
                            f"{regime_now} → setup trend-follow inválido, fechando"
                        )
                        close_side = "Sell" if pos.direction == "LONG" else "Buy"
                        await self.client.place_order(
                            symbol=sym, side=close_side, qty=pos.qty, sl=0, tp=0,
                            instruments=self.instruments, reduce_only=True,
                        )
            except Exception as exc:
                log.error(f"_check_stagnation_and_invalidation {sym}: {exc}")

    TradingEngine.__init__ = init_with_paper_isolation
    TradingEngine._reconcile_exchange_positions = reconcile_with_paper_guard
    TradingEngine._check_stagnation_and_invalidation = stagnation_with_real_age
    TradingEngine._paper_execution_hardening_patched = True
