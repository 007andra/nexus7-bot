"""Operator LIVE sizing and explicit risk-override policy.

The controlled LIVE pilot applies the operator margin ceiling:

* operator cap: initial margin <= 50% of freshly authenticated available
  collateral by default, or an explicitly configured fraction up to 100%;
* leverage is read from the existing configuration (production currently uses 50x);
* the executed quantity is decided by ``final_sizing_invariants``:
  ``min(stop_risk_qty, operator_margin_cap_qty)`` with RiskManagerV3 as risk
  authority. The hook installed here is shadowed in a pilot context.

A drawdown breach is fail-closed by default. It may be bypassed only when the
operator explicitly enables LIVE_RISK_OVERRIDE_APPROVED=true. The override
never bypasses balance, position-count, protection, duplicate-order,
reconciliation, instrument or other execution-safety gates.
"""
from __future__ import annotations

import math
import os

from bot.config import cfg
from bot import market_radar, startup_ready_notification


MARGIN_FRACTION = 0.50
RISK_OVERRIDE_ENV = "LIVE_RISK_OVERRIDE_APPROVED"


def _risk_override_enabled() -> bool:
    """Return True only for an explicit boolean operator acknowledgement."""
    return os.environ.get(RISK_OVERRIDE_ENV, "").strip().lower() == "true"


def _protect_drawdown_update(self, bound_update, log, *, source: str):
    """Wrap one balance update without silently neutralizing drawdown safety."""
    async def _guarded_update(*args, **kwargs):
        was_active = bool(getattr(self, "active", False))
        risk_before = getattr(self, "risk", None)
        drawdown_before = float(getattr(risk_before, "drawdown", 0.0) or 0.0)
        override = _risk_override_enabled()

        if was_active and drawdown_before >= float(cfg.MAX_DRAWDOWN):
            self._dd_alerted = True
            if override:
                log.critical(
                    "[DRAWDOWN_OVERRIDE_%s] drawdown=%.2f%% configured_limit=%.2f%% "
                    "override=true entries_blocked=false execution_effect=ALLOW_NEW_ENTRIES",
                    source,
                    drawdown_before * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                )
            else:
                log.error(
                    "[DRAWDOWN_HARD_GATE_%s] drawdown=%.2f%% configured_limit=%.2f%% "
                    "override=false entries_blocked=true execution_effect=BLOCK_NEW_ENTRIES",
                    source,
                    drawdown_before * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                )

        try:
            return await bound_update(*args, **kwargs)
        finally:
            risk = getattr(self, "risk", None)
            drawdown = float(getattr(risk, "drawdown", 0.0) or 0.0)
            became_inactive = was_active and not bool(getattr(self, "active", False))
            if became_inactive and drawdown >= float(cfg.MAX_DRAWDOWN):
                if _risk_override_enabled():
                    self.active = True
                    self._dd_alerted = True
                    log.critical(
                        "[DRAWDOWN_OVERRIDE_%s] drawdown=%.2f%% configured_limit=%.2f%% "
                        "legacy_pause_neutralized=true active_restored=true override=true",
                        source,
                        drawdown * 100.0,
                        float(cfg.MAX_DRAWDOWN) * 100.0,
                    )
                else:
                    log.error(
                        "[DRAWDOWN_HARD_GATE_%s] drawdown=%.2f%% configured_limit=%.2f%% "
                        "legacy_pause_preserved=true active_restored=false override=false",
                        source,
                        drawdown * 100.0,
                        float(cfg.MAX_DRAWDOWN) * 100.0,
                    )

    return _guarded_update


def _install_drawdown_advisory(TradingEngine_or_log, log=None) -> None:
    """Install drawdown hard-gate with an explicit, auditable operator override.

    Backward compatible with the previous private helper signature
    ``_install_drawdown_advisory(log)`` used by regression tests. The engine
    wrappers are installed only when a TradingEngine class is explicitly passed.
    """
    if log is None:
        TradingEngine = None
        log = TradingEngine_or_log
    else:
        TradingEngine = TradingEngine_or_log

    from bot.risk import RiskManager
    from bot.risk_manager_v3 import RiskManagerV3

    if not getattr(RiskManager, "_operator_drawdown_advisory", False):
        def _legacy_can_open(self, n: int) -> bool:
            if not self._ready:
                log.warning(
                    "⛔ RiskManager não inicializado (saldo lido: $%.2f) — scan bloqueado",
                    float(self.balance),
                )
                return False
            if not self.balance_confirmed or self.balance <= 0:
                log.warning("[BALANCE] new entries blocked: zero or unconfirmed balance")
                return False
            log.info(
                "[ENTRY_RISK_STATE] drawdown=%.2f%% configured_limit=%.2f%% entry_authorization=%s override=%s",
                float(self.drawdown) * 100.0,
                float(cfg.MAX_DRAWDOWN) * 100.0,
                "BLOCK_DRAWDOWN" if (self.drawdown >= cfg.MAX_DRAWDOWN and not _risk_override_enabled()) else "ALLOW",
                _risk_override_enabled(),
            )
            if self.drawdown >= cfg.MAX_DRAWDOWN:
                if not _risk_override_enabled():
                    from bot.drawdown_recovery import threshold_decision
                    recovery_allowed, recovery_reason, recovery = threshold_decision(self.drawdown)
                    if not (recovery_allowed and recovery_reason == "recovery_threshold_exception"):
                        log.error(
                            "[DRAWDOWN_HARD_GATE] drawdown=%.2f%% configured_limit=%.2f%% "
                            "override=false entries_blocked=true recovery_reason=%s",
                            float(self.drawdown) * 100.0,
                            float(cfg.MAX_DRAWDOWN) * 100.0,
                            recovery_reason,
                        )
                        return False
                    log.critical(
                        "[DRAWDOWN_RECOVERY_SCAN] episode=%s drawdown=%.2f%% "
                        "threshold_exception=true final_authority=predispatch",
                        recovery.episode_id,
                        float(self.drawdown) * 100.0,
                    )
                else:
                    log.critical(
                    "[DRAWDOWN_OVERRIDE] drawdown=%.2f%% configured_limit=%.2f%% "
                    "override=true entries_blocked=false",
                    float(self.drawdown) * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                )
            if n >= cfg.MAX_POSITIONS:
                log.info("⛔ %s/%s posições → aguardando", n, cfg.MAX_POSITIONS)
                return False
            return True

        RiskManager.can_open = _legacy_can_open
        RiskManager._operator_drawdown_advisory = True

    if not getattr(RiskManagerV3, "_operator_drawdown_advisory", False):
        def _v3_can_open(self, open_positions: int) -> bool:
            if not self.confirmed:
                return False
            if self.equity <= 0 or self.available_collateral <= 0:
                return False
            if self.drawdown >= cfg.MAX_DRAWDOWN:
                if not _risk_override_enabled():
                    from bot.drawdown_recovery import threshold_decision
                    recovery_allowed, recovery_reason, recovery = threshold_decision(self.drawdown)
                    if not (recovery_allowed and recovery_reason == "recovery_threshold_exception"):
                        log.error(
                            "[DRAWDOWN_HARD_GATE_V3] drawdown=%.2f%% configured_limit=%.2f%% "
                            "override=false entries_blocked=true recovery_reason=%s",
                            float(self.drawdown) * 100.0,
                            float(cfg.MAX_DRAWDOWN) * 100.0,
                            recovery_reason,
                        )
                        return False
                    log.critical(
                        "[DRAWDOWN_RECOVERY_SCAN_V3] episode=%s drawdown=%.2f%% "
                        "threshold_exception=true final_authority=predispatch",
                        recovery.episode_id,
                        float(self.drawdown) * 100.0,
                    )
                else:
                    log.critical(
                    "[DRAWDOWN_OVERRIDE_V3] drawdown=%.2f%% configured_limit=%.2f%% "
                    "override=true entries_blocked=false",
                    float(self.drawdown) * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                )
            if open_positions >= cfg.MAX_POSITIONS:
                return False
            return True

        RiskManagerV3.can_open = _v3_can_open
        RiskManagerV3._operator_drawdown_advisory = True

    if TradingEngine is None:
        return

    if not TradingEngine.__dict__.get("_operator_drawdown_engine_advisory", False):
        previous_update_balance = TradingEngine._update_balance

        async def _update_balance_advisory(self, *args, **kwargs):
            protected = _protect_drawdown_update(
                self,
                lambda *a, **k: previous_update_balance(self, *a, **k),
                log,
                source="ENGINE",
            )
            return await protected(*args, **kwargs)

        TradingEngine._update_balance = _update_balance_advisory
        TradingEngine._operator_drawdown_engine_advisory = True

    if (hasattr(TradingEngine, "run") and
            not TradingEngine.__dict__.get("_operator_drawdown_run_binding", False)):
        previous_run = TradingEngine.run

        async def _run_with_instance_drawdown_advisory(self, *args, **kwargs):
            if not getattr(self, "_operator_drawdown_instance_advisory", False):
                current_bound_update = self._update_balance
                self._update_balance = _protect_drawdown_update(
                    self,
                    current_bound_update,
                    log,
                    source="INSTANCE",
                )
                self._operator_drawdown_instance_advisory = True
                log.critical(
                    "[DRAWDOWN_POLICY_INSTANCE] installed=true class=%s "
                    "bound_update_module=%s bound_update_name=%s "
                    "default=hard_gate explicit_override_supported=true",
                    type(self).__name__,
                    getattr(current_bound_update, "__module__", "unknown"),
                    getattr(current_bound_update, "__name__", type(current_bound_update).__name__),
                )
            watcher = startup_ready_notification.start(self, log)
            # Observability-only Telegram panel; shares this reviewed run
            # owner's lifecycle and never touches execution state.
            radar = market_radar.start(self, log)
            try:
                return await previous_run(self, *args, **kwargs)
            finally:
                await market_radar.cancel(radar)
                await startup_ready_notification.cancel(watcher)

        TradingEngine.run = _run_with_instance_drawdown_advisory
        TradingEngine._operator_drawdown_run_binding = True


def _install_margin_sizing(log) -> None:
    """Apply the operator allocation to fresh LIVE available collateral."""
    from bot import engine as engine_module
    from bot import pilot_live_runtime
    from bot import pilot_risk_cap_hardening as pilot_cap

    if getattr(engine_module, "_operator_margin_sizing_installed", False):
        return

    previous_minimum = engine_module.minimum_base_quantity

    def _margin_target_quantity(info, price):
        engine = pilot_cap._PILOT_ENGINE.get()
        symbol = pilot_cap._PILOT_SYMBOL.get()
        if engine is None or not symbol:
            return previous_minimum(info, price)
        if getattr(engine, "paper_trade", False) or not bool(
            getattr(getattr(engine, "pilot", None), "enabled", False)
        ):
            return previous_minimum(info, price)

        available = float(getattr(engine, "_pilot_available_balance", 0.0) or 0.0)
        leverage = float(cfg.LEVERAGE)
        price_f = float(price)
        if (
            not math.isfinite(available) or available <= 0
            or not math.isfinite(leverage) or leverage <= 0
            or not math.isfinite(price_f) or price_f <= 0
        ):
            log.critical(
                "[PILOT_MARGIN_SIZING] symbol=%s result=BLOCK reason=invalid_context "
                "available=%s leverage=%s price=%s",
                symbol, available, leverage, price_f,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        from bot.final_sizing_invariants import operator_margin_fraction, operator_target_margin
        try:
            margin_fraction = operator_margin_fraction()
        except ValueError:
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            log.critical(
                "[PILOT_MARGIN_SIZING] symbol=%s result=BLOCK reason=invalid_margin_fraction",
                symbol,
            )
            return 0.0
        target_margin = operator_target_margin(available, leverage, margin_fraction)
        target_notional = target_margin * leverage
        try:
            target_qty = float(
                pilot_live_runtime._pilot_quantity_for_notional(
                    info, price_f, target_notional
                )
            )
        except Exception as exc:
            log.critical(
                "[PILOT_MARGIN_SIZING] symbol=%s result=BLOCK reason=quantity_%s",
                symbol, type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        risk_qty = 0.0
        try:
            risk_qty = float(
                engine.risk.size(
                    symbol,
                    price_f,
                    engine.instruments,
                    open_positions=engine.positions,
                )
            )
        except Exception as exc:
            log.warning(
                "[PILOT_MARGIN_SIZING] symbol=%s stop_risk_telemetry=%s",
                symbol, type(exc).__name__,
            )

        if not math.isfinite(target_qty) or target_qty <= 0:
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        actual_notional = target_qty * price_f
        actual_margin = actual_notional / leverage
        pilot_cap._PILOT_FINAL_QTY.set(target_qty)
        log.warning(
            "[PILOT_MARGIN_SIZING] symbol=%s result=PASS available=%.6f "
            "margin_pct=%.2f%% target_margin=%.6f leverage=%.0fx "
            "target_notional=%.6f qty=%.12g actual_margin=%.6f "
            "stop_risk_qty_advisory=%.12g authority=operator_margin_policy "
            "superseded_by=final_sizing_invariants",
            symbol,
            available,
            margin_fraction * 100.0,
            target_margin,
            leverage,
            target_notional,
            target_qty,
            actual_margin,
            risk_qty,
        )
        return target_qty

    engine_module.minimum_base_quantity = _margin_target_quantity
    engine_module._operator_margin_sizing_installed = True


def install(TradingEngine, log) -> None:
    """Install after all controlled-pilot sizing/risk wrappers."""
    _install_drawdown_advisory(TradingEngine, log)
    _install_margin_sizing(log)
    from bot.final_sizing_invariants import operator_margin_fraction
    log.critical(
        "[OPERATOR_RUNTIME_POLICY] installed margin_cap=%.2fpct_available "
        "sizing_authority=final_sizing_invariants "
        "leverage=%sx drawdown_default=hard_gate explicit_override_supported=true "
        "override_enabled=%s",
        operator_margin_fraction() * 100.0,
        cfg.LEVERAGE,
        _risk_override_enabled(),
    )
