"""BBO Calibration V2 reporting over persisted HARD_GATE_SHADOW research rows.

RESEARCH ONLY. This module is not imported by the trading runtime and has no
decision, risk, sizing, dispatch, order, position, protection or LIVE authority.

It consumes append-only hard-gate shadow candidate payloads that contain a
`bbo_cost_observation` and produces candidate-level calibration statistics.
"""
from __future__ import annotations

import json
import math
import statistics

POPULATION = "HARD_GATE_SHADOW"
MIN_SAMPLE = 50
PREFERRED_SAMPLE = 100
AUTHORITY = {
    "research_only": True,
    "shadow_only": True,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _percentile(values, q):
    xs = sorted(v for v in values if _finite(v) is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * float(q)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(xs[lo])
    frac = pos - lo
    return float(xs[lo] * (1 - frac) + xs[hi] * frac)


def _series_stats(values):
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


def _candidate(raw):
    obs = raw.get("bbo_cost_observation")
    if not isinstance(obs, dict):
        return None
    if obs.get("shadow_only") is not True:
        return None
    if obs.get("decision_effect") != "NONE" or obs.get("execution_effect") != "NONE":
        return None

    costs = obs.get("costs") if isinstance(obs.get("costs"), dict) else {}
    static = _finite(costs.get("static_total_cost_bps"))
    live = _finite(costs.get("live_plus_static_impact_cost_bps"))
    rr_static = _finite(obs.get("rr_net_static"))
    rr_live = _finite(obs.get("rr_net_live"))
    ev_static = _finite(obs.get("ev_static"))
    ev_live = _finite(obs.get("ev_live"))

    return {
        "candidate_id": str(raw.get("candidate_id") or obs.get("candidate_id") or "UNKNOWN"),
        "symbol": str(raw.get("symbol") or obs.get("symbol") or "UNKNOWN"),
        "setup": str(raw.get("setup") or obs.get("setup") or "UNKNOWN"),
        "regime": str(raw.get("regime") or obs.get("regime") or "UNKNOWN"),
        "side": str(raw.get("side") or obs.get("side") or "UNKNOWN").upper(),
        "bbo_valid": obs.get("bbo_valid") is True,
        "bbo_age_ms": _finite(obs.get("bbo_age_ms")),
        "spread_bps": _finite(obs.get("spread_bps")),
        "static_cost_bps": static,
        "bbo_cost_bps": live,
        "delta_cost_bps": None if static is None or live is None else live - static,
        "cost_reduction_bps": None if static is None or live is None else static - live,
        "delta_rr": None if rr_static is None or rr_live is None else rr_live - rr_static,
        "delta_ev": None if ev_static is None or ev_live is None else ev_live - ev_static,
        "would_change_decision": obs.get("would_change_decision"),
    }


def _metric_block(rows):
    valid = [r for r in rows if r["bbo_valid"]]
    changed = [r for r in valid if r["would_change_decision"] is True]
    return {
        "candidates": len(rows),
        "valid_bbo": len(valid),
        "valid_rate": (len(valid) / len(rows)) if rows else None,
        "would_change_decision": len(changed),
        "would_change_rate": (len(changed) / len(valid)) if valid else None,
        "static_cost_bps": _series_stats(r["static_cost_bps"] for r in valid),
        "bbo_cost_bps": _series_stats(r["bbo_cost_bps"] for r in valid),
        "delta_cost_bps": _series_stats(r["delta_cost_bps"] for r in valid),
        "cost_reduction_bps": _series_stats(r["cost_reduction_bps"] for r in valid),
        "delta_rr": _series_stats(r["delta_rr"] for r in valid),
        "delta_ev": _series_stats(r["delta_ev"] for r in valid),
        "bbo_age_ms": _series_stats(r["bbo_age_ms"] for r in valid),
        "spread_bps": _series_stats(r["spread_bps"] for r in valid),
    }


def _groups(rows, key):
    buckets = {}
    for row in rows:
        buckets.setdefault(row[key], []).append(row)
    return {name: _metric_block(items) for name, items in sorted(buckets.items())}


def _stability(grouped):
    result = {}
    for metric in ("cost_reduction_bps", "delta_rr", "delta_ev", "spread_bps"):
        means = [block[metric]["mean"] for block in grouped.values()
                 if block[metric]["mean"] is not None]
        result[metric] = _series_stats(means)
    return result


def _outliers(rows, metric, n=5):
    values = [r for r in rows if r["bbo_valid"] and _finite(r.get(metric)) is not None]
    values.sort(key=lambda r: abs(float(r[metric])), reverse=True)
    return [
        {"candidate_id": r["candidate_id"], "symbol": r["symbol"], "setup": r["setup"],
         "regime": r["regime"], "side": r["side"], "value": r[metric]}
        for r in values[:n]
    ]


def build_report(payloads):
    by_id = {}
    for raw in payloads:
        row = _candidate(raw)
        if row is not None:
            by_id.setdefault(row["candidate_id"], row)
    rows = list(by_id.values())
    valid = sum(r["bbo_valid"] for r in rows)
    status = ("PREFERRED_SAMPLE_REACHED" if valid >= PREFERRED_SAMPLE else
              "MIN_SAMPLE_REACHED" if valid >= MIN_SAMPLE else "COLLECTING")
    groups = {k: _groups(rows, k) for k in ("symbol", "setup", "regime", "side")}
    return {
        **AUTHORITY,
        "status": status,
        "target_min": MIN_SAMPLE,
        "target_preferred": PREFERRED_SAMPLE,
        "unique_candidates": len(rows),
        "valid_bbo_candidates": valid,
        "global": _metric_block(rows),
        "groups": groups,
        "stability": {k: _stability(v) for k, v in groups.items()},
        "outliers": {
            "delta_cost_bps": _outliers(rows, "delta_cost_bps"),
            "delta_rr": _outliers(rows, "delta_rr"),
            "delta_ev": _outliers(rows, "delta_ev"),
            "bbo_age_ms": _outliers(rows, "bbo_age_ms"),
            "spread_bps": _outliers(rows, "spread_bps"),
        },
    }


async def load_payloads(db):
    rows = await db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 WHERE population=? ORDER BY captured_epoch ASC",
        (POPULATION,),
    )
    payloads = []
    for row in rows:
        raw = row["payload"] if hasattr(row, "keys") else row[0]
        try:
            payloads.append(json.loads(raw) if isinstance(raw, str) else dict(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return payloads


async def snapshot(db):
    return build_report(await load_payloads(db))


def format_summary(report):
    g = report["global"]
    def f(value, digits=4):
        return "NA" if value is None else f"{float(value):.{digits}f}"
    return (
        "[BBO_CALIBRATION_V2] "
        f"status={report['status']} unique_candidates={report['unique_candidates']} "
        f"valid_bbo={report['valid_bbo_candidates']} target_min={report['target_min']} "
        f"target_preferred={report['target_preferred']} "
        f"static_cost_mean_bps={f(g['static_cost_bps']['mean'], 3)} "
        f"bbo_cost_mean_bps={f(g['bbo_cost_bps']['mean'], 3)} "
        f"cost_reduction_mean_bps={f(g['cost_reduction_bps']['mean'], 3)} "
        f"delta_rr_mean={f(g['delta_rr']['mean'], 4)} "
        f"delta_ev_mean={f(g['delta_ev']['mean'], 4)} "
        f"would_change={g['would_change_decision']} "
        f"bbo_age_p95_ms={f(g['bbo_age_ms']['p95'], 1)} "
        f"spread_p95_bps={f(g['spread_bps']['p95'], 3)} "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )


__all__ = ["AUTHORITY", "MIN_SAMPLE", "PREFERRED_SAMPLE", "build_report",
           "format_summary", "load_payloads", "snapshot"]
