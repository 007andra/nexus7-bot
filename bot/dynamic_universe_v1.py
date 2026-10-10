"""Cross-sectional dynamic-universe diagnostics for NEXUS research.

Ranks market quality from liquidity, spread, OI, funding stability, execution
quality and data reliability. Rankings never add/remove LIVE symbols.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True)
class MarketQuality:
    symbol: str
    volume_usdt: float
    spread_bps: float
    depth_10bps_usdt: float
    open_interest_usdt: float
    funding_rate_abs: float
    realized_vol_pct: float
    execution_quality: float
    data_reliability: float
    history_days: float

    def validate(self) -> "MarketQuality":
        if not self.symbol:
            raise ValueError("symbol required")
        values = (
            self.volume_usdt,
            self.spread_bps,
            self.depth_10bps_usdt,
            self.open_interest_usdt,
            self.funding_rate_abs,
            self.realized_vol_pct,
            self.execution_quality,
            self.data_reliability,
            self.history_days,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("non-finite market quality")
        if min(values) < 0:
            raise ValueError("market quality values cannot be negative")
        if not 0 <= self.execution_quality <= 1:
            raise ValueError("execution_quality must be in [0,1]")
        if not 0 <= self.data_reliability <= 1:
            raise ValueError("data_reliability must be in [0,1]")
        return self


def _percentile_ranks(
    rows: list[MarketQuality],
    attr: str,
    *,
    higher_is_better: bool = True,
) -> dict[str, float]:
    ordered = sorted(
        rows,
        key=lambda row: (
            float(getattr(row, attr)),
            row.symbol,
        ),
    )
    n = len(ordered)
    if n == 1:
        return {ordered[0].symbol: 1.0}

    ranks: dict[str, float] = {}
    for index, row in enumerate(ordered):
        rank = index / (n - 1)
        ranks[row.symbol] = rank if higher_is_better else 1.0 - rank
    return ranks


def rank_universe(
    markets: Iterable[MarketQuality],
    *,
    min_history_days: float = 30.0,
    min_data_reliability: float = 0.95,
) -> tuple[dict[str, object], ...]:
    rows = [row.validate() for row in markets]
    if not rows:
        return ()
    if len({row.symbol for row in rows}) != len(rows):
        raise ValueError("duplicate symbol")

    volume = _percentile_ranks(rows, "volume_usdt")
    spread = _percentile_ranks(rows, "spread_bps", higher_is_better=False)
    depth = _percentile_ranks(rows, "depth_10bps_usdt")
    oi = _percentile_ranks(rows, "open_interest_usdt")
    funding = _percentile_ranks(
        rows,
        "funding_rate_abs",
        higher_is_better=False,
    )
    vol_percentile = _percentile_ranks(rows, "realized_vol_pct")

    output = []
    for row in rows:
        # Mid-distribution volatility scores highest. This is a market-quality
        # prior only; empirical edge by volatility bucket remains separate.
        vol_utility = 1.0 - abs(vol_percentile[row.symbol] - 0.5) * 2.0
        score = (
            0.20 * volume[row.symbol]
            + 0.20 * depth[row.symbol]
            + 0.10 * oi[row.symbol]
            + 0.15 * spread[row.symbol]
            + 0.10 * row.execution_quality
            + 0.15 * row.data_reliability
            + 0.05 * funding[row.symbol]
            + 0.05 * vol_utility
        )
        eligible_for_research = (
            row.history_days >= min_history_days
            and row.data_reliability >= min_data_reliability
        )
        output.append({
            "symbol": row.symbol,
            "market_quality_score": round(score, 12),
            "eligible_for_research": eligible_for_research,
            "history_days": row.history_days,
            "data_reliability": row.data_reliability,
            "promotion_authority": False,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        })

    output.sort(
        key=lambda item: (
            -float(item["market_quality_score"]),
            str(item["symbol"]),
        )
    )
    for index, item in enumerate(output, 1):
        item["research_rank"] = index
    return tuple(output)
