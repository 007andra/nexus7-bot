"""Leakage-safe regime-transition feature construction.

Only contemporaneous and lagged features are emitted. Future outcome labels may
be joined later by the research pipeline, never computed here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True)
class RegimePoint:
    timestamp: float
    adx: float
    ema_spread_pct: float
    volatility_pct: float
    momentum_pct: float
    regime: str

    def validate(self) -> "RegimePoint":
        if not self.regime:
            raise ValueError("regime required")
        if not all(
            math.isfinite(float(value))
            for value in (
                self.timestamp,
                self.adx,
                self.ema_spread_pct,
                self.volatility_pct,
                self.momentum_pct,
            )
        ):
            raise ValueError("non-finite regime point")
        return self


def build_transition_rows(
    points: Iterable[RegimePoint],
) -> tuple[dict[str, object], ...]:
    vals = sorted(
        (point.validate() for point in points),
        key=lambda point: point.timestamp,
    )
    output = []
    for previous, current in zip(vals, vals[1:]):
        if current.timestamp <= previous.timestamp:
            raise ValueError("regime timestamps must be strictly increasing")
        output.append({
            "timestamp": current.timestamp,
            "from_regime": previous.regime,
            "to_regime": current.regime,
            "regime_changed": previous.regime != current.regime,
            "adx": current.adx,
            "adx_delta": current.adx - previous.adx,
            "ema_spread_pct": current.ema_spread_pct,
            "ema_spread_delta": (
                current.ema_spread_pct - previous.ema_spread_pct
            ),
            "volatility_pct": current.volatility_pct,
            "volatility_delta": (
                current.volatility_pct - previous.volatility_pct
            ),
            "momentum_pct": current.momentum_pct,
            "momentum_delta": (
                current.momentum_pct - previous.momentum_pct
            ),
            "future_features_used": False,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        })
    return tuple(output)
