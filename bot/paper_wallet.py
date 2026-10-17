"""PAPER-only virtual wallet isolation with durable restart state.

PAPER capital must not depend on deposits, withdrawals, manual trades, margin
reservation, or any other KuCoin account-balance movement. The simulator uses
explicit virtual capital when configured and then changes that capital only
through simulated PAPER results.

The virtual balance and peak balance are persisted in the existing key_value
store so a process/deploy restart cannot silently reset PAPER PnL/drawdown.

LIVE behavior, trading thresholds, leverage, risk parameters and production
decision gates are unchanged.
"""
from __future__ import annotations

import asyncio
import json
import os

_PAPER_WALLET_KEY = "paper_wallet_state_v1"


def install(log):
    from bot.engine import TradingEngine
    from bot.exchange import TAKER_FEE

    if getattr(TradingEngine, "_paper_wallet_patched", False):
        return

    original_connect = TradingEngine._connect
    original_startup_risk_balance = TradingEngine._startup_risk_balance
    original_filter_viable = TradingEngine._filter_viable_symbols
    original_update_balance = TradingEngine._update_balance
    original_refresh_entry_balance = TradingEngine._refresh_entry_balance
    original_manage_partial_tp = TradingEngine._manage_partial_tp

    async def _persist_state(self, reason: str):
        """Persist PAPER-only wallet state; never affects execution semantics."""
        if not getattr(self, "paper_trade", False):
            return
        try:
            from bot import database as db
            balance = max(0.0, float(getattr(self, "_paper_balance", 0.0) or 0.0))
            peak = max(balance, float(getattr(self.risk, "peak_balance", balance) or balance))
            payload = json.dumps({
                "version": 1,
                "balance": balance,
                "peak_balance": peak,
                "reason": str(reason)[:160],
            }, separators=(",", ":"), sort_keys=True)
            await db.save_key_value(_PAPER_WALLET_KEY, payload)
            log.debug(
                "[PAPER_WALLET] state persisted balance=$%.4f peak=$%.4f reason=%s",
                balance, peak, reason,
            )
        except Exception as exc:
            # Persistence failure must be visible, but cannot mutate trade/risk state.
            log.warning("[PAPER_WALLET] persistence failed: %s: %s", type(exc).__name__, exc)

    def _schedule_persist(self, reason: str):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("[PAPER_WALLET] persistence skipped: no running event loop")
            return
        runtime_persist = getattr(self, "_paper_persist_runtime", None)
        if callable(runtime_persist) and getattr(self, "_durable_state_enforced", False):
            loop.create_task(runtime_persist(reason))
        else:
            loop.create_task(_persist_state(self, reason))

    async def _load_persisted_state(self):
        """Return validated persisted state or None when no valid state exists."""
        try:
            from bot import database as db
            raw = await db.load_key_value(_PAPER_WALLET_KEY)
        except Exception as exc:
            log.warning("[PAPER_WALLET] restore read failed: %s: %s", type(exc).__name__, exc)
            return None
        if not raw:
            return None
        try:
            state = json.loads(raw)
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("unsupported state schema")
            balance = float(state.get("balance"))
            peak = float(state.get("peak_balance"))
            if balance < 0 or peak < 0 or peak < balance:
                raise ValueError("invalid balance/peak relationship")
            return {"balance": balance, "peak_balance": peak}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("[PAPER_WALLET] persisted state invalid; ignoring: %s", exc)
            return None

    def _sync_daily_limits(self, balance: float):
        """Keep engine and DailyTracker limits on the same PAPER capital."""
        from bot.config import cfg

        if balance <= 0:
            return
        if cfg.DAILY_TARGET <= 0:
            self.daily_target = round(balance * cfg.DAILY_TARGET_PCT, 2)
        # Daily stop is intentionally disabled in DailyTracker. Do not revive it
        # here merely because a legacy config value is zero.
        self.daily_stop_loss = 0.0
        try:
            self.daily_tracker.recalc_limits(balance)
            self.daily_tracker.daily_target = self.daily_target
            self.daily_tracker.daily_stop_loss = 0.0
        except Exception as exc:
            log.warning("[PAPER_WALLET] daily-limit sync failed: %s", exc)

    async def _restore_persisted_state(self) -> bool:
        """Restore a validated PAPER wallet snapshot without exchange I/O.

        Kept as a dedicated method so restart semantics can be regression-tested
        deterministically without connecting to KuCoin or placing any order.
        """
        persisted = await _load_persisted_state(self)
        if persisted is None:
            return False

        balance = persisted["balance"]
        peak = persisted["peak_balance"]
        self._paper_balance = balance
        self.risk.peak_balance = peak
        self.risk.balance = balance
        self.risk.drawdown = ((peak - balance) / peak) if peak > 0 else 0.0
        self.risk.balance_confirmed = balance > 0
        _sync_daily_limits(self, balance)
        log.info(
            "🧪 PAPER wallet restaurada: saldo=$%.4f peak=$%.4f drawdown=%.2f%% "
            "source=database; restart não resetou o histórico virtual",
            balance, peak, self.risk.drawdown * 100.0,
        )
        return True

    def _apply_virtual_balance(self, new_balance: float, reason: str):
        new_balance = max(0.0, float(new_balance))
        self._paper_balance = new_balance
        self.risk.update(new_balance)
        log.info(
            "[PAPER_WALLET] balance=$%.4f drawdown=%.2f%% reason=%s",
            new_balance,
            self.risk.drawdown * 100.0,
            reason,
        )
        _schedule_persist(self, reason)

    def _virtual_initial_balance(observed_balance: float) -> tuple[float, str]:
        """Choose PAPER capital without depending on mutable exchange balance."""
        from bot.config import cfg

        raw = os.environ.get("PAPER_INITIAL_BALANCE", "").strip()
        if raw:
            try:
                configured = float(raw)
                if configured > 0:
                    return configured, "PAPER_INITIAL_BALANCE"
            except (TypeError, ValueError):
                log.warning(
                    "[PAPER_WALLET] PAPER_INITIAL_BALANCE inválido; usando fallback seguro"
                )

        configured = float(getattr(cfg, "INITIAL_CAP", 0.0) or 0.0)
        if configured > 0:
            return configured, "INITIAL_CAP"

        observed = max(0.0, float(observed_balance or 0.0))
        if observed > 0:
            return observed, "startup_exchange_snapshot"

        return 0.0, "unconfirmed_zero"

    async def _filter_viable_symbols_paper_safe(self):
        """Use virtual buying power during PAPER startup viability filtering."""
        if not getattr(self, "paper_trade", False):
            return await original_filter_viable(self)

        # original _connect() reads authenticated exchange balance before this
        # filter. PAPER viability must never depend on that mutable real value,
        # including when it is a small positive balance rather than exactly 0.
        if not hasattr(self, "_paper_balance"):
            restored = await _restore_persisted_state(self)
            if not restored:
                observed = float(getattr(self.risk, "balance", 0.0) or 0.0)
                initial, source = _virtual_initial_balance(observed)
                self._paper_balance = initial
                self.risk.peak_balance = initial
                self.risk.balance = initial
                self.risk.drawdown = 0.0
                self.risk.balance_confirmed = initial > 0
                _sync_daily_limits(self, initial)
                log.info(
                    "[PAPER_WALLET] viability uses virtual capital=$%.4f source=%s",
                    initial,
                    source,
                )
                await _persist_state(self, f"initial_seed:{source}")
        else:
            virtual = float(getattr(self, "_paper_balance", 0.0) or 0.0)
            self.risk.update(virtual)
            self.risk.balance_confirmed = virtual > 0
            _sync_daily_limits(self, virtual)

        return await original_filter_viable(self)

    async def _startup_risk_balance_paper_safe(self):
        if not getattr(self, "paper_trade", False):
            return await original_startup_risk_balance(self)

        if hasattr(self, "_paper_balance"):
            value = float(getattr(self, "_paper_balance", 0.0) or 0.0)
            if value > 0:
                return value

        if await _restore_persisted_state(self):
            return float(getattr(self, "_paper_balance", 0.0) or 0.0)

        initial, source = _virtual_initial_balance(0.0)
        if initial > 0:
            self._paper_balance = initial
            self.risk.peak_balance = initial
            self.risk.balance = initial
            self.risk.drawdown = 0.0
            self.risk.balance_confirmed = True
            _sync_daily_limits(self, initial)
            log.info(
                "[PAPER_WALLET] startup uses virtual capital=$%.4f source=%s; "
                "exchange credentials are not required for PAPER startup",
                initial, source,
            )
            await _persist_state(self, f"initial_seed:{source}")
            return initial

        return await original_startup_risk_balance(self)

    async def _connect_with_paper_wallet(self):
        await original_connect(self)
        if not getattr(self, "paper_trade", False):
            return
        if not getattr(self, "connected", False):
            return

        if not hasattr(self, "_paper_balance"):
            if await _restore_persisted_state(self):
                return

            observed = float(getattr(self.risk, "balance", 0.0) or 0.0)
            initial, source = _virtual_initial_balance(observed)
            self._paper_balance = initial
            self.risk.peak_balance = initial
            self.risk.balance = initial
            self.risk.drawdown = 0.0
            self.risk.balance_confirmed = initial > 0
            _sync_daily_limits(self, initial)
            log.info(
                "🧪 PAPER wallet isolada: saldo virtual inicial=$%.4f source=%s; "
                "mudanças na conta KuCoin não alteram PnL/drawdown PAPER",
                initial,
                source,
            )
            await _persist_state(self, f"initial_seed:{source}")

    async def _update_balance_paper_safe(self):
        if not getattr(self, "paper_trade", False):
            return await original_update_balance(self)

        bal = float(getattr(self, "_paper_balance", self.risk.balance) or 0.0)
        self.risk.update(bal)
        _sync_daily_limits(self, bal)

        from bot.config import cfg
        if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
            if not getattr(self, "_dd_alerted", False):
                self._dd_alerted = True
                self.active = False
                log.warning(
                    "🚨 [PAPER] Drawdown virtual %.1f%% ≥ %.0f%% → pausando entradas",
                    self.risk.drawdown * 100.0,
                    cfg.MAX_DRAWDOWN * 100.0,
                )
        else:
            self._dd_alerted = False

    async def _refresh_entry_balance_paper_safe(self):
        if not getattr(self, "paper_trade", False):
            return await original_refresh_entry_balance(self)

        bal = float(getattr(self, "_paper_balance", self.risk.balance) or 0.0)
        try:
            self.risk.update(bal)
        except Exception:
            self.risk.balance_confirmed = False
            return False
        if bal <= 0:
            log.warning("[PAPER_WALLET] entry blocked: virtual balance <= 0")
            return False
        return True

    async def _manage_partial_tp_with_wallet(self):
        if not getattr(self, "paper_trade", False):
            return await original_manage_partial_tp(self)

        before = {}
        for sym, pos in self.positions.items():
            before[sym] = {
                "qty": float(getattr(pos, "qty", 0.0) or 0.0),
                "tp1_hit": bool(getattr(pos, "tp1_hit", False)),
                "entry": float(getattr(pos, "entry", 0.0) or 0.0),
                "sl": float(getattr(pos, "sl", 0.0) or 0.0),
            }

        result = await original_manage_partial_tp(self)

        for sym, old in before.items():
            pos = self.positions.get(sym)
            if pos is None:
                continue
            if old["tp1_hit"] or not getattr(pos, "tp1_hit", False):
                continue

            qty_after = float(getattr(pos, "qty", 0.0) or 0.0)
            partial_qty = max(0.0, old["qty"] - qty_after)
            if partial_qty <= 0:
                continue

            risk_dist = abs(old["entry"] - old["sl"])
            cur = float(getattr(pos, "current_price", old["entry"]) or old["entry"])
            pnl_partial = risk_dist * partial_qty
            fee_p = partial_qty * cur * TAKER_FEE * 2
            pnl_net = pnl_partial - fee_p
            _apply_virtual_balance(
                self,
                float(getattr(self, "_paper_balance", self.risk.balance)) + pnl_net,
                f"partial_tp:{sym}:{pnl_net:+.4f}",
            )
            runtime_persist = getattr(self, "_paper_persist_runtime", None)
            if callable(runtime_persist) and getattr(self, "_durable_state_enforced", False):
                await runtime_persist(f"partial_tp:{sym}")

        return result

    TradingEngine._startup_risk_balance = _startup_risk_balance_paper_safe
    TradingEngine._filter_viable_symbols = _filter_viable_symbols_paper_safe
    TradingEngine._connect = _connect_with_paper_wallet
    TradingEngine._update_balance = _update_balance_paper_safe
    TradingEngine._refresh_entry_balance = _refresh_entry_balance_paper_safe
    TradingEngine._manage_partial_tp = _manage_partial_tp_with_wallet
    TradingEngine._paper_apply_virtual_balance = _apply_virtual_balance
    TradingEngine._paper_persist_wallet = _persist_state
    TradingEngine._paper_restore_wallet_from_db = _restore_persisted_state

    async def _persist_runtime(self, reason: str):
        from bot import durable_execution as durable
        return await durable.persist_paper_runtime(self, reason, strict=False)

    TradingEngine._paper_persist_runtime = _persist_runtime
    TradingEngine._paper_wallet_patched = True

    log.info("🧪 PAPER wallet isolation + persistence installed; LIVE balance path unchanged")
