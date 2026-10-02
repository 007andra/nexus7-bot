"""Versioned feature schema, reproducibility fingerprint and drift metrics.

This module is deliberately read-only with respect to trading decisions.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    dtype: str = "float"
    nullable: bool = False
    source: str = "nexus"


@dataclass(frozen=True)
class FeatureSchema:
    version: str
    features: tuple[FeatureSpec, ...]

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("feature schema version required")
        names = [f.name for f in self.features]
        if len(names) != len(set(names)):
            raise ValueError("duplicate feature name")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.features)

    def validate(self, values: Mapping[str, object]) -> dict[str, object]:
        unknown = set(values) - set(self.names)
        if unknown:
            raise ValueError(f"unknown features: {sorted(unknown)}")
        normalized: dict[str, object] = {}
        for spec in self.features:
            value = values.get(spec.name)
            if value is None:
                if spec.nullable:
                    normalized[spec.name] = None
                    continue
                raise ValueError(f"missing feature: {spec.name}")
            if spec.dtype == "float":
                try:
                    value = float(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"feature {spec.name} is not float") from exc
                if not math.isfinite(value):
                    raise ValueError(f"feature {spec.name} is non-finite")
            elif spec.dtype == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"feature {spec.name} is not int") from exc
            elif spec.dtype == "str":
                value = str(value)
            elif spec.dtype == "bool":
                if not isinstance(value, bool):
                    raise ValueError(f"feature {spec.name} is not bool")
            else:
                raise ValueError(f"unsupported dtype: {spec.dtype}")
            normalized[spec.name] = value
        return normalized

    def fingerprint(self, values: Mapping[str, object], *, symbol: str, decision_ts: int) -> str:
        payload = {
            "schema": self.version,
            "symbol": str(symbol).upper(),
            "decision_ts": int(decision_ts),
            "values": self.validate(values),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def population_stability_index(
    reference: Sequence[float],
    current: Sequence[float],
    *,
    bins: int = 10,
    epsilon: float = 1e-6,
) -> float:
    """PSI with reference quantile bins. Larger values imply distribution shift."""
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    if ref.ndim != 1 or cur.ndim != 1 or not ref.size or not cur.size:
        raise ValueError("reference/current must be non-empty vectors")
    if not np.isfinite(ref).all() or not np.isfinite(cur).all():
        raise ValueError("non-finite drift input")
    if bins < 2:
        raise ValueError("bins must be >= 2")
    edges = np.unique(np.quantile(ref, np.linspace(0.0, 1.0, bins + 1)))
    if edges.size < 3:
        return 0.0 if np.allclose(np.mean(ref), np.mean(cur)) else float("inf")
    edges[0], edges[-1] = -np.inf, np.inf
    ref_counts, _ = np.histogram(ref, bins=edges)
    cur_counts, _ = np.histogram(cur, bins=edges)
    ref_pct = np.maximum(ref_counts / ref.size, epsilon)
    cur_pct = np.maximum(cur_counts / cur.size, epsilon)
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def categorical_total_variation(reference: Sequence[str], current: Sequence[str]) -> float:
    if not reference or not current:
        raise ValueError("reference/current must be non-empty")
    labels = sorted(set(reference) | set(current))
    r = {k: 0 for k in labels}
    c = {k: 0 for k in labels}
    for value in reference:
        r[value] += 1
    for value in current:
        c[value] += 1
    return 0.5 * sum(abs(r[k] / len(reference) - c[k] / len(current)) for k in labels)


def drift_level(psi: float) -> str:
    if not math.isfinite(psi):
        return "SEVERE"
    if psi < 0.10:
        return "STABLE"
    if psi < 0.25:
        return "WATCH"
    return "DRIFT"
