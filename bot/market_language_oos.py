"""Offline OOS validation for the native NEXUS market-language candidate.

Research-only. This module does not import exchange clients, mutate runtime
policy, or participate in production scoring/execution.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite, sqrt
import random
from statistics import median
from typing import Iterable, Mapping, Sequence

from bot.execution_cost import LEGACY_CONSERVATIVE_TAKER_FEE, static_slippage_rate
from bot.market_language import forecast_market_language


@dataclass(frozen=True)
class OOSPoint:
    index: int
    timestamp: float | None
    probability_up: float
    available: bool
    actual_return: float
    actual_up: bool
    direction: int
    gross_directional_return: float | None
    net_directional_return: float | None


@dataclass(frozen=True)
class MarketLanguageOOSReport:
    symbol: str
    evaluated: int
    available: int
    coverage: float
    horizon: int
    step: int
    round_trip_cost_fraction: float
    brier_up: float | None
    climatology_brier: float | None
    brier_skill: float | None
    directional_accuracy_when_available: float | None
    mean_gross_directional_return: float | None
    mean_net_directional_return: float | None
    median_net_directional_return: float | None
    net_win_rate: float | None
    bootstrap_ci_low_net_return: float | None
    bootstrap_ci_high_net_return: float | None
    points: tuple[OOSPoint, ...]

    def summary_dict(self) -> dict:
        data = asdict(self)
        data.pop("points", None)
        return data


def _close(candle: Mapping[str, object]) -> float:
    value = candle.get("c", candle.get("close"))
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid close") from exc
    if not isfinite(out) or out <= 0:
        raise ValueError("invalid close")
    return out


def _timestamp(candle: Mapping[str, object]) -> float | None:
    value = candle.get("ts", candle.get("timestamp", candle.get("time")))
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if isfinite(out) else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _moving_block_bootstrap_ci(
    values: Sequence[float],
    *,
    samples: int = 2000,
    seed: int = 19,
) -> tuple[float | None, float | None]:
    """95% moving-block bootstrap CI for mean net return."""
    vals = [float(v) for v in values if isfinite(float(v))]
    n = len(vals)
    if n < 2 or samples <= 0:
        return None, None
    block = max(2, min(n, int(round(sqrt(n)))))
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(int(samples)):
        draw: list[float] = []
        while len(draw) < n:
            start = rng.randrange(n)
            for j in range(block):
                draw.append(vals[(start + j) % n])
                if len(draw) >= n:
                    break
        means.append(sum(draw) / n)
    means.sort()
    lo = means[max(0, int(0.025 * (len(means) - 1)))]
    hi = means[min(len(means) - 1, int(0.975 * (len(means) - 1)))]
    return lo, hi


def summarize_oos_points(
    symbol: str,
    points: Iterable[OOSPoint],
    *,
    horizon: int,
    step: int,
    round_trip_cost_fraction: float,
    bootstrap_samples: int = 2000,
    seed: int = 19,
) -> MarketLanguageOOSReport:
    vals = tuple(points)
    evaluated = len(vals)
    available_points = [p for p in vals if p.available and p.net_directional_return is not None]
    available = len(available_points)
    coverage = available / evaluated if evaluated else 0.0

    if evaluated:
        actual = [1.0 if p.actual_up else 0.0 for p in vals]
        probs = [max(0.0, min(1.0, float(p.probability_up))) for p in vals]
        brier = sum((p - y) ** 2 for p, y in zip(probs, actual)) / evaluated
        climatology = sum(actual) / evaluated
        climatology_brier = sum((climatology - y) ** 2 for y in actual) / evaluated
        brier_skill = 1.0 - brier / climatology_brier if climatology_brier > 0 else None
    else:
        brier = climatology_brier = brier_skill = None

    gross = [float(p.gross_directional_return) for p in available_points]
    net = [float(p.net_directional_return) for p in available_points]
    correct = [
        (p.direction > 0 and p.actual_return > 0)
        or (p.direction < 0 and p.actual_return < 0)
        for p in available_points
    ]
    ci_low, ci_high = _moving_block_bootstrap_ci(net, samples=bootstrap_samples, seed=seed)

    return MarketLanguageOOSReport(
        symbol=str(symbol),
        evaluated=evaluated,
        available=available,
        coverage=coverage,
        horizon=int(horizon),
        step=int(step),
        round_trip_cost_fraction=float(round_trip_cost_fraction),
        brier_up=brier,
        climatology_brier=climatology_brier,
        brier_skill=brier_skill,
        directional_accuracy_when_available=(sum(bool(x) for x in correct) / len(correct) if correct else None),
        mean_gross_directional_return=_mean(gross),
        mean_net_directional_return=_mean(net),
        median_net_directional_return=median(net) if net else None,
        net_win_rate=(sum(v > 0 for v in net) / len(net)) if net else None,
        bootstrap_ci_low_net_return=ci_low,
        bootstrap_ci_high_net_return=ci_high,
        points=vals,
    )


def evaluate_market_language_oos(
    candles: Sequence[Mapping[str, object]],
    *,
    symbol: str,
    warmup: int = 240,
    horizon: int = 4,
    step: int | None = None,
    sample_count: int = 96,
    cost_fraction: float | None = None,
    bootstrap_samples: int = 2000,
    seed: int = 19,
) -> MarketLanguageOOSReport:
    """Strict prefix-only, non-overlapping OOS evaluation."""
    rows = list(candles)
    horizon = int(horizon)
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    step = horizon if step is None else int(step)
    if step < horizon:
        raise ValueError("step must be >= horizon to keep OOS labels non-overlapping")
    if warmup < 81:
        raise ValueError("warmup must be >= 81")
    cost = (
        2.0 * LEGACY_CONSERVATIVE_TAKER_FEE + 2.0 * static_slippage_rate(symbol)
        if cost_fraction is None
        else float(cost_fraction)
    )
    if not isfinite(cost) or cost < 0 or cost >= 0.20:
        raise ValueError("invalid round-trip cost fraction")

    points: list[OOSPoint] = []
    for cut in range(int(warmup), len(rows) - horizon + 1, step):
        prefix = rows[:cut]
        forecast = forecast_market_language(prefix, horizon=horizon, sample_count=sample_count)
        reference = _close(prefix[-1])
        future = _close(rows[cut + horizon - 1])
        actual_return = future / reference - 1.0
        actual_up = actual_return > 0.0

        direction = 0
        gross = net = None
        if forecast.available:
            if forecast.probability_up > forecast.probability_down:
                direction = 1
            elif forecast.probability_down > forecast.probability_up:
                direction = -1
            if direction:
                gross = direction * actual_return
                net = gross - cost

        points.append(OOSPoint(
            index=cut,
            timestamp=_timestamp(prefix[-1]),
            probability_up=float(forecast.probability_up),
            available=bool(forecast.available and direction),
            actual_return=actual_return,
            actual_up=actual_up,
            direction=direction,
            gross_directional_return=gross,
            net_directional_return=net,
        ))

    return summarize_oos_points(
        symbol,
        points,
        horizon=horizon,
        step=step,
        round_trip_cost_fraction=cost,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
    )


def market_language_promotion_decision(
    report: MarketLanguageOOSReport,
    *,
    min_evaluated: int = 200,
    min_available: int = 75,
    min_coverage: float = 0.15,
    min_brier_skill: float = 0.0,
) -> tuple[bool, tuple[str, ...]]:
    """Fail closed; this is evidence policy, never runtime promotion itself."""
    blockers: list[str] = []
    if report.evaluated < min_evaluated:
        blockers.append("INSUFFICIENT_OOS_SAMPLE")
    if report.available < min_available:
        blockers.append("INSUFFICIENT_AVAILABLE_SIGNAL_SAMPLE")
    if report.coverage < min_coverage:
        blockers.append("OOS_COVERAGE_TOO_LOW")
    if report.brier_skill is None or report.brier_skill <= min_brier_skill:
        blockers.append("NO_POSITIVE_BRIER_SKILL")
    if report.mean_net_directional_return is None or report.mean_net_directional_return <= 0.0:
        blockers.append("NO_POSITIVE_NET_RETURN")
    if report.bootstrap_ci_low_net_return is None or report.bootstrap_ci_low_net_return <= 0.0:
        blockers.append("NET_RETURN_NOT_STATISTICALLY_POSITIVE")
    return (not blockers, tuple(blockers))
