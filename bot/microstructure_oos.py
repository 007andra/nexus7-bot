"""Compatibility facade for NEXUS SHADOW microstructure OOS evidence.

The canonical implementation lives in :mod:`bot.microstructure_oos_evidence`.
This module preserves the older public helper without maintaining a second
independent evidence engine.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping, Sequence

from bot.microstructure_oos_evidence import (
    evaluate_microstructure_ranking,
    microstructure_review_gate,
)
from bot.opportunity_ranker import (
    Opportunity,
    evaluate_ranked_outcomes,
    realized_by_observation,
)


def strip_microstructure(item: Opportunity) -> Opportunity:
    """Return the pre-microstructure view used by legacy callers."""
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
    """Legacy single-batch comparison retained for compatibility.

    New multi-symbol/time OOS evidence must use
    `evaluate_microstructure_ranking`, which adds paired batches, bootstrap
    confidence intervals, temporal folds and the fail-closed review gate.
    """
    ids = [item.observation_id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate research observation id")
    realized_r = realized_by_observation(items, realized_r)
    complete = [
        item for item in items
        if item.microstructure_alignment is not None
        and item.taker_pressure is not None
        and item.depth_notional_1pct is not None
    ]
    observed = [
        item for item in complete
        if item.observation_id in realized_r
        and math.isfinite(float(realized_r[item.observation_id]))
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
        "canonical_engine": "bot.microstructure_oos_evidence",
        "execution_effect": "NONE",
        "promotion_authority": False,
    }


__all__ = [
    "compare_rankers_oos",
    "evaluate_microstructure_ranking",
    "microstructure_review_gate",
    "strip_microstructure",
]
