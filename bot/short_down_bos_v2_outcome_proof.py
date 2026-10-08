"""Read-only, fail-closed provenance audit for prospective SHORT/DOWN/BOS V2.

This module cannot select/authorize orders or mutate the database. Enrollment
is based exclusively on stored canonical NEXUS decisions, never counterfactual
approval telemetry. A missing or UNKNOWN_CACHE_GAP horizon is NOT observed.
"""
from __future__ import annotations

import json
import math
import time

COHORT_ID = "SHORT_DOWN_BOS_PROSPECTIVE_V2_20261007"
CUTOFF_EPOCH = 1791397605.0  # 2026-10-07T18:26:45Z (frozen in issue #579)
TARGET = 12
MAX_PER_SYMBOL = 3
MAX_SCAN = 5000
INTERVAL_S = 300.0
_LAST_EMIT = 0.0
AUTHORITY = {
    "research_only": True,
    "shadow_only": True,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _raw(row, key, pos=0):
    return row[key] if hasattr(row, "keys") else row[pos]


def _json(raw):
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def canonical(row):
    """Check top-level persisted production-equivalent NEXUS decision."""
    return (
        row.get("population") == "HARD_GATE_SHADOW"
        and _finite(row.get("captured_epoch")) is not None
        and float(row["captured_epoch"]) >= CUTOFF_EPOCH
        and row.get("side") == "SHORT"
        and row.get("regime") == "TRENDING_DOWN"
        and row.get("setup") == "BOS_BREAK"
        and row.get("nexus_called") is True
        and row.get("nexus_allowed") is True
        and row.get("shadow_only") is True
        and row.get("live_eligible") is False
        and row.get("decision_effect") == "NONE"
        and row.get("execution_effect") == "NONE"
        and isinstance(row.get("candidate_id"), str)
        and isinstance(row.get("symbol"), str)
    )


def _proof(horizon, source):
    empty = {"outcome": "OUTCOME_NOT_PROVEN", "future_return": None,
             "MFE": None, "MAE": None, "observation_start": None,
             "return_basis": None, "verified": False}
    if not source:
        return empty
    data = _json(source)
    if data.get("outcome") != "OBSERVED" or data.get("horizon") != horizon:
        return {**empty, "outcome": str(data.get("outcome") or "OUTCOME_NOT_PROVEN")}
    metrics = [_finite(data.get(key)) for key in ("future_return", "MFE", "MAE")]
    if any(value is None for value in metrics):
        return empty
    if data.get("return_basis") != "hypothetical_entry_gross":
        return empty
    return {"outcome": "OBSERVED", "future_return": metrics[0],
            "MFE": metrics[1], "MAE": metrics[2],
            "observation_start": _finite(data.get("observation_start")),
            "return_basis": data["return_basis"], "verified": True}


def _metrics(members, horizon):
    proofs = [member[str(horizon)] for member in members]
    valid = [p for p in proofs if p["verified"]]
    if not valid:
        return {"n": 0, "avg_return": None, "positive_rate": None,
                "avg_mfe": None, "avg_mae": None}
    def avg(key):
        return sum(p[key] for p in valid) / len(valid)
    return {"n": len(valid), "avg_return": avg("future_return"),
            "positive_rate": sum(p["future_return"] > 0 for p in valid) / len(valid),
            "avg_mfe": avg("MFE"), "avg_mae": avg("MAE")}


async def snapshot(db):
    """Read ONLY two existing research tables; never backfill or overwrite."""
    candidates = await db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? "
        "ORDER BY captured_epoch, candidate_id LIMIT ?",
        ("HARD_GATE_SHADOW", CUTOFF_EPOCH, MAX_SCAN),
    )
    truncated = len(candidates or ()) >= MAX_SCAN
    sample, counts, seen = [], {}, set()
    quota_skipped = 0
    for raw in candidates or ():
        row = _json(_raw(raw, "payload"))
        if not canonical(row) or row["candidate_id"] in seen:
            continue
        seen.add(row["candidate_id"])
        if counts.get(row["symbol"], 0) >= MAX_PER_SYMBOL:
            quota_skipped += 1
            continue
        if len(sample) >= TARGET:
            break
        counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1
        sample.append({"candidate_id": row["candidate_id"],
                       "symbol": row["symbol"],
                       "captured_epoch": row["captured_epoch"]})
    for member in sample:
        raw_outcomes = await db._fetchall(
            "SELECT horizon, payload FROM hard_gate_shadow_outcomes_v1 "
            "WHERE candidate_id=? AND population=? AND horizon IN (?,?)",
            (member["candidate_id"], "HARD_GATE_SHADOW", 60, 240),
        )
        by_horizon = {}
        for raw in raw_outcomes or ():
            h = _raw(raw, "horizon", 0)
            if h in (60, 240):
                by_horizon[h] = _raw(raw, "payload", 1)
        for horizon in (60, 240):
            member[str(horizon)] = _proof(horizon, by_horizon.get(horizon))

    m60, m240 = _metrics(sample, 60), _metrics(sample, 240)
    complete = len(sample) == TARGET and m60["n"] == TARGET and m240["n"] == TARGET
    leave_min = {}
    for horizon in (60, 240):
        means = [_metrics([m for m in sample if m["symbol"] != symbol], horizon)["avg_return"]
                 for symbol in counts]
        leave_min[str(horizon)] = min(means) if means and all(x is not None for x in means) else None
    top_share = max(counts.values()) / len(sample) if sample else None
    passed = complete and not truncated and len(counts) >= 4 and top_share <= 0.25
    for stats, horizon in ((m60, 60), (m240, 240)):
        passed = passed and stats["avg_return"] is not None and stats["avg_return"] > 0
        passed = passed and stats["positive_rate"] is not None and stats["positive_rate"] >= 0.6
        passed = passed and leave_min[str(horizon)] is not None and leave_min[str(horizon)] > 0
    status = ("EVIDENCE_FAIL" if not passed else "READY_FOR_MANUAL_REVIEW") if complete else "COLLECTING"
    if truncated:
        status = "SCAN_TRUNCATED_MANUAL_AUDIT_REQUIRED"
    return {
        "cohort_id": COHORT_ID,
        "status": status,
        "sample_approvals": len(sample),
        "observed_60m": m60["n"],
        "observed_240m": m240["n"],
        "symbol_counts": counts,
        "distinct_symbols": len(counts),
        "top_symbol_share": top_share,
        "quota_skipped": quota_skipped,
        "avg_return_60m": m60["avg_return"],
        "avg_return_240m": m240["avg_return"],
        "positive_rate_60m": m60["positive_rate"],
        "positive_rate_240m": m240["positive_rate"],
        "avg_mfe_60m": m60["avg_mfe"],
        "avg_mae_60m": m60["avg_mae"],
        "avg_mfe_240m": m240["avg_mfe"],
        "avg_mae_240m": m240["avg_mae"],
        "leave_one_symbol_out_min_60m": leave_min["60"],
        "leave_one_symbol_out_min_240m": leave_min["240"],
        "sample_complete": len(sample) == TARGET,
        "scan_truncated": truncated,
        "members": sample,
        **AUTHORITY,
    }


async def maybe_snapshot(db):
    """At most one bounded DB readout every five minutes."""
    global _LAST_EMIT
    now = time.monotonic()
    if _LAST_EMIT and now - _LAST_EMIT < INTERVAL_S:
        return None
    _LAST_EMIT = now
    return await snapshot(db)
