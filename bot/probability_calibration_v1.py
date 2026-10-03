"""Leakage-aware probability calibration primitives for NEXUS research.

Supports isotonic regression and Platt scaling. Fit/evaluation samples are
explicitly separated and candidate IDs may not overlap. No calibrated value is
fed into LIVE decisions by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


_EPS = 1e-6


@dataclass(frozen=True)
class CalibrationSample:
    candidate_id: str
    probability: float
    outcome: int
    timestamp: float

    def validate(self) -> "CalibrationSample":
        if not self.candidate_id:
            raise ValueError("candidate_id required")
        if not math.isfinite(float(self.probability)) or not 0 <= self.probability <= 1:
            raise ValueError("probability must be in [0,1]")
        if self.outcome not in (0, 1):
            raise ValueError("outcome must be 0 or 1")
        if not math.isfinite(float(self.timestamp)):
            raise ValueError("timestamp must be finite")
        return self


@dataclass(frozen=True)
class IsotonicCalibrator:
    upper_bounds: tuple[float, ...]
    values: tuple[float, ...]

    def predict(self, probability: float) -> float:
        p = _clip_probability(probability)
        for upper, value in zip(self.upper_bounds, self.values):
            if p <= upper:
                return value
        return self.values[-1] if self.values else p


@dataclass(frozen=True)
class PlattCalibrator:
    slope: float
    intercept: float

    def predict(self, probability: float) -> float:
        x = _logit(_clip_probability(probability))
        return _sigmoid(self.slope * x + self.intercept)


def _clip_probability(value: float) -> float:
    p = float(value)
    if not math.isfinite(p):
        raise ValueError("non-finite probability")
    return min(1.0 - _EPS, max(_EPS, p))


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def fit_isotonic(samples: Sequence[CalibrationSample]) -> IsotonicCalibrator:
    vals = sorted(
        (sample.validate() for sample in samples),
        key=lambda sample: (sample.probability, sample.timestamp, sample.candidate_id),
    )
    if len(vals) < 2:
        raise ValueError("at least two calibration samples are required")

    blocks: list[dict[str, float]] = []
    for sample in vals:
        blocks.append({
            "upper": float(sample.probability),
            "sum": float(sample.outcome),
            "count": 1.0,
        })
        while len(blocks) >= 2:
            left = blocks[-2]
            right = blocks[-1]
            left_mean = left["sum"] / left["count"]
            right_mean = right["sum"] / right["count"]
            if left_mean <= right_mean:
                break
            merged = {
                "upper": right["upper"],
                "sum": left["sum"] + right["sum"],
                "count": left["count"] + right["count"],
            }
            blocks[-2:] = [merged]

    return IsotonicCalibrator(
        upper_bounds=tuple(block["upper"] for block in blocks),
        values=tuple(block["sum"] / block["count"] for block in blocks),
    )


def fit_platt(
    samples: Sequence[CalibrationSample],
    *,
    max_iter: int = 100,
    l2: float = 1e-6,
) -> PlattCalibrator:
    vals = [sample.validate() for sample in samples]
    if len(vals) < 3:
        raise ValueError("at least three calibration samples are required")
    if l2 < 0:
        raise ValueError("l2 cannot be negative")

    a, b = 1.0, 0.0
    for _ in range(max_iter):
        g_a = g_b = 0.0
        h_aa = h_ab = h_bb = 0.0
        for sample in vals:
            x = _logit(_clip_probability(sample.probability))
            q = _sigmoid(a * x + b)
            error = float(sample.outcome) - q
            weight = max(_EPS, q * (1.0 - q))
            g_a += error * x
            g_b += error
            h_aa += weight * x * x
            h_ab += weight * x
            h_bb += weight

        h_aa += l2
        h_bb += l2
        det = h_aa * h_bb - h_ab * h_ab
        if det <= 1e-15:
            break
        delta_a = (g_a * h_bb - g_b * h_ab) / det
        delta_b = (h_aa * g_b - h_ab * g_a) / det
        a += delta_a
        b += delta_b
        if max(abs(delta_a), abs(delta_b)) < 1e-8:
            break

    if not math.isfinite(a) or not math.isfinite(b):
        raise ValueError("Platt fit diverged")
    return PlattCalibrator(a, b)


def brier_score(
    samples: Iterable[CalibrationSample],
    calibrator=None,
) -> float:
    vals = [sample.validate() for sample in samples]
    if not vals:
        raise ValueError("empty calibration sample")
    total = 0.0
    for sample in vals:
        p = (
            calibrator.predict(sample.probability)
            if calibrator is not None
            else sample.probability
        )
        total += (p - sample.outcome) ** 2
    return total / len(vals)


def expected_calibration_error(
    samples: Iterable[CalibrationSample],
    *,
    calibrator=None,
    bins: int = 10,
) -> float:
    vals = [sample.validate() for sample in samples]
    if not vals or bins <= 0:
        raise ValueError("samples and positive bins required")
    total = len(vals)
    error = 0.0
    for index in range(bins):
        lo = index / bins
        hi = (index + 1) / bins
        bucket = []
        for sample in vals:
            p = (
                calibrator.predict(sample.probability)
                if calibrator is not None
                else sample.probability
            )
            if lo <= p < hi or (index == bins - 1 and p == 1.0):
                bucket.append((p, sample.outcome))
        if bucket:
            avg_p = sum(item[0] for item in bucket) / len(bucket)
            avg_y = sum(item[1] for item in bucket) / len(bucket)
            error += len(bucket) / total * abs(avg_p - avg_y)
    return error


def fit_and_evaluate(
    train: Sequence[CalibrationSample],
    test: Sequence[CalibrationSample],
) -> dict[str, object]:
    train_vals = [sample.validate() for sample in train]
    test_vals = [sample.validate() for sample in test]
    train_ids = {sample.candidate_id for sample in train_vals}
    test_ids = {sample.candidate_id for sample in test_vals}
    overlap = train_ids & test_ids
    if overlap:
        raise ValueError("train/test candidate overlap")

    isotonic = fit_isotonic(train_vals)
    platt = fit_platt(train_vals)
    raw_brier = brier_score(test_vals)
    raw_ece = expected_calibration_error(test_vals)
    iso_brier = brier_score(test_vals, isotonic)
    iso_ece = expected_calibration_error(test_vals, calibrator=isotonic)
    platt_brier = brier_score(test_vals, platt)
    platt_ece = expected_calibration_error(test_vals, calibrator=platt)

    candidates = [
        ("isotonic", isotonic, iso_brier, iso_ece),
        ("platt", platt, platt_brier, platt_ece),
    ]
    selected_name, selected, selected_brier, selected_ece = min(
        candidates,
        key=lambda item: (item[2], item[3], item[0]),
    )
    return {
        "train_n": len(train_vals),
        "test_n": len(test_vals),
        "raw_brier": raw_brier,
        "raw_ece": raw_ece,
        "isotonic_brier": iso_brier,
        "isotonic_ece": iso_ece,
        "platt_brier": platt_brier,
        "platt_ece": platt_ece,
        "selected": selected_name,
        "selected_brier": selected_brier,
        "selected_ece": selected_ece,
        "calibrator": selected,
        "promotion_authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
