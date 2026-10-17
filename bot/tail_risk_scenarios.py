"""Tail-risk stress scenarios for candidate/trade research."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True)
class TailScenario:
    name: str
    adverse_gap_pct: float = 0.0
    slippage_multiplier: float = 1.0
    fee_multiplier: float = 1.0
    funding_shock_pct: float = 0.0
    partial_fill_fraction: float = 1.0
    extra_latency_ms: float = 0.0

    def validate(self) -> "TailScenario":
        vals = (
            self.adverse_gap_pct,
            self.slippage_multiplier,
            self.fee_multiplier,
            self.funding_shock_pct,
            self.partial_fill_fraction,
            self.extra_latency_ms,
        )
        if not self.name or any(not math.isfinite(float(v)) for v in vals):
            raise ValueError("invalid tail scenario")
        if self.adverse_gap_pct < 0 or self.slippage_multiplier < 0:
            raise ValueError("gap/slippage must be nonnegative")
        if self.fee_multiplier < 0 or self.extra_latency_ms < 0:
            raise ValueError("fee/latency must be nonnegative")
        if not 0 < self.partial_fill_fraction <= 1:
            raise ValueError("partial_fill_fraction must be in (0,1]")
        return self


def stress_trade(
    *,
    notional_usdt: float,
    stop_loss_pct: float,
    baseline_slippage_pct: float,
    baseline_fee_pct: float,
    scenario: TailScenario,
) -> dict[str, object]:
    scenario.validate()
    inputs = (
        notional_usdt,
        stop_loss_pct,
        baseline_slippage_pct,
        baseline_fee_pct,
    )
    if any(not math.isfinite(float(v)) for v in inputs):
        raise ValueError("non-finite stress input")
    if notional_usdt <= 0 or min(
        stop_loss_pct,
        baseline_slippage_pct,
        baseline_fee_pct,
    ) < 0:
        raise ValueError("invalid stress geometry")

    stressed_move_pct = (
        stop_loss_pct
        + scenario.adverse_gap_pct
        + baseline_slippage_pct * scenario.slippage_multiplier
        + baseline_fee_pct * scenario.fee_multiplier
        + scenario.funding_shock_pct
    )
    return {
        "scenario": scenario.name,
        "stressed_loss_pct": stressed_move_pct,
        "stressed_loss_usdt": notional_usdt * stressed_move_pct / 100.0,
        "filled_fraction": scenario.partial_fill_fraction,
        "extra_latency_ms": scenario.extra_latency_ms,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def stress_matrix(
    scenarios: Iterable[TailScenario],
    **trade,
) -> tuple[dict[str, object], ...]:
    return tuple(
        stress_trade(scenario=scenario, **trade)
        for scenario in scenarios
    )
