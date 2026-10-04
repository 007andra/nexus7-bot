"""Counterfactual NEXUS study for candidates blocked only by MIN_ORDER.

Research-only. This study asks whether an otherwise production-equivalent
candidate would have passed NEXUS if minimum-order capital feasibility had not
blocked it. It never changes canonical nexus_called/nexus_allowed fields and
never contributes to Risk Epoch traversal or LIVE authority.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
import statistics

FLAG = "MIN_ORDER_COUNTERFACTUAL_NEXUS_V1"
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
}


def enabled() -> bool:
    return os.environ.get(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _reason_category(reason):
    upper = str(reason or "").upper()
    if "EV NEGATIVO" in upper or "EXPECTED VALUE" in upper:
        return "EV_NEGATIVE"
    if "R:R" in upper or "RR " in upper:
        return "RR_BELOW_MIN"
    if "SCORE" in upper or "THRESHOLD" in upper:
        return "SCORE"
    if "DADO" in upper or "DATA" in upper or "QUALIDADE" in upper:
        return "DATA"
    if "REGIME" in upper or "TENDÊNCIA" in upper or "TENDENCIA" in upper:
        return "REGIME"
    return "OTHER"


def _normalize_record(obs):
    row = dict(obs)
    score = row.get("score_snapshot")
    score = score if isinstance(score, dict) else {}
    effective = {
        "confidence": _finite(score.get("fusion_confidence")),
        "risk_reward": _finite(score.get("rr_net")),
        "expected_value": _finite(score.get("ev_pct")),
    }
    for key, value in effective.items():
        if value is not None:
            row[key] = value
    row["reason_category"] = str(
        row.get("reason_category") or _reason_category(row.get("reason"))
    )
    row["metric_source"] = (
        "SCORE_SNAPSHOT_PRE_VETO"
        if any(value is not None for value in effective.values())
        else str(row.get("metric_source") or "DECISION_FIELDS")
    )
    return row


def build_observation(sig, decision, minimum_order, *, captured_epoch):
    reasoning = list(getattr(decision, "reasoning", None) or [])
    score = getattr(decision, "_bgx_score_snapshot", None)
    score = score if isinstance(score, dict) else {}
    reason = str(reasoning[-1] if reasoning else "NONE")[:240]
    row = {
        **AUTHORITY,
        "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
        "candidate_id": str(sig.candidate_id),
        "captured_epoch": float(captured_epoch),
        "symbol": str(sig.symbol),
        "side": str(sig.direction).upper(),
        "setup": str(sig.entry_type),
        "regime": str(getattr(sig, "regime", "UNKNOWN")),
        "execution_allowed": getattr(decision, "execution_allowed", False) is True,
        "decision": str(getattr(decision, "decision", "UNKNOWN")),
        "confidence": _finite(getattr(decision, "confidence", None)),
        "setup_quality": _finite(getattr(decision, "setup_quality", None)),
        "risk_reward": _finite(getattr(decision, "risk_reward", None)),
        "expected_value": _finite(getattr(decision, "expected_value", None)),
        "reason": reason,
        "reason_category": _reason_category(reason),
        "score_snapshot": score or None,
        "required_equity_at_min_qty": _finite(
            minimum_order.get("required_equity_at_min_qty")
        ),
        "counterfactual_risk_pct": _finite(
            minimum_order.get("counterfactual_risk_pct")
        ),
        "canonical_nexus_called": False,
        "canonical_nexus_allowed": False,
        "risk_epoch_traversal_credit": False,
    }
    return _normalize_record(row)


def error_observation(sig, minimum_order, *, captured_epoch, error):
    return {
        **AUTHORITY,
        "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
        "candidate_id": str(getattr(sig, "candidate_id", "UNKNOWN")),
        "captured_epoch": float(captured_epoch),
        "symbol": str(getattr(sig, "symbol", "UNKNOWN")),
        "side": str(getattr(sig, "direction", "UNKNOWN")).upper(),
        "setup": str(getattr(sig, "entry_type", "UNKNOWN")),
        "regime": str(getattr(sig, "regime", "UNKNOWN")),
        "status": "ERROR",
        "error": str(error),
        "execution_allowed": False,
        "required_equity_at_min_qty": _finite(
            minimum_order.get("required_equity_at_min_qty")
        ),
        "canonical_nexus_called": False,
        "canonical_nexus_allowed": False,
        "risk_epoch_traversal_credit": False,
    }


def _mean(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return statistics.fmean(xs) if xs else None


def build_report(payloads, outcomes60=(), *, epoch_id="UNKNOWN", started_epoch=None):
    records = []
    for raw in payloads:
        obs = raw.get("counterfactual_nexus_v1")
        if not isinstance(obs, dict):
            continue
        if obs.get("cohort") != "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS":
            continue
        if obs.get("risk_epoch_traversal_credit") is not False:
            continue
        records.append(_normalize_record(obs))

    by_id = {str(r.get("candidate_id")): r for r in records if r.get("candidate_id")}
    records = list(by_id.values())
    allowed = [r for r in records if r.get("execution_allowed") is True]
    errors = [r for r in records if r.get("status") == "ERROR"]

    outcome_by_id = {
        str(o.get("candidate_id")): o
        for o in outcomes60
        if o.get("outcome") == "OBSERVED" and o.get("candidate_id")
    }
    observed = [outcome_by_id[cid] for cid in by_id if cid in outcome_by_id]
    allowed_ids = {
        str(r.get("candidate_id")) for r in allowed if r.get("candidate_id")
    }
    approved_observed = [
        outcome_by_id[cid] for cid in allowed_ids if cid in outcome_by_id
    ]

    rejection_reasons = defaultdict(int)
    for row in records:
        if row.get("execution_allowed") is not True and row.get("status") != "ERROR":
            rejection_reasons[str(row.get("reason_category") or "OTHER")] += 1

    groups = defaultdict(lambda: {"evaluated": 0, "allowed": 0})
    for row in records:
        key = (str(row.get("symbol") or "UNKNOWN"), str(row.get("setup") or "UNKNOWN"))
        groups[key]["evaluated"] += 1
        groups[key]["allowed"] += int(row.get("execution_allowed") is True)
    group_rows = [
        {
            "symbol": symbol,
            "setup": setup,
            **counts,
            "approval_rate": counts["allowed"] / counts["evaluated"],
        }
        for (symbol, setup), counts in groups.items()
    ]
    group_rows.sort(
        key=lambda r: (-r["allowed"], -r["approval_rate"], -r["evaluated"],
                       r["symbol"], r["setup"])
    )

    n = len(records)
    n60 = len(observed)
    if n < TARGET_EVALUATIONS:
        status = "COLLECTING_EVALUATIONS"
    elif n60 < TARGET_OUTCOMES_60M:
        status = "EVALUATION_SAMPLE_COMPLETE_OUTCOMES_PENDING"
    else:
        status = "EVIDENCE_SAMPLE_COMPLETE_RESEARCH_ONLY"

    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": status,
        "target_evaluations": TARGET_EVALUATIONS,
        "target_outcomes_60m": TARGET_OUTCOMES_60M,
        "evaluated": n,
        "allowed": len(allowed),
        "rejected": n - len(allowed) - len(errors),
        "errors": len(errors),
        "approval_rate": len(allowed) / n if n else None,
        "avg_risk_reward": _mean(r.get("risk_reward") for r in records),
        "avg_expected_value": _mean(r.get("expected_value") for r in records),
        "avg_required_equity": _mean(
            r.get("required_equity_at_min_qty") for r in records
        ),
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "observed_60m": n60,
        "approved_observed_60m": len(approved_observed),
        "approved_60m_avg_gross_return": _mean(
            r.get("future_return") for r in approved_observed
        ),
        "approved_60m_avg_mfe": _mean(r.get("MFE") for r in approved_observed),
        "approved_60m_avg_mae": _mean(r.get("MAE") for r in approved_observed),
        "groups": group_rows,
        "risk_epoch_traversal_credit": False,
        "automatic_promotion": False,
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

    outcome_rows = await db._fetchall(
        "SELECT o.candidate_id,o.payload FROM hard_gate_shadow_outcomes_v1 o "
        "JOIN hard_gate_shadow_candidates_v1 c ON c.candidate_id=o.candidate_id "
        "WHERE c.population=? AND c.captured_epoch>=? AND o.horizon=?",
        (POPULATION, started, 60),
    )
    outcomes = []
    for item in outcome_rows or []:
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
    return (
        "[MIN_ORDER_COUNTERFACTUAL_NEXUS_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"evaluated={report['evaluated']} allowed={report['allowed']} "
        f"rejected={report['rejected']} errors={report['errors']} "
        f"approval_rate={_fmt(report['approval_rate'], 4)} "
        f"avg_rr={_fmt(report['avg_risk_reward'], 4)} "
        f"avg_ev={_fmt(report['avg_expected_value'], 4)} "
        f"avg_required_equity={_fmt(report['avg_required_equity'], 4)} "
        f"rejection_reasons={','.join(f'{k}:{v}' for k, v in report.get('rejection_reasons', {}).items()) or 'NONE'} "
        f"observed_60m={report['observed_60m']} "
        f"approved_observed_60m={report['approved_observed_60m']} "
        f"approved_60m_avg_return={_fmt(report['approved_60m_avg_gross_return'], 6)} "
        "risk_epoch_traversal_credit=false automatic_promotion=false "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


def format_top_groups(report, limit=8):
    rows = list(report.get("groups") or [])[: max(1, int(limit))]
    parts = [
        f"{r['symbol']}/{r['setup']}:n={r['evaluated']}:allowed={r['allowed']}"
        f":rate={_fmt(r['approval_rate'], 3)}"
        for r in rows
    ]
    return (
        "[MIN_ORDER_COUNTERFACTUAL_NEXUS_V1_TOP_GROUPS] "
        + ("|".join(parts) if parts else "NONE")
        + " risk_epoch_traversal_credit=false decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY", "FLAG", "TARGET_EVALUATIONS", "TARGET_OUTCOMES_60M",
    "build_observation", "build_report", "enabled", "error_observation",
    "format_summary", "format_top_groups", "snapshot",
]
