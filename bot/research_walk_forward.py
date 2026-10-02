"""Deterministic research split and temporal-leakage primitives.

Research-only module. It has no exchange, runtime, sizing or dispatch authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    purge: int
    embargo: int

    def __post_init__(self) -> None:
        if min(self.train_start, self.train_end, self.test_start, self.test_end) < 0:
            raise ValueError("negative walk-forward boundary")
        if not self.train_start < self.train_end <= self.test_start < self.test_end:
            raise ValueError("invalid chronological walk-forward window")
        if self.test_start - self.train_end < self.purge:
            raise ValueError("purge gap violated")

    @property
    def train_slice(self) -> slice:
        return slice(self.train_start, self.train_end)

    @property
    def test_slice(self) -> slice:
        return slice(self.test_start, self.test_end)


def purged_walk_forward(
    n_samples: int,
    *,
    train_size: int,
    test_size: int,
    purge: int = 0,
    embargo: int = 0,
    step_size: int | None = None,
    expanding: bool = False,
) -> list[WalkForwardWindow]:
    """Create chronological train/test windows with explicit purge and embargo."""
    values = (n_samples, train_size, test_size, purge, embargo)
    if any(int(v) != v or v < 0 for v in values):
        raise ValueError("walk-forward arguments must be non-negative integers")
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")
    step = test_size + embargo if step_size is None else int(step_size)
    if step <= 0:
        raise ValueError("step_size must be positive")
    if step < test_size + embargo:
        raise ValueError("step_size must cover test_size + embargo")

    windows: list[WalkForwardWindow] = []
    offset = 0
    while True:
        train_start = 0 if expanding else offset
        train_end = train_size + offset
        test_start = train_end + purge
        test_end = test_start + test_size
        if test_end > n_samples:
            break
        windows.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                purge=purge,
                embargo=embargo,
            )
        )
        offset += step
    return windows


def assert_no_window_overlap(windows: Sequence[WalkForwardWindow]) -> None:
    """Reject test overlap and embargo violations across consecutive windows."""
    previous: WalkForwardWindow | None = None
    for current in windows:
        if previous is not None:
            if current.test_start < previous.test_end + previous.embargo:
                raise ValueError("test window violates prior embargo")
            if current.train_end > current.test_start - current.purge:
                raise ValueError("training data enters purge region")
        previous = current


def validate_temporal_contract(
    rows: Iterable[Mapping[str, object]],
    *,
    decision_ts_key: str = "decision_ts",
    feature_ts_keys: Sequence[str] = ("feature_ts",),
    label_ts_keys: Sequence[str] = ("label_ts",),
) -> None:
    """Fail if a research row can see the future."""
    for idx, row in enumerate(rows):
        try:
            decision = int(row[decision_ts_key])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"row {idx}: invalid decision timestamp") from exc
        for key in feature_ts_keys:
            if key not in row:
                raise ValueError(f"row {idx}: missing feature timestamp {key}")
            try:
                feature_ts = int(row[key])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"row {idx}: invalid feature timestamp {key}") from exc
            if feature_ts > decision:
                raise ValueError(f"row {idx}: lookahead feature {key}")
        for key in label_ts_keys:
            if key not in row:
                raise ValueError(f"row {idx}: missing label timestamp {key}")
            try:
                label_ts = int(row[key])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"row {idx}: invalid label timestamp {key}") from exc
            if label_ts <= decision:
                raise ValueError(f"row {idx}: non-future label {key}")
