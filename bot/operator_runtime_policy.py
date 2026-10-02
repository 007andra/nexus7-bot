"""Operator LIVE risk-override policy (drawdown gate).

Sizing is NOT owned here: since F-003 the LIVE pilot sizes from the stop-loss
risk budget in ``final_sizing_invariants`` (RiskManagerV3); leverage is read
from configuration and only determines required margin.

A drawdown breach is fail-closed by default. It may be bypassed only when the
operator explicitly enables LIVE_RISK_OVERRIDE_APPROVED=true. The override
never bypasses balance, position-count, protection, duplicate-order,
reconciliation, instrument or other execution-safety gates.
"""
from __future__ import annotations

import os

from bot.config import cfg
from bot import startup_ready_notification


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
                    log.error(
                        "[DRAWDOWN_HARD_GATE] drawdown=%.2f%% configured_limit=%.2f%% "
                        "override=false entries_blocked=true",
                        float(self.drawdown) * 100.0,
                        float(cfg.MAX_DRAWDOWN) * 100.0,
                    )
                    return False
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
                    log.error(
                        "[DRAWDOWN_HARD_GATE_V3] drawdown=%.2f%% configured_limit=%.2f%% "
                        "override=false entries_blocked=true",
                        float(self.drawdown) * 100.0,
                        float(cfg.MAX_DRAWDOWN) * 100.0,
                    )
                    return False
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
            try:
                return await previous_run(self, *args, **kwargs)
            finally:
                await startup_ready_notification.cancel(watcher)

        TradingEngine.run = _run_with_instance_drawdown_advisory
        TradingEngine._operator_drawdown_run_binding = True


def install(TradingEngine, log) -> None:
    """Install after all controlled-pilot sizing/risk wrappers."""
    _install_drawdown_advisory(TradingEngine, log)
    log.critical(
        "[OPERATOR_RUNTIME_POLICY] installed sizing_authority=RISK_BUDGET_V3 "
        "leverage=%sx drawdown_default=hard_gate explicit_override_supported=true "
        "override_enabled=%s railway_variables_unchanged=true",
        cfg.LEVERAGE,
        _risk_override_enabled(),
    )
