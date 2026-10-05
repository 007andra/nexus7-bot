"""Calibration Failure Analysis V1.

Research-only calibration diagnostics over the existing MIN_ORDER-blocked
counterfactual cohort. Fixed, predeclared bins test whether larger NEXUS
confidence/R:R/EV/score/setup-quality are monotonically associated with better
60m and 240m hypothetical outcomes.

This is discovery evidence only. It never changes production thresholds,
scoring, risk, sizing, dispatch, drawdown/HWM, recovery or LIVE authority.
"""
from __future__ import annotations

import json
import math
import os

FLAG = "CALIBRATION_FAILURE_ANALYSIS_V1"
POPULATION = "HARD_GATE_SHADOW"
MIN_BIN_N = 5

AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "association_not_causation": True,
    "fixed_bins": True,
    "production_scoring_unchanged": True,
    "thresholds_unchanged": True,
    "risk_epoch_traversal_credit": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}

BINS = {
    "score": ((0, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 101)),
    "confidence": ((0, 30), (30, 45), (45, 60), (60, 75), (75, 101)),
    "risk_reward": ((0, 1.30), (1.30, 1.60), (1.60, 2.00), (2.00, 2.50), (2.50, 100)),
    "expected_value": ((-100, 0), (0, 0.25), (0.25, 0.50), (0.50, 1.00), (1.00, 100)),
    "setup_quality": ((0, 40), (40, 60), (60, 75), (75, 90), (90, 101)),
}


def enabled() -> bool:
    return os.environ.get(FLAG, "true").strip().lower() in {"1", "true", "yes", "on"}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _mean(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return sum(xs) / len(xs) if xs else None


def _rate(num, den):
    return float(num) / float(den) if den else None


def _bin_label(lo, hi):
    return f"[{lo:g},{hi:g})"


def _bin_rows(rows, feature):
    out = []
    for lo, hi in BINS[feature]:
        members = [
            row for row in rows
            if _finite(row.get(feature)) is not None
            and lo <= float(row[feature]) < hi
        ]
        out.append({
            "feature": feature,
            "range": _bin_label(lo, hi),
            "low": lo,
            "high": hi,
            "n": len(members),
            "allowed_n": sum(1 for r in members if r.get("allowed")),
            "avg_return": _mean(r.get("future_return") for r in members),
            "positive_rate": _rate(
                sum(1 for r in members if float(r["future_return"]) > 0), len(members)
            ),
            "avg_mfe": _mean(r.get("MFE") for r in members),
            "avg_mae": _mean(r.get("MAE") for r in members),
        })
    return out


def _calibration(rows, feature):
    bins = _bin_rows(rows, feature)
    usable = [b for b in bins if b["n"] >= MIN_BIN_N and b["avg_return"] is not None]
    inversions = []
    for left, right in zip(usable, usable[1:]):
        if float(right["avg_return"]) < float(left["avg_return"]):
            inversions.append({
                "lower_bin": left["range"],
                "higher_bin": right["range"],
                "lower_avg_return": left["avg_return"],
                "higher_avg_return": right["avg_return"],
                "delta": right["avg_return"] - left["avg_return"],
            })
    return {
        "feature": feature,
        "bins": bins,
        "usable_bins": len(usable),
        "inversions": inversions,
        "inversion_count": len(inversions),
        "monotonic_non_decreasing": len(usable) >= 2 and not inversions,
    }


def build_report(payloads, outcomes60=(), outcomes240=(), *, epoch_id="UNKNOWN", started_epoch=None):
    from bot import counterfactual_approval_failure_analysis_v1 as base

    records = base._records(payloads)
    m60 = base._matched(records, base._outcome_map(outcomes60))
    m240 = base._matched(records, base._outcome_map(outcomes240))

    calibrations60 = [_calibration(m60, feature) for feature in BINS]
    calibrations240 = [_calibration(m240, feature) for feature in BINS]
    approved60 = [r for r in m60 if r.get("allowed")]
    approved240 = [r for r in m240 if r.get("allowed")]
    approved_cal60 = [_calibration(approved60, feature) for feature in BINS]
    approved_cal240 = [_calibration(approved240, feature) for feature in BINS]

    inv60 = sum(r["inversion_count"] for r in calibrations60)
    inv240 = sum(r["inversion_count"] for r in calibrations240)
    approved_inv60 = sum(r["inversion_count"] for r in approved_cal60)
    approved_inv240 = sum(r["inversion_count"] for r in approved_cal240)

    if len(m60) < 20 or len(m240) < 20:
        status = "INSUFFICIENT_MATCHED_EVIDENCE"
    elif inv60 > 0 and inv240 > 0:
        status = "CALIBRATION_INVERSION_CONFIRMED_BOTH_HORIZONS"
    elif inv60 > 0 or inv240 > 0:
        status = "CALIBRATION_INVERSION_ONE_HORIZON"
    else:
        status = "NO_FIXED_BIN_INVERSION_DETECTED"

    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": status,
        "evaluated": len(records),
        "observed_60m": len(m60),
        "observed_240m": len(m240),
        "approved_observed_60m": len(approved60),
        "approved_observed_240m": len(approved240),
        "inversions_60m": inv60,
        "inversions_240m": inv240,
        "approved_inversions_60m": approved_inv60,
        "approved_inversions_240m": approved_inv240,
        "calibrations_60m": calibrations60,
        "calibrations_240m": calibrations240,
        "approved_calibrations_60m": approved_cal60,
        "approved_calibrations_240m": approved_cal240,
        "prospective_hypothesis": (
            "CURRENT_NEXUS_APPROVAL_MUST_SHOW_POSITIVE_OOS_LIFT_AT_60M_AND_240M"
        ),
        "prospective_hypothesis_frozen": True,
        "interpretation_guard": (
            "DISCOVERY_ONLY_FIXED_BIN_ASSOCIATIONS_REQUIRE_NEW_PROSPECTIVE_OOS_COHORT"
        ),
    }


async def snapshot(db):
    from bot import risk_epoch_shadow

    eid = risk_epoch_shadow.epoch_id()
    meta = await db._fetchall(
        "SELECT payload FROM risk_epoch_shadow_v1 WHERE epoch_id=?", (eid,)
    )
    if not meta:
        return build_report([], epoch_id=eid)
    raw = meta[0]["payload"] if hasattr(meta[0], "keys") else meta[0][0]
    baseline = json.loads(raw)
    started = float(baseline["started_epoch"])

    rows = await db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch",
        (POPULATION, started),
    )
    payloads = []
    for item in rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[0]
        try:
            payloads.append(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    out_rows = await db._fetchall(
        "SELECT o.candidate_id,o.horizon,o.payload "
        "FROM hard_gate_shadow_outcomes_v1 o "
        "JOIN hard_gate_shadow_candidates_v1 c ON c.candidate_id=o.candidate_id "
        "WHERE c.population=? AND c.captured_epoch>=? AND o.horizon IN (?,?)",
        (POPULATION, started, 60, 240),
    )
    out60, out240 = [], []
    for item in out_rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[2]
        cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
        horizon = int(item["horizon"] if hasattr(item, "keys") else item[1])
        try:
            obj = json.loads(raw)
            obj["candidate_id"] = cid
            (out60 if horizon == 60 else out240).append(obj)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return build_report(payloads, out60, out240, epoch_id=eid, started_epoch=started)


def _fmt(value, digits=6):
    return "NA" if value is None else f"{float(value):.{digits}f}"


def format_summary(report):
    return (
        "[CALIBRATION_FAILURE_ANALYSIS_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"evaluated={report['evaluated']} observed_60m={report['observed_60m']} "
        f"observed_240m={report['observed_240m']} "
        f"approved_observed_60m={report['approved_observed_60m']} "
        f"approved_observed_240m={report['approved_observed_240m']} "
        f"inversions_60m={report['inversions_60m']} "
        f"inversions_240m={report['inversions_240m']} "
        f"approved_inversions_60m={report['approved_inversions_60m']} "
        f"approved_inversions_240m={report['approved_inversions_240m']} "
        "prospective_hypothesis_frozen=true thresholds_unchanged=true "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )


def format_features(report, *, horizon=60):
    rows = report["calibrations_60m"] if horizon == 60 else report["calibrations_240m"]
    parts = []
    for row in rows:
        parts.append(
            f"{row['feature']}:usable={row['usable_bins']}:inv={row['inversion_count']}"
            f":monotonic={str(row['monotonic_non_decreasing']).lower()}"
        )
    return (
        f"[CALIBRATION_FAILURE_FEATURES_{horizon}M_V1] "
        + "|".join(parts)
        + " fixed_bins=true association_not_causation=true execution_effect=NONE"
    )
