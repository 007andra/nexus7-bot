"""Validation layer for MIN_ORDER Counterfactual NEXUS V1.

Research-only analytics over the separate MIN_ORDER-blocked counterfactual NEXUS
cohort. It compares NEXUS-approved and NEXUS-rejected candidates against the
direction-adjusted 60-minute shadow outcome already persisted by
hard_gate_shadow_scan.

This module never changes canonical pipeline traversal, candidate authority,
risk, sizing, drawdown, dispatch, or LIVE eligibility.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
import statistics

FLAG = "MIN_ORDER_COUNTERFACTUAL_VALIDATION_V1"
POPULATION = "HARD_GATE_SHADOW"
COHORT = "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS"
TARGET_EVALUATIONS = 20
TARGET_OBSERVED_60M = 20

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
}


def enabled() -> bool:
    return os.environ.get(FLAG, "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


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
    return statistics.fmean(xs) if xs else None


def _median(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return statistics.median(xs) if xs else None


def _safe_rate(num, den):
    return (float(num) / float(den)) if den else None


def _metrics(rows):
    returns = [r["future_return"] for r in rows]
    positive = [r for r in rows if r["future_return"] > 0]
    non_positive = [r for r in rows if r["future_return"] <= 0]
    return {
        "n": len(rows),
        "positive": len(positive),
        "non_positive": len(non_positive),
        "positive_rate": _safe_rate(len(positive), len(rows)),
        "avg_return": _mean(returns),
        "median_return": _median(returns),
        "avg_mfe": _mean(r["MFE"] for r in rows),
        "avg_mae": _mean(r["MAE"] for r in rows),
    }


def build_report(payloads, outcomes60=(), *, epoch_id="UNKNOWN", started_epoch=None):
    observations = {}
    for raw in payloads:
        obs = raw.get("counterfactual_nexus_v1")
        if not isinstance(obs, dict):
            continue
        if obs.get("cohort") != COHORT:
            continue
        if obs.get("risk_epoch_traversal_credit") is not False:
            continue
        cid = str(obs.get("candidate_id") or "")
        if not cid:
            continue
        observations[cid] = dict(obs)

    outcome_by_id = {}
    for raw in outcomes60:
        cid = str(raw.get("candidate_id") or "")
        if not cid or raw.get("outcome") != "OBSERVED":
            continue
        future_return = _finite(raw.get("future_return"))
        mfe = _finite(raw.get("MFE"))
        mae = _finite(raw.get("MAE"))
        if future_return is None or mfe is None or mae is None:
            continue
        outcome_by_id[cid] = {
            "candidate_id": cid,
            "future_return": future_return,
            "MFE": mfe,
            "MAE": mae,
        }

    matched = []
    for cid, obs in observations.items():
        out = outcome_by_id.get(cid)
        if out is None:
            continue
        matched.append({
            **out,
            "execution_allowed": obs.get("execution_allowed") is True,
            "symbol": str(obs.get("symbol") or "UNKNOWN"),
            "setup": str(obs.get("setup") or "UNKNOWN"),
            "regime": str(obs.get("regime") or "UNKNOWN"),
            "risk_reward": _finite(obs.get("risk_reward")),
            "expected_value": _finite(obs.get("expected_value")),
            "required_equity_at_min_qty": _finite(
                obs.get("required_equity_at_min_qty")
            ),
        })

    allowed = [r for r in matched if r["execution_allowed"]]
    rejected = [r for r in matched if not r["execution_allowed"]]
    allowed_metrics = _metrics(allowed)
    rejected_metrics = _metrics(rejected)
    all_metrics = _metrics(matched)

    tp = sum(1 for r in allowed if r["future_return"] > 0)
    fp = sum(1 for r in allowed if r["future_return"] <= 0)
    fn = sum(1 for r in rejected if r["future_return"] > 0)
    tn = sum(1 for r in rejected if r["future_return"] <= 0)

    mean_lift = None
    if (
        allowed_metrics["avg_return"] is not None
        and rejected_metrics["avg_return"] is not None
    ):
        mean_lift = (
            allowed_metrics["avg_return"] - rejected_metrics["avg_return"]
        )

    positive_rate_lift = None
    if (
        allowed_metrics["positive_rate"] is not None
        and rejected_metrics["positive_rate"] is not None
    ):
        positive_rate_lift = (
            allowed_metrics["positive_rate"] - rejected_metrics["positive_rate"]
        )

    groups = defaultdict(list)
    for row in matched:
        groups[(row["symbol"], row["setup"])].append(row)
    group_rows = []
    for (symbol, setup), rows in groups.items():
        allowed_rows = [r for r in rows if r["execution_allowed"]]
        rejected_rows = [r for r in rows if not r["execution_allowed"]]
        group_rows.append({
            "symbol": symbol,
            "setup": setup,
            "observed": len(rows),
            "allowed_observed": len(allowed_rows),
            "rejected_observed": len(rejected_rows),
            "allowed_avg_return": _mean(r["future_return"] for r in allowed_rows),
            "rejected_avg_return": _mean(r["future_return"] for r in rejected_rows),
            "all_avg_return": _mean(r["future_return"] for r in rows),
        })
    group_rows.sort(
        key=lambda r: (
            -r["observed"],
            r["symbol"],
            r["setup"],
        )
    )

    evaluated = len(observations)
    observed = len(matched)
    if evaluated < TARGET_EVALUATIONS:
        status = "COLLECTING_EVALUATIONS"
    elif observed < TARGET_OBSERVED_60M:
        status = "OUTCOMES_PENDING"
    elif not allowed or not rejected:
        status = "EVIDENCE_SAMPLE_COMPLETE_GROUP_IMBALANCE"
    else:
        status = "EVIDENCE_SAMPLE_COMPLETE_RESEARCH_ONLY"

    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": status,
        "target_evaluations": TARGET_EVALUATIONS,
        "target_observed_60m": TARGET_OBSERVED_60M,
        "evaluated": evaluated,
        "observed_60m": observed,
        "unobserved_60m": max(0, evaluated - observed),
        "all": all_metrics,
        "allowed": allowed_metrics,
        "rejected": rejected_metrics,
        "confusion": {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "true_negative": tn,
            "precision_on_positive_return": _safe_rate(tp, tp + fp),
            "positive_capture_rate": _safe_rate(tp, tp + fn),
            "missed_positive_rate": _safe_rate(fn, tp + fn),
            "non_positive_rejection_rate": _safe_rate(tn, tn + fp),
        },
        "allowed_vs_rejected_mean_return_lift": mean_lift,
        "allowed_vs_rejected_positive_rate_lift": positive_rate_lift,
        "groups": group_rows,
        "outcome_basis": "DIRECTION_ADJUSTED_HYPOTHETICAL_ENTRY_GROSS_60M",
        "statistical_claims_allowed": observed >= TARGET_OBSERVED_60M,
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

    return build_report(
        payloads,
        outcomes,
        epoch_id=eid,
        started_epoch=started,
    )


def _fmt(value, digits=6):
    return "NA" if value is None else f"{float(value):.{digits}f}"


def format_summary(report):
    all_metrics = report["all"]
    a = report["allowed"]
    r = report["rejected"]
    c = report["confusion"]
    return (
        "[MIN_ORDER_COUNTERFACTUAL_VALIDATION_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"evaluated={report['evaluated']} observed_60m={report['observed_60m']} "
        f"target_evaluations={report['target_evaluations']} "
        f"target_observed_60m={report['target_observed_60m']} "
        f"all_avg_return={_fmt(all_metrics['avg_return'])} "
        f"all_median_return={_fmt(all_metrics['median_return'])} "
        f"all_positive_rate={_fmt(all_metrics['positive_rate'], 4)} "
        f"all_avg_mfe={_fmt(all_metrics['avg_mfe'])} "
        f"all_avg_mae={_fmt(all_metrics['avg_mae'])} "
        f"allowed_observed={a['n']} rejected_observed={r['n']} "
        f"allowed_avg_return={_fmt(a['avg_return'])} "
        f"allowed_median_return={_fmt(a['median_return'])} "
        f"allowed_avg_mfe={_fmt(a['avg_mfe'])} "
        f"allowed_avg_mae={_fmt(a['avg_mae'])} "
        f"rejected_avg_return={_fmt(r['avg_return'])} "
        f"rejected_median_return={_fmt(r['median_return'])} "
        f"rejected_avg_mfe={_fmt(r['avg_mfe'])} "
        f"rejected_avg_mae={_fmt(r['avg_mae'])} "
        f"mean_return_lift={_fmt(report['allowed_vs_rejected_mean_return_lift'])} "
        f"allowed_positive_rate={_fmt(a['positive_rate'], 4)} "
        f"rejected_positive_rate={_fmt(r['positive_rate'], 4)} "
        f"positive_rate_lift={_fmt(report['allowed_vs_rejected_positive_rate_lift'], 4)} "
        f"tp={c['true_positive']} fp={c['false_positive']} "
        f"fn={c['false_negative']} tn={c['true_negative']} "
        f"precision={_fmt(c['precision_on_positive_return'], 4)} "
        f"positive_capture_rate={_fmt(c['positive_capture_rate'], 4)} "
        f"missed_positive_rate={_fmt(c['missed_positive_rate'], 4)} "
        "risk_epoch_traversal_credit=false automatic_promotion=false "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


def format_top_groups(report, limit=8):
    rows = list(report.get("groups") or [])[: max(1, int(limit))]
    parts = [
        f"{row['symbol']}/{row['setup']}:n={row['observed']}"
        f":allowed={row['allowed_observed']}"
        f":rejected={row['rejected_observed']}"
        f":all_avg={_fmt(row['all_avg_return'])}"
        for row in rows
    ]
    return (
        "[MIN_ORDER_COUNTERFACTUAL_VALIDATION_V1_TOP_GROUPS] "
        + ("|".join(parts) if parts else "NONE")
        + " research_only=true risk_epoch_traversal_credit=false "
        "decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY",
    "COHORT",
    "FLAG",
    "TARGET_EVALUATIONS",
    "TARGET_OBSERVED_60M",
    "build_report",
    "enabled",
    "format_summary",
    "format_top_groups",
    "snapshot",
]
