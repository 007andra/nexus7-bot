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
from bot.oos_model_validation import ValidationRow, purged_walk_forward
from bot.research_manifest import ResearchManifest


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
    for symbol_report in symbol_reports:
        for candidate in symbol_report.get("candidates", []) or []:
            if not candidate.baseline_eligible or not candidate.outcome_known:
                continue
            r_value = float(candidate.r_multiple)
            rows.append(ValidationRow(
                timestamp=float(candidate.timestamp),
                confidence=float(candidate.confidence),
                outcome=1 if r_value > 0.0 else 0,
                r_multiple=r_value,
            ))
    rows.sort(key=lambda row: row.timestamp)

    # Fixed evidence requirements: never shrink windows to manufacture a pass.
    folds = purged_walk_forward(
        rows,
        train_size=200,
        test_size=75,
        purge_size=4,
        step_size=75,
    )
    blockers = []
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
        "evidence_complete": not blockers,
        "evidence_blockers": tuple(blockers),
        "methods": methods,
        "fit_scope": "TRAIN_ONLY",
        "evaluation_scope": "OOS_ONLY",
        "live_probability_effect": "NONE",
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
            "same_bar_ambiguity": "STOP_FIRST",
            "fees_included": True,
            "slippage_included": True,
            "funding_included": True,
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
        "stable_positive_point_estimate": summary.get(
            "stable_positive_point_estimate"
        ),
        "manifest_hash": bundle["manifest_hash"],
        "output": str(output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
