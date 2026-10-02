"""One-population Binance USD-M OOS evidence bundle.

Primary edge evidence and robustness decomposition are derived from the exact
same checksum-verified replay population. Research-only; no promotion or
execution authority.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from bot.binance_oos_replay import month_range, replay_symbol
from bot.nexus_oos_edge_gate import build_edge_report, edge_promotion_decision
from bot.nexus_oos_robustness import analyze_robustness
from bot.nexus_oos_calibration import calibrate_walk_forward
from bot.oos_model_validation import (
    ValidationRow,
    label_aware_purged_embargo_walk_forward,
)
from bot.opportunity_ranker import (
    Opportunity,
    apply_cross_sectional_liquidity,
    evaluate_ranked_outcomes,
    rank_opportunities,
)
from bot.research_manifest import ResearchManifest
from bot.research_sensitivity import incremental_cost_surface


REPLAY_SOURCE = "binance_oos_replay"
EXECUTION_MODEL = "BINANCE_USDM_RESEARCH_PROXY_V1"


async def collect(
    symbols: list[str],
    *,
    start_month: str,
    end_month: str,
    cache_dir: str | Path | None,
) -> tuple[list[dict], tuple]:
    months = month_range(start_month, end_month)
    reports = []
    artifacts = []
    for symbol in symbols:
        report, symbol_artifacts = await replay_symbol(
            str(symbol).upper(),
            months=months,
            cache_dir=cache_dir,
        )
        reports.append(report)
        artifacts.extend(symbol_artifacts)
    return reports, tuple(artifacts)


def build_primary_report(symbol_reports: list[dict]) -> dict:
    all_rows = [
        row
        for report in symbol_reports
        for row in (report.get("candidates", []) or [])
    ]
    edge = build_edge_report(all_rows)
    statistically_ok, blockers = edge_promotion_decision(edge)

    valid = [report for report in symbol_reports if not report.get("error")]
    context_parity = bool(valid) and all(
        bool(report.get("historical_context", {}).get("parity_complete"))
        for report in valid
    )
    final_blockers = list(blockers)
    if not context_parity:
        final_blockers.append("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")

    compact = []
    for report in symbol_reports:
        item = {
            key: value
            for key, value in report.items()
            if key not in {"candidates", "candidate_diagnostics"}
        }
        item["candidate_count"] = len(report.get("candidates", []) or [])
        compact.append(item)

    return {
        "status": (
            "AI_EDGE_PROVEN"
            if statistically_ok and context_parity
            else "AI_EDGE_NOT_PROVEN"
        ),
        "blockers": sorted(set(final_blockers)),
        "report": asdict(edge),
        "symbols": compact,
        "context_parity_complete": context_parity,
        "promotion_authority": False,
        "execution_effect": "NONE",
    }


def build_robustness_report(symbol_reports: list[dict]) -> dict:
    robustness = analyze_robustness(symbol_reports, temporal_folds=4)
    return {
        "status": "ROBUSTNESS_RESEARCH_ONLY",
        "replay_source": REPLAY_SOURCE,
        "execution_model": EXECUTION_MODEL,
        "robustness": robustness,
        "promotion_authority": False,
        "execution_effect": "NONE",
    }


def build_calibration_report(symbol_reports: list[dict]) -> dict:
    rows = []
    missing_label_end = 0

    for symbol_report in symbol_reports:
        diagnostics_by_ts = {}
        for diagnostic in symbol_report.get("candidate_diagnostics", []) or []:
            try:
                ts = float(diagnostic["timestamp"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if ts in diagnostics_by_ts:
                raise RuntimeError(
                    f"duplicate calibration diagnostic timestamp: {ts}"
                )
            diagnostics_by_ts[ts] = diagnostic

        for candidate in symbol_report.get("candidates", []) or []:
            if not candidate.baseline_eligible or not candidate.outcome_known:
                continue
            ts = float(candidate.timestamp)
            diagnostic = diagnostics_by_ts.get(ts)
            if not isinstance(diagnostic, dict) or diagnostic.get("exit_ts") is None:
                missing_label_end += 1
                continue
            try:
                label_end = float(diagnostic["exit_ts"])
            except (TypeError, ValueError, OverflowError):
                missing_label_end += 1
                continue
            r_value = float(candidate.r_multiple)
            rows.append(ValidationRow(
                timestamp=ts,
                confidence=float(candidate.confidence),
                outcome=1 if r_value > 0.0 else 0,
                r_multiple=r_value,
                label_end_timestamp=label_end,
            ).validate())

    rows.sort(key=lambda row: row.timestamp)

    folds = label_aware_purged_embargo_walk_forward(
        rows,
        train_size=200,
        test_size=75,
        embargo_size=4,
    )
    blockers = []
    if missing_label_end:
        blockers.append("MISSING_LABEL_END_TIMESTAMPS")
    if len(folds) < 4:
        blockers.append("INSUFFICIENT_CALIBRATION_OOS_FOLDS")

    methods = {}
    for method in ("platt", "isotonic"):
        if not folds:
            methods[method] = {
                "status": "NOT_RUN",
                "reason": "NO_OOS_FOLDS",
                "execution_effect": "NONE",
            }
            continue
        try:
            methods[method] = {
                "status": "OK",
                "evidence": calibrate_walk_forward(
                    folds, method=method, bins=10
                ),
            }
        except ValueError as exc:
            methods[method] = {
                "status": "FAILED",
                "reason": type(exc).__name__,
                "execution_effect": "NONE",
            }

    if not any(item.get("status") == "OK" for item in methods.values()):
        blockers.append("NO_CALIBRATOR_VALID_OOS")

    return {
        "fold_count": len(folds),
        "rows_with_label_end": len(rows),
        "missing_label_end": missing_label_end,
        "evidence_complete": not blockers,
        "evidence_blockers": tuple(blockers),
        "methods": methods,
        "fit_scope": "TRAIN_ONLY",
        "evaluation_scope": "OOS_ONLY",
        "purge_basis": "ACTUAL_LABEL_END_TIMESTAMP",
        "embargo_rows": 4,
        "live_probability_effect": "NONE",
        "promotion_authority": False,
    }


def build_sensitivity_report(symbol_reports: list[dict]) -> dict:
    """Attach honest cost sensitivity; parameter grid requires independent replays."""
    base_net_returns = []
    for symbol_report in symbol_reports:
        for diagnostic in symbol_report.get("candidate_diagnostics", []) or []:
            try:
                base_net_returns.append(float(diagnostic["net_return"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue

    execution_cost = (
        incremental_cost_surface(base_net_returns)
        if base_net_returns
        else {
            "status": "NOT_RUN",
            "reason": "NO_NET_RETURN_DIAGNOSTICS",
            "points": [],
            "promotion_effect": "NONE",
        }
    )
    return {
        "parameter": {
            "status": "NOT_RUN",
            "reason": "REQUIRES_INDEPENDENT_REPLAY_PARAMETER_GRID",
            "parameter_sets": 0,
            "promotion_effect": "NONE",
        },
        "execution_cost": execution_cost,
        "execution_effect": "NONE",
        "promotion_authority": False,
    }


def build_opportunity_ranking_report(symbol_reports: list[dict]) -> dict:
    """Evaluate same-timestamp cross-symbol SHADOW ranking OOS.

    Rank inputs are strictly pre-trade. Realized R is joined only after the
    ranking has been computed for evaluation.
    """
    grouped: dict[int, list[tuple[Opportunity, float]]] = {}
    seen_candidates: set[str] = set()
    total_diagnostics = 0
    micro_available = 0

    for symbol_report in symbol_reports:
        symbol = str(symbol_report.get("symbol", "") or "").upper()
        for item in symbol_report.get("candidate_diagnostics", []) or []:
            total_diagnostics += 1
            candidate_id = str(item.get("candidate_id", "") or "")
            if not candidate_id:
                continue
            if candidate_id in seen_candidates:
                raise RuntimeError(
                    f"duplicate candidate_id in ranking evidence: {candidate_id}"
                )
            seen_candidates.add(candidate_id)

            micro = item.get("shadow_microstructure") or {}
            alignment = micro.get("directional_alignment")
            taker_pressure = micro.get("taker_pressure")
            if micro.get("available") is True:
                micro_available += 1

            opportunity = Opportunity(
                candidate_id=candidate_id,
                symbol=symbol,
                expected_value=float(
                    item.get("nexus_expected_value_pct", 0.0) or 0.0
                ),
                net_rr=float(item.get("nexus_rr_net", 0.0) or 0.0),
                setup_score=float(
                    item.get("nexus_setup_quality", 0.0) or 0.0
                ),
                liquidity_score=50.0,
                regime_confidence=float(
                    item.get("nexus_regime_compat", 0.0) or 0.0
                ),
                round_trip_cost=float(
                    item.get("round_trip_cost", 0.0) or 0.0
                ),
                decision_ts=int(item.get("timestamp", 0) or 0),
                side=str(item.get("direction", "UNKNOWN") or "UNKNOWN"),
                confidence=float(
                    item.get("nexus_confidence", 0.0) or 0.0
                ),
                microstructure_alignment=(
                    float(alignment) if alignment is not None else None
                ),
                taker_pressure=(
                    float(taker_pressure)
                    if taker_pressure is not None else None
                ),
                depth_notional_1pct=(
                    float(item["depth_notional_1pct"])
                    if item.get("depth_notional_1pct") is not None
                    else None
                ),
            )
            grouped.setdefault(opportunity.decision_ts, []).append(
                (opportunity, float(item.get("r_multiple", 0.0) or 0.0))
            )

    cross_sections = []
    top_returns: list[float] = []
    rest_returns: list[float] = []
    spearman_values: list[float] = []
    ranked_candidates = 0

    for timestamp in sorted(grouped):
        entries = grouped[timestamp]
        if len(entries) < 2:
            continue
        items = apply_cross_sectional_liquidity(
            [item for item, _ in entries]
        )
        realized = {
            item.candidate_id: realized_r
            for item, realized_r in entries
        }
        ranked = rank_opportunities(items)
        evaluation = evaluate_ranked_outcomes(items, realized)
        top_item, top_score = ranked[0]
        top_r = float(realized[top_item.candidate_id])
        other_r = [
            float(realized[item.candidate_id])
            for item, _score in ranked[1:]
        ]
        top_returns.append(top_r)
        rest_returns.extend(other_r)
        ranked_candidates += len(ranked)
        rho = evaluation.get("rank_outcome_spearman")
        if rho is not None:
            spearman_values.append(float(rho))

        cross_sections.append({
            "timestamp": int(timestamp),
            "candidates": len(ranked),
            "top_candidate_id": top_item.candidate_id,
            "top_symbol": top_item.symbol,
            "top_pretrade_rank_score": float(top_score),
            "top_realized_r": top_r,
            "microstructure_available": sum(
                1 for item in items
                if item.microstructure_alignment is not None
            ),
            "evaluation": evaluation,
        })

    top_expectancy = (
        sum(top_returns) / len(top_returns)
        if top_returns else None
    )
    rest_expectancy = (
        sum(rest_returns) / len(rest_returns)
        if rest_returns else None
    )
    uplift = (
        top_expectancy - rest_expectancy
        if top_expectancy is not None and rest_expectancy is not None
        else None
    )
    mean_spearman = (
        sum(spearman_values) / len(spearman_values)
        if spearman_values else None
    )

    return {
        "status": (
            "EVIDENCE_AVAILABLE"
            if cross_sections else "INSUFFICIENT_CROSS_SECTIONAL_SAMPLE"
        ),
        "cross_sections": len(cross_sections),
        "ranked_candidates": ranked_candidates,
        "total_candidate_diagnostics": total_diagnostics,
        "microstructure_coverage": (
            micro_available / total_diagnostics
            if total_diagnostics else 0.0
        ),
        "top1_expectancy_r": top_expectancy,
        "rest_expectancy_r": rest_expectancy,
        "top1_uplift_r": uplift,
        "mean_rank_outcome_spearman": mean_spearman,
        "details": cross_sections,
        "rank_inputs": (
            "expected_value,net_rr,setup_score,liquidity_percentile,"
            "regime_compat,confidence,cost,microstructure,taker_pressure"
        ),
        "outcome_used_in_rank": False,
        "execution_effect": "NONE",
        "score_effect": "NONE",
        "promotion_authority": False,
    }


def assert_population_parity(primary: dict, robustness: dict) -> None:
    primary_symbols = {
        item["symbol"]: int(item.get("candidate_count", 0))
        for item in primary.get("symbols", [])
    }
    robust_per_symbol = (
        robustness.get("robustness", {}).get("per_symbol", {}) or {}
    )
    robust_symbols = {
        symbol: int(report.get("baseline_candidates", 0))
        for symbol, report in robust_per_symbol.items()
    }
    primary_nonzero = {
        symbol: count for symbol, count in primary_symbols.items() if count > 0
    }
    if primary_nonzero != robust_symbols:
        raise RuntimeError(
            "Binance OOS population drift: "
            f"primary={primary_nonzero} robustness={robust_symbols}"
        )

    primary_total = int(
        primary.get("report", {}).get("baseline_candidates", 0)
    )
    pooled = robustness.get("robustness", {}).get("pooled") or {}
    robust_total = int(pooled.get("baseline_candidates", 0) or 0)
    if primary_total != robust_total:
        raise RuntimeError(
            "Binance OOS candidate total drift: "
            f"primary={primary_total} robustness={robust_total}"
        )


async def run(
    symbols: list[str],
    *,
    start_month: str,
    end_month: str,
    cache_dir: str | Path | None = None,
    code_sha: str = "UNSPECIFIED",
) -> dict:
    from bot.runtime_bootstrap import install as install_runtime

    install_runtime()
    reports, artifacts = await collect(
        symbols,
        start_month=start_month,
        end_month=end_month,
        cache_dir=cache_dir,
    )
    primary = build_primary_report(reports)
    robustness = build_robustness_report(reports)
    calibration = build_calibration_report(reports)
    sensitivity = build_sensitivity_report(reports)
    assert_population_parity(primary, robustness)

    manifest = ResearchManifest(
        version="BINANCE_USDM_OOS_BUNDLE_V1",
        code_sha=str(code_sha),
        created_at_ms=int(time.time() * 1000),
        artifacts=artifacts,
    )

    return {
        "primary": primary,
        "robustness": robustness,
        "calibration": calibration,
        "sensitivity": sensitivity,
        "manifest": manifest.canonical_dict(),
        "manifest_hash": manifest.fingerprint,
        "methodology": {
            "venue": "BINANCE_USDM",
            "source": "data.binance.vision",
            "archive_checksums_verified": True,
            "shared_candidate_population": True,
            "closed_candles_only": True,
            "historical_clock_frozen": True,
            "metrics_label_shift_normalized": True,
            "oi_delta_semantics": "PREVIOUS_NEXUS_CANDIDATE",
            "same_bar_ambiguity": "STOP_FIRST",
            "fees_included": True,
            "slippage_included": True,
            "funding_included": True,
            "shadow_microstructure_ranked": True,
            "opportunity_rank_outcome_leakage": False,
            "platt_calibration_train_only": True,
            "platt_calibration_live_effect": "NONE",
            "authenticated_api": False,
            "exchange_mutations": False,
            "runtime_policy_mutations": False,
            "promotion_authority": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    )
    parser.add_argument("--start-month", required=True)
    parser.add_argument("--end-month", required=True)
    parser.add_argument(
        "--cache-dir", default="artifacts/binance_research_cache"
    )
    parser.add_argument(
        "--code-sha",
        default=os.environ.get("RAILWAY_GIT_COMMIT_SHA", "UNSPECIFIED"),
    )
    parser.add_argument(
        "--output",
        default="artifacts/nexus_oos_binance_usdm_bundle.json",
    )
    args = parser.parse_args()

    bundle = asyncio.run(run(
        list(args.symbols),
        start_month=args.start_month,
        end_month=args.end_month,
        cache_dir=args.cache_dir,
        code_sha=args.code_sha,
    ))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(bundle, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    pooled = bundle["robustness"]["robustness"].get("pooled") or {}
    summary = bundle["robustness"]["robustness"].get("summary") or {}
    print(json.dumps({
        "status": bundle["primary"]["status"],
        "blockers": bundle["primary"]["blockers"],
        "candidate_count": bundle["primary"]["report"]["baseline_candidates"],
        "approved_candidates": bundle["primary"]["report"]["approved_candidates"],
        "expectancy_uplift_r": bundle["primary"]["report"]["expectancy_uplift_r"],
        "robustness_candidate_count": pooled.get("baseline_candidates"),
        "opportunity_cross_sections": bundle["opportunity_ranking"].get(
            "cross_sections"
        ),
        "opportunity_top1_uplift_r": bundle["opportunity_ranking"].get(
            "top1_uplift_r"
        ),
        "stable_positive_point_estimate": summary.get(
            "stable_positive_point_estimate"
        ),
        "manifest_hash": bundle["manifest_hash"],
        "output": str(output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
