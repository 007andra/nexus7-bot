"""Research-only NEXUS edge dashboard from canonical candidate evidence.

Metrics are descriptive evidence. They have no promotion or LIVE execution
authority.
"""
from __future__ import annotations

from statistics import mean, pstdev
from typing import Iterable

from bot.candidate_outcome_v2 import CandidateEvidenceV2


def _metrics(
    rows: list[CandidateEvidenceV2],
) -> dict[str, float | int | None]:
    known = [row for row in rows if row.outcome_net_pct is not None]
    approved = [row for row in known if row.approved]

    baseline_r = [
        float(outcome.r_multiple)
        for row in known
        if (outcome := row.to_candidate_outcome()).r_multiple is not None
    ]
    nexus_r = [
        float(outcome.r_multiple)
        for row in approved
        if (outcome := row.to_candidate_outcome()).r_multiple is not None
    ]

    positives = [value for value in nexus_r if value > 0.0]
    negatives = [value for value in nexus_r if value < 0.0]
    if negatives:
        profit_factor: float | None = sum(positives) / abs(sum(negatives))
    elif positives:
        profit_factor = float("inf")
    else:
        profit_factor = None

    sharpe_like = None
    if nexus_r:
        sigma = pstdev(nexus_r)
        if sigma > 0.0:
            sharpe_like = mean(nexus_r) / sigma

    cumulative_r = 0.0
    peak_r = 0.0
    max_drawdown_r = 0.0
    for value in nexus_r:
        cumulative_r += value
        peak_r = max(peak_r, cumulative_r)
        max_drawdown_r = max(max_drawdown_r, peak_r - cumulative_r)

    tail_loss = None
    if nexus_r:
        ordered = sorted(nexus_r)
        index = max(0, int(0.05 * (len(ordered) - 1)))
        tail_loss = ordered[index]

    return {
        "candidates": len(rows),
        "known_outcomes": len(known),
        "approved_known": len(approved),
        "baseline_expectancy_r": mean(baseline_r) if baseline_r else None,
        "nexus_expectancy_r": mean(nexus_r) if nexus_r else None,
        "expectancy_uplift_r": (
            mean(nexus_r) - mean(baseline_r)
            if nexus_r and baseline_r
            else None
        ),
        "hit_rate": (
            sum(1 for value in nexus_r if value > 0.0) / len(nexus_r)
            if nexus_r
            else None
        ),
        "profit_factor": profit_factor,
        "sharpe_like": sharpe_like,
        "max_drawdown_r": max_drawdown_r if nexus_r else None,
        "average_r": mean(nexus_r) if nexus_r else None,
        "tail_loss_p05_r": tail_loss,
    }


def _segment(rows, key):
    groups: dict[str, list[CandidateEvidenceV2]] = {}
    for row in rows:
        groups.setdefault(str(key(row)), []).append(row)
    return {
        name: _metrics(values)
        for name, values in sorted(groups.items())
    }


def build_dashboard(
    rows: Iterable[CandidateEvidenceV2],
) -> dict[str, object]:
    vals = tuple(
        sorted(
            (row.validate() for row in rows),
            key=lambda item: (item.timestamp, item.candidate_id),
        )
    )
    return {
        "scope": "NEXUS_EVALUATED_BASELINE",
        "overall": _metrics(list(vals)),
        "by_regime": _segment(vals, lambda row: row.regime),
        "by_setup": _segment(vals, lambda row: row.setup),
        "by_symbol": _segment(vals, lambda row: row.symbol),
        "by_side": _segment(vals, lambda row: row.side),
        "by_month": _segment(vals, lambda row: row.month),
        "promotion_authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


async def build_dashboard_from_db(
    db,
    *,
    horizon: str = "240m",
    since_epoch: float | None = None,
    limit: int = 10000,
) -> dict[str, object]:
    from bot.candidate_outcome_v2 import load_opportunity_candidates

    rows = await load_opportunity_candidates(
        db,
        horizon=horizon,
        since_epoch=since_epoch,
        limit=limit,
    )
    return build_dashboard(rows)
