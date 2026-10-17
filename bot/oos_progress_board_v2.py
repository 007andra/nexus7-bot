"""OOS Progress Board V2.

Research-only operational dashboard for the immutable prospective OOS cohort.
It summarizes collection progress, missing evidence, outcome maturity and
release-board state without changing candidate generation, thresholds, scoring,
risk, sizing, dispatch, exchange state or LIVE authority.
"""
from __future__ import annotations

import math

AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "research_only": True,
    "candidate_generation_unchanged": True,
    "thresholds_unchanged": True,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}

TARGET_CANDIDATES = 50
TARGET_60M = 30
TARGET_240M = 30


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _ratio(value, target):
    if target <= 0:
        return None
    return min(max(float(value) / float(target), 0.0), 1.0)


def evaluate(oos: dict, readiness: dict, release_board: dict) -> dict:
    enrolled = _int(oos.get("enrolled_candidates"))
    observed60 = _int(oos.get("observed_60m"))
    observed240 = _int(oos.get("observed_240m"))

    a60 = oos.get("allowed_60m") or {}
    r60 = oos.get("rejected_60m") or {}
    a240 = oos.get("allowed_240m") or {}
    r240 = oos.get("rejected_240m") or {}

    missing = []
    if enrolled < TARGET_CANDIDATES:
        missing.append(f"CANDIDATES:{TARGET_CANDIDATES-enrolled}")
    if observed60 < TARGET_60M:
        missing.append(f"OUTCOMES_60M:{TARGET_60M-observed60}")
    if observed240 < TARGET_240M:
        missing.append(f"OUTCOMES_240M:{TARGET_240M-observed240}")

    progress = {
        "candidates": _ratio(enrolled, TARGET_CANDIDATES),
        "outcomes_60m": _ratio(observed60, TARGET_60M),
        "outcomes_240m": _ratio(observed240, TARGET_240M),
    }
    progress_values = [v for v in progress.values() if v is not None]
    composite = sum(progress_values) / len(progress_values) if progress_values else 0.0

    status = str(oos.get("status") or "UNAVAILABLE")
    if status == "READY_FOR_MANUAL_REVIEW":
        phase = "EVIDENCE_READY_FOR_MANUAL_REVIEW"
    elif status == "EVIDENCE_FAIL":
        phase = "EVIDENCE_FAILED"
    elif status == "COLLECTING_PROSPECTIVE_OOS":
        phase = "COLLECTING"
    else:
        phase = "UNAVAILABLE"

    return {
        **AUTHORITY,
        "phase": phase,
        "oos_status": status,
        "cohort_id": oos.get("cohort_id"),
        "started_epoch": _finite(oos.get("started_epoch")),
        "enrolled_candidates": enrolled,
        "target_candidates": TARGET_CANDIDATES,
        "observed_60m": observed60,
        "target_60m": TARGET_60M,
        "observed_240m": observed240,
        "target_240m": TARGET_240M,
        "candidate_progress": progress["candidates"],
        "outcome_60m_progress": progress["outcomes_60m"],
        "outcome_240m_progress": progress["outcomes_240m"],
        "composite_progress": composite,
        "missing_evidence": tuple(missing),
        "allowed_60m_n": _int(a60.get("n")),
        "allowed_60m_avg_return": _finite(a60.get("avg_return")),
        "rejected_60m_n": _int(r60.get("n")),
        "rejected_60m_avg_return": _finite(r60.get("avg_return")),
        "lift_60m": _finite(oos.get("allowed_vs_rejected_mean_lift_60m")),
        "allowed_240m_n": _int(a240.get("n")),
        "allowed_240m_avg_return": _finite(a240.get("avg_return")),
        "rejected_240m_n": _int(r240.get("n")),
        "rejected_240m_avg_return": _finite(r240.get("avg_return")),
        "lift_240m": _finite(oos.get("allowed_vs_rejected_mean_lift_240m")),
        "sample_complete": bool(oos.get("sample_complete")),
        "performance_criteria_pass": bool(oos.get("performance_criteria_pass")),
        "concentration_criteria_pass": bool(oos.get("concentration_criteria_pass")),
        "m1_risk_gate": release_board.get("m1_risk_gate", "UNKNOWN"),
        "m2_canonical_pipeline": release_board.get("m2_canonical_pipeline", "UNKNOWN"),
        "m3_edge_evidence": release_board.get("m3_edge_evidence", "UNKNOWN"),
        "m4_runtime_precheck": release_board.get("m4_runtime_precheck", "UNKNOWN"),
        "drawdown": _finite(readiness.get("drawdown")),
        "configured_limit": _finite(readiness.get("configured_limit")),
        "positions": _int(readiness.get("positions")),
        "pending_orders": _int(readiness.get("pending_orders")),
        "hypothesis_frozen": bool(oos.get("hypothesis_frozen")),
        "discovery_sample_excluded": bool(oos.get("discovery_sample_excluded")),
    }


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, (tuple, list)):
        return ",".join(map(str, value)) or "NONE"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).replace(" ", "_")


def format_log(row: dict) -> str:
    keys = (
        "phase", "oos_status", "cohort_id",
        "enrolled_candidates", "target_candidates",
        "observed_60m", "target_60m",
        "observed_240m", "target_240m",
        "candidate_progress", "outcome_60m_progress",
        "outcome_240m_progress", "composite_progress", "missing_evidence",
        "allowed_60m_n", "allowed_60m_avg_return",
        "rejected_60m_n", "rejected_60m_avg_return", "lift_60m",
        "allowed_240m_n", "allowed_240m_avg_return",
        "rejected_240m_n", "rejected_240m_avg_return", "lift_240m",
        "sample_complete", "performance_criteria_pass",
        "concentration_criteria_pass",
        "m1_risk_gate", "m2_canonical_pipeline",
        "m3_edge_evidence", "m4_runtime_precheck",
        "drawdown", "configured_limit", "positions", "pending_orders",
        "hypothesis_frozen", "discovery_sample_excluded",
        "candidate_generation_unchanged", "thresholds_unchanged",
        "promotion_allowed", "live_allowed", "decision_effect", "execution_effect",
    )
    return "[OOS_PROGRESS_BOARD_V2] " + " ".join(
        f"{key}={_fmt(row.get(key))}" for key in keys
    )
