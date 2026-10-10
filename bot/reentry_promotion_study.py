"""Observability-only promotion study for a future controlled LIVE re-entry.

This is governance, not an execution gate and not an authorization mechanism.
It combines the persistent Risk Epoch shadow cohort with the current runtime
readiness snapshot and says only whether evidence is sufficient for a HUMAN
review. It can never return LIVE_ALLOWED or mutate any trading state.
"""
from __future__ import annotations

EXECUTION_PROOF_REQUIREMENTS = (
    "OWNERSHIP_FENCING",
    "DURABLE_STATE",
    "PRIVATE_STREAM_RECONCILIATION",
    "FINAL_SIZING",
    "FINAL_RISK_BUDGET",
    "FRESH_PREDISPATCH_MARKET",
    "BINANCE_CROSS_STRESS",
    "PROTECTION_INSTALLATION",
)

def _int(value, default=99):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "promotion_allowed": False,
    "live_allowed": False,
}


def evaluate(epoch: dict, readiness: dict, *, target_pipeline: int = 20) -> dict:
    target = max(1, int(target_pipeline))
    blockers = []

    if epoch.get("status") == "DISABLED":
        blockers.append("RISK_EPOCH_DISABLED")
    if int(epoch.get("traversed_to_nexus", 0) or 0) < target:
        blockers.append("MARKET_PIPELINE_SAMPLE_INCOMPLETE")
    if int(epoch.get("observed_60m", 0) or 0) < target:
        blockers.append("OUTCOME_60M_SAMPLE_INCOMPLETE")

    readiness_blockers = set(readiness.get("blockers") or ())
    # The lifetime drawdown is expected to remain the sole LIVE blocker during
    # the study. Any additional runtime blocker means the system is not ready
    # even for a re-entry review.
    unexpected = sorted(readiness_blockers - {"DRAWDOWN_ABOVE_LIMIT"})
    if unexpected:
        blockers.append("RUNTIME_READINESS_BLOCKERS:" + ",".join(unexpected))
    if "DRAWDOWN_ABOVE_LIMIT" not in readiness_blockers:
        blockers.append("LIFETIME_DRAWDOWN_GATE_NOT_PRESENT")

    conservative = (
        float(readiness.get("max_risk_pct", 0) or 0) <= 0.0025
        and float(readiness.get("margin_fraction", 0) or 0) <= 0.25
        and _int(readiness.get("max_positions")) <= 1
        and _int(readiness.get("pilot_max_positions")) <= 1
        and _int(readiness.get("pilot_max_submissions")) <= 1
        and readiness.get("recovery_authorized") is False
        and readiness.get("override") is False
        and readiness.get("preflight_ready") is True
        and _int(readiness.get("positions")) == 0
        and _int(readiness.get("pending_orders")) == 0
    )
    if not conservative:
        blockers.append("CONSERVATIVE_REENTRY_PROFILE_NOT_PROVEN")

    status = (
        "EVIDENCE_SUFFICIENT_FOR_MANUAL_REVIEW"
        if not blockers else "COLLECTING_OR_BLOCKED"
    )
    return {
        "status": status,
        "blockers": tuple(blockers),
        "target_market_pipeline_candidates": target,
        "market_pipeline_candidates": int(epoch.get("traversed_to_nexus", 0) or 0),
        "observed_60m": int(epoch.get("observed_60m", 0) or 0),
        "nexus_allowed": int(epoch.get("nexus_allowed", 0) or 0),
        "execution_proof_requirements": EXECUTION_PROOF_REQUIREMENTS,
        "execution_proof_status": "REQUIRES_CURRENT_SHA_CI_AND_RUNTIME_REVIEW",
        "manual_loss_acceptance_required": True,
        "explicit_live_authorization_required": True,
        "automatic_promotion": False,
        **AUTHORITY,
    }


def format_log(row: dict) -> str:
    def fmt(value):
        if isinstance(value, bool):
            return str(value).lower()
        if value is None:
            return "NA"
        if isinstance(value, (tuple, list)):
            return ",".join(map(str, value)) or "NONE"
        return str(value).replace(" ", "_")
    keys = (
        "status", "blockers", "target_market_pipeline_candidates",
        "market_pipeline_candidates", "observed_60m", "nexus_allowed",
        "execution_proof_status", "manual_loss_acceptance_required",
        "explicit_live_authorization_required", "automatic_promotion",
        "promotion_allowed", "live_allowed", "decision_effect", "execution_effect",
    )
    return "[REENTRY_PROMOTION_STUDY_V1] " + " ".join(
        f"{key}={fmt(row.get(key))}" for key in keys
    )
