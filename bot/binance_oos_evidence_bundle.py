"""One-population Binance USD-M OOS evidence bundle.

Primary edge evidence and robustness decomposition are derived from the exact
same checksum-verified replay population. Research-only; no promotion or
execution authority.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
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
from bot.microstructure_oos_evidence import (
    evaluate_microstructure_ranking,
    microstructure_review_gate,
)
from bot.research_manifest import ResearchManifest
from bot.research_sensitivity import incremental_cost_surface


REPLAY_SOURCE = "binance_oos_replay"
EXECUTION_MODEL = "BINANCE_USDM_RESEARCH_PROXY_V1"


def _symbol_universe(symbols: list[str]) -> tuple[list[str], str]:
    normalized = sorted({str(symbol).upper().strip() for symbol in symbols})
    if not normalized or any(not symbol for symbol in normalized):
        raise ValueError("research symbol universe must be non-empty")
    raw = json.dumps(
        normalized,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return normalized, hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def collect(
    symbols: list[str],
    *,
    start_month: str,
    end_month: str,
    cache_dir: str | Path | None,
    include_agg_trades: bool = False,
) -> tuple[list[dict], tuple]:
    months = month_range(start_month, end_month)
    reports = []
    artifacts = []
    for symbol in symbols:
        report, symbol_artifacts = await replay_symbol(
            str(symbol).upper(),
            months=months,
            cache_dir=cache_dir,
            include_agg_trades=include_agg_trades,
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
    """Canonical SHADOW ranking evidence with legacy wrapper error contract."""
    try:
        return evaluate_microstructure_ranking(symbol_reports)
    except ValueError as exc:
        if "duplicate candidate_id" in str(exc):
            raise RuntimeError(str(exc)) from exc
        raise


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
    include_agg_trades: bool = False,
) -> dict:
    from bot.runtime_bootstrap import install as install_runtime

    install_runtime()
    symbol_universe, symbol_universe_hash = _symbol_universe(symbols)
    reports, artifacts = await collect(
        symbol_universe,
        start_month=start_month,
        end_month=end_month,
        cache_dir=cache_dir,
        include_agg_trades=include_agg_trades,
    )
    primary = build_primary_report(reports)
    robustness = build_robustness_report(reports)
    calibration = build_calibration_report(reports)
    sensitivity = build_sensitivity_report(reports)
    opportunity_ranking = build_opportunity_ranking_report(reports)
    microstructure_review = microstructure_review_gate(
        opportunity_ranking
    )
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
        "opportunity_ranking": opportunity_ranking,
        "microstructure_review": microstructure_review,
        "manifest": manifest.canonical_dict(),
        "manifest_hash": manifest.fingerprint,
        "dataset_fingerprint": manifest.dataset_fingerprint,
        "methodology": {
            "venue": "BINANCE_USDM",
            "source": "data.binance.vision",
            "evidence_claim": "NEXUS_SELECTION_EDGE",
            "execution_pnl_claim": False,
            "instrument_rule_parity": "NOT_MODELED_BY_ALPHA_REPLAY",
            "universe_selection": "PREDECLARED_FIXED_PANEL",
            "claim_scope": "SYMBOL_PANEL_ONLY",
            "symbol_universe": symbol_universe,
            "symbol_universe_hash": symbol_universe_hash,
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
            "microstructure_incremental_comparison": True,
            "microstructure_same_population": True,
            "agg_trades_mode": (
                "CANDIDATE_DAY_REAL"
                if include_agg_trades else "DISABLED"
            ),
            "agg_trades_interpolation_applied": False,
            "microstructure_review_requires_real_agg_trades": True,
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
        "--include-agg-trades",
        action="store_true",
        help="Enable heavy candidate-day aggTrades microstructure evidence.",
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
        include_agg_trades=args.include_agg_trades,
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
        "microstructure_incremental_top_pick_uplift_r": (
            bundle["opportunity_ranking"].get(
                "incremental_top_pick_uplift_r"
            )
        ),
        "microstructure_uplift_ci95": bundle["opportunity_ranking"].get(
            "top_pick_uplift_ci95"
        ),
        "microstructure_ready_for_operator_review": (
            bundle["microstructure_review"].get(
                "ready_for_operator_review"
            )
        ),
        "microstructure_review_blockers": (
            bundle["microstructure_review"].get("blockers")
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
