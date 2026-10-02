"""Shadow-only cross-symbol opportunity ranking.

All ranking inputs must exist before trade entry. Realized outcomes are accepted
only by the separate evaluation helpers and never feed the rank score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence


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
    decision_ts: int = 0
    side: str = "UNKNOWN"
    confidence: float = 0.0
    microstructure_alignment: float | None = None
    taker_pressure: float | None = None
    depth_notional_1pct: float | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.symbol:
            raise ValueError("candidate identity required")
        for name in (
            "setup_score", "liquidity_score", "regime_confidence", "confidence"
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 <= value <= 100.0:
                raise ValueError(f"{name} must be in [0,100]")
        if not math.isfinite(float(self.expected_value)):
            raise ValueError("expected_value must be finite")
        if not math.isfinite(float(self.net_rr)) or self.net_rr < 0:
            raise ValueError("net_rr must be finite and non-negative")
        if not math.isfinite(float(self.round_trip_cost)) or self.round_trip_cost < 0:
            raise ValueError("round_trip_cost cannot be negative")
        if self.side not in {"UNKNOWN", "LONG", "SHORT"}:
            raise ValueError("side must be LONG, SHORT or UNKNOWN")
        if self.decision_ts < 0:
            raise ValueError("decision_ts cannot be negative")
        for name in ("microstructure_alignment", "taker_pressure"):
            value = getattr(self, name)
            if value is not None:
                value = float(value)
                if not math.isfinite(value) or not -1.0 <= value <= 1.0:
                    raise ValueError(f"{name} must be in [-1,1]")
        if self.depth_notional_1pct is not None:
            value = float(self.depth_notional_1pct)
            if not math.isfinite(value) or value < 0:
                raise ValueError("depth_notional_1pct must be non-negative")


def rank_score(item: Opportunity) -> float:
    """Dimensionless pre-trade SHADOW ranking score; higher is better."""
    ev_term = max(min(item.expected_value / 5.0, 1.0), -1.0)
    rr_term = max(min((item.net_rr - 1.0) / 2.0, 1.0), -1.0)
    setup = item.setup_score / 100.0
    liquidity = item.liquidity_score / 100.0
    regime = item.regime_confidence / 100.0
    confidence = item.confidence / 100.0
    cost_penalty = min(item.round_trip_cost / 0.005, 1.0)

    score = (
        0.24 * ev_term
        + 0.16 * rr_term
        + 0.18 * setup
        + 0.12 * liquidity
        + 0.12 * regime
        + 0.08 * confidence
        - 0.16 * cost_penalty
    )
    if item.microstructure_alignment is not None:
        score += 0.06 * float(item.microstructure_alignment)
    if item.taker_pressure is not None:
        side_sign = 1.0 if item.side == "LONG" else (-1.0 if item.side == "SHORT" else 0.0)
        score += 0.04 * side_sign * float(item.taker_pressure)
    return float(score)


def with_microstructure(item: Opportunity, snapshot) -> Opportunity:
    """Attach a complete same-symbol SHADOW snapshot without execution authority."""
    if snapshot is None or not bool(getattr(snapshot, "complete", False)):
        raise ValueError("complete microstructure snapshot required")
    if str(getattr(snapshot, "symbol", "")).upper() != item.symbol.upper():
        raise ValueError("microstructure symbol mismatch")
    if getattr(snapshot, "execution_effect", None) != "NONE":
        raise ValueError("microstructure snapshot cannot affect execution")
    if bool(getattr(snapshot, "promotion_authority", False)):
        raise ValueError("microstructure snapshot cannot promote")

    return Opportunity(
        candidate_id=item.candidate_id,
        symbol=item.symbol,
        expected_value=item.expected_value,
        net_rr=item.net_rr,
        setup_score=item.setup_score,
        liquidity_score=item.liquidity_score,
        regime_confidence=item.regime_confidence,
        round_trip_cost=item.round_trip_cost,
        decision_ts=item.decision_ts,
        side=item.side,
        confidence=item.confidence,
        microstructure_alignment=float(snapshot.microstructure_alignment),
        taker_pressure=float(snapshot.taker_pressure),
        depth_notional_1pct=float(snapshot.depth_notional_100bps),
    )


def rank_opportunities(items: Sequence[Opportunity]) -> list[tuple[Opportunity, float]]:
    ranked = [(item, rank_score(item)) for item in items]
    return sorted(
        ranked,
        key=lambda pair: (-pair[1], pair[0].candidate_id),
    )


def _percentile_scores(values: Mapping[str, float]) -> dict[str, float]:
    """Deterministic 0-100 cross-sectional percentile scores."""
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda pair: (pair[1], pair[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 50.0}
    out = {}
    for index, (key, _value) in enumerate(ordered):
        out[key] = 100.0 * index / (len(ordered) - 1)
    return out


def apply_cross_sectional_liquidity(
    items: Sequence[Opportunity],
) -> list[Opportunity]:
    """Replace liquidity score with same-timestamp depth percentiles when present."""
    raw = {
        item.candidate_id: float(item.depth_notional_1pct)
        for item in items
        if item.depth_notional_1pct is not None
    }
    percentiles = _percentile_scores(raw)
    if not percentiles:
        return list(items)
    return [
        Opportunity(
            candidate_id=item.candidate_id,
            symbol=item.symbol,
            expected_value=item.expected_value,
            net_rr=item.net_rr,
            setup_score=item.setup_score,
            liquidity_score=percentiles.get(
                item.candidate_id, item.liquidity_score
            ),
            regime_confidence=item.regime_confidence,
            round_trip_cost=item.round_trip_cost,
            decision_ts=item.decision_ts,
            side=item.side,
            confidence=item.confidence,
            microstructure_alignment=item.microstructure_alignment,
            taker_pressure=item.taker_pressure,
            depth_notional_1pct=item.depth_notional_1pct,
        )
        for item in items
    ]


def evaluate_ranked_outcomes(
    items: Sequence[Opportunity],
    realized_r: Mapping[str, float],
) -> dict:
    """Evaluate ranking quality after outcomes exist; realized R never affects rank."""
    if not items:
        return {
            "n": 0,
            "top_half_expectancy_r": None,
            "bottom_half_expectancy_r": None,
            "spread_r": None,
            "rank_outcome_spearman": None,
            "execution_effect": "NONE",
        }
    ranked = rank_opportunities(items)
    observed = [
        (item, score, float(realized_r[item.candidate_id]))
        for item, score in ranked
        if item.candidate_id in realized_r
        and math.isfinite(float(realized_r[item.candidate_id]))
    ]
    if len(observed) < 2:
        return {
            "n": len(observed),
            "top_half_expectancy_r": None,
            "bottom_half_expectancy_r": None,
            "spread_r": None,
            "rank_outcome_spearman": None,
            "execution_effect": "NONE",
        }

    split = max(1, len(observed) // 2)
    top = [row[2] for row in observed[:split]]
    bottom = [row[2] for row in observed[split:]]
    top_mean = sum(top) / len(top)
    bottom_mean = sum(bottom) / len(bottom) if bottom else None

    # Spearman via deterministic average-free ranks; candidate_id breaks ties.
    by_outcome = sorted(
        observed,
        key=lambda row: (row[2], row[0].candidate_id),
    )
    outcome_rank = {
        row[0].candidate_id: index
        for index, row in enumerate(by_outcome)
    }
    n = len(observed)
    rank_positions = list(range(n))
    outcome_positions = [
        outcome_rank[row[0].candidate_id]
        for row in observed
    ]
    mean_rank = (n - 1) / 2.0
    numerator = sum(
        (a - mean_rank) * (b - mean_rank)
        for a, b in zip(rank_positions, outcome_positions)
    )
    denominator = math.sqrt(
        sum((a - mean_rank) ** 2 for a in rank_positions)
        * sum((b - mean_rank) ** 2 for b in outcome_positions)
    )
    spearman = numerator / denominator if denominator > 0 else 0.0

    return {
        "n": n,
        "top_half_expectancy_r": top_mean,
        "bottom_half_expectancy_r": bottom_mean,
        "spread_r": (
            top_mean - bottom_mean
            if bottom_mean is not None else None
        ),
        # Lower index means higher rank, so invert sign for intuitive positive=good.
        "rank_outcome_spearman": -spearman,
        "execution_effect": "NONE",
        "promotion_authority": False,
    }
