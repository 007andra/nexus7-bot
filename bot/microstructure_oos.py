"""Out-of-sample attribution for SHADOW opportunity ranking.

Compares the existing base rank against a microstructure-enriched rank using
realized outcomes only after ranking is frozen. This module reports evidence;
it never promotes a model or changes execution.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping, Sequence

from bot.opportunity_ranker import Opportunity, evaluate_ranked_outcomes


def strip_microstructure(item: Opportunity) -> Opportunity:
    return replace(
        item,
        microstructure_alignment=None,
        taker_pressure=None,
        depth_notional_1pct=None,
    )


def compare_rankers_oos(
    items: Sequence[Opportunity],
    realized_r: Mapping[str, float],
) -> dict:
    """Compare base and enriched rank quality on the exact same OOS population."""
    ids = [item.candidate_id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate candidate id")
    complete = [
        item for item in items
        if item.microstructure_alignment is not None
        and item.taker_pressure is not None
        and item.depth_notional_1pct is not None
    ]
    observed = [
        item for item in complete
        if item.candidate_id in realized_r
        and math.isfinite(float(realized_r[item.candidate_id]))
    ]
    base = evaluate_ranked_outcomes(
        [strip_microstructure(item) for item in observed],
        realized_r,
    )
    enriched = evaluate_ranked_outcomes(observed, realized_r)

    base_spread = base.get("spread_r")
    enriched_spread = enriched.get("spread_r")
    base_spearman = base.get("rank_outcome_spearman")
    enriched_spearman = enriched.get("rank_outcome_spearman")
    return {
        "population_n": len(items),
        "microstructure_complete_n": len(complete),
        "observed_n": len(observed),
        "base": base,
        "microstructure": enriched,
        "spread_delta_r": (
            float(enriched_spread) - float(base_spread)
            if enriched_spread is not None and base_spread is not None
            else None
        ),
        "spearman_delta": (
            float(enriched_spearman) - float(base_spearman)
            if enriched_spearman is not None and base_spearman is not None
            else None
        ),
        "execution_effect": "NONE",
        "promotion_authority": False,
    }
