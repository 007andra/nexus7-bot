"""Spoof/iceberg evidence candidates for research only.

These are microstructure diagnostics, not claims that manipulation occurred.
Predictive value must be established OOS before any use beyond telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from typing import Iterable


@dataclass(frozen=True)
class LevelObservation:
    timestamp: float
    side: str
    price: float
    displayed_qty: float
    traded_qty_at_level: float = 0.0

    def validate(self) -> "LevelObservation":
        if self.side.upper() not in {"BID", "ASK"}:
            raise ValueError("side must be BID or ASK")
        vals = (
            self.timestamp,
            self.price,
            self.displayed_qty,
            self.traded_qty_at_level,
        )
        if not all(math.isfinite(float(value)) for value in vals):
            raise ValueError("non-finite level observation")
        if self.price <= 0 or self.displayed_qty < 0 or self.traded_qty_at_level < 0:
            raise ValueError("invalid level values")
        return self


def analyze_level_sequence(
    observations: Iterable[LevelObservation],
    *,
    max_spoof_lifetime_seconds: float = 5.0,
) -> dict[str, object]:
    vals = sorted(
        (item.validate() for item in observations),
        key=lambda item: item.timestamp,
    )
    if len(vals) < 3:
        raise ValueError("at least three level observations required")
    side = vals[0].side.upper()
    price = vals[0].price
    if any(
        item.side.upper() != side or not math.isclose(item.price, price)
        for item in vals
    ):
        raise ValueError("observations must describe one price level")

    quantities = [item.displayed_qty for item in vals]
    baseline = median(quantities)
    peak = max(quantities)
    peak_index = quantities.index(peak)
    after = vals[peak_index + 1 :]
    disappearance = min(
        (item.displayed_qty for item in after),
        default=peak,
    )
    lifetime = (
        vals[-1].timestamp - vals[peak_index].timestamp
        if after
        else float("inf")
    )
    traded_after_peak = sum(
        item.traded_qty_at_level for item in after
    )

    large_appearance = peak >= max(1e-12, baseline * 3.0)
    rapid_disappearance = (
        after
        and lifetime <= max_spoof_lifetime_seconds
        and disappearance <= peak * 0.2
    )
    low_execution = traded_after_peak <= peak * 0.1
    spoof_candidate = bool(
        large_appearance and rapid_disappearance and low_execution
    )

    total_traded = sum(item.traded_qty_at_level for item in vals)
    median_display = max(1e-12, median(quantities))
    repeated_replenishment_proxy = (
        total_traded >= median_display * 3.0
        and min(quantities) >= median_display * 0.5
    )

    return {
        "side": side,
        "price": price,
        "baseline_displayed_qty": baseline,
        "peak_displayed_qty": peak,
        "traded_qty": total_traded,
        "spoof_candidate": spoof_candidate,
        "iceberg_replenishment_proxy": repeated_replenishment_proxy,
        "diagnostic_only": True,
        "predictive_value": "NOT_PROVEN",
        "promotion_authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
