"""BBO Calibration V3: decoupled COST_ONLY and DECISION_IMPACT research cohorts.

COST_ONLY is available for every fresh HARD_GATE_SHADOW candidate with a BBO
snapshot, including candidates that fail MIN_ORDER before NEXUS. It measures
market-cost calibration only and never fabricates EV/R:R or a NEXUS decision.

DECISION_IMPACT remains the narrower V2 cohort and is populated only when NEXUS
was actually evaluated. Neither cohort has LIVE, risk, sizing or dispatch
authority.
"""
from __future__ import annotations

import json
import math
import os
import statistics

from bot import bbo_calibration_v2

POPULATION = "HARD_GATE_SHADOW"
FLAG = "NEXUS_BBO_CALIBRATION_V3"
MIN_SAMPLE = 50
PREFERRED_SAMPLE = 100
AUTHORITY = {
    "research_only": True,
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


def _percentile(values, q):
    xs = sorted(float(v) for v in values if _finite(v) is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * float(q)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def _stats(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    if not xs:
        return {"n": 0, "mean": None, "min": None, "max": None,
                "p50": None, "p95": None, "p99": None, "std": None}
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs),
        "min": min(xs),
        "max": max(xs),
        "p50": _percentile(xs, 0.50),
        "p95": _percentile(xs, 0.95),
        "p99": _percentile(xs, 0.99),
        "std": statistics.pstdev(xs) if len(xs) > 1 else 0.0,
    }


def _cost_candidate(raw):
    obs = raw.get("bbo_cost_only_observation")
    if not isinstance(obs, dict):
        return None
    if obs.get("cohort") != "COST_ONLY":
        return None
    if obs.get("shadow_only") is not True:
        return None
    if obs.get("decision_effect") != "NONE" or obs.get("execution_effect") != "NONE":
        return None

    static = _finite(obs.get("static_total_cost_bps"))
    live = _finite(obs.get("live_total_cost_bps"))
    reduction = _finite(obs.get("cost_reduction_bps"))
    if reduction is None and static is not None and live is not None:
        reduction = static - live

    return {
        "candidate_id": str(raw.get("candidate_id") or obs.get("candidate_id") or "UNKNOWN"),
        "symbol": str(raw.get("symbol") or obs.get("symbol") or "UNKNOWN"),
        "setup": str(raw.get("setup") or obs.get("setup") or "UNKNOWN"),
        "regime": str(raw.get("regime") or obs.get("regime") or "UNKNOWN"),
        "side": str(raw.get("side") or obs.get("side") or "UNKNOWN").upper(),
        "bbo_valid": obs.get("bbo_valid") is True,
        "bbo_reason": str(obs.get("bbo_reason") or "UNKNOWN"),
        "bbo_age_ms": _finite(obs.get("bbo_age_ms")),
        "spread_bps": _finite(obs.get("spread_bps")),
        "static_cost_bps": static,
        "bbo_cost_bps": live,
        "spread_only_cost_bps": _finite(obs.get("live_spread_only_cost_bps")),
        "cost_reduction_bps": reduction,
        "production_sha": str(obs.get("production_sha") or "UNKNOWN"),
    }


def _metric_block(rows):
    valid = [r for r in rows if r["bbo_valid"] and r["bbo_cost_bps"] is not None]
    return {
        "candidates": len(rows),
        "valid_bbo": len(valid),
        "valid_rate": len(valid) / len(rows) if rows else None,
        "static_cost_bps": _stats(r["static_cost_bps"] for r in valid),
        "bbo_cost_bps": _stats(r["bbo_cost_bps"] for r in valid),
        "spread_only_cost_bps": _stats(r["spread_only_cost_bps"] for r in valid),
        "cost_reduction_bps": _stats(r["cost_reduction_bps"] for r in valid),
        "bbo_age_ms": _stats(r["bbo_age_ms"] for r in valid),
        "spread_bps": _stats(r["spread_bps"] for r in valid),
    }


def _groups(rows, key):
    buckets = {}
    for row in rows:
        buckets.setdefault(row[key], []).append(row)
    return {name: _metric_block(items) for name, items in sorted(buckets.items())}


def _outliers(rows, metric, n=5):
    values = [r for r in rows if r["bbo_valid"] and _finite(r.get(metric)) is not None]
    values.sort(key=lambda r: abs(float(r[metric])), reverse=True)
    return [
        {
            "candidate_id": r["candidate_id"],
            "symbol": r["symbol"],
            "setup": r["setup"],
            "regime": r["regime"],
            "side": r["side"],
            "value": r[metric],
        }
        for r in values[:n]
    ]


def build_report(payloads):
    by_id = {}
    for raw in payloads:
        row = _cost_candidate(raw)
        if row is not None:
            by_id.setdefault(row["candidate_id"], row)
    cost_rows = list(by_id.values())
    valid = sum(1 for r in cost_rows if r["bbo_valid"] and r["bbo_cost_bps"] is not None)
    status = (
        "PREFERRED_SAMPLE_REACHED" if valid >= PREFERRED_SAMPLE
        else "MIN_SAMPLE_REACHED" if valid >= MIN_SAMPLE
        else "COLLECTING"
    )

    # V2 remains the truthful decision-impact cohort: no synthetic decisions.
    decision = bbo_calibration_v2.build_report(payloads)
    groups = {k: _groups(cost_rows, k) for k in ("symbol", "setup", "regime", "side")}
    return {
        **AUTHORITY,
        "status": status,
        "target_min": MIN_SAMPLE,
        "target_preferred": PREFERRED_SAMPLE,
        "cost_only_unique_candidates": len(cost_rows),
        "cost_only_valid_bbo": valid,
        "cost_only_global": _metric_block(cost_rows),
        "cost_only_groups": groups,
        "cost_only_outliers": {
            "cost_reduction_bps": _outliers(cost_rows, "cost_reduction_bps"),
            "bbo_age_ms": _outliers(cost_rows, "bbo_age_ms"),
            "spread_bps": _outliers(cost_rows, "spread_bps"),
        },
        "decision_impact_status": decision["status"],
        "decision_impact_unique_candidates": decision["unique_candidates"],
        "decision_impact_valid_bbo": decision["valid_bbo_candidates"],
        "decision_impact_global": decision["global"],
        "decision_impact_authority": {
            "promotion_allowed": decision["promotion_allowed"],
            "live_allowed": decision["live_allowed"],
            "decision_effect": decision["decision_effect"],
            "execution_effect": decision["execution_effect"],
        },
    }


async def load_payloads(db):
    rows = await db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? ORDER BY captured_epoch ASC",
        (POPULATION,),
    )
    payloads = []
    for row in rows or []:
        raw = row["payload"] if hasattr(row, "keys") else row[0]
        try:
            payloads.append(json.loads(raw) if isinstance(raw, str) else dict(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return payloads


async def snapshot(db):
    return build_report(await load_payloads(db))


def format_summary(report):
    g = report["cost_only_global"]

    def f(value, digits=3):
        return "NA" if value is None else f"{float(value):.{digits}f}"

    def group_counts(key):
        groups = report["cost_only_groups"].get(key, {})
        return ",".join(
            f"{name}:{int(block.get('candidates') or 0)}"
            for name, block in sorted(groups.items())
        ) or "NONE"

    return (
        "[BBO_CALIBRATION_V3] "
        f"status={report['status']} "
        f"cost_only_unique={report['cost_only_unique_candidates']} "
        f"cost_only_valid={report['cost_only_valid_bbo']} "
        f"target_min={report['target_min']} target_preferred={report['target_preferred']} "
        f"side_counts={group_counts('side')} "
        f"regime_counts={group_counts('regime')} "
        f"setup_counts={group_counts('setup')} "
        f"distinct_symbols={len(report['cost_only_groups'].get('symbol', {}))} "
        f"static_cost_mean_bps={f(g['static_cost_bps']['mean'])} "
        f"bbo_cost_mean_bps={f(g['bbo_cost_bps']['mean'])} "
        f"cost_reduction_mean_bps={f(g['cost_reduction_bps']['mean'])} "
        f"bbo_age_p95_ms={f(g['bbo_age_ms']['p95'], 1)} "
        f"spread_p95_bps={f(g['spread_bps']['p95'])} "
        f"decision_impact_unique={report['decision_impact_unique_candidates']} "
        f"decision_impact_valid={report['decision_impact_valid_bbo']} "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )


__all__ = [
    "AUTHORITY", "FLAG", "MIN_SAMPLE", "PREFERRED_SAMPLE", "build_report",
    "enabled", "format_summary", "load_payloads", "snapshot",
]
