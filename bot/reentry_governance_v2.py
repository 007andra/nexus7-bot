"""REENTRY_GOVERNANCE_V2.

Fail-closed research model for a future segregated re-entry pilot.

The current lifetime drawdown ledger, HWM and hard gate remain authoritative and
untouched. This module does not reset losses, change drawdown limits, authorize
recovery/override, change risk/sizing/leverage/thresholds, move funds, create an
account, place orders or grant LIVE authority.

Its only purpose is to define what evidence and controls would be required
before a separately governed pilot could even be presented for manual approval.
"""
from __future__ import annotations

import math

AUTHORITY = {
    "authority": "GOVERNANCE_RESEARCH_ONLY",
    "historical_loss_ledger_preserved": True,
    "historical_hwm_preserved": True,
    "lifetime_drawdown_preserved": True,
    "current_hard_gate_unchanged": True,
    "external_capital_does_not_clear_drawdown": True,
    "recovery_override_unchanged": True,
    "thresholds_unchanged": True,
    "risk_policy_unchanged": True,
    "sizing_unchanged": True,
    "leverage_unchanged": True,
    "fund_movement_authorized": False,
    "account_creation_authorized": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}

SEGREGATED_PILOT_CONTROLS = {
    "distinct_pilot_ledger_required": True,
    "historical_ledger_link_required": True,
    "independent_capital_proof_required": True,
    "absolute_loss_budget_required": True,
    "absolute_loss_budget_usdt": None,
    "single_trade_stop_required": True,
    "max_positions": 1,
    "max_new_submissions": 1,
    "averaging_down_allowed": False,
    "martingale_allowed": False,
    "loss_budget_breach_action": "BLOCK_NEW_ENTRIES",
    "manual_loss_acceptance_required": True,
    "explicit_live_authorization_required": True,
    "current_account_hard_gate_bypass_allowed": False,
}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def evaluate(readiness: dict, oos: dict, release_board: dict) -> dict:
    dd = _finite(readiness.get("drawdown"))
    limit = _finite(readiness.get("configured_limit"))
    equity = _finite(readiness.get("equity"))
    max_risk_pct = _finite(readiness.get("max_risk_pct"))

    inherited_stop_budget = (
        equity * max_risk_pct
        if equity is not None and max_risk_pct is not None
        else None
    )

    oos_status = str(oos.get("status") or "UNAVAILABLE")
    runtime_pass = release_board.get("m4_runtime_precheck") == "PASS"
    oos_ready = oos_status == "READY_FOR_MANUAL_REVIEW"
    oos_failed = oos_status == "EVIDENCE_FAIL"

    blockers = []
    if not oos_ready:
        blockers.append("PROSPECTIVE_OOS_NOT_READY")
    if not runtime_pass:
        blockers.append("RUNTIME_PRECHECK_NOT_READY")
    if SEGREGATED_PILOT_CONTROLS["absolute_loss_budget_usdt"] is None:
        blockers.append("ABSOLUTE_LOSS_BUDGET_UNSET")
    blockers.extend((
        "DISTINCT_PILOT_LEDGER_NOT_PROVEN",
        "INDEPENDENT_CAPITAL_PROOF_NOT_PROVEN",
        "EXPLICIT_LIVE_AUTHORIZATION_NOT_GRANTED",
    ))

    if oos_failed:
        status = "DO_NOT_ADVANCE_EDGE_FAILED"
    elif oos_ready and runtime_pass:
        status = "DESIGN_READY_FOR_GOVERNANCE_REVIEW"
    else:
        status = "DESIGN_ONLY_AWAITING_EVIDENCE"

    # Even DESIGN_READY_FOR_GOVERNANCE_REVIEW remains non-executable because
    # budget/ledger/capital/auth proof are deliberately not satisfiable here.
    return {
        **AUTHORITY,
        "status": status,
        "blockers": tuple(blockers),
        "current_account_status": (
            "HARD_GATE_BLOCKED"
            if readiness.get("status") != "READY"
            else "READINESS_PASS"
        ),
        "current_account_drawdown": dd,
        "current_account_drawdown_limit": limit,
        "current_account_equity": equity,
        "current_account_max_risk_pct": max_risk_pct,
        "current_account_inherited_single_trade_stop_budget": inherited_stop_budget,
        "current_account_entries_blocked": readiness.get("status") != "READY",
        "current_account_gate_bypass_proposed": False,
        "m1_risk_gate": release_board.get("m1_risk_gate", "UNKNOWN"),
        "m2_canonical_pipeline": release_board.get("m2_canonical_pipeline", "UNKNOWN"),
        "m3_edge_evidence": release_board.get("m3_edge_evidence", "UNKNOWN"),
        "m4_runtime_precheck": release_board.get("m4_runtime_precheck", "UNKNOWN"),
        "prospective_oos_status": oos_status,
        "prospective_oos_enrolled": _int(oos.get("enrolled_candidates")),
        "prospective_oos_observed_60m": _int(oos.get("observed_60m")),
        "prospective_oos_observed_240m": _int(oos.get("observed_240m")),
        "segregated_pilot": dict(SEGREGATED_PILOT_CONTROLS),
        "m1_resolution_path": (
            "SEPARATE_GOVERNANCE_LEDGER_ONLY; "
            "DO_NOT_RESET_HWM_OR_REWRITE_LIFETIME_DRAWDOWN"
        ),
        "governance_review_required": True,
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
    pilot = row.get("segregated_pilot") or {}
    fields = {
        "status": row.get("status"),
        "blockers": row.get("blockers"),
        "current_account_status": row.get("current_account_status"),
        "drawdown": row.get("current_account_drawdown"),
        "configured_limit": row.get("current_account_drawdown_limit"),
        "equity": row.get("current_account_equity"),
        "max_risk_pct": row.get("current_account_max_risk_pct"),
        "inherited_single_trade_stop_budget": (
            row.get("current_account_inherited_single_trade_stop_budget")
        ),
        "current_account_entries_blocked": row.get("current_account_entries_blocked"),
        "current_account_gate_bypass_proposed": row.get("current_account_gate_bypass_proposed"),
        "m1_risk_gate": row.get("m1_risk_gate"),
        "m2_canonical_pipeline": row.get("m2_canonical_pipeline"),
        "m3_edge_evidence": row.get("m3_edge_evidence"),
        "m4_runtime_precheck": row.get("m4_runtime_precheck"),
        "prospective_oos_status": row.get("prospective_oos_status"),
        "prospective_oos_enrolled": row.get("prospective_oos_enrolled"),
        "prospective_oos_observed_60m": row.get("prospective_oos_observed_60m"),
        "prospective_oos_observed_240m": row.get("prospective_oos_observed_240m"),
        "distinct_pilot_ledger_required": pilot.get("distinct_pilot_ledger_required"),
        "independent_capital_proof_required": pilot.get("independent_capital_proof_required"),
        "absolute_loss_budget_required": pilot.get("absolute_loss_budget_required"),
        "absolute_loss_budget_usdt": pilot.get("absolute_loss_budget_usdt"),
        "max_positions": pilot.get("max_positions"),
        "max_new_submissions": pilot.get("max_new_submissions"),
        "averaging_down_allowed": pilot.get("averaging_down_allowed"),
        "martingale_allowed": pilot.get("martingale_allowed"),
        "historical_loss_ledger_preserved": row.get("historical_loss_ledger_preserved"),
        "historical_hwm_preserved": row.get("historical_hwm_preserved"),
        "lifetime_drawdown_preserved": row.get("lifetime_drawdown_preserved"),
        "current_hard_gate_unchanged": row.get("current_hard_gate_unchanged"),
        "external_capital_does_not_clear_drawdown": row.get("external_capital_does_not_clear_drawdown"),
        "promotion_allowed": row.get("promotion_allowed"),
        "live_allowed": row.get("live_allowed"),
        "decision_effect": row.get("decision_effect"),
        "execution_effect": row.get("execution_effect"),
    }
    return "[REENTRY_GOVERNANCE_V2] " + " ".join(
        f"{key}={_fmt(value)}" for key, value in fields.items()
    )
