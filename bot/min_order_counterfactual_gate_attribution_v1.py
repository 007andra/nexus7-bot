"""Gate attribution for the MIN_ORDER counterfactual NEXUS cohort.

Research-only diagnostic. It quantifies why counterfactual candidates were
rejected and how far their pre-veto metrics were from the *current* NEXUS
thresholds. It does not model or recommend threshold changes and never grants
pipeline traversal or LIVE authority.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
import statistics

FLAG = "MIN_ORDER_COUNTERFACTUAL_GATE_ATTRIBUTION_V1"
POPULATION = "HARD_GATE_SHADOW"
COHORT = "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS"

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
    "thresholds_unchanged": True,
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


def current_thresholds():
    from bot.config import cfg

    rr_min = float(os.environ.get(
        "NEXUS_MIN_RR_NET",
        str(round(float(cfg.MIN_RR_RATIO) * 0.80, 2)),
    ))
    return {
        "rr_net_min": rr_min,
        "ev_pct_min_exclusive": 0.0,
        "min_score": float(os.environ.get(
            "NEXUS_MIN_SCORE", str(cfg.MIN_ENTRY_SCORE)
        )),
    }


def _normalize(obs):
    from bot import min_order_counterfactual_nexus_v1 as study

    return study._normalize_record(obs)


def _mean(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return statistics.fmean(xs) if xs else None


def _median(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return statistics.median(xs) if xs else None


def _group_metrics(rows):
    return {
        "n": len(rows),
        "avg_rr": _mean(r.get("risk_reward") for r in rows),
        "median_rr": _median(r.get("risk_reward") for r in rows),
        "avg_ev": _mean(r.get("expected_value") for r in rows),
        "median_ev": _median(r.get("expected_value") for r in rows),
        "avg_rr_shortfall": _mean(r.get("rr_shortfall") for r in rows),
        "median_rr_shortfall": _median(r.get("rr_shortfall") for r in rows),
        "positive_ev": sum(
            1 for r in rows
            if _finite(r.get("expected_value")) is not None
            and float(r["expected_value"]) > 0
        ),
    }


def build_report(payloads, outcomes60=(), *, epoch_id="UNKNOWN", started_epoch=None):
    thresholds = current_thresholds()
    rr_min = thresholds["rr_net_min"]

    records = []
    for raw in payloads:
        obs = raw.get("counterfactual_nexus_v1")
        if not isinstance(obs, dict):
            continue
        if obs.get("cohort") != COHORT:
            continue
        if obs.get("risk_epoch_traversal_credit") is not False:
            continue

        row = _normalize(obs)
        rr = _finite(row.get("risk_reward"))
        ev = _finite(row.get("expected_value"))
        category = str(row.get("reason_category") or "OTHER")

        row["rr_gap_to_current_min"] = (
            rr - rr_min if rr is not None else None
        )
        row["rr_shortfall"] = (
            max(0.0, rr_min - rr) if rr is not None else None
        )
        row["ev_positive"] = ev is not None and ev > 0
        row["current_rr_min"] = rr_min
        records.append(row)

    by_id = {
        str(r.get("candidate_id")): r
        for r in records if r.get("candidate_id")
    }
    records = list(by_id.values())

    rejected = [
        r for r in records
        if r.get("execution_allowed") is not True
        and r.get("status") != "ERROR"
    ]
    allowed = [r for r in records if r.get("execution_allowed") is True]

    reason_groups = defaultdict(list)
    for row in rejected:
        reason_groups[str(row.get("reason_category") or "OTHER")].append(row)
    reason_rows = [
        {"reason": reason, **_group_metrics(rows)}
        for reason, rows in sorted(reason_groups.items())
    ]
    reason_rows.sort(key=lambda r: (-r["n"], r["reason"]))

    rr_veto_positive_ev = [
        r for r in rejected
        if r.get("reason_category") == "RR_BELOW_MIN"
        and r.get("ev_positive") is True
    ]
    ev_negative = [
        r for r in rejected
        if r.get("reason_category") == "EV_NEGATIVE"
    ]

    closest_rr = sorted(
        [
            r for r in rejected
            if r.get("reason_category") == "RR_BELOW_MIN"
            and _finite(r.get("rr_shortfall")) is not None
        ],
        key=lambda r: (
            float(r["rr_shortfall"]),
            str(r.get("symbol") or ""),
            str(r.get("candidate_id") or ""),
        ),
    )

    outcome_by_id = {}
    for raw in outcomes60:
        if raw.get("outcome") != "OBSERVED":
            continue
        cid = str(raw.get("candidate_id") or "")
        ret = _finite(raw.get("future_return"))
        mfe = _finite(raw.get("MFE"))
        mae = _finite(raw.get("MAE"))
        if not cid or ret is None or mfe is None or mae is None:
            continue
        outcome_by_id[cid] = {
            "future_return": ret,
            "MFE": mfe,
            "MAE": mae,
        }

    outcome_reason = defaultdict(list)
    for cid, row in by_id.items():
        outcome = outcome_by_id.get(cid)
        if outcome is None:
            continue
        outcome_reason[str(row.get("reason_category") or "OTHER")].append(outcome)

    outcome_reason_rows = []
    for reason, rows in sorted(outcome_reason.items()):
        outcome_reason_rows.append({
            "reason": reason,
            "observed_60m": len(rows),
            "avg_return": _mean(r["future_return"] for r in rows),
            "positive_rate": (
                sum(1 for r in rows if r["future_return"] > 0) / len(rows)
                if rows else None
            ),
            "avg_mfe": _mean(r["MFE"] for r in rows),
            "avg_mae": _mean(r["MAE"] for r in rows),
        })

    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": (
            "AWAITING_COUNTERFACTUAL_SAMPLE"
            if not records else
            "ATTRIBUTION_ACTIVE_RESEARCH_ONLY"
        ),
        "evaluated": len(records),
        "allowed": len(allowed),
        "rejected": len(rejected),
        "thresholds": thresholds,
        "reason_rows": reason_rows,
        "ev_negative_rejected": len(ev_negative),
        "rr_below_min_positive_ev_rejected": len(rr_veto_positive_ev),
        "rr_positive_ev_shortfall": {
            "n": len(rr_veto_positive_ev),
            "mean": _mean(r.get("rr_shortfall") for r in rr_veto_positive_ev),
            "median": _median(r.get("rr_shortfall") for r in rr_veto_positive_ev),
            "min": (
                min(float(r["rr_shortfall"]) for r in rr_veto_positive_ev)
                if rr_veto_positive_ev else None
            ),
            "max": (
                max(float(r["rr_shortfall"]) for r in rr_veto_positive_ev)
                if rr_veto_positive_ev else None
            ),
        },
        "closest_rr_rejects": [
            {
                "candidate_id": r.get("candidate_id"),
                "symbol": r.get("symbol"),
                "setup": r.get("setup"),
                "rr_net": r.get("risk_reward"),
                "ev_pct": r.get("expected_value"),
                "rr_shortfall": r.get("rr_shortfall"),
            }
            for r in closest_rr[:8]
        ],
        "outcome_reason_rows": outcome_reason_rows,
        "observed_60m": sum(len(v) for v in outcome_reason.values()),
        "interpretation_guard": (
            "DISTANCE_TO_THRESHOLD_IS_DIAGNOSTIC_ONLY_NOT_THRESHOLD_CHANGE_EVIDENCE"
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
        payloads, outcomes, epoch_id=eid, started_epoch=started
    )


def _fmt(value, digits=4):
    return "NA" if value is None else f"{float(value):.{digits}f}"


def format_summary(report):
    shortfall = report["rr_positive_ev_shortfall"]
    reasons = ",".join(
        f"{r['reason']}:{r['n']}" for r in report.get("reason_rows", [])
    ) or "NONE"
    return (
        "[MIN_ORDER_COUNTERFACTUAL_GATE_ATTRIBUTION_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"evaluated={report['evaluated']} allowed={report['allowed']} "
        f"rejected={report['rejected']} "
        f"rr_current_min={_fmt(report['thresholds']['rr_net_min'], 3)} "
        f"ev_negative_rejected={report['ev_negative_rejected']} "
        f"rr_below_min_positive_ev_rejected="
        f"{report['rr_below_min_positive_ev_rejected']} "
        f"rr_positive_ev_shortfall_median={_fmt(shortfall['median'], 3)} "
        f"rr_positive_ev_shortfall_min={_fmt(shortfall['min'], 3)} "
        f"reasons={reasons} observed_60m={report['observed_60m']} "
        "diagnostic_only=true thresholds_unchanged=true "
        "risk_epoch_traversal_credit=false automatic_promotion=false "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


def format_closest(report):
    rows = report.get("closest_rr_rejects") or []
    parts = [
        f"{r['symbol']}/{r['setup']}:rr={_fmt(r['rr_net'],3)}"
        f":ev={_fmt(r['ev_pct'],4)}"
        f":shortfall={_fmt(r['rr_shortfall'],3)}"
        for r in rows
    ]
    return (
        "[MIN_ORDER_COUNTERFACTUAL_GATE_ATTRIBUTION_V1_CLOSEST_RR] "
        + ("|".join(parts) if parts else "NONE")
        + " threshold_change_mode=false decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY",
    "COHORT",
    "FLAG",
    "build_report",
    "current_thresholds",
    "enabled",
    "format_closest",
    "format_summary",
    "snapshot",
]
