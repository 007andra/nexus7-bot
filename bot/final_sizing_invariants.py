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
The historical projected-loss ceiling in ``final_loss_budget`` is diagnostic only.
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


def _controlled_absolute_budget_quantity(
    info: dict,
    *,
    entry: float,
    stop: float,
    direction: str,
    cost_fraction: float,
    loss_budget_usdt: float,
) -> float:
    """Floor base quantity to the largest exchange lot inside an absolute loss budget.

    This is a one-shot controlled-reentry safety cap only. It can only reduce
    the quantity already authorized by RiskManagerV3/operator margin sizing; it
    never raises quantity, moves the technical stop, changes leverage, or grants
    execution authority.
    """
    try:
        entry_d = Decimal(str(entry))
        stop_d = Decimal(str(stop))
        cost_d = Decimal(str(cost_fraction))
        budget_d = Decimal(str(loss_budget_usdt))
        direction_s = str(direction).upper()
    except Exception:
        return 0.0

    if any(not value.is_finite() for value in (entry_d, stop_d, cost_d, budget_d)):
        return 0.0
    if entry_d <= 0 or stop_d <= 0 or cost_d < 0 or budget_d <= 0:
        return 0.0
    if not (
        (direction_s == "LONG" and stop_d < entry_d)
        or (direction_s == "SHORT" and stop_d > entry_d)
    ):
        return 0.0

    loss_per_base = abs(entry_d - stop_d) + entry_d * cost_d
    if loss_per_base <= 0:
        return 0.0

    try:
        multiplier, lot, minimum, min_notional = quantity_rules(info)
    except Exception:
        return 0.0
    if any(value <= 0 for value in (multiplier, lot, minimum)):
        return 0.0

    raw_base_qty = budget_d / loss_per_base
    contracts = raw_base_qty / multiplier
    contracts = (contracts / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
    if contracts < minimum:
        return 0.0

    base_qty = contracts * multiplier
    if base_qty * entry_d < min_notional:
        return 0.0
    return float(base_qty)


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
            controlled_ok = False
            controlled_reason = "not_configured"
            try:
                from bot import controlled_live_reentry_v1 as controlled_reentry
                controlled_ok, controlled_reason, _controlled_evidence = (
                    controlled_reentry.readiness(engine)
                )
            except Exception as exc:
                controlled_ok = False
                controlled_reason = f"controlled_reentry_{type(exc).__name__}"

            if controlled_ok:
                # The planned RiskManagerV3 percentage already encodes the
                # absolute one-shot USDT ceiling, so no drawdown multiplier is
                # applied a second time.
                recovery_mult = 1.0
            else:
                from bot.drawdown_recovery import recovery_size_multiplier
                drawdown = float(getattr(engine.risk, "drawdown", 0.0) or 0.0)
                recovery_mult = recovery_size_multiplier(drawdown)
                if recovery_mult <= 0:
                    log.critical(
                        "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK "
                        "reason=drawdown_authorization_invalid controlled_reason=%s",
                        symbol,
                        controlled_reason,
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
        emit_telemetry(
            log, symbol=symbol, setup_id=setup_id,
            stage="FINAL_SIZING_INVARIANT", qty=final_qty, entry=price_f,
            stop=getattr(signal, "sl", float("nan")),
            direction=getattr(signal, "direction", "UNKNOWN"),
            leverage=leverage, cost_fraction=cost_fraction, result=result,
            specific_reason=specific_reason,
            risk_v3_advisory_qty=risk_qty,
        )
        # Normal LIVE keeps the historical margin-relative loss ceiling.
        # The explicitly armed one-shot re-entry instead uses RiskManagerV3 plus
        # an absolute USDT ceiling. A legacy WARN is telemetry there, never a
        # reason to exceed the absolute budget; UNAVAILABLE remains fail-closed.
        if controlled_ok:
            if result == "UNAVAILABLE" or not isinstance(_metrics, dict):
                log.critical(
                    "[CONTROLLED_LIVE_REENTRY_V1] symbol=%s stage=FINAL_SIZING "
                    "result=BLOCK reason=loss_geometry_unavailable",
                    symbol,
                )
                pilot_cap._PILOT_FINAL_QTY.set(0.0)
                return 0.0
            absolute_ok, absolute_reason, absolute_evidence = (
                controlled_reentry.projected_loss_allowed(
                    engine, float(_metrics["projected_loss"])
                )
            )
            if not absolute_ok and absolute_reason == "absolute_loss_budget_exceeded":
                initial_qty = final_qty
                budget_qty = _controlled_absolute_budget_quantity(
                    info,
                    entry=price_f,
                    stop=getattr(signal, "sl", float("nan")),
                    direction=getattr(signal, "direction", "UNKNOWN"),
                    cost_fraction=cost_fraction,
                    loss_budget_usdt=float(
                        absolute_evidence.get("loss_budget_usdt", 0.0) or 0.0
                    ),
                )
                clamped_qty = min(float(final_qty), float(budget_qty))
                if not math.isfinite(clamped_qty) or clamped_qty <= 0 or clamped_qty >= final_qty:
                    log.critical(
                        "[CONTROLLED_LIVE_REENTRY_V1] symbol=%s stage=FINAL_SIZING "
                        "result=BLOCK reason=absolute_loss_budget_no_feasible_lot "
                        "initial_qty=%.12g budget_qty=%.12g projected_loss_usdt=%s "
                        "absolute_loss_budget_usdt=%s",
                        symbol, initial_qty, budget_qty,
                        absolute_evidence.get("projected_loss_usdt", "NA"),
                        absolute_evidence.get("loss_budget_usdt", "NA"),
                    )
                    pilot_cap._PILOT_FINAL_QTY.set(0.0)
                    return 0.0

                clamp_result, clamp_reason, clamp_metrics = diagnose(
                    clamped_qty,
                    price_f,
                    getattr(signal, "sl", float("nan")),
                    getattr(signal, "direction", "UNKNOWN"),
                    leverage,
                    cost_fraction,
                )
                if clamp_result == "UNAVAILABLE" or not isinstance(clamp_metrics, dict):
                    log.critical(
                        "[CONTROLLED_LIVE_REENTRY_V1] symbol=%s stage=FINAL_SIZING "
                        "result=BLOCK reason=absolute_loss_budget_clamp_unavailable",
                        symbol,
                    )
                    pilot_cap._PILOT_FINAL_QTY.set(0.0)
                    return 0.0

                absolute_ok, absolute_reason, absolute_evidence = (
                    controlled_reentry.projected_loss_allowed(
                        engine, float(clamp_metrics["projected_loss"])
                    )
                )
                if not absolute_ok:
                    log.critical(
                        "[CONTROLLED_LIVE_REENTRY_V1] symbol=%s stage=FINAL_SIZING "
                        "result=BLOCK reason=%s initial_qty=%.12g clamped_qty=%.12g "
                        "projected_loss_usdt=%s absolute_loss_budget_usdt=%s",
                        symbol, absolute_reason, initial_qty, clamped_qty,
                        absolute_evidence.get("projected_loss_usdt", "NA"),
                        absolute_evidence.get("loss_budget_usdt", "NA"),
                    )
                    pilot_cap._PILOT_FINAL_QTY.set(0.0)
                    return 0.0

                final_qty = clamped_qty
                final_margin = (final_qty * price_f) / leverage
                result, specific_reason, _metrics = (
                    clamp_result, clamp_reason, clamp_metrics
                )
                emit_telemetry(
                    log, symbol=symbol, setup_id=setup_id,
                    stage="CONTROLLED_ABSOLUTE_LOSS_CLAMP",
                    qty=final_qty, entry=price_f,
                    stop=getattr(signal, "sl", float("nan")),
                    direction=getattr(signal, "direction", "UNKNOWN"),
                    leverage=leverage, cost_fraction=cost_fraction,
                    result=result, specific_reason=specific_reason,
                    risk_v3_advisory_qty=risk_qty,
                )
                log.critical(
                    "[CONTROLLED_LIVE_REENTRY_V1] symbol=%s stage=FINAL_SIZING "
                    "result=CLAMPED_PASS initial_qty=%.12g final_qty=%.12g "
                    "projected_loss_usdt=%.12g absolute_loss_budget_usdt=%.12g "
                    "headroom_usdt=%.12g quantity_only_reduced=true "
                    "risk_authority=RiskManagerV3",
                    symbol, initial_qty, final_qty,
                    float(absolute_evidence["projected_loss_usdt"]),
                    float(absolute_evidence["loss_budget_usdt"]),
                    float(absolute_evidence["headroom_usdt"]),
                )
            elif not absolute_ok:
                log.critical(
                    "[CONTROLLED_LIVE_REENTRY_V1] symbol=%s stage=FINAL_SIZING "
                    "result=BLOCK reason=%s projected_loss_usdt=%s "
                    "absolute_loss_budget_usdt=%s",
                    symbol,
                    absolute_reason,
                    absolute_evidence.get("projected_loss_usdt", "NA"),
                    absolute_evidence.get("loss_budget_usdt", "NA"),
                )
                pilot_cap._PILOT_FINAL_QTY.set(0.0)
                return 0.0

            log.critical(
                "[CONTROLLED_LIVE_REENTRY_V1] symbol=%s stage=FINAL_SIZING "
                "result=PASS projected_loss_usdt=%.12g absolute_loss_budget_usdt=%.12g "
                "headroom_usdt=%.12g legacy_margin_ceiling=%s "
                "risk_authority=RiskManagerV3",
                symbol,
                float(absolute_evidence["projected_loss_usdt"]),
                float(absolute_evidence["loss_budget_usdt"]),
                float(absolute_evidence["headroom_usdt"]),
                result,
            )
        elif result != "PASS":
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK "
                "reason=final_loss_budget_%s candidate_only=true "
                "runtime_paused=false",
                symbol, specific_reason,
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
