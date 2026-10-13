"""Offline, aggregated setup-exclusion sensitivity for frozen OOS reports.

Research-only: receives existing counterfactual approval-failure report aggregates.
No DB access, no network, no exchange, no symbol/candidate IDs, no runtime hooks.
For each setup, subtract its observed contribution from BOTH approval arms,
preserving original cohort and the original observed denominators.

A positive result after exclusion is a discovery hypothesis, NOT a validated
strategy or permission to change production vetoes. No automatic promotion.
"""
from __future__ import annotations

import math

SETUPS = ("MOMENTUM", "BOS_BREAK", "PULLBACK")
HORIZONS = (60, 240)
MIN_REMAINDER_PER_ARM = 5
AUTHORITY = {
    "research_only": True,
    "historical_sensitivity_only": True,
    "association_not_causation": True,
    "gross_hypothetical_returns_only": True,
    "cohort_membership_unchanged": True,
    "setup_vetoes_unchanged": True,
    "thresholds_unchanged": True,
    "promotion_allowed": False,
    "live_allowed": False,
    "execution_effect": "NONE",
    "decision_effect": "NONE",
}


def _count(v):
    if type(v) is not int or v < 0:
        raise ValueError("RCA_INVALID_COUNT")
    return v


def _avg(v, n):
    if n == 0 and v is None:
        return None
    if type(v) not in (float, int) or not math.isfinite(v):
        raise ValueError("RCA_INVALID_MEAN")
    if n == 0:
        raise ValueError("RCA_MEAN_WITHOUT_SAMPLES")
    return float(v)


def _performance(report, arm, horizon):
    value = report.get(f"{arm}_{horizon}m")
    if not isinstance(value, dict):
        raise ValueError("RCA_MISSING_ARM_METRICS")
    n = _count(value.get("n"))
    mean = _avg(value.get("avg_return"), n)
    return n, mean


def _groups(report, horizon):
    dimensions = report.get(f"dimensions_{horizon}m")
    if not isinstance(dimensions, dict) or not isinstance(dimensions.get("setup"), list):
        raise ValueError("RCA_SETUP_AGGREGATES_MISSING")
    groups = {}
    for x in dimensions["setup"]:
        if not isinstance(x, dict) or x.get("dimension") != "setup":
            raise ValueError("RCA_INVALID_SETUP_GROUP")
        label = str(x.get("value") or "").upper()
        # The report may contain other/unclassified setups. They remain in
        # baseline totals, never silently reclassified into the target group.
        if not label or label in groups:
            raise ValueError("RCA_INVALID_SETUP_LABEL")
        an = _count(x.get("allowed_n"))
        rn = _count(x.get("rejected_n"))
        groups[label] = (
            (an, _avg(x.get("allowed_avg_return"), an)),
            (rn, _avg(x.get("rejected_avg_return"), rn)),
        )
    return groups


def _without(total_n, total_mean, excluded_n, excluded_mean):
    if excluded_n > total_n:
        raise ValueError("RCA_GROUP_EXCEEDS_BASELINE")
    remainder = total_n - excluded_n
    if remainder == 0:
        return 0, None
    total_sum = total_n * total_mean
    excluded_sum = excluded_n * excluded_mean if excluded_n else 0.0
    return remainder, (total_sum - excluded_sum) / remainder


def evaluate(report: dict, *, min_remainder=MIN_REMAINDER_PER_ARM) -> dict:
    """Return a descriptive historical 'remove one setup from both arms' table."""
    if type(min_remainder) is not int or min_remainder < 2:
        raise ValueError("RCA_INVALID_MINIMUM")
    if not isinstance(report, dict):
        raise ValueError("RCA_INVALID_REPORT")
    results = []
    for horizon in HORIZONS:
        a_n, a_m = _performance(report, "allowed", horizon)
        r_n, r_m = _performance(report, "rejected", horizon)
        if not a_n or not r_n:
            raise ValueError("RCA_INSUFFICIENT_BASELINE")
        baseline_lift = a_m - r_m
        groups = _groups(report, horizon)
        # Guard scope consistency: dimensions must partition matched arms.
        if (sum(arms[0][0] for arms in groups.values()) != a_n
                or sum(arms[1][0] for arms in groups.values()) != r_n):
            raise ValueError("RCA_GROUP_COUNTS_DO_NOT_PARTITION")
        for setup in SETUPS:
            if setup not in groups:
                results.append({
                    "horizon_min": horizon, "excluded_setup": setup,
                    "status": "NOT_OBSERVED", "baseline_lift": baseline_lift,
                    "excluded_allowed_n": 0, "excluded_rejected_n": 0,
                    "remaining_allowed_n": a_n, "remaining_rejected_n": r_n,
                    "remaining_lift": None, "change_in_lift": None,
                })
                continue
            (drop_a_n, drop_a_m), (drop_r_n, drop_r_m) = groups[setup]
            remaining_a_n, remaining_a_m = _without(a_n, a_m, drop_a_n, drop_a_m)
            remaining_r_n, remaining_r_m = _without(r_n, r_m, drop_r_n, drop_r_m)
            comparable = (remaining_a_n >= min_remainder and
                          remaining_r_n >= min_remainder)
            remaining_lift = (remaining_a_m - remaining_r_m) if comparable else None
            results.append({
                "horizon_min": horizon, "excluded_setup": setup,
                "status": "DESCRIPTIVE_ONLY" if comparable else "INSUFFICIENT_REMAINDER",
                "baseline_lift": baseline_lift,
                "excluded_allowed_n": drop_a_n,
                "excluded_rejected_n": drop_r_n,
                "remaining_allowed_n": remaining_a_n,
                "remaining_rejected_n": remaining_r_n,
                "remaining_lift": remaining_lift,
                "change_in_lift": (
                    remaining_lift - baseline_lift
                    if remaining_lift is not None else None
                ),
            })
    return {
        **AUTHORITY,
        "status": "HISTORICAL_SETUP_SENSITIVITY_ONLY",
        "source_status": str(report.get("status") or "UNKNOWN"),
        "results": tuple(results),
        "prospective_confirmation_required": True,
        "cannot_reset_or_relabel_frozen_oos": True,
        "real_execution_net_proof": False,
    }


def public_summary(report: dict) -> dict:
    """Allowlisted aggregate-only: never echo raw source payloads or IDs."""
    result = evaluate(report)
    return {
        key: value for key, value in result.items()
        if key != "results"
    } | {"results": result["results"]}
