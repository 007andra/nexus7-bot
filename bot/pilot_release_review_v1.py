"""PILOT_RELEASE_REVIEW_V1.

Research-only consolidated review for a future segregated pilot.
A READY status means only that the design/evidence package is ready for human
review. It never grants LIVE execution authority and cannot bypass M1/M2/M3.
"""
from __future__ import annotations

AUTHORITY = {
    "authority": "MANUAL_REVIEW_ONLY",
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "explicit_live_authorization_required": True,
}


def evaluate(
    readiness: dict,
    release_board: dict,
    oos: dict,
    budget_study: dict,
    ledger: dict,
    *,
    budget_proof: dict | None = None,
) -> dict:
    blockers = []
    oos_ready = oos.get("status") == "READY_FOR_MANUAL_REVIEW"
    runtime_ready = release_board.get("m4_runtime_precheck") == "PASS"
    budget_ready = budget_study.get("status") == "AVAILABLE_RESEARCH_ONLY"
    ledger_ready = (
        ledger.get("status") == "SHADOW_LEDGER_ACTIVE"
        and ledger.get("budget_guard_configured") is True
        and ledger.get("isolation_contract_active") is True
        and ledger.get("enrollment_scope") == "EXACT_PROSPECTIVE_OOS_COHORT"
        and ledger.get("historical_loss_ledger_untouched") is True
        and ledger.get("historical_hwm_preserved") is True
        and ledger.get("lifetime_drawdown_preserved") is True
        and ledger.get("current_hard_gate_unchanged") is True
    )
    budget_proof_ready = (
        (budget_proof or {}).get("status") == "PROOF_PASS"
        and (budget_proof or {}).get("proof_pass") is True
        and (budget_proof or {}).get("synthetic_entries_persisted") is False
        and (budget_proof or {}).get("oos_enrollment_credit") is False
        and (budget_proof or {}).get("canonical_pipeline_credit") is False
    )

    if not oos_ready:
        blockers.append("PROSPECTIVE_OOS_NOT_READY")
    if not runtime_ready:
        blockers.append("RUNTIME_PRECHECK_NOT_READY")
    if not budget_ready:
        blockers.append("BUDGET_STUDY_UNAVAILABLE")
    if not ledger_ready:
        blockers.append("SEGREGATED_LEDGER_SHADOW_PROOF_NOT_READY")
    if not budget_proof_ready:
        blockers.append("SEGREGATED_BUDGET_GUARD_PROOF_NOT_READY")

    # These remain mandatory even if all research evidence passes.
    blockers.extend((
        "INDEPENDENT_CAPITAL_PROOF_NOT_PROVEN",
        "LIVE_ABSOLUTE_LOSS_BUDGET_NOT_APPROVED",
        "EXPLICIT_LIVE_AUTHORIZATION_NOT_GRANTED",
        "LIVE_SEGREGATED_EXECUTION_PATH_NOT_IMPLEMENTED",
    ))

    research_package_ready = (
        oos_ready and runtime_ready and budget_ready and ledger_ready
        and budget_proof_ready
    )
    status = (
        "DESIGN_EVIDENCE_READY_FOR_MANUAL_REVIEW"
        if research_package_ready
        else "BLOCKED_AWAITING_RESEARCH_EVIDENCE"
    )

    return {
        **AUTHORITY,
        "status": status,
        "blockers": tuple(blockers),
        "research_package_ready": research_package_ready,
        "m1_risk_gate": release_board.get("m1_risk_gate", "UNKNOWN"),
        "m2_canonical_pipeline": release_board.get("m2_canonical_pipeline", "UNKNOWN"),
        "m3_edge_evidence": release_board.get("m3_edge_evidence", "UNKNOWN"),
        "m4_runtime_precheck": release_board.get("m4_runtime_precheck", "UNKNOWN"),
        "oos_status": oos.get("status"),
        "oos_enrolled": oos.get("enrolled_candidates"),
        "oos_observed_60m": oos.get("observed_60m"),
        "oos_observed_240m": oos.get("observed_240m"),
        "budget_study_status": budget_study.get("status"),
        "shadow_reference_budget_usdt": budget_study.get(
            "shadow_reference_budget_usdt"
        ),
        "segregated_ledger_status": ledger.get("status"),
        "segregated_ledger_remaining_budget_usdt": ledger.get(
            "remaining_budget_usdt"
        ),
        "segregated_ledger_budget_blocked_entries": ledger.get(
            "budget_blocked_entries"
        ),
        "segregated_ledger_enrollment_scope": ledger.get("enrollment_scope"),
        "budget_guard_proof_status": (budget_proof or {}).get("status"),
        "budget_guard_proof_pass": bool((budget_proof or {}).get("proof_pass")),
        "budget_guard_proof_reserved_attempts": (budget_proof or {}).get(
            "reserved_attempts"
        ),
        "budget_guard_proof_blocked_attempts": (budget_proof or {}).get(
            "blocked_attempts"
        ),
        "budget_guard_proof_synthetic_entries_persisted": (budget_proof or {}).get(
            "synthetic_entries_persisted"
        ),
        "current_account_entries_blocked": readiness.get("status") != "READY",
        "current_account_gate_bypass_allowed": False,
        "historical_hwm_reset_allowed": False,
        "lifetime_drawdown_rewrite_allowed": False,
        "external_capital_clears_lifetime_drawdown": False,
    }


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, (tuple, list)):
        return ",".join(map(str, value)) or "NONE"
    return str(value).replace(" ", "_")


def format_log(row: dict) -> str:
    keys = (
        "status", "blockers", "research_package_ready",
        "m1_risk_gate", "m2_canonical_pipeline",
        "m3_edge_evidence", "m4_runtime_precheck",
        "oos_status", "oos_enrolled", "oos_observed_60m", "oos_observed_240m",
        "budget_study_status", "shadow_reference_budget_usdt",
        "segregated_ledger_status", "segregated_ledger_remaining_budget_usdt",
        "segregated_ledger_budget_blocked_entries",
        "segregated_ledger_enrollment_scope",
        "budget_guard_proof_status", "budget_guard_proof_pass",
        "budget_guard_proof_reserved_attempts", "budget_guard_proof_blocked_attempts",
        "budget_guard_proof_synthetic_entries_persisted",
        "current_account_entries_blocked", "current_account_gate_bypass_allowed",
        "historical_hwm_reset_allowed", "lifetime_drawdown_rewrite_allowed",
        "external_capital_clears_lifetime_drawdown",
        "promotion_allowed", "live_allowed", "decision_effect", "execution_effect",
    )
    return "[PILOT_RELEASE_REVIEW_V1] " + " ".join(
        f"{k}={_fmt(row.get(k))}" for k in keys
    )
