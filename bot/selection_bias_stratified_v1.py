"""Research-only stratified selection-bias diagnostic for frozen shadow outcomes.

Compares approvals and rejections within *identical* side/regime/setup strata.
This is descriptive, not a causal adjustment, trading score, or permission.
Never emits symbols, candidate identifiers, full records or credentials.
"""
from __future__ import annotations

from collections import defaultdict
import math

MIN_PER_ARM = 5
AUTHORITY = {
    "research_only": True,
    "prospective_hypothesis_unchanged": True,
    "association_not_causation": True,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _avg(values):
    return sum(values) / len(values) if values else None


def evaluate(matched_rows, *, min_per_arm=MIN_PER_ARM):
    """Return overlap-weighted mean lift; no claims beyond observed matched rows.

    A stratum contributes only when >=min_per_arm valid observed returns
    exist in *each* approval arm, preventing comparison to a missing control.
    Weight = min(n_allowed,n_rejected), explicitly not causal propensity score.
    """
    if isinstance(min_per_arm, bool) or not isinstance(min_per_arm, int) or min_per_arm < 2:
        raise ValueError("INVALID_STRATIFIED_MIN_PER_ARM")
    groups = defaultdict(lambda: {"allowed": [], "rejected": []})
    all_allowed, all_rejected = [], []
    for row in matched_rows:
        if not isinstance(row, dict) or row.get("status") == "ERROR":
            continue
        allowed = row.get("allowed")
        result = _finite(row.get("future_return"))
        if type(allowed) is not bool or result is None:
            continue
        key = tuple(str(row.get(n) or "UNKNOWN").upper() for n in ("side", "regime", "setup"))
        name = "allowed" if allowed else "rejected"
        groups[key][name].append(result)
        (all_allowed if allowed else all_rejected).append(result)

    eligible, compared_a, compared_r = [], 0, 0
    weighted_sum, total_weight = 0.0, 0
    for key in sorted(groups):
        arms = groups[key]
        a, r = arms["allowed"], arms["rejected"]
        if len(a) < min_per_arm or len(r) < min_per_arm:
            continue
        weight = min(len(a), len(r))
        delta = _avg(a) - _avg(r)
        weighted_sum += weight * delta
        total_weight += weight
        compared_a += len(a)
        compared_r += len(r)
        eligible.append({
            "side": key[0], "regime": key[1], "setup": key[2],
            "allowed_n": len(a), "rejected_n": len(r),
            "allowed_avg_return": _avg(a), "rejected_avg_return": _avg(r),
            "lift": delta, "overlap_weight": weight,
        })

    within_lift = weighted_sum / total_weight if total_weight else None
    overall_lift = (
        _avg(all_allowed) - _avg(all_rejected)
        if all_allowed and all_rejected else None
    )
    reversal = (
        within_lift is not None and overall_lift is not None
        and within_lift * overall_lift < 0.0
    )
    return {
        **AUTHORITY,
        "status": "COMPARABLE_OVERLAP" if eligible else "INSUFFICIENT_OVERLAP",
        "min_per_arm": min_per_arm,
        "observed_allowed_n": len(all_allowed),
        "observed_rejected_n": len(all_rejected),
        "eligible_strata_n": len(eligible),
        "total_strata_n": len(groups),
        "eligible_allowed_n": compared_a,
        "eligible_rejected_n": compared_r,
        "eligible_allowed_share": compared_a / len(all_allowed) if all_allowed else 0.0,
        "eligible_rejected_share": compared_r / len(all_rejected) if all_rejected else 0.0,
        "overall_mean_lift": overall_lift,
        "within_strata_weighted_lift": within_lift,
        "possible_simpson_reversal": reversal,
        "strata": tuple(eligible),
        "interpretation_guard": "WITHIN_STRATA_ASSOCIATION_NOT_CAUSAL_OR_LIVE_EVIDENCE",
    }


def _fmt(v):
    return "NA" if v is None else f"{v:.6f}"


def format_log(row, *, horizon):
    if horizon not in (60, 240):
        raise ValueError("INVALID_HORIZON")
    return (
        f"[COUNTERFACTUAL_SELECTION_STRATA_{horizon}M_V1] "
        f"status={row['status']} min_per_arm={row['min_per_arm']} "
        f"eligible_strata={row['eligible_strata_n']}/{row['total_strata_n']} "
        f"eligible_allowed={row['eligible_allowed_n']}/{row['observed_allowed_n']} "
        f"eligible_rejected={row['eligible_rejected_n']}/{row['observed_rejected_n']} "
        f"overall_lift={_fmt(row['overall_mean_lift'])} "
        f"within_strata_weighted_lift={_fmt(row['within_strata_weighted_lift'])} "
        f"possible_simpson_reversal={str(row['possible_simpson_reversal']).lower()} "
        "association_not_causation=true research_only=true thresholds_unchanged=true "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )
