"""Research-only opportunity ranker v2.

Ranks candidates for capital efficiency without authorizing or vetoing LIVE
trades. Inputs are normalized explicitly so ranking remains auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from math import isfinite
from typing import Iterable


@dataclass(frozen=True)
class Opportunity:
    candidate_id: str
    symbol: str
    side: str
    net_ev_r: float
    calibrated_win_probability: float
    liquidity_score: float
    spread_bps: float
    expected_slippage_bps: float
    diversification_benefit: float
    tail_risk_score: float
    margin_efficiency: float
    regime_quality: float

    def validate(self) -> "Opportunity":
        values = [
            self.net_ev_r, self.calibrated_win_probability, self.liquidity_score,
            self.spread_bps, self.expected_slippage_bps, self.diversification_benefit,
            self.tail_risk_score, self.margin_efficiency, self.regime_quality,
        ]
        if not self.candidate_id or not self.symbol:
            raise ValueError("candidate_id and symbol are required")
        if not all(isfinite(float(v)) for v in values):
            raise ValueError("non-finite opportunity input")
        for name in ("calibrated_win_probability", "liquidity_score", "diversification_benefit", "tail_risk_score", "regime_quality"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0,1]")
        if self.spread_bps < 0 or self.expected_slippage_bps < 0 or self.margin_efficiency < 0:
            raise ValueError("costs and margin_efficiency cannot be negative")
        return self


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def rank_score(item: Opportunity) -> float:
    x = item.validate()
    ev = _clip(x.net_ev_r / 2.0, -1.0, 1.0)
    p = (x.calibrated_win_probability - 0.5) * 2.0
    cost_penalty = _clip((x.spread_bps + x.expected_slippage_bps) / 50.0, 0.0, 1.0)
    margin = _clip(x.margin_efficiency / 2.0, 0.0, 1.0)
    score = (
        0.28 * ev
        + 0.14 * p
        + 0.12 * x.regime_quality
        + 0.12 * x.liquidity_score
        + 0.12 * x.diversification_benefit
        + 0.12 * margin
        - 0.06 * cost_penalty
        - 0.04 * x.tail_risk_score
    )
    return round(score, 12)


def rank_opportunities(items: Iterable[Opportunity]) -> tuple[dict[str, object], ...]:
    rows = []
    seen: set[str] = set()
    for item in items:
        item.validate()
        if item.candidate_id in seen:
            raise ValueError("duplicate candidate_id")
        seen.add(item.candidate_id)
        rows.append({**asdict(item), "rank_score": rank_score(item)})
    rows.sort(key=lambda row: (-float(row["rank_score"]), str(row["candidate_id"])))
    for idx, row in enumerate(rows, 1):
        row["research_rank"] = idx
        row["promotion_authority"] = False
        row["decision_effect"] = "NONE"
        row["execution_effect"] = "NONE"
    return tuple(rows)
