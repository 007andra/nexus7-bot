"""Research-only robustness and sensitivity surfaces for NEXUS."""
from __future__ import annotations

import json
from dataclasses import dataclass
from math import isfinite
from typing import Mapping, Sequence

from bot.research_statistics import performance_metrics


@dataclass(frozen=True)
class ParameterRun:
    parameters: Mapping[str, object]
    net_returns: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.parameters:
            raise ValueError("parameter run needs parameters")
        if not self.net_returns:
            raise ValueError("parameter run needs returns")
        if any(not isfinite(float(value)) or float(value) <= -1 for value in self.net_returns):
            raise ValueError("invalid sensitivity return")

    @property
    def parameter_key(self) -> str:
        return json.dumps(
            dict(self.parameters), sort_keys=True, separators=(",", ":"), default=str
        )


def parameter_surface(runs: Sequence[ParameterRun]) -> dict:
    """Summarize a parameter neighborhood without selecting/promoting a winner."""
    if not runs:
        raise ValueError("empty sensitivity surface")
    keys = [run.parameter_key for run in runs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate parameter run")
    points = []
    positive = 0
    for run in sorted(runs, key=lambda item: item.parameter_key):
        metrics = performance_metrics(run.net_returns)
        if metrics["expectancy"] > 0:
            positive += 1
        points.append({
            "parameters": dict(run.parameters),
            "metrics": metrics,
        })
    expectancies = [point["metrics"]["expectancy"] for point in points]
    ordered = sorted(expectancies)
    mid = len(ordered) // 2
    median = (
        ordered[mid] if len(ordered) % 2
        else (ordered[mid - 1] + ordered[mid]) / 2.0
    )
    return {
        "points": points,
        "parameter_sets": len(points),
        "positive_expectancy_fraction": positive / len(points),
        "median_expectancy": median,
        "min_expectancy": min(expectancies),
        "max_expectancy": max(expectancies),
        "promotion_effect": "NONE",
    }


def execution_cost_surface(
    gross_returns: Sequence[float],
    *,
    turnovers: Sequence[float] | None = None,
    fee_bps_grid: Sequence[float] = (0.0, 2.0, 4.0, 6.0, 10.0),
    slippage_bps_grid: Sequence[float] = (0.0, 1.0, 2.0, 5.0, 10.0),
) -> dict:
    """Stress gross returns across fee/slippage grids with per-trade turnover."""
    gross = tuple(float(x) for x in gross_returns)
    if not gross:
        raise ValueError("empty gross return series")
    if any(not isfinite(x) or x <= -1 for x in gross):
        raise ValueError("invalid gross return")
    turns = (
        tuple(float(x) for x in turnovers)
        if turnovers is not None else tuple(1.0 for _ in gross)
    )
    if len(turns) != len(gross) or any(not isfinite(x) or x < 0 for x in turns):
        raise ValueError("invalid turnover series")
    points = []
    for fee_bps in fee_bps_grid:
        for slip_bps in slippage_bps_grid:
            fee = float(fee_bps)
            slip = float(slip_bps)
            if fee < 0 or slip < 0:
                raise ValueError("negative cost grid")
            net = tuple(
                r - t * (fee + slip) / 10_000.0
                for r, t in zip(gross, turns)
            )
            points.append({
                "fee_bps": fee,
                "slippage_bps": slip,
                "metrics": performance_metrics(net),
            })
    return {
        "points": points,
        "promotion_effect": "NONE",
    }


def incremental_cost_surface(
    base_net_returns: Sequence[float],
    *,
    extra_round_trip_fee_bps_grid: Sequence[float] = (0.0, 2.0, 5.0, 10.0),
    extra_round_trip_slippage_bps_grid: Sequence[float] = (0.0, 2.0, 5.0, 10.0),
) -> dict:
    """Stress already-net returns with *additional* round-trip execution drag.

    This is the correct surface when the base replay already includes fees,
    adverse slippage and funding. It never subtracts those baseline costs a
    second time.
    """
    base = tuple(float(value) for value in base_net_returns)
    if not base:
        raise ValueError("empty base net return series")
    if any(not isfinite(value) or value <= -1 for value in base):
        raise ValueError("invalid base net return")

    points = []
    for fee_bps in extra_round_trip_fee_bps_grid:
        for slippage_bps in extra_round_trip_slippage_bps_grid:
            fee = float(fee_bps)
            slippage = float(slippage_bps)
            if (
                not isfinite(fee)
                or not isfinite(slippage)
                or fee < 0
                or slippage < 0
            ):
                raise ValueError("invalid incremental cost grid")
            extra_drag = (fee + slippage) / 10_000.0
            stressed = tuple(value - extra_drag for value in base)
            points.append({
                "extra_round_trip_fee_bps": fee,
                "extra_round_trip_slippage_bps": slippage,
                "extra_drag_fraction": extra_drag,
                "metrics": performance_metrics(stressed),
            })

    return {
        "basis": "BASE_NET_RETURNS_ALREADY_INCLUDE_BASELINE_EXECUTION_COSTS",
        "points": points,
        "promotion_effect": "NONE",
    }
