"""Capital Ladder V1 for MIN_ORDER-blocked HARD_GATE_SHADOW candidates.

Research-only counterfactual sizing analysis. Fixed hypothetical equity levels
are applied only to the already-computed required_equity_at_min_qty field. The
ladder never changes account capital, drawdown accounting, HWM, risk, leverage,
sizing, thresholds, recovery, override, dispatch, or LIVE authority.

External funding is explicitly NOT treated as clearing lifetime drawdown.
"""
from __future__ import annotations

import json
import math
import os
import statistics

FLAG = "MIN_ORDER_CAPITAL_LADDER_V1"
POPULATION = "HARD_GATE_SHADOW"
COHORT = "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS"
EQUITY_LEVELS = (10.0, 14.0, 19.0, 25.0, 35.0, 50.0)

AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "capital_metric_is_counterfactual_not_recommendation": True,
    "external_capital_does_not_clear_drawdown": True,
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
        "avg_return": _mean(r["future_return"] for r in rows),
        "median_return": _median(r["future_return"] for r in rows),
        "positive_rate": _rate(
            sum(1 for r in rows if r["future_return"] > 0.0), len(rows)
        ),
        "avg_mfe": _mean(r["MFE"] for r in rows),
        "avg_mae": _mean(r["MAE"] for r in rows),
    }


def _pipeline_min_order_rows(payloads):
    rows = {}
    for raw in payloads:
        pullback = raw.get("pullback_pass") is True
        funnel = raw.get("production_equivalent_funnel_result") is True
        capital_confirmed = raw.get("capital_source") not in (None, "UNCONFIRMED")
        feasible = raw.get("shadow_min_order_feasible")
        required = _finite(raw.get("required_equity_at_min_qty"))
        cid = str(raw.get("candidate_id") or "")
        if (
            cid
            and pullback
            and funnel
            and capital_confirmed
            and feasible is False
            and required is not None
        ):
            cf = raw.get("counterfactual_nexus_v1")
            cf = cf if isinstance(cf, dict) and cf.get("cohort") == COHORT else None
            rows[cid] = {
                "candidate_id": cid,
                "symbol": str(raw.get("symbol") or "UNKNOWN"),
                "setup": str(raw.get("setup") or "UNKNOWN"),
                "side": str(raw.get("side") or "UNKNOWN").upper(),
                "regime": str(raw.get("regime") or "UNKNOWN"),
                "required_equity": required,
                "counterfactual_evaluated": cf is not None,
                "counterfactual_allowed": (
                    cf is not None and cf.get("execution_allowed") is True
                ),
                "counterfactual_error": (
                    cf is not None and cf.get("status") == "ERROR"
                ),
            }
    return rows


def _outcome_map(outcomes):
    result = {}
    for raw in outcomes:
        cid = str(raw.get("candidate_id") or "")
        if not cid or raw.get("outcome") != "OBSERVED":
            continue
        ret = _finite(raw.get("future_return"))
        mfe = _finite(raw.get("MFE"))
        mae = _finite(raw.get("MAE"))
        if ret is None or mfe is None or mae is None:
            continue
        result[cid] = {
            "candidate_id": cid,
            "future_return": ret,
            "MFE": mfe,
            "MAE": mae,
        }
    return result


def build_report(
    payloads,
    outcomes60=(),
    outcomes240=(),
    *,
    epoch_id="UNKNOWN",
    started_epoch=None,
    equity_levels=EQUITY_LEVELS,
):
    rows = _pipeline_min_order_rows(payloads)
    out60 = _outcome_map(outcomes60)
    out240 = _outcome_map(outcomes240)

    ladder = []
    for level in tuple(float(x) for x in equity_levels):
        eligible = [
            row for row in rows.values()
            if row["required_equity"] <= level + 1e-12
        ]
        evaluated = [
            row for row in eligible
            if row["counterfactual_evaluated"]
            and not row["counterfactual_error"]
        ]
        allowed = [row for row in evaluated if row["counterfactual_allowed"]]
        allowed_ids = {row["candidate_id"] for row in allowed}

        p60 = _performance([
            out60[cid] for cid in allowed_ids if cid in out60
        ])
        p240 = _performance([
            out240[cid] for cid in allowed_ids if cid in out240
        ])

        ladder.append({
            "equity": level,
            "min_order_feasible_candidates": len(eligible),
            "feasible_rate": _rate(len(eligible), len(rows)),
            "counterfactual_evaluated": len(evaluated),
            "counterfactual_allowed": len(allowed),
            "counterfactual_approval_rate": _rate(len(allowed), len(evaluated)),
            "allowed_60m": p60,
            "allowed_240m": p240,
            "canonical_pipeline_credit": 0,
            "lifetime_drawdown_credit": 0,
            "live_eligible": False,
        })

    reqs = [row["required_equity"] for row in rows.values()]
    closest = min(rows.values(), key=lambda r: r["required_equity"]) if rows else None
    return {
        **AUTHORITY,
        "epoch_id": epoch_id,
        "started_epoch": started_epoch,
        "status": (
            "CAPITAL_LADDER_AVAILABLE_RESEARCH_ONLY"
            if rows else "AWAITING_MIN_ORDER_ONLY_SAMPLE"
        ),
        "min_order_only_candidates": len(rows),
        "counterfactual_evaluated": sum(
            r["counterfactual_evaluated"] for r in rows.values()
        ),
        "counterfactual_allowed": sum(
            r["counterfactual_allowed"] for r in rows.values()
        ),
        "required_equity_min": min(reqs) if reqs else None,
        "required_equity_median": statistics.median(reqs) if reqs else None,
        "required_equity_max": max(reqs) if reqs else None,
        "closest_candidate": closest,
        "equity_levels": list(equity_levels),
        "ladder": ladder,
        "interpretation_guard": (
            "HYPOTHETICAL_EQUITY_ONLY_DOES_NOT_RESET_HWM_OR_CLEAR_DRAWDOWN"
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


def _fmt(v, digits=4):
    return "NA" if v is None else f"{float(v):.{digits}f}"


def format_summary(report):
    closest = report.get("closest_candidate") or {}
    closest_text = (
        f"{closest.get('symbol')}/{closest.get('setup')}"
        f":req={_fmt(closest.get('required_equity'))}"
        if closest else "NONE"
    )
    return (
        "[MIN_ORDER_CAPITAL_LADDER_V1] "
        f"epoch_id={report['epoch_id']} status={report['status']} "
        f"min_order_only_candidates={report['min_order_only_candidates']} "
        f"counterfactual_evaluated={report['counterfactual_evaluated']} "
        f"counterfactual_allowed={report['counterfactual_allowed']} "
        f"required_equity_min={_fmt(report['required_equity_min'])} "
        f"required_equity_median={_fmt(report['required_equity_median'])} "
        f"closest={closest_text} "
        "external_capital_does_not_clear_drawdown=true "
        "capital_metric_is_counterfactual_not_recommendation=true "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


def format_ladder(report):
    parts = []
    for row in report["ladder"]:
        p60 = row["allowed_60m"]
        p240 = row["allowed_240m"]
        parts.append(
            f"eq={row['equity']:.0f}"
            f":feasible={row['min_order_feasible_candidates']}"
            f":evaluated={row['counterfactual_evaluated']}"
            f":allowed={row['counterfactual_allowed']}"
            f":a60_n={p60['n']}:a60_avg={_fmt(p60['avg_return'],6)}"
            f":a60_pos={_fmt(p60['positive_rate'],4)}"
            f":a240_n={p240['n']}:a240_avg={_fmt(p240['avg_return'],6)}"
            f":a240_pos={_fmt(p240['positive_rate'],4)}"
        )
    return (
        "[MIN_ORDER_CAPITAL_LADDER_V1_LEVELS] "
        + "|".join(parts)
        + " canonical_pipeline_credit=0 lifetime_drawdown_credit=0 "
        "live_eligible=false execution_effect=NONE"
    )
