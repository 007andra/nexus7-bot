"""Risk-authoritative sizing and final execution-quality guard for LIVE pilot.

The operator target remains 50% of authenticated available USDT as POSITION
NOTIONAL, as implemented by ``pilot_live_runtime``. This guard makes that target
a cap/target rather than an override of stop-risk sizing:

    final_qty = min(stop_risk_qty, pilot_target_qty)

The exact final quantity is then carried to the engine's existing
``_refresh_entry_balance`` call. The engine invokes that refresh both before
sizing and again immediately before order-registry, durable-intent,
pilot-reservation and exchange dispatch. The LIVE spread/depth/signal-drift
guard therefore runs only after the final quantity has actually been computed;
the earlier balance refresh is balance-only. Any invalid final quantity or bad
execution-quality data still fails closed before ``place_order`` can be reached.

This module does not authorize LIVE mode, change leverage, modify Railway
variables, weaken PilotGuard, or submit orders by itself. Reduce-only exits and
emergency protection actions are not blocked by this new-entry guard.
"""
from __future__ import annotations

import contextvars
import math

from bot.pre_dispatch_guard import live_microstructure_recheck


_PILOT_ENGINE = contextvars.ContextVar("nexus_pilot_risk_engine", default=None)
_PILOT_SYMBOL = contextvars.ContextVar("nexus_pilot_risk_symbol", default=None)
_PILOT_SIGNAL = contextvars.ContextVar("nexus_pilot_risk_signal", default=None)
_PILOT_FINAL_QTY = contextvars.ContextVar("nexus_pilot_final_qty", default=None)


def _select_final_quantity(*, target_qty: float, risk_qty: float) -> float:
    """Return the smaller positive finite quantity, otherwise fail closed."""
    values = (float(target_qty), float(risk_qty))
    if any((not math.isfinite(v) or v <= 0) for v in values):
        return 0.0
    return min(values)


def install(TradingEngine, log) -> None:
    """Install after ``pilot_live_runtime`` so its 50% target remains intact."""
    if getattr(TradingEngine, "_pilot_risk_cap_hardening_installed", False):
        return

    from bot import engine as engine_module

    original_open = TradingEngine._open
    original_refresh_entry_balance = TradingEngine._refresh_entry_balance
    original_minimum = engine_module.minimum_base_quantity

    async def _open_with_pilot_risk_context(self, sig, *args, **kwargs):
        # PAPER and non-pilot execution keep their existing behavior.
        if getattr(self, "paper_trade", False) or not bool(
            getattr(getattr(self, "pilot", None), "enabled", False)
        ):
            return await original_open(self, sig, *args, **kwargs)

        token_engine = _PILOT_ENGINE.set(self)
        token_symbol = _PILOT_SYMBOL.set(getattr(sig, "symbol", None))
        token_signal = _PILOT_SIGNAL.set(sig)
        token_qty = _PILOT_FINAL_QTY.set(None)
        try:
            return await original_open(self, sig, *args, **kwargs)
        finally:
            _PILOT_FINAL_QTY.reset(token_qty)
            _PILOT_SIGNAL.reset(token_signal)
            _PILOT_SYMBOL.reset(token_symbol)
            _PILOT_ENGINE.reset(token_engine)

    def _risk_authoritative_pilot_quantity(info, price):
        engine = _PILOT_ENGINE.get()
        symbol = _PILOT_SYMBOL.get()
        if engine is None or not symbol:
            return original_minimum(info, price)

        # ``original_minimum`` is the pilot-aware hook installed immediately
        # before this guard. In the controlled pilot it yields the quantity for
        # the operator's 50%-of-available position-notional target.
        target_qty = float(original_minimum(info, price))

        try:
            risk_qty = float(
                engine.risk.size(
                    symbol,
                    float(price),
                    engine.instruments,
                    open_positions=engine.positions,
                )
            )
        except Exception as exc:
            log.critical(
                "[PILOT_RISK_CAP] symbol=%s result=BLOCK reason=risk_sizing_%s",
                symbol,
                type(exc).__name__,
            )
            _PILOT_FINAL_QTY.set(0.0)
            return 0.0

        final_qty = _select_final_quantity(
            target_qty=target_qty,
            risk_qty=risk_qty,
        )
        _PILOT_FINAL_QTY.set(final_qty)
        if final_qty <= 0:
            log.critical(
                "[PILOT_RISK_CAP] symbol=%s result=BLOCK target_qty=%.12g "
                "risk_qty=%.12g reason=nonpositive_or_invalid",
                symbol,
                target_qty,
                risk_qty,
            )
            return 0.0

        log.warning(
            "[PILOT_RISK_CAP] symbol=%s target_qty=%.12g risk_qty=%.12g "
            "final_qty=%.12g authority=RiskManagerV3 target_policy=legacy_50pct_available_notional "
            "superseded_by=final_sizing_invariants",
            symbol,
            target_qty,
            risk_qty,
            final_qty,
        )
        return final_qty

    async def _refresh_entry_balance_with_final_market_guard(self, *args, **kwargs):
        ok = await original_refresh_entry_balance(self, *args, **kwargs)
        if not ok:
            return ok

        sig = _PILOT_SIGNAL.get()
        symbol = _PILOT_SYMBOL.get()
        qty = _PILOT_FINAL_QTY.get()
        if (
            getattr(self, "paper_trade", False)
            or not bool(getattr(getattr(self, "pilot", None), "enabled", False))
            or sig is None
            or not symbol
        ):
            return ok

        # ``_refresh_entry_balance`` is intentionally called once before pilot
        # sizing and again immediately before dispatch. ``None`` means sizing has
        # not run yet, so this is the pre-sizing balance refresh, not a final
        # dispatch context. Do not turn that valid stage into a false veto.
        if qty is None:
            log.debug(
                "[LIVE_PREDISPATCH_MARKET] symbol=%s stage=PRE_SIZING "
                "result=SKIP reason=final_qty_not_computed execution_effect=NONE",
                symbol,
            )
            return ok

        try:
            qty_f = float(qty)
            entry = float(getattr(sig, "entry", 0.0) or 0.0)
            direction = str(getattr(sig, "direction", "")).upper()
        except (TypeError, ValueError):
            qty_f, entry, direction = 0.0, 0.0, ""

        # Once sizing has executed, invalid/zero quantity is a real final-stage
        # failure and remains fail-closed.
        if qty_f <= 0 or entry <= 0 or direction not in ("LONG", "SHORT"):
            log.critical(
                "[LIVE_PREDISPATCH_MARKET] symbol=%s result=BLOCK "
                "reason=invalid_final_dispatch_context qty=%s entry=%s direction=%s",
                symbol, qty_f, entry, direction,
            )
            return False

        result = await live_microstructure_recheck(
            self.client,
            instruments=self.instruments,
            symbol=symbol,
            signal_entry=entry,
            side="BUY" if direction == "LONG" else "SELL",
            qty=qty_f,
        )
        metrics = result.metrics or {}
        signed_drift = float(metrics.get("signed_signal_drift_bps", 0.0) or 0.0)
        drift_class = str(getattr(result, "drift_classification", "UNKNOWN") or "UNKNOWN")
        # A beyond-threshold favorable drift is not a free pass. Revalidate the
        # fixed protective geometry at the fresh executable price and ensure
        # the already-quantized quantity still fits the same operator margin
        # fraction enforced by final_sizing_invariants. Any missing or
        # inconsistent context remains fail-closed.
        if drift_class == "FAVORABLE_IMPROVEMENT" and abs(signed_drift) > float(
            __import__("bot.pre_dispatch_guard", fromlist=["limits_from_env"]).limits_from_env().max_signal_drift_bps
        ):
            executable = float(metrics.get("executable_price", 0.0) or 0.0)
            sl = float(getattr(sig, "sl", 0.0) or 0.0)
            tp = float(getattr(sig, "tp", 0.0) or 0.0)
            available = float(getattr(self, "_pilot_available_balance", 0.0) or 0.0)
            leverage = float(getattr(__import__("bot.config", fromlist=["cfg"]).cfg, "LEVERAGE", 0.0) or 0.0)
            geometry_ok = (
                executable > 0 and sl > 0 and tp > 0
                and ((direction == "LONG" and sl < executable < tp)
                     or (direction == "SHORT" and tp < executable < sl))
            )
            margin = (qty_f * executable / leverage) if leverage > 0 else float("inf")
            try:
                from bot.final_sizing_invariants import operator_margin_fraction
                margin_fraction = float(operator_margin_fraction())
            except (ImportError, TypeError, ValueError, ArithmeticError):
                margin_fraction = 0.0
            margin_ceiling = available * margin_fraction
            collateral_ok = (
                available > 0 and math.isfinite(margin)
                and margin > 0 and margin <= margin_ceiling + 1e-9
            )
            if not geometry_ok or not collateral_ok:
                log.warning(
                    "[LIVE_PREDISPATCH_MARKET] symbol=%s result=BLOCK "
                    "blockers=FAVORABLE_REVALIDATION_FAILED executable=%.8f sl=%.8f tp=%.8f "
                    "margin=%.8f margin_ceiling=%.8f geometry_ok=%s collateral_ok=%s "
                    "execution_effect=NONE",
                    symbol, executable, sl, tp, margin, margin_ceiling,
                    str(geometry_ok).lower(), str(collateral_ok).lower(),
                )
                return False
            log.info(
                "[LIVE_PREDISPATCH_FAVORABLE_REVALIDATION] symbol=%s result=PASS "
                "executable=%.8f sl=%.8f tp=%.8f margin=%.8f margin_ceiling=%.8f "
                "margin_fraction=%.6f geometry_improved=true collateral_within_target=true",
                symbol, executable, sl, tp, margin, margin_ceiling, margin_fraction,
            )

        if not result.allowed:
            log.warning(
                "[LIVE_PREDISPATCH_MARKET] symbol=%s result=BLOCK blockers=%s "
                "spread_bps=%.4f drift_bps=%.4f signed_drift_bps=%+.4f drift_class=%s "
                "depth_multiple=%.4f execution_effect=NONE",
                symbol,
                "+".join(result.blockers) or "unknown",
                float(metrics.get("spread_bps", 0.0) or 0.0),
                float(metrics.get("signal_drift_bps", 0.0) or 0.0),
                signed_drift,
                drift_class,
                float(metrics.get("depth_multiple", 0.0) or 0.0),
            )
            return False

        executable_price = metrics.get("executable_price")
        cost_fraction = float("nan")
        setup_id = str(getattr(sig, "_bgx_setup_id", "") or "UNKNOWN")
        from bot.final_loss_budget import diagnose, emit_telemetry
        from bot.execution_cost import stress_cost_fraction
        from bot.config import cfg

        try:
            cost_fraction, _cost_ref = stress_cost_fraction(sig, symbol)
            result_name, specific_reason, _loss_metrics = diagnose(
                qty_f,
                executable_price,
                getattr(sig, "sl", float("nan")),
                direction,
                cfg.LEVERAGE,
                cost_fraction,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic must not affect dispatch
            result_name = "UNAVAILABLE"
            specific_reason = f"diagnostic_{type(exc).__name__}"
        emit_telemetry(
            log, symbol=symbol, setup_id=setup_id,
            stage="FRESH_PREDISPATCH_RECHECK", qty=qty_f,
            entry=executable_price, stop=getattr(sig, "sl", float("nan")),
            direction=direction, leverage=cfg.LEVERAGE,
            cost_fraction=cost_fraction, result=result_name,
            specific_reason=specific_reason,
        )

        risk_recheck = getattr(
            getattr(self, "risk", None),
            "validate_fresh_executable_risk",
            None,
        )
        if not callable(risk_recheck):
            log.critical(
                "[FINAL_RISK_BUDGET_INVARIANT] symbol=%s setup_id=%s "
                "stage=FRESH_PREDISPATCH_RECHECK result=BLOCK "
                "reason=risk_authority_unavailable execution_effect=BLOCK_NEW_ENTRY",
                symbol, setup_id,
            )
            return False
        try:
            risk_allowed, risk_metrics = risk_recheck(
                symbol, float(executable_price), qty_f
            )
        except Exception as exc:
            log.critical(
                "[FINAL_RISK_BUDGET_INVARIANT] symbol=%s setup_id=%s "
                "stage=FRESH_PREDISPATCH_RECHECK result=BLOCK "
                "reason=check_%s execution_effect=BLOCK_NEW_ENTRY",
                symbol, setup_id, type(exc).__name__,
            )
            return False

        risk_budget = float(risk_metrics.get("risk_budget", float("nan")))
        projected_loss = float(risk_metrics.get("projected_loss", float("nan")))
        headroom = float(risk_metrics.get("headroom_usdt", float("nan")))
        if not risk_allowed:
            log.critical(
                "[FINAL_RISK_BUDGET_INVARIANT] symbol=%s setup_id=%s "
                "stage=FRESH_PREDISPATCH_RECHECK result=BLOCK "
                "projected_loss=%.12g risk_budget=%.12g headroom_usdt=%.12g "
                "risk_authority=RiskManagerV3 execution_effect=BLOCK_NEW_ENTRY",
                symbol, setup_id, projected_loss, risk_budget, headroom,
            )
            return False
        log.info(
            "[FINAL_RISK_BUDGET_INVARIANT] symbol=%s setup_id=%s "
            "stage=FRESH_PREDISPATCH_RECHECK result=PASS "
            "projected_loss=%.12g risk_budget=%.12g headroom_usdt=%.12g "
            "risk_authority=RiskManagerV3 execution_effect=NONE",
            symbol, setup_id, projected_loss, risk_budget, headroom,
        )

        log.info(
            "[LIVE_PREDISPATCH_MARKET] symbol=%s result=PASS "
            "spread_bps=%.4f drift_bps=%.4f signed_drift_bps=%+.4f drift_class=%s "
            "depth_multiple=%.4f qty=%.12g source=fresh_rest",
            symbol,
            float(metrics.get("spread_bps", 0.0) or 0.0),
            float(metrics.get("signal_drift_bps", 0.0) or 0.0),
            signed_drift,
            drift_class,
            float(metrics.get("depth_multiple", 0.0) or 0.0),
            qty_f,
        )
        return True

    TradingEngine._open = _open_with_pilot_risk_context
    TradingEngine._refresh_entry_balance = _refresh_entry_balance_with_final_market_guard
    engine_module.minimum_base_quantity = _risk_authoritative_pilot_quantity
    TradingEngine._pilot_risk_cap_hardening_installed = True

    log.critical(
        "[PILOT_RISK_CAP] installed: final sizing is owned by final_sizing_invariants "
        "(final_qty=min(stop_risk_qty,operator_margin_cap_qty), risk_authority=RiskManagerV3); "
        "LIVE spread/depth/signal-drift rechecked fail-closed only after final sizing; "
        "directional_drift_telemetry=true authorization_unchanged=true"
    )
