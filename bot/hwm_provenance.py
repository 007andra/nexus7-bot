"""Durable, non-secret provenance payloads for LIVE account-equity HWM transitions.

This module is accounting/observability only. It does not authorize execution,
change risk thresholds, leverage, sizing, strategy, orders, or exchange state.
Persistence is performed atomically with the HWM by drawdown_persistence.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

HWM_PROVENANCE_KEY = "risk:account_equity_peak:provenance:v1"
_ALLOWED_REASONS = {
    "bootstrap",
    "new_equity_high",
    "incident_repair",
    "external_capital_flow_rebase",
    "external_position_performance_rebase",
}


def _finite_positive(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} boolean")
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{label} must be positive and finite")
    return value


def build_hwm_provenance(
    *,
    reason: str,
    old_peak: float | None,
    new_peak: float,
    account_equity: float,
    evidence_ref: str,
) -> str:
    """Build one validated, compact, non-secret provenance snapshot."""
    if reason not in _ALLOWED_REASONS:
        raise ValueError("unsupported HWM provenance reason")
    new_peak = _finite_positive(new_peak, "new_peak")
    account_equity = _finite_positive(account_equity, "account_equity")
    old = None if old_peak is None else _finite_positive(old_peak, "old_peak")
    evidence_ref = str(evidence_ref or "").strip()
    if not evidence_ref or len(evidence_ref) > 160:
        raise ValueError("evidence_ref must be non-empty and <=160 chars")

    payload = {
        "version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "old_peak": old,
        "new_peak": new_peak,
        "account_equity": account_equity,
        "evidence_ref": evidence_ref,
        "execution_effect": "NONE",
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
