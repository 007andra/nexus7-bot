"""OOS evidence for incremental SHADOW microstructure ranking value.

The evaluator compares base and microstructure-enriched rankings on the exact
same candidate population and decision timestamp. Realized outcomes are read
only after both rankings are fixed. No result in this module has execution or
promotion authority.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Mapping, Sequence

from bot.opportunity_ranker import (
    Opportunity,
    apply_cross_sectional_liquidity,
    evaluate_ranked_outcomes,
    rank_opportunities,
)
from bot.research_statistics import bootstrap_mean_ci


def _finite(value: object, *, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} is not numeric") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} is not finite")
    return out


def base_opportunity(symbol: str, row: Mapping[str, object]) -> Opportunity:
    return Opportunity(
        candidate_id=str(row.get("candidate_id", "") or ""),
        symbol=str(symbol).upper(),
        expected_value=_finite(
            row.get("nexus_expected_value_pct", 0.0),
            name="expected_value",
        ),
        net_rr=max(
            0.0,
            _finite(row.get("nexus_rr_net", 0.0), name="net_rr"),
        ),
        setup_score=max(
            0.0,
            min(
                100.0,
                _finite(
                    row.get("nexus_setup_quality", 0.0),
                    name="setup_quality",
                ),
            ),
        ),
        liquidity_score=50.0,
        regime_confidence=max(
            0.0,
            min(
                100.0,
                _finite(
                    row.get("nexus_regime_compat", 0.0),
                    name="regime_compat",
                ),
            ),
        ),
        round_trip_cost=max(
            0.0,
            _finite(
                row.get("round_trip_cost", 0.0),
                name="round_trip_cost",
            ),
        ),
        decision_ts=int(row.get("timestamp", 0) or 0),
        side=str(row.get("direction", "UNKNOWN") or "UNKNOWN").upper(),
        confidence=max(
            0.0,
            min(
                100.0,
                _finite(
                    row.get("nexus_confidence", 0.0),
                    name="confidence",
                ),
            ),
        ),
    )


def enriched_opportunity(
    symbol: str,
    row: Mapping[str, object],
) -> Opportunity | None:
    micro = row.get("shadow_microstructure")
    if not isinstance(micro, Mapping) or micro.get("available") is not True:
        return None
    if micro.get("execution_effect") != "NONE":
        raise ValueError("historical microstructure cannot affect execution")
    if micro.get("score_effect") != "NONE":
        raise ValueError("historical microstructure cannot affect NEXUS score")
    if bool(micro.get("promotion_authority", False)):
        raise ValueError("historical microstructure cannot promote")

    base = base_opportunity(symbol, row)
    alignment = micro.get("directional_alignment")
    taker = micro.get("taker_pressure")
    if alignment is None and taker is None:
        return None

    depth = row.get("depth_notional_1pct")
    return Opportunity(
        candidate_id=base.candidate_id,
        symbol=base.symbol,
        expected_value=base.expected_value,
        net_rr=base.net_rr,
        setup_score=base.setup_score,
        liquidity_score=base.liquidity_score,
        regime_confidence=base.regime_confidence,
        round_trip_cost=base.round_trip_cost,
        decision_ts=base.decision_ts,
        side=base.side,
        confidence=base.confidence,
        microstructure_alignment=(
            max(-1.0, min(1.0, _finite(alignment, name="alignment")))
            if alignment is not None else None
        ),
        taker_pressure=(
            max(-1.0, min(1.0, _finite(taker, name="taker_pressure")))
            if taker is not None else None
        ),
        depth_notional_1pct=(
            max(0.0, _finite(depth, name="depth_notional_1pct"))
            if depth is not None else None
        ),
    )


def _fold_means(values: Sequence[float], folds: int = 4) -> tuple[float, ...]:
    if folds <= 0:
        raise ValueError("folds must be positive")
    if len(values) < folds:
        return tuple()
    out = []
    for fold in range(folds):
        start = len(values) * fold // folds
        end = len(values) * (fold + 1) // folds
        chunk = values[start:end]
        if not chunk:
            return tuple()
        out.append(sum(chunk) / len(chunk))
    return tuple(out)


def evaluate_microstructure_ranking(
    symbol_reports: Sequence[Mapping[str, object]],
    *,
    bootstrap_samples: int = 4000,
    seed: int = 31,
    temporal_folds: int = 4,
) -> dict:
    """Compare base vs enriched cross-symbol rankings on identical OOS batches."""
    by_ts: dict[int, list[tuple[Opportunity, Opportunity, float]]] = defaultdict(list)
    total_diagnostics = 0
    micro_available = 0
    seen_candidates: set[str] = set()

    for report in symbol_reports:
        symbol = str(report.get("symbol", "") or "").upper()
        diagnostics = report.get("candidate_diagnostics") or ()
        if not isinstance(diagnostics, Sequence):
            raise ValueError("candidate_diagnostics must be a sequence")
        for row in diagnostics:
            if not isinstance(row, Mapping):
                raise ValueError("invalid candidate diagnostic")
            total_diagnostics += 1
            enriched = enriched_opportunity(symbol, row)
            if enriched is None:
                continue
            micro_available += 1
            base = base_opportunity(symbol, row)
            if base.candidate_id in seen_candidates:
                raise ValueError("duplicate candidate_id in microstructure evidence")
            seen_candidates.add(base.candidate_id)
            realized = _finite(row.get("r_multiple"), name="r_multiple")
            by_ts[base.decision_ts].append((base, enriched, realized))

    batch_rows = []
    top_pick_deltas: list[float] = []
    base_top_returns: list[float] = []
    enriched_top_returns: list[float] = []
    spearman_deltas: list[float] = []
    changed = 0
    depth_complete_batches = 0
    comparable_candidates = 0

    for decision_ts in sorted(by_ts):
        rows = by_ts[decision_ts]
        if len(rows) < 2:
            continue
        bases = [row[0] for row in rows]
        enriched = [row[1] for row in rows]
        realized = {row[0].candidate_id: row[2] for row in rows}

        depth_complete = all(
            item.depth_notional_1pct is not None for item in enriched
        )
        if depth_complete:
            enriched = apply_cross_sectional_liquidity(enriched)
            depth_complete_batches += 1

        base_ranked = rank_opportunities(bases)
        enriched_ranked = rank_opportunities(enriched)
        base_top = base_ranked[0][0].candidate_id
        enriched_top = enriched_ranked[0][0].candidate_id
        base_top_r = realized[base_top]
        enriched_top_r = realized[enriched_top]
        delta = enriched_top_r - base_top_r

        base_eval = evaluate_ranked_outcomes(bases, realized)
        enriched_eval = evaluate_ranked_outcomes(enriched, realized)
        base_spear = base_eval.get("rank_outcome_spearman")
        enriched_spear = enriched_eval.get("rank_outcome_spearman")
        spear_delta = (
            float(enriched_spear) - float(base_spear)
            if base_spear is not None and enriched_spear is not None
            else None
        )

        top_pick_deltas.append(delta)
        base_top_returns.append(base_top_r)
        enriched_top_returns.append(enriched_top_r)
        if spear_delta is not None:
            spearman_deltas.append(spear_delta)
        changed += int(base_top != enriched_top)
        comparable_candidates += len(rows)
        batch_rows.append({
            "decision_ts": int(decision_ts),
            "timestamp": int(decision_ts),
            "candidates": len(rows),
            "base_top_candidate_id": base_top,
            "enriched_top_candidate_id": enriched_top,
            "top_candidate_id": enriched_top,
            "top_pick_changed": base_top != enriched_top,
            "base_top_r": base_top_r,
            "enriched_top_r": enriched_top_r,
            "top_pick_uplift_r": delta,
            "base_rank_outcome_spearman": base_spear,
            "enriched_rank_outcome_spearman": enriched_spear,
            "rank_spearman_delta": spear_delta,
            "depth_cross_section_complete": depth_complete,
        })

    ci = (
        bootstrap_mean_ci(
            top_pick_deltas,
            confidence=0.95,
            n_bootstrap=bootstrap_samples,
            seed=seed,
        )
        if top_pick_deltas else
        {"mean": 0.0, "low": 0.0, "high": 0.0, "n": 0}
    )
    fold_uplift = _fold_means(top_pick_deltas, folds=temporal_folds)
    base_mean = (
        sum(base_top_returns) / len(base_top_returns)
        if base_top_returns else None
    )
    enriched_mean = (
        sum(enriched_top_returns) / len(enriched_top_returns)
        if enriched_top_returns else None
    )
    spearman_delta_mean = (
        sum(spearman_deltas) / len(spearman_deltas)
        if spearman_deltas else None
    )

    rest_returns = []
    for batch in batch_rows:
        timestamp = int(batch["decision_ts"])
        entries = by_ts[timestamp]
        enriched_items = [row[1] for row in entries]
        if all(item.depth_notional_1pct is not None for item in enriched_items):
            enriched_items = apply_cross_sectional_liquidity(enriched_items)
        ranked = rank_opportunities(enriched_items)
        realized = {row[0].candidate_id: row[2] for row in entries}
        rest_returns.extend(
            float(realized[item.candidate_id])
            for item, _score in ranked[1:]
        )
    rest_mean = (
        sum(rest_returns) / len(rest_returns)
        if rest_returns else None
    )
    enriched_vs_rest = (
        enriched_mean - rest_mean
        if enriched_mean is not None and rest_mean is not None
        else None
    )

    return {
        "status": (
            "EVIDENCE_AVAILABLE"
            if batch_rows else "INSUFFICIENT_CROSS_SECTIONAL_SAMPLE"
        ),
        "cross_sections": len(batch_rows),
        "ranked_candidates": comparable_candidates,
        "total_candidate_diagnostics": total_diagnostics,
        "microstructure_available_candidates": micro_available,
        "microstructure_coverage": (
            micro_available / total_diagnostics
            if total_diagnostics else 0.0
        ),
        "comparable_batches": len(batch_rows),
        "comparable_candidates": comparable_candidates,
        "depth_complete_batches": depth_complete_batches,
        "top_pick_changed_batches": changed,
        "top_pick_changed_fraction": (
            changed / len(batch_rows) if batch_rows else 0.0
        ),
        "base_top_pick_expectancy_r": base_mean,
        "enriched_top_pick_expectancy_r": enriched_mean,
        "top1_expectancy_r": enriched_mean,
        "rest_expectancy_r": rest_mean,
        "top1_uplift_r": enriched_vs_rest,
        "top_pick_expectancy_uplift_r": (
            enriched_mean - base_mean
            if base_mean is not None and enriched_mean is not None
            else None
        ),
        "incremental_top_pick_uplift_r": (
            enriched_mean - base_mean
            if base_mean is not None and enriched_mean is not None
            else None
        ),
        "top_pick_uplift_ci95": ci,
        "mean_rank_spearman_delta": spearman_delta_mean,
        "temporal_fold_uplift_r": fold_uplift,
        "temporal_folds_evaluated": len(fold_uplift),
        "positive_uplift_folds": sum(1 for value in fold_uplift if value > 0),
        "batches": batch_rows,
        "details": batch_rows,
        "outcome_used_in_rank": False,
        "execution_effect": "NONE",
        "nexus_score_effect": "NONE",
        "score_effect": "NONE",
        "promotion_authority": False,
    }
