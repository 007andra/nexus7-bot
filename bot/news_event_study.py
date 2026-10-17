"""Leakage-safe market event-study primitives for NEXUS research."""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import mean, pstdev
from typing import Iterable


@dataclass(frozen=True)
class MarketPoint:
    timestamp: float
    price: float
    spread_bps: float = 0.0

    def validate(self) -> "MarketPoint":
        if not all(
            math.isfinite(float(value))
            for value in (self.timestamp, self.price, self.spread_bps)
        ):
            raise ValueError("non-finite market point")
        if self.price <= 0 or self.spread_bps < 0:
            raise ValueError("invalid market point")
        return self


@dataclass(frozen=True)
class Event:
    timestamp: float
    event_type: str

    def validate(self) -> "Event":
        if not math.isfinite(float(self.timestamp)) or not self.event_type:
            raise ValueError("invalid event")
        return self


def _nearest_before(points, timestamp):
    candidates = [point for point in points if point.timestamp <= timestamp]
    return candidates[-1] if candidates else None


def _nearest_after(points, timestamp):
    return next(
        (point for point in points if point.timestamp >= timestamp),
        None,
    )


def event_window(
    event: Event,
    market: Iterable[MarketPoint],
    *,
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, object]:
    event.validate()
    points = sorted(
        (point.validate() for point in market),
        key=lambda point: point.timestamp,
    )
    if pre_seconds <= 0 or post_seconds <= 0:
        raise ValueError("event windows must be positive")

    pre = _nearest_before(points, event.timestamp - pre_seconds)
    at = _nearest_after(points, event.timestamp)
    post = _nearest_after(points, event.timestamp + post_seconds)
    if pre is None or at is None or post is None:
        raise ValueError("insufficient event-window market history")

    window = [
        point
        for point in points
        if pre.timestamp <= point.timestamp <= post.timestamp
    ]
    returns = [
        math.log(right.price / left.price)
        for left, right in zip(window, window[1:])
    ]
    realized_vol = pstdev(returns) if len(returns) >= 2 else 0.0
    max_price = max(point.price for point in window)
    min_price = min(point.price for point in window)
    range_pct = (max_price - min_price) / at.price * 100.0

    return {
        "event_type": event.event_type,
        "event_timestamp": event.timestamp,
        "pre_return_pct": (at.price / pre.price - 1.0) * 100.0,
        "post_return_pct": (post.price / at.price - 1.0) * 100.0,
        "window_range_pct": range_pct,
        "realized_log_return_vol": realized_vol,
        "spread_at_event_bps": at.spread_bps,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def aggregate_event_study(
    windows: Iterable[dict[str, object]],
) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for row in windows:
        groups.setdefault(str(row["event_type"]), []).append(row)

    result = {}
    for event_type, rows in sorted(groups.items()):
        result[event_type] = {
            "n": len(rows),
            "mean_pre_return_pct": mean(
                float(row["pre_return_pct"]) for row in rows
            ),
            "mean_post_return_pct": mean(
                float(row["post_return_pct"]) for row in rows
            ),
            "mean_window_range_pct": mean(
                float(row["window_range_pct"]) for row in rows
            ),
            "mean_spread_at_event_bps": mean(
                float(row["spread_at_event_bps"]) for row in rows
            ),
        }
    return result
