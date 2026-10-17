"""Explain a stop-risk sizing result in exact Decimal terms (observability only).

This module never decides a quantity. It re-derives, from the same inputs the
runtime already used (``ProfessionalRiskAdapter.size`` ->
``RiskManagerV3.size_for_stop`` -> ``professional_risk.stop_risk_size``), why a
candidate was or was not sizeable, and names the binding constraint:

- ``INVALID_METADATA``        exchange filters unusable (fail closed);
- ``MIN_QTY_BINDING``         the smallest valid order is set by minQty;
- ``MIN_NOTIONAL_BINDING``    the smallest valid order is set by minNotional;
- ``MARGIN_CAP_BINDING``      collateral cap is below the smallest valid order;
- ``RISK_BUDGET``             the risk quantity is valid (PASS path).

Result classes: ``INSUFFICIENT_RISK_BUDGET`` (the smallest valid order would
lose more than ``equity * risk_pct`` at the stop), ``ROUNDING_TO_ZERO`` is
reported as a flag when the floored risk quantity is exactly zero.

Units (dimensional contract):
    risk_budget            USDT
    loss_per_unit          USDT / base-asset unit (price distance + costs)
    raw_qty                USDT / (USDT/base) = base-asset units
    step, min_qty          base-asset units (Binance BASE_ASSET instruments)
    min_notional           USDT
    notional = qty * entry USDT
    margin = notional / leverage   USDT (leverage only scales collateral)
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from bot.quantity import number, quantity_rules


def _d(value) -> Decimal:
    out = Decimal(str(value))
    if not out.is_finite():
        raise ValueError("non-finite sizing input")
    return out


def decompose(
    *,
    info: dict,
    equity,
    available,
    entry,
    stop,
    risk_pct,
    leverage,
    max_margin_pct,
    fee_rate_per_side=0,
    slippage_pct=0,
) -> dict:
    """Exact Decimal decomposition mirroring ``stop_risk_size`` semantics."""
    try:
        multiplier, lot, minimum, min_notional = quantity_rules(info)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        return {"result": "BLOCK", "reason": "INVALID_METADATA", "binding": "INVALID_METADATA",
                "error": type(exc).__name__}

    equity, available, entry, stop = _d(equity), _d(available), _d(entry), _d(stop)
    risk_pct, leverage, max_margin_pct = _d(risk_pct), _d(leverage), _d(max_margin_pct)
    fee, slippage = _d(fee_rate_per_side), _d(slippage_pct)
    if equity <= 0 or entry <= 0 or stop <= 0 or entry == stop or leverage <= 0:
        return {"result": "BLOCK", "reason": "INVALID_INPUT", "binding": "INVALID_INPUT"}

    step = multiplier * lot                       # base units per order step
    min_qty = minimum * multiplier                # base units (exchange minQty)
    risk_budget = equity * risk_pct               # USDT
    risk_per_unit = abs(entry - stop)             # USDT per base unit (price only)
    loss_per_unit = risk_per_unit + entry * fee * 2 + entry * slippage  # USDT/base
    raw_qty_risk = risk_budget / loss_per_unit    # base units
    margin_cap = available * max_margin_pct       # USDT of initial margin
    raw_qty_margin = margin_cap * leverage / entry  # base units
    raw_qty = min(raw_qty_risk, raw_qty_margin)
    rounded_qty = (raw_qty / step).to_integral_value(rounding=ROUND_FLOOR) * step

    # Smallest exchange-valid order: max(minQty, minNotional/price) rounded UP
    # to a whole step (this is quantity.minimum_base_quantity, in Decimal).
    notional_units = (min_notional / (entry * multiplier)) if min_notional > 0 else Decimal(0)
    min_units = max(minimum, notional_units)
    min_units = (min_units / lot).to_integral_value(rounding=ROUND_CEILING) * lot
    min_valid_qty = min_units * multiplier
    min_qty_by_notional = (
        (notional_units / lot).to_integral_value(rounding=ROUND_CEILING) * lot * multiplier
        if min_notional > 0 else Decimal(0)
    )
    min_binding = "MIN_NOTIONAL_BINDING" if min_qty_by_notional > min_qty else "MIN_QTY_BINDING"
    risk_at_min_valid = min_valid_qty * loss_per_unit
    margin_at_min_valid = min_valid_qty * entry / leverage

    out = {
        "equity": equity,
        "risk_pct": risk_pct,
        "risk_budget": risk_budget,
        "entry": entry,
        "stop": stop,
        "risk_per_unit": risk_per_unit,
        "loss_per_unit": loss_per_unit,
        "raw_qty": raw_qty,
        "raw_qty_risk": raw_qty_risk,
        "raw_qty_margin": raw_qty_margin,
        "step_size": step,
        "rounded_qty": rounded_qty,
        "min_qty": min_qty,
        "min_notional": min_notional,
        "min_valid_qty": min_valid_qty,
        "risk_at_min_valid_qty": risk_at_min_valid,
        "margin_at_min_valid_qty": margin_at_min_valid,
        "required_equity_at_min_valid_qty": risk_at_min_valid / risk_pct if risk_pct > 0 else None,
        "rounding_to_zero": raw_qty > 0 and rounded_qty == 0,
    }
    if rounded_qty >= min_valid_qty and rounded_qty > 0:
        out.update(result="PASS", reason="SIZED",
                   binding="RISK_BUDGET" if raw_qty_risk <= raw_qty_margin else "MARGIN_CAP_BINDING")
        return out
    if raw_qty_margin < min_valid_qty and raw_qty_risk >= min_valid_qty:
        out.update(result="BLOCK", reason="MARGIN_CAP_BINDING", binding="MARGIN_CAP_BINDING")
        return out
    out.update(result="BLOCK", reason="INSUFFICIENT_RISK_BUDGET", binding=min_binding)
    return out


def _fmt(value) -> str:
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value == value.to_integral() else f"{value:.10g}"
    return str(value)


def format_log(symbol: str, d: dict) -> str:
    keys = (
        "equity", "risk_pct", "risk_budget", "entry", "stop", "risk_per_unit",
        "loss_per_unit", "raw_qty", "step_size", "rounded_qty", "min_qty",
        "min_notional", "min_valid_qty", "risk_at_min_valid_qty",
        "margin_at_min_valid_qty", "required_equity_at_min_valid_qty",
        "rounding_to_zero", "binding", "reason", "result",
    )
    parts = [f"symbol={symbol}"]
    for key in keys:
        if key in d:
            parts.append(f"{key}={_fmt(d[key])}")
    return "[SIZING_DECOMPOSITION] " + " ".join(parts) + " decision_effect=NONE"


__all__ = ["decompose", "format_log", "number"]
