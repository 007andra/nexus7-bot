"""Independent confirmatory cohort for SHORT + TRENDING_DOWN + BOS_BREAK.

Research-only evaluator over the existing HARD_GATE_SHADOW dataset. The cohort
starts at a durable post-deploy cutoff and only admits natural NEXUS approvals
observed after that cutoff. Enrollment uses a pre-registered chronological
per-symbol quota so no single symbol can dominate the confirmatory sample.

This module cannot generate candidates, call NEXUS, change thresholds/risk/
sizing/leverage, or grant LIVE authority.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import os
import time

FLAG = "SHORT_DOWN_BOS_PROSPECTIVE_V2"
COHORT_ID = "SHORT_DOWN_BOS_PROSPECTIVE_V2_20261007"
POPULATION = "HARD_GATE_SHADOW"
SOURCE_COHORT_ID = "SHORT_DOWN_BOS_PROSPECTIVE_V1_20261006"

TARGET_APPROVALS = 12
TARGET_OUTCOMES_60M = 12
TARGET_OUTCOMES_240M = 12
MAX_PER_SYMBOL = 3
MAX_SYMBOL_CONCENTRATION = 0.25
MIN_DISTINCT_SYMBOLS = 4
MIN_POSITIVE_RATE_60M = 0.60
MIN_POSITIVE_RATE_240M = 0.60

SELECTION = {
    "side": "SHORT",
    "regime": "TRENDING_DOWN",
    "setup": "BOS_BREAK",
    "natural_nexus_approval_required": True,
}

AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "research_only": True,
    "read_only_evaluation": True,
    "shadow_only": True,
    "prospective_only": True,
    "independent_confirmatory_cohort": True,
    "candidate_generation_unchanged": True,
    "thresholds_unchanged": True,
    "risk_unchanged": True,
    "sizing_unchanged": True,
    "leverage_unchanged": True,
    "historical_hwm_preserved": True,
    "lifetime_drawdown_preserved": True,
    "current_hard_gate_unchanged": True,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}

_META = """CREATE TABLE IF NOT EXISTS short_down_bos_prospective_v2 (
 cohort_id TEXT PRIMARY KEY,
 started_epoch REAL NOT NULL,
 payload TEXT NOT NULL
)"""


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
    vals = [float(v) for v in values if _finite(v) is not None]
    return sum(vals) / len(vals) if vals else None


def _rate(num, den):
    return float(num) / float(den) if den else None


async def ensure_cohort(db, *, started_epoch=None):
    await db._exec(_META)
    rows = await db._fetchall(
        "SELECT payload FROM short_down_bos_prospective_v2 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)

    started = float(time.time() if started_epoch is None else started_epoch)
    baseline = {
        **AUTHORITY,
        "cohort_id": COHORT_ID,
        "source_cohort_id": SOURCE_COHORT_ID,
        "started_epoch": started,
        "discovery_cutoff_epoch": started,
        "selection": SELECTION,
        "target_approvals": TARGET_APPROVALS,
        "target_outcomes_60m": TARGET_OUTCOMES_60M,
        "target_outcomes_240m": TARGET_OUTCOMES_240M,
        "max_per_symbol": MAX_PER_SYMBOL,
        "max_symbol_concentration": MAX_SYMBOL_CONCENTRATION,
        "min_distinct_symbols": MIN_DISTINCT_SYMBOLS,
        "min_positive_rate_60m": MIN_POSITIVE_RATE_60M,
        "min_positive_rate_240m": MIN_POSITIVE_RATE_240M,
        "required_avg_return_60m_gt_zero": True,
        "required_avg_return_240m_gt_zero": True,
        "required_leave_one_symbol_out_both_horizons_positive": True,
        "sequential_symbol_quota": True,
        "chronological_quota_compliant_sample": True,
        "sample_replacement_allowed": False,
        "hypothesis_frozen": True,
        "reset_allowed": False,
    }
    await db._exec(
        "INSERT INTO short_down_bos_prospective_v2 "
        "(cohort_id,started_epoch,payload) VALUES (?,?,?) "
        "ON CONFLICT(cohort_id) DO NOTHING",
        (
            COHORT_ID,
            started,
            json.dumps(baseline, sort_keys=True, allow_nan=False),
        ),
    )
    rows = await db._fetchall(
        "SELECT payload FROM short_down_bos_prospective_v2 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)
    return baseline


def _candidate(raw, *, started_epoch):
    if not isinstance(raw, dict):
        return None
    captured = _finite(raw.get("captured_epoch"))
    if captured is None or captured + 1e-9 < float(started_epoch):
        return None
    if raw.get("population") != POPULATION:
        return None
    if str(raw.get("side") or "").upper() != SELECTION["side"]:
        return None
    if str(raw.get("regime") or "").upper() != SELECTION["regime"]:
        return None
    if str(raw.get("setup") or "").upper() != SELECTION["setup"]:
        return None
    if raw.get("nexus_called") is not True or raw.get("nexus_allowed") is not True:
        return None
    if raw.get("shadow_only") is not True or raw.get("live_eligible") is not False:
        return None
    if raw.get("decision_effect") != "NONE" or raw.get("execution_effect") != "NONE":
        return None
    cid = str(raw.get("candidate_id") or "")
    symbol = str(raw.get("symbol") or "").upper()
    if not cid or not symbol:
        return None
    return {
        "candidate_id": cid,
        "captured_epoch": captured,
        "symbol": symbol,
        "side": "SHORT",
        "regime": "TRENDING_DOWN",
        "setup": "BOS_BREAK",
        "score": _finite(raw.get("score")),
        "risk_reward": _finite(raw.get("nexus_net_rr")),
        "expected_value": _finite(raw.get("nexus_ev")),
    }


def _outcome(raw, *, horizon):
    if not isinstance(raw, dict) or raw.get("outcome") != "OBSERVED":
        return None
    try:
        payload_horizon = int(raw.get("horizon"))
    except (TypeError, ValueError):
        return None
    if payload_horizon != int(horizon):
        return None
    ret = _finite(raw.get("future_return"))
    mfe = _finite(raw.get("MFE"))
    mae = _finite(raw.get("MAE"))
    if None in (ret, mfe, mae):
        return None
    return {"future_return": ret, "MFE": mfe, "MAE": mae}


def _quota_sample(eligible):
    sample = []
    counts = Counter()
    skipped = Counter()
    for row in eligible:
        symbol = row["symbol"]
        if counts[symbol] >= MAX_PER_SYMBOL:
            skipped[symbol] += 1
            continue
        sample.append(row)
        counts[symbol] += 1
        if len(sample) >= TARGET_APPROVALS:
            break
    return sample, counts, skipped


def _leave_one_symbol_out(rows):
    symbols = sorted({r["symbol"] for r in rows})
    values = {}
    for symbol in symbols:
        kept = [r["future_return"] for r in rows if r["symbol"] != symbol]
        values[symbol] = _mean(kept)
    finite = [v for v in values.values() if v is not None]
    return values, (min(finite) if finite else None)


def evaluate(candidates, outcomes60, outcomes240, *, baseline):
    started = _finite(baseline.get("started_epoch"))
    if started is None:
        return {
            **AUTHORITY,
            "status": "AUDIT_FAIL_CLOSED",
            "blockers": ("INVALID_FROZEN_CUTOFF",),
            "cohort_id": baseline.get("cohort_id"),
        }

    eligible = []
    seen = set()
    for raw in candidates:
        row = _candidate(raw, started_epoch=started)
        if row is None or row["candidate_id"] in seen:
            continue
        seen.add(row["candidate_id"])
        eligible.append(row)
    eligible.sort(key=lambda r: (r["captured_epoch"], r["candidate_id"]))

    sample, symbols, quota_skipped = _quota_sample(eligible)
    matched60, matched240 = [], []
    for row in sample:
        cid = row["candidate_id"]
        o60 = _outcome(outcomes60.get(cid), horizon=60)
        o240 = _outcome(outcomes240.get(cid), horizon=240)
        if o60 is not None:
            matched60.append({**row, **o60})
        if o240 is not None:
            matched240.append({**row, **o240})

    top_symbol, top_symbol_n = "NONE", 0
    if symbols:
        top_symbol, top_symbol_n = sorted(
            symbols.items(), key=lambda item: (-item[1], item[0])
        )[0]
    top_share = _rate(top_symbol_n, len(sample))
    distinct_symbols = len(symbols)

    avg60 = _mean(r["future_return"] for r in matched60)
    avg240 = _mean(r["future_return"] for r in matched240)
    pos60 = _rate(
        sum(1 for r in matched60 if r["future_return"] > 0.0), len(matched60)
    )
    pos240 = _rate(
        sum(1 for r in matched240 if r["future_return"] > 0.0), len(matched240)
    )
    loo60, loo60_min = _leave_one_symbol_out(matched60)
    loo240, loo240_min = _leave_one_symbol_out(matched240)

    blockers = []
    if len(sample) < TARGET_APPROVALS:
        blockers.append("TARGET_APPROVALS")
    if len(matched60) < TARGET_OUTCOMES_60M:
        blockers.append("TARGET_OUTCOMES_60M")
    if len(matched240) < TARGET_OUTCOMES_240M:
        blockers.append("TARGET_OUTCOMES_240M")

    sample_complete = not any(
        b in blockers
        for b in ("TARGET_APPROVALS", "TARGET_OUTCOMES_60M", "TARGET_OUTCOMES_240M")
    )
    if sample_complete:
        if distinct_symbols < MIN_DISTINCT_SYMBOLS:
            blockers.append("DISTINCT_SYMBOLS")
        if top_share is None or top_share > MAX_SYMBOL_CONCENTRATION + 1e-12:
            blockers.append("SYMBOL_CONCENTRATION")
        if avg60 is None or avg60 <= 0.0:
            blockers.append("AVG_RETURN_60M_NOT_POSITIVE")
        if avg240 is None or avg240 <= 0.0:
            blockers.append("AVG_RETURN_240M_NOT_POSITIVE")
        if pos60 is None or pos60 < MIN_POSITIVE_RATE_60M:
            blockers.append("POSITIVE_RATE_60M")
        if pos240 is None or pos240 < MIN_POSITIVE_RATE_240M:
            blockers.append("POSITIVE_RATE_240M")
        if loo60_min is None or loo60_min <= 0.0:
            blockers.append("LEAVE_ONE_SYMBOL_OUT_60M_NOT_POSITIVE")
        if loo240_min is None or loo240_min <= 0.0:
            blockers.append("LEAVE_ONE_SYMBOL_OUT_240M_NOT_POSITIVE")

    if not sample_complete:
        status = (
            "COLLECTING_APPROVALS"
            if len(sample) < TARGET_APPROVALS
            else "OUTCOMES_PENDING"
        )
    elif blockers:
        status = "EVIDENCE_FAIL"
    else:
        status = "READY_FOR_MANUAL_REVIEW"

    return {
        **AUTHORITY,
        "status": status,
        "blockers": tuple(blockers),
        "cohort_id": baseline.get("cohort_id"),
        "source_cohort_id": baseline.get("source_cohort_id"),
        "started_epoch": started,
        "selection": baseline.get("selection"),
        "hypothesis_frozen": baseline.get("hypothesis_frozen") is True,
        "reset_allowed": baseline.get("reset_allowed"),
        "target_approvals": TARGET_APPROVALS,
        "eligible_approvals_total": len(eligible),
        "sample_approvals": len(sample),
        "sample_candidate_ids": tuple(r["candidate_id"] for r in sample),
        "quota_skipped_total": sum(quota_skipped.values()),
        "quota_skipped_by_symbol": dict(sorted(quota_skipped.items())),
        "observed_60m": len(matched60),
        "observed_240m": len(matched240),
        "avg_return_60m": avg60,
        "avg_return_240m": avg240,
        "positive_rate_60m": pos60,
        "positive_rate_240m": pos240,
        "avg_mfe_60m": _mean(r["MFE"] for r in matched60),
        "avg_mae_60m": _mean(r["MAE"] for r in matched60),
        "avg_mfe_240m": _mean(r["MFE"] for r in matched240),
        "avg_mae_240m": _mean(r["MAE"] for r in matched240),
        "symbol_counts": dict(sorted(symbols.items())),
        "distinct_symbols": distinct_symbols,
        "top_symbol": top_symbol,
        "top_symbol_n": top_symbol_n,
        "top_symbol_share": top_share,
        "max_per_symbol": MAX_PER_SYMBOL,
        "max_symbol_concentration": MAX_SYMBOL_CONCENTRATION,
        "min_distinct_symbols": MIN_DISTINCT_SYMBOLS,
        "min_positive_rate_60m": MIN_POSITIVE_RATE_60M,
        "min_positive_rate_240m": MIN_POSITIVE_RATE_240M,
        "leave_one_symbol_out_avg_return_60m": loo60,
        "leave_one_symbol_out_avg_return_240m": loo240,
        "leave_one_symbol_out_min_avg_return_60m": loo60_min,
        "leave_one_symbol_out_min_avg_return_240m": loo240_min,
        "leave_one_symbol_out_positive_both_horizons": bool(
            sample_complete
            and loo60_min is not None and loo60_min > 0.0
            and loo240_min is not None and loo240_min > 0.0
        ),
        "sample_complete": sample_complete,
        "sequential_symbol_quota": True,
        "chronological_quota_compliant_sample": True,
        "sample_replacement_allowed": False,
    }


async def snapshot(db):
    baseline = await ensure_cohort(db)
    started = float(baseline["started_epoch"])

    rows = await db._fetchall(
        "SELECT candidate_id,payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? "
        "ORDER BY captured_epoch,candidate_id",
        (POPULATION, max(0.0, started - 1.0)),
    )
    candidates = []
    for item in rows or []:
        table_cid = str(item["candidate_id"] if hasattr(item, "keys") else item[0])
        raw = item["payload"] if hasattr(item, "keys") else item[1]
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if str(obj.get("candidate_id") or "") != table_cid:
            continue
        candidates.append(obj)

    out_rows = await db._fetchall(
        "SELECT candidate_id,horizon,payload FROM hard_gate_shadow_outcomes_v1 "
        "WHERE population=? AND horizon IN (?,?) ORDER BY candidate_id,horizon",
        (POPULATION, 60, 240),
    )
    out60, out240 = {}, {}
    for item in out_rows or []:
        cid = str(item["candidate_id"] if hasattr(item, "keys") else item[0])
        horizon = int(item["horizon"] if hasattr(item, "keys") else item[1])
        raw = item["payload"] if hasattr(item, "keys") else item[2]
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        (out60 if horizon == 60 else out240)[cid] = obj

    return evaluate(candidates, out60, out240, baseline=baseline)


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, (tuple, list)):
        return ",".join(map(str, value)) or "NONE"
    if isinstance(value, dict):
        return ",".join(f"{k}:{v}" for k, v in value.items()) or "NONE"
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value).replace(" ", "_")


def format_log(row):
    keys = (
        "status", "blockers", "cohort_id", "source_cohort_id", "started_epoch",
        "eligible_approvals_total", "sample_approvals", "quota_skipped_total",
        "quota_skipped_by_symbol", "observed_60m", "observed_240m",
        "avg_return_60m", "avg_return_240m",
        "positive_rate_60m", "positive_rate_240m",
        "avg_mfe_60m", "avg_mae_60m", "avg_mfe_240m", "avg_mae_240m",
        "symbol_counts", "distinct_symbols", "top_symbol", "top_symbol_share",
        "max_per_symbol", "max_symbol_concentration", "min_distinct_symbols",
        "leave_one_symbol_out_min_avg_return_60m",
        "leave_one_symbol_out_min_avg_return_240m",
        "leave_one_symbol_out_positive_both_horizons",
        "sample_complete", "hypothesis_frozen", "sample_replacement_allowed",
        "thresholds_unchanged", "risk_unchanged", "sizing_unchanged",
        "promotion_allowed", "live_allowed", "decision_effect",
        "execution_effect",
    )
    return "[SHORT_DOWN_BOS_PROSPECTIVE_V2] " + " ".join(
        f"{key}={_fmt(row.get(key))}" for key in keys
    )
