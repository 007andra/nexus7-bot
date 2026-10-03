"""Final LIVE pilot sizing authority (F-003: stop-loss risk budget).

    TECHNICAL STOP -> RISK BUDGET (equity x MAX_RISK_PCT) -> CONTRACTS (floor)
    -> MARGIN CEILING (available x MAX_MARGIN_PCT) -> OPEN-RISK CAP
    -> FINAL INVARIANT -> authorization carried to the transport boundary.

``RiskManagerV3`` (through ``ProfessionalRiskAdapter.size_detail``) is the only
function that turns a risk budget into a maximum quantity. This wrapper adds no
target of its own: it can only accept that quantity or refuse the trade. There
is no margin target; margin is a ceiling. NO TRADE is a valid result.
"""
from __future__ import annotations

import math

from bot.config import cfg
from bot.quantity import quantity_rules


def install(engine_module, pilot_cap, log) -> None:
    if getattr(engine_module, "_final_sizing_invariants_installed", False):
        return

    previous_minimum = engine_module.minimum_base_quantity

    def _reject(symbol, reason, **fields):
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        log.critical("[RISK_BUDGET_REJECTED] symbol=%s reason=%s %s", symbol, reason, extra)
        pilot_cap._PILOT_FINAL_QTY.set(0.0)
        return 0.0

    def _final_risk_authoritative_quantity(info, price):
        engine = pilot_cap._PILOT_ENGINE.get()
        symbol = pilot_cap._PILOT_SYMBOL.get()
        if engine is None or not symbol:
            return previous_minimum(info, price)
        if getattr(engine, "paper_trade", False) or not bool(
            getattr(getattr(engine, "pilot", None), "enabled", False)
        ):
            return previous_minimum(info, price)

        from bot import risk_budget
        signal = pilot_cap._PILOT_SIGNAL.get()
        try:
            price_f = float(price)
            leverage = float(cfg.LEVERAGE)
            direction = str(getattr(signal, "direction", "")).upper()
            stop = float(getattr(signal, "sl"))
            multiplier, _, _, _ = quantity_rules(info)
            open_pct, _ = risk_budget.configured_risk_limits()
            if direction not in ("LONG", "SHORT") or not all(
                    math.isfinite(v) and v > 0 for v in (price_f, leverage, stop)):
                raise ValueError("invalid_context")
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError,
                risk_budget.RiskBudgetRefused) as exc:
            return _reject(symbol, f"context_{getattr(exc, 'reason', type(exc).__name__)}")

        try:
            sizing, risk_pct, cost = engine.risk.size_detail(
                symbol, price_f, engine.instruments)
            equity = float(engine.risk.professional_snapshot.capital.equity)
        except risk_budget.RiskBudgetRefused as exc:
            return _reject(symbol, exc.reason)
        except Exception as exc:
            return _reject(symbol, f"risk_sizing_{type(exc).__name__}")

        if sizing.qty <= 0:
            return _reject(symbol, sizing.rejection_reason or sizing.binding_constraint,
                           risk_budget=f"{sizing.risk_budget:.6f}",
                           stop_pct=f"{sizing.stop_distance_pct:.5f}")
        contracts = round(float(sizing.qty) / float(multiplier))
        if not math.isclose(contracts * float(multiplier), float(sizing.qty),
                            rel_tol=1e-9, abs_tol=1e-12) or contracts <= 0:
            return _reject(symbol, "quantity_not_integral_contracts")

        try:
            metrics = risk_budget.assert_projected_loss_within_budget(
                symbol=symbol, contracts=contracts, multiplier=multiplier, entry=price_f,
                stop=stop, direction=direction, cost_fraction=cost, equity=equity,
                risk_pct=risk_pct, stage="FINAL_SIZING", leverage=leverage)
            reserved, total = risk_budget.assert_open_risk_within_cap(
                engine, symbol=symbol, proposed=equity * risk_pct, equity=equity,
                risk_pct=risk_pct, open_pct=open_pct)
        except risk_budget.RiskBudgetRefused as exc:
            return _reject(symbol, exc.reason)

        qty = contracts * float(multiplier)
        risk_budget.authorize(risk_budget.RiskAuthorization(
            symbol=symbol, side="buy" if direction == "LONG" else "sell",
            direction=direction, contracts=contracts, multiplier=float(multiplier),
            entry=price_f, stop=stop, cost_fraction=cost, equity=equity,
            risk_pct=risk_pct, risk_budget=equity * risk_pct,
            projected_loss=metrics["projected_loss"], reserved_before=reserved,
            leverage=leverage))
        pilot_cap._PILOT_FINAL_QTY.set(qty)
        log.warning(
            "[RISK_SIZING] symbol=%s result=PASS equity=%.6f risk_pct=%.4f risk_budget=%.6f "
            "entry=%.10g stop=%.10g stop_pct=%.5f cost_fraction=%.5f final_contracts=%s "
            "notional=%.6f margin=%.6f projected_loss=%.6f projected_loss_pct=%.5f "
            "aggregate_reserved_pct=%.5f leverage=%.0fx binding=%s authority=RISK_BUDGET_V3",
            symbol, equity, risk_pct, equity * risk_pct, price_f, stop,
            metrics["stop_pct"], cost, contracts, metrics["notional"], metrics["margin"],
            metrics["projected_loss"], metrics["projected_loss_pct"], total / equity,
            leverage, sizing.binding_constraint,
        )
        return qty

    engine_module.minimum_base_quantity = _final_risk_authoritative_quantity
    engine_module._final_sizing_invariants_installed = True
    log.critical(
        "[FINAL_SIZING_INVARIANT] installed=true sizing_authority=RISK_BUDGET_V3 "
        "risk_pct=%s open_risk_cap=%s margin_role=CEILING leverage_role=MARGIN_ONLY "
        "configured_leverage_unchanged=true fail_closed=true",
        cfg.MAX_RISK_PCT, cfg.MAX_OPEN_RISK_PCT,
    )
