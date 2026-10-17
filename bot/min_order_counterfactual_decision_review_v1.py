"""Counterfactual Decision Review V1.

Research-only synthesis for the MIN_ORDER-blocked counterfactual NEXUS cohort.
It never changes production thresholds or LIVE authority. Before the cohort has
>=20 evaluations and >=20 matched 60m outcomes it can only return WAIT_FOR_20_20.
After that it may classify the evidence for manual review as:
- KEEP_RR_1_60_PENDING_MORE_EVIDENCE
- STUDY_RR_1_50_MANUAL_REVIEW
- DISCARD_RR_RELAXATION_IN_THIS_SAMPLE

No classification is an automatic policy change.
"""
from __future__ import annotations

import json
import os

FLAG = "MIN_ORDER_COUNTERFACTUAL_DECISION_REVIEW_V1"
POPULATION = "HARD_GATE_SHADOW"
TARGET_EVALUATIONS = 20
TARGET_OUTCOMES_60M = 20

AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
    "risk_epoch_traversal_credit": False,
    "automatic_promotion": False,
    "production_thresholds_unchanged": True,
    "manual_review_required": True,
}


def enabled() -> bool:
    return os.environ.get(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _reason_row(report, reason):
    for row in report.get("outcome_reason_rows") or []:
        if row.get("reason") == reason:
            return row
    return None


def _scenario(report, floor):
    for row in report.get("scenarios") or []:
        if abs(float(row.get("rr_floor", -1)) - float(floor)) < 1e-9:
            return row
    return None


def build_report(payloads, outcomes60=(), *, epoch_id="UNKNOWN", started_epoch=None):
    from bot import min_order_counterfactual_gate_attribution_v1 as attribution
    from bot import min_order_counterfactual_threshold_sensitivity_v1 as sensitivity

    attr = attribution.build_report(
        payloads, outcomes60, epoch_id=epoch_id, started_epoch=started_epoch
    )
    sens = sensitivity.build_report(
        payloads, outcomes60, epoch_id=epoch_id, started_epoch=started_epoch
    )

    evaluated = int(attr.get("evaluated") or 0)
    observed = int(attr.get("observed_60m") or 0)
    recommendation = "WAIT_FOR_20_20"
    rationale = "Insufficient prospective evaluations or matched 60m outcomes."

    rr_row = _reason_row(attr, "RR_BELOW_MIN")
    ev_row = _reason_row(attr, "EV_NEGATIVE")
    s150 = _scenario(sens, 1.50)

    if evaluated >= TARGET_EVALUATIONS and observed >= TARGET_OUTCOMES_60M:
        rr_n = int((rr_row or {}).get("observed_60m") or 0)
        ev_n = int((ev_row or {}).get("observed_60m") or 0)
        rr_avg = (rr_row or {}).get("avg_return")
        ev_avg = (ev_row or {}).get("avg_return")
        rr_pos = (rr_row or {}).get("positive_rate")
        s150_n = int((s150 or {}).get("observed_60m") or 0)
        s150_avg = (s150 or {}).get("avg_return")
        s150_pos = (s150 or {}).get("positive_rate")

        if rr_n == 0 or ev_n == 0:
            recommendation = "KEEP_RR_1_60_PENDING_MORE_EVIDENCE"
            rationale = "20/20 reached but veto-reason groups are imbalanced."
        elif rr_avg is not None and rr_avg <= 0:
            recommendation = "DISCARD_RR_RELAXATION_IN_THIS_SAMPLE"
            rationale = "RR-floor rejects had non-positive average 60m return."
        elif (
            s150_n >= 5
            and s150_avg is not None and s150_avg > 0
            and s150_pos is not None and s150_pos >= 0.60
            and rr_avg is not None and ev_avg is not None and rr_avg > ev_avg
            and rr_pos is not None and rr_pos >= 0.55
        ):
            recommendation = "STUDY_RR_1_50_MANUAL_REVIEW"
            rationale = (
                "RR-floor rejects outperformed EV-negative rejects and the 1.50 "
                "shadow subset showed positive return with >=60% positive rate."
            )
        else:
            recommendation = "KEEP_RR_1_60_PENDING_MORE_EVIDENCE"
            rationale = "Evidence does not justify studying a lower LIVE R:R floor yet."

    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": (
            "WAIT_FOR_20_20"
            if evaluated < TARGET_EVALUATIONS or observed < TARGET_OUTCOMES_60M
            else "EVIDENCE_READY_FOR_MANUAL_REVIEW"
        ),
        "target_evaluations": TARGET_EVALUATIONS,
        "target_outcomes_60m": TARGET_OUTCOMES_60M,
        "evaluated": evaluated,
        "observed_60m": observed,
        "recommendation": recommendation,
        "rationale": rationale,
        "attribution": attr,
        "sensitivity": sens,
        "interpretation_guard": (
            "RESEARCH_RECOMMENDATION_NEVER_CHANGES_PRODUCTION_THRESHOLD"
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
        "SELECT o.candidate_id,o.payload FROM hard_gate_shadow_outcomes_v1 o "
        "JOIN hard_gate_shadow_candidates_v1 c ON c.candidate_id=o.candidate_id "
        "WHERE c.population=? AND c.captured_epoch>=? AND o.horizon=?",
        (POPULATION, started, 60),
    )
    outcomes = []
    for item in out_rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[1]
        cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
        try:
            obj = json.loads(raw)
            obj["candidate_id"] = cid
            outcomes.append(obj)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    return build_report(payloads, outcomes, epoch_id=eid, started_epoch=started)


def format_summary(report):
    return (
        "[MIN_ORDER_COUNTERFACTUAL_DECISION_REVIEW_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"evaluated={report['evaluated']}/{report['target_evaluations']} "
        f"observed_60m={report['observed_60m']}/{report['target_outcomes_60m']} "
        f"recommendation={report['recommendation']} "
        "manual_review_required=true production_thresholds_unchanged=true "
        "risk_epoch_traversal_credit=false automatic_promotion=false "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY", "FLAG", "TARGET_EVALUATIONS", "TARGET_OUTCOMES_60M",
    "build_report", "enabled", "format_summary", "snapshot",
]
