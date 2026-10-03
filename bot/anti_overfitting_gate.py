"""Anti-overfitting diagnostics for NEXUS research.

Provides multiple-testing awareness and threshold-sensitivity diagnostics.
Outputs evidence only; it cannot promote a feature or mutate LIVE settings.
"""
from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Iterable, Mapping


def benjamini_hochberg(
    p_values: Mapping[str, float],
    *,
    false_discovery_rate: float = 0.05,
) -> dict[str, dict[str, float | bool]]:
    if not 0 < false_discovery_rate < 1:
        raise ValueError("false_discovery_rate must be in (0,1)")
    ordered = sorted(
        (
            (name, float(value))
            for name, value in p_values.items()
        ),
        key=lambda item: (item[1], item[0]),
    )
    if any(not math.isfinite(value) or not 0 <= value <= 1 for _, value in ordered):
        raise ValueError("p-values must be finite in [0,1]")
    m = len(ordered)
    if m == 0:
        return {}

    largest_rejected = 0
    for rank, (_, value) in enumerate(ordered, 1):
        if value <= rank / m * false_discovery_rate:
            largest_rejected = rank

    adjusted = [0.0] * m
    running = 1.0
    for index in range(m - 1, -1, -1):
        rank = index + 1
        raw = ordered[index][1] * m / rank
        running = min(running, raw)
        adjusted[index] = min(1.0, running)

    return {
        name: {
            "p_value": value,
            "adjusted_p_value": adjusted[index],
            "discovery": index + 1 <= largest_rejected,
        }
        for index, (name, value) in enumerate(ordered)
    }


def threshold_sensitivity(
    threshold_to_expectancy: Mapping[float, float],
) -> dict[str, float | bool | int | None]:
    points = sorted(
        (float(threshold), float(expectancy))
        for threshold, expectancy in threshold_to_expectancy.items()
    )
    if len(points) < 3:
        raise ValueError("at least three thresholds are required")
    if any(
        not math.isfinite(threshold) or not math.isfinite(expectancy)
        for threshold, expectancy in points
    ):
        raise ValueError("non-finite threshold sensitivity input")

    values = [expectancy for _, expectancy in points]
    positive_share = sum(value > 0 for value in values) / len(values)
    center = mean(values)
    dispersion = pstdev(values)
    relative_dispersion = (
        dispersion / abs(center)
        if abs(center) > 1e-12
        else None
    )
    sign_changes = sum(
        1
        for left, right in zip(values, values[1:])
        if (left > 0) != (right > 0)
    )
    return {
        "points": len(points),
        "positive_share": positive_share,
        "mean_expectancy_r": center,
        "expectancy_dispersion_r": dispersion,
        "relative_dispersion": relative_dispersion,
        "sign_changes": sign_changes,
        "locally_stable": positive_share >= 0.8 and sign_changes <= 1,
        "promotion_authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def robustness_gate(
    *,
    temporal_positive_share: float,
    symbol_positive_share: float,
    side_positive_share: float,
    slippage_stress_positive: bool,
    fee_stress_positive: bool,
    delayed_entry_positive: bool,
    alternative_thresholds_stable: bool,
) -> tuple[bool, tuple[str, ...]]:
    blockers: list[str] = []
    for name, value in (
        ("TEMPORAL_INSTABILITY", temporal_positive_share),
        ("SYMBOL_INSTABILITY", symbol_positive_share),
        ("SIDE_INSTABILITY", side_positive_share),
    ):
        if not 0 <= float(value) <= 1:
            raise ValueError("positive shares must be in [0,1]")
        if float(value) < 0.6:
            blockers.append(name)
    if not slippage_stress_positive:
        blockers.append("SLIPPAGE_STRESS_FAIL")
    if not fee_stress_positive:
        blockers.append("FEE_STRESS_FAIL")
    if not delayed_entry_positive:
        blockers.append("DELAYED_ENTRY_FAIL")
    if not alternative_thresholds_stable:
        blockers.append("THRESHOLD_SENSITIVITY_FAIL")
    return not blockers, tuple(blockers)
