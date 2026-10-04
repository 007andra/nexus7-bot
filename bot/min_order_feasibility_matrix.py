"""Read-only universe audit for minimum-order risk feasibility.

This module does not participate in trading decisions. It derives, from the
already-loaded Binance instrument filters and current prices, the maximum stop
distance that the smallest exchange-valid order could tolerate under the
current risk budget.

The result is intentionally optimistic with respect to costs: it uses the
runtime fallback taker fee and configured minimum slippage. Candidate-specific
cost snapshots can only make the later executable gate stricter.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
import math
import os

from bot.config import cfg
from bot.drawdown_recovery import recovery_size_multiplier
from bot import execution_cost
from bot.quantity import quantity_rules


def _d(value) -> Decimal:
    out = Decimal(str(value))
    if not out.is_finite():
        raise ValueError("non-finite feasibility matrix input")
    return out


def effective_risk_pct(engine) -> Decimal:
    base = float(engine._effective_risk_pct())
    drawdown = float(getattr(getattr(engine, "risk", None), "drawdown", 0.0) or 0.0)
    mult = float(recovery_size_multiplier(drawdown))
    value = base * mult
    if not math.isfinite(value) or value <= 0 or value > 1:
        raise ValueError("invalid effective risk pct")
    return _d(value)


def available_collateral(engine, equity: Decimal) -> Decimal:
    value = getattr(engine, "_pilot_available_balance", None)
    try:
        candidate = _d(value)
        if candidate > 0:
            return min(candidate, equity)
    except Exception:
        pass
    return equity


def audit_symbol(
    *,
    info: dict,
    price,
    equity,
    available,
    risk_pct,
    leverage,
    max_margin_pct,
    fee_rate_per_side,
    slippage_pct,
) -> dict:
    multiplier, lot, minimum, min_notional = quantity_rules(info)
    price = _d(price)
    equity = _d(equity)
    available = _d(available)
    risk_pct = _d(risk_pct)
    leverage = _d(leverage)
    max_margin_pct = _d(max_margin_pct)
    fee = _d(fee_rate_per_side)
    slippage = _d(slippage_pct)

    if price <= 0 or equity <= 0 or available <= 0 or risk_pct <= 0 or leverage <= 0:
        raise ValueError("invalid positive audit input")

    step = multiplier * lot
    min_qty = minimum * multiplier
    notional_units = (min_notional / (price * multiplier)) if min_notional > 0 else Decimal(0)
    min_units = max(minimum, notional_units)
    min_units = (min_units / lot).to_integral_value(rounding=ROUND_CEILING) * lot
    min_valid_qty = min_units * multiplier
    min_qty_by_notional = (
        (notional_units / lot).to_integral_value(rounding=ROUND_CEILING) * lot * multiplier
        if min_notional > 0 else Decimal(0)
    )
    binding = "MIN_NOTIONAL_BINDING" if min_qty_by_notional > min_qty else "MIN_QTY_BINDING"

    risk_budget = equity * risk_pct
    min_order_notional = min_valid_qty * price
    margin_at_min = min_order_notional / leverage
    margin_cap = available * max_margin_pct

    fixed_cost_per_unit = price * (fee * Decimal(2) + slippage)
    max_loss_per_unit = risk_budget / min_valid_qty
    max_stop_abs = max_loss_per_unit - fixed_cost_per_unit
    max_stop_pct = (
        max_stop_abs / price * Decimal(100)
        if max_stop_abs > 0
        else Decimal(0)
    )

    if margin_at_min > margin_cap:
        status = "MARGIN_BLOCK"
    elif max_stop_abs <= 0:
        status = "COST_BLOCK"
    else:
        status = "CONDITIONAL"

    return {
        "status": status,
        "binding": binding,
        "step_size": step,
        "min_qty": min_qty,
        "min_notional": min_notional,
        "min_valid_qty": min_valid_qty,
        "min_order_notional": min_order_notional,
        "risk_budget": risk_budget,
        "margin_at_min": margin_at_min,
        "margin_cap": margin_cap,
        "max_stop_abs": max(Decimal(0), max_stop_abs),
        "max_stop_pct": max_stop_pct,
        "fee_rate_per_side": fee,
        "slippage_pct": slippage,
    }


def build_matrix(engine, price_map: dict[str, float], *, risk_pct_override=None) -> list[dict]:
    equity = _d(getattr(getattr(engine, "risk", None), "balance", 0.0) or 0.0)
    if equity <= 0:
        raise ValueError("equity unavailable")
    available = available_collateral(engine, equity)
    if risk_pct_override is None:
        risk_pct = effective_risk_pct(engine)
    else:
        risk_pct = _d(risk_pct_override)
        if risk_pct <= 0 or risk_pct > 1:
            raise ValueError("invalid risk pct override")
    fee = _d(execution_cost.fallback_taker_fee())
    slippage = _d(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001"))

    rows = []
    for symbol in cfg.SYMBOLS:
        info = (getattr(engine, "instruments", {}) or {}).get(symbol)
        price = price_map.get(symbol)
        if not info or price is None or float(price) <= 0:
            rows.append({"symbol": symbol, "status": "UNAVAILABLE"})
            continue
        row = audit_symbol(
            info=info,
            price=price,
            equity=equity,
            available=available,
            risk_pct=risk_pct,
            leverage=cfg.LEVERAGE,
            max_margin_pct=getattr(cfg, "MAX_MARGIN_PCT", 0.80),
            fee_rate_per_side=fee,
            slippage_pct=slippage,
        )
        row["symbol"] = symbol
        row["price"] = _d(price)
        rows.append(row)
    return rows


def enabled():
    return os.environ.get("MIN_ORDER_FEASIBILITY_MATRIX", "false").lower() == "true"


def shadow_record(row):
    """Classify the existing counterfactual result; never perform LIVE sizing."""
    from bot.hard_gate_shadow_context import AUTHORITY, POPULATION
    if row.get("population") != POPULATION or row.get("counterfactual") is not True:
        raise ValueError("counterfactual research population required")
    fields = ("candidate_id", "symbol", "counterfactual", "counterfactual_risk_pct",
              "risk_budget", "min_valid_qty", "risk_at_min_qty", "binding",
              "shadow_min_order_feasible", "live_risk_authority",
              "capital_source", "capital_age_ms")
    return {**{k: row[k] for k in fields}, **AUTHORITY, "observability_only": True}


def log_once(engine, price_map: dict[str, float], log) -> None:
    if not enabled():
        return
    from bot.hard_gate_shadow_context import active
    if active():
        return  # shadow uses an explicit counterfactual row, not LIVE budget math
    if getattr(engine, "_min_order_feasibility_matrix_logged", False):
        return
    try:
        rows = build_matrix(engine, price_map)
    except Exception as exc:
        log.warning(
            "[MIN_ORDER_FEASIBILITY_MATRIX] result=DEFER reason=%s "
            "observability_only=true thresholds_unchanged=true leverage_unchanged=true",
            type(exc).__name__,
        )
        return

    conditional = margin_block = cost_block = unavailable = 0
    for row in rows:
        status = row["status"]
        if status == "CONDITIONAL":
            conditional += 1
        elif status == "MARGIN_BLOCK":
            margin_block += 1
        elif status == "COST_BLOCK":
            cost_block += 1
        else:
            unavailable += 1

        if status == "UNAVAILABLE":
            log.info(
                "[MIN_ORDER_FEASIBILITY_MATRIX] symbol=%s status=UNAVAILABLE "
                "observability_only=true decision_effect=NONE execution_effect=NONE",
                row["symbol"],
            )
            continue

        log.info(
            "[MIN_ORDER_FEASIBILITY_MATRIX] symbol=%s status=%s binding=%s "
            "price=%s min_qty=%s min_notional=%s min_valid_qty=%s "
            "min_order_notional=%s risk_budget=%s margin_at_min=%s margin_cap=%s "
            "max_stop_pct=%s fee_per_side=%s slippage=%s "
            "observability_only=true decision_effect=NONE execution_effect=NONE",
            row["symbol"],
            status,
            row["binding"],
            row["price"],
            row["min_qty"],
            row["min_notional"],
            row["min_valid_qty"],
            row["min_order_notional"],
            row["risk_budget"],
            row["margin_at_min"],
            row["margin_cap"],
            row["max_stop_pct"],
            row["fee_rate_per_side"],
            row["slippage_pct"],
        )

    log.warning(
        "[MIN_ORDER_FEASIBILITY_MATRIX_SUMMARY] symbols=%d conditional=%d "
        "margin_block=%d cost_block=%d unavailable=%d "
        "meaning=CONDITIONAL_REQUIRES_ACTUAL_STOP_AT_OR_BELOW_MAX_STOP_PCT "
        "observability_only=true thresholds_unchanged=true leverage_unchanged=true "
        "decision_effect=NONE execution_effect=NONE",
        len(rows), conditional, margin_block, cost_block, unavailable,
    )
    engine._min_order_feasibility_matrix_logged = True


__all__ = ["audit_symbol", "build_matrix", "log_once"]
