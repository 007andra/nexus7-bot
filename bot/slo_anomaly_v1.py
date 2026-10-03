"""Operational SLO and robust-anomaly diagnostics.

Read-only evaluation for market freshness, ACK, DB, protection, reconciliation,
WS and ownership health.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from typing import Mapping, Sequence


@dataclass(frozen=True)
class SLOThresholds:
    market_freshness_ms: float
    ack_latency_ms: float
    db_latency_ms: float
    protection_latency_ms: float
    min_ws_uptime: float
    min_reconciliation_success: float
    min_lease_remaining_seconds: float


def evaluate_slo(
    metrics: Mapping[str, float],
    thresholds: SLOThresholds,
) -> dict[str, object]:
    expected = {
        "market_freshness_ms",
        "ack_latency_ms",
        "db_latency_ms",
        "protection_latency_ms",
        "ws_uptime",
        "reconciliation_success",
        "lease_remaining_seconds",
    }
    missing = sorted(expected - set(metrics))
    if missing:
        raise ValueError(f"missing SLO metrics: {missing}")
    if any(not math.isfinite(float(metrics[key])) for key in expected):
        raise ValueError("non-finite SLO metric")

    checks = {
        "market_freshness": (
            metrics["market_freshness_ms"]
            <= thresholds.market_freshness_ms
        ),
        "ack_latency": (
            metrics["ack_latency_ms"] <= thresholds.ack_latency_ms
        ),
        "db_latency": (
            metrics["db_latency_ms"] <= thresholds.db_latency_ms
        ),
        "protection_latency": (
            metrics["protection_latency_ms"]
            <= thresholds.protection_latency_ms
        ),
        "ws_uptime": metrics["ws_uptime"] >= thresholds.min_ws_uptime,
        "reconciliation": (
            metrics["reconciliation_success"]
            >= thresholds.min_reconciliation_success
        ),
        "ownership_lease": (
            metrics["lease_remaining_seconds"]
            >= thresholds.min_lease_remaining_seconds
        ),
    }
    return {
        "all_slos_met": all(checks.values()),
        "checks": checks,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def robust_anomaly_score(
    history: Sequence[float],
    current: float,
) -> dict[str, float | bool]:
    vals = [float(value) for value in history]
    if len(vals) < 5 or any(not math.isfinite(value) for value in vals):
        raise ValueError("at least five finite history values required")
    current = float(current)
    if not math.isfinite(current):
        raise ValueError("current must be finite")

    center = median(vals)
    deviations = [abs(value - center) for value in vals]
    mad = median(deviations)
    if mad <= 1e-12:
        score = 0.0 if abs(current - center) <= 1e-12 else float("inf")
    else:
        score = 0.6745 * abs(current - center) / mad
    return {
        "median": center,
        "mad": mad,
        "robust_z": score,
        "anomaly": score >= 3.5,
    }
