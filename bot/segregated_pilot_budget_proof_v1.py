"""SEGREGATED_PILOT_BUDGET_PROOF_V1.

Deterministic, non-persistent replay proving the arithmetic and fail-closed
behavior of the segregated 5R shadow budget guard. It never reads/writes the
production trade ledger, never enrolls OOS candidates, and never creates orders.

Given a positive risk unit and absolute budget, the proof replays a fixed number
of synthetic 1R reservation attempts. Attempts reserve only while remaining
budget can fully cover 1R; later attempts must become SHADOW_BUDGET_BLOCK.
"""
from __future__ import annotations

import math

SYNTHETIC_ATTEMPTS = 7

AUTHORITY = {
    "research_only": True,
    "shadow_only": True,
    "synthetic_replay": True,
    "synthetic_entries_persisted": False,
    "oos_enrollment_credit": False,
    "canonical_pipeline_credit": False,
    "historical_loss_ledger_untouched": True,
    "historical_hwm_preserved": True,
    "lifetime_drawdown_preserved": True,
    "current_hard_gate_unchanged": True,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def evaluate(budget_study: dict, *, attempts: int = SYNTHETIC_ATTEMPTS) -> dict:
    risk_unit = _finite(budget_study.get("risk_unit_usdt"))
    budget = _finite(budget_study.get("shadow_reference_budget_usdt"))

    valid = (
        risk_unit is not None and risk_unit > 0.0
        and budget is not None and budget > 0.0
        and isinstance(attempts, int) and attempts >= 1
    )
    if not valid:
        return {
            **AUTHORITY,
            "status": "INPUT_UNAVAILABLE",
            "attempts": attempts,
            "risk_unit_usdt": risk_unit,
            "reference_budget_usdt": budget,
            "reserved_attempts": 0,
            "blocked_attempts": 0,
            "reserved_loss_usdt": 0.0,
            "remaining_budget_usdt": budget,
            "expected_reservable_units": None,
            "sequence": (),
            "proof_pass": False,
        }

    remaining = budget
    sequence = []
    reserved_n = 0
    blocked_n = 0
    reserved_loss = 0.0

    for index in range(1, attempts + 1):
        before = max(0.0, remaining)
        if before + 1e-12 >= risk_unit:
            status = "SHADOW_RESERVED"
            amount = risk_unit
            reserved_n += 1
            reserved_loss += amount
            remaining = max(0.0, before - amount)
        else:
            status = "SHADOW_BUDGET_BLOCK"
            amount = 0.0
            blocked_n += 1
            remaining = before
        sequence.append({
            "attempt": index,
            "status": status,
            "reserved_loss_usdt": amount,
            "remaining_before_usdt": before,
            "remaining_after_usdt": remaining,
        })

    expected_units = int(math.floor((budget + 1e-12) / risk_unit))
    expected_reserved = min(attempts, expected_units)
    expected_blocked = max(0, attempts - expected_units)
    proof_pass = (
        reserved_n == expected_reserved
        and blocked_n == expected_blocked
        and reserved_loss <= budget + 1e-12
        and remaining >= -1e-12
        and all(
            row["status"] == "SHADOW_RESERVED"
            for row in sequence[:expected_reserved]
        )
        and all(
            row["status"] == "SHADOW_BUDGET_BLOCK"
            for row in sequence[expected_reserved:]
        )
    )

    return {
        **AUTHORITY,
        "status": "PROOF_PASS" if proof_pass else "PROOF_FAIL",
        "attempts": attempts,
        "risk_unit_usdt": risk_unit,
        "reference_budget_usdt": budget,
        "expected_reservable_units": expected_units,
        "reserved_attempts": reserved_n,
        "blocked_attempts": blocked_n,
        "reserved_loss_usdt": reserved_loss,
        "remaining_budget_usdt": max(0.0, remaining),
        "sequence": tuple(sequence),
        "proof_pass": proof_pass,
        "invariant": "NO_PARTIAL_RESERVATION_AND_NO_BUDGET_OVERRUN",
    }


def format_log(row: dict) -> str:
    sequence = "|".join(
        f"{item['attempt']}:{item['status']}"
        for item in row.get("sequence") or ()
    ) or "NONE"
    return (
        "[SEGREGATED_PILOT_BUDGET_PROOF_V1] "
        f"status={row.get('status')} proof_pass={str(bool(row.get('proof_pass'))).lower()} "
        f"attempts={row.get('attempts')} "
        f"risk_unit_usdt={row.get('risk_unit_usdt')} "
        f"reference_budget_usdt={row.get('reference_budget_usdt')} "
        f"expected_reservable_units={row.get('expected_reservable_units')} "
        f"reserved_attempts={row.get('reserved_attempts')} "
        f"blocked_attempts={row.get('blocked_attempts')} "
        f"reserved_loss_usdt={row.get('reserved_loss_usdt')} "
        f"remaining_budget_usdt={row.get('remaining_budget_usdt')} "
        f"sequence={sequence} "
        "synthetic_entries_persisted=false oos_enrollment_credit=false "
        "canonical_pipeline_credit=false historical_loss_ledger_untouched=true "
        "historical_hwm_preserved=true lifetime_drawdown_preserved=true "
        "current_hard_gate_unchanged=true promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )
