"""Read-only feature and regime drift evaluation for SHADOW/research."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from bot.feature_contract import (
    categorical_total_variation,
    drift_level,
    population_stability_index,
)


@dataclass(frozen=True)
class DriftMetric:
    name: str
    metric: str
    value: float
    level: str


def evaluate_drift(
    reference_numeric: Mapping[str, Sequence[float]],
    current_numeric: Mapping[str, Sequence[float]],
    *,
    reference_categorical: Mapping[str, Sequence[str]] | None = None,
    current_categorical: Mapping[str, Sequence[str]] | None = None,
) -> dict:
    metrics: list[DriftMetric] = []
    numeric_names = sorted(set(reference_numeric) | set(current_numeric))
    for name in numeric_names:
        if name not in reference_numeric or name not in current_numeric:
            metrics.append(DriftMetric(name, "PSI", float("inf"), "SEVERE"))
            continue
        value = population_stability_index(
            reference_numeric[name], current_numeric[name]
        )
        metrics.append(DriftMetric(name, "PSI", value, drift_level(value)))

    ref_cat = reference_categorical or {}
    cur_cat = current_categorical or {}
    categorical_names = sorted(set(ref_cat) | set(cur_cat))
    for name in categorical_names:
        if name not in ref_cat or name not in cur_cat:
            metrics.append(DriftMetric(name, "TVD", 1.0, "SEVERE"))
            continue
        value = categorical_total_variation(ref_cat[name], cur_cat[name])
        level = "STABLE" if value < 0.10 else ("WATCH" if value < 0.25 else "DRIFT")
        metrics.append(DriftMetric(name, "TVD", value, level))

    severity_order = {"STABLE": 0, "WATCH": 1, "DRIFT": 2, "SEVERE": 3}
    worst = max(
        (metric.level for metric in metrics),
        key=lambda level: severity_order[level],
        default="STABLE",
    )
    return {
        "status": worst,
        "metrics": [metric.__dict__ for metric in metrics],
        "execution_effect": "NONE",
    }
