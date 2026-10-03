"""Prospective champion/challenger comparison on identical opportunities."""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import mean
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ChallengerDecision:
    candidate_id: str
    timestamp: float
    approved: bool
    score: float

    def validate(self) -> "ChallengerDecision":
        if not self.candidate_id or not math.isfinite(self.timestamp):
            raise ValueError("invalid challenger decision identity")
        if not math.isfinite(self.score):
            raise ValueError("non-finite challenger score")
        return self


def paired_report(
    champion: Iterable[ChallengerDecision],
    challenger: Iterable[ChallengerDecision],
    outcomes_r: Mapping[str, float | None],
) -> dict[str, object]:
    champ = {item.candidate_id: item.validate() for item in champion}
    chall = {item.candidate_id: item.validate() for item in challenger}
    if set(champ) != set(chall):
        raise ValueError("champion/challenger opportunity populations differ")

    ids = sorted(
        champ,
        key=lambda cid: (champ[cid].timestamp, cid),
    )
    disagreements = 0
    champion_r: list[float] = []
    challenger_r: list[float] = []
    both_r: list[float] = []
    for candidate_id in ids:
        c = champ[candidate_id]
        h = chall[candidate_id]
        disagreements += int(c.approved != h.approved)
        raw = outcomes_r.get(candidate_id)
        if raw is None:
            continue
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("non-finite outcome")
        if c.approved:
            champion_r.append(value)
        if h.approved:
            challenger_r.append(value)
        if c.approved and h.approved:
            both_r.append(value)

    return {
        "candidate_population": len(ids),
        "known_outcomes": sum(
            1 for candidate_id in ids if outcomes_r.get(candidate_id) is not None
        ),
        "decision_disagreements": disagreements,
        "disagreement_rate": disagreements / len(ids) if ids else 0.0,
        "champion_selected": sum(champ[candidate_id].approved for candidate_id in ids),
        "challenger_selected": sum(chall[candidate_id].approved for candidate_id in ids),
        "champion_expectancy_r": mean(champion_r) if champion_r else None,
        "challenger_expectancy_r": mean(challenger_r) if challenger_r else None,
        "paired_common_expectancy_r": mean(both_r) if both_r else None,
        "expectancy_uplift_point_r": (
            mean(challenger_r) - mean(champion_r)
            if champion_r and challenger_r
            else None
        ),
        "promotion_authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
