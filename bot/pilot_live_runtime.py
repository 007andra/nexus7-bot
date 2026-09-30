"""Runtime hardening used only after explicit controlled-pilot release.

The release gate is evaluated elsewhere. This module does not itself authorize
LIVE execution. When installed, it keeps account-equity drawdown durable and
cash-flow aware, tracks free collateral separately, performs authenticated
read-only exposure and private-WS preflight, and refreshes those checks
immediately before each candidate reaches the normal _open pipeline.

Sizing truth: this module publishes the fresh authenticated available balance
(``_pilot_available_balance``) that the final sizing authority consumes. Its own
``PILOT_NOTIONAL_PCT`` position-notional hook is installed first and is
shadowed in a LIVE pilot context by ``final_sizing_invariants``, whose contract
is ``final_qty = min(stop_risk_qty, operator_margin_cap_qty)`` with
RiskManagerV3 as risk authority. The ``[PILOT_LEGACY_TARGET]`` lines below are
diagnostic only and say so explicitly. All existing AI, RR/EV, drawdown,
liquidation, affordability, exposure, durable-state and release gates still run
and may block an entry.

External/manual positions remain governed by pilot_external_position_guard and
pilot_exposure_capacity; this module never adopts, amends, reduces or closes
one.
"""
from __future__ import annotations

import contextvars
import os
import time
from decimal import Decimal, ROUND_FLOOR

from bot import account_balance_semantics as account_semantics
from bot import capital_flow_reconciliation as capital_flows
from bot import external_position_performance as external_performance
from bot import binance_hwm_incident_repair as hwm_incident_repair
from bot.drawdown_persistence import (
    restore_real_account_peak_without_new_high,
    restore_update_real_account_peak,
    restore_zero_equity_peak_fail_closed,
)
from bot.quantity import quantity_rules


_PILOT_TARGET_NOTIONAL = contextvars.ContextVar(
    "nexus_pilot_target_notional", default=None
)
_PILOT_NOTIONAL_PCT = float(os.environ.get("PILOT_NOTIONAL_PCT", "0.50"))


def _pilot_quantity_for_notional(info: dict, price: float, target_notional: float) -> float:
    """Largest valid base quantity that does not exceed allocated notional."""
    if isinstance(target_notional, bool):
        raise ValueError("target_notional must be numeric")
    price_d = Decimal(str(price))
    target_d = Decimal(str(target_notional))
    if not price_d.is_finite() or price_d <= 0:
        raise ValueError("invalid price")
    if not target_d.is_finite() or target_d <= 0:
        raise ValueError("invalid target_notional")

    multiplier, lot, minimum, min_notional = quantity_rules(info)
    contracts = target_d / (price_d * multiplier)
    contracts = (contracts / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
    if contracts < minimum or contracts * multiplier * price_d < min_notional:
        return 0.0
    return float(contracts * multiplier)


def _install_pilot_notional_sizing(log) -> None:
    """Patch only the LIVE-pilot minimum-lot hook used by core engine._open.

    The core engine deliberately calls ``minimum_base_quantity`` while pilot is
    enabled. Rather than weakening PilotGuard, replace that narrow hook with a
    context-scoped target. Outside a LIVE pilot candidate the original function
    is returned unchanged.
    """
    from bot import engine as engine_module

    if getattr(engine_module, "_pilot_notional_sizing_installed", False):
        return

    original_minimum = engine_module.minimum_base_quantity

    def _pilot_aware_minimum(info, price):
        target = _PILOT_TARGET_NOTIONAL.get()
        if target is None:
            return original_minimum(info, price)
        return _pilot_quantity_for_notional(info, price, target)

    engine_module.minimum_base_quantity = _pilot_aware_minimum
    engine_module._pilot_notional_sizing_installed = True
    log.critical(
        "[PILOT_LEGACY_TARGET] installed legacy_notional_pct=%.2f%% basis=available_balance "
        "meaning=position_notional_not_margin superseded_by=final_sizing_invariants "
        "execution_effect=NONE",
        _PILOT_NOTIONAL_PCT * 100.0,
    )


async def _refresh_account(engine, log, *, for_entry: bool = False) -> dict:
    state = await account_semantics.read_account_state(engine.client)
    equity = float(state["equity"])
    available = float(state["available"])

    account_semantics.update_risk_from_equity(engine.risk, equity)

    if equity == 0.0:
        # Zero is an authenticated observable account state, not executable
        # capital. Preserve the durable HWM at 100% drawdown so the runtime can
        # continue read-only reconciliation/forensics without ever opening risk.
        await restore_zero_equity_peak_fail_closed(engine.risk, strict=True)
        engine._pilot_prev_account_equity = 0.0
        engine._pilot_prev_account_observed_ms = external_performance.server_now_ms(
            engine.client
        )
        engine._pilot_account_equity = 0.0
        engine._pilot_available_balance = 0.0
        engine._pilot_balance_source = state.get("available_source", "unknown")
        snapshot = dict(state)
        snapshot["_observed_at"] = time.time()
        snapshot["_zero_equity_fail_closed"] = True
        engine.client._last_account_overview_snapshot = snapshot
        log.critical(
            "[PILOT_LIVE_BALANCE] equity=0.0000 observed_available=%.4f "
            "published_available=0.0000 peak_equity=%.4f drawdown=100.00%% "
            "zero_equity=true capital_confirmed=false "
            "execution_effect=BLOCK_NEW_ENTRIES",
            available,
            float(getattr(getattr(engine.risk, "_legacy", engine.risk), "peak_balance", 0.0) or 0.0),
        )
        return state

    # KuCoin accountEquity includes external deposits/withdrawals/transfers.
    # Before enforcing the durable HWM, reconcile completed ledger cash flows
    # so a user moving capital cannot be misclassified as trading PnL.
    now = time.time()
    previous_equity = getattr(engine, "_pilot_prev_account_equity", None)
    last_flow_check = float(getattr(engine, "_pilot_last_capital_flow_check", 0.0) or 0.0)
    material_change = (
        previous_equity is None
        or abs(equity - float(previous_equity)) >= max(0.02, abs(float(previous_equity)) * 0.01)
    )
    if material_change or (now - last_flow_check) >= 300.0:
        await capital_flows.reconcile_external_capital_flows(
            engine.client,
            engine.risk,
            equity,
            strict=True,
        )
        engine._pilot_last_capital_flow_check = now

    await hwm_incident_repair.repair_if_needed(
        engine.risk, equity, strict=True
    )

    performance_mode = await external_performance.evaluate(engine, state, log=log)
    if performance_mode in {"FREEZE", "QUARANTINE"}:
        await restore_real_account_peak_without_new_high(
            engine.risk, equity, strict=True
        )
    else:
        await restore_update_real_account_peak(engine.risk, equity, strict=True)

    engine._pilot_prev_account_equity = equity
    engine._pilot_prev_account_observed_ms = external_performance.server_now_ms(
        engine.client
    )

    engine._pilot_account_equity = equity
    engine._pilot_available_balance = available
    engine._pilot_balance_source = state.get("available_source", "unknown")

    # Pilot exposure-capacity evaluation consumes this normalized, timestamped
    # snapshot. Keep it synchronized with the same read used for sizing.
    snapshot = dict(state)
    snapshot["_observed_at"] = time.time()
    engine.client._last_account_overview_snapshot = snapshot

    legacy = getattr(engine.risk, "_legacy", engine.risk)
    log.info(
        "[PILOT_LIVE_BALANCE] equity=%.4f available=%.4f peak_equity=%.4f "
        "drawdown=%.2f%% collateral_basis=%s entry_refresh=%s",
        equity,
        available,
        float(getattr(legacy, "peak_balance", 0.0) or 0.0),
        float(getattr(legacy, "drawdown", 0.0) or 0.0) * 100.0,
        state.get("available_source", "unknown"),
        str(bool(for_entry)).lower(),
    )
    return state


async def _entry_drawdown_allows(engine, log) -> bool:
    """Hard drawdown gate on the equity read that was just refreshed.

    ``_refresh_entry_balance`` is called again immediately before the order
    registry/durable intent/dispatch. Earlier drawdown gates (scan ``can_open``
    and RiskManagerV3 ``can_open`` in the sizing plan) ran on older reads, so a
    candidate approved before equity deteriorated must be re-checked here.
    Threshold and the explicit ``LIVE_RISK_OVERRIDE_APPROVED`` semantics are the
    same as every other drawdown gate. An unresolved external-performance
    attribution quarantine is not a threshold override case and remains
    fail-closed. Unreadable drawdown also fails closed.
    """
    from bot.config import cfg
    from bot.operator_runtime_policy import _risk_override_enabled

    if bool(getattr(engine, "_external_performance_quarantine", False)):
        log.critical(
            "[PILOT_PREDISPATCH_DRAWDOWN] result=BLOCK "
            "reason=external_performance_quarantine "
            "override_not_applicable=true execution_effect=BLOCK_NEW_ENTRY"
        )
        return False

    risk = getattr(engine, "risk", None)
    legacy = getattr(risk, "_legacy", risk)
    candidates = [getattr(legacy, "drawdown", None)]
    v3 = getattr(risk, "_v3", None)
    if v3 is not None and getattr(v3, "confirmed", False):
        candidates.append(getattr(v3, "drawdown", None))
    try:
        values = [float(v) for v in candidates if v is not None]
    except (TypeError, ValueError):
        values = []
    limit = float(cfg.MAX_DRAWDOWN)
    if not values or any(v != v or v < 0 or v == float("inf") for v in values):
        log.critical(
            "[PILOT_PREDISPATCH_DRAWDOWN] result=BLOCK reason=drawdown_unreadable "
            "execution_effect=BLOCK_NEW_ENTRY"
        )
        return False
    drawdown = max(values)
    if drawdown < limit:
        return True
    if _risk_override_enabled():
        log.critical(
            "[PILOT_PREDISPATCH_DRAWDOWN] result=OVERRIDE drawdown=%.4f%% limit=%.4f%% "
            "override=true execution_effect=ALLOW_NEW_ENTRY",
            drawdown * 100.0, limit * 100.0,
        )
        return True

    # Recovery is a narrow drawdown-threshold exception only. It cannot
    # supersede external-performance quarantine (checked above) or any of the
    # independent preflight/integrity/ownership/fencing/private-stream/
    # durable/CROSS/market gates surrounding this function.
    from bot.drawdown_recovery import threshold_decision
    recovery_allowed, recovery_reason, recovery = threshold_decision(drawdown)
    if recovery_allowed and recovery_reason == "recovery_threshold_exception":
        # Recovery is only valid from a flat engine state. This also enforces
        # the one-position recovery limit: any existing position fails closed.
        if bool(getattr(engine, "positions", {})):
            log.error(
                "[PILOT_PREDISPATCH_RECOVERY] result=BLOCK reason=account_not_flat "
                "episode=%s execution_effect=BLOCK_NEW_ENTRY",
                recovery.episode_id,
            )
            return False
        from bot.drawdown_recovery import ensure_durable_episode
        durable_ok, durable_reason, _receipt = await ensure_durable_episode(
            engine,
            drawdown,
            recovery,
            strict=True,
        )
        if not durable_ok:
            log.error(
                "[PILOT_PREDISPATCH_RECOVERY] result=BLOCK reason=%s episode=%s "
                "execution_effect=BLOCK_NEW_ENTRY",
                durable_reason,
                recovery.episode_id,
            )
            return False
        log.critical(
            "[PILOT_PREDISPATCH_RECOVERY] result=PASS episode=%s drawdown=%.4f%% "
            "normal_limit=%.4f%% recovery_ceiling=%.4f%% recovery_risk_pct=%.4f%% "
            "durable_receipt=%s scope=drawdown_threshold_only other_gates_unchanged=true",
            recovery.episode_id,
            drawdown * 100.0,
            limit * 100.0,
            float(recovery.max_drawdown) * 100.0,
            float(recovery.risk_pct) * 100.0,
            durable_reason,
        )
        return True

    log.error(
        "[PILOT_PREDISPATCH_DRAWDOWN] result=BLOCK drawdown=%.4f%% limit=%.4f%% "
        "override=false recovery_reason=%s source=fresh_authenticated_equity "
        "execution_effect=BLOCK_NEW_ENTRY",
        drawdown * 100.0, limit * 100.0, recovery_reason,
    )
    return False


async def _private_stream_ready(engine, log) -> tuple[bool, str]:
    """Live private-stream truth for new exposure (Binance user-data stream).

    After any (re)connect/disconnect/handler error the stream may have missed
    events, so it is trusted again only after an authenticated REST
    reconciliation pass completed for the current connection: the exposure
    read (positions + open orders) in this same preflight AND the durable
    order reconciliation by clientOrderId. WS absence never implies that an
    order or fill does not exist. Clients without the authority (KuCoin/test
    doubles) keep the historical behaviour.
    """
    from bot.private_stream_health import PrivateStreamHealth

    health = getattr(engine.client, "private_stream_health", None)
    if not isinstance(health, PrivateStreamHealth):
        return True, "not_applicable"
    snap = health.snapshot()
    if snap["reconcile_required"] and snap["state"] == "CONNECTED":
        epoch = snap["connection_epoch"]
        exposure_verified = bool(
            getattr(engine.client, "_prelive_account_exposure_verified", False)
        )
        orders_converged = False
        try:
            from bot.durable_live_reconciliation import reconcile_pending

            orders_converged = bool(await reconcile_pending(engine, min_interval_s=0.0))
        except Exception as exc:
            log.warning(
                "[PRIVATE_STREAM] event=reconcile_failed error=%s action=BLOCK_NEW_ENTRIES",
                type(exc).__name__,
            )
        if exposure_verified and orders_converged and health.mark_reconciled(epoch):
            log.warning(
                "[PRIVATE_STREAM] event=reconciled epoch=%d source=REST "
                "exposure_verified=true orders_converged=true",
                epoch,
            )
    ok, reason = health.check()
    return ok, reason


async def _run_readonly_preflight(engine, log, *, probe_private_ws: bool) -> bool:
    from bot import private_ws_readonly_observability as prelive

    if probe_private_ws:
        ready = bool(await prelive.run(engine.client, engine.instruments, log))
    else:
        exposure_clear = bool(await prelive.refresh_account_exposure(engine.client, log))
        private_ws_ok = bool(getattr(engine.client, "_prelive_private_ws_probe_ok", False))
        ready = bool(exposure_clear and private_ws_ok)

    stream_ok, stream_reason = await _private_stream_ready(engine, log)
    ready = bool(ready and stream_ok)
    engine._pilot_live_prelive_ready = ready
    log.warning(
        "[PILOT_LIVE_PREFLIGHT] result=%s exposure_verified=%s exposure_clear=%s "
        "private_ws=%s private_stream=%s",
        "PASS" if ready else "BLOCKED",
        getattr(engine.client, "_prelive_account_exposure_verified", False),
        getattr(engine.client, "_prelive_account_exposure_clear", False),
        getattr(engine.client, "_prelive_private_ws_probe_ok", False),
        stream_reason,
    )
    return ready


def install(TradingEngine, log) -> None:
    """Install LIVE-pilot-only wrappers. Caller must enforce release auth."""
    if getattr(TradingEngine, "_pilot_live_runtime_patched", False):
        return

    if not (0.0 < _PILOT_NOTIONAL_PCT <= 1.0):
        raise RuntimeError("PILOT_NOTIONAL_PCT must be >0 and <=1")

    _install_pilot_notional_sizing(log)

    original_connect = TradingEngine._connect
    original_update_balance = TradingEngine._update_balance
    original_refresh_entry_balance = TradingEngine._refresh_entry_balance
    original_open = TradingEngine._open

    async def _connect_live_pilot(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_connect(self, *args, **kwargs)

        # Establish canonical accountEquity before the legacy connect routine
        # performs its first RiskManager init/update. Available collateral is
        # recorded separately and must never occupy the equity slot.
        try:
            startup_state = await account_semantics.read_account_state(self.client)
            self._pilot_startup_account_equity = float(startup_state["equity"])
            self._pilot_account_equity = float(startup_state["equity"])
            self._pilot_available_balance = float(startup_state["available"])
            self._pilot_balance_source = startup_state.get("available_source", "unknown")
        except Exception as exc:
            self._pilot_live_prelive_ready = False
            self.risk.balance_confirmed = False
            self.connected = False
            log.critical(
                "[PILOT_LIVE_STARTUP] result=BLOCKED reason=%s action=no_connect",
                type(exc).__name__,
            )
            return None

        result = await original_connect(self, *args, **kwargs)
        if not getattr(self, "connected", False):
            self._pilot_live_prelive_ready = False
            return result

        try:
            await _refresh_account(self, log)
            await _run_readonly_preflight(self, log, probe_private_ws=True)
        except Exception as exc:
            self._pilot_live_prelive_ready = False
            self.risk.balance_confirmed = False
            log.critical(
                "[PILOT_LIVE_PREFLIGHT] result=BLOCKED reason=%s action=no_new_entry",
                type(exc).__name__,
            )
        return result

    async def _update_balance_live_pilot(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_update_balance(self, *args, **kwargs)
        try:
            state = await _refresh_account(self, log)
            self.risk.balance_confirmed = bool(float(state["equity"]) > 0.0)
            return state
        except Exception as exc:
            self.risk.balance_confirmed = False
            log.critical(
                "[PILOT_LIVE_BALANCE] result=BLOCKED reason=%s action=no_new_entry",
                type(exc).__name__,
            )
            return None

    async def _refresh_entry_balance_live_pilot(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_refresh_entry_balance(self, *args, **kwargs)
        try:
            state = await _refresh_account(self, log, for_entry=True)
            available = float(state["available"])
            self.risk.balance_confirmed = bool(float(state["equity"]) > 0.0)

            # Keep risk.balance semantically stable as authenticated account
            # equity. The core engine reads _pilot_available_balance explicitly
            # for pilot affordability checks, so free collateral is never
            # published through the equity field.
            if available <= 0:
                log.warning("[PILOT_LIVE_BALANCE] entry blocked: available collateral <= 0")
                return False
            return await _entry_drawdown_allows(self, log)
        except Exception as exc:
            self.risk.balance_confirmed = False
            log.critical(
                "[PILOT_LIVE_BALANCE] entry blocked reason=%s",
                type(exc).__name__,
            )
            return False

    async def _open_live_pilot(self, sig, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_open(self, sig, *args, **kwargs)

        # Re-read account exposure immediately before entering the normal
        # AI/risk/PilotGuard pipeline. This is read-only and fails closed.
        try:
            if not await _run_readonly_preflight(self, log, probe_private_ws=False):
                log.warning(
                    "[PILOT_LIVE_GATE] symbol=%s stage=PRELIVE result=BLOCK",
                    getattr(sig, "symbol", "?"),
                )
                return None
            await self.integrity.assess(self.client, self)
            if not self.integrity.can_open_new():
                log.warning(
                    "[PILOT_LIVE_GATE] symbol=%s stage=INTEGRITY result=BLOCK reason=%s",
                    getattr(sig, "symbol", "?"),
                    self.integrity.block_reason(),
                )
                return None

            # Fresh authenticated balance for the position-notional target.
            # This is intentionally not inferred from stale dashboard state.
            sizing_state = await _refresh_account(self, log, for_entry=True)
            available = float(sizing_state["available"])
            if available <= 0:
                log.warning(
                    "[PILOT_SIZING] symbol=%s result=BLOCK reason=available_balance_nonpositive",
                    getattr(sig, "symbol", "?"),
                )
                return None
            target_notional = available * _PILOT_NOTIONAL_PCT
            log.warning(
                "[PILOT_LEGACY_TARGET] diagnostic_only=true superseded_by=final_sizing_invariants "
                "symbol=%s available=%.4f legacy_pct=%.2f%% "
                "legacy_notional=%.4f leverage=%sx legacy_margin_approx=%.4f execution_effect=NONE",
                getattr(sig, "symbol", "?"),
                available,
                _PILOT_NOTIONAL_PCT * 100.0,
                target_notional,
                int(getattr(__import__("bot.config", fromlist=["cfg"]).cfg, "LEVERAGE", 1)),
                target_notional / max(1, int(getattr(__import__("bot.config", fromlist=["cfg"]).cfg, "LEVERAGE", 1))),
            )
        except Exception as exc:
            log.critical(
                "[PILOT_LIVE_GATE] symbol=%s stage=PRELIVE result=BLOCK reason=%s",
                getattr(sig, "symbol", "?"),
                type(exc).__name__,
            )
            return None

        self._pilot_open_in_progress = True
        token = _PILOT_TARGET_NOTIONAL.set(target_notional)
        try:
            return await original_open(self, sig, *args, **kwargs)
        finally:
            _PILOT_TARGET_NOTIONAL.reset(token)
            self._pilot_open_in_progress = False
            # Restore risk.balance to the account-equity basis even if the
            # candidate is rejected or dispatch raises. Failure keeps future
            # entries fail-closed through balance_confirmed.
            try:
                await _refresh_account(self, log)
                self.risk.balance_confirmed = True
            except Exception as exc:
                self.risk.balance_confirmed = False
                log.critical(
                    "[PILOT_LIVE_BALANCE] post-candidate restore failed reason=%s",
                    type(exc).__name__,
                )

    TradingEngine._connect = _connect_live_pilot
    TradingEngine._update_balance = _update_balance_live_pilot
    TradingEngine._refresh_entry_balance = _refresh_entry_balance_live_pilot
    TradingEngine._open = _open_live_pilot
    TradingEngine._pilot_live_runtime_patched = True

    log.critical(
        "[PILOT_LIVE_RUNTIME] installed: cash-flow-aware durable equity drawdown + "
        "fresh available-collateral publication for final sizing "
        "(sizing_authority=final_sizing_invariants) + pre-dispatch drawdown hard gate + "
        "read-only exposure/private-WS preflight + external-performance HWM quarantine; "
        "external positions immutable"
    )
