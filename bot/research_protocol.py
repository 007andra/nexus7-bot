"""Institutional research protocol primitives for NEXUS.

Pure analytics: no exchange mutation and no runtime threshold changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable

import numpy as np

from bot.research_statistics import (
    bootstrap_mean_ci,
    monte_carlo_trade_paths,
    performance_metrics,
)
from bot.research_walk_forward import purged_walk_forward


@dataclass(frozen=True)
class ResearchObservation:
    timestamp: float
    symbol: str
    side: str
    regime: str
    volatility: float
    net_return: float

    def validate(self) -> "ResearchObservation":
        if not isfinite(float(self.timestamp)):
            raise ValueError("non-finite observation timestamp")
        if not self.symbol:
            raise ValueError("missing observation symbol")
        if self.side.upper() not in {"LONG", "SHORT"}:
            raise ValueError("invalid observation side")
        if not isfinite(float(self.volatility)) or float(self.volatility) < 0:
            raise ValueError("invalid observation volatility")
        if not isfinite(float(self.net_return)) or float(self.net_return) <= -1:
            raise ValueError("invalid observation return")
        return self


def _ordered(rows: Iterable[ResearchObservation]) -> tuple[ResearchObservation, ...]:
    values = sorted(
        (row.validate() for row in rows),
        key=lambda row: (row.timestamp, row.symbol, row.side),
    )
    seen: set[tuple[float, str, str]] = set()
    for row in values:
        key = (float(row.timestamp), row.symbol, row.side.upper())
        if key in seen:
            raise ValueError("duplicate research observation")
        seen.add(key)
    return tuple(values)


def volatility_tercile_labels(rows: Iterable[ResearchObservation]) -> tuple[str, ...]:
    values = _ordered(rows)
    if not values:
        return tuple()
    vols = np.asarray([float(row.volatility) for row in values], dtype=float)
    q1, q2 = np.quantile(vols, [1.0 / 3.0, 2.0 / 3.0])
    labels = []
    for value in vols:
        if value <= q1:
            labels.append("LOW")
        elif value <= q2:
            labels.append("MID")
        else:
            labels.append("HIGH")
    return tuple(labels)


def _metric_pack(returns: list[float], *, bootstrap_samples: int, seed: int) -> dict:
    base = performance_metrics(returns)
    ci = bootstrap_mean_ci(
        returns, n_bootstrap=bootstrap_samples, seed=seed
    ) if returns else {"mean": 0.0, "low": 0.0, "high": 0.0, "n": 0}
    return {**base, "expectancy_ci95": ci}


def segmented_report(
    rows: Iterable[ResearchObservation],
    *,
    bootstrap_samples: int = 2000,
    seed: int = 42,
) -> dict:
    values = _ordered(rows)
    labels = volatility_tercile_labels(values)
    dimensions: dict[str, dict[str, list[float]]] = {
        "symbol": {},
        "side": {},
        "regime": {},
        "volatility_tercile": {},
    }
    for row, vol_label in zip(values, labels):
        pairs = (
            ("symbol", row.symbol),
            ("side", row.side.upper()),
            ("regime", row.regime or "UNKNOWN"),
            ("volatility_tercile", vol_label),
        )
        for dim, key in pairs:
            dimensions[dim].setdefault(key, []).append(float(row.net_return))

    segments: dict[str, dict] = {}
    for dim, groups in dimensions.items():
        segments[dim] = {
            key: _metric_pack(vals, bootstrap_samples=bootstrap_samples, seed=seed)
            for key, vals in sorted(groups.items())
        }
    aggregate_returns = [float(row.net_return) for row in values]
    return {
        "n": len(values),
        "aggregate": _metric_pack(
            aggregate_returns, bootstrap_samples=bootstrap_samples, seed=seed
        ),
        "segments": segments,
    }


def walk_forward_oos_report(
    rows: Iterable[ResearchObservation],
    *,
    train_size: int,
    test_size: int,
    purge: int = 0,
    embargo: int = 0,
    bootstrap_samples: int = 2000,
    monte_carlo_paths: int = 2000,
    seed: int = 42,
) -> dict:
    values = _ordered(rows)
    windows = purged_walk_forward(
        len(values),
        train_size=train_size,
        test_size=test_size,
        purge=purge,
        embargo=embargo,
    )
    fold_reports = []
    all_oos: list[ResearchObservation] = []
    for index, window in enumerate(windows):
        test_rows = values[window.test_slice]
        all_oos.extend(test_rows)
        fold_reports.append({
            "fold": index,
            "train_start": window.train_start,
            "train_end": window.train_end,
            "test_start": window.test_start,
            "test_end": window.test_end,
            "report": segmented_report(
                test_rows,
                bootstrap_samples=bootstrap_samples,
                seed=seed + index,
            ),
        })

    returns = [float(row.net_return) for row in all_oos]
    monte_carlo = monte_carlo_trade_paths(
        returns,
        paths=monte_carlo_paths,
        seed=seed,
    )
    return {
        "fold_count": len(windows),
        "oos_n": len(all_oos),
        "folds": fold_reports,
        "aggregate_oos": segmented_report(
            all_oos,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
        ) if all_oos else {"n": 0, "aggregate": {}, "segments": {}},
        "monte_carlo": monte_carlo.__dict__,
        "protocol": {
            "purged": purge > 0,
            "embargo": embargo,
            "chronological": True,
            "shuffled": False,
        },
    }
