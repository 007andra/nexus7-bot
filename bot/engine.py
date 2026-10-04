"""
BGX Capital Trading Engine v11.0
  ✅ Multi-Timeframe: 4H regime → 1H direção → 15M entrada
  ✅ Score MTF ponderado (4H:25% / 1H:30% / 15M:45%)
  ✅ Candle fechado confirmado — sem repainting
  ✅ ATR por timeframe de entrada (15M para entrada 15M)
  ✅ Regime Switching: TRENDING/RANGING/COMPRESSED
  ✅ Trailing stop progressivo (50% do alvo → ativa)
  ✅ Partial TP: fecha 50% no TP1, SL → breakeven, corre até TP2
  ✅ Circuit breaker por ativo (3 perdas consecutivas → 24h cooldown)
  ✅ Filtro de correlação entre pares (máx 1 posição por grupo)
  ✅ Filtro de sessão de mercado (penaliza altcoins em sessão ASIA)
  ✅ Meta diária: $100 lucro / $50 stop-loss (escala com saldo)
  ✅ Máximo 3 posições simultâneas (com controle de correlação)
  ✅ Score mínimo 60/100 (72 após meta diária)
  ✅ Order Flow: Spoofing, Iceberg, Agressão, CVD 4h
  ✅ Sentimento: Fear&Greed + Notícias NLP + Macro correlações
  ✅ Otimização semanal Optuna + Walk-Forward + Monte Carlo
  ✅ PostgreSQL/SQLite persistente com reconciliação de posições
  ✅ WebSocket Bybit com reconnect automático + fallback REST
  ✅ Paper Trade mode funcional (PAPER_TRADE=true)
"""
import asyncio, time, itertools, os, math
from datetime import datetime, timedelta, timezone
import time
import os
from typing import Dict, Optional, List
import numpy as np

# Cliente selecionado em bot.exchange; o engine não depende da venue.
from bot.exchange import ExchangeClient, is_binance
from bot.strategy import Analyzer, Signal
from bot.config import cfg
from bot.logger import log
from bot.notifier import (notify, notify_nexus, notify_nexus_score, signal_msg, order_opened_msg, close_msg,
    daily_report_msg, daily_target_msg, daily_stop_msg, drawdown_msg, consecutive_losses_msg, online_msg)
from bot import database as db
from bot import score as scoring
from bot import market_data as mdata
from bot import backtest as bt
from bot.daily_tracker import DailyTracker
from bot import optimizer as opt
# ── Fase 3: hardening ─────────────────────────────────────────────
from bot.integrity import IntegrityGuard, Severity
from bot.order_state import OrderRegistry, OrderState, InvalidTransition, TERMINAIS
from bot.pilot import PilotGuard
from bot.quantity import minimum_base_quantity, validate_base_quantity
from bot.paper_loss_budget import cap_quantity as cap_paper_quantity
from bot import liquidation as liq
from bot import durable_execution as durable
# ── NEXUS AI Decision Engine (seções 1-24) ────────────────────────
from bot import nexus_ai
from bot.nexus_types import NexusDecision, decision_validation_error
_NEXUS_ENABLED = os.environ.get("NEXUS_AI_ENABLED", "true").lower() == "true"
_NEXUS_TIMEOUT_S = 10.0


# ─── Trade (histórico fechado) ─────────────────────────────────────────────────
# Taxa Bybit: 0.055% por lado (maker) ou 0.055% taker — usamos 0.055% x2 = 0.11% total
# CORRIGIDO (auditoria #8): 0.00055 era a taxa da Bybit. A exchange agora
# vem da exchange ativa. Importado da boundary para manter uma
# única fonte de verdade — antes o PnL líquido reportado era subestimado.
from bot.exchange import TAKER_FEE

class Trade:
    def __init__(self, symbol, direction, entry, exit_price, qty, pnl_gross, opened_at,
                 fee_open=0.0, fee_close=0.0):
        self.symbol      = symbol
        self.direction   = direction
        self.entry       = entry
        self.exit_price  = exit_price
        self.qty         = qty
        self.opened_at   = opened_at
        self.closed_at   = datetime.utcnow()

        # Calcula taxas se não fornecidas explicitamente
        if fee_open == 0.0 and fee_close == 0.0:
            # fee = qty * preço * taxa_taker
            fee_open  = qty * entry      * TAKER_FEE
            fee_close = qty * exit_price * TAKER_FEE

        self.fee_open    = fee_open
        self.fee_close   = fee_close
        self.total_fees  = fee_open + fee_close
        self.pnl_gross   = pnl_gross              # PnL bruto (sem taxas)
        self.pnl         = pnl_gross - self.total_fees  # PnL LÍQUIDO (com taxas)


# ─── Position ──────────────────────────────────────────────────────────────────
class Position:
    def __init__(self, sig: Signal, qty: float):
        self.symbol      = sig.symbol
        self.direction   = sig.direction
        self.entry       = sig.entry
        self.sl          = sig.sl
        self.tp          = sig.tp
        self.score       = sig.score
        self.qty         = qty
        self.opened_at   = datetime.utcnow()
        self.pnl         = 0.0
        self.peak_pnl    = 0.0
        self.current_price = sig.entry
        # Trailing stop progressivo
        self.trailing_sl       = sig.sl
        self.trailing_active   = False
        self.trailing_milestone= 0
        # Tempo mínimo no trade: 3 candles de 15min = 45min
        self.min_hold_until    = datetime.utcnow().timestamp() + 90 * 60  # 90min = 6 candles 15M
        self.expected_pnl      = getattr(sig, 'expected_pnl', 0.0)
        self.total_fees_pct    = getattr(sig, 'total_fees', 0.0)
        # TP Parcial — dois alvos técnicos
        self.tp1               = getattr(sig, 'tp1', sig.tp)   # fecha 50% aqui
        self.tp2               = getattr(sig, 'tp2', sig.tp)   # fecha 50% aqui
        self.tp1_hit           = False    # já fechou metade no TP1?
        self.qty_original      = qty      # quantidade original para TP parcial
        self.rr1               = getattr(sig, 'rr1', sig.rr)
        self.rr2               = getattr(sig, 'rr2', sig.rr)

    def update_pnl(self, current_price: float):
        self.current_price = current_price
        if self.direction == "LONG":
            self.pnl = (current_price - self.entry) * self.qty
        else:
            self.pnl = (self.entry - current_price) * self.qty
        if self.pnl > self.peak_pnl:
            self.peak_pnl = self.pnl

    def pnl_pct(self) -> float:
        if self.entry <= 0 or self.qty <= 0:
            return 0.0
        if self.direction == "LONG":
            return (self.current_price - self.entry) / self.entry * 100 * cfg.LEVERAGE
        return (self.entry - self.current_price) / self.entry * 100 * cfg.LEVERAGE

    def calc_trailing_sl(self) -> Optional[float]:
        """
        Trailing Stop progressivo:
        - Ativa quando lucro >= 50% do alvo (TRAILING_TRIGGER)
        - Trava 25% abaixo do pico de lucro (TRAILING_LOCK)
        - Protege ganhos sem cortar o trade cedo demais
        """
        if self.pnl <= 0 or self.tp == self.entry:
            return None
        target = abs(self.tp - self.entry)
        if target <= 0:
            return None
        # Ativa trailing quando lucro >= TRAILING_TRIGGER % do alvo
        trigger_pnl = target * cfg.TRAILING_TRIGGER * self.qty
        if self.pnl < trigger_pnl:
            return None
        self.trailing_active = True
        # Trava TRAILING_LOCK % abaixo do pico de preço
        if self.direction == "LONG":
            peak_price = self.entry + (self.peak_pnl / self.qty if self.qty > 0 else 0)
            new_sl = peak_price * (1 - cfg.TRAILING_LOCK * 0.1)
            return max(new_sl, self.sl)   # nunca recua abaixo do SL original
        else:
            peak_price = self.entry - (self.peak_pnl / self.qty if self.qty > 0 else 0)
            new_sl = peak_price * (1 + cfg.TRAILING_LOCK * 0.1)
            return min(new_sl, self.sl)   # nunca recua acima do SL original

    def to_dict(self) -> dict:
        return {
            "symbol":           self.symbol,
            "direction":        self.direction,
            "entry":            round(self.entry, 6),
            "current_price":    round(self.current_price, 6),
            "sl":               round(self.trailing_sl, 6),
            "tp":               round(self.tp, 6),
            "qty":              self.qty,
            "pnl":              round(self.pnl, 4),
            "pnl_pct":          round(self.pnl_pct(), 2),
            "peak_pnl":         round(self.peak_pnl, 4),
            "trailing_active":  self.trailing_active,
            "trailing_sl":      round(self.trailing_sl, 6),
            "score":            self.score,
            "opened_at":        str(self.opened_at),
            "tp1":              round(self.tp1, 6),
            "tp2":              round(self.tp2, 6),
            "tp1_hit":          self.tp1_hit,
            "rr1":              self.rr1,
            "rr2":              self.rr2,
        }


# ─── Stats ────────────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.trades: List[Trade] = []
        self.session_start = datetime.utcnow()

    def add(self, trade: Trade):
        self.trades.append(trade)

    def _filter(self, days: int = None) -> List[Trade]:
        if days is None:
            return self.trades
        cutoff = datetime.utcnow() - timedelta(days=days)
        return [t for t in self.trades if t.closed_at >= cutoff]

    def summary(self, days: int = None) -> dict:
        trades = self._filter(days)
        if not trades:
            return {
                "pnl": 0.0, "pnl_gross": 0.0, "total_fees": 0.0,
                "wins": 0, "losses": 0, "win_rate": 0.0, "trades": 0,
                "closed_trades": [],
            }
        wins   = [t for t in trades if t.pnl >= 0]   # pnl já é líquido
        losses = [t for t in trades if t.pnl < 0]
        return {
            "pnl":          round(sum(t.pnl       for t in trades), 4),  # LÍQUIDO
            "pnl_gross":    round(sum(t.pnl_gross for t in trades), 4),  # bruto
            "total_fees":   round(sum(t.total_fees for t in trades), 4), # total taxas
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / len(trades) * 100, 1),
            "trades":       len(trades),
            "closed_trades": [
                {
                    "symbol":    t.symbol,
                    "direction": t.direction,
                    "entry":     round(t.entry, 6),
                    "exit":      round(t.exit_price, 6),
                    "qty":       t.qty,
                    "pnl_gross": round(t.pnl_gross, 4),
                    "fees":      round(t.total_fees, 4),
                    "pnl":       round(t.pnl, 4),       # LÍQUIDO
                    "pnl_pct":   round(t.pnl / (t.entry * t.qty) * 100, 2) if t.entry * t.qty > 0 else 0,
                }
                for t in reversed(trades[-50:])
            ],
        }

    def all_summaries(self) -> dict:
        return {
            "session": self.summary(),
            "1d":      self.summary(1),
            "7d":      self.summary(7),
            "30d":     self.summary(30),
        }

    def live_metrics(self) -> dict:
        """
        Métricas quantitativas avançadas em tempo real.
        Calculadas sobre TODOS os trades da sessão.
        Expostas via /api/metrics para monitoramento de qualidade.
        """
        import itertools
        trades = self.trades
        if not trades:
            return {"status": "Sem trades na sessão"}

        rets = [
            t.pnl / (t.entry * t.qty) if t.entry * t.qty > 0 else 0
            for t in trades
        ]
        arr  = np.array(rets)
        wins    = arr[arr > 0]
        losses  = arr[arr < 0]
        total   = len(arr)

        # Expectância: ganho médio esperado por trade
        wr       = len(wins) / total if total else 0
        avg_win  = float(wins.mean())  if len(wins)  > 0 else 0.0
        avg_loss = float(losses.mean()) if len(losses) > 0 else 0.0
        expectancy = wr * avg_win + (1 - wr) * avg_loss

        # Consistência: desvio padrão dos retornos (menor = mais consistente)
        consistency = float(arr.std()) if total > 1 else 0.0

        # Sharpe simples (sem risk-free rate)
        sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 and total > 1 else 0.0

        # Recovery Factor: lucro total / max drawdown
        cum   = np.cumsum(arr)
        peak  = np.maximum.accumulate(cum)
        dd    = peak - cum
        max_dd = float(dd.max()) if len(dd) > 0 else 0.0
        recovery_factor = round(float(cum[-1]) / max_dd, 2) if max_dd > 0 else float("inf")

        # Maior sequência consecutiva de perdas
        max_consec = 0
        for is_loss, group in itertools.groupby(rets, lambda x: x < 0):
            if is_loss:
                max_consec = max(max_consec, len(list(group)))

        # Profit Factor
        gp = float(wins.sum())  if len(wins)   > 0 else 0.0
        gl = float(abs(losses.sum())) if len(losses) > 0 else 1e-9
        pf = round(gp / gl, 2)

        return {
            "total_trades":       total,
            "win_rate_pct":       round(wr * 100, 1),
            "expectancy_pct":     round(expectancy * 100, 4),
            "profit_factor":      pf,
            "sharpe_ratio":       round(sharpe, 3),
            "consistency_std":    round(consistency * 100, 4),
            "max_drawdown_pct":   round(max_dd * 100, 2),
            "recovery_factor":    recovery_factor,
            "max_consec_losses":  max_consec,
            "avg_win_pct":        round(avg_win * 100, 3),
            "avg_loss_pct":       round(avg_loss * 100, 3),
            "edge_ratio":         round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0,
        }

    def daily_pnl(self) -> float:
        """PnL realizado apenas hoje (UTC)."""
        if hasattr(self, '_durable_daily_pnl'):
            from bot.durable_daily_pnl import realized
            return realized(self)
        today = datetime.now(timezone.utc).date()
        total = 0.0
        for t in self.trades:
            if t.closed_at.date() == today:
                total += t.pnl
        return total


# ─── Risk Manager ─────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════
# 🔴 P0 CORRIGIDO — CLASSE RiskManager DUPLICADA
#
# engine.py definia sua PRÓPRIA classe RiskManager, sombreando a de
# bot/risk.py. Como este arquivo não importava a versão canônica, o bot
# sempre usou a cópia local — mais simples e com bugs já corrigidos no
# outro arquivo:
#
#   • max(qty, min_qty) DESFAZIA o clamp de margem
#     → saldo $0.50 gerava margem de $10.800 (2.160.000% do saldo)
#   • cap fixo em 80%, ignorando cfg.MAX_MARGIN_PCT
#   • não descontava margem já em uso por posições abertas
#   • confundia unidades: minQty (contratos) vs qty (base)
#
# Toda a lógica robusta de bot/risk.py (TPs parciais, trailing
# verificado, expectancy, margem em uso) estava MORTA.
#
# Agora engine.py usa a implementação canônica.
# ══════════════════════════════════════════════════════════════════
from bot.risk import RiskManager

class TradingEngine:
    def __init__(self, client: ExchangeClient):
        self.client       = client
        # KuCoin's final transport fence executes on the exchange client and
        # needs the canonical engine to evaluate the same readiness authority.
        self.client._engine = self
        self._execution_ownership_valid = False
        self._execution_ownership_expires_at = None
        self.analyzer     = Analyzer()
        self.risk         = RiskManager()
        self.stats        = Stats()
        self.positions:   Dict[str, Position] = {}
        self._trade_ids:  Dict[str, int] = {}   # symbol → DB trade id
        self.instruments: dict = {}
        self.viable_symbols: List[str] = []
        # ══════════════════════════════════════════════════════════
        # P0 — connected=True não pode significar "operacional" quando
        # viable_symbols está vazio.
        #
        # CAUSA RAIZ (auditoria): _filter_viable_symbols() era chamado
        # UMA ÚNICA VEZ dentro de _connect(), sem checar sucesso, antes
        # de self.connected=True. Se /api/v1/contracts/active falhasse
        # naquele instante específico do boot (rede, rate limit, saldo
        # ainda não propagado), viable_symbols ficava [] e NUNCA MAIS
        # era re-tentado — connected=True desliga o único gatilho de
        # reconexão do loop principal (`if not self.connected: ...`).
        #
        # Resultado reproduzido em teste: o bot ficava "conectado" e
        # "ativo" para sempre, sem nenhum par para escanear, exigindo
        # restart manual mesmo que a KuCoin voltasse a responder
        # segundos depois.
        #
        # FIX: retry com backoff PRÓPRIO para viable_symbols, rodando
        # a cada ciclo do loop principal (não depende de connected virar
        # False). Timestamps abaixo controlam esse backoff.
        # ══════════════════════════════════════════════════════════
        self._viable_retry_next_ts: float = 0.0
        self._viable_retry_attempt: int   = 0
        # LOG-throttle (LOW, observabilidade): SCAN_SUSPENSO era emitido
        # a cada ciclo (~5s) enquanto o retry ficava travado no backoff
        # de até 300s — ~720 warnings idênticos por hora nos logs do
        # Railway. Confirmado em produção. Estes campos limitam APENAS a
        # emissão do log; retry, gate e backoff seguem inalterados.
        self._scan_susp_last_log_ts: float = 0.0
        self._scan_susp_last_key:    str   = ""

        # Modo piloto controlado (REAL_TRADING_PILOT). Inerte quando a
        # variável não está definida — comportamento idêntico ao anterior.
        self.pilot = PilotGuard()

        # ══════════════════════════════════════════════════════════
        # P0 (ADV-01) — RECONCILIAÇÃO DE POSIÇÃO ÓRFÃ
        #
        # CAUSA RAIZ: quando wait_for_fill() atinge timeout com uma
        # ordem PARCIALMENTE preenchida mas ainda ativa (isActive=True,
        # filledSize>0), _open() dava 'return' sem nunca criar
        # self.positions[symbol]. A posição real (com leverage, sem
        # stop) continuava existindo na exchange. IntegrityGuard já
        # detectava a divergência (STATE_DIVERGENCE), mas apenas
        # BLOQUEAVA novas entradas — nunca descobria a posição, nunca
        # aplicava proteção. Confirmado por execução real em auditoria
        # adversarial (mock: fill 90%, isActive=True, timeout 5s).
        #
        # _unprotected_symbols: posições reconciliadas da exchange que
        # AINDA NÃO tiveram set_position_stops() confirmado. Enquanto
        # não vazio, novas entradas continuam bloqueadas (ver
        # IntegrityGuard) — a proteção real na exchange, não a marcação
        # interna, é o critério de saída deste estado.
        # ══════════════════════════════════════════════════════════
        self._unprotected_symbols: set = set()
        self._reconcile_lock = asyncio.Lock()   # evita reconciliações simultâneas
        self.connected    = False
        self.active       = False
        self._running     = False
        self._scan_idx    = 0
        # Parâmetros otimizados pelo Optuna (carregados do JSON se disponível)
        self._opt_params  = opt.load_optimized_params()
        self._cooldown:   Dict[str, float] = {}   # símbolo → timestamp até quando não operar
        self._oi_hist:    Dict[str, float] = {}   # OI anterior por símbolo (delta)
        self._liq_alert:  Dict[str, float] = {}   # dedup do alerta de liquidação

        # ── Fase 3 — P0 ───────────────────────────────────────────
        # Kill switch de integridade: única autoridade sobre permissão
        # de NOVAS entradas. Fail-closed por construção.
        self.integrity = IntegrityGuard()
        # Registro de ordens com máquina de estados (idempotência que
        # sobrevive a retry, timeout, restart e múltiplos workers).
        self.orders = OrderRegistry()
        # Enabled by run() only after the database has initialized and durable
        # snapshots have been restored. Direct unit construction stays inert.
        self._durable_state_enforced = False
        self._durable_state_ok = True
        self._durable_state_errors = set()
        # BGX-READY-002: durable storage health is not startup reconciliation.
        # Fail closed until authenticated exchange truth, positions, active
        # orders, ownership attribution and protection state are all proven.
        self._initial_reconciliation_complete = False
        self._initial_reconciliation_evidence = {}
        # BGX-READY-003: protection readiness is derived from exchange readback.
        # It must never default to ready before the first canonical evaluation.
        self._protection_system_ready = False
        self._protection_readiness_evidence = {}

        # BUG CORRIGIDO: self.paper_trade era usado em engine.py e
        # position_manager.py mas NUNCA foi atribuído → AttributeError.
        # A flag vive em bot.kucoin (lida da env var PAPER_TRADE).
        from bot.exchange import PAPER_TRADE as _PT
        self.paper_trade: bool = bool(_PT)
        # PnL diário separado: só o REALIZADO conta para a meta.
        # O não realizado oscila muito com 50x e não é lucro de fato.
        self.daily_pnl_realized:   float = 0.0
        self.daily_pnl_unrealized: float = 0.0
        self._last_nexus: Dict[str, dict]  = {}   # última decisão da IA (observabilidade)

        # ── Lock de posições (race condition) ─────────────────────
        # self.positions é mutado por 7 pontos em corrotinas diferentes:
        # _sync_positions, _manage_partial_tp, _check_rr_double,
        # _apply_trailing_stops, _open e emergency_close_all.
        # Sem lock, duas corrotinas podem ler o mesmo estado e emitir
        # ordens duplicadas de fechamento para a mesma posição.
        self._pos_lock = asyncio.Lock()
        self._consec_losses: Dict[str, int] = {}  # símbolo → perdas consecutivas

        # ── Meta diária ──────────────────────────────────────────
        self.daily_pnl        = 0.0      # PnL acumulado no dia (USDT)
        # RISK-4: meta e stop diário escalam com saldo (1% lucro / 0.5% stop)
        # Se DAILY_TARGET > 0: usa valor fixo. Se = 0: calcula dinamicamente.
        self.daily_target     = cfg.DAILY_TARGET      # $100 fixo ou recalcula no reset
        self.daily_stop_loss  = cfg.DAILY_STOP_LOSS   # $50 fixo ou recalcula no reset
        self.daily_tracker    = DailyTracker()        # usado pelo SignalProcessorMixin
        self.daily_target_hit = False    # meta batida hoje?
        self.daily_stopped    = False    # stop-loss diário ativado?
        self._last_reset_day  = -1       # último dia (UTC) que resetou

    # ── Lifecycle ──────────────────────────────────────────────
    async def run(self):
        if self._running:
            return
        self._running = True
        self._background_tasks = set()
        try:
            log.info("⚡ Engine v10 iniciando...")
            await db.init()   # inicia DB (PostgreSQL ou SQLite)
            self._durable_state_enforced = True
            await durable.restore_engine_state(self)

            # Canonical readiness requires a valid LIVE execution lease. Acquire
            # it during startup rather than lazily on the first order, otherwise
            # /ready can never converge before any dispatch is permitted.
            from bot.execution_ownership import (
                wait_for_live_execution_ownership,
                execution_ownership_heartbeat,
            )
            ownership = await wait_for_live_execution_ownership(self)
            if ownership is None:
                return
            self._start_background(execution_ownership_heartbeat(self))

            self._start_background(scoring.update_macro_cache())        # Fear&Greed
            self._start_background(scoring.news_reader_loop())           # news 24/7
            self._start_background(mdata.update_macro_correlations())    # DXY/S&P
            self._start_background(bt.weekly_backtest_loop(self.client))   # backtest semanal
            self._start_background(opt.weekly_optimization_loop(self.client)) # otimização semanal
            self._start_background(self._monitor_news_pipeline())               # pipeline de notícias
            await self._connect()
            _orders_reconciled = await durable.reconcile_orders(self)
            from bot.initial_reconciliation import finalize_initial_reconciliation
            self._initial_reconciliation_complete = await finalize_initial_reconciliation(
                self,
                orders_reconciled=_orders_reconciled,
            )
            from bot.protection_readiness import refresh_protection_readiness
            await refresh_protection_readiness(self)

            _ciclos = 0
            while self._running:
                try:
                    _ciclos += 1
                    # Prova de vida: sem isso, um loop travado era
                    # indistinguível de "mercado sem setup".
                    if _ciclos == 1 or _ciclos % 60 == 0:
                        log.info(
                            f"💓 Loop #{_ciclos} | connected={self.connected} "
                            f"active={self.active} pares={len(self.viable_symbols)} "
                            f"posições={len(self.positions)} "
                            f"retry_viable={self._viable_retry_attempt}"
                        )

                    if not self.connected:
                        await asyncio.sleep(20)   # scan a cada 20s
                        await self._connect()
                        continue

                    if self.connected:
                        self._check_daily_reset()
                        self._gc_caches()
                        await self._update_balance()
                        # Serializa a gestão de posições sob um único lock,
                        # impedindo ordens concorrentes na mesma posição.
                        async with self._pos_lock:
                            if is_binance():
                                # Ambiguous BGX entries (submit result unknown)
                                # are resolved from exchange truth and, when the
                                # fill lineage is exact, adopted and protected.
                                from bot.ambiguous_entry_recovery import (
                                    recover_unadopted_entries,
                                )
                                await recover_unadopted_entries(self)
                            await self._guard_naked_positions()
                            await self._sync_positions()
                            await self._check_stagnation_and_invalidation()
                            await self._manage_partial_tp()
                            await self._apply_trailing_stops()
                            await self._check_rr_double()
                        from bot.protection_readiness import refresh_protection_readiness
                        await refresh_protection_readiness(self)
                        await self._heartbeat_telegram()
                        from bot.durable_daily_pnl import checkpoint as checkpoint_daily_pnl
                        daily_pnl_ok = await checkpoint_daily_pnl(self)
                        self._update_daily_pnl()
                        from bot.durable_daily_stop import entries_blocked
                        daily_state_blocked = await entries_blocked(self)
                        if is_binance():
                            from bot.binance_accounting_evidence import schedule as schedule_accounting
                        else:
                            from bot.exchange_accounting_evidence import schedule as schedule_accounting
                        schedule_accounting(self)
                    
                        # Independent, cache-only research. No candidate is handed
                        # to the LIVE funnel; its authorization below is unchanged.
                        from bot.hard_gate_shadow_scan import scan_if_enabled
                        await scan_if_enabled(self)

                        if not self.active or getattr(self, 'entries_paused', False) or self.daily_stopped or daily_state_blocked or not daily_pnl_ok:
                            # FIX: logar apenas 1x — não a cada 5s em loop infinito
                            pass   # já logado em _update_daily_pnl, não repetir aqui
                        elif self.risk.can_open(len(self.positions)):
                            # ══════════════════════════════════════════
                            # P0 — KILL SWITCH DE INTEGRIDADE
                            #
                            # Avalia o estado ANTES de qualquer entrada.
                            # Bloqueia se a exchange não puder ser
                            # confirmada, se houver divergência local ↔
                            # exchange, ou se alguma posição estiver sem
                            # stop confirmado.
                            #
                            # NÃO interrompe a gestão de posições abertas —
                            # essa roda antes, sob o _pos_lock.
                            # ══════════════════════════════════════════
                            await self.integrity.assess(self.client, self)

                            # ══════════════════════════════════════════
                            # P0 — GATE viable_symbols=[] BLOQUEIA ORDENS
                            #
                            # Critério de aceite: viable_symbols=[] NUNCA
                            # pode resultar em tentativa de abertura de
                            # posição, independente do estado de
                            # connected/active/integrity.
                            #
                            # _ensure_viable_symbols() faz o retry com
                            # backoff (recarregando instrumentos e preços)
                            # e retorna True assim que houver >=1 par
                            # viável — sem exigir restart manual nem
                            # depender de connected virar False.
                            # ══════════════════════════════════════════
                            _tem_pares = await self._ensure_viable_symbols()

                            if not _tem_pares:
                                # Throttle SOMENTE do log (LOW). O gate acima
                                # já bloqueou o scan — nada aqui altera
                                # segurança, retry ou backoff.
                                #
                                # Emite imediatamente na primeira vez e sempre
                                # que o estado relevante mudar (nº da tentativa);
                                # mensagens idênticas ficam limitadas a 1x/60s.
                                _susp_key = f"viable_empty|{self._viable_retry_attempt}"
                                _now_log = time.time()
                                if (_susp_key != self._scan_susp_last_key or
                                        _now_log - self._scan_susp_last_log_ts >= 60.0):
                                    self._scan_susp_last_key = _susp_key
                                    self._scan_susp_last_log_ts = _now_log
                                    log.warning(
                                        f"🚫 SCAN_SUSPENSO: viable_symbols=[] — "
                                        f"nenhuma ordem será aberta até a "
                                        f"recuperação automática "
                                        f"(tentativa #{self._viable_retry_attempt})"
                                    )
                            elif not self.integrity.can_open_new():
                                log.warning(
                                    f"🚫 ENTRADAS BLOQUEADAS: "
                                    f"{self.integrity.block_reason()}"
                                )
                            else:
                                await self._scan_all_and_enter()
                        else:
                            # Sem este else, um can_open()=False fazia o ciclo
                            # passar direto sem nenhum registro — parecia que o
                            # bot tinha parado de analisar.
                            _n = len(self.positions)
                            log.info(
                                f"⏸️ Scan pulado: posições={_n}/{cfg.MAX_POSITIONS} "
                                f"drawdown={self.risk.drawdown:.1%}/"
                                f"{cfg.MAX_DRAWDOWN:.0%} "
                                f"ready={self.risk._ready} "
                                f"stop_diário={self.daily_stopped}"
                            )

                    await asyncio.sleep(5)

                except asyncio.CancelledError:
                    break
                except (NameError, AttributeError, TypeError, ImportError) as e:
                    # Erro de programação no ciclo principal: log CRITICAL com
                    # traceback e alerta único (evita spam a cada 5s).
                    import traceback
                    _sig = f"{type(e).__name__}:{e}"
                    if getattr(self, "_last_bug_sig", None) != _sig:
                        self._last_bug_sig = _sig
                        log.critical(
                            f"🐛 BUG DE CÓDIGO no engine loop: {type(e).__name__}: {e}\n"
                            f"Traceback:\n{traceback.format_exc()}"
                        )
                        try:
                            asyncio.create_task(notify(
                                f"🐛 *BUG NO CICLO PRINCIPAL*\n"
                                f"❌ `{type(e).__name__}`\n"
                                f"💬 `{str(e)[:140]}`\n"
                                f"_O bot continua rodando, mas este ciclo falhou._"
                            ))
                        except Exception:
                            pass
                    await asyncio.sleep(5)
                except Exception as e:
                    log.error(f"Engine loop: {e}")
                    await asyncio.sleep(5)
        finally:
            self._running = False
            accounting_task = getattr(self, '_accounting_evidence_task', None)
            if accounting_task is not None:
                self._background_tasks.add(accounting_task)
            tasks = tuple(self._background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()

    def _start_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        return task

    def pause_entries(self):
        self.entries_paused = True
        self.client.entries_paused = True
        log.warning('[ENTRY_PAUSE] paused=true position_management=CONTINUES')

    def resume_entries(self):
        self.entries_paused = False
        self.client.entries_paused = False
        log.info('[ENTRY_PAUSE] paused=false risk_gates=UNCHANGED')

    def stop(self):
        self.pause_entries()
        self.active   = False
        self._running = False   # FIX: permite que run() seja recriado no resume
        log.info("⏸️ Bot pausado (servidor continua rodando)")

    # ── Meta diária ────────────────────────────────────────────
    def _check_daily_reset(self):
        """Reseta contadores de PnL diário à meia-noite UTC."""
        today = datetime.now(timezone.utc).day
        if today != self._last_reset_day:
            if self._last_reset_day != -1:
                log.info(
                    f"📅 Novo dia UTC — resetando contadores. "
                    f"PnL ontem: ${self.daily_pnl:+.4f} | "
                    f"Meta {'✅ BATIDA' if self.daily_target_hit else '❌ não atingida'}"
                )
            self.daily_pnl        = 0.0
            self.daily_target_hit = False
            self.daily_stopped    = False
            self._last_reset_day  = today
            # RISK-4: Recalcula meta/stop com saldo atual se configurados como dinâmicos
            # cfg.DAILY_TARGET=0 → usa 1% do saldo; cfg.DAILY_STOP_LOSS=0 → usa 0.5% do saldo
            bal = self.risk.balance or 1000.0
            if cfg.DAILY_TARGET == 0:
                self.daily_target    = round(bal * cfg.DAILY_TARGET_PCT, 2)    # dinâmico
            # Stop diário removido — sem limite de perda diária
            log.info(f"🎯 Meta diária: ${self.daily_target:.2f} | Stop-loss dia: -${self.daily_stop_loss:.2f} | Saldo: ${bal:.2f}")

    def _update_daily_pnl(self):
        """
        Atualiza o PnL do dia e verifica meta/stop.

        ══════════════════════════════════════════════════════════
        BUG CRÍTICO CORRIGIDO — meta batida com lucro inexistente
        ══════════════════════════════════════════════════════════
        Antes: daily_pnl = realizado + NÃO REALIZADO.

        Com 50x e notional de ~$766, uma oscilação de 1.5% no preço
        produzia "+$11.60 de lucro" com a posição ainda ABERTA. O bot
        anunciava META BATIDA e entrava em modo conservador sobre um
        lucro que podia evaporar no minuto seguinte — e a flag
        daily_target_hit não voltava atrás.

        Agora: a META considera apenas PnL REALIZADO (trades fechados).
        O não realizado continua sendo exibido, mas separadamente.

        Também corrigido: havia DOIS caminhos marcando a meta como
        batida, usando contadores diferentes de PnL. Eles divergiam e
        disparavam notificações duplicadas com valores distintos.
        Agora existe um único ponto de decisão.
        """
        realized   = self.stats.daily_pnl()
        unrealized = sum(p.pnl for p in self.positions.values())

        # Exposto para dashboard/heartbeat (informativo)
        self.daily_pnl_realized   = realized
        self.daily_pnl_unrealized = unrealized
        self.daily_pnl            = realized + unrealized   # exibição

        # ── STOP: considera realizado + não realizado ─────────────
        # Aqui o não realizado DEVE contar: uma posição perdendo muito
        # é risco presente, não hipotético.
        result = self.daily_tracker.check_limits()
        if result in ('STOP', 'WEEKLY_STOP', 'MONTHLY_STOP') and not self.daily_stopped:
            self.daily_stopped = True
            label = {'STOP': 'DIÁRIO', 'WEEKLY_STOP': 'SEMANAL',
                     'MONTHLY_STOP': 'MENSAL'}[result]
            log.warning(f"🛑 STOP-LOSS {label} ATINGIDO: ${self.daily_pnl:.2f}")
            asyncio.create_task(notify(
                f"🛑 *Stop-Loss {label}*\n"
                f"PnL calculado (realizado + em aberto): `${self.daily_pnl:.2f}`\n"
                "Novas entradas bloqueadas; posições abertas continuam sendo gerenciadas e protegidas."
            ))

        # ── META: SOMENTE PnL REALIZADO ───────────────────────────
        # Ponto ÚNICO de decisão — elimina a duplicação de notificação.
        if not self.daily_target_hit and realized >= self.daily_target:
            self.daily_target_hit = True
            log.info(
                f"🎯 META DIÁRIA BATIDA! Realizado=${realized:.4f} "
                f"≥ ${self.daily_target:.2f} — modo CONSERVADOR "
                f"(score ≥ {cfg.POST_TARGET_SCORE})"
            )
            asyncio.create_task(notify(
                f"🎯 *META DIÁRIA BATIDA!*\n"
                f"`{'━'*26}`\n"
                f"💵 Lucro realizado: `+${realized:.2f} USDT`\n"
                f"📊 Em aberto:       `${unrealized:+.2f} USDT`\n"
                f"🎯 Meta era:        `${self.daily_target:.2f}`\n"
                f"`{'━'*26}`\n"
                f"Próximas entradas: score ≥ `{cfg.POST_TARGET_SCORE}/100`\n"
                f"_Modo conservador ativado_"
            ))

    def _effective_score(self) -> int:
        """Score mínimo efetivo — aumenta após bater a meta."""
        if self.daily_target_hit:
            return cfg.POST_TARGET_SCORE  # mais seletivo (default 72)
        return cfg.MIN_ENTRY_SCORE        # padrão (default 60)

    def _effective_risk_pct(self) -> float:
        """Risco por trade — reduz após bater a meta."""
        if self.daily_target_hit:
            return cfg.POST_TARGET_RISK   # conservador (default 0.5%)
        return cfg.MAX_RISK_PCT           # padrão (default 1%)

    # ── Connect ────────────────────────────────────────────────
    async def _startup_risk_balance(self) -> float:
        """Return the authority used for the first risk balance initialization.

        Controlled LIVE publishes canonical KuCoin accountEquity before the
        legacy connect routine runs. That mode must never initialize the equity
        slot from availableBalance/availableMargin. PAPER and non-pilot callers
        retain the existing get_balance() behavior unchanged.
        """
        controlled_live = (
            not getattr(self, "paper_trade", False)
            and bool(getattr(type(self), "_pilot_live_runtime_patched", False))
        )
        if controlled_live:
            value = getattr(self, "_pilot_startup_account_equity", None)
            if value is None:
                raise RuntimeError("controlled LIVE startup equity unavailable")
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise RuntimeError("controlled LIVE startup equity invalid")
            return value
        return await self.client.get_balance()

    async def _connect(self):
        try:
            # Ping é opcional — não bloqueia o bot se falhar
            # O bot tenta operar mesmo sem ping (REST pode funcionar)
            ping_ok = await self.client.ping()
            if not ping_ok:
                log.warning("⚠️ Ping da exchange falhou — continuando mesmo assim (REST pode funcionar)")

            bal = await self._startup_risk_balance()
            if bal < 0:
                log.error("❌ Autenticação falhou")
                self.connected = False
                return

            self.risk.init(bal)
            self.risk.update(bal)

            # BUG CORRIGIDO: meta e stop diários só eram recalculados no
            # reset das 00:00 UTC. No startup, ficavam com o valor de
            # config — que era $100/$50 fixos. Com saldo de $19, a meta
            # era inatingível e o stop nunca dispararia.
            if bal > 0:
                if cfg.DAILY_TARGET <= 0:
                    self.daily_target = round(bal * cfg.DAILY_TARGET_PCT, 2)
                if cfg.DAILY_STOP_LOSS <= 0:
                    self.daily_stop_loss = round(bal * cfg.DAILY_STOP_LOSS_PCT, 2)
                self.daily_tracker.daily_target    = self.daily_target
                self.daily_tracker.daily_stop_loss = self.daily_stop_loss

            # Sanidade de configuração: MAX_POSITIONS × MAX_MARGIN_PCT > 100%
            # significa que as posições extras nunca terão margem suficiente.
            # Aviso destacado quando o filtro de liquidação está desligado
            if os.environ.get("ALLOW_SL_BEYOND_LIQUIDATION", "false").lower() == "true":
                _liq = 100.0 / max(1, cfg.LEVERAGE)
                log.warning("=" * 62)
                log.warning(
                    f"⚠️ FILTRO DE LIQUIDAÇÃO DESATIVADO "
                    f"(ALLOW_SL_BEYOND_LIQUIDATION=true)"
                )
                log.warning(
                    f"   Com {cfg.LEVERAGE}x, a liquidação ocorre a "
                    f"~{_liq:.2f}% de movimento adverso."
                )
                log.warning(
                    f"   Trades com SL acima disso serão abertos e a perda "
                    f"será de 100% da margem, não 1R."
                )
                log.warning("=" * 62)

            _mc = getattr(cfg, "MAX_MARGIN_PCT", 0.80)
            if cfg.MAX_POSITIONS * _mc > 1.0:
                log.warning(
                    f"⚠️ CONFIG: MAX_POSITIONS={cfg.MAX_POSITIONS} × "
                    f"MAX_MARGIN_PCT={_mc:.0%} = {cfg.MAX_POSITIONS*_mc:.0%} > 100%. "
                    f"A 1ª posição consome quase toda a margem — as demais "
                    f"ficarão residuais. Para {cfg.MAX_POSITIONS} posições "
                    f"equilibradas use MAX_MARGIN_PCT≈{1/cfg.MAX_POSITIONS:.2f}"
                )
            self.instruments = self.client.get_instruments()
            await self._filter_viable_symbols()

            # set_leverage é no-op (a alavancagem vai como parâmetro em
            # cada ordem). O loop com sleep de 0.3s × 12 pares somava 3.6s
            # de atraso no startup sem nenhum efeito prático.
            log.info(
                f"⚙️ Leverage {cfg.LEVERAGE}x será aplicado em cada ordem "
                f"({len(self.viable_symbols)} pares)"
            )

            # ══════════════════════════════════════════════════════
            # connected=True ANTES das etapas opcionais.
            #
            # Tudo que roda aqui bloqueia o LOOP PRINCIPAL, porque
            # _connect() é aguardado antes do while. Uma etapa lenta
            # (ou travada) impedia qualquer scan de acontecer — e isso
            # ficava invisível nos logs, já que o WebSocket roda em task
            # separada e seguia publicando normalmente.
            #
            # O essencial (saldo, instrumentos, pares viáveis) já foi
            # feito. O resto tem timeout e não pode travar o engine.
            # ══════════════════════════════════════════════════════
            self.connected = True
            self.active    = True
            log.info("✅ Engine PRONTO — loop de scan liberado")

            try:
                await asyncio.wait_for(self._load_existing_positions(), timeout=20)
            except asyncio.TimeoutError:
                log.warning("⏱️ _load_existing_positions excedeu 20s — seguindo")
            except Exception as e:
                log.warning(f"_load_existing_positions: {e}")

            # Inicia WebSocket para dados em tempo real
            # ══════════════════════════════════════════════════════════
            # LIMITE DE SÍMBOLOS NO WEBSOCKET
            #
            # O corte fixo em 10 deixava 2 dos 12 pares configurados SEM
            # cache — eles caíam em "WS cache miss" e dependiam de REST a
            # cada scan, com dados mais defasados que os demais.
            #
            # A KuCoin permite até 100 tópicos por conexão. Com 3
            # intervalos por par, 12 pares = 36 tópicos + 1 ticker = 37,
            # bem dentro do limite.
            #
            # Configurável via WS_MAX_SYMBOLS caso a lista cresça muito.
            # ══════════════════════════════════════════════════════════
            _ws_max = int(os.environ.get("WS_MAX_SYMBOLS", "30"))
            _n_iv   = 3
            # Margem de segurança: 100 tópicos é o teto da KuCoin
            _cap_por_topicos = max(1, (100 - 1) // _n_iv)
            _ws_max = min(_ws_max, _cap_por_topicos)

            ws_symbols = self.viable_symbols[:_ws_max]
            if not ws_symbols:
                log.warning(
                    "⚠️ viable_symbols vazio — usando fallback cfg.SYMBOLS para WebSocket"
                )
                ws_symbols = cfg.SYMBOLS[:_ws_max]

            _fora = [s for s in self.viable_symbols if s not in ws_symbols]
            if _fora:
                log.warning(
                    f"⚠️ {len(_fora)} pares fora do WebSocket (limite {_ws_max}): "
                    f"{', '.join(_fora)} — dependerão de REST a cada scan"
                )

            log.info(
                f"🔌 Iniciando WebSocket com {len(ws_symbols)} símbolos: "
                f"{', '.join(ws_symbols)}"
            )
            # start_websocket já usa create_task internamente, mas o
            # timeout garante que uma mudança futura não volte a travar.
            try:
                await asyncio.wait_for(
                    self.client.start_websocket(
                        ws_symbols, intervals=["15", "60", "240"]
                    ),
                    timeout=10,
                )
            except asyncio.TimeoutError:
                log.warning("⏱️ start_websocket excedeu 10s — seguindo")
            except Exception as e:
                log.error(f"start_websocket: {e}")

            # WS privado de ordens: confirmação ADICIONAL, não única.
            # Fecha o gap PRIVATE_WS_RECONCILIATION (FAIL na auditoria
            # anterior). wait_for_fill() via REST continua rodando em
            # _open() independentemente — o WS acelera a detecção e
            # mantém o OrderRegistry atualizado, mas nunca é a única
            # fonte que decide se uma ordem está FILLED.
            try:
                self.client.start_private_websocket(self.orders, ws_symbols)
            except Exception as e:
                log.error(f"start_private_websocket: {e}")

            log.info(f"✅ Conectado! ${bal:.4f} USDT | {len(self.viable_symbols)} pares | max {cfg.MAX_POSITIONS} posições | score >= {cfg.MIN_ENTRY_SCORE}")

            # Telegram em background: uma API lenta não pode atrasar o scan.
            async def _notify_online():
                try:
                    await notify(await online_msg(
                        bal, bal * cfg.LEVERAGE,
                        len(self.viable_symbols), cfg.MAX_POSITIONS
                    ))
                    await notify(
                        f"Score mínimo: `{cfg.MIN_ENTRY_SCORE}/100`\n"
                        f"Pares ativos: `{len(self.viable_symbols)}`"
                    )
                except Exception as e:
                    log.debug(f"notify online: {e}")

            asyncio.create_task(_notify_online())
        except Exception as e:
            log.error(f"_connect: {e}")
            self.connected = False

    async def _ensure_viable_symbols(self) -> bool:
        """
        Retry automático com backoff quando viable_symbols está vazio.

        Chamado a CADA CICLO do loop principal (não depende de
        self.connected virar False, que era o único gatilho de retry
        antes desta correção — e nunca disparava porque connected=True
        é setado incondicionalmente em _connect()).

        Recarrega instrumentos E preços a cada tentativa: uma falha em
        load_instruments() no boot não se corrige sozinha só rodando
        _filter_viable_symbols() de novo com o cache antigo (vazio).

        Retorna True se há pelo menos 1 par viável (imediatamente, ou
        após uma tentativa de recuperação bem-sucedida nesta chamada).
        """
        if self.viable_symbols:
            # Já operacional — reseta o backoff para a próxima falha
            # começar do zero, e não acumular de uma degradação antiga.
            if self._viable_retry_attempt > 0:
                log.info(
                    f"✅ RECOVERY: viable_symbols recuperado "
                    f"({len(self.viable_symbols)} pares) após "
                    f"{self._viable_retry_attempt} tentativa(s) — "
                    f"voltando ao estado operacional normal"
                )
            self._viable_retry_attempt = 0
            self._viable_retry_next_ts = 0.0
            return True

        now = time.time()
        if now < self._viable_retry_next_ts:
            return False   # ainda dentro da janela de backoff — não tenta agora

        self._viable_retry_attempt += 1
        _backoff = min(300.0, 5.0 * (2 ** min(self._viable_retry_attempt - 1, 6)))
        self._viable_retry_next_ts = now + _backoff

        log.warning(
            f"🔄 RETRY_VIABLE_SYMBOLS: tentativa #{self._viable_retry_attempt} "
            f"— recarregando instrumentos e preços "
            f"(próxima tentativa em {_backoff:.0f}s se esta falhar)"
        )

        try:
            await self.client.load_instruments()
            self.instruments = self.client.get_instruments()
        except Exception as e:
            log.error(f"RETRY_VIABLE_SYMBOLS: load_instruments falhou: {e}")

        ok = await self._filter_viable_symbols()
        if ok:
            log.info(
                f"✅ RECOVERY: viable_symbols recuperado "
                f"({len(self.viable_symbols)} pares) na tentativa "
                f"#{self._viable_retry_attempt} — bot volta a operar"
            )
            self._viable_retry_attempt = 0
            self._viable_retry_next_ts = 0.0
        return ok

    async def _filter_viable_symbols(self) -> bool:
        """
        BUG CORRIGIDO: o custo mínimo era calculado como minQty × price,
        tratando lotSize como quantidade na moeda base. Na KuCoin Futures
        lotSize é o número mínimo de CONTRATOS, e cada contrato vale
        multiplier × price.

        Efeito do bug: BTCUSDT aparecia custando 1 × $108.000 = $108.000
        (em vez de 1 × 0.001 × $108.000 = $108) e era eliminado do scan,
        junto com todos os pares de preço alto. Só sobravam pares baratos
        — por isso o bot analisava basicamente SOLUSDT.

        Fórmula correta: min_cost = lotSize × multiplier × price

        RETORNA True se pelo menos 1 par ficou viável, False caso
        contrário — usado pelo retry automático em _ensure_viable_symbols
        para decidir se deve tentar de novo.
        """
        try:
            # ══════════════════════════════════════════════════════
            # OBSERVABILIDADE (auditoria de viable_symbols=[]) — cada
            # causa raiz tem uma assinatura de log DISTINTA, para que
            # o operador saiba imediatamente se o problema é:
            #   instrumentos indisponíveis / tickers indisponíveis /
            #   saldo insuficiente / zero pares viáveis
            # sem precisar adivinhar a partir de "0/12 pares viáveis".
            # ══════════════════════════════════════════════════════
            if not self.instruments:
                log.warning(
                    "⛔ INSTRUMENTS_UNAVAILABLE: self.instruments vazio — "
                    "load_instruments() não populou nenhum símbolo "
                    "(provável falha de /api/v1/contracts/active)"
                )

            tickers = await self.client.get_all_tickers()
            if not tickers:
                log.warning(
                    "⛔ TICKERS_UNAVAILABLE: get_all_tickers() retornou "
                    "vazio — preços indisponíveis via REST, tentando "
                    "fallback por cache do WS símbolo a símbolo"
                )
            price_map = {t["symbol"]: float(t.get("lastPrice", 0)) for t in tickers}
            buying_power = self.risk.balance * cfg.LEVERAGE

            # Observability only: expose the minimum-order feasibility envelope
            # for the full configured universe using the same exchange metadata
            # already loaded by the runtime. This never changes viable_symbols,
            # scores, risk, leverage, sizing, or dispatch.
            try:
                from bot import min_order_feasibility_matrix as _feasibility_matrix
                _feasibility_matrix.log_once(self, price_map, log)
            except Exception as _matrix_exc:
                log.warning(
                    "[MIN_ORDER_FEASIBILITY_MATRIX] result=DEFER reason=%s "
                    "observability_only=true decision_effect=NONE execution_effect=NONE",
                    type(_matrix_exc).__name__,
                )

            if buying_power <= 0:
                log.warning(
                    f"⛔ INSUFFICIENT_BUYING_POWER: poder de compra "
                    f"${buying_power:.2f} (saldo=${self.risk.balance:.2f} "
                    f"× {cfg.LEVERAGE}x) — nenhum par poderá ser viável"
                )

            viable, rejected = [], []

            for sym in cfg.SYMBOLS:
                info  = self.instruments.get(sym)
                price = price_map.get(sym, 0)

                # Fallback: se o ticker não trouxe preço, tenta o cache do WS
                if price <= 0:
                    tk = self.client.get_cached_ticker(sym) or {}
                    price = float(tk.get("lastPrice", 0) or 0)

                if not info:
                    rejected.append(f"{sym}(sem instrumento)")
                    continue
                if price <= 0:
                    rejected.append(f"{sym}(sem preço)")
                    continue

                lot_size   = float(info.get("minQty",     1))
                multiplier = float(info.get("multiplier", 1))
                # Custo de 1 lote mínimo, em USDT
                min_cost   = lot_size * multiplier * price

                if buying_power >= min_cost * 1.1:
                    viable.append(sym)
                else:
                    rejected.append(f"{sym}(min ${min_cost:.2f})")

            self.viable_symbols = viable

            if viable:
                log.info(
                    f"✅ {len(viable)}/{len(cfg.SYMBOLS)} pares viáveis "
                    f"(poder=${buying_power:.2f}): {', '.join(viable)}"
                )
            else:
                log.error(
                    f"⛔ ZERO_VIABLE_SYMBOLS: 0/{len(cfg.SYMBOLS)} pares "
                    f"viáveis (poder=${buying_power:.2f}) — nenhuma ordem "
                    f"pode ser aberta até que isto seja resolvido"
                )
            if rejected:
                log.info(f"⛔ Rejeitados: {', '.join(rejected)}")

            return len(viable) > 0
        except Exception as e:
            # LIVE safety invariant: unknown instrument viability must never
            # expand the tradable universe.  An exchange/instrument failure
            # is retried by _ensure_viable_symbols(); until a successful
            # refresh proves viability, new entries remain fail-closed.
            self.viable_symbols = []
            self._viable_retry_next_ts = 0.0
            log.error(
                "[MARKET_VIABILITY_FAIL_CLOSED] verified=0 dropped=ALL "
                "entries_blocked=true reason=%s execution_effect=BLOCK_NEW_ENTRIES",
                type(e).__name__,
            )
            return False

    # ── Sincronização em tempo real com a exchange ─────────────
    def _contracts_to_base_qty(self, symbol: str, contracts: float) -> float:
        """
        EXEC-01 — Converte CONTRATOS (unidade da KuCoin) → UNIDADE BASE.

        ══════════════════════════════════════════════════════════════
        INVARIANTE DE UNIDADES (estabelecido nesta correção)
        ══════════════════════════════════════════════════════════════
          KuCoin currentQty / get_positions()["size"]  = CONTRATOS
          engine.Position.qty                          = UNIDADE BASE
          RiskManager.size()                           = UNIDADE BASE
          place_order(qty=...)                         = UNIDADE BASE
          _round_qty()  = ÚNICO ponto base → contratos (para envio)

        CAUSA RAIZ do EXEC-01: Position.qty recebia unidade base quando
        criada por _open() (via risk.size()), mas CONTRATOS quando
        criada por qualquer caminho de reconciliação (via
        get_positions()). Medido com DOGEUSDT (multiplier=100): a mesma
        posição física produzia qty=2600.0 por _open() e qty=26.0 por
        reconciliação — margem calculada divergindo em 53.000x.

        Esta conversão é de ESTADO, não de submissão: nenhum
        arredondamento de lote é aplicado aqui (isso é papel de
        _round_qty, no momento do envio da ordem).

        FALHA SEGURA (EXEC01-L): multiplier ausente, zero, negativo ou
        NaN NÃO assume 1 silenciosamente — levanta ValueError. Operar
        com unidade desconhecida é pior que não operar.
        """
        # Fonte primária: instrumentos já carregados no engine.
        # Fallback: o próprio cliente (self.instruments pode ainda estar
        # vazio se este método for chamado antes de _connect() popular,
        # ex: restart que chama _load_existing_positions() cedo). Não é
        # uma segunda fonte de verdade — é a MESMA (client._instruments),
        # apenas acessada diretamente.
        info = (self.instruments or {}).get(symbol) or {}
        if not info:
            try:
                info = (self.client.get_instruments() or {}).get(symbol) or {}
            except Exception:
                info = {}
        raw  = info.get("multiplier", None)

        if raw is None:
            raise ValueError(
                f"_contracts_to_base_qty({symbol}): multiplier ausente nos "
                f"instrumentos carregados — impossível converter contratos "
                f"para unidade base com segurança"
            )
        try:
            mult = float(raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"_contracts_to_base_qty({symbol}): multiplier inválido "
                f"({raw!r}) — não é numérico"
            )
        # NaN != NaN — única forma confiável de detectar sem importar math
        if mult != mult or mult <= 0:
            raise ValueError(
                f"_contracts_to_base_qty({symbol}): multiplier inválido "
                f"({mult}) — deve ser > 0 e não-NaN"
            )

        return float(contracts) * mult

    async def _reconcile_exchange_positions(self, only_symbol: str = None) -> list:
        """
        P0 (ADV-01) — descobre e protege posições que existem na
        exchange mas não em self.positions (posição órfã).

        Chamada em 3 pontos (Fase 4 da correção):
          A) logo após wait_for_fill() der timeout em _open()
          B) periodicamente no ciclo principal (defesa em profundidade,
             cobre WS perdido, restart, ou qualquer gap não previsto)
          C) após reconexão do WebSocket privado

        only_symbol: se informado, reconcilia apenas esse símbolo
        (usado no caminho A, onde já sabemos qual símbolo investigar
        — evita uma varredura completa desnecessária logo após o
        timeout de uma ordem específica).

        Retorna a lista de símbolos que continuam SEM proteção
        confirmada após a tentativa (vazia = tudo protegido ou nada
        para reconciliar).

        Idempotente e serializada por _reconcile_lock: chamadas
        concorrentes (REST + WS, ou dois pontos de chamada disparando
        ao mesmo tempo) não duplicam o registro nem enviam
        set_position_stops() duas vezes sem necessidade.
        """
        async with self._reconcile_lock:
            try:
                all_pos = await self.client.get_positions()
            except Exception as e:
                log.error(f"_reconcile_exchange_positions: get_positions falhou: {e}")
                return list(self._unprotected_symbols)

            for p in all_pos:
                sym = p.get("symbol", "")
                if only_symbol and sym != only_symbol:
                    continue
                size = float(p.get("size", 0) or 0)
                if size <= 0:
                    continue

                # ── Posição JÁ conhecida internamente ──
                if sym in self.positions:
                    sl_atual = float(p.get("stopLoss", 0) or 0)
                    if sl_atual > 0:
                        self._unprotected_symbols.discard(sym)

                    # ══════════════════════════════════════════════════
                    # EXEC-03 (HIGH) — RECONCILIAR qty DIVERGENTE
                    #
                    # Antes: 'continue' direto — a qty local nunca era
                    # atualizada para posição já conhecida. Se a exchange
                    # mudasse a quantidade (partial fill que completou,
                    # fechamento parcial externo, TP executado pela
                    # própria exchange), o estado local ficava para
                    # sempre desatualizado e o IntegrityGuard bloqueava
                    # novas entradas indefinidamente, sem caminho de
                    # auto-recuperação (exigia restart manual).
                    #
                    # Reproduzido: exchange 10→26 contratos, qty local
                    # travada em 1000.0, STATE_DIVERGENCE permanente.
                    #
                    # A EXCHANGE é a fonte de verdade para exposição.
                    # ══════════════════════════════════════════════════
                    try:
                        _qty_ex = self._contracts_to_base_qty(sym, size)
                    except ValueError as _ue:
                        log.error(f"RECONCILE {sym}: {_ue}")
                        continue

                    _pos_local = self.positions[sym]

                    # ══════════════════════════════════════════════════
                    # EXEC-04 (HIGH) — SIDE DIVERGENTE
                    #
                    # Corrigir só a qty quando o LADO diverge é pior que
                    # não corrigir nada: o bot ficaria com LONG 2600
                    # local enquanto a exchange tem SHORT 2600 — PnL
                    # invertido, SL no lado errado, e o IntegrityGuard
                    # (que não compara side) liberaria novas entradas.
                    #
                    # Reproduzido: local LONG, exchange currentQty=-26.
                    # Antes deste fix: qty convergia p/ 2600 mas
                    # direction permanecia LONG, can_open=True.
                    #
                    # Divergência de lado NÃO é corrigida silenciosamente:
                    # a posição é marcada desprotegida (bloqueia novas
                    # entradas) e registrada como incidente para
                    # resolução explícita.
                    # ══════════════════════════════════════════════════
                    _side_ex = "LONG" if p.get("side", "Buy") == "Buy" else "SHORT"
                    _side_local = getattr(_pos_local, "direction", "")
                    if _side_local and _side_ex != _side_local:
                        log.critical(
                            f"🚨🚨 RECONCILE {sym}: SIDE DIVERGENTE — local "
                            f"{_side_local}, exchange {_side_ex}. NÃO corrigindo "
                            f"qty silenciosamente; posição marcada como "
                            f"desprotegida até resolução explícita."
                        )
                        self._unprotected_symbols.add(sym)
                        continue

                    _qty_local = float(getattr(_pos_local, "qty", 0) or 0)
                    if _qty_local > 0 and _qty_ex > 0:
                        _div = abs(_qty_local - _qty_ex) / max(_qty_local, _qty_ex)
                        if _div > 0.02:      # mesma tolerância do IntegrityGuard
                            log.warning(
                                f"🔧 RECONCILE {sym}: qty local {_qty_local} "
                                f"≠ exchange {_qty_ex} ({size} contratos) — "
                                f"adotando a da exchange (fonte de verdade)"
                            )
                            _pos_local.qty = _qty_ex
                            # qty_original serve de base para o TP parcial;
                            # só cresce, nunca encolhe abaixo da atual.
                            if _qty_ex > float(getattr(_pos_local, "qty_original", 0) or 0):
                                _pos_local.qty_original = _qty_ex
                    continue

                # ── POSIÇÃO ÓRFÃ: existe na exchange, não rastreada localmente ──
                side = p.get("side", "Buy")
                ep   = float(p.get("entryPrice", p.get("avgPrice", 0)) or 0)
                liq  = float(p.get("liquidationPrice", p.get("liqPrice", 0)) or 0)
                upnl = float(p.get("unrealisedPnl", 0) or 0)
                sl_existente = float(p.get("stopLoss", 0) or 0)
                tp_existente = float(p.get("takeProfit", 0) or 0)

                if ep <= 0:
                    log.error(
                        f"🚨 RECONCILE {sym}: posição órfã mas entryPrice "
                        f"inválido ({ep}) — NÃO é possível reconstruir com "
                        f"segurança. Permanece UNPROTECTED. Campos: "
                        f"{list(p.keys())}"
                    )
                    self._unprotected_symbols.add(sym)
                    continue

                direction = "LONG" if side == "Buy" else "SHORT"

                # ══════════════════════════════════════════════════
                # NOVO-F013A-1 — a Binance não traz stopLoss na linha da
                # posição: sem isto o loader SEMPRE enviava SL/TP estimados
                # (ATR/liquidação), mesmo com a proteção nativa ativa.
                # Timeout não é sinal de mercado: a lineage conhecida usa os
                # próprios níveis; origem desconhecida nunca substitui um stop
                # condicional já ativo por estimativa.
                # ══════════════════════════════════════════════════
                from bot import timeout_adoption as _ta
                _timeout_ctx = (getattr(self, "_timeout_adoptions", None) or {}).get(sym)
                _lineage_levels = None
                _conditional_sl = None
                if not self.paper_trade:
                    _tick = (getattr(self, "instruments", None) or {}).get(sym, {}).get("tickSize")
                    if _timeout_ctx and _timeout_ctx.get("direction") == direction:
                        _lineage_levels = await _ta.adopt_known_lineage(
                            self.client, sym, direction, p, _timeout_ctx, _tick, log)
                    elif _timeout_ctx:
                        log.critical(
                            f"🚨 [TIMEOUT_ADOPTION] {sym}: lado da exchange ({direction}) ≠ "
                            f"opening order ({_timeout_ctx.get('direction')}) — não adotada"
                        )
                        self._unprotected_symbols.add(sym)
                        continue
                    elif sl_existente <= 0:
                        _conditional_sl = await _ta.existing_conditional_stop(
                            self.client, sym, direction)

                # Preço de entrada vem da EXCHANGE (ep), nunca do ticker —
                # exigência explícita da correção. SL/TP: usa o que já
                # está na exchange se existir; caso contrário, calcula
                # um SL conservador baseado na liquidação (mesma fórmula
                # já usada e validada em _load_existing_positions).
                atr_est = ep * 0.007
                if _lineage_levels is not None:
                    sl, tp_existente = _lineage_levels[0], _lineage_levels[1]
                elif _conditional_sl:
                    sl = _conditional_sl
                elif sl_existente > 0:
                    sl = sl_existente
                elif direction == "LONG":
                    sl = max(liq * 1.02, ep - atr_est * 1.5) if liq > 0 else ep - atr_est * 1.5
                else:
                    sl = min(liq * 0.98, ep + atr_est * 1.5) if liq > 0 else ep + atr_est * 1.5
                tp = tp_existente if tp_existente > 0 else (
                    ep + atr_est * 3.0 if direction == "LONG" else ep - atr_est * 3.0
                )

                # EXEC-01: `size` vem de get_positions() e está em CONTRATOS.
                # Position.qty DEVE ser unidade base.
                try:
                    _base_qty = self._contracts_to_base_qty(sym, size)
                except ValueError as _ue:
                    log.critical(
                        f"🚨 RECONCILE {sym}: {_ue} — posição NÃO reconstruída, "
                        f"marcada como desprotegida para bloquear novas entradas"
                    )
                    self._unprotected_symbols.add(sym)
                    continue

                sig = Signal(sym, direction, ep, sl, tp, 0.75, "Reconciled orphan", 75)
                pos = Position(sig, _base_qty)
                pos.pnl = upnl
                cur = float(p.get("markPrice", ep))
                pos.update_pnl(cur)
                self.positions[sym] = pos

                log.warning(
                    f"🔧 RECONCILE {sym}: posição ÓRFÃ descoberta e "
                    f"reconstruída — {direction} {size} @ ${ep:.6f} "
                    f"(sl_exchange={'sim' if sl_existente>0 else 'NÃO — usando fallback'})"
                )

                # ── Proteção: só marca protegida se a exchange confirmar ──
                if _lineage_levels is not None:
                    # Lineage conhecida: proteção já reconciliada nos níveis
                    # originais por timeout_adoption (nunca estimativa).
                    if _lineage_levels[2]:
                        self._unprotected_symbols.discard(sym)
                    else:
                        self._unprotected_symbols.add(sym)
                elif _conditional_sl:
                    self._unprotected_symbols.discard(sym)
                    log.info(f"✓ RECONCILE {sym}: stop condicional já ativo "
                             f"(${_conditional_sl:.6f}) — nenhuma estimativa enviada")
                elif sl_existente > 0:
                    # A exchange já tinha um stop — nada a enviar, só
                    # confirmar que a marcação de risco está correta.
                    self._unprotected_symbols.discard(sym)
                    log.info(f"✓ RECONCILE {sym}: já possuía SL na exchange (${sl_existente:.6f})")
                else:
                    self._unprotected_symbols.add(sym)
                    try:
                        ok = await self.client.set_position_stops(sym, sl=sl, tp=tp)
                    except Exception as e:
                        ok = False
                        log.error(f"🚨 RECONCILE {sym}: set_position_stops levantou exceção: {e}")

                    if ok:
                        self._unprotected_symbols.discard(sym)
                        log.info(
                            f"✓ RECONCILE {sym}: proteção aplicada e "
                            f"confirmada — SL=${sl:.6f} TP=${tp:.6f}"
                        )
                    else:
                        log.critical(
                            f"🚨🚨 RECONCILE {sym}: posição órfã SEM PROTEÇÃO "
                            f"— set_position_stops falhou. Novas entradas "
                            f"permanecem bloqueadas. Intervenção manual pode "
                            f"ser necessária."
                        )

            return list(self._unprotected_symbols)

    async def _sync_positions(self):
        """Puxa posições abertas da exchange e reconcilia estado local."""
        try:
            all_pos   = await self.client.get_positions()
            open_syms = {}
            for p in all_pos:
                if float(p.get("size", 0)) > 0:
                    open_syms[p["symbol"]] = p

            # Posições fechadas remotamente
            for sym in list(self.positions.keys()):
                pos = self.positions[sym]
                if sym not in open_syms:
                    # Respeita tempo mínimo no trade (só SL/TP da Bybit fecha)
                    # Se a Bybit fechou, aceita — mas loga o motivo
                    hold_left = pos.min_hold_until - time.time()
                    if hold_left > 0:
                        log.warning(
                            f"⚠️ {sym} posição ausente na exchange antes do tempo mínimo "
                            f"({hold_left/60:.0f}min restantes) — motivo não confirmado nesta reconciliação"
                        )
                    # PnL bruto (sem taxas)
                    pnl_gross = pos.pnl
                    exit_px   = pos.current_price or pos.entry

                    # Taxas: 0.055% por lado (taker Bybit)
                    fee_open  = pos.qty * pos.entry * TAKER_FEE
                    fee_close = pos.qty * exit_px   * TAKER_FEE
                    total_fee = fee_open + fee_close
                    pnl_net   = pnl_gross - total_fee   # PnL LÍQUIDO

                    trade = Trade(
                        sym, pos.direction, pos.entry, exit_px,
                        pos.qty, pnl_gross, pos.opened_at,
                        fee_open=fee_open, fee_close=fee_close
                    )
                    trade.accounting_source = "ESTIMATED_LOCAL_MARK_AND_FEE_RATE"
                    from bot.durable_daily_pnl import checkpoint as checkpoint_daily_pnl
                    await checkpoint_daily_pnl(self, extra=trade)
                    self.stats.add(trade)
                    # Persiste fechamento no banco
                    tid = self._trade_ids.pop(sym, 0)
                    if tid:
                        # BUG CRÍTICO CORRIGIDO: usava 'price', variável que não
                        # existe neste escopo → NameError capturado pelo except.
                        # Efeito: NENHUM trade fechado remotamente (SL/TP da
                        # exchange) era persistido no banco. O histórico ficava
                        # com trades eternamente "abertos", inviabilizando
                        # métricas de performance e a calibração de pesos.
                        await db.save_trade_close(
                            tid, exit_px, pnl_net, total_fee,
                            (datetime.utcnow() - pos.opened_at).total_seconds() / 60,
                            exit_reason="EXCHANGE_CLOSE_UNCONFIRMED_ESTIMATE",
                        )
                    del self.positions[sym]
                    self._cooldown[sym] = time.time() + 1800

                    # ── Circuit breaker individual por ativo ───────────
                    await self._record_trade_result(sym, pnl_net)
                    # ── Journal estruturado com metadados ─────────────
                    rr_achieved = abs(pnl_net / max(abs(pos.entry - pos.sl), 0.0001) / pos.qty) if pos.sl else 0
                    self.daily_tracker.add_pnl(
                        pnl_net,
                        symbol=sym,
                        entry_type=getattr(pos, "entry_type", ""),
                        regime=getattr(pos, "regime", ""),
                        session=self._get_market_session(),
                        rr_achieved=round(rr_achieved, 2),
                    )

                    # ── 3 perdas consecutivas: registra, bot CONTINUA ──
                    consecutive = await db.update_consecutive_losses(pnl_net)
                    if consecutive >= 3:
                        log.warning(
                            f"⚠️ {consecutive} perdas consecutivas — registrado, bot continua"
                        )
                        _cbal = await self.client.get_balance()
                        await notify(await consecutive_losses_msg(consecutive, _cbal, _cbal*cfg.LEVERAGE))
                        await db.save_risk_event(
                            "CONSECUTIVE_LOSSES",
                            f"{consecutive} perdas consecutivas",
                            pnl_net,
                        )
                    log.info(
                        f"📭 {sym} fechado | ESTIMATIVA sem fills confirmados | Bruto=${pnl_gross:+.4f} "
                        f"Taxas=-${total_fee:.4f} | Líquido=${pnl_net:+.4f}"
                    )
                    _bal = await self.client.get_balance()
                    await notify("⚠️ Valores estimados pelo último preço local e taxa prevista; fills e funding não confirmados.\n" + await close_msg(sym, pos.direction, pnl_net, pos.pnl_pct(), exit_px, _bal, _bal*cfg.LEVERAGE))
                else:
                    # Atualiza dados da posição aberta
                    bp = open_syms[sym]
                    cur = float(bp.get("markPrice", pos.current_price))
                    upnl = float(bp.get("unrealisedPnl", pos.pnl))
                    pos.update_pnl(cur)
                    pos.pnl = upnl

            # Posições abertas externamente (ex: manual)
            for sym, bp in open_syms.items():
                if (not self.paper_trade and getattr(self, "_pilot_external_position_guard_patched", False)):
                    continue  # External adoption requires the dedicated ownership proof.
                if sym not in self.positions:
                    # CORRIGIDO: KuCoinClient.get_positions() normaliza como
                    # "entryPrice"; "avgPrice" era o formato da Bybit e retornava
                    # 0, zerando entry/SL/TP da posição carregada.
                    ep  = float(bp.get("entryPrice", bp.get("avgPrice", 0)))
                    sz  = float(bp.get("size", 0))
                    side = bp.get("side", "Buy")
                    if ep > 0 and sz > 0:
                        direction = "LONG" if side == "Buy" else "SHORT"
                        atr_est = ep * 0.007
                        if direction == "LONG":
                            sl = ep - atr_est * 1.5
                            tp = ep + atr_est * 3.0
                        else:
                            sl = ep + atr_est * 1.5
                            tp = ep - atr_est * 3.0
                        # EXEC-01: `sz` vem de get_positions() = CONTRATOS.
                        try:
                            _base_sz = self._contracts_to_base_qty(sym, sz)
                        except ValueError as _ue:
                            log.critical(
                                f"🚨 SYNC {sym}: {_ue} — posição externa NÃO "
                                f"carregada (unidade desconhecida)"
                            )
                            continue

                        sig = Signal(sym, direction, ep, sl, tp, 0.75, "sync exchange", 75)
                        pos = Position(sig, _base_sz)
                        pos.pnl = float(bp.get("unrealisedPnl", 0))
                        cur = float(bp.get("markPrice", ep))
                        pos.update_pnl(cur)
                        self.positions[sym] = pos
                        log.info(f"📥 Posição externa carregada: {sym} {direction}")

        except Exception as e:
            log.error(f"_sync_positions: {e}")

    # ── Trailing stop DESATIVADO ────────────────────────────────


    async def _monitor_news_pipeline(self):
        """
        Monitora o pipeline e alerta quando notícia de alto impacto aparece.
        Rodado a cada ciclo de background (30min).
        """
        try:
            from bot.news_pipeline import _pipeline_cache, get_pipeline_status
            from bot.notifier import high_impact_news_msg, news_summary_msg

            if not _pipeline_cache:
                return

            # Alertar top notícia se relevância >= 80 e recente (<30min)
            import time as _t
            for item in _pipeline_cache[:5]:
                if (item.relevance >= 80 and
                    item.sentiment != "NEUTRAL" and
                    (_t.time() - item.timestamp) < 1800):
                    from bot.news_pipeline import get_news_impact
                    impact = get_news_impact("LONG")
                    await notify(await high_impact_news_msg(
                        item.title, item.source, item.sentiment,
                        item.relevance, impact.get("score_pts", 0)
                    ))
                    break   # apenas 1 alerta por ciclo

            # Resumo a cada 6h
            now_h = __import__("datetime").datetime.utcnow().hour
            if now_h in (0, 6, 12, 18):
                st = get_pipeline_status()
                if st["total"] > 0:
                    top = [
                        {"source": i.source, "sentiment": i.sentiment,
                         "title": i.title}
                        for i in _pipeline_cache[:3]
                    ]
                    await notify(await news_summary_msg(
                        st["total"], st["bullish"], st["bearish"],
                        st["sources"], top
                    ))
        except Exception as e:
            log.debug(f"_monitor_news_pipeline: {e}")

    async def _notify_session_info(self):
        """Envia info da sessão atual no Telegram uma vez por hora."""
        sess = mdata.get_market_session()   # BUG: função vive em market_data
        if sess["quality"] < 50:
            await notify(
                f"{sess['emoji']} *SESSÃO FRACA — Bot em modo cauteloso*\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                f"⏰ Sessão: `{sess['session']}`\n"
                f"📊 Qualidade: `{sess['quality']}%`\n"
                f"📋 `{sess['description']}`\n"
                f"_Aguardando sessão de maior liquidez..._"
            )
        else:
            log.info(f"{sess['emoji']} Sessão: {sess['session']} (q={sess['quality']}%)")


    # ── Configuração do circuit breaker por ativo ─────────────────────────
    _MAX_CONSEC_LOSSES: int = int(os.environ.get('MAX_CONSEC_LOSSES', '3'))  # configurável via env var
    _CB_COOLDOWN_HOURS:  int = int(os.environ.get('CB_COOLDOWN_HOURS',  '24')) # configurável via env var

    async def _record_trade_result(self, symbol: str, pnl: float):
        """
        Registra resultado de um trade e ativa circuit breaker
        se o símbolo atingir MAX_CONSEC_LOSSES perdas consecutivas.

        Circuit breaker individual: mais cirúrgico que o drawdown global.
        Permite continuar operando outros pares enquanto um par problemático
        fica em cooldown de 24h.
        """
        if pnl < 0:
            self._consec_losses[symbol] = self._consec_losses.get(symbol, 0) + 1
            count = self._consec_losses[symbol]

            if count >= self._MAX_CONSEC_LOSSES:
                cooldown_until = time.time() + self._CB_COOLDOWN_HOURS * 3600
                self._cooldown[symbol] = cooldown_until
                log.warning(
                    f"🚫 [{symbol}] Circuit breaker ativado: "
                    f"{count} perdas consecutivas → "
                    f"cooldown de {self._CB_COOLDOWN_HOURS}h"
                )
                await notify(
                    f"🚫 *Circuit Breaker — {symbol}*\n"
                    f"`{count}` perdas consecutivas\n"
                    f"Cooldown: `{self._CB_COOLDOWN_HOURS}h`\n"
                    f"Retoma às: `{__import__('datetime').datetime.utcfromtimestamp(cooldown_until).strftime('%H:%M UTC')}`"
                )
        else:
            # Reset após lucro
            if self._consec_losses.get(symbol, 0) > 0:
                log.info(
                    f"✅ [{symbol}] Trade lucrativo — "
                    f"reset de perdas consecutivas "
                    f"({self._consec_losses[symbol]} → 0)"
                )
            self._consec_losses[symbol] = 0


    async def _check_stagnation_and_invalidation(self):
        """
        Saída por tempo: fecha posição se em 4h o preço não se moveu > 0.5x ATR.
        Saída por invalidação: fecha se CHoCH oposto aparece após a entrada.
        Saída por regime: fecha se regime mudou para RANGING/COMPRESSED/CHOPPY.
        Itens 8, 9, 15 da lista de melhorias.
        """
        from bot.indicators import atr as calc_atr
        from bot.strategy import detect_regime

        for sym, pos in list(self.positions.items()):
            try:
                k15 = self.client.get_cached_klines(sym, "15", limit=100) or []
                if len(k15) < 20:
                    continue

                closes = [float(k["c"]) for k in k15[:-1]]
                highs  = [float(k["h"]) for k in k15[:-1]]
                lows   = [float(k["l"]) for k in k15[:-1]]
                cur    = pos.current_price or closes[-1]

                atr_val = float(calc_atr(highs, lows, closes, 14)[-1])
                if atr_val <= 0:
                    continue

                # ── Saída por TEMPO ────────────────────────────────
                # Fecha se após 4h (16 candles 15M) o preço não se moveu > 0.5x ATR
                STAGNATION_BARS  = 16
                STAGNATION_MULT  = 0.5
                movement = abs(cur - pos.entry)
                bars_open = len(k15)  # proxy de tempo em trade
                if (bars_open >= STAGNATION_BARS
                        and movement < atr_val * STAGNATION_MULT):
                    log.info(
                        f"⏱️  [{sym}] Saída por TEMPO: {bars_open} candles aberto, "
                        f"movimento={movement:.4f} < {atr_val*STAGNATION_MULT:.4f} "
                        f"(0.5×ATR) → fechando para evitar funding acumulado"
                    )
                    close_side = "Sell" if pos.direction == "LONG" else "Buy"
                    await self.client.place_order(
                        symbol=sym, side=close_side,
                        qty=pos.qty, sl=0, tp=0,
                        instruments=self.instruments,
                        reduce_only=True,   # auditoria #3
                    )
                    continue

                # ── Saída por INVALIDAÇÃO (CHoCH oposto) ──────────
                # Se após entrada LONG aparece CHoCH de baixa → setup invalidado
                if len(closes) >= 10:
                    recent_c = closes[-10:]
                    recent_h = highs[-10:]
                    recent_l = lows[-10:]

                    # CHoCH simples: HH seguido de LL (topos e fundos)
                    choch_bear = (
                        recent_h[-1] < recent_h[-3] and
                        recent_l[-1] < recent_l[-3] and
                        recent_c[-1] < recent_c[-3]
                    )
                    choch_bull = (
                        recent_l[-1] > recent_l[-3] and
                        recent_h[-1] > recent_h[-3] and
                        recent_c[-1] > recent_c[-3]
                    )

                    invalidated = (
                        (pos.direction == "LONG"  and choch_bear) or
                        (pos.direction == "SHORT" and choch_bull)
                    )

                    if invalidated and not pos.tp1_hit:
                        log.info(
                            f"❌ [{sym}] Saída por INVALIDAÇÃO: CHoCH oposto detectado "
                            f"após entrada {pos.direction} → fechando antes do SL"
                        )
                        close_side = "Sell" if pos.direction == "LONG" else "Buy"
                        await self.client.place_order(
                            symbol=sym, side=close_side,
                            qty=pos.qty, sl=0, tp=0,
                            instruments=self.instruments,
                            reduce_only=True,   # auditoria #3
                        )
                        continue

                # ── Saída por MUDANÇA DE REGIME ────────────────────
                # Se o regime mudou para RANGING/COMPRESSED/CHOPPY após a entrada
                # EMA50 precisa de 50+ velas; 50 é o limite exato e gera NaN.
                k4h = self.client.get_cached_klines(sym, "240", limit=120) or []
                if len(k4h) >= 20:
                    c4h   = [float(k["c"]) for k in k4h[:-1]]
                    h4h   = [float(k["h"]) for k in k4h[:-1]]
                    l4h   = [float(k["l"]) for k in k4h[:-1]]
                    atr4h = float(calc_atr(h4h, l4h, c4h, 14)[-1])
                    regime_now = detect_regime(c4h, h4h, l4h, atr4h)

                    if regime_now in ("RANGING", "COMPRESSED", "CHOPPY") and not pos.tp1_hit:
                        log.info(
                            f"🔄 [{sym}] Saída por REGIME: mercado mudou para "
                            f"{regime_now} → setup trend-follow inválido, fechando"
                        )
                        close_side = "Sell" if pos.direction == "LONG" else "Buy"
                        await self.client.place_order(
                            symbol=sym, side=close_side,
                            qty=pos.qty, sl=0, tp=0,
                            instruments=self.instruments,
                            reduce_only=True,   # auditoria #3
                        )

            except Exception as e:
                log.error(f"_check_stagnation_and_invalidation {sym}: {e}")

    async def _manage_partial_tp(self):
        """
        Partial Take Profit: fecha 50% da posição ao atingir TP1,
        move SL para breakeven e deixa os 50% restantes correrem até TP2.

        TP1 = entry ± 1× risco (1:1 R:R) — captura rápida
        TP2 = tp original    — alvo final com trailing

        RISK-2: TP1 inclui custo de funding estimado (8h × taxa média 0.01%)
        para evitar fechar "lucrativo" com funding negativo acumulado.

        Benefício: garante lucro parcial, elimina risco de breakeven,
        melhora consistência do win rate ajustado por expectativa.
        """
        for sym, pos in list(self.positions.items()):
            try:
                if pos.tp1_hit:
                    continue   # já executou o parcial

                cur = pos.current_price
                if not cur or cur <= 0:
                    continue

                # TP1 = entry ± distância do SL (1:1 R:R)
                # RISK-2: adiciona custo estimado de funding (8h × 0.01% = 0.08%)
                # para garantir que partial TP seja genuinamente lucrativo
                risk_dist    = abs(pos.entry - pos.sl)
                if risk_dist <= 0:
                    continue
                funding_cost = pos.entry * 0.0001 * 3  # 3 períodos de 8h = 0.03%
                tp1_long  = pos.entry + risk_dist + funding_cost
                tp1_short = pos.entry - risk_dist - funding_cost
                tp1_price = tp1_long if pos.direction == "LONG" else tp1_short

                # Verificar se TP1 foi atingido
                tp1_hit = (
                    (pos.direction == "LONG"  and cur >= tp1_price) or
                    (pos.direction == "SHORT" and cur <= tp1_price)
                )
                if not tp1_hit:
                    continue

                # Calcular qty parcial (50% da posição original)
                # RISK-3: respeita qty_step do instrumento para evitar rejeição
                raw_partial = pos.qty_original * 0.5
                qty_step = 0.001  # fallback conservador
                if self.instruments:
                    inst = self.instruments.get(sym, {})
                    qty_step = float(inst.get("lotSizeFilter", {}).get("qtyStep", 0.001))
                if qty_step > 0:
                    partial_qty = round(raw_partial - (raw_partial % qty_step), len(str(qty_step).rstrip("0").split(".")[-1]))
                else:
                    partial_qty = round(raw_partial, 4)
                if partial_qty <= 0 or partial_qty > pos.qty:
                    continue

                # Fechar 50% da posição
                close_side = "Sell" if pos.direction == "LONG" else "Buy"
                result = await self.client.place_order(
                    symbol=sym, side=close_side,
                    qty=partial_qty, sl=0, tp=0,
                    instruments=self.instruments,
                    reduce_only=True,   # auditoria #3
                )
                # auditoria #4: só atualiza estado se a ordem foi aceita
                if not result or not result.get("orderId"):
                    log.error(
                        f"❌ TP parcial {sym} REJEITADO pela exchange — "
                        f"estado da posição mantido inalterado"
                    )
                    continue

                # Mover SL para breakeven — verificado.
                # Sem confirmação, a posição restante ficaria com o stop
                # original enquanto o bot a trataria como protegida.
                _be = await self.client.set_sl(sym, pos.entry)
                if not _be:
                    log.error(
                        f"🚨 {sym}: TP parcial executado mas SL NÃO moveu "
                        f"para break-even (${pos.entry:.4f}) — restante "
                        f"ainda com stop original"
                    )

                # Atualizar estado da posição
                pnl_partial = risk_dist * partial_qty   # PnL gross do parcial
                fee_p = partial_qty * cur * TAKER_FEE * 2   # taxa KuCoin
                pnl_net = pnl_partial - fee_p

                pos.tp1_hit     = True
                pos.sl          = pos.entry   # SL no breakeven
                pos.trailing_sl = pos.entry
                pos.qty         = pos.qty - partial_qty   # atualiza qty restante

                log.info(
                    f"✂️  [{sym}] Partial TP1: fechou {partial_qty} @ {cur:.6f} "
                    f"| PnL parcial: ${pnl_net:.2f} "
                    f"| SL → breakeven {pos.entry:.6f} "
                    f"| Restante: {pos.qty:.4f} até TP2={pos.tp:.6f}"
                )
                await notify(
                    f"✂️ *Partial TP1 — {sym}*\n"
                    f"Fechou 50% @ `{cur:.4f}`\n"
                    f"PnL parcial: `${pnl_net:.2f}`\n"
                    f"SL movido para breakeven\n"
                    f"Restante correndo até TP2 `{pos.tp:.4f}`"
                )
            except Exception as e:
                log.error(f"_manage_partial_tp {sym}: {e}")

    async def _apply_trailing_stops(self):
        """
        Trailing Stop progressivo.
        Ativa quando lucro >= 50% do alvo (cfg.TRAILING_TRIGGER).
        Trava 25% * 10% = 2.5% abaixo do pico (cfg.TRAILING_LOCK * 0.1).
        Nunca recua abaixo do SL original — protege capital sem cortar early.
        Move o SL na exchange via /v5/position/trading-stop.
        """
        for sym, pos in list(self.positions.items()):
            if getattr(pos, "_geometry_unproven", False) is True:
                continue    # Q-01: trailing never runs on an invented geometry
            try:
                # Atualiza PnL com preço atual
                cur = pos.current_price
                if not cur or cur <= 0:
                    continue
                pos.update_pnl(cur)

                # Calcula novo SL via método da Position
                new_sl = pos.calc_trailing_sl()
                if new_sl is None:
                    continue

                # Só move se o SL melhorou (LONG: sobe, SHORT: desce)
                improved = (
                    (pos.direction == "LONG"  and new_sl > pos.trailing_sl) or
                    (pos.direction == "SHORT" and new_sl < pos.trailing_sl)
                )
                if not improved:
                    continue

                old_sl = pos.trailing_sl

                # RISCO CORRIGIDO: o estado interno era atualizado SEM
                # verificar se a exchange aceitou o novo stop.
                #
                # Se set_sl falhasse, o bot passava a acreditar que o stop
                # estava mais apertado do que realmente estava — calculando
                # risco, break-even e trailing sobre um valor fictício.
                # Numa reversão, a perda real seria maior que a esperada.
                _ok = await self.client.set_sl(sym, new_sl)
                if not _ok:
                    log.error(
                        f"🚨 [{sym}] Trailing FALHOU: exchange não aceitou "
                        f"SL {new_sl:.6f} — mantendo estado em {old_sl:.6f}. "
                        f"O stop real continua no valor anterior."
                    )
                    continue

                pos.trailing_sl = new_sl
                pos.sl          = new_sl   # mantém sl e trailing_sl sincronizados

                if self.paper_trade and self._durable_state_enforced:
                    await durable.persist_paper_runtime(
                        self, f"trailing_sl:{sym}", strict=True
                    )

                log.info(
                    f"🔒 [{sym}] Trailing SL: {old_sl:.6f} → {new_sl:.6f} "
                    f"| preço={cur:.6f} pnl=${pos.pnl:.2f} "
                    f"(ativo={pos.trailing_active})"
                )
            except Exception as e:
                log.error(f"_apply_trailing_stops {sym}: {e}")

    # ── Fecha posição quando lucro = 2x o risco (R:R dobrado) ──
    async def _check_rr_double(self):
        """
        Fecha a posição quando o lucro atingir o dobro do risco original.
        Ex: risco = $5 → fecha quando lucro = $10.
        NÃO fecha por ruído, micro reversões ou trailing.
        Só fecha 100% da posição — sem parciais.
        """
        if not self.paper_trade:
            from bot.confirmed_rr_exit import check
            return await check(self)
        for sym, pos in list(self.positions.items()):
            try:
                risk_dist   = abs(pos.entry - pos.sl)   # distância SL original
                if risk_dist <= 0:
                    continue

                price = pos.current_price or pos.entry
                if pos.direction == "LONG":
                    lucro_dist = price - pos.entry
                else:
                    lucro_dist = pos.entry - price

                # Lucro atingiu o dobro do risco? → fecha 100%
                if lucro_dist >= risk_dist * 2.0:
                    rr_atual = lucro_dist / risk_dist
                    log.info(
                        f"🎯 {sym} R:R dobrado! "
                        f"Lucro={lucro_dist:.4f} ≥ 2x Risco={risk_dist:.4f} "
                        f"(R:R={rr_atual:.2f}) → fechando 100%"
                    )
                    close_side = "Sell" if pos.direction == "LONG" else "Buy"
                    _res = await self.client.place_order(
                        symbol=sym, side=close_side,
                        qty=pos.qty, sl=0, tp=0,
                        instruments=self.instruments,
                        reduce_only=True,   # auditoria #3
                    )
                    # auditoria #4: não marca como fechada se foi rejeitada
                    if not _res or not _res.get("orderId"):
                        log.error(
                            f"❌ Fechamento R:R {sym} REJEITADO — "
                            f"posição permanece monitorada"
                        )
                        continue
                    # Registra como trade fechado
                    pnl_gross = lucro_dist * pos.qty
                    fee_open  = pos.qty * pos.entry * TAKER_FEE
                    fee_close = pos.qty * price     * TAKER_FEE
                    total_fee = fee_open + fee_close
                    pnl_net   = pnl_gross - total_fee
                    fee_open  = pos.qty * pos.entry * TAKER_FEE
                    fee_close = pos.qty * price     * TAKER_FEE
                    trade = Trade(
                        sym, pos.direction, pos.entry, price,
                        pos.qty, pnl_gross, pos.opened_at,
                        fee_open=fee_open, fee_close=fee_close,
                    )
                    from bot.durable_daily_pnl import checkpoint as checkpoint_daily_pnl
                    await checkpoint_daily_pnl(self, extra=trade)
                    self.stats.add(trade)
                    # Persiste fechamento no banco
                    tid = self._trade_ids.pop(sym, 0)
                    if tid:
                        await db.save_trade_close(
                            tid, price, pnl_net, total_fee,
                            (datetime.utcnow() - pos.opened_at).total_seconds() / 60,
                            exit_reason="RR_DOUBLE",   # fechado pelo R:R dobrado
                        )
                    del self.positions[sym]
                    self._cooldown[sym] = time.time() + 1800

                    # ── Circuit breaker individual por ativo ───────────
                    await self._record_trade_result(sym, pnl_net)
                    # ── Journal estruturado com metadados ─────────────
                    rr_achieved = abs(pnl_net / max(abs(pos.entry - pos.sl), 0.0001) / pos.qty) if pos.sl else 0
                    self.daily_tracker.add_pnl(
                        pnl_net,
                        symbol=sym,
                        entry_type=getattr(pos, "entry_type", ""),
                        regime=getattr(pos, "regime", ""),
                        session=self._get_market_session(),
                        rr_achieved=round(rr_achieved, 2),
                    )

                    # ── 3 perdas consecutivas: registra, bot CONTINUA ──
                    consecutive = await db.update_consecutive_losses(pnl_net)
                    if consecutive >= 3:
                        log.warning(
                            f"⚠️ {consecutive} perdas consecutivas — registrado, bot continua"
                        )
                        _cbal = await self.client.get_balance()
                        await notify(await consecutive_losses_msg(consecutive, _cbal, _cbal*cfg.LEVERAGE))
                        await db.save_risk_event(
                            "CONSECUTIVE_LOSSES",
                            f"{consecutive} perdas consecutivas",
                            pnl_net,
                        )
                    _bal2 = await self.client.get_balance()
                    await notify(await close_msg(sym, pos.direction, pnl_net, pos.pnl_pct(), price, _bal2, _bal2*cfg.LEVERAGE))
            except Exception as e:
                log.error(f"_check_rr_double {sym}: {e}")

    # ── Scan & Enter ────────────────────────────────────────────
    # ── Grupos de correlação — pares com beta > 0.8 entre si ─────────
    # Limite: no máximo 1 posição aberta por grupo simultaneamente
    _CORR_GROUPS: list = [
        {"BTCUSDT", "ETHUSDT"},              # BTC e ETH: correlação ~0.92
        {"SOLUSDT", "AVAXUSDT", "DOTUSDT"},  # L1 alternativos: correlação ~0.88
        {"BNBUSDT"},                          # BNB: isolado (exchange token)
        {"XRPUSDT", "ADAUSDT"},              # pagamentos/contratos: correlação ~0.85
        {"DOGEUSDT", "MATICUSDT"},           # meme/polygon: correlação ~0.80
        {"LINKUSDT", "LTCUSDT"},             # oráculos/store-of-value
    ]


    # ── Sessões de mercado e penalidades por par ──────────────────────────
    # Cripto tem comportamento diferente por sessão:
    # ASIA (00-08 UTC): volume baixo, altcoins fracas, BTC/ETH ok
    # LONDON (08-16 UTC): tendências se formam, liquidez crescente
    # NEW_YORK (16-24 UTC): maior volume, breakouts mais confiáveis
    _SESSION_PENALTY: dict = {
        "ASIA":     {"SOLUSDT": -8, "BNBUSDT": -8, "XRPUSDT": -5,
                     "DOGEUSDT": -10, "MATICUSDT": -8, "AVAXUSDT": -8},
        "LONDON":   {},   # sem penalidades — boa sessão para todos
        "NEW_YORK": {},   # sem penalidades — melhor sessão para breakouts
    }


    # ── Regime Switching: parâmetros por regime de mercado ────────────────
    # Comportamento diferente para cada condição de mercado:
    #   TRENDING_UP/DOWN: score relaxado, direção bloqueada, TP maior
    #   RANGING:          score alto, ambas direções, TP menor (mean reversion)
    #   COMPRESSED:       não opera — aguarda breakout (bloqueado no analyze_mtf)
    _REGIME_PARAMS: dict = {
        "TRENDING_UP": {
            "min_score":      55,    # entrada mais fácil com tendência
            "allowed_sides":  ["LONG"],   # só long — não nadar contra a maré
            "sl_mult_adj":    +0.2,  # SL um pouco mais largo em tendência
            "tp_mult_adj":    +0.5,  # TP maior — tendências andam mais
            "score_bonus":    +5,    # bônus de score por estar na direção certa
        },
        "TRENDING_DOWN": {
            "min_score":      55,
            "allowed_sides":  ["SHORT"],
            "sl_mult_adj":    +0.2,
            "tp_mult_adj":    +0.5,
            "score_bonus":    +5,
        },
        "RANGING": {
            "min_score":      72,    # exige score alto — sem tendência, mais falsos
            "allowed_sides":  ["LONG", "SHORT"],
            "sl_mult_adj":    -0.2,  # SL mais apertado em range
            "tp_mult_adj":    -0.8,  # TP menor — range não anda muito
            "score_bonus":    -5,    # penalidade por mercado lateral
        },
        "COMPRESSED": {
            "min_score":      999,   # nunca opera (bloqueado)
            "allowed_sides":  [],
            "sl_mult_adj":    0.0,
            "tp_mult_adj":    0.0,
            "score_bonus":    0,
        },
    }

    def _get_regime_params(self, regime: str) -> dict:
        """Retorna parâmetros ajustados para o regime atual."""
        return self._REGIME_PARAMS.get(regime, self._REGIME_PARAMS["RANGING"])

    def _regime_allows_direction(self, regime: str, direction: str) -> bool:
        """Verifica se o regime atual permite a direção do sinal."""
        rp = self._get_regime_params(regime)
        allowed = rp.get("allowed_sides", ["LONG", "SHORT"])
        if direction not in allowed:
            log.info(
                f"[Regime {regime}] Direção {direction} bloqueada "
                f"— apenas {allowed} permitido neste regime"
            )
            return False
        return True

    @staticmethod
    def _get_market_session() -> str:
        """Retorna a sessão de mercado atual com base no horário UTC."""
        hour = datetime.now(timezone.utc).hour
        if 0 <= hour < 8:
            return "ASIA"
        elif 8 <= hour < 16:
            return "LONDON"
        else:
            return "NEW_YORK"

    def _session_score_adjustment(self, symbol: str, base_score: int) -> int:
        """
        Ajusta o score de entrada com base na sessão de mercado.
        Penaliza altcoins de baixa liquidez na sessão asiática.
        Retorna score ajustado (nunca abaixo de 0).
        """
        session = self._get_market_session()
        penalty = self._SESSION_PENALTY.get(session, {}).get(symbol, 0)
        if penalty != 0:
            log.debug(
                f"[{symbol}] Sessão {session}: "
                f"score {base_score} {penalty:+d} = {base_score + penalty}"
            )
        return max(0, base_score + penalty)

    def _correlation_allows(self, symbol: str) -> bool:
        """
        Retorna True se é seguro abrir posição em 'symbol'.
        Regra: máximo 1 posição aberta por grupo de correlação.
        Isso garante que MAX_POSITIONS=3 representa 3 apostas DISTINTAS,
        não 3x a mesma aposta direcional em cripto.
        """
        for group in self._CORR_GROUPS:
            if symbol not in group:
                continue
            # Verifica se já existe posição aberta em outro membro do grupo
            for open_sym in self.positions:
                if open_sym != symbol and open_sym in group:
                    log.info(
                        f"[{symbol}] Bloqueado por correlação: "
                        f"{open_sym} já aberto no mesmo grupo {group}"
                    )
                    return False
        return True

    async def _scan_all_and_enter(self):
        """
        Multi-Timeframe scan: busca 15m, 1h e 4h para cada símbolo.
        Fast-track: usa WebSocket cache quando disponível (sem latência REST).
        Fallback: busca os três timeframes em paralelo via asyncio.gather().
        Só entra quando os timeframes apontam na mesma direção.
        """
        candidates = []
        min_score = self._effective_score()

        # Prova de vida do scan. Sem isso, viable_symbols vazio fazia o
        # método rodar e retornar sem analisar nada — indistinguível de
        # "o bot parou de funcionar".
        _alvos = self.viable_symbols or []
        if not _alvos:
            log.warning(
                "⛔ viable_symbols VAZIO — nenhum par para analisar. "
                "O filtro de viabilidade rejeitou todos ou ainda não rodou."
            )
            return

        # Thresholds mínimos para considerar o cache WS "suficiente"
        WS_MIN_15  = 20
        WS_MIN_1H  = 15
        WS_MIN_4H  = 10
        # Thresholds mínimos para prosseguir com a análise (após REST fallback)
        ANAL_MIN_15 = 20
        ANAL_MIN_1H = 15
        ANAL_MIN_4H = 10

        for sym in self.viable_symbols:
            if sym in self.positions:
                continue
            # Filtro de correlação: não abre posição em par do mesmo grupo
            if not self._correlation_allows(sym):
                continue
            cooldown_left = self._cooldown.get(sym, 0) - time.time()
            if cooldown_left > 0:
                log.debug(f"[{sym}] cooldown {cooldown_left/60:.0f}min → skip")
                continue
            try:
                # ── Fast-track: WebSocket cache (zero latência REST) ──
                k15 = self.client.get_cached_klines(sym, "15",  100)
                k1h = self.client.get_cached_klines(sym, "60",  100)
                k4h = self.client.get_cached_klines(sym, "240", 100)

                ws_hit = (
                    len(k15) >= WS_MIN_15
                    and len(k1h) >= WS_MIN_1H
                    and len(k4h) >= WS_MIN_4H
                )

                if ws_hit:
                    log.debug(
                        f"🔍 [{sym}] WS cache hit "
                        f"(15m={len(k15)} 1h={len(k1h)} 4h={len(k4h)}) — sem REST"
                    )
                else:
                    # ── Fallback REST paralelo (sem delays sequenciais) ──
                    missing = []
                    if len(k15) < WS_MIN_15:
                        missing.append(("15",  100))
                    if len(k1h) < WS_MIN_1H:
                        missing.append(("60",  100))
                    if len(k4h) < WS_MIN_4H:
                        missing.append(("240", 100))

                    log.debug(
                        f"🔍 [{sym}] WS cache miss "
                        f"(15m={len(k15)} 1h={len(k1h)} 4h={len(k4h)}) "
                        f"— REST paralelo para {[m[0] for m in missing]}"
                    )

                    results = await asyncio.gather(
                        *[self.client.get_klines(sym, iv, lim) for iv, lim in missing],
                        return_exceptions=True,
                    )

                    idx = 0
                    if len(k15) < WS_MIN_15:
                        r = results[idx]; idx += 1
                        if not isinstance(r, Exception):
                            k15 = r
                    if len(k1h) < WS_MIN_1H:
                        r = results[idx]; idx += 1
                        if not isinstance(r, Exception):
                            k1h = r
                    if len(k4h) < WS_MIN_4H:
                        r = results[idx]; idx += 1
                        if not isinstance(r, Exception):
                            k4h = r

                if len(k15) < ANAL_MIN_15 or len(k1h) < ANAL_MIN_1H or len(k4h) < ANAL_MIN_4H:
                    # Era log.debug — invisível. Se o cache e o REST
                    # falharem, TODOS os pares saem aqui e o scan termina
                    # sem analisar nada, sem deixar rastro.
                    log.warning(
                        f"⛔ [{sym}] SEM DADOS: 15m={len(k15)}/{ANAL_MIN_15} "
                        f"1h={len(k1h)}/{ANAL_MIN_1H} 4h={len(k4h)}/{ANAL_MIN_4H} "
                        f"— cache e REST falharam"
                    )
                    continue

                sig = self.analyzer.analyze_mtf(
                    sym, k15, k1h, k4h,
                    min_score=min_score,
                    fee_mult=cfg.FEE_MULTIPLIER,
                    vol_mult=cfg.MIN_VOLUME_MULT,
                )
                if sig:
                    # ══════════════════════════════════════════════
                    # FUNIL PÓS-SINAL — agora com log VISÍVEL.
                    #
                    # Estes três filtros usavam log.debug (invisível com
                    # LOG_LEVEL=INFO) ou nenhum log. Um sinal com score 79
                    # podia ser descartado aqui sem deixar rastro, dando a
                    # impressão de que o problema era o score.
                    # ══════════════════════════════════════════════

                    # 1. Ajuste por sessão de mercado
                    adjusted = self._session_score_adjustment(sym, sig.score)
                    if adjusted < min_score:
                        log.info(
                            f"⛔ [{sym}] REJEITADO no ajuste de sessão: "
                            f"{sig.score}→{adjusted} < {min_score} "
                            f"(sessão {self._get_market_session()})"
                        )
                        continue
                    sig.score = adjusted

                    # 2. Regime permite a direção?
                    regime_from_sig = getattr(sig, "regime", "RANGING")
                    if not self._regime_allows_direction(regime_from_sig, sig.direction):
                        log.info(
                            f"⛔ [{sym}] REJEITADO pelo regime: "
                            f"{sig.direction} não permitido em "
                            f"{regime_from_sig} (score era {sig.score})"
                        )
                        continue

                    # 3. PnL esperado positivo após taxas?
                    if sig.expected_pnl <= 0:
                        log.info(
                            f"⛔ [{sym}] REJEITADO por PnL: "
                            f"{sig.expected_pnl:.3f}% ≤ 0 após taxas "
                            f"(score {sig.score}, R:R {sig.rr:.2f})"
                        )
                        continue

                    log.info(
                        f"✅ [{sym}] CANDIDATO: {sig.direction} score={sig.score} "
                        f"R:R={sig.rr:.2f} PnL_est={sig.expected_pnl:+.2f}%"
                    )
                    candidates.append(sig)

                    # Histórico de scores para diagnóstico no heartbeat:
                    # responde "quão perto o bot está de operar?"
                    _hist = getattr(self, "_score_hist", [])
                    _hist.append(sig.score)
                    self._score_hist = _hist[-200:]

                    # Guarda o melhor score visto (usado pelo heartbeat)
                    _prev = getattr(self, "_last_best_score", None)
                    if not _prev or sig.score > _prev.get("score", 0):
                        self._last_best_score = {
                            "symbol":    sym,
                            "score":     sig.score,
                            "direction": sig.direction,
                        }

                    # Notifica no Telegram TODO sinal detectado, mesmo que a
                    # ordem não chegue a ser aberta (falta de margem, filtro,
                    # limite de posições). Antes só havia notificação após a
                    # ordem ser aceita — por isso nada chegava no Telegram.
                    asyncio.create_task(notify(
                        f"{'🟢' if sig.direction == 'LONG' else '🔴'} *SINAL DETECTADO*\n"
                        f"`━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                        f"📍 Par:     `{sym}`\n"
                        f"📊 Direção: `{sig.direction}`\n"
                        f"🧠 Score:   `{sig.score}/100`\n"
                        f"💰 Entrada: `${sig.entry:,.4f}`\n"
                        f"🛑 SL:      `${sig.sl:,.4f}`\n"
                        f"🎯 TP:      `${sig.tp:,.4f}`\n"
                        f"⚖️ R:R:     `{sig.rr:.2f}`\n"
                        f"📈 PnL est: `+{sig.expected_pnl:.2f}%`\n"
                        f"`━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                        f"_{getattr(sig, 'reason', '')[:120]}_"
                    ))
                    log.debug(
                        f"🎯 SINAL PREMIUM: {sym} score={sig.score}/100 "
                        f"{sig.direction} R:R={sig.rr} "
                        f"PnL_líq≈+{sig.expected_pnl:.2f}% | {sig.reason}"
                    )
                    signal_reason = (
                        f"{sig.reason} | regime={getattr(sig,'regime','TREND')} "
                        f"RSI={getattr(sig,'rsi',0):.0f} vol={getattr(sig,'vol_ratio',0):.2f}x "
                        f"4H=↑ 1H=↑"
                    )
                    await db.log_decision(sym, "SIGNAL", sig.score, signal_reason)
                else:
                    # Mostra score parcial para diagnóstico
                    try:
                        from bot.strategy import score_tf, detect_regime
                        from bot.indicators import atr as atr_fn
                        def ga(kl): return ([k["c"] for k in kl],[k["h"] for k in kl],[k["l"] for k in kl],[k["o"] for k in kl],[k["v"] for k in kl])
                        c15,h15,l15,o15,v15 = ga(k15)
                        c1h,h1h,l1h,o1h,v1h = ga(k1h)
                        c4h,h4h,l4h,o4h,v4h = ga(k4h)
                        def get_atr(h,l,c):
                            a=atr_fn(h,l,c); return a[-1], float(np.mean(a[-20:])) if len(a)>=20 else a[-1]
                        av15,ag15=get_atr(h15,l15,c15)
                        av1h,ag1h=get_atr(h1h,l1h,c1h)
                        av4h,ag4h=get_atr(h4h,l4h,c4h)
                        e20_4h=__import__('bot.indicators',fromlist=['ema']).ema(c4h,20)[-1]
                        e50_4h=__import__('bot.indicators',fromlist=['ema']).ema(c4h,50)[-1]
                        e20_1h=__import__('bot.indicators',fromlist=['ema']).ema(c1h,20)[-1]
                        e50_1h=__import__('bot.indicators',fromlist=['ema']).ema(c1h,50)[-1]
                        # ══════════════════════════════════════════════
                        # BUG CORRIGIDO — NaN NA EMA50 ZERAVA A DIREÇÃO
                        #
                        # A validação testava apenas isnan(e20). Se a EMA50
                        # fosse NaN (candles insuficientes — comum no 4H, que
                        # busca só 50-60 velas), TODAS as comparações com NaN
                        # retornam False:
                        #     e20 > NaN → False
                        #     e20 < NaN → False
                        # Resultado: bull=False E bear=False ao mesmo tempo,
                        # exatamente o que apareceu nos logs
                        # ("bull4h=False bear4h=False bull1h=False bear1h=False").
                        #
                        # Efeito: o par era descartado como "4H/1H não
                        # alinhados" mesmo em tendência clara — sem que o
                        # motivo real (dados insuficientes) fosse registrado.
                        # ══════════════════════════════════════════════
                        _np = __import__('numpy')

                        def _valid(*vals):
                            return all(v is not None and not _np.isnan(v) for v in vals)

                        _ok_4h = _valid(e20_4h, e50_4h)
                        _ok_1h = _valid(e20_1h, e50_1h)

                        if not _ok_4h or not _ok_1h:
                            log.warning(
                                f"[{sym}] EMAs indisponíveis "
                                f"(4H ok={_ok_4h} com {len(c4h)} velas, "
                                f"1H ok={_ok_1h} com {len(c1h)} velas) — "
                                f"EMA50 precisa de 50+ candles. Direção não "
                                f"pode ser determinada."
                            )

                        bull_4h = _ok_4h and e20_4h > e50_4h and c4h[-1] > e20_4h
                        bear_4h = _ok_4h and e20_4h < e50_4h and c4h[-1] < e20_4h
                        bull_1h = _ok_1h and e20_1h > e50_1h and c1h[-1] > e20_1h
                        bear_1h = _ok_1h and e20_1h < e50_1h and c1h[-1] < e20_1h
                        direction = "LONG" if (bull_4h or bull_1h) else "SHORT"
                        s4=score_tf(c4h,h4h,l4h,o4h,v4h,direction,av4h,ag4h)
                        s1=score_tf(c1h,h1h,l1h,o1h,v1h,direction,av1h,ag1h)
                        s15=score_tf(c15,h15,l15,o15,v15,direction,av15,ag15)
                        combined=round(s4["total"]*0.30+s1["total"]*0.30+s15["total"]*0.40)
                        regime=detect_regime(c4h,h4h,l4h,av4h)
                        from bot.indicators import rsi as rsi_fn
                        rsi_v=rsi_fn(c15)[-1]
                        vols=__import__('numpy').array(v15); avg_vol=vols[-21:-1].mean() if len(vols)>21 else vols.mean() or 1
                        vol_r=vols[-1]/avg_vol
                        log.debug(
                            f"[{sym}] Score={combined}/100 (4H:{s4['total']} 1H:{s1['total']} 15M:{s15['total']}) "
                            f"| regime={regime} RSI={rsi_v:.0f} vol={vol_r:.2f}x "
                            f"| 4H={'↑' if bull_4h else '↓' if bear_4h else '→'} "
                            f"1H={'↑' if bull_1h else '↓' if bear_1h else '→'} → HOLD"
                        )
                    except Exception as ex:
                        # BUG CORRIGIDO: a exceção era descartada e logada como
                        # "Sem sinal". Um erro real na análise (indicador, dados
                        # malformados) ficava indistinguível de ausência
                        # legítima de setup — mascarando falhas por tempo
                        # indeterminado.
                        log.warning(
                            f"[{sym}] ✗ Falha ao montar log de diagnóstico: "
                            f"{type(ex).__name__}: {ex}"
                        )
                        combined = 0
                        regime = "UNKNOWN"
                        rsi_v = 0
                        vol_r = 0
                    # Salva score real no banco para o dashboard
                    hold_reason = (
                        f"regime={regime} RSI={rsi_v:.0f} vol={vol_r:.2f}x "
                        f"4H={'↑' if locals().get('bull_4h') else '↓' if locals().get('bear_4h') else '→'} "
                        f"1H={'↑' if locals().get('bull_1h') else '↓' if locals().get('bear_1h') else '→'}"
                    )
                    await db.log_decision(sym, "HOLD", combined, hold_reason)
                    # Alerta "quase entrando" — DESATIVADO por padrão.
                    #
                    # Com 12 pares em tendência, esses alertas disparavam
                    # em bloco a cada scan. Como o notifier impõe 3s entre
                    # mensagens, eles formavam fila e ATRASAVAM os avisos
                    # que realmente importam (sinal aprovado, ordem aberta,
                    # ordem rejeitada por liquidação).
                    #
                    # Reativável com ALERT_QUASE_ENTRANDO=true.
                    _quase_on = os.environ.get(
                        "ALERT_QUASE_ENTRANDO", "false"
                    ).lower() == "true"
                    if _quase_on and cfg.MIN_ENTRY_SCORE - 5 <= combined < cfg.MIN_ENTRY_SCORE:
                        asyncio.create_task(notify(
                            f"🔔 *QUASE ENTRANDO — {sym}*\n"
                            f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                            f"📊 Score: `{combined}/{cfg.MIN_ENTRY_SCORE}` (faltam {cfg.MIN_ENTRY_SCORE - combined}pts)\n"
                            f"📍 Par: `{sym}`\n"
                            f"🕐 Regime: `{regime}`\n"
                            f"_Monitorando..._"
                        ))
            except Exception as e:
                log.error(f"scan {sym}: {e}")

        # ══════════════════════════════════════════════════════════
        # RESUMO DO CICLO DE SCAN
        #
        # As linhas de score por par são individuais e se perdem no meio
        # do log (ainda mais com DEBUG do WS ativo). Este resumo único
        # mostra o estado de TODOS os pares de uma vez, em INFO.
        # ══════════════════════════════════════════════════════════
        try:
            from bot.strategy import get_score_log
            _recentes = get_score_log(len(self.viable_symbols) or 12)
            if _recentes:
                _mn    = cfg.MIN_ENTRY_SCORE
                _linha = " | ".join(
                    f"{x['symbol'].replace('USDT',''):<5}{x['score']:>3}"
                    for x in _recentes
                )
                _mx     = max(x["score"] for x in _recentes)
                _perto  = len([x for x in _recentes if x["score"] >= _mn - 5])
                log.info(
                    f"🔎 SCAN: {len(_recentes)} pares | máx={_mx} "
                    f"(mín={_mn}) | {_perto} a ≤5pts | "
                    f"{len(candidates)} aprovados"
                )
                log.info(f"   {_linha}")

                # Quando algum par chega perto, mostra qual TF está fraco.
                if _perto and not candidates:
                    _top = max(_recentes, key=lambda x: x["score"])
                    _fraco = min(
                        [("4H", _top["s4h"]), ("1H", _top["s1h"]), ("15M", _top["s15"])],
                        key=lambda t: t[1],
                    )
                    log.info(
                        f"   ↳ melhor: {_top['symbol']} {_top['score']} "
                        f"(4H:{_top['s4h']} 1H:{_top['s1h']} 15M:{_top['s15']}) "
                        f"— {_fraco[0]} é o mais fraco ({_fraco[1]})"
                    )
        except Exception as _e:
            log.debug(f"resumo do scan: {_e}")

        # Ordena por score decrescente e entra nos melhores
        candidates = self.analyzer.rank_signals(candidates)
        for sig in candidates:
            # RACE CONDITION CORRIGIDA: _open() escreve em self.positions,
            # mas rodava FORA do _pos_lock que protege o ciclo de gestão
            # (_sync_positions, _check_rr_double, trailing...).
            #
            # Sem o lock, uma posição podia ser inserida enquanto outro
            # caminho iterava sobre o dict — gerando estado inconsistente
            # ou ordens duplicadas para o mesmo símbolo.
            #
            # A checagem de MAX_POSITIONS também precisa estar sob o lock,
            # senão duas iterações podem ler o mesmo valor e abrir posições
            # além do limite.
            async with self._pos_lock:
                if len(self.positions) >= cfg.MAX_POSITIONS:
                    break
                if sig.symbol in self.positions:
                    log.debug(f"[{sig.symbol}] já tem posição aberta — pulando")
                    continue
                await self._open(sig)

    async def _nexus_validate(self, sig: Signal):
        """
        Consulta o NEXUS AI Decision Engine para o sinal proposto.

        Reúne os dados disponíveis e delega a decisão. Falhas propagam
        para o gate fail-closed em _open; nunca autorizam fallback.
        """
        try:
            k15 = self.client.get_cached_klines(sig.symbol, "15",  200)
            k1h = self.client.get_cached_klines(sig.symbol, "60",  100)
            k4h = self.client.get_cached_klines(sig.symbol, "240",  120)

            # Cache insuficiente → busca via REST (sem inventar dados)
            missing = []
            if len(k15) < 60: missing.append(("15", 200))
            if len(k1h) < 40: missing.append(("60", 100))
            if len(k4h) < 20: missing.append(("240", 120))
            if missing:
                try:
                    fetched = await asyncio.gather(
                        *[self.client.get_klines(sig.symbol, iv, lim)
                          for iv, lim in missing],
                        return_exceptions=True,
                    )
                    for (iv, _), data in zip(missing, fetched):
                        if isinstance(data, Exception) or not data:
                            continue
                        if iv == "15":  k15 = data
                        elif iv == "60": k1h = data
                        elif iv == "240": k4h = data
                except Exception as e:
                    log.debug(f"nexus fetch {sig.symbol}: {e}")

            # Dados opcionais — ausência é registrada, não estimada
            ticker = self.client.get_cached_ticker(sig.symbol) or None
            funding = oi = oi_delta = None
            try:
                funding = await self.client.get_funding_rate(sig.symbol)
            except Exception as _e:
                log.debug(f"nexus: funding indisponível para {sig.symbol}: {_e}")
            try:
                oi = await self.client.get_open_interest(sig.symbol)
                prev = self._oi_hist.get(sig.symbol)
                cur  = float(oi.get("openInterest", 0)) if oi else 0.0
                if prev and prev > 0 and cur > 0:
                    oi_delta = (cur - prev) / prev
                if cur > 0:
                    self._oi_hist[sig.symbol] = cur
            except Exception as _e:
                log.debug(f"nexus: open interest indisponível para {sig.symbol}: {_e}")

            news_score = None
            try:
                ns = mdata.get_market_sentiment()
                if isinstance(ns, dict) and ns.get("score") is not None:
                    news_score = float(ns["score"])
            except Exception as _e:
                log.debug(f"nexus: news sentiment indisponível: {_e}")

            decision = await asyncio.to_thread(
                nexus_ai.decide, symbol=sig.symbol,
                k15=k15, k1h=k1h, k4h=k4h,
                entry=sig.entry, sl=sig.sl, tp=sig.tp,
                ticker=ticker, funding=funding, oi=oi, oi_delta=oi_delta,
                news_score=news_score,
            )

            # nexus_ai.decide executes in a worker thread. Telegram scheduling
            # must happen back on the engine event loop, never inside that
            # worker thread.
            raw_decision = getattr(decision, "decision", "UNKNOWN")
            decision_value = getattr(raw_decision, "value", raw_decision)
            if str(decision_value).upper() == "WAIT":
                snapshot = getattr(decision, "_bgx_score_snapshot", {}) or {}
                reasoning = getattr(decision, "reasoning", None) or []
                payload = {
                    "symbol": sig.symbol,
                    "decision": "WAIT",
                    "final_score": float(getattr(decision, "setup_quality", 0.0) or 0.0),
                    "estimated_final": snapshot.get("estimated_final"),
                    "component_score": snapshot.get("component_score"),
                    "risk_penalty": snapshot.get("risk_penalty"),
                    "confidence": snapshot.get(
                        "fusion_confidence",
                        getattr(decision, "confidence", None),
                    ),
                    "rr_net": snapshot.get(
                        "rr_net",
                        getattr(decision, "risk_reward", None),
                    ),
                    "ev_pct": snapshot.get(
                        "ev_pct",
                        getattr(decision, "expected_value", None),
                    ),
                    "data_quality": snapshot.get(
                        "data_quality",
                        getattr(decision, "data_quality", None),
                    ),
                    "regime": snapshot.get(
                        "regime",
                        getattr(decision, "market_regime", "N/A"),
                    ),
                    "mtf": snapshot.get("mtf", "N/A"),
                    "reason": reasoning[-1] if reasoning else "aguardando confirmação",
                }
                asyncio.create_task(notify_nexus_score(payload))
            return decision
        except Exception as e:
            log.error(f"_nexus_validate {sig.symbol}: {type(e).__name__}")
            raise

    def _entry_available_funds(self) -> float:
        """Return the capital authority for immediate entry affordability.

        Account equity remains in risk.balance. Controlled LIVE keeps KuCoin
        free collateral separately in _pilot_available_balance so affordability
        never changes the semantic meaning of the equity field.
        """
        if self.pilot.enabled:
            return float(getattr(self, "_pilot_available_balance", 0.0) or 0.0)
        return float(getattr(self.risk, "balance", 0.0) or 0.0)

    async def _refresh_entry_balance(self) -> bool:
        """Zero/negative is a valid account result; query failure is separate."""
        try:
            balance = await self.client.get_balance()
            self.risk.update(balance)
        except Exception as exc:
            self.risk.balance_confirmed = False
            log.warning(f"[BALANCE] entry blocked source=query_failure error={type(exc).__name__}")
            return False
        if balance <= 0:
            log.warning("[BALANCE] entry blocked source=account availableBalance<=0")
            return False
        return True

    async def _open(self, sig: Signal):
        try:
            # ══════════════════════════════════════════════════════
            # DEFESA EM PROFUNDIDADE — viable_symbols (auditoria
            # adversarial: confirmado por execução real que _open()
            # não tinha proteção PRÓPRIA contra símbolos fora da lista
            # viável, dependendo inteiramente de nunca ser chamado
            # fora de _scan_all_and_enter(), que é hoje o único call
            # site. Isso é um single point of failure estrutural, não
            # um bug ativo — hoje não há caminho de execução real que
            # o explore. Esta guarda fecha a lacuna para o futuro, sem
            # alterar nenhum comportamento do fluxo normal.
            # ══════════════════════════════════════════════════════
            if sig.symbol not in self.viable_symbols:
                log.error(
                    f"🚫 _open BLOQUEADO: {sig.symbol} não está em "
                    f"viable_symbols ({len(self.viable_symbols)} pares "
                    f"viáveis) — abortando por segurança"
                )
                return

            if (self._durable_state_enforced
                    and not durable.can_open(self)):
                log.critical(
                    "[DURABLE_STATE] nova entrada bloqueada: estado persistente "
                    "não confirmado; posições existentes continuam gerenciadas "
                    "errors=%s pending=%s paused=%s daily_pnl_ok=%s",
                    sorted(getattr(self, "_durable_state_errors", set())),
                    [(o.client_oid, o.symbol, o.state.value) for o in self.orders.pending_orders()],
                    bool(getattr(self, "entries_paused", False)),
                    getattr(self, "_daily_pnl_ok", True),
                )
                return

            # Mandatory for every new entry, including paper/pilot. Closing
            # reduceOnly routes do not pass through this gate.
            nx_dec = None
            decision_source = "validation_failure"
            reason = "ai_disabled"
            if _NEXUS_ENABLED:
                try:
                    nx_dec = await asyncio.wait_for(
                        self._nexus_validate(sig), timeout=_NEXUS_TIMEOUT_S
                    )
                    reason = decision_validation_error(
                        nx_dec, sig.symbol, sig.direction, sig.entry, sig.sl, sig.tp
                    )
                    decision_source = "validation_failure" if reason else "nexus_ai"
                except asyncio.TimeoutError:
                    decision_source, reason = "timeout", "ai_timeout"
                except Exception as exc:
                    decision_source, reason = "exception", type(exc).__name__

            approved = (reason is None and nx_dec.execution_allowed is True)
            # Log both outcomes before any order. Failure records never claim
            # to be a decision made by nexus_ai; arbitrary exception text is omitted.
            score = nx_dec.setup_quality if decision_source == "nexus_ai" else "N/A"
            confidence = nx_dec.confidence if decision_source == "nexus_ai" else "N/A"
            log.info(
                f"[AI_DECISION] symbol={sig.symbol} side={sig.direction} "
                f"decision={'APPROVE' if approved else 'REJECT'} approved={approved} "
                f"decision_source={decision_source} score={score} confidence={confidence} "
                f"ts={int(time.time())} reason={reason or ('approved' if approved else 'ai_veto')}"
            )
            if not approved:
                return

            self._last_nexus[sig.symbol] = nx_dec.to_dict()
            asyncio.create_task(notify_nexus(nx_dec.to_dict(), approved=True))
            if not await self._refresh_entry_balance():
                return
            fresh_bal = self._entry_available_funds()
            # Piloto não permite fallback silencioso: same mandatory AI gate.
            if not self.pilot.can_open_pilot(self, self.client, sig.symbol, nx_dec):
                return

            # ADV-margin: repassa self.positions (fonte real de posições
            # confirmadas — inclui as reconciliadas pelo ADV-01) para
            # que o sizing desconte a margem já comprometida.
            if self.pilot.enabled:
                qty = minimum_base_quantity(self.instruments[sig.symbol], sig.entry)
                # A minimum lot is not permission to exceed available margin.
                # Include opening taker fee; use the current read, never cached balance.
                required = qty * sig.entry * (1.0 / cfg.LEVERAGE + TAKER_FEE)
                if fresh_bal <= 0 or required > fresh_bal:
                    log.warning(f"[PILOT] {sig.symbol} minimum lot exceeds available balance")
                    return
            else:
                qty = self.risk.size(
                    sig.symbol, sig.entry, self.instruments,
                    open_positions=self.positions,
                )
            if qty <= 0:
                log.warning(f"⚠️ {sig.symbol}: qty=0 — saldo insuficiente (${self.risk.balance:.2f})")
                return

            # ── Score pré-trade ───────────────────────────────
            # Buscar klines para pré-trade — REST se cache insuficiente
            kl = self.client.get_cached_klines(sig.symbol, "15", 50)
            if len(kl) < 20:
                try:
                    kl = await self.client.get_klines(sig.symbol, "15", 50)
                except Exception as _e:
                    log.warning(
                        f"score pré-trade {sig.symbol}: klines indisponíveis "
                        f"via REST ({_e}) — usando fallback"
                    )
                    kl = []

            # The final cache candle is still forming. Pretrade needs at least
            # 20 closed candles after pretrade_hardening removes that final
            # candle. Missing market data is not replaced with synthetic flat
            # candles: execution must fail closed.
            if len(kl) <= 20:
                log.warning(
                    f"[PRETRADE] {sig.symbol} bloqueado: dados insuficientes "
                    f"para 20 candles fechados (recebidos={len(kl)})"
                )
                return

            c = [float(k.get("c", sig.entry) if isinstance(k, dict) else (k[4] if len(k) > 4 else sig.entry)) for k in kl]
            h = [float(k.get("h", sig.entry) if isinstance(k, dict) else (k[2] if len(k) > 2 else sig.entry)) for k in kl]
            l = [float(k.get("l", sig.entry) if isinstance(k, dict) else (k[3] if len(k) > 3 else sig.entry)) for k in kl]
            v = [float(k.get("v", 1000.0) if isinstance(k, dict) else (k[5] if len(k) > 5 else 1000.0)) for k in kl]

            pre_score = await scoring.calculate(
                sig.symbol, sig.direction, c, h, l, v, self.client
            )
            if not pre_score["aprovado"]:
                # Log detalhado mostrando o que bloqueou
                tec = pre_score.get("tecnico", 0)
                of  = pre_score.get("orderflow", 0)
                mac = pre_score.get("macro", 0)
                news= pre_score.get("news_mod", 0)
                log.info(
                    f"[{sig.symbol}] Pré-trade REPROVADO {pre_score['total']}/100 "
                    f"(TEC={tec} OF={of} MAC={mac} NEWS={news:+d}) "
                    f"mínimo={scoring.MIN_SCORE}"
                )
                return

            # Salva snapshot de mercado
            await db.save_snapshot(
                sig.symbol,
                pre_score.get("oi", 0),
                pre_score.get("funding", 0),
                pre_score.get("cvd", 0),
            )

            side = "Buy" if sig.direction == "LONG" else "Sell"

            # ── Validação de parâmetros antes de enviar à API ─────
            info      = self.instruments.get(sig.symbol, {})
            qty_step  = float(info.get("qtyStep",  0.001))
            tick_size = float(info.get("tickSize", 0.01))
            min_not   = float(info.get("minNotional", 0.0))  # USDT
            # Exchange limits are normalized explicitly; qty stays in base asset.
            validate_base_quantity(qty, info, sig.entry)

            # Validar SL/TP — devem estar no lado correto da entrada
            if sig.sl <= 0 or sig.tp <= 0:
                log.error(
                    f"❌ _open {sig.symbol}: SL={sig.sl} ou TP={sig.tp} inválido (≤ 0) — abortando"
                )
                return
            if sig.direction == "LONG":
                if sig.sl >= sig.entry:
                    log.error(
                        f"❌ _open {sig.symbol} LONG: SL={sig.sl:.6f} >= entry={sig.entry:.6f} — abortando"
                    )
                    return
                if sig.tp <= sig.entry:
                    log.error(
                        f"❌ _open {sig.symbol} LONG: TP={sig.tp:.6f} <= entry={sig.entry:.6f} — abortando"
                    )
                    return
            else:  # SHORT
                if sig.sl <= sig.entry:
                    log.error(
                        f"❌ _open {sig.symbol} SHORT: SL={sig.sl:.6f} <= entry={sig.entry:.6f} — abortando"
                    )
                    return
                if sig.tp >= sig.entry:
                    log.error(
                        f"❌ _open {sig.symbol} SHORT: TP={sig.tp:.6f} >= entry={sig.entry:.6f} — abortando"
                    )
                    return

            log.info(
                f"🔎 _open {sig.symbol} {sig.direction} | "
                f"entry={sig.entry} sl={sig.sl} tp={sig.tp} | "
                f"qty={qty} qty_step={qty_step} tick={tick_size} | "
                f"notional={qty * sig.entry:.2f} min_not={min_not}"
            )

            # ── Retry com backoff exponencial (3 tentativas) ─────
            MAX_RETRIES   = 1 if self.pilot.enabled else 3
            RETRY_DELAYS  = [1.0, 2.0, 4.0]   # segundos entre tentativas
            last_exc: Exception | None = None

            # ══════════════════════════════════════════════════════════
            # P0 — SL ALÉM DA LIQUIDAÇÃO
            #
            # Com LEVERAGE=50, a liquidação ocorre a ~2% de movimento
            # adverso. Se o ATR estiver alto, o SL calculado pode cair
            # ALÉM desse ponto — a posição seria liquidada ANTES do stop
            # disparar, transformando uma perda planejada de 1R em perda
            # TOTAL da margem.
            #
            # Não havia validação disso na abertura (só existia ao adotar
            # posições órfãs). Aqui recusamos o trade em vez de aceitar
            # um stop que nunca será executado.
            # ══════════════════════════════════════════════════════════
            # ══════════════════════════════════════════════════════
            # P0 — MATEMÁTICA REAL DE LIQUIDAÇÃO (Fase 3)
            #
            # A aproximação 100/leverage era OTIMISTA em ~33%: ignorava
            # margem de manutenção (0.5%), taxas (0.12%) e slippage.
            #
            # Com 50x:  aproximação 2.00%  |  real 1.33%
            #
            # O stop precisa ficar numa região que NÃO dependa da
            # liquidação para encerrar a posição.
            # ══════════════════════════════════════════════════════
            # Fase 6: conta real opera em CROSS MARGIN (confirmado por
            # print de tela do usuário). Com 2+ posições simultâneas, a
            # margem de manutenção depende da conta inteira — informa
            # isso ao módulo para que ele se declare não-confiável
            # nesse cenário, em vez de dar um número falsamente preciso.
            if is_binance():
                from bot import binance_cross_portfolio_stress as binance_cross_stress

                _binance_stress = await binance_cross_stress.evaluate(
                    self, sig, qty
                )
                _sl_pct = (
                    abs(sig.entry - sig.sl) / sig.entry * 100
                    if sig.entry > 0 and sig.sl > 0
                    else 0.0
                )
                _liq_pct = 0.0
                _sl_inseguro = False

                if not _binance_stress.allowed:
                    log.warning(
                        "[BINANCE_CROSS_STRESS] symbol=%s result=BLOCK reason=%s "
                        "mode=%s risk_rate=%.4f limit=%.4f stressed_margin=%.8f "
                        "maintenance=%.8f closing_fees=%.8f opening_fee=%.8f "
                        "existing_positions=%d stage=PRE_ORDER "
                        "execution_effect=BLOCK_NEW_ENTRY",
                        sig.symbol,
                        _binance_stress.reason,
                        _binance_stress.mode,
                        _binance_stress.risk_rate,
                        binance_cross_stress.MAX_STOP_STRESS_RISK_RATE,
                        _binance_stress.stressed_margin,
                        _binance_stress.maintenance,
                        _binance_stress.closing_fees,
                        _binance_stress.opening_fee,
                        _binance_stress.existing_positions,
                    )
                    try:
                        await db.save_signal(
                            sig.symbol,
                            sig.direction,
                            {"total": int(sig.score)},
                            entrou=False,
                            motivo=(
                                "binance cross stress: "
                                f"{_binance_stress.reason}"
                            ),
                        )
                    except Exception as _e:
                        log.debug(f"save_signal binance_cross_stress: {_e}")
                    return

                log.info(
                    "[BINANCE_CROSS_STRESS] symbol=%s result=PASS reason=%s "
                    "mode=%s risk_rate=%.4f limit=%.4f existing_positions=%d "
                    "stage=PRE_ORDER kucoin_liquidation_formula_used=false "
                    "execution_effect=NONE",
                    sig.symbol,
                    _binance_stress.reason,
                    _binance_stress.mode,
                    _binance_stress.risk_rate,
                    binance_cross_stress.MAX_STOP_STRESS_RISK_RATE,
                    _binance_stress.existing_positions,
                )
            else:
                _liq = liq.analyze(
                    entry=sig.entry, stop=sig.sl, leverage=cfg.LEVERAGE,
                    is_long=(sig.direction == "LONG"), symbol=sig.symbol,
                    n_open_positions=len(self.positions) + 1,
                )
                # KuCoin-only legacy tier audit. Binance uses user-specific
                # leverage brackets in binance_cross_portfolio_stress.
                _notional_est = qty * sig.entry
                if _notional_est and liq.notional_exceeds_tier1(_notional_est):
                    log.warning(
                        f"⚠️ [{sig.symbol}] notional ${_notional_est:,.0f} pode "
                        f"exceder o Tier 1 assumido (MMR={_liq.mmr:.3%}) — "
                        f"MMR real pode ser maior, liquidação mais próxima "
                        f"do que calculado"
                    )
                _liq_pct = _liq.liq_move_pct
                _sl_pct = _liq.stop_move_pct

            # ══════════════════════════════════════════════════════════
            # OVERRIDE EXPLÍCITO — ALLOW_SL_BEYOND_LIQUIDATION
            #
            # Quando o SL fica além do preço de liquidação, a posição é
            # liquidada ANTES do stop disparar. A perda deixa de ser 1R
            # e passa a ser 100% da margem.
            #
            # Exemplo real (SOLUSDT, 50x):
            #   liquidação a 2.00% | SL a 2.27%
            #   → o preço atinge a liquidação primeiro; o SL nunca executa
            #
            # Ativado por escolha do usuário, ciente de que o stop loss
            # se torna decorativo nesses trades.
            # ══════════════════════════════════════════════════════════
            _allow_beyond = os.environ.get(
                "ALLOW_SL_BEYOND_LIQUIDATION", "false"
            ).lower() == "true"

            # Binance uses account-level CROSS stop stress above. KuCoin keeps
            # the legacy liquidation-price effectiveness check.
            if not is_binance():
                _sl_inseguro = not _liq.stop_effective

            if _sl_inseguro and _allow_beyond:
                # Não bloqueia, mas registra e avisa — o operador precisa
                # saber quais trades entraram sem proteção efetiva.
                _sera_liquidado = _sl_pct >= _liq_pct
                log.warning(
                    f"⚠️ [{sig.symbol}] SL a {_sl_pct:.2f}% vs liquidação "
                    f"{_liq_pct:.2f}% — "
                    f"{'LIQUIDA ANTES DO STOP' if _sera_liquidado else 'margem apertada'}. "
                    f"Prosseguindo por ALLOW_SL_BEYOND_LIQUIDATION=true"
                )
                if _sera_liquidado:
                    _k = f"beyond_{sig.symbol}"
                    _now = time.time()
                    if _now - self._liq_alert.get(_k, 0) > 1800:
                        self._liq_alert[_k] = _now
                        asyncio.create_task(notify(
                            f"⚠️ *ORDEM SEM PROTEÇÃO EFETIVA*\n"
                            f"`{'━'*26}`\n"
                            f"📍 Par:        `{sig.symbol}`\n"
                            f"🛑 SL a:       `{_sl_pct:.2f}%`\n"
                            f"💀 Liquidação: `{_liq_pct:.2f}%` ({cfg.LEVERAGE}x)\n"
                            f"`{'━'*26}`\n"
                            f"_A liquidação ocorre ANTES do stop. Se este "
                            f"trade perder, a perda é de 100% da margem, "
                            f"não 1R._"
                        ))

            elif _sl_inseguro:
                _lev_ok = liq.max_leverage_for_stop(_sl_pct)
                log.warning(
                    f"⛔ [{sig.symbol}] REJEITADO: {_liq.reason} | "
                    f"leverage máximo seguro para este SL: {_lev_ok}x"
                )
                try:
                    await db.save_signal(
                        sig.symbol, sig.direction, {"total": int(sig.score)},
                        entrou=False,
                        motivo=f"stop inefetivo: SL {_sl_pct:.2f}% vs liq {_liq_pct:.2f}%",
                    )
                except Exception as _e:
                    log.debug(f"save_signal sl_liq: {_e}")

                _k = f"liq_{sig.symbol}"
                _now = time.time()
                if _now - self._liq_alert.get(_k, 0) > 1800:   # 30 min
                    self._liq_alert[_k] = _now
                    asyncio.create_task(notify(
                        f"⛔ *ORDEM NÃO ABERTA — STOP INEFETIVO*\n"
                        f"`{'━'*26}`\n"
                        f"📍 Par:        `{sig.symbol}`\n"
                        f"🛑 SL a:       `{_sl_pct:.2f}%` do entry\n"
                        f"💀 Liquidação: `{_liq_pct:.2f}%` ({cfg.LEVERAGE}x)\n"
                        f"📏 Folga:      `{_liq.gap_pct:+.2f}%`\n"
                        f"✅ SL máximo:  `{_liq.max_safe_stop_pct:.2f}%`\n"
                        f"`{'━'*26}`\n"
                        f"_Liquidação calculada com margem de manutenção, "
                        f"taxas e slippage._\n"
                        f"_Leverage máximo seguro para este SL: `{_lev_ok}x`_"
                    ))
                return

            if self.paper_trade:
                try:
                    qty_before = qty
                    qty = cap_paper_quantity(
                        qty, self.risk.balance, sig.entry, sig.sl,
                        sig.direction, self.instruments[sig.symbol], TAKER_FEE,
                    )
                    log.info(
                        "[PAPER_LOSS_BUDGET] %s qty=%s -> %s; projected stop loss "
                        "including fees/slippage <=70%%; drawdown gate unchanged",
                        sig.symbol, qty_before, qty,
                    )
                except (ValueError, ArithmeticError, TypeError, KeyError) as exc:
                    log.warning("[PAPER_LOSS_BUDGET] %s blocked: %s", sig.symbol, exc)
                    return

            # P0: chave de idempotência FIXA para todas as tentativas deste
            # sinal. Garante que retries reusem o mesmo clientOid e a
            # exchange rejeite duplicatas.
            _idem = f"{sig.symbol}_{side}_{qty}_{int(time.time()//60)}"
            _client_oid = self.client.build_client_oid(
                sig.symbol, side, qty, _idem
            )
            # NOVO-F013A-1f — a key that repeats for the SAME unresolved intent
            # (duplicate submission guard) must never hand a NEW financial
            # intent the ManagedOrder of a finished trade (same symbol/side/qty
            # inside the same minute after the previous trade closed). A
            # terminal previous order with no live position => new generation.
            _base_idem, _generation = _idem, 0
            _prev = self.orders.get(_client_oid) if hasattr(self.orders, "get") else None
            while (_prev is not None and _prev.state in TERMINAIS
                   and sig.symbol not in self.positions and _generation < 16):
                _generation += 1
                _idem = f"{_base_idem}|g{_generation}"
                _client_oid = self.client.build_client_oid(sig.symbol, side, qty, _idem)
                _prev = self.orders.get(_client_oid)
            if _generation:
                log.warning(
                    "[ORDER_IDENTITY] symbol=%s previous_intent=TERMINAL generation=%s "
                    "clientOid=%s reason=new_financial_intent_same_minute",
                    sig.symbol, _generation, _client_oid,
                )

            for attempt in range(1, MAX_RETRIES + 1):
                _managed = None
                try:
                    # ══════════════════════════════════════════════════
                    # P0 — NUNCA RETENTAR ORDEM SEM VERIFICAR EXECUÇÃO
                    #
                    # Um timeout de rede não significa que a ordem falhou.
                    # A partir da 2ª tentativa, confirma na exchange se a
                    # posição já existe antes de reenviar.
                    # ══════════════════════════════════════════════════
                    if attempt > 1:
                        if await self.client._position_exists(sig.symbol):
                            log.warning(
                                f"⚠️ {sig.symbol}: posição JÁ EXISTE na exchange "
                                f"(tentativa {attempt}) — a ordem anterior "
                                f"executou apesar do erro. Abortando retry para "
                                f"não duplicar exposição."
                            )
                            last_exc = None
                            break

                    log.info(
                        f"📡 _open {sig.symbol} tentativa {attempt}/{MAX_RETRIES} | "
                        f"side={side} qty={qty} entry={sig.entry:.6f} "
                        f"sl={sig.sl:.6f} tp={sig.tp:.6f} "
                        f"qty_step={qty_step} tick={tick_size} "
                        f"notional={qty * sig.entry:.4f} balance={self.risk.balance:.4f}"
                    )

                    # ══════════════════════════════════════════════════
                    # CONECTA O OrderRegistry EXISTENTE AO FLUXO REAL
                    #
                    # Auditoria anterior confirmou por grep: self.orders
                    # (OrderRegistry) era instanciado no __init__ mas
                    # NUNCA chamado dentro de _open() — máquina de
                    # estados existia sem uso. Corrigido aqui.
                    # ══════════════════════════════════════════════════
                    # Recheck after analysis/network waits, immediately before
                    # reservation and dispatch. Never size/send against stale funds.
                    if is_binance() and not self.paper_trade:
                        from bot.binance_cross_portfolio_stress import final_dispatch_context_valid

                        if not final_dispatch_context_valid(self, sig, qty):
                            log.critical(
                                "[BINANCE_CROSS_STRESS] symbol=%s result=BLOCK "
                                "reason=final_dispatch_context_mismatch stage=FINAL_PREDISPATCH",
                                sig.symbol,
                            )
                            return
                    if not await self._refresh_entry_balance():
                        return
                    if self.paper_trade:
                        # Do not change an idempotent order's quantity on retry.
                        try:
                            allowed_qty = cap_paper_quantity(
                                qty, self.risk.balance, sig.entry, sig.sl,
                                sig.direction, self.instruments[sig.symbol], TAKER_FEE,
                            )
                            if allowed_qty < qty:
                                log.warning("[PAPER_LOSS_BUDGET] balance changed; entry blocked")
                                return
                        except (ValueError, ArithmeticError, TypeError, KeyError) as exc:
                            log.warning("[PAPER_LOSS_BUDGET] entry blocked: %s", exc)
                            return
                    required = qty * sig.entry * (1.0 / cfg.LEVERAGE + TAKER_FEE)
                    current_funds = self._entry_available_funds()
                    if required > current_funds:
                        log.warning(f"[BALANCE] {sig.symbol} insufficient current funds")
                        return
                    _managed, _ = self.orders.get_or_create(
                        _client_oid, sig.symbol, side, qty
                    )
                    try:
                        _managed.transition(OrderState.SUBMITTING, source="REST")
                    except InvalidTransition as _ie:
                        log.debug(f"OrderRegistry {sig.symbol}: {_ie}")
                    if _managed.protection_plan is None:
                        # Durable protective intent travels with the clientOid
                        # so an ambiguous submit can still be protected once
                        # the BGX fill is proven (ambiguous_entry_recovery).
                        from bot.order_state import normalize_protection_plan
                        _managed.protection_plan = normalize_protection_plan({
                            "direction": sig.direction, "entry": sig.entry,
                            "sl": sig.sl, "tp": sig.tp,
                        })

                    # The exact exchange clientOid and SUBMITTING intent must
                    # be durable before place_order can perform network I/O.
                    if (self._durable_state_enforced
                            and not await durable.persist_orders(
                                self, "before_dispatch", strict=True
                            )):
                        return

                    if not self.pilot.reserve_submission(sig.symbol):
                        return
                    _order = await self.client.place_order(
                        symbol=sig.symbol, side=side, qty=qty,
                        single_submission=self.pilot.enabled,
                        sl=sig.sl, tp=sig.tp,
                        instruments=self.instruments,
                        idem_key=_idem,   # P0: mesmo OID em todas as tentativas
                    )

                    # EXEC-02: place_order() retorna dict com clientOid mesmo
                    # quando TODAS as tentativas falham (para correlação de
                    # log). Um dict não-vazio SEM orderId significa FALHA,
                    # não sucesso — trata explicitamente antes de seguir.
                    if not _order or not _order.get("orderId"):
                        log.error(
                            f"❌ {sig.symbol}: place_order não retornou "
                            f"orderId (clientOid="
                            f"{(_order or {}).get('clientOid','?')}) — "
                            f"ordem NÃO confirmada pela exchange"
                        )
                        last_exc = RuntimeError(
                            f"place_order sem orderId para {sig.symbol}"
                        )
                        if self._durable_state_enforced:
                            await durable.persist_orders(
                                self, "ambiguous_dispatch", strict=False
                            )
                            durable._block(self, "orders")
                        break

                    _oid_for_registry = _order.get("orderId", "") if _order else ""
                    if _oid_for_registry:
                        self.orders.index_order_id(_oid_for_registry, _client_oid)
                        try:
                            _managed.transition(
                                OrderState.SUBMITTED,
                                order_id=_oid_for_registry, source="REST",
                            )
                        except InvalidTransition as _ie:
                            log.debug(f"OrderRegistry {sig.symbol}: {_ie}")
                        if self._durable_state_enforced:
                            await durable.persist_orders(
                                self, "submitted", strict=False
                            )

                    # ── PROTEÇÃO CRÍTICA: posição sem SL não pode existir ──
                    # Com 50x, liquidação ocorre a ~2% de movimento adverso.
                    # Se o trading-stop falhou, a posição está desprotegida:
                    # fecha imediatamente em vez de deixá-la exposta.
                    if _order and _order.get("sl_tp_failed"):
                        log.error(
                            f"🚨 {sig.symbol}: SL/TP não anexados — "
                            f"FECHANDO posição imediatamente por segurança"
                        )
                        try:
                            _retry_ok = await self.client.set_position_stops(
                                sig.symbol, sl=sig.sl, tp=sig.tp
                            )
                            if not _retry_ok:
                                _close_res = await self.client.place_order(
                                    symbol=sig.symbol,
                                    side="Sell" if side == "Buy" else "Buy",
                                    qty=qty, sl=0, tp=0,
                                    instruments=self.instruments,
                                    reduce_only=True,
                                )
                                _close_oid = (
                                    _close_res.get("orderId")
                                    if isinstance(_close_res, dict) else ""
                                )
                                _close_fill = (
                                    await self.client.wait_for_fill(
                                        _close_oid, timeout_s=8.0
                                    )
                                    if _close_oid else {"filled": False}
                                )
                                _flat = False
                                if _close_oid:
                                    try:
                                        _after_close = await self.client.get_positions()
                                        _flat = not any(
                                            ep.get("symbol") == sig.symbol
                                            and abs(float(ep.get("size", 0) or 0)) > 0
                                            for ep in _after_close
                                        )
                                    except Exception as _verify_exc:
                                        log.critical(
                                            "[POST_OPEN_EMERGENCY_CLOSE_UNCONFIRMED] "
                                            "symbol=%s orderId=%s stage=POSITION_READ "
                                            "error=%s entries_blocked=true",
                                            sig.symbol, _close_oid,
                                            type(_verify_exc).__name__,
                                        )

                                if _close_fill.get("filled") and _flat:
                                    log.critical(
                                        "[POST_OPEN_EMERGENCY_CLOSE_CONFIRMED] "
                                        "symbol=%s orderId=%s fill_confirmed=true "
                                        "exchange_flat=true",
                                        sig.symbol, _close_oid,
                                    )
                                    await notify(
                                        f"🚨 *POSIÇÃO FECHADA POR SEGURANÇA*\n"
                                        f"`{sig.symbol}` foi aberta sem proteção; "
                                        f"o fechamento foi confirmado na KuCoin."
                                    )
                                    return

                                self._unprotected_symbols.add(sig.symbol)
                                log.critical(
                                    "[POST_OPEN_EMERGENCY_CLOSE_UNCONFIRMED] "
                                    "symbol=%s orderId=%s fill_confirmed=%s "
                                    "exchange_flat=%s entries_blocked=true",
                                    sig.symbol, _close_oid or "NONE",
                                    bool(_close_fill.get("filled", False)), _flat,
                                )
                                await notify(
                                    f"🆘 *AÇÃO MANUAL NECESSÁRIA*\n"
                                    f"`{sig.symbol}` foi aberta sem proteção e o "
                                    f"fechamento não foi confirmado como FILLED + FLAT.\n"
                                    f"Novas entradas foram bloqueadas."
                                )
                                return
                            log.info(f"✓ {sig.symbol}: SL/TP anexados na 2ª tentativa")
                        except Exception as _e:
                            log.error(f"🚨 {sig.symbol}: falha ao proteger/fechar: {_e}")
                            return

                    # ══════════════════════════════════════════════════
                    # P0 — CONFIRMAÇÃO DE FILLED (não apenas HTTP 200)
                    #
                    # Antes, "orderId recebido" era tratado como sucesso
                    # definitivo. Agora consulta o status real da ordem
                    # antes de aceitar o resultado como FILLED.
                    # ══════════════════════════════════════════════════
                    _oid_real = _order.get("orderId", "") if _order else ""
                    _fill_check = await self.client.wait_for_fill(_oid_real)

                    if _fill_check["filled"]:
                        _st = _fill_check.get("status", {}) or {}
                        _fsz = float(_st.get("filledSize", 0) or 0)
                        _dsz = float(_st.get("dealSize", 0) or 0)
                        _dval = float(_st.get("dealValue", 0) or 0)
                        _avg_px = (_dval / _dsz) if _dsz > 0 else 0.0
                        try:
                            _managed.transition(
                                OrderState.FILLED, filled_qty=_fsz,
                                avg_price=_avg_px, source="REST",
                            )
                        except InvalidTransition as _ie:
                            log.debug(f"OrderRegistry {sig.symbol}: {_ie}")
                        if self._durable_state_enforced:
                            await durable.persist_orders(
                                self, "filled", strict=False
                            )

                        # GAP DE OBSERVABILIDADE CORRIGIDO — não existia
                        # log de sucesso do FILLED via REST. A Fase 8 do
                        # protocolo de prova E2E exige demonstrar
                        # explicitamente "FILLED source = REST" com os
                        # valores de origem (filledSize/dealSize/
                        # dealValue) para correlação com o WS depois.
                        #
                        # BUG CORRIGIDO NESTA MESMA SESSÃO: usava _idem
                        # (chave interna pré-hash) em vez do clientOid
                        # REAL enviado à exchange (_order["clientOid"],
                        # com o prefixo bgx7-). Capturado em teste E2E:
                        # os logs [ORDER] e [FILLED] mostravam valores
                        # DIFERENTES para a mesma ordem, quebrando a
                        # correlação exigida pelo protocolo.
                        _oid_correlacao = (
                            _order.get("clientOid", _idem) if _order else _idem
                        )
                        log.info(
                            f"✅ [FILLED] source=REST "
                            f"clientOid={_oid_correlacao} "
                            f"orderId={_oid_real} symbol={sig.symbol} "
                            f"filledSize={_fsz} dealSize={_dsz} "
                            f"dealValue={_dval} avgPrice={_avg_px:.8f}"
                        )

                    if not _fill_check["filled"]:
                        log.error(
                            f"🚨 {sig.symbol}: orderId={_oid_real} aceito pela "
                            f"API mas NÃO confirmado como FILLED "
                            f"(status={_fill_check['status']}, "
                            f"timeout={_fill_check['timed_out']})"
                        )
                        # ══════════════════════════════════════════════
                        # P0 (ADV-01) — RECONCILIAÇÃO IMEDIATA
                        #
                        # ANTES: "não sabemos se há posição real — não
                        # assume nada" ficava só no comentário. Nada de
                        # fato consultava a exchange aqui, e
                        # _load_existing_positions() só roda 1x no
                        # boot — a posição órfã (ex: 90% preenchida)
                        # ficava sem SL indefinidamente, com
                        # IntegrityGuard apenas bloqueando entradas
                        # NOVAS, sem nunca corrigir a existente.
                        #
                        # AGORA: consulta a exchange NA HORA para este
                        # símbolo específico. Se a posição existir de
                        # fato (fill parcial real), ela é descoberta,
                        # registrada com o entry price REAL da exchange
                        # (nunca ticker) e recebe SL/TP imediatamente.
                        # ══════════════════════════════════════════════
                        _adoptions = dict(getattr(self, "_timeout_adoptions", None) or {})
                        _adoptions[sig.symbol] = {
                            "order_id": _oid_real,
                            "client_oid": (_order or {}).get("clientOid", "") or "",
                            "direction": sig.direction,
                            "planned_sl": sig.sl, "planned_tp": sig.tp,
                        }
                        self._timeout_adoptions = _adoptions
                        try:
                            _ainda_desprotegidos = await self._reconcile_exchange_positions(
                                only_symbol=sig.symbol
                            )
                            _adopted = self.positions.get(sig.symbol)
                            if _adopted is not None and not self.paper_trade:
                                # F-013 at the adopted position's exchange average:
                                # same budget authority and stop repair as no-timeout.
                                from bot import postfill_risk_recheck as _pfg
                                _geo = await _pfg.reconcile(
                                    self, sig, _adopted.qty, {}, log, position_fill=_adopted.entry)
                                _adopted.sl = _adopted.trailing_sl = _geo.sl
                            if sig.symbol in self.positions:
                                try:
                                    _managed.transition(
                                        OrderState.FILLED,
                                        order_id=_oid_real,
                                        source="POSITION_RECONCILE",
                                    )
                                except InvalidTransition as _ie:
                                    log.debug(f"OrderRegistry {sig.symbol}: {_ie}")
                            if sig.symbol in _ainda_desprotegidos:
                                log.critical(
                                    f"🚨🚨 {sig.symbol}: posição órfã "
                                    f"reconciliada mas AINDA SEM PROTEÇÃO "
                                    f"confirmada — novas entradas bloqueadas "
                                    f"até resolução"
                                )
                        except Exception as _re:
                            log.error(
                                f"_reconcile_exchange_positions falhou para "
                                f"{sig.symbol}: {_re}"
                            )
                        finally:
                            (getattr(self, "_timeout_adoptions", None) or {}).pop(sig.symbol, None)
                        if self._durable_state_enforced:
                            await durable.persist_orders(
                                self, "fill_timeout_reconcile", strict=False
                            )
                            if sig.symbol not in self.positions:
                                durable._block(self, "orders")
                        last_exc = RuntimeError(
                            f"ordem {_oid_real} não confirmada como FILLED"
                        )
                        break

                    last_exc = None
                    break   # sucesso — sai do loop de retry
                except Exception as exc:
                    last_exc = exc
                    err_str  = str(exc)

                    # BinanceClient owns transport retries and ambiguous-order
                    # recovery by newClientOrderId. A second engine-level
                    # submission would create a second logical dispatch boundary,
                    # so Binance fails closed here and waits for the next signal.
                    if is_binance():
                        if self._durable_state_enforced:
                            await durable.record_binance_margin_rejection(self, _managed, exc)
                        import re as _re
                        _code_match = _re.search(
                            r"code=(-?\d+)", err_str
                        )
                        _msg_match = _re.search(
                            r"msg=(.*)$", err_str
                        )
                        ret_code = (
                            _code_match.group(1)
                            if _code_match else "?"
                        )
                        ret_msg = (
                            _msg_match.group(1).strip()
                            if _msg_match else err_str
                        )
                        log.error(
                            f"❌ _open {sig.symbol} tentativa "
                            f"{attempt}/{MAX_RETRIES} FALHOU | "
                            f"exchange=binance code={ret_code} "
                            f"msg='{ret_msg}' | "
                            f"outer_retry=false "
                            f"client_transport_recovery=authoritative"
                        )
                        log.error(
                            f"🚫 _open {sig.symbol}: Binance não fará "
                            f"nova submissão lógica após falha do "
                            f"dispatcher; aguardando reconciliação/novo sinal"
                        )
                        break

                    # KuCoin legacy classification remains venue-specific.
                    import re as _re
                    rc_match  = _re.search(r"KuCoin\s+(\d+):\s*(.*)|code['\"]?\s*[:=]\s*['\"]?(\d+)", err_str)
                    ret_code  = rc_match.group(1) if rc_match else "?"
                    ret_msg   = rc_match.group(2).strip() if rc_match else err_str

                    log.error(
                        f"❌ _open {sig.symbol} tentativa {attempt}/{MAX_RETRIES} FALHOU | "
                        f"retCode={ret_code} retMsg='{ret_msg}' | "
                        f"params: side={side} qty={qty} qty_step={qty_step} "
                        f"entry={sig.entry:.6f} sl={sig.sl:.6f} tp={sig.tp:.6f} "
                        f"tick={tick_size} notional={qty * sig.entry:.4f} "
                        f"balance={self.risk.balance:.4f} leverage={cfg.LEVERAGE} | "
                        f"raw_error={err_str}"
                    )

                    # Erros não-recuperáveis — não faz sentido tentar de novo
                    # CORRIGIDO: os códigos anteriores (10001, 110007...) eram da
                    # BYBIT. Na KuCoin nenhum deles existe, então erros permanentes
                    # (saldo insuficiente, qty inválida) eram retentados 3x
                    # inutilmente, com backoff — atrasando o scan.
                    NON_RETRYABLE = {
                        "400100",  # parâmetro inválido
                        "400001",  # parâmetro obrigatório ausente
                        "400002",  # KC-API-TIMESTAMP inválido
                        "400003",  # KC-API-KEY inválida
                        "400004",  # KC-API-PASSPHRASE inválida
                        "400005",  # KC-API-SIGN inválida
                        "400006",  # IP não autorizado
                        "400007",  # sem permissão
                        "300003",  # saldo insuficiente
                        "300012",  # ordem rejeitada por risco
                        "100001",  # ordem não existe
                    }
                    if ret_code in NON_RETRYABLE:
                        log.error(
                            f"🚫 _open {sig.symbol}: retCode={ret_code} é não-recuperável "
                            f"— abortando sem retry"
                        )
                        break

                    if attempt < MAX_RETRIES:
                        delay = RETRY_DELAYS[attempt - 1]
                        log.warning(
                            f"⏳ _open {sig.symbol}: aguardando {delay}s antes da "
                            f"tentativa {attempt + 1}/{MAX_RETRIES}..."
                        )
                        await asyncio.sleep(delay)

            if last_exc is not None:
                # Todas as tentativas falharam — loga resumo final e aborta
                log.error(
                    f"💀 _open {sig.symbol}: todas as {MAX_RETRIES} tentativas falharam | "
                    f"último erro: {last_exc} | "
                    f"parâmetros finais: side={side} qty={qty} "
                    f"sl={sig.sl:.6f} tp={sig.tp:.6f} entry={sig.entry:.6f}"
                )
                return

            # ══════════════════════════════════════════════════════
            # P1 (Auditoria forense final) — PREÇO DE EXECUÇÃO
            #
            # GAP ENCONTRADO: wait_for_fill() já consulta
            # GET /api/v1/orders/{orderId}, que a KuCoin responde com
            # dealSize/dealValue (de onde dá para derivar o preço médio
            # REAL de execução). O código descartava esse dado e usava
            # o ticker público em cache como aproximação — uma fonte
            # menos precisa quando já havia uma mais precisa disponível
            # na mesma resposta que acabara de ser consultada.
            #
            # Prioridade: avgDealPrice/dealValue-dealSize (dado real da
            # ordem) > ticker em cache (aproximação de mercado).
            # ══════════════════════════════════════════════════════
            if not self.paper_trade:
                # ══════════════════════════════════════════════════
                # F-013 (Binance) — GEOMETRIA PÓS-FILL ÚNICA
                #
                # A proteção nativa já foi instalada na Binance nos níveis
                # planejados. Deslocar SL/TP localmente pelo delta do fill
                # (ou pelo ticker) criava duas geometrias. Agora: fill só do
                # status da ordem; SL/TP locais = proteção na exchange; aperto
                # (se o orçamento V3 exigir) é feito NA EXCHANGE com readback.
                # ══════════════════════════════════════════════════
                from bot import postfill_risk_recheck as _pfg
                _geo = await _pfg.reconcile(self, sig, qty, _fill_check.get("status") or {}, log)
                sig.entry, sig.sl, sig.tp = _geo.entry, _geo.sl, _geo.tp
            else:
                try:
                    _fill = 0.0
                    _st = _fill_check.get("status", {}) or {}
                    _deal_size  = float(_st.get("dealSize", 0)  or 0)
                    _deal_value = float(_st.get("dealValue", 0) or 0)
                    if _deal_size > 0 and _deal_value > 0:
                        _fill = _deal_value / _deal_size   # preço médio real
                    if _fill <= 0:
                        _tk = self.client.get_cached_ticker(sig.symbol) or {}
                        _fill = float(_tk.get("lastPrice", 0) or 0)
                    if _fill > 0:
                        _slip_pct = abs(_fill - sig.entry) / sig.entry * 100
                        if _slip_pct > 0.05:
                            log.warning(
                                f"📊 {sig.symbol}: slippage {_slip_pct:.3f}% "
                                f"(sinal ${sig.entry:.4f} → fill ${_fill:.4f})"
                            )
                        # Desloca SL/TP na mesma proporção para preservar o R:R
                        _delta = _fill - sig.entry
                        sig.entry += _delta
                        sig.sl    += _delta
                        sig.tp    += _delta
                except Exception as e:
                    log.debug(f"fill price {sig.symbol}: {e}")

            # EXEC-01: `qty` aqui vem de RiskManager.size() e JÁ está em
            # UNIDADE BASE — NÃO converter. Este é o caminho de origem
            # da unidade correta; a conversão base→contratos acontece
            # apenas em _round_qty(), no momento do envio da ordem.
            pos = Position(sig, qty)
            pos.pre_score = pre_score["total"]
            self.positions[sig.symbol] = pos
            _plan = getattr(_managed, "protection_plan", None)
            if isinstance(_plan, dict):
                _plan["materialized"] = True
            # Diagnostic filled-position count only. Submission was consumed
            # before sending, including every ambiguous/error path.
            if self.pilot.enabled:
                self.pilot.register_position_opened(sig.symbol)
            # Persiste no banco
            # ITEM 2: grava os COMPONENTES do score, não só o total.
            # Mantém os dados necessários para uma futura calibração offline
            # dos pesos (+10/+5/+3), hoje definidos manualmente. O antigo
            # calibrador bot/score_weights.py nunca foi conectado e foi removido.
            _feats = {}
            try:
                for _k, _v in (pre_score or {}).items():
                    if isinstance(_v, (int, float)) and _k != "total":
                        _feats[_k] = float(_v)
                _feats["mtf_score"] = float(sig.score)
                _feats["rr"]        = float(sig.rr)
                _feats["regime"]    = 1.0 if "TRENDING" in str(sig.reason) else 0.0
            except Exception as _e:
                log.debug(f"score_features {sig.symbol}: {_e}")

            try:
                if self.paper_trade and self._durable_state_enforced:
                    def _paper_open_snapshot(new_trade_id):
                        self._trade_ids[sig.symbol] = int(new_trade_id)
                        return durable.build_paper_runtime_payload(
                            self, f"position_opened:{sig.symbol}"
                        )

                    trade_id = await db.save_paper_open_atomic(
                        sig.symbol, side, sig.entry, qty,
                        cfg.LEVERAGE, pre_score["total"],
                        durable.PAPER_STATE_KEY, _paper_open_snapshot,
                        score_features=_feats,
                        sl=sig.sl,             # ITEM 4: define 1R do trade
                        direction=sig.direction,
                    )
                else:
                    trade_id = await db.save_trade_open(
                        sig.symbol, side, sig.entry, qty,
                        cfg.LEVERAGE, pre_score["total"],
                        score_features=_feats,
                        sl=sig.sl,             # ITEM 4: define 1R do trade
                        direction=sig.direction,
                    )
            except db.PersistenceError as exc:
                self._trade_ids.pop(sig.symbol, None)
                if self.paper_trade:
                    # PAPER has no external fill to preserve. Roll back the
                    # in-memory simulation when its durable open did not commit.
                    self.positions.pop(sig.symbol, None)
                durable._block(self, "trade")
                if self.paper_trade:
                    log.critical(
                        "[DURABLE_TRADE] %s PAPER open rolled back because its "
                        "atomic snapshot failed; new entries blocked: %s",
                        sig.symbol, exc,
                    )
                else:
                    log.critical(
                        "[DURABLE_TRADE] %s filled but trade row was not persisted; "
                        "new entries blocked, position management remains active: %s",
                        sig.symbol, exc,
                    )
                if self.paper_trade and self._durable_state_enforced:
                    await durable.persist_paper_runtime(
                        self, "trade_row_failure", strict=False
                    )
                return
            self._trade_ids[sig.symbol] = trade_id
            durable._clear(self, "trade")
            entry_type = "BOS_BREAK" if "ENTRY:BOS_BREAK" in sig.reason else \
                         "MOMENTUM" if "ENTRY:MOMENTUM" in sig.reason else "PULLBACK"
            log.info(
                f"✅ ABERTO {sig.direction} {qty} {sig.symbol} @ ${sig.entry:.4f} "
                f"SL=${sig.sl:.4f} TP=${sig.tp:.4f} "
                f"Score={sig.score}/100 RR={sig.rr} "
                f"Tipo={entry_type} ADX={sig.reason}"
            )
            _obal = await self.client.get_balance()
            # Enriquecer sinal com TP1/TP2 se disponível
            if hasattr(sig, 'tp1') and sig.tp1 != sig.tp:
                asyncio.create_task(notify(
                    f"{'🟢🚀' if sig.direction == 'LONG' else '🔴🩸'} *SINAL — {'COMPRA (LONG)' if sig.direction == 'LONG' else 'VENDA (SHORT)'}*\n"
                    f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                    f"📍 Par:    `{sig.symbol}`\n"
                    f"💰 Entrada: `${sig.entry:,.4f}`\n"
                    f"🛑 SL:      `${sig.sl:,.4f}` _(nível técnico)_\n"
                    f"🎯 TP1:     `${sig.tp1:,.4f}` _(50% — R:R {sig.rr1:.1f})_\n"
                    f"🏆 TP2:     `${sig.tp2:,.4f}` _(50% — R:R {sig.rr2:.1f})_\n"
                    f"🧠 Score:   `{sig.score}/100`\n"
                    f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                    f"_SL move para break-even ao atingir TP1_"
                ))
            else:
                await notify(await signal_msg(sig))
            await notify(await order_opened_msg(sig, qty, _obal, _obal*cfg.LEVERAGE))
        except (NameError, AttributeError, TypeError, ImportError) as e:
            # ══════════════════════════════════════════════════════
            # ERRO DE PROGRAMAÇÃO — não é falha operacional.
            #
            # Três bugs críticos deste projeto ficaram escondidos aqui:
            #   • notify_nexus sem import      → NameError
            #   • self._recalc_daily_limits()  → AttributeError
            #   • price indefinida             → NameError
            #
            # Todos eram engolidos como "exceção inesperada" e o bot
            # seguia sem abrir posição, sem sinal claro do que houve.
            #
            # Agora: log CRITICAL + alerta no Telegram, para que um bug
            # de código nunca mais passe despercebido.
            # ══════════════════════════════════════════════════════
            import traceback
            _tb = traceback.format_exc()
            log.critical(
                f"🐛 BUG DE CÓDIGO em _open {sig.symbol}: "
                f"{type(e).__name__}: {e}\n"
                f"Isto NÃO é falha de mercado ou de API — é erro de "
                f"programação e precisa ser corrigido.\n"
                f"Traceback:\n{_tb}"
            )
            try:
                asyncio.create_task(notify(
                    f"🐛 *BUG DE CÓDIGO DETECTADO*\n"
                    f"`{'━'*26}`\n"
                    f"📍 Par:  `{sig.symbol}`\n"
                    f"❌ Erro: `{type(e).__name__}`\n"
                    f"💬 `{str(e)[:120]}`\n"
                    f"`{'━'*26}`\n"
                    f"_O bot não conseguiu abrir esta posição por erro de "
                    f"programação, não por condição de mercado._"
                ))
            except Exception:
                pass   # notificação é best-effort; o log CRITICAL basta

        except Exception as e:
            import traceback
            log.error(
                f"❌ _open {sig.symbol}: exceção inesperada — {e}\n"
                f"Parâmetros do sinal: direction={sig.direction} entry={sig.entry} "
                f"sl={sig.sl} tp={sig.tp} score={sig.score} rr={sig.rr}\n"
                f"Traceback:\n{traceback.format_exc()}"
            )

    # ── Load existing positions on startup ─────────────────────
    async def _load_existing_positions(self):
        try:
            all_pos = await self.client.get_positions()
            count = 0
            for p in all_pos:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                sym   = p["symbol"]
                side  = p.get("side", "Buy")
                # CORRIGIDO (auditoria #2): KuCoinClient.get_positions() usa
                # "entryPrice" e "liquidationPrice". As chaves "avgPrice"/"liqPrice"
                # eram do formato Bybit e retornavam 0 na KuCoin, zerando
                # entry/SL/TP de posições carregadas após restart.
                ep    = float(p.get("entryPrice", p.get("avgPrice", 0)))
                upnl  = float(p.get("unrealisedPnl", 0))
                liq   = float(p.get("liquidationPrice", p.get("liqPrice", 0)))

                if ep <= 0:
                    log.error(
                        f"⚠️ {sym}: entryPrice inválido ({ep}) no retorno da "
                        f"exchange — posição NÃO carregada para evitar SL/TP "
                        f"zerados. Campos recebidos: {list(p.keys())}"
                    )
                    continue

                direction = "LONG" if side == "Buy" else "SHORT"
                atr_est = ep * 0.007

                if direction == "LONG":
                    sl = max(liq * 1.02, ep - atr_est * 1.5) if liq > 0 else ep - atr_est * 1.5
                    tp = ep + atr_est * 3.0
                else:
                    sl = min(liq * 0.98, ep + atr_est * 1.5) if liq > 0 else ep + atr_est * 1.5
                    tp = ep - atr_est * 3.0

                # EXEC-01: `size` vem de get_positions() = CONTRATOS.
                try:
                    _base_size = self._contracts_to_base_qty(sym, size)
                except ValueError as _ue:
                    log.critical(
                        f"🚨 STARTUP SYNC {sym}: {_ue} — posição NÃO carregada "
                        f"(unidade desconhecida, não é seguro operá-la)"
                    )
                    continue

                sig = Signal(sym, direction, ep, sl, tp, 0.75, "Startup sync", 75)
                pos = Position(sig, _base_size)
                pos.pnl = upnl
                cur = float(p.get("markPrice", ep))
                pos.update_pnl(cur)
                self.positions[sym] = pos
                count += 1
                log.info(f"📂 Carregada: {direction} {size} {sym} @ ${ep:.4f} PnL=${upnl:.4f}")

            if count:
                log.info(f"✅ {count} posição(ões) sincronizadas da exchange")
        except Exception as e:
            log.error(f"_load_existing: {e}")

    async def _heartbeat_telegram(self):
        """
        Envia um resumo periódico ao Telegram (default: a cada 30 min).
        Mostra saldo, posições, PnL do dia e o melhor score visto no scan,
        para o usuário saber que o bot está vivo e o que ele está enxergando.
        Configurável via HEARTBEAT_MIN (0 desativa).
        """
        try:
            interval_min = int(os.environ.get("HEARTBEAT_MIN", "30"))
            if interval_min <= 0:
                return
            now  = time.time()
            last = getattr(self, "_last_heartbeat", 0.0)
            if now - last < interval_min * 60:
                return
            self._last_heartbeat = now

            bal    = self.risk.balance
            best   = getattr(self, "_last_best_score", None)
            n_open = len(self.positions)

            # Diagnóstico: distribuição dos scores recentes.
            # Se todos ficam muito abaixo do mínimo, o problema é de
            # mercado ou de threshold — não de bug.
            # Usa o buffer do strategy, que registra TODO score avaliado
            # (inclusive os que deram HOLD). O buffer local só recebia
            # sinais aprovados e ficava vazio justamente quando nada passa.
            try:
                from bot.strategy import get_score_log
                _sc = [x["score"] for x in get_score_log(200)]
            except Exception:
                _sc = getattr(self, "_score_hist", [])

            if _sc:
                _mx  = max(_sc)
                _avg = sum(_sc) / len(_sc)
                _perto = len([s for s in _sc if s >= cfg.MIN_ENTRY_SCORE - 5])
                score_line = (
                    f"📊 Scores ({len(_sc)}): máx `{_mx}` méd `{_avg:.0f}` | "
                    f"`{_perto}` a ≤5pts do mínimo\n"
                )
            else:
                score_line = "📊 Scores: `nenhum sinal avaliado ainda`\n"

            best_line = (
                f"🧠 Melhor score: `{best['score']}/100` em `{best['symbol']}` "
                f"({best['direction']})\n"
                if best else
                "🧠 Melhor score: `nenhum sinal no último scan`\n"
            )

            # ITEM 4: expectancy em vez de win rate isolado.
            # Win rate sozinho não diz se a estratégia é lucrativa — o que
            # diz é a expectancy e a margem sobre o win rate de breakeven.
            exp_line = ""
            try:
                _ex = await db.get_expectancy_stats()
                if _ex.get("status") == "ok" and _ex.get("n_trades", 0) > 0:
                    _m = _ex["margem_pp"]
                    _icon = "✅" if _m > 5 else "⚠️" if _m > 0 else "🔴"
                    exp_line = (
                        f"`━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                        f"📊 Trades:     `{_ex['n_trades']}`\n"
                        f"🎲 Win rate:   `{_ex['win_rate']}%` "
                        f"(breakeven `{_ex['breakeven_wr']}%`)\n"
                        f"{_icon} Margem:     `{_m:+.1f}pp`\n"
                        f"💡 Expectancy: `{_ex['expectancy_R']:+.3f}R` "
                        f"(`${_ex['expectancy_usd']:+.3f}`/trade)\n"
                        f"⚖️ Payoff:     `{_ex['payoff_ratio']:.2f}` | "
                        f"PF `{_ex.get('profit_factor') or '—'}`\n"
                        f"📉 Maior seq. perdas: `{_ex['max_loss_streak']}`\n"
                        f"_{_ex['verdict']}_\n"
                    )
            except Exception as _e:
                log.debug(f"heartbeat expectancy: {_e}")

            await notify(
                f"💓 *BGX — STATUS*\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                f"💰 Saldo:    `${bal:,.2f}` USDT\n"
                f"⚡ Poder:    `${bal * cfg.LEVERAGE:,.2f}` ({cfg.LEVERAGE}x)\n"
                f"📊 Posições: `{n_open}/{cfg.MAX_POSITIONS}`\n"
                f"💵 PnL real: `${getattr(self, 'daily_pnl_realized', 0.0):+,.2f}` "
                f"_(conta p/ meta)_\n"
                f"📊 Em aberto:`${getattr(self, 'daily_pnl_unrealized', 0.0):+,.2f}`\n"
                f"🎯 Meta:     `${self.daily_target:,.2f}`\n"
                f"🔍 Pares:    `{len(self.viable_symbols)}`\n"
                f"{best_line}"
                f"{score_line}"
                f"{exp_line}"
                f"⚙️ Score mín: `{cfg.MIN_ENTRY_SCORE}` | R:R mín: `{cfg.MIN_RR_RATIO}`\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━`"
            )
        except Exception as e:
            log.debug(f"_heartbeat_telegram: {e}")

    async def _guard_naked_positions(self):
        """
        GUARDIÃO DE POSIÇÕES DESPROTEGIDAS (proteção de capital).

        Verifica a cada ciclo se TODA posição aberta na exchange tem stop
        loss anexado. Uma posição sem SL com 50x liquida a ~2% de
        movimento adverso — é o cenário de perda total.

        Cenários que isto cobre e que a verificação da abertura não pega:
          • stop removido manualmente na interface da KuCoin
          • stop cancelado pela exchange após execução parcial
          • posição aberta em deploy anterior, antes do fix do SL
          • falha de rede no momento exato do trading-stop

        Ação: tenta reaplicar o SL. Se falhar, FECHA a posição.
        """
        if self.paper_trade:
            return
        try:
            positions = await self.client.get_positions()
        except Exception as e:
            log.debug(f"_guard_naked_positions: {e}")
            return

        for p in positions:
            try:
                sym  = p.get("symbol", "")
                size = float(p.get("size", 0) or 0)
                if size <= 0:
                    continue
                sl_ex = float(p.get("stopLoss", 0) or 0)
                if sl_ex > 0:
                    continue   # protegida

                entry = float(p.get("entryPrice", 0) or 0)
                side  = p.get("side", "Buy")
                if entry <= 0:
                    continue

                log.critical(
                    f"🚨 {sym}: POSIÇÃO SEM STOP LOSS na exchange "
                    f"(size={size} entry=${entry:.4f}) — reaplicando"
                )

                # ══════════════════════════════════════════════════
                # P0 (ADV-01) — REGISTRA A POSIÇÃO ÓRFÃ AQUI TAMBÉM
                #
                # Este guardião já rodava a cada ciclo e já reaplicava
                # o SL na exchange — mas NUNCA adicionava a posição a
                # self.positions quando ela não existia localmente.
                # Resultado: mesmo com SL reaplicado, a posição órfã
                # nunca entrava no trailing/TP-parcial/stagnação, e o
                # IntegrityGuard continuava vendo STATE_DIVERGENCE para
                # sempre (bloqueio permanente de novas entradas mesmo
                # depois de "resolvido").
                #
                # Delega para _reconcile_exchange_positions, que já
                # tem toda a lógica de reconstrução com dados REAIS da
                # exchange (entry/liq/SL/TP) — evita duplicar a lógica
                # aqui.
                # ══════════════════════════════════════════════════
                if sym not in self.positions:
                    await self._reconcile_exchange_positions(only_symbol=sym)

                # SL do estado interno, ou 1.5% como fallback de emergência
                pos_local = self.positions.get(sym)
                if pos_local and getattr(pos_local, "sl", 0) > 0:
                    sl_target = pos_local.sl
                else:
                    sl_target = entry * (0.985 if side == "Buy" else 1.015)

                ok = await self.client.set_position_stops(sym, sl=sl_target)
                if ok:
                    log.info(f"✓ {sym}: SL reaplicado @ ${sl_target:.4f}")
                    await notify(
                        f"🛡️ *SL REAPLICADO*\n"
                        f"`{sym}` estava sem stop loss na exchange.\n"
                        f"Stop reaplicado em `${sl_target:.4f}`."
                    )
                    continue

                # Não conseguiu proteger → fecha para não ficar exposto
                log.critical(f"🚨 {sym}: falha ao reaplicar SL — FECHANDO posição")
                res = await self.client.place_order(
                    symbol=sym,
                    side="Sell" if side == "Buy" else "Buy",
                    qty=size, sl=0, tp=0,
                    instruments=self.instruments,
                    reduce_only=True,
                )
                _emergency_oid = res.get("orderId") if isinstance(res, dict) else ""
                _emergency_flat = False
                if _emergency_oid:
                    _emergency_fill = await self.client.wait_for_fill(
                        _emergency_oid, timeout_s=8.0
                    )
                    try:
                        _after = await self.client.get_positions()
                        _remaining = next(
                            (
                                ep for ep in _after
                                if ep.get("symbol") == sym
                                and abs(float(ep.get("size", 0) or 0)) > 0
                            ),
                            None,
                        )
                        _emergency_flat = _remaining is None
                    except Exception as _verify_exc:
                        log.critical(
                            "[EMERGENCY_CLOSE_UNCONFIRMED] symbol=%s orderId=%s "
                            "stage=POSITION_READ error=%s local_position_preserved=true "
                            "entries_blocked=true",
                            sym, _emergency_oid, type(_verify_exc).__name__,
                        )

                    if _emergency_fill.get("filled") and _emergency_flat:
                        self.positions.pop(sym, None)
                        self._unprotected_symbols.discard(sym)
                        log.critical(
                            "[EMERGENCY_CLOSE_CONFIRMED] symbol=%s orderId=%s "
                            "fill_confirmed=true exchange_flat=true",
                            sym, _emergency_oid,
                        )
                        await notify(
                            f"🚨 *POSIÇÃO FECHADA POR SEGURANÇA*\n"
                            f"`{sym}` estava SEM STOP LOSS e não foi possível\n"
                            f"reaplicá-lo. Fechamento confirmado na KuCoin."
                        )
                        continue

                # orderId/HTTP acceptance is not a fill. Keep the local
                # position authoritative until KuCoin proves the account flat.
                self._unprotected_symbols.add(sym)
                log.critical(
                    "[EMERGENCY_CLOSE_UNCONFIRMED] symbol=%s orderId=%s "
                    "fill_confirmed=%s exchange_flat=%s local_position_preserved=true "
                    "entries_blocked=true",
                    sym, _emergency_oid or "NONE",
                    bool(locals().get("_emergency_fill", {}).get("filled", False)),
                    _emergency_flat,
                )
                await notify(
                    f"🆘 *AÇÃO MANUAL NECESSÁRIA*\n"
                    f"`{sym}` está SEM STOP LOSS e o fechamento automático\n"
                    f"não foi confirmado como FILLED + FLAT na KuCoin.\n"
                    f"Novas entradas foram bloqueadas."
                )
            except Exception as e:
                log.error(f"_guard_naked_positions {p.get('symbol','?')}: {e}")

    def _gc_caches(self):
        """
        Expurga entradas expiradas dos dicts auxiliares.

        _cooldown e _trade_ids só recebiam escritas e nunca eram limpos.
        Em operação 24/7 crescem indefinidamente — leak lento mas real.
        """
        now = time.time()
        for sym in [s for s, t in list(self._cooldown.items()) if t < now]:
            self._cooldown.pop(sym, None)

        # Dicts sem expurgo até então — leak lento em operação 24/7.
        # Mantém apenas símbolos que ainda estão na lista operável.
        _viaveis = set(self.viable_symbols or []) | set(self.positions.keys())
        for d_name in ("_oi_hist", "_last_nexus"):
            d = getattr(self, d_name, None)
            if isinstance(d, dict) and len(d) > 60:
                for k in [k for k in list(d.keys()) if k not in _viaveis]:
                    d.pop(k, None)
        if len(getattr(self, "_trade_ids", {})) > 200:
            for sym in list(self._trade_ids.keys()):
                if sym not in self.positions:
                    self._trade_ids.pop(sym, None)

    async def _update_balance(self):
        """
        BUG CORRIGIDO: o await notify(drawdown_msg) estava FORA do if de
        drawdown (indentação errada), disparando o alerta "DRAWDOWN ELEVADO"
        a cada ciclo mesmo com drawdown 0.0%.

        Agora: só notifica quando o limite é realmente ultrapassado, e apenas
        UMA vez por evento (flag _dd_alerted evita spam).
        """
        try:
            bal = await self.client.get_balance()
            self.risk.update(bal)

            # BUG CORRIGIDO: self._recalc_daily_limits() era chamado aqui mas
            # o método NÃO EXISTE na classe — lançava AttributeError a cada
            # ciclo, capturado pelo except abaixo. Efeito: o bloco inteiro
            # abortava, então o alerta de drawdown NUNCA era avaliado.
            # A lógica de recálculo já existe em _check_daily_reset(); aqui
            # apenas mantemos meta/stop coerentes com o saldo atual.
            if bal > 0:
                self.daily_target    = round(bal * cfg.DAILY_TARGET_PCT, 2)
                self.daily_stop_loss = round(bal * cfg.DAILY_STOP_LOSS_PCT, 2)

            if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
                if not getattr(self, "_dd_alerted", False):
                    self._dd_alerted = True
                    self.active = False
                    log.warning(
                        f"🚨 Drawdown {self.risk.drawdown:.1%} ≥ "
                        f"{cfg.MAX_DRAWDOWN:.0%} → pausando entradas"
                    )
                    await notify(await drawdown_msg(self.risk.drawdown, bal))
            else:
                # Drawdown normalizou → rearma o alerta para o próximo evento
                self._dd_alerted = False
        except Exception as e:
            self.risk.balance_confirmed = False
            log.error(f"_update_balance: {type(e).__name__}")

    # ── Status (endpoint /api/status) ──────────────────────────
    def get_status(self) -> dict:
        summaries = self.stats.all_summaries()
        return {
            "connected":        self.connected,
            "active":           self.active,
            "entries_paused":   bool(getattr(self, 'entries_paused', False)),
            "balance":          round(self.risk.balance, 4),
            "buying_power":     round(self.risk.balance * cfg.LEVERAGE, 2),
            "drawdown_pct":     round(self.risk.drawdown * 100, 2),
            "leverage":         cfg.LEVERAGE,
            "max_positions":    cfg.MAX_POSITIONS,
            "min_entry_score":  cfg.MIN_ENTRY_SCORE,
            "open_positions":   len(self.positions),
            "viable_symbols":   len(self.viable_symbols),
            "positions":        [p.to_dict() for p in self.positions.values()],
            "pnl_session":      summaries["session"],
            "pnl_1d":           summaries["1d"],
            "pnl_7d":           summaries["7d"],
            "pnl_30d":          summaries["30d"],
            "wins":             summaries["session"]["wins"],
            "losses":           summaries["session"]["losses"],
            "win_rate_pct":     summaries["session"]["win_rate"],
            "total_pnl":        summaries["session"]["pnl"],
            "symbols":          self.viable_symbols[:10],
            "macro_corr":       mdata.get_macro_summary(),
            # ── Meta diária
            "daily_target":     self.daily_target,
            "daily_stop_loss":  self.daily_stop_loss,
            "daily_pnl":        round(self.daily_pnl, 4),
            "daily_target_hit": self.daily_target_hit,
            "daily_stopped":    self.daily_stopped,
            "daily_progress":   round(min(self.daily_pnl / self.daily_target * 100, 100), 1) if self.daily_target else 0,
            "effective_score":  self._effective_score(),
            "mode":             "CONSERVADOR" if self.daily_target_hit else ("PARADO" if self.daily_stopped else "AGRESSIVO"),
        }
