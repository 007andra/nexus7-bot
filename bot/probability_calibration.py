"""Train-only probability calibration for NEXUS research.

This module calibrates heuristic NEXUS confidence without changing LIVE
probabilities or thresholds. Every fitted model is trained exclusively on the
training side of a chronological purged walk-forward fold and is applied only
to that fold's OOS test rows.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

from bot.oos_model_validation import (
    CalibrationReport,
    ValidationRow,
    purged_walk_forward,
    report,
)


_EPS = 1e-6


def _clip_probability(value: float) -> float:
    p = float(value)
    if not math.isfinite(p):
        raise ValueError("non-finite probability")
    return min(1.0 - _EPS, max(_EPS, p))


def _logit(value: float) -> float:
    p = _clip_probability(value)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    x = max(-40.0, min(40.0, float(value)))
    return 1.0 / (1.0 + math.exp(-x))


@dataclass(frozen=True)
class PlattModel:
    slope: float
    intercept: float
    train_n: int
    train_positive_rate: float
    iterations: int
    converged: bool

    def predict(self, confidence: float) -> float:
        return _sigmoid(
            self.intercept + self.slope * _logit(confidence)
        )


def fit_platt(
    rows: Iterable[ValidationRow],
    *,
    l2: float = 1e-3,
    max_iter: int = 100,
    tolerance: float = 1e-9,
) -> PlattModel:
    """Fit logistic calibration on logit(raw confidence) via Newton updates."""
    values = [row.validate() for row in rows]
    if len(values) < 20:
        raise ValueError("Platt calibration requires at least 20 training rows")
    positives = sum(row.outcome for row in values)
    if positives <= 0 or positives >= len(values):
        raise ValueError("Platt calibration requires both outcome classes")
    if l2 < 0 or max_iter <= 0 or tolerance <= 0:
        raise ValueError("invalid Platt optimizer settings")

    xs = [_logit(row.confidence) for row in values]
    ys = [float(row.outcome) for row in values]
    positive_rate = positives / len(values)

    slope = 1.0
    intercept = math.log(positive_rate / (1.0 - positive_rate))
    converged = False

    for iteration in range(1, int(max_iter) + 1):
        g_slope = float(l2) * slope
        g_intercept = 0.0
        h_ss = float(l2)
        h_si = 0.0
        h_ii = 0.0

        for x, y in zip(xs, ys):
            pred = _sigmoid(intercept + slope * x)
            err = pred - y
            weight = max(pred * (1.0 - pred), 1e-9)
            g_slope += err * x
            g_intercept += err
            h_ss += weight * x * x
            h_si += weight * x
            h_ii += weight

        det = h_ss * h_ii - h_si * h_si
        if not math.isfinite(det) or abs(det) < 1e-15:
            raise ValueError("singular Platt calibration Hessian")

        step_slope = (h_ii * g_slope - h_si * g_intercept) / det
        step_intercept = (-h_si * g_slope + h_ss * g_intercept) / det
        if not all(math.isfinite(v) for v in (step_slope, step_intercept)):
            raise ValueError("non-finite Platt optimizer step")

        # Cap Newton jumps so separation cannot explode the optimizer.
        step_slope = max(-5.0, min(5.0, step_slope))
        step_intercept = max(-5.0, min(5.0, step_intercept))
        slope -= step_slope
        intercept -= step_intercept

        if max(abs(step_slope), abs(step_intercept)) <= tolerance:
            converged = True
            break

    if not all(math.isfinite(v) for v in (slope, intercept)):
        raise ValueError("non-finite Platt model")

    return PlattModel(
        slope=float(slope),
        intercept=float(intercept),
        train_n=len(values),
        train_positive_rate=float(positive_rate),
        iterations=iteration,
        converged=converged,
    )


def apply_platt(
    model: PlattModel,
    rows: Iterable[ValidationRow],
) -> tuple[ValidationRow, ...]:
    """Apply a fixed train-fitted model; outcome/R-multiple are not inputs."""
    calibrated = []
    for row in rows:
        item = row.validate()
        calibrated.append(ValidationRow(
            timestamp=item.timestamp,
            confidence=model.predict(item.confidence),
            outcome=item.outcome,
            r_multiple=item.r_multiple,
        ))
    return tuple(calibrated)


@dataclass(frozen=True)
class CalibrationFoldEvidence:
    fold: int
    train_start_ts: float
    train_end_ts: float
    test_start_ts: float
    test_end_ts: float
    model: PlattModel
    raw_report: CalibrationReport
    calibrated_report: CalibrationReport


def walk_forward_platt_report(
    rows: Sequence[ValidationRow],
    *,
    train_size: int,
    test_size: int,
    purge_size: int = 0,
    minimum_folds: int = 4,
    bins: int = 10,
) -> dict:
    """Fit on each train window and evaluate calibrated probabilities OOS only."""
    if minimum_folds <= 0:
        raise ValueError("minimum_folds must be positive")
    folds = purged_walk_forward(
        rows,
        train_size=train_size,
        test_size=test_size,
        purge_size=purge_size,
        step_size=test_size,
    )

    evidence: list[CalibrationFoldEvidence] = []
    raw_oos: list[ValidationRow] = []
    calibrated_oos: list[ValidationRow] = []
    blockers: list[str] = []

    for index, fold in enumerate(folds):
        try:
            model = fit_platt(fold.train)
        except ValueError as exc:
            blockers.append(f"FOLD_{index}_CALIBRATION_FAILED:{type(exc).__name__}")
            continue
        calibrated = apply_platt(model, fold.test)
        raw_oos.extend(fold.test)
        calibrated_oos.extend(calibrated)
        evidence.append(CalibrationFoldEvidence(
            fold=index,
            train_start_ts=fold.train[0].timestamp,
            train_end_ts=fold.train[-1].timestamp,
            test_start_ts=fold.test[0].timestamp,
            test_end_ts=fold.test[-1].timestamp,
            model=model,
            raw_report=report(fold.test, bins=bins),
            calibrated_report=report(calibrated, bins=bins),
        ))

    if len(evidence) < minimum_folds:
        blockers.append("INSUFFICIENT_CALIBRATED_OOS_FOLDS")
    if not calibrated_oos:
        blockers.append("EMPTY_CALIBRATED_OOS")

    raw_aggregate = report(raw_oos, bins=bins) if raw_oos else None
    calibrated_aggregate = (
        report(calibrated_oos, bins=bins) if calibrated_oos else None
    )

    return {
        "fold_count": len(evidence),
        "evidence_complete": not blockers,
        "evidence_blockers": tuple(blockers),
        "raw_oos": asdict(raw_aggregate) if raw_aggregate else None,
        "calibrated_oos": (
            asdict(calibrated_aggregate) if calibrated_aggregate else None
        ),
        "folds": [
            {
                "fold": item.fold,
                "train_start_ts": item.train_start_ts,
                "train_end_ts": item.train_end_ts,
                "test_start_ts": item.test_start_ts,
                "test_end_ts": item.test_end_ts,
                "model": asdict(item.model),
                "raw_report": asdict(item.raw_report),
                "calibrated_report": asdict(item.calibrated_report),
            }
            for item in evidence
        ],
        "live_probability_effect": "NONE",
        "promotion_authority": False,
    }
