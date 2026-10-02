"""LIVE-pilot sizing context and final execution-quality guard.

Sizing authority is ``final_sizing_invariants`` (F-003 stop-loss risk budget,
RiskManagerV3). This module:

* carries the candidate context (engine/symbol/signal/final qty) and the F-003
  risk authorization for the duration of one ``_open`` call;
* stamps the opened position with the risk budget it reserved
  (INV-OPEN-RISK-001);
* re-runs the LIVE spread/depth/signal-drift guard and the F-003 loss-budget
  invariant at the fresh executable price immediately before dispatch.

The legacy ``minimum_base_quantity`` hook below is shadowed by the final
authority in the LIVE pilot; it returns ``min(previous, risk_qty)`` and can
therefore never exceed the risk-budget quantity.

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
    """Install the LIVE-pilot sizing context and final pre-dispatch guards."""
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
        from bot import risk_budget
        token_auth = risk_budget.authorize(None)
        try:
            result = await original_open(self, sig, *args, **kwargs)
            auth = risk_budget.current_authorization()
            position = (getattr(self, "positions", {}) or {}).get(getattr(sig, "symbol", None))
            if auth is not None and position is not None and \
                    getattr(position, "_risk_reserved_usdt", None) is None:
                # INV-OPEN-RISK-001: reserve the INITIAL budget for the trade's life.
                position._risk_reserved_usdt = float(auth.risk_budget)
                position._risk_initial_projected_usdt = float(auth.projected_loss)
            return result
        finally:
            risk_budget.reset_authorization(token_auth)
            _PILOT_FINAL_QTY.reset(token_qty)
            _PILOT_SIGNAL.reset(token_signal)
            _PILOT_SYMBOL.reset(token_symbol)
            _PILOT_ENGINE.reset(token_engine)

    def _risk_authoritative_pilot_quantity(info, price):
        engine = _PILOT_ENGINE.get()
        symbol = _PILOT_SYMBOL.get()
        if engine is None or not symbol:
            return original_minimum(info, price)

        # Shadowed in the LIVE pilot by final_sizing_invariants; min() keeps it
        # at or below the risk-budget quantity whenever it is reached.
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
            "final_qty=%.12g authority=RiskManagerV3 role=shadowed_min_cap",
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
        # the already-quantized quantity still fits the MAX_MARGIN_PCT
        # collateral ceiling. Any missing/inconsistent context
        # remains fail-closed.
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
            margin_ceiling = available * float(
                getattr(__import__("bot.config", fromlist=["cfg"]).cfg, "MAX_MARGIN_PCT", 0.0) or 0.0)
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
                "geometry_improved=true collateral_within_target=true",
                symbol, executable, sl, tp, margin, margin_ceiling,
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

        # INV-PREDISPATCH-RISK-001 at the fresh executable price: contracts and
        # native stop are fixed; a worse price can only BLOCK, never resize.
        from bot import risk_budget
        auth = risk_budget.current_authorization()
        try:
            if auth is None:
                raise risk_budget.RiskBudgetRefused("NO_RISK_AUTHORIZATION")
            executable_price = float(metrics.get("executable_price"))
            risk_budget.assert_projected_loss_within_budget(
                symbol=symbol, contracts=auth.contracts, multiplier=auth.multiplier,
                entry=executable_price, stop=float(sig.sl), direction=direction,
                cost_fraction=auth.cost_fraction, equity=auth.equity,
                risk_pct=auth.risk_pct, stage="FRESH_PREDISPATCH_RECHECK",
                leverage=auth.leverage)
        except (risk_budget.RiskBudgetRefused, TypeError, ValueError) as exc:
            log.critical(
                "[RISK_BUDGET_REJECTED] symbol=%s stage=FRESH_PREDISPATCH_RECHECK reason=%s "
                "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                symbol, getattr(exc, "reason", type(exc).__name__),
            )
            return False
        risk_budget.update_entry_price(executable_price)

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
        "[PILOT_RISK_CAP] installed: sizing authority=RISK_BUDGET_V3 (final_sizing_invariants); "
        "risk authorization carried to transport; "
        "LIVE spread/depth/signal-drift rechecked fail-closed only after final sizing; "
        "directional_drift_telemetry=true authorization_unchanged=true"
    )
