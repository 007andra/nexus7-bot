"""Out-of-sample model validation primitives for NEXUS-7.

Pure analytics only: no exchange access, no order routing and no runtime
threshold mutation. The goal is to measure whether NEXUS confidence and
expectancy survive chronological, purged out-of-sample validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ValidationRow:
    timestamp: float
    confidence: float  # probability-like value in [0, 1]
    outcome: int       # 1 win, 0 non-win
    r_multiple: float
    # When known, the timestamp at which the outcome label became observable.
    # This lets research splits purge overlapping future-label information
    # exactly instead of approximating leakage with a candidate count.
    label_end_timestamp: float | None = None

    def validate(self) -> "ValidationRow":
        if not isfinite(self.timestamp):
            raise ValueError("timestamp must be finite")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        if self.outcome not in (0, 1):
            raise ValueError("outcome must be 0 or 1")
        if not isfinite(self.r_multiple):
            raise ValueError("r_multiple must be finite")
        if self.label_end_timestamp is not None:
            if (
                not isfinite(self.label_end_timestamp)
                or self.label_end_timestamp < self.timestamp
            ):
                raise ValueError("invalid label_end_timestamp")
        return self


@dataclass(frozen=True)
class WalkForwardFold:
    train: tuple[ValidationRow, ...]
    test: tuple[ValidationRow, ...]


@dataclass(frozen=True)
class CalibrationReport:
    n: int
    brier: float
    ece: float
    win_rate: float
    expectancy_r: float
    calibration_slope: float
    log_loss: float = 0.0
    calibration_intercept: float = 0.0


def purged_walk_forward(
    rows: Sequence[ValidationRow],
    *,
    train_size: int,
    test_size: int,
    purge_size: int = 0,
    step_size: int | None = None,
) -> list[WalkForwardFold]:
    """Chronological walk-forward splits with an embargo/purge gap.

    The purge removes observations immediately before each test window from the
    train slice, reducing leakage when labels depend on overlapping future
    returns. Rows are sorted by timestamp and never shuffled.
    """
    if train_size <= 0 or test_size <= 0 or purge_size < 0:
        raise ValueError("invalid walk-forward sizes")
    step = test_size if step_size is None else step_size
    if step <= 0:
        raise ValueError("step_size must be positive")

    ordered = tuple(sorted((r.validate() for r in rows), key=lambda r: r.timestamp))
    folds: list[WalkForwardFold] = []
    test_start = train_size + purge_size
    while test_start + test_size <= len(ordered):
        train_end = test_start - purge_size
        train_start = max(0, train_end - train_size)
        train = ordered[train_start:train_end]
        test = ordered[test_start:test_start + test_size]
        if len(train) == train_size and len(test) == test_size:
            folds.append(WalkForwardFold(train=train, test=test))
        test_start += step
    return folds


def label_aware_purged_embargo_walk_forward(
    rows: Sequence[ValidationRow],
    *,
    train_size: int,
    test_size: int,
    embargo_size: int = 0,
    step_size: int | None = None,
) -> list[WalkForwardFold]:
    """Chronological folds purged by actual label availability plus embargo.

    A training row is eligible only when its outcome label was observable
    strictly before the first decision timestamp of the test fold. Rows in the
    post-test embargo region are permanently excluded from later training.
    """
    if train_size <= 0 or test_size <= 0 or embargo_size < 0:
        raise ValueError("invalid label-aware walk-forward sizes")
    step = (
        test_size + embargo_size
        if step_size is None
        else int(step_size)
    )
    if step < test_size + embargo_size:
        raise ValueError("step_size must cover test_size + embargo_size")

    ordered = tuple(
        sorted((row.validate() for row in rows), key=lambda row: row.timestamp)
    )
    folds: list[WalkForwardFold] = []
    embargoed_indices: set[int] = set()
    test_start = train_size

    while test_start + test_size <= len(ordered):
        test_end = test_start + test_size
        test = ordered[test_start:test_end]
        test_start_ts = float(test[0].timestamp)

        eligible = []
        for index, row in enumerate(ordered[:test_start]):
            if index in embargoed_indices:
                continue
            label_end = (
                float(row.label_end_timestamp)
                if row.label_end_timestamp is not None
                else float(row.timestamp)
            )
            if label_end < test_start_ts:
                eligible.append(row)

        train = tuple(eligible[-train_size:])
        if len(train) < train_size:
            # Move one observation at a time until enough labels are actually
            # known. A non-emitted fold must not create an embargo region.
            test_start += 1
            continue

        folds.append(
            WalkForwardFold(
                train=train,
                test=tuple(test),
            )
        )
        embargo_end = min(len(ordered), test_end + embargo_size)
        embargoed_indices.update(range(test_end, embargo_end))
        test_start += step

    return folds


def brier_score(rows: Iterable[ValidationRow]) -> float:
    vals = [r.validate() for r in rows]
    if not vals:
        raise ValueError("empty validation sample")
    return sum((r.confidence - r.outcome) ** 2 for r in vals) / len(vals)


def expected_calibration_error(rows: Iterable[ValidationRow], bins: int = 10) -> float:
    vals = [r.validate() for r in rows]
    if not vals:
        raise ValueError("empty validation sample")
    if bins <= 0:
        raise ValueError("bins must be positive")
    total = len(vals)
    err = 0.0
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        bucket = [r for r in vals if (lo <= r.confidence < hi) or (i == bins - 1 and r.confidence == 1.0)]
        if not bucket:
            continue
        avg_conf = sum(r.confidence for r in bucket) / len(bucket)
        avg_outcome = sum(r.outcome for r in bucket) / len(bucket)
        err += (len(bucket) / total) * abs(avg_conf - avg_outcome)
    return err


def log_loss(rows: Iterable[ValidationRow], epsilon: float = 1e-12) -> float:
    """Binary cross-entropy with probability clipping for numerical stability."""
    import math
    vals = [r.validate() for r in rows]
    if not vals:
        raise ValueError("empty validation sample")
    if not 0.0 < epsilon < 0.5:
        raise ValueError("epsilon must be in (0,0.5)")
    total = 0.0
    for row in vals:
        p = min(1.0 - epsilon, max(epsilon, row.confidence))
        total += -(row.outcome * math.log(p) + (1 - row.outcome) * math.log(1.0 - p))
    return total / len(vals)


def calibration_linear_fit(rows: Iterable[ValidationRow]) -> tuple[float, float]:
    """OLS intercept/slope diagnostic of outcome on confidence."""
    vals = [r.validate() for r in rows]
    if len(vals) < 2:
        return 0.0, 0.0
    mx = sum(r.confidence for r in vals) / len(vals)
    my = sum(r.outcome for r in vals) / len(vals)
    var = sum((r.confidence - mx) ** 2 for r in vals)
    if var <= 0:
        return my, 0.0
    cov = sum((r.confidence - mx) * (r.outcome - my) for r in vals)
    slope = cov / var
    return my - slope * mx, slope


def calibration_slope(rows: Iterable[ValidationRow]) -> float:
    """Simple OLS slope of outcome on confidence; 1 is ideal, 0 uninformative."""
    return calibration_linear_fit(rows)[1]


def reliability_bins(rows: Iterable[ValidationRow], bins: int = 10) -> tuple[dict, ...]:
    """Return auditable reliability-curve buckets without interpolation."""
    vals = [r.validate() for r in rows]
    if not vals:
        raise ValueError("empty validation sample")
    if bins <= 0:
        raise ValueError("bins must be positive")
    result = []
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        bucket = [
            r for r in vals
            if (lo <= r.confidence < hi)
            or (i == bins - 1 and r.confidence == 1.0)
        ]
        if not bucket:
            continue
        result.append({
            "bin": i,
            "low": lo,
            "high": hi,
            "n": len(bucket),
            "mean_confidence": sum(r.confidence for r in bucket) / len(bucket),
            "observed_rate": sum(r.outcome for r in bucket) / len(bucket),
        })
    return tuple(result)


def report(rows: Iterable[ValidationRow], bins: int = 10) -> CalibrationReport:
    vals = [r.validate() for r in rows]
    if not vals:
        raise ValueError("empty validation sample")
    intercept, slope = calibration_linear_fit(vals)
    return CalibrationReport(
        n=len(vals),
        brier=brier_score(vals),
        ece=expected_calibration_error(vals, bins=bins),
        win_rate=sum(r.outcome for r in vals) / len(vals),
        expectancy_r=sum(r.r_multiple for r in vals) / len(vals),
        calibration_slope=slope,
        log_loss=log_loss(vals),
        calibration_intercept=intercept,
    )


def aggregate_oos_report(folds: Sequence[WalkForwardFold], bins: int = 10) -> CalibrationReport:
    test_rows = [row for fold in folds for row in fold.test]
    if not test_rows:
        raise ValueError("no out-of-sample rows")
    return report(test_rows, bins=bins)


def promotion_decision(
    rep: CalibrationReport,
    *,
    min_samples: int = 100,
    max_brier: float = 0.24,
    max_ece: float = 0.10,
    min_expectancy_r: float = 0.0,
    min_slope: float = 0.20,
) -> tuple[bool, tuple[str, ...]]:
    """Fail-closed evidence gate for promoting a calibrated confidence model.

    This function does not mutate runtime configuration. It only returns an
    auditable decision and blockers.
    """
    blockers: list[str] = []
    if rep.n < min_samples:
        blockers.append("INSUFFICIENT_OOS_SAMPLE")
    if rep.brier > max_brier:
        blockers.append("BRIER_TOO_HIGH")
    if rep.ece > max_ece:
        blockers.append("CALIBRATION_ERROR_TOO_HIGH")
    if rep.expectancy_r <= min_expectancy_r:
        blockers.append("NON_POSITIVE_EXPECTANCY")
    if rep.calibration_slope < min_slope:
        blockers.append("CONFIDENCE_NOT_INFORMATIVE")
    return (not blockers, tuple(blockers))
