"""Prospective validation for SHORT + TRENDING_DOWN + BOS_BREAK.

Research-only evaluator over the existing HARD_GATE_SHADOW dataset. It never
generates candidates, calls NEXUS, changes thresholds/risk/sizing, or grants
LIVE authority.

The hypothesis is frozen prospectively at first startup after deployment. The
three UNI observations that motivated this study are necessarily pre-cutoff and
therefore excluded. The sample is the first 10 naturally NEXUS-approved rows
matching the frozen segment, in chronological order; later rows cannot replace
or improve the sample.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import os
import time

FLAG = "SHORT_DOWN_BOS_PROSPECTIVE_V1"
COHORT_ID = "SHORT_DOWN_BOS_PROSPECTIVE_V1_20261006"
POPULATION = "HARD_GATE_SHADOW"

TARGET_APPROVALS = 10
TARGET_OUTCOMES_60M = 10
TARGET_OUTCOMES_240M = 10
MAX_SYMBOL_CONCENTRATION = 0.50

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
    "prior_r3_seed_excluded": True,
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

_META = """CREATE TABLE IF NOT EXISTS short_down_bos_prospective_v1 (
 cohort_id TEXT PRIMARY KEY,
 started_epoch REAL NOT NULL,
 payload TEXT NOT NULL
)"""


def enabled() -> bool:
    # Explicit opt-in: importing this research observer must never add database
    # writes to the hard-gate scan or weaken its mid-scan clear guarantees.
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
        "SELECT payload FROM short_down_bos_prospective_v1 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)

    started = float(time.time() if started_epoch is None else started_epoch)
    baseline = {
        **AUTHORITY,
        "cohort_id": COHORT_ID,
        "started_epoch": started,
        "discovery_cutoff_epoch": started,
        "selection": SELECTION,
        "target_approvals": TARGET_APPROVALS,
        "target_outcomes_60m": TARGET_OUTCOMES_60M,
        "target_outcomes_240m": TARGET_OUTCOMES_240M,
        "required_avg_return_60m_gt_zero": True,
        "required_avg_return_240m_gt_zero": True,
        "max_symbol_concentration": MAX_SYMBOL_CONCENTRATION,
        "first_n_chronological_sample": True,
        "sample_replacement_allowed": False,
        "hypothesis_frozen": True,
        "reset_allowed": False,
    }
    await db._exec(
        "INSERT INTO short_down_bos_prospective_v1 "
        "(cohort_id,started_epoch,payload) VALUES (?,?,?) "
        "ON CONFLICT(cohort_id) DO NOTHING",
        (
            COHORT_ID,
            started,
            json.dumps(baseline, sort_keys=True, allow_nan=False),
        ),
    )
    rows = await db._fetchall(
        "SELECT payload FROM short_down_bos_prospective_v1 WHERE cohort_id=?",
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
    if not cid:
        return None
    return {
        "candidate_id": cid,
        "captured_epoch": captured,
        "symbol": str(raw.get("symbol") or ""),
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
    return {
        "future_return": ret,
        "MFE": mfe,
        "MAE": mae,
    }


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
        if row is None:
            continue
        cid = row["candidate_id"]
        if cid in seen:
            continue
        seen.add(cid)
        eligible.append(row)
    eligible.sort(key=lambda r: (r["captured_epoch"], r["candidate_id"]))

    sample = eligible[:TARGET_APPROVALS]
    sample_ids = {r["candidate_id"] for r in sample}
    matched60 = []
    matched240 = []
    for row in sample:
        cid = row["candidate_id"]
        o60 = _outcome(outcomes60.get(cid), horizon=60)
        o240 = _outcome(outcomes240.get(cid), horizon=240)
        if o60 is not None:
            matched60.append({**row, **o60})
        if o240 is not None:
            matched240.append({**row, **o240})

    symbols = Counter(r["symbol"] for r in sample)
    top_symbol = "NONE"
    top_symbol_n = 0
    if symbols:
        top_symbol, top_symbol_n = sorted(
            symbols.items(), key=lambda item: (-item[1], item[0])
        )[0]
    top_share = _rate(top_symbol_n, len(sample))

    avg60 = _mean(r["future_return"] for r in matched60)
    avg240 = _mean(r["future_return"] for r in matched240)
    pos60 = _rate(
        sum(1 for r in matched60 if r["future_return"] > 0.0), len(matched60)
    )
    pos240 = _rate(
        sum(1 for r in matched240 if r["future_return"] > 0.0), len(matched240)
    )

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
        if avg60 is None or avg60 <= 0.0:
            blockers.append("AVG_RETURN_60M_NOT_POSITIVE")
        if avg240 is None or avg240 <= 0.0:
            blockers.append("AVG_RETURN_240M_NOT_POSITIVE")
        if top_share is None or top_share > MAX_SYMBOL_CONCENTRATION:
            blockers.append("SYMBOL_CONCENTRATION")

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
        "started_epoch": started,
        "selection": baseline.get("selection"),
        "hypothesis_frozen": baseline.get("hypothesis_frozen") is True,
        "reset_allowed": baseline.get("reset_allowed"),
        "target_approvals": TARGET_APPROVALS,
        "eligible_approvals_total": len(eligible),
        "sample_approvals": len(sample),
        "post_target_ignored": max(0, len(eligible) - TARGET_APPROVALS),
        "sample_candidate_ids": tuple(r["candidate_id"] for r in sample),
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
        "top_symbol": top_symbol,
        "top_symbol_n": top_symbol_n,
        "top_symbol_share": top_share,
        "max_symbol_concentration": MAX_SYMBOL_CONCENTRATION,
        "sample_complete": sample_complete,
        "first_n_chronological_sample": True,
        "sample_replacement_allowed": False,
        "prior_seed_candidate_ids_counted": len(
            sample_ids.intersection({
                "HARD_GATE_SHADOW:UNIUSDT:SHORT:BOS_BREAK:1990333",
                "HARD_GATE_SHADOW:UNIUSDT:SHORT:BOS_BREAK:1990334",
                "HARD_GATE_SHADOW:UNIUSDT:SHORT:BOS_BREAK:1990335",
            })
        ),
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
        "status", "blockers", "cohort_id", "started_epoch",
        "eligible_approvals_total", "sample_approvals", "post_target_ignored",
        "observed_60m", "observed_240m",
        "avg_return_60m", "avg_return_240m",
        "positive_rate_60m", "positive_rate_240m",
        "avg_mfe_60m", "avg_mae_60m", "avg_mfe_240m", "avg_mae_240m",
        "symbol_counts", "top_symbol", "top_symbol_share",
        "max_symbol_concentration", "sample_complete",
        "prior_seed_candidate_ids_counted", "hypothesis_frozen",
        "sample_replacement_allowed", "thresholds_unchanged",
        "risk_unchanged", "sizing_unchanged", "promotion_allowed",
        "live_allowed", "decision_effect", "execution_effect",
    )
    return "[SHORT_DOWN_BOS_PROSPECTIVE_V1] " + " ".join(
        f"{key}={_fmt(row.get(key))}" for key in keys
    )
