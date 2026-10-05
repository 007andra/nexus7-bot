"""Fail-closed observability board for controlled BGX LIVE re-entry.

This module consolidates the four re-entry milestones:
1) lifetime drawdown readiness;
2) canonical market-pipeline sample;
3) counterfactual edge evidence;
4) execution/runtime precheck.

It has no order, risk, sizing, threshold, recovery, override or dispatch authority.
Even MANUAL_REVIEW_READY never means LIVE_ALLOWED and never authorizes an order.
"""
from __future__ import annotations

import math

TARGET_PIPELINE = 20
TARGET_OUTCOMES_60M = 20
TARGET_OUTCOMES_240M = 20

AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _finite(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def evaluate(readiness: dict, epoch: dict, validation: dict, decision_review: dict) -> dict:
    blockers = []

    dd = _finite(readiness.get("drawdown"))
    limit = _finite(readiness.get("configured_limit"))
    risk_ready = (
        readiness.get("status") == "READY"
        and dd is not None
        and limit is not None
        and dd < limit
        and readiness.get("recovery_authorized") is False
        and readiness.get("override") is False
    )
    risk_reason = "PASS" if risk_ready else "DRAWDOWN_OR_RUNTIME_READINESS_BLOCKED"
    if not risk_ready:
        blockers.append("M1_RISK_GATE")

    traversed = _int(epoch.get("traversed_to_nexus"))
    canonical_obs60 = _int(epoch.get("observed_60m"))
    pipeline_ready = (
        traversed >= TARGET_PIPELINE
        and canonical_obs60 >= TARGET_OUTCOMES_60M
    )
    if not pipeline_ready:
        blockers.append("M2_CANONICAL_PIPELINE")

    eval_count = _int(validation.get("evaluated"))
    cf_obs60 = _int(validation.get("observed_60m"))
    cf_obs240 = _int(validation.get("observed_240m"))
    recommendation = str(decision_review.get("recommendation") or "UNKNOWN")
    allowed60 = (validation.get("allowed") or {})
    allowed240 = ((validation.get("horizon_240m") or {}).get("allowed") or {})

    if recommendation == "DISCARD_RR_RELAXATION_IN_THIS_SAMPLE":
        edge_status = "NEGATIVE_EVIDENCE"
    elif (
        eval_count >= TARGET_PIPELINE
        and cf_obs60 >= TARGET_OUTCOMES_60M
        and cf_obs240 >= TARGET_OUTCOMES_240M
        and recommendation in {
            "KEEP_RR_1_60_PENDING_MORE_EVIDENCE",
            "STUDY_RR_1_50_MANUAL_REVIEW",
        }
    ):
        edge_status = "MANUAL_REVIEW_REQUIRED"
    else:
        edge_status = "INSUFFICIENT_EVIDENCE"

    if edge_status != "MANUAL_REVIEW_REQUIRED":
        blockers.append("M3_EDGE_EVIDENCE")

    readiness_blockers = set(readiness.get("blockers") or ())
    unexpected_runtime = sorted(readiness_blockers - {"DRAWDOWN_ABOVE_LIMIT"})
    runtime_precheck = (
        not unexpected_runtime
        and readiness.get("preflight_ready") is True
        and _int(readiness.get("positions")) == 0
        and _int(readiness.get("pending_orders")) == 0
        and readiness.get("recovery_authorized") is False
        and readiness.get("override") is False
    )
    if not runtime_precheck:
        blockers.append("M4_RUNTIME_PRECHECK")

    status = "MANUAL_REVIEW_READY" if not blockers else "BLOCKED"
    return {
        "status": status,
        "blockers": tuple(blockers),
        "m1_risk_gate": "PASS" if risk_ready else "BLOCKED",
        "m1_reason": risk_reason,
        "drawdown": dd,
        "configured_limit": limit,
        "equity": _finite(readiness.get("equity")),
        "required_equity_for_limit": _finite(
            readiness.get("required_equity_for_limit")
        ),
        "equity_gap_to_limit": _finite(readiness.get("equity_gap_to_limit")),
        "external_capital_flow_preserves_drawdown": bool(
            readiness.get("external_capital_flow_preserves_drawdown")
        ),
        "m2_canonical_pipeline": "PASS" if pipeline_ready else "BLOCKED",
        "canonical_traversed_to_nexus": traversed,
        "canonical_observed_60m": canonical_obs60,
        "target_pipeline": TARGET_PIPELINE,
        "target_canonical_observed_60m": TARGET_OUTCOMES_60M,
        "m3_edge_evidence": edge_status,
        "counterfactual_evaluated": eval_count,
        "counterfactual_observed_60m": cf_obs60,
        "counterfactual_observed_240m": cf_obs240,
        "decision_review_recommendation": recommendation,
        "allowed_60m_n": _int(allowed60.get("n")),
        "allowed_60m_avg_return": _finite(allowed60.get("avg_return")),
        "allowed_60m_positive_rate": _finite(allowed60.get("positive_rate")),
        "allowed_240m_n": _int(allowed240.get("n")),
        "allowed_240m_avg_return": _finite(allowed240.get("avg_return")),
        "allowed_240m_positive_rate": _finite(allowed240.get("positive_rate")),
        "m4_runtime_precheck": "PASS" if runtime_precheck else "BLOCKED",
        "unexpected_runtime_blockers": tuple(unexpected_runtime),
        "preflight_ready": bool(readiness.get("preflight_ready")),
        "positions": _int(readiness.get("positions")),
        "pending_orders": _int(readiness.get("pending_orders")),
        "execution_proof_status": "REQUIRES_CURRENT_SHA_CI_AND_RUNTIME_REVIEW",
        "manual_loss_acceptance_required": True,
        "explicit_live_authorization_required": True,
        **AUTHORITY,
    }


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, (tuple, list)):
        return ",".join(map(str, value)) or "NONE"
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value).replace(" ", "_")


def format_log(row: dict) -> str:
    keys = (
        "status", "blockers",
        "m1_risk_gate", "drawdown", "configured_limit",
        "equity", "required_equity_for_limit", "equity_gap_to_limit",
        "external_capital_flow_preserves_drawdown",
        "m2_canonical_pipeline", "canonical_traversed_to_nexus",
        "canonical_observed_60m",
        "m3_edge_evidence", "counterfactual_evaluated",
        "counterfactual_observed_60m", "counterfactual_observed_240m",
        "decision_review_recommendation",
        "allowed_60m_n", "allowed_60m_avg_return", "allowed_60m_positive_rate",
        "allowed_240m_n", "allowed_240m_avg_return", "allowed_240m_positive_rate",
        "m4_runtime_precheck", "unexpected_runtime_blockers",
        "preflight_ready", "positions", "pending_orders",
        "execution_proof_status", "manual_loss_acceptance_required",
        "explicit_live_authorization_required", "automatic_promotion",
        "promotion_allowed", "live_allowed", "decision_effect", "execution_effect",
    )
    return "[REENTRY_RELEASE_BOARD_V1] " + " ".join(
        f"{key}={_fmt(row.get(key))}" for key in keys
    )


__all__ = [
    "AUTHORITY",
    "TARGET_PIPELINE",
    "TARGET_OUTCOMES_60M",
    "TARGET_OUTCOMES_240M",
    "evaluate",
    "format_log",
]
