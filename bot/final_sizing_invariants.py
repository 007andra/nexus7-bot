"""Final LIVE pilot sizing authority (the last ``minimum_base_quantity`` hook).

Canonical sizing contract, which every sizing log reports verbatim:

* ``risk_authority=RiskManagerV3``: the stop-risk quantity sized from
  ``equity * effective_risk_pct`` over the planned stop distance plus
  round-trip fees and slippage. Leverage only changes required collateral.
* ``target_policy=50pct_available_initial_margin_cap``: the operator ceiling,
  ``available * 0.50`` of initial margin at configured leverage, floored to
  exchange lots.
* ``final_quantity_policy=min(stop_risk_qty,operator_margin_cap_qty)``.

Any invalid/non-positive input on either side yields ``qty=0`` (fail closed).
The legacy projected-loss ceiling in ``final_loss_budget`` is diagnostic only;\nmonetary risk remains owned by RiskManagerV3 and collateral by this invariant.
Earlier pilot hooks (``pilot_live_runtime``, ``pilot_risk_cap_hardening``,
``operator_runtime_policy``) are shadowed by this one in a pilot context.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_FLOOR

from bot.config import cfg
from bot.quantity import quantity_rules

MARGIN_FRACTION = 0.50

TARGET_POLICY = "50pct_available_initial_margin_cap"
RISK_AUTHORITY = "RiskManagerV3"
FINAL_QUANTITY_POLICY = "min(stop_risk_qty,operator_margin_cap_qty)"
SIZING_CONTRACT = (
    f"target_policy={TARGET_POLICY} risk_authority={RISK_AUTHORITY} "
    f"final_quantity_policy={FINAL_QUANTITY_POLICY}"
)


def _select_final_quantity(*, target_qty: float, risk_qty: float) -> float:
    """Return ``min(stop_risk_qty, operator_margin_cap_qty)`` or fail closed.

    Both inputs are already floored to the exchange lot, so their minimum is a
    valid exchange quantity. A risk quantity above the cap is clamped; a risk
    quantity below it binds. Non-finite or non-positive input returns zero.
    """
    try:
        values = (float(target_qty), float(risk_qty))
    except (TypeError, ValueError):
        return 0.0
    if any(not math.isfinite(v) or v <= 0 for v in values):
        return 0.0
    return min(values)


def binding_constraint(*, target_qty: float, risk_qty: float) -> str:
    """Name the constraint that produced the final quantity."""
    return "RISK_BUDGET" if float(risk_qty) <= float(target_qty) else "OPERATOR_MARGIN_CAP"


def _operator_target_quantity(info: dict, price: float, available: float, leverage: float) -> float:
    """Derive the 50%-margin target directly from fresh collateral.

    This deliberately does not depend on any earlier legacy sizing wrapper.
    Native KuCoin contract lots are floored so rounding can never consume more
    than the operator's 50% initial-margin allocation.
    """
    price_d = Decimal(str(price))
    available_d = Decimal(str(available))
    leverage_d = Decimal(str(leverage))
    if any(not v.is_finite() or v <= 0 for v in (price_d, available_d, leverage_d)):
        return 0.0

    multiplier, lot, minimum, min_notional = quantity_rules(info)
    target_margin = available_d * Decimal(str(MARGIN_FRACTION))
    target_notional = target_margin * leverage_d
    contracts = target_notional / (price_d * multiplier)
    contracts = (contracts / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
    if contracts < minimum:
        return 0.0
    if contracts * multiplier * price_d < min_notional:
        return 0.0
    return float(contracts * multiplier)


def install(engine_module, pilot_cap, log) -> None:
    if getattr(engine_module, "_final_sizing_invariants_installed", False):
        return

    previous_minimum = engine_module.minimum_base_quantity

    def _final_operator_authoritative_quantity(info, price):
        engine = pilot_cap._PILOT_ENGINE.get()
        symbol = pilot_cap._PILOT_SYMBOL.get()
        if engine is None or not symbol:
            return previous_minimum(info, price)
        if getattr(engine, "paper_trade", False) or not bool(
            getattr(getattr(engine, "pilot", None), "enabled", False)
        ):
            return previous_minimum(info, price)

        try:
            price_f = float(price)
            available = float(getattr(engine, "_pilot_available_balance", 0.0) or 0.0)
            leverage = float(cfg.LEVERAGE)
            target_qty = _operator_target_quantity(info, price_f, available, leverage)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=context_%s",
                symbol, type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        if any(not math.isfinite(v) or v <= 0 for v in (target_qty, price_f, available, leverage)):
            log.critical("[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=invalid_context", symbol)
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        try:
            risk_qty = float(engine.risk.size(
                symbol, price_f, engine.instruments, open_positions=engine.positions,
            ))
        except Exception as exc:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=risk_validation_%s",
                symbol, type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        final_qty = _select_final_quantity(target_qty=target_qty, risk_qty=risk_qty)
        if final_qty <= 0:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=invalid_quantity "
                "operator_margin_cap_qty=%.12g stop_risk_qty=%.12g %s",
                symbol, target_qty, risk_qty, SIZING_CONTRACT,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        target_margin = available * MARGIN_FRACTION
        target_notional = target_margin * leverage
        final_margin = (final_qty * price_f) / leverage
        tolerance = max(1e-9, target_margin * 1e-6)
        if not math.isfinite(final_margin) or final_margin > target_margin + tolerance:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=margin_cap_exceeded final_margin=%.12g target_margin=%.12g",
                symbol, final_margin, target_margin,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        signal = None
        cost_fraction = float("nan")
        setup_id = "UNKNOWN"
        try:
            from bot.final_loss_budget import emit_telemetry, measure
            from bot.execution_cost import stress_cost_fraction
            signal = pilot_cap._PILOT_SIGNAL.get()
            # Conservative diagnostic input: max(candidate snapshot, static fallback).
            cost_fraction, _cost_ref = stress_cost_fraction(signal, symbol)
            setup_id = str(getattr(signal, "_bgx_setup_id", "") or "UNKNOWN")
            legacy_metrics = measure(
                final_qty, price_f, signal.sl, signal.direction, leverage,
                cost_fraction,
            )
        except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
            # Invalid sizing context remains fail-closed. Only a *valid*
            # measurement above the historical 50%-of-entry-margin threshold
            # is non-blocking.
            emit_telemetry(
                log, symbol=symbol, setup_id=setup_id,
                stage="FINAL_SIZING_INVARIANT", qty=final_qty, entry=price_f,
                stop=getattr(signal, "sl", float("nan")),
                direction=getattr(signal, "direction", "UNKNOWN"),
                leverage=leverage, cost_fraction=cost_fraction, result="INVALID",
                specific_reason=type(exc).__name__,
                risk_v3_advisory_qty=risk_qty,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        legacy_exceeded = (
            legacy_metrics["projected_loss"]
            > legacy_metrics["loss_limit"]
            + max(1e-12, legacy_metrics["loss_limit"] * 1e-12)
        )
        emit_telemetry(
            log, symbol=symbol, setup_id=setup_id,
            stage="FINAL_SIZING_INVARIANT", qty=final_qty, entry=price_f,
            stop=signal.sl, direction=signal.direction, leverage=leverage,
            cost_fraction=cost_fraction,
            result="EXCEEDS_LEGACY_CEILING" if legacy_exceeded else "PASS",
            specific_reason=(
                "legacy_50pct_entry_margin_exceeded_diagnostic_only"
                if legacy_exceeded else "within_legacy_50pct_entry_margin"
            ),
            risk_v3_advisory_qty=risk_qty,
        )

        pilot_cap._PILOT_FINAL_QTY.set(final_qty)
        log.warning(
            "[FINAL_SIZING_INVARIANT] symbol=%s result=PASS operator_margin_cap_qty=%.12g "
            "stop_risk_qty=%.12g final_qty=%.12g binding=%s cap_margin=%.6f "
            "cap_notional=%.6f final_notional=%.6f final_margin=%.6f margin_cap_pct=50.00%% "
            "leverage=%.0fx %s",
            symbol, target_qty, risk_qty, final_qty,
            binding_constraint(target_qty=target_qty, risk_qty=risk_qty),
            target_margin, target_notional, final_qty * price_f, final_margin, leverage,
            SIZING_CONTRACT,
        )
        return final_qty

    engine_module.minimum_base_quantity = _final_operator_authoritative_quantity
    engine_module._final_sizing_invariants_installed = True
    log.critical(
        "[FINAL_SIZING_INVARIANT] installed=true %s configured_leverage_unchanged=true "
        "fail_closed=true",
        SIZING_CONTRACT,
    )
