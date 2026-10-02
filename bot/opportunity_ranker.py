"""Shadow-only cross-symbol opportunity ranking.

The ranker compares research candidates. It does not authorize, size or dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Opportunity:
    candidate_id: str
    symbol: str
    expected_value: float
    net_rr: float
    setup_score: float
    liquidity_score: float
    regime_confidence: float
    round_trip_cost: float

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.symbol:
            raise ValueError("candidate identity required")
        for name in ("setup_score", "liquidity_score", "regime_confidence"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 100.0:
                raise ValueError(f"{name} must be in [0,100]")
        if self.round_trip_cost < 0:
            raise ValueError("round_trip_cost cannot be negative")


def rank_score(item: Opportunity) -> float:
    """Dimensionless shadow ranking score; higher is better."""
    ev_term = max(min(item.expected_value * 100.0, 5.0), -5.0) / 5.0
    rr_term = max(min((item.net_rr - 1.0) / 2.0, 1.0), -1.0)
    setup = item.setup_score / 100.0
    liquidity = item.liquidity_score / 100.0
    regime = item.regime_confidence / 100.0
    cost_penalty = min(item.round_trip_cost * 100.0 / 0.50, 1.0)
    return (
        0.30 * ev_term
        + 0.20 * rr_term
        + 0.20 * setup
        + 0.15 * liquidity
        + 0.15 * regime
        - 0.20 * cost_penalty
    )


def rank_opportunities(items: list[Opportunity]) -> list[tuple[Opportunity, float]]:
    ranked = [(item, rank_score(item)) for item in items]
    return sorted(ranked, key=lambda pair: (-pair[1], pair[0].candidate_id))
