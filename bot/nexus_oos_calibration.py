"""Leakage-safe probability calibration for NEXUS research.

Calibrators are fit on train folds only and evaluated on untouched OOS rows.
Nothing in this module changes LIVE probabilities, thresholds, sizing or orders.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from bot.oos_model_validation import (
    ValidationRow,
    WalkForwardFold,
    brier_score,
    expected_calibration_error,
    log_loss,
)


_EPS = 1e-6


def _clip_probability(value: float) -> float:
    p = float(value)
    if not math.isfinite(p) or not 0.0 <= p <= 1.0:
        raise ValueError("probability must be finite and within [0,1]")
    return min(1.0 - _EPS, max(_EPS, p))


def _logit(value: float) -> float:
    p = _clip_probability(value)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _training_rows(rows: Iterable[ValidationRow]) -> tuple[ValidationRow, ...]:
    values = tuple(row.validate() for row in rows)
    if len(values) < 4:
        raise ValueError("calibration requires at least four train observations")
    labels = {row.outcome for row in values}
    if labels != {0, 1}:
        raise ValueError("calibration train fold requires both outcome classes")
    return values


@dataclass(frozen=True)
class PlattCalibrator:
    intercept: float
    slope: float

    def predict(self, probability: float) -> float:
        return _sigmoid(self.intercept + self.slope * _logit(probability))


def fit_platt(
    rows: Iterable[ValidationRow],
    *,
    l2: float = 1e-4,
    max_iter: int = 100,
    tolerance: float = 1e-10,
) -> PlattCalibrator:
    """Fit logistic scaling on logit(raw probability) with Newton updates."""
    values = _training_rows(rows)
    if l2 < 0 or max_iter <= 0 or tolerance <= 0:
        raise ValueError("invalid Platt optimization settings")

    xs = [_logit(row.confidence) for row in values]
    ys = [float(row.outcome) for row in values]
    mean_y = min(1.0 - _EPS, max(_EPS, sum(ys) / len(ys)))
    intercept = math.log(mean_y / (1.0 - mean_y))
    slope = 1.0

    for _ in range(int(max_iter)):
        g0 = g1 = 0.0
        h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            p = _sigmoid(intercept + slope * x)
            error = p - y
            weight = max(p * (1.0 - p), 1e-12)
            g0 += error
            g1 += error * x
            h00 += weight
            h01 += weight * x
            h11 += weight * x * x

        g0 += l2 * intercept
        g1 += l2 * slope
        h00 += l2
        h11 += l2

        determinant = h00 * h11 - h01 * h01
        if not math.isfinite(determinant) or determinant <= 1e-18:
            raise ValueError("Platt calibration Hessian is singular")

        step0 = (h11 * g0 - h01 * g1) / determinant
        step1 = (-h01 * g0 + h00 * g1) / determinant
        intercept -= step0
        slope -= step1

        if not all(math.isfinite(v) for v in (intercept, slope)):
            raise ValueError("Platt calibration diverged")
        if max(abs(step0), abs(step1)) < tolerance:
            break

    return PlattCalibrator(intercept=intercept, slope=slope)


@dataclass(frozen=True)
class IsotonicCalibrator:
    upper_bounds: tuple[float, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.upper_bounds or len(self.upper_bounds) != len(self.values):
            raise ValueError("invalid isotonic calibrator")
        if any(a >= b for a, b in zip(self.upper_bounds, self.upper_bounds[1:])):
            raise ValueError("isotonic upper bounds must be strictly increasing")
        if any(not 0.0 <= value <= 1.0 for value in self.values):
            raise ValueError("isotonic values must be probabilities")
        if any(a > b for a, b in zip(self.values, self.values[1:])):
            raise ValueError("isotonic values must be monotone")

    def predict(self, probability: float) -> float:
        p = float(probability)
        if not math.isfinite(p) or not 0.0 <= p <= 1.0:
            raise ValueError("probability must be finite and within [0,1]")
        idx = bisect.bisect_left(self.upper_bounds, p)
        if idx >= len(self.values):
            idx = len(self.values) - 1
        return float(self.values[idx])


def fit_isotonic(rows: Iterable[ValidationRow]) -> IsotonicCalibrator:
    """Pool-adjacent-violators fit over raw probabilities."""
    values = _training_rows(rows)
    grouped: list[dict] = []
    for row in sorted(values, key=lambda item: (item.confidence, item.timestamp)):
        x = float(row.confidence)
        y = float(row.outcome)
        if grouped and math.isclose(grouped[-1]["x_max"], x, abs_tol=1e-15):
            grouped[-1]["sum_y"] += y
            grouped[-1]["weight"] += 1
            grouped[-1]["value"] = (
                grouped[-1]["sum_y"] / grouped[-1]["weight"]
            )
        else:
            grouped.append({
                "x_min": x,
                "x_max": x,
                "sum_y": y,
                "weight": 1,
                "value": y,
            })

    blocks: list[dict] = []
    for group in grouped:
        blocks.append(dict(group))
        while len(blocks) >= 2 and blocks[-2]["value"] > blocks[-1]["value"]:
            right = blocks.pop()
            left = blocks.pop()
            weight = left["weight"] + right["weight"]
            sum_y = left["sum_y"] + right["sum_y"]
            blocks.append({
                "x_min": left["x_min"],
                "x_max": right["x_max"],
                "sum_y": sum_y,
                "weight": weight,
                "value": sum_y / weight,
            })

    return IsotonicCalibrator(
        upper_bounds=tuple(float(block["x_max"]) for block in blocks),
        values=tuple(float(block["value"]) for block in blocks),
    )


def _evaluate(
    rows: Sequence[ValidationRow],
    probabilities: Sequence[float],
    *,
    bins: int,
) -> dict:
    if len(rows) != len(probabilities) or not rows:
        raise ValueError("calibration evaluation rows/probabilities mismatch")
    calibrated = [
        ValidationRow(
            timestamp=row.timestamp,
            confidence=float(probability),
            outcome=row.outcome,
            r_multiple=row.r_multiple,
        ).validate()
        for row, probability in zip(rows, probabilities)
    ]
    return {
        "n": len(calibrated),
        "brier": brier_score(calibrated),
        "log_loss": log_loss(calibrated),
        "ece": expected_calibration_error(calibrated, bins=bins),
    }


def calibrate_walk_forward(
    folds: Sequence[WalkForwardFold],
    *,
    method: str = "platt",
    bins: int = 10,
) -> dict:
    """Fit each fold on train only, then evaluate raw vs calibrated test rows."""
    name = str(method).strip().lower()
    if name not in {"platt", "isotonic"}:
        raise ValueError("method must be platt or isotonic")
    if not folds:
        raise ValueError("no walk-forward folds")

    reports = []
    pooled_rows: list[ValidationRow] = []
    pooled_raw: list[float] = []
    pooled_calibrated: list[float] = []

    for index, fold in enumerate(folds):
        train = tuple(row.validate() for row in fold.train)
        test = tuple(row.validate() for row in fold.test)
        if not test:
            raise ValueError("empty OOS calibration fold")

        calibrator = fit_platt(train) if name == "platt" else fit_isotonic(train)
        raw_probabilities = [row.confidence for row in test]
        calibrated_probabilities = [
            calibrator.predict(row.confidence) for row in test
        ]
        raw_metrics = _evaluate(test, raw_probabilities, bins=bins)
        calibrated_metrics = _evaluate(
            test, calibrated_probabilities, bins=bins
        )

        reports.append({
            "fold": index,
            "train_n": len(train),
            "test_n": len(test),
            "train_end_ts": max(row.timestamp for row in train),
            "test_start_ts": min(row.timestamp for row in test),
            "raw": raw_metrics,
            "calibrated": calibrated_metrics,
            "calibrator": (
                {
                    "intercept": calibrator.intercept,
                    "slope": calibrator.slope,
                }
                if isinstance(calibrator, PlattCalibrator)
                else {
                    "upper_bounds": calibrator.upper_bounds,
                    "values": calibrator.values,
                }
            ),
        })
        pooled_rows.extend(test)
        pooled_raw.extend(raw_probabilities)
        pooled_calibrated.extend(calibrated_probabilities)

    if any(
        report["train_end_ts"] >= report["test_start_ts"]
        for report in reports
    ):
        raise ValueError("calibration fold violates chronology")

    return {
        "method": name,
        "folds": reports,
        "pooled_raw": _evaluate(pooled_rows, pooled_raw, bins=bins),
        "pooled_calibrated": _evaluate(
            pooled_rows, pooled_calibrated, bins=bins
        ),
        "fit_scope": "TRAIN_ONLY",
        "evaluation_scope": "OOS_ONLY",
        "execution_effect": "NONE",
        "promotion_authority": False,
    }
