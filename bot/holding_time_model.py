"""Empirical holding-time model for funding/capital-efficiency research."""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from typing import Iterable


@dataclass(frozen=True)
class HoldingObservation:
    setup: str
    regime: str
    symbol: str
    side: str
    volatility_bucket: str
    hold_seconds: float

    def validate(self) -> "HoldingObservation":
        if not all(
            str(value).strip()
            for value in (
                self.setup,
                self.regime,
                self.symbol,
                self.side,
                self.volatility_bucket,
            )
        ):
            raise ValueError("holding-time dimensions are required")
        if not math.isfinite(self.hold_seconds) or self.hold_seconds < 0:
            raise ValueError("invalid hold_seconds")
        return self


@dataclass(frozen=True)
class HoldingTimeModel:
    exact: dict[tuple[str, str, str, str, str], tuple[int, float]]
    setup_regime: dict[tuple[str, str], tuple[int, float]]
    global_n: int
    global_median_seconds: float
    min_bucket_n: int

    def predict(
        self,
        *,
        setup: str,
        regime: str,
        symbol: str,
        side: str,
        volatility_bucket: str,
    ) -> dict[str, object]:
        exact_key = (
            setup.upper(),
            regime.upper(),
            symbol.upper(),
            side.upper(),
            volatility_bucket.upper(),
        )
        exact = self.exact.get(exact_key)
        if exact and exact[0] >= self.min_bucket_n:
            source = "EXACT_BUCKET"
            n, seconds = exact
        else:
            coarse = self.setup_regime.get(
                (setup.upper(), regime.upper())
            )
            if coarse and coarse[0] >= self.min_bucket_n:
                source = "SETUP_REGIME"
                n, seconds = coarse
            else:
                source = "GLOBAL"
                n, seconds = self.global_n, self.global_median_seconds
        return {
            "expected_hold_seconds": seconds,
            "source": source,
            "sample_n": n,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        }


def fit_holding_time_model(
    observations: Iterable[HoldingObservation],
    *,
    min_bucket_n: int = 20,
) -> HoldingTimeModel:
    vals = [item.validate() for item in observations]
    if not vals:
        raise ValueError("holding observations required")
    if min_bucket_n <= 0:
        raise ValueError("min_bucket_n must be positive")

    exact_raw: dict[tuple[str, str, str, str, str], list[float]] = {}
    coarse_raw: dict[tuple[str, str], list[float]] = {}
    all_values: list[float] = []
    for item in vals:
        exact_key = (
            item.setup.upper(),
            item.regime.upper(),
            item.symbol.upper(),
            item.side.upper(),
            item.volatility_bucket.upper(),
        )
        coarse_key = (item.setup.upper(), item.regime.upper())
        exact_raw.setdefault(exact_key, []).append(item.hold_seconds)
        coarse_raw.setdefault(coarse_key, []).append(item.hold_seconds)
        all_values.append(item.hold_seconds)

    exact = {
        key: (len(values), median(values))
        for key, values in exact_raw.items()
    }
    coarse = {
        key: (len(values), median(values))
        for key, values in coarse_raw.items()
    }
    return HoldingTimeModel(
        exact=exact,
        setup_regime=coarse,
        global_n=len(all_values),
        global_median_seconds=median(all_values),
        min_bucket_n=int(min_bucket_n),
    )
