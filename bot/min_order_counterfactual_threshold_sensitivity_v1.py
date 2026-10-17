"""Counterfactual R:R threshold sensitivity over the MIN_ORDER-blocked NEXUS cohort.

Research-only. This module changes no production threshold. It applies fixed
shadow R:R floors to already-produced pre-veto metrics while preserving the
existing EV>0 requirement and all non-R:R vetoes. It reports scenario selection
counts immediately and 60m outcome metrics only when matched outcomes exist.
"""
from __future__ import annotations

import json
import math
import os
import statistics

FLAG = "MIN_ORDER_COUNTERFACTUAL_THRESHOLD_SENSITIVITY_V1"
POPULATION = "HARD_GATE_SHADOW"
COHORT = "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS"
RR_SCENARIOS = (1.60, 1.50, 1.40, 1.30)
MIN_OUTCOMES_FOR_CLAIMS = 20

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
}


def enabled() -> bool:
    return os.environ.get(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _finite(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _mean(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return statistics.fmean(xs) if xs else None


def _median(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return statistics.median(xs) if xs else None


def _rate(num, den):
    return float(num) / float(den) if den else None


def _normalize(obs):
    from bot import min_order_counterfactual_nexus_v1 as study
    return study._normalize_record(obs)


def _eligible_for_rr_sensitivity(row):
    """Only candidates whose surviving issue is the R:R floor."""
    if row.get("status") == "ERROR":
        return False
    if row.get("execution_allowed") is True:
        return True
    if row.get("reason_category") != "RR_BELOW_MIN":
        return False
    ev = _finite(row.get("expected_value"))
    return ev is not None and ev > 0.0


def _scenario_selected(row, rr_floor):
    if not _eligible_for_rr_sensitivity(row):
        return False
    rr = _finite(row.get("risk_reward"))
    ev = _finite(row.get("expected_value"))
    return (
        rr is not None
        and ev is not None
        and ev > 0.0
        and rr >= float(rr_floor)
    )


def _outcome_metrics(rows):
    if not rows:
        return {
            "n": 0, "positive": 0, "positive_rate": None,
            "avg_return": None, "median_return": None,
            "avg_mfe": None, "avg_mae": None,
        }
    return {
        "n": len(rows),
        "positive": sum(1 for r in rows if r["future_return"] > 0),
        "positive_rate": _rate(
            sum(1 for r in rows if r["future_return"] > 0), len(rows)
        ),
        "avg_return": _mean(r["future_return"] for r in rows),
        "median_return": _median(r["future_return"] for r in rows),
        "avg_mfe": _mean(r["MFE"] for r in rows),
        "avg_mae": _mean(r["MAE"] for r in rows),
    }


def build_report(payloads, outcomes60=(), *, epoch_id="UNKNOWN", started_epoch=None):
    records = {}
    for raw in payloads:
        obs = raw.get("counterfactual_nexus_v1")
        if not isinstance(obs, dict):
            continue
        if obs.get("cohort") != COHORT:
            continue
        if obs.get("risk_epoch_traversal_credit") is not False:
            continue
        row = _normalize(obs)
        cid = str(row.get("candidate_id") or "")
        if cid:
            records[cid] = row

    outcome_by_id = {}
    for raw in outcomes60:
        cid = str(raw.get("candidate_id") or "")
        if not cid or raw.get("outcome") != "OBSERVED":
            continue
        ret = _finite(raw.get("future_return"))
        mfe = _finite(raw.get("MFE"))
        mae = _finite(raw.get("MAE"))
        if ret is None or mfe is None or mae is None:
            continue
        outcome_by_id[cid] = {
            "candidate_id": cid,
            "future_return": ret,
            "MFE": mfe,
            "MAE": mae,
        }

    scenarios = []
    for rr_floor in RR_SCENARIOS:
        selected_ids = [
            cid for cid, row in records.items()
            if _scenario_selected(row, rr_floor)
        ]
        observed = [
            outcome_by_id[cid]
            for cid in selected_ids
            if cid in outcome_by_id
        ]
        metrics = _outcome_metrics(observed)
        scenarios.append({
            "rr_floor": rr_floor,
            "selected": len(selected_ids),
            "selected_rate": _rate(len(selected_ids), len(records)),
            "observed_60m": metrics["n"],
            "positive": metrics["positive"],
            "positive_rate": metrics["positive_rate"],
            "avg_return": metrics["avg_return"],
            "median_return": metrics["median_return"],
            "avg_mfe": metrics["avg_mfe"],
            "avg_mae": metrics["avg_mae"],
            "statistical_claims_allowed": metrics["n"] >= MIN_OUTCOMES_FOR_CLAIMS,
        })

    current = scenarios[0] if scenarios else None
    status = (
        "AWAITING_COUNTERFACTUAL_SAMPLE"
        if not records else
        "OUTCOMES_PENDING"
        if max((s["observed_60m"] for s in scenarios), default=0) < MIN_OUTCOMES_FOR_CLAIMS
        else "SENSITIVITY_EVIDENCE_AVAILABLE_RESEARCH_ONLY"
    )
    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": status,
        "evaluated": len(records),
        "rr_scenarios": list(RR_SCENARIOS),
        "current_policy_rr_floor": current["rr_floor"] if current else RR_SCENARIOS[0],
        "scenarios": scenarios,
        "interpretation_guard": (
            "SHADOW_SELECTION_SENSITIVITY_IS_NOT_EVIDENCE_TO_CHANGE_LIVE_THRESHOLD"
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


def _fmt(v, digits=4):
    return "NA" if v is None else f"{float(v):.{digits}f}"


def format_summary(report):
    parts = []
    for s in report["scenarios"]:
        parts.append(
            f"rr={s['rr_floor']:.2f}:selected={s['selected']}"
            f":obs60={s['observed_60m']}"
            f":avg_ret={_fmt(s['avg_return'],6)}"
            f":median_ret={_fmt(s['median_return'],6)}"
            f":pos_rate={_fmt(s['positive_rate'],4)}"
            f":avg_mfe={_fmt(s['avg_mfe'],6)}"
            f":avg_mae={_fmt(s['avg_mae'],6)}"
        )
    return (
        "[MIN_ORDER_COUNTERFACTUAL_THRESHOLD_SENSITIVITY_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"evaluated={report['evaluated']} "
        + "|".join(parts)
        + " production_thresholds_unchanged=true "
        "risk_epoch_traversal_credit=false automatic_promotion=false "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY", "COHORT", "FLAG", "MIN_OUTCOMES_FOR_CLAIMS", "RR_SCENARIOS",
    "build_report", "enabled", "format_summary", "snapshot",
]
