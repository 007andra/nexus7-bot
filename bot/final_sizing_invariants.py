"""Final LIVE pilot sizing authority (the last ``minimum_base_quantity`` hook).

Canonical sizing contract, which every sizing log reports verbatim:

* ``risk_authority=RiskManagerV3``: the stop-risk quantity sized from
  ``equity * effective_risk_pct`` over the planned stop distance plus
  round-trip fees and slippage. Leverage only changes required collateral.
* The operator ceiling defaults to ``available * 0.50`` of initial margin.
  An explicit ``LIVE_OPERATOR_MARGIN_FRACTION`` may raise the ceiling up to
  1.0; exchange lots are always floored.
* ``final_quantity_policy=min(stop_risk_qty,operator_margin_cap_qty)``.

Any invalid/non-positive input on either side yields ``qty=0`` (fail closed).
The projected-loss ceiling in ``final_loss_budget`` is enforced fail-closed
after final sizing; the fresh pre-dispatch path rechecks it at executable price.
Earlier pilot hooks (``pilot_live_runtime``, ``pilot_risk_cap_hardening``,
``operator_runtime_policy``) are shadowed by this one in a pilot context.
"""
from __future__ import annotations

import math
import os
from decimal import Decimal, ROUND_FLOOR

from bot.config import cfg
from bot.execution_cost import fallback_taker_fee
from bot.quantity import quantity_rules

MARGIN_FRACTION = 0.50
ENTRY_PRICE_BUFFER = 0.005

TARGET_POLICY = "50pct_available_initial_margin_cap"
RISK_AUTHORITY = "RiskManagerV3"
FINAL_QUANTITY_POLICY = "min(stop_risk_qty,operator_margin_cap_qty)"
SIZING_CONTRACT = (
    f"target_policy={TARGET_POLICY} risk_authority={RISK_AUTHORITY} "
    f"final_quantity_policy={FINAL_QUANTITY_POLICY}"
)


def operator_margin_fraction() -> float:
    """Read the explicit LIVE allocation cap; invalid values fail closed."""
    raw = os.environ.get("LIVE_OPERATOR_MARGIN_FRACTION")
    if raw is None:
        return MARGIN_FRACTION
    try:
        fraction = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid LIVE operator margin fraction") from exc
    if not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("invalid LIVE operator margin fraction")
    return fraction


def sizing_contract(fraction: float) -> str:
    policy = f"{fraction * 100:g}pct_available_initial_margin_cap"
    return (
        f"target_policy={policy} risk_authority={RISK_AUTHORITY} "
        f"final_quantity_policy={FINAL_QUANTITY_POLICY}"
    )


def operator_target_margin(available: float, leverage: float, fraction: float) -> float:
    """Cap opening margin so fee and a 0.5% price buffer also fit collateral.

    At a 100% allocation setting, spending the full wallet on initial margin
    can make the Binance entry fail before the protective stop can be placed.
    The reserve is calculated on notional, then the exchange lot floors it.
    """
    values = (available, leverage, fraction)
    if any(not math.isfinite(float(v)) or float(v) <= 0 for v in values):
        raise ValueError("invalid operator margin input")
    if fraction > 1:
        raise ValueError("operator margin fraction above one")
    available_d = Decimal(str(available))
    leverage_d = Decimal(str(leverage))
    fraction_d = Decimal(str(fraction))
    fee_d = Decimal(str(fallback_taker_fee()))
    execution_factor = Decimal("1") + Decimal(str(ENTRY_PRICE_BUFFER))
    collateral_per_notional = execution_factor * (Decimal("1") / leverage_d + fee_d)
    notional = min(available_d * fraction_d * leverage_d, available_d / collateral_per_notional)
    return float(notional / leverage_d)


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


def _operator_target_quantity(
    info: dict, price: float, available: float, leverage: float,
    *, fraction: float | None = None,
) -> float:
    """Derive the operator margin cap directly from fresh collateral.

    This deliberately does not depend on any earlier legacy sizing wrapper.
    Contract lots are floored so rounding cannot exceed the allocation.
    """
    price_d = Decimal(str(price))
    available_d = Decimal(str(available))
    leverage_d = Decimal(str(leverage))
    if any(not v.is_finite() or v <= 0 for v in (price_d, available_d, leverage_d)):
        return 0.0

    multiplier, lot, minimum, min_notional = quantity_rules(info)
    fraction = operator_margin_fraction() if fraction is None else fraction
    target_margin = Decimal(str(operator_target_margin(available, leverage, fraction)))
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
            fraction = operator_margin_fraction()
            target_qty = _operator_target_quantity(
                info, price_f, available, leverage, fraction=fraction,
            )
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
            from bot.drawdown_recovery import recovery_size_multiplier
            drawdown = float(getattr(engine.risk, "drawdown", 0.0) or 0.0)
            recovery_mult = recovery_size_multiplier(drawdown)
            if recovery_mult <= 0:
                log.critical(
                    "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK "
                    "reason=drawdown_authorization_invalid",
                    symbol,
                )
                pilot_cap._PILOT_FINAL_QTY.set(0.0)
                return 0.0
            risk_qty = float(engine.risk.size(
                symbol,
                price_f,
                engine.instruments,
                size_mult=recovery_mult,
                open_positions=engine.positions,
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
                symbol, target_qty, risk_qty, sizing_contract(fraction),
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        target_margin = operator_target_margin(available, leverage, fraction)
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

        signal = pilot_cap._PILOT_SIGNAL.get()
        cost_fraction = float("nan")
        setup_id = str(getattr(signal, "_bgx_setup_id", "") or "UNKNOWN")
        from bot.final_loss_budget import diagnose, emit_telemetry
        from bot.execution_cost import stress_cost_fraction

        try:
            # Conservative diagnostic input: max(candidate snapshot, static fallback).
            cost_fraction, _cost_ref = stress_cost_fraction(signal, symbol)
            result, specific_reason, _metrics = diagnose(
                final_qty,
                price_f,
                getattr(signal, "sl", float("nan")),
                getattr(signal, "direction", "UNKNOWN"),
                leverage,
                cost_fraction,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic must not affect sizing
            result = "UNAVAILABLE"
            specific_reason = f"diagnostic_{type(exc).__name__}"
        loss_execution_effect = (
            "NONE" if str(result).upper() == "PASS" else "BLOCK_NEW_ENTRY"
        )
        emit_telemetry(
            log, symbol=symbol, setup_id=setup_id,
            stage="FINAL_SIZING_INVARIANT", qty=final_qty, entry=price_f,
            stop=getattr(signal, "sl", float("nan")),
            direction=getattr(signal, "direction", "UNKNOWN"),
            leverage=leverage, cost_fraction=cost_fraction, result=result,
            specific_reason=specific_reason,
            risk_v3_advisory_qty=risk_qty,
            execution_effect=loss_execution_effect,
        )
        if str(result).upper() != "PASS":
            log.critical(
                "[FINAL_LOSS_BUDGET_GATE] symbol=%s setup_id=%s "
                "stage=FINAL_SIZING_INVARIANT result=BLOCK reason=%s "
                "execution_effect=BLOCK_NEW_ENTRY",
                symbol, setup_id, specific_reason,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        pilot_cap._PILOT_FINAL_QTY.set(final_qty)
        log.warning(
            "[FINAL_SIZING_INVARIANT] symbol=%s result=PASS operator_margin_cap_qty=%.12g "
            "stop_risk_qty=%.12g final_qty=%.12g binding=%s cap_margin=%.6f "
            "cap_notional=%.6f final_notional=%.6f final_margin=%.6f margin_cap_pct=%.2f%% "
            "leverage=%.0fx recovery_size_mult=%.6f %s",
            symbol, target_qty, risk_qty, final_qty,
            binding_constraint(target_qty=target_qty, risk_qty=risk_qty),
            target_margin, target_notional, final_qty * price_f, final_margin,
            fraction * 100.0, leverage, recovery_mult, sizing_contract(fraction),
        )
        return final_qty

    engine_module.minimum_base_quantity = _final_operator_authoritative_quantity
    engine_module._final_sizing_invariants_installed = True
    log.critical(
        "[FINAL_SIZING_INVARIANT] installed=true %s configured_leverage_unchanged=true "
        "fail_closed=true",
        sizing_contract(operator_margin_fraction()),
    )
