"""Prospective OOS Cohort V1.

Immutable-start, research-only validation cohort created after discovery.
Only HARD_GATE_SHADOW candidates captured after the cohort start are eligible.
The frozen hypothesis is intentionally simple: the existing NEXUS approval
function must show positive out-of-sample lift over rejected candidates at both
60m and 240m, with controlled concentration and no worse adverse excursion.

Passing this study can only make evidence READY_FOR_MANUAL_REVIEW. It never
authorizes LIVE execution or modifies risk, sizing, thresholds, HWM/drawdown,
recovery, override, dispatch, or exchange state.
"""
from bot.oos_readonly_connection_v1 import OOSReadOnlyConnection
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
import time

FLAG = "PROSPECTIVE_OOS_COHORT_V1"
POPULATION = "HARD_GATE_SHADOW"
COHORT = "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS"
COHORT_ID = "CALIBRATION_GENERALIZATION_V1"
TARGET_CANDIDATES = 50
TARGET_OBSERVED_60M = 30
TARGET_OBSERVED_240M = 30
MIN_ALLOWED_OUTCOMES = 5
MIN_REJECTED_OUTCOMES = 20
MAX_ALLOWED_CONCENTRATION = 0.80

AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "prospective_only": True,
    "discovery_sample_excluded": True,
    "thresholds_unchanged": True,
    "risk_epoch_traversal_credit": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}

_META = """CREATE TABLE IF NOT EXISTS prospective_oos_cohort_v1 (
 cohort_id TEXT PRIMARY KEY,
 started_epoch REAL NOT NULL,
 payload TEXT NOT NULL
)"""

FROZEN_HYPOTHESIS = {
    "hypothesis_id": COHORT_ID,
    "selection": "CURRENT_COUNTERFACTUAL_NEXUS_APPROVAL_FUNCTION_UNCHANGED",
    "required_60m_allowed_avg_return_gt_zero": True,
    "required_240m_allowed_avg_return_gt_zero": True,
    "required_allowed_vs_rejected_mean_lift_positive_both_horizons": True,
    "required_allowed_vs_rejected_positive_rate_lift_positive_both_horizons": True,
    "required_allowed_mae_not_worse_both_horizons": True,
    "max_allowed_concentration_side": MAX_ALLOWED_CONCENTRATION,
    "max_allowed_concentration_regime": MAX_ALLOWED_CONCENTRATION,
    "max_allowed_concentration_setup": MAX_ALLOWED_CONCENTRATION,
    "target_candidates": TARGET_CANDIDATES,
    "target_observed_60m": TARGET_OBSERVED_60M,
    "target_observed_240m": TARGET_OBSERVED_240M,
    "min_allowed_outcomes_each_horizon": MIN_ALLOWED_OUTCOMES,
    "min_rejected_outcomes_each_horizon": MIN_REJECTED_OUTCOMES,
    "production_threshold_changes": False,
}


def enabled() -> bool:
    return os.environ.get(FLAG, "true").strip().lower() in {"1", "true", "yes", "on"}


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
    return sum(xs) / len(xs) if xs else None


def _rate(num, den):
    return float(num) / float(den) if den else None


def _performance(rows):
    if not rows:
        return {
            "n": 0, "avg_return": None, "positive_rate": None,
            "avg_mfe": None, "avg_mae": None,
        }
    return {
        "n": len(rows),
        "avg_return": _mean(r["future_return"] for r in rows),
        "positive_rate": _rate(
            sum(1 for r in rows if r["future_return"] > 0.0), len(rows)
        ),
        "avg_mfe": _mean(r["MFE"] for r in rows),
        "avg_mae": _mean(r["MAE"] for r in rows),
    }


def _lift(a, r, key):
    av, rv = a.get(key), r.get(key)
    if av is None or rv is None:
        return None
    return float(av) - float(rv)


def _concentration(records):
    allowed = [r for r in records.values() if r["allowed"]]
    result = {}
    for dim in ("side", "regime", "setup"):
        counts = defaultdict(int)
        for row in allowed:
            counts[str(row.get(dim) or "UNKNOWN")] += 1
        top_value, top_n = ("NONE", 0)
        if counts:
            top_value, top_n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        result[dim] = {
            "top_value": top_value,
            "top_n": top_n,
            "allowed_total": len(allowed),
            "top_share": _rate(top_n, len(allowed)),
            "distinct": len(counts),
        }
    return result


# Explicit opt-in performance experiment. Frozen metadata stays durable and
# authoritative; do not mutate, reset, or backfill it for research convenience.
METADATA_FAST_PATH_FLAG = "OOS_METADATA_FROZEN_READONLY_V1"


def metadata_fast_path_enabled() -> bool:
    return os.environ.get(METADATA_FAST_PATH_FLAG, "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


async def load_frozen_metadata_for_snapshot(db, *, use_fast_path=None):
    """Read existing immutable metadata first; preserve legacy fallback.

    Under the default OFF setting the exact former ensure_cohort path runs.
    ON: one existing SELECT rather than DDL+SELECT when the row exists.
    If the table or cohort row is missing, return to the original guarded
    ensure_cohort path; do not create a new cohort without that guard.
    No new tables, risk authority, candidate/outcome selection or timer change.
    """
    # A dedicated OOS reader must never fall back to DDL or the trading DB.
    # Legacy callers retain their existing feature-flag behavior.
    isolated_reader = isinstance(db, OOSReadOnlyConnection)
    if isolated_reader:
        use_fast_path = True
    elif use_fast_path is None:
        use_fast_path = metadata_fast_path_enabled()
    if not use_fast_path:
        return await ensure_cohort(db)
    rows = await db._fetchall(
        "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)
    if isolated_reader:
        raise RuntimeError("OOS_READONLY_METADATA_MISSING")
    return await ensure_cohort(db)


async def ensure_cohort(db, *, started_epoch=None):
    await db._exec(_META)
    rows = await db._fetchall(
        "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)

    start = float(time.time() if started_epoch is None else started_epoch)
    row = {
        **AUTHORITY,
        "cohort_id": COHORT_ID,
        "started_epoch": start,
        "hypothesis": FROZEN_HYPOTHESIS,
        "hypothesis_frozen": True,
        "discovery_cutoff_epoch": start,
        "reset_allowed": False,
    }
    await db._exec(
        "INSERT INTO prospective_oos_cohort_v1 (cohort_id,started_epoch,payload) "
        "VALUES (?,?,?) ON CONFLICT(cohort_id) DO NOTHING",
        (COHORT_ID, start, json.dumps(row, sort_keys=True, allow_nan=False)),
    )
    rows = await db._fetchall(
        "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)
    return row


def build_report(payloads, outcomes60=(), outcomes240=(), *, baseline):
    from bot import counterfactual_approval_failure_analysis_v1 as base

    records = base._records(payloads)
    m60 = base._matched(records, base._outcome_map(outcomes60))
    m240 = base._matched(records, base._outcome_map(outcomes240))

    a60 = _performance([r for r in m60 if r["allowed"]])
    r60 = _performance([r for r in m60 if not r["allowed"]])
    a240 = _performance([r for r in m240 if r["allowed"]])
    r240 = _performance([r for r in m240 if not r["allowed"]])

    concentration = _concentration(records)
    lift60 = _lift(a60, r60, "avg_return")
    lift240 = _lift(a240, r240, "avg_return")
    pos_lift60 = _lift(a60, r60, "positive_rate")
    pos_lift240 = _lift(a240, r240, "positive_rate")

    sample_complete = (
        len(records) >= TARGET_CANDIDATES
        and len(m60) >= TARGET_OBSERVED_60M
        and len(m240) >= TARGET_OBSERVED_240M
        and a60["n"] >= MIN_ALLOWED_OUTCOMES
        and a240["n"] >= MIN_ALLOWED_OUTCOMES
        and r60["n"] >= MIN_REJECTED_OUTCOMES
        and r240["n"] >= MIN_REJECTED_OUTCOMES
    )
    concentration_ok = all(
        row["top_share"] is not None
        and row["top_share"] <= MAX_ALLOWED_CONCENTRATION
        for row in concentration.values()
    )
    performance_ok = (
        a60["avg_return"] is not None and a60["avg_return"] > 0.0
        and a240["avg_return"] is not None and a240["avg_return"] > 0.0
        and lift60 is not None and lift60 > 0.0
        and lift240 is not None and lift240 > 0.0
        and pos_lift60 is not None and pos_lift60 > 0.0
        and pos_lift240 is not None and pos_lift240 > 0.0
        and a60["avg_mae"] is not None and r60["avg_mae"] is not None
        and a60["avg_mae"] >= r60["avg_mae"]
        and a240["avg_mae"] is not None and r240["avg_mae"] is not None
        and a240["avg_mae"] >= r240["avg_mae"]
    )

    blockers = []
    if len(records) < TARGET_CANDIDATES:
        blockers.append("TARGET_CANDIDATES")
    if len(m60) < TARGET_OBSERVED_60M:
        blockers.append("TARGET_OBSERVED_60M")
    if len(m240) < TARGET_OBSERVED_240M:
        blockers.append("TARGET_OBSERVED_240M")
    if a60["n"] < MIN_ALLOWED_OUTCOMES or a240["n"] < MIN_ALLOWED_OUTCOMES:
        blockers.append("MIN_ALLOWED_OUTCOMES")
    if r60["n"] < MIN_REJECTED_OUTCOMES or r240["n"] < MIN_REJECTED_OUTCOMES:
        blockers.append("MIN_REJECTED_OUTCOMES")
    if sample_complete and not performance_ok:
        blockers.append("PERFORMANCE_CRITERIA")
    if sample_complete and not concentration_ok:
        blockers.append("CONCENTRATION_CRITERIA")

    if not sample_complete:
        status = "COLLECTING_PROSPECTIVE_OOS"
    elif performance_ok and concentration_ok:
        status = "READY_FOR_MANUAL_REVIEW"
    else:
        status = "EVIDENCE_FAIL"

    return {
        **AUTHORITY,
        "cohort_id": baseline["cohort_id"],
        "started_epoch": baseline["started_epoch"],
        "discovery_cutoff_epoch": baseline["discovery_cutoff_epoch"],
        "hypothesis": baseline["hypothesis"],
        "hypothesis_frozen": baseline["hypothesis_frozen"],
        "reset_allowed": baseline["reset_allowed"],
        "status": status,
        "blockers": tuple(blockers),
        "enrolled_candidates": len(records),
        "observed_60m": len(m60),
        "observed_240m": len(m240),
        "allowed_60m": a60,
        "rejected_60m": r60,
        "allowed_240m": a240,
        "rejected_240m": r240,
        "allowed_vs_rejected_mean_lift_60m": lift60,
        "allowed_vs_rejected_mean_lift_240m": lift240,
        "allowed_vs_rejected_positive_rate_lift_60m": pos_lift60,
        "allowed_vs_rejected_positive_rate_lift_240m": pos_lift240,
        "allowed_concentration": concentration,
        "sample_complete": sample_complete,
        "performance_criteria_pass": performance_ok if sample_complete else False,
        "concentration_criteria_pass": concentration_ok if sample_complete else False,
        "manual_review_required": True,
        "explicit_live_authorization_required": True,
        "interpretation_guard": (
            "PASS_MEANS_EVIDENCE_READY_FOR_MANUAL_REVIEW_NOT_LIVE_PERMISSION"
        ),
    }


async def snapshot(db, *, timing_probe=None):
    # Optional per-stage metrics only; no change to SQL, cohort membership or
    # trading authority. Normal callers retain the exact previous behavior.
    _meta_work = load_frozen_metadata_for_snapshot(db)
    if timing_probe is not None:
        # The label describes the selected mode, not a claim of server speed.
        timing_probe.metadata_read_mode = (
            "IMMUTABLE_READ_FIRST"
            if metadata_fast_path_enabled()
            else "LEGACY_DDL_GUARDED"
        )
    baseline = (
        await timing_probe.await_stage("metadata", _meta_work)
        if timing_probe is not None else await _meta_work
    )
    started = float(baseline["started_epoch"])

    _candidate_work = db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch",
        (POPULATION, started),
    )
    rows = (
        await timing_probe.await_stage("candidates", _candidate_work)
        if timing_probe is not None else await _candidate_work
    )
    _parse_started = timing_probe.clock() if timing_probe is not None else None
    payloads = []
    for item in rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[0]
        try:
            payloads.append(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if timing_probe is not None:
        timing_probe.stage_ms["compute"] += (
            timing_probe.clock() - _parse_started
        ) * 1000

    _outcome_work = db._fetchall(
        "SELECT o.candidate_id,o.horizon,o.payload "
        "FROM hard_gate_shadow_outcomes_v1 o "
        "JOIN hard_gate_shadow_candidates_v1 c ON c.candidate_id=o.candidate_id "
        "WHERE c.population=? AND c.captured_epoch>=? AND o.horizon IN (?,?)",
        (POPULATION, started, 60, 240),
    )
    out_rows = (
        await timing_probe.await_stage("outcomes", _outcome_work)
        if timing_probe is not None else await _outcome_work
    )
    _parse_started = timing_probe.clock() if timing_probe is not None else None
    out60, out240 = [], []
    for item in out_rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[2]
        cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
        horizon = int(item["horizon"] if hasattr(item, "keys") else item[1])
        try:
            obj = json.loads(raw)
            obj["candidate_id"] = cid
            (out60 if horizon == 60 else out240).append(obj)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if timing_probe is not None:
        timing_probe.stage_ms["compute"] += (
            timing_probe.clock() - _parse_started
        ) * 1000
        return timing_probe.compute(
            lambda: build_report(payloads, out60, out240, baseline=baseline)
        )
    return build_report(payloads, out60, out240, baseline=baseline)


def _fmt(value, digits=6):
    return "NA" if value is None else f"{float(value):.{digits}f}"


def format_summary(report):
    a60, r60 = report["allowed_60m"], report["rejected_60m"]
    a240, r240 = report["allowed_240m"], report["rejected_240m"]
    return (
        "[PROSPECTIVE_OOS_COHORT_V1] "
        f"cohort_id={report['cohort_id']} started_epoch={report['started_epoch']:.3f} "
        f"status={report['status']} blockers={','.join(report['blockers']) or 'NONE'} "
        f"enrolled={report['enrolled_candidates']}/{TARGET_CANDIDATES} "
        f"obs60={report['observed_60m']}/{TARGET_OBSERVED_60M} "
        f"obs240={report['observed_240m']}/{TARGET_OBSERVED_240M} "
        f"allowed60_n={a60['n']} allowed60_avg={_fmt(a60['avg_return'])} "
        f"rejected60_n={r60['n']} rejected60_avg={_fmt(r60['avg_return'])} "
        f"lift60={_fmt(report['allowed_vs_rejected_mean_lift_60m'])} "
        f"allowed240_n={a240['n']} allowed240_avg={_fmt(a240['avg_return'])} "
        f"rejected240_n={r240['n']} rejected240_avg={_fmt(r240['avg_return'])} "
        f"lift240={_fmt(report['allowed_vs_rejected_mean_lift_240m'])} "
        f"sample_complete={str(report['sample_complete']).lower()} "
        f"performance_pass={str(report['performance_criteria_pass']).lower()} "
        f"concentration_pass={str(report['concentration_criteria_pass']).lower()} "
        "hypothesis_frozen=true discovery_sample_excluded=true "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )


def format_concentration(report):
    parts = []
    for dim, row in report["allowed_concentration"].items():
        parts.append(
            f"{dim}={row['top_value']}:n={row['top_n']}/{row['allowed_total']}"
            f":share={_fmt(row['top_share'],4)}:distinct={row['distinct']}"
        )
    return (
        "[PROSPECTIVE_OOS_CONCENTRATION_V1] "
        + "|".join(parts)
        + f" max_allowed={MAX_ALLOWED_CONCENTRATION:.2f} "
        "live_allowed=false execution_effect=NONE"
    )
