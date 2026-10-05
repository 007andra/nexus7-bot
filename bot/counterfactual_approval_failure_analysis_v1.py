"""Approval Failure Analysis V1 for the MIN_ORDER counterfactual cohort.

Research-only diagnostics over already-persisted HARD_GATE_SHADOW candidates and
60m/240m outcomes. The report asks why the counterfactual NEXUS-approved subset
has underperformed the rejected subset. It measures association and
concentration only; it does not claim causality, tune thresholds, or authorize
execution.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
import statistics
import time

FLAG = "COUNTERFACTUAL_APPROVAL_FAILURE_ANALYSIS_V1"
POPULATION = "HARD_GATE_SHADOW"
COHORT = "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS"
MIN_GROUP_N = 3

AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "association_not_causation": True,
    "thresholds_unchanged": True,
    "risk_epoch_traversal_credit": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}


def enabled() -> bool:
    return os.environ.get(FLAG, "true").strip().lower() in {
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


def _rate(num, den):
    return float(num) / float(den) if den else None


def _metric(rows, key):
    return _mean(row.get(key) for row in rows)


def _performance(rows):
    if not rows:
        return {
            "n": 0,
            "avg_return": None,
            "median_return": None,
            "positive_rate": None,
            "avg_mfe": None,
            "avg_mae": None,
        }
    return {
        "n": len(rows),
        "avg_return": _mean(r.get("future_return") for r in rows),
        "median_return": _median(r.get("future_return") for r in rows),
        "positive_rate": _rate(
            sum(1 for r in rows if float(r["future_return"]) > 0.0), len(rows)
        ),
        "avg_mfe": _mean(r.get("MFE") for r in rows),
        "avg_mae": _mean(r.get("MAE") for r in rows),
    }


def _cost_fraction(raw):
    snap = raw.get("cost_snapshot")
    if not isinstance(snap, dict):
        return None
    fee = _finite(snap.get("taker_fee"))
    entry_slip = _finite(snap.get("entry_slippage"))
    exit_slip = _finite(snap.get("exit_slippage"))
    if None in (fee, entry_slip, exit_slip):
        return None
    return 2.0 * fee + entry_slip + exit_slip


def _stop_width(raw):
    entry = _finite(raw.get("entry"))
    stop = _finite(raw.get("stop"))
    if entry is None or stop is None or entry <= 0:
        return None
    return abs(entry - stop) / entry


def _time_bucket(raw):
    captured = _finite(raw.get("captured_epoch"))
    if captured is None:
        return "UNKNOWN"
    hour = int(time.gmtime(captured).tm_hour)
    start = (hour // 4) * 4
    return f"{start:02d}-{(start + 4) % 24:02d}UTC"


def _records(payloads):
    from bot import min_order_counterfactual_nexus_v1 as study

    out = {}
    for raw in payloads:
        obs = raw.get("counterfactual_nexus_v1")
        if not isinstance(obs, dict):
            continue
        if obs.get("cohort") != COHORT:
            continue
        if obs.get("risk_epoch_traversal_credit") is not False:
            continue
        row = study._normalize_record(obs)
        cid = str(row.get("candidate_id") or "")
        if not cid:
            continue
        score = row.get("score_snapshot")
        score = score if isinstance(score, dict) else {}
        out[cid] = {
            "candidate_id": cid,
            "allowed": row.get("execution_allowed") is True,
            "status": row.get("status"),
            "reason_category": str(row.get("reason_category") or "OTHER"),
            "symbol": str(row.get("symbol") or raw.get("symbol") or "UNKNOWN"),
            "side": str(row.get("side") or raw.get("side") or "UNKNOWN").upper(),
            "setup": str(row.get("setup") or raw.get("setup") or "UNKNOWN"),
            "regime": str(row.get("regime") or raw.get("regime") or "UNKNOWN"),
            "time_bucket": _time_bucket(raw),
            "score": _finite(raw.get("score")),
            "confidence": _finite(
                score.get("fusion_confidence")
                if score.get("fusion_confidence") is not None
                else row.get("confidence")
            ),
            "risk_reward": _finite(row.get("risk_reward")),
            "expected_value": _finite(row.get("expected_value")),
            "setup_quality": _finite(row.get("setup_quality")),
            "required_equity": _finite(row.get("required_equity_at_min_qty")),
            "stop_width_pct": _stop_width(raw),
            "cost_fraction": _cost_fraction(raw),
            "spread_bps": _finite(
                raw.get("cost_snapshot", {}).get("spread_bps")
                if isinstance(raw.get("cost_snapshot"), dict) else None
            ),
        }
    return out


def _outcome_map(outcomes):
    out = {}
    for raw in outcomes:
        cid = str(raw.get("candidate_id") or "")
        if not cid or raw.get("outcome") != "OBSERVED":
            continue
        ret = _finite(raw.get("future_return"))
        mfe = _finite(raw.get("MFE"))
        mae = _finite(raw.get("MAE"))
        if ret is None or mfe is None or mae is None:
            continue
        out[cid] = {
            "future_return": ret,
            "MFE": mfe,
            "MAE": mae,
        }
    return out


def _matched(records, outcomes):
    out = []
    for cid, row in records.items():
        outcome = outcomes.get(cid)
        if outcome is None or row.get("status") == "ERROR":
            continue
        out.append({**row, **outcome})
    return out


def _feature_comparison(rows):
    features = (
        "score", "confidence", "risk_reward", "expected_value", "setup_quality",
        "required_equity", "stop_width_pct", "cost_fraction", "spread_bps",
    )
    allowed = [r for r in rows if r["allowed"]]
    rejected = [r for r in rows if not r["allowed"]]
    result = []
    for feature in features:
        a = _metric(allowed, feature)
        r = _metric(rejected, feature)
        result.append({
            "feature": feature,
            "allowed_mean": a,
            "rejected_mean": r,
            "difference": a - r if a is not None and r is not None else None,
            "allowed_n": sum(_finite(x.get(feature)) is not None for x in allowed),
            "rejected_n": sum(_finite(x.get(feature)) is not None for x in rejected),
        })
    return result


def _dimension_groups(rows, dimension):
    groups = defaultdict(list)
    for row in rows:
        groups[str(row.get(dimension) or "UNKNOWN")].append(row)
    result = []
    for value, grp in groups.items():
        allowed = [r for r in grp if r["allowed"]]
        rejected = [r for r in grp if not r["allowed"]]
        result.append({
            "dimension": dimension,
            "value": value,
            "n": len(grp),
            "allowed_n": len(allowed),
            "rejected_n": len(rejected),
            "approval_rate": _rate(len(allowed), len(grp)),
            "allowed_avg_return": _mean(r["future_return"] for r in allowed),
            "rejected_avg_return": _mean(r["future_return"] for r in rejected),
            "all_avg_return": _mean(r["future_return"] for r in grp),
            "claims_guarded": len(grp) < MIN_GROUP_N,
        })
    result.sort(key=lambda x: (-x["allowed_n"], -x["n"], x["value"]))
    return result


def _concentration(records):
    allowed = [r for r in records.values() if r["allowed"] and r.get("status") != "ERROR"]
    total = len(allowed)
    result = []
    for dim in ("symbol", "setup", "side", "regime", "time_bucket"):
        counts = defaultdict(int)
        for row in allowed:
            counts[str(row.get(dim) or "UNKNOWN")] += 1
        top_value, top_n = ("NONE", 0)
        if counts:
            top_value, top_n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        result.append({
            "dimension": dim,
            "top_value": top_value,
            "top_n": top_n,
            "allowed_total": total,
            "top_share": _rate(top_n, total),
            "distinct": len(counts),
        })
    return result


def build_report(payloads, outcomes60=(), outcomes240=(), *, epoch_id="UNKNOWN", started_epoch=None):
    records = _records(payloads)
    out60 = _outcome_map(outcomes60)
    out240 = _outcome_map(outcomes240)
    m60 = _matched(records, out60)
    m240 = _matched(records, out240)

    a60 = [r for r in m60 if r["allowed"]]
    r60 = [r for r in m60 if not r["allowed"]]
    a240 = [r for r in m240 if r["allowed"]]
    r240 = [r for r in m240 if not r["allowed"]]

    p60a, p60r = _performance(a60), _performance(r60)
    p240a, p240r = _performance(a240), _performance(r240)

    negative60 = (
        p60a["n"] >= 5 and p60r["n"] >= 5
        and p60a["avg_return"] is not None and p60r["avg_return"] is not None
        and p60a["avg_return"] < p60r["avg_return"]
    )
    negative240 = (
        p240a["n"] >= 5 and p240r["n"] >= 5
        and p240a["avg_return"] is not None and p240r["avg_return"] is not None
        and p240a["avg_return"] < p240r["avg_return"]
    )
    if negative60 and negative240:
        status = "NEGATIVE_SELECTION_CONFIRMED_BOTH_HORIZONS"
    elif negative60 or negative240:
        status = "NEGATIVE_SELECTION_SIGNAL_ONE_HORIZON"
    elif not m60:
        status = "AWAITING_MATCHED_OUTCOMES"
    else:
        status = "NO_NEGATIVE_SELECTION_CONFIRMED"

    diagnostics = []
    if negative60 and negative240:
        diagnostics.append("APPROVED_UNDERPERFORMS_REJECTED_AT_60M_AND_240M")
    if (
        p60a["positive_rate"] is not None and p60r["positive_rate"] is not None
        and p60a["positive_rate"] < p60r["positive_rate"]
    ):
        diagnostics.append("APPROVED_POSITIVE_RATE_BELOW_REJECTED_60M")
    if (
        p60a["avg_mae"] is not None and p60r["avg_mae"] is not None
        and p60a["avg_mae"] < p60r["avg_mae"]
    ):
        diagnostics.append("APPROVED_MAE_WORSE_THAN_REJECTED_60M")
    for row in _concentration(records):
        if (
            row["allowed_total"] >= 5
            and row["top_share"] is not None
            and row["top_share"] >= 0.60
        ):
            diagnostics.append(
                f"APPROVAL_CONCENTRATION_{row['dimension'].upper()}_{row['top_value']}"
            )

    dimensions60 = {
        dim: _dimension_groups(m60, dim)
        for dim in ("symbol", "setup", "side", "regime", "time_bucket", "reason_category")
    }
    dimensions240 = {
        dim: _dimension_groups(m240, dim)
        for dim in ("symbol", "setup", "side", "regime", "time_bucket")
    }

    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": status,
        "evaluated": len(records),
        "allowed": sum(1 for r in records.values() if r["allowed"]),
        "rejected": sum(
            1 for r in records.values()
            if not r["allowed"] and r.get("status") != "ERROR"
        ),
        "observed_60m": len(m60),
        "observed_240m": len(m240),
        "allowed_60m": p60a,
        "rejected_60m": p60r,
        "allowed_240m": p240a,
        "rejected_240m": p240r,
        "allowed_vs_rejected_60m_mean_lift": (
            p60a["avg_return"] - p60r["avg_return"]
            if p60a["avg_return"] is not None and p60r["avg_return"] is not None
            else None
        ),
        "allowed_vs_rejected_240m_mean_lift": (
            p240a["avg_return"] - p240r["avg_return"]
            if p240a["avg_return"] is not None and p240r["avg_return"] is not None
            else None
        ),
        "diagnostics": tuple(diagnostics),
        "feature_comparison_60m": _feature_comparison(m60),
        "approval_concentration": _concentration(records),
        "dimensions_60m": dimensions60,
        "dimensions_240m": dimensions240,
        "minimum_group_n_for_interpretation": MIN_GROUP_N,
        "interpretation_guard": (
            "DIAGNOSTIC_ASSOCIATIONS_ONLY_REQUIRE_NEW_PROSPECTIVE_OOS_COHORT"
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
        "SELECT o.candidate_id,o.horizon,o.payload "
        "FROM hard_gate_shadow_outcomes_v1 o "
        "JOIN hard_gate_shadow_candidates_v1 c ON c.candidate_id=o.candidate_id "
        "WHERE c.population=? AND c.captured_epoch>=? AND o.horizon IN (?,?)",
        (POPULATION, started, 60, 240),
    )
    out60, out240 = [], []
    for item in outcome_rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[2]
        cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
        horizon = int(item["horizon"] if hasattr(item, "keys") else item[1])
        try:
            obj = json.loads(raw)
            obj["candidate_id"] = cid
            (out60 if horizon == 60 else out240).append(obj)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return build_report(
        payloads, out60, out240, epoch_id=eid, started_epoch=started
    )


def _fmt(v, digits=6):
    return "NA" if v is None else f"{float(v):.{digits}f}"


def format_summary(report):
    a60, r60 = report["allowed_60m"], report["rejected_60m"]
    a240, r240 = report["allowed_240m"], report["rejected_240m"]
    return (
        "[COUNTERFACTUAL_APPROVAL_FAILURE_ANALYSIS_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"evaluated={report['evaluated']} allowed={report['allowed']} "
        f"rejected={report['rejected']} observed_60m={report['observed_60m']} "
        f"observed_240m={report['observed_240m']} "
        f"allowed60_n={a60['n']} allowed60_avg={_fmt(a60['avg_return'])} "
        f"rejected60_n={r60['n']} rejected60_avg={_fmt(r60['avg_return'])} "
        f"lift60={_fmt(report['allowed_vs_rejected_60m_mean_lift'])} "
        f"allowed240_n={a240['n']} allowed240_avg={_fmt(a240['avg_return'])} "
        f"rejected240_n={r240['n']} rejected240_avg={_fmt(r240['avg_return'])} "
        f"lift240={_fmt(report['allowed_vs_rejected_240m_mean_lift'])} "
        f"diagnostics={','.join(report['diagnostics']) or 'NONE'} "
        "association_not_causation=true thresholds_unchanged=true "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


def format_feature_comparison(report):
    parts = []
    for row in report["feature_comparison_60m"]:
        parts.append(
            f"{row['feature']}:a={_fmt(row['allowed_mean'])}"
            f":r={_fmt(row['rejected_mean'])}:d={_fmt(row['difference'])}"
            f":an={row['allowed_n']}:rn={row['rejected_n']}"
        )
    return (
        "[COUNTERFACTUAL_APPROVAL_FAILURE_FEATURES_V1] "
        + "|".join(parts)
        + " association_not_causation=true execution_effect=NONE"
    )


def format_concentration(report):
    parts = [
        f"{r['dimension']}={r['top_value']}:n={r['top_n']}/{r['allowed_total']}"
        f":share={_fmt(r['top_share'],4)}:distinct={r['distinct']}"
        for r in report["approval_concentration"]
    ]
    return (
        "[COUNTERFACTUAL_APPROVAL_FAILURE_CONCENTRATION_V1] "
        + "|".join(parts)
        + " association_not_causation=true execution_effect=NONE"
    )


def format_worst_groups(report, *, horizon=60, limit=10):
    dims = report["dimensions_60m"] if horizon == 60 else report["dimensions_240m"]
    rows = []
    for dimension, groups in dims.items():
        for row in groups:
            if row["allowed_n"] < MIN_GROUP_N or row["allowed_avg_return"] is None:
                continue
            rows.append(row)
    rows.sort(
        key=lambda r: (
            float(r["allowed_avg_return"]),
            -r["allowed_n"],
            r["dimension"],
            r["value"],
        )
    )
    parts = [
        f"{r['dimension']}={r['value']}:allowed_n={r['allowed_n']}"
        f":allowed_avg={_fmt(r['allowed_avg_return'])}"
        f":rejected_avg={_fmt(r['rejected_avg_return'])}"
        for r in rows[:limit]
    ]
    return (
        f"[COUNTERFACTUAL_APPROVAL_FAILURE_WORST_GROUPS_{horizon}M_V1] "
        + ("|".join(parts) if parts else "NONE")
        + " small_n_guard=true association_not_causation=true execution_effect=NONE"
    )
