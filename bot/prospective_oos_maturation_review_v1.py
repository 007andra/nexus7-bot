"""Prospective OOS maturation and approved-vs-rejected review V1.

Read-only research observability over the immutable prospective OOS cohort.
This module never participates in candidate generation, NEXUS decisions, sizing,
risk, dispatch, recovery, HWM/drawdown accounting or LIVE authorization.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import time

from bot import prospective_oos_cohort_v1 as oos
from bot import segregated_pilot_ledger_v1 as ledger

FLAG = "PROSPECTIVE_OOS_MATURATION_REVIEW_V1"

AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "research_only": True,
    "observability_only": True,
    "read_only": True,
    "shadow_only": True,
    "prospective_only": True,
    "association_not_causation": True,
    "candidate_generation_unchanged": True,
    "thresholds_unchanged": True,
    "risk_unchanged": True,
    "sizing_unchanged": True,
    "leverage_unchanged": True,
    "historical_hwm_preserved": True,
    "lifetime_drawdown_preserved": True,
    "current_hard_gate_unchanged": True,
    "frozen_hypothesis_unchanged": True,
    "cohort_start_unchanged": True,
    "forced_diversity": False,
    "distribution_manipulation": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}

MIN_APPROVED_N = oos.MIN_ALLOWED_OUTCOMES
MIN_REJECTED_N = oos.MIN_REJECTED_OUTCOMES


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _mean(values):
    xs = [float(v) for v in values if _finite(v) is not None]
    return sum(xs) / len(xs) if xs else None


def _rate(num, den):
    return float(num) / float(den) if den else None


def _ceil_15m(epoch):
    return math.ceil(float(epoch) / 900.0) * 900.0


def _candidate(raw, *, started_epoch):
    if not isinstance(raw, dict):
        return None, "MALFORMED_PAYLOAD"
    cf = raw.get("counterfactual_nexus_v1")
    if not isinstance(cf, dict) or cf.get("cohort") != oos.COHORT:
        return None, "NON_OOS"
    cid = str(raw.get("candidate_id") or "")
    cf_cid = str(cf.get("candidate_id") or "")
    table_cid = raw.get("_review_table_candidate_id")
    captured = _finite(raw.get("captured_epoch"))
    if (
        not cid
        or cf_cid != cid
        or table_cid is not None and str(table_cid) != cid
        or captured is None
    ):
        return None, "IDENTITY"
    if captured + 1e-9 < float(started_epoch):
        return None, "BEFORE_CUTOFF"
    if (
        cf.get("risk_epoch_traversal_credit") is not False
        or raw.get("shadow_only") is not True
        or raw.get("live_eligible") is not False
    ):
        return None, "AUTHORITY"
    return {
        "candidate_id": cid,
        "symbol": cf.get("symbol") or raw.get("symbol"),
        "side": cf.get("side") or raw.get("side"),
        "regime": cf.get("regime") or raw.get("regime"),
        "setup": cf.get("setup") or raw.get("setup"),
        "captured_epoch": captured,
        "allowed": cf.get("execution_allowed") is True,
        "approval_state": (
            "NATURAL_COUNTERFACTUAL_NEXUS_APPROVED"
            if cf.get("execution_allowed") is True
            else "NATURAL_COUNTERFACTUAL_NEXUS_REJECTED"
        ),
    }, None


def _observed_outcome(raw, *, candidate_id, horizon, captured_epoch, now_epoch):
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, f"OUTCOME_{horizon}M_MALFORMED"
    if raw.get("outcome") != "OBSERVED":
        return None, None

    payload_cid = str(raw.get("candidate_id") or candidate_id)
    if payload_cid != candidate_id:
        return None, f"OUTCOME_{horizon}M_CANDIDATE_ID_MISMATCH"
    try:
        payload_horizon = int(raw.get("horizon"))
    except (TypeError, ValueError):
        payload_horizon = None
    if payload_horizon != int(horizon):
        return None, f"OUTCOME_{horizon}M_HORIZON_MISMATCH"

    obs_start = _finite(raw.get("observation_start"))
    ret = _finite(raw.get("future_return"))
    mfe = _finite(raw.get("MFE"))
    mae = _finite(raw.get("MAE"))
    if None in (obs_start, ret, mfe, mae):
        return None, f"OUTCOME_{horizon}M_MALFORMED"

    expected_start = _ceil_15m(captured_epoch)
    if obs_start + 1e-9 < expected_start:
        return None, f"OUTCOME_{horizon}M_EARLY_OBSERVATION_START"
    maturity_epoch = obs_start + float(horizon) * 60.0
    if float(now_epoch) + 1e-9 < maturity_epoch:
        return None, f"OUTCOME_{horizon}M_IMMATURE_OBSERVED"

    return {
        "future_return": ret,
        "MFE": mfe,
        "MAE": mae,
        "observation_start": obs_start,
        "maturity_epoch": maturity_epoch,
    }, None


def _ledger_state(entry, *, candidate_id):
    if not isinstance(entry, dict):
        return None, "APPROVED_LEDGER_ENTRY_MISSING"
    risk_unit = _finite(entry.get("risk_unit_usdt"))
    reserved = _finite(entry.get("reserved_loss_usdt"))
    before = _finite(entry.get("remaining_before_usdt"))
    after = _finite(entry.get("remaining_after_usdt"))
    identity_ok = (
        entry.get("ledger_id") == ledger.LEDGER_ID
        and str(entry.get("candidate_id") or "") == candidate_id
        and entry.get("prospective_oos_cohort") == oos.COHORT
    )
    authority_ok = (
        entry.get("research_only") is True
        and entry.get("shadow_only") is True
        and entry.get("production_order_created") is False
        and entry.get("oos_enrollment_credit") is False
        and entry.get("canonical_pipeline_credit") is False
        and entry.get("live_allowed") is False
        and entry.get("decision_effect") == "NONE"
        and entry.get("execution_effect") == "NONE"
    )
    if (
        not identity_ok
        or not authority_ok
        or risk_unit is None
        or risk_unit <= 0.0
        or reserved is None
        or before is None
        or after is None
    ):
        return None, "APPROVED_LEDGER_SCOPE_INVALID"

    status = entry.get("status")
    if status == "SHADOW_RESERVED":
        exact_one_r = (
            abs(reserved - risk_unit) <= 1e-12
            and before + 1e-12 >= risk_unit
            and abs(after - max(0.0, before - reserved)) <= 1e-12
        )
        if not exact_one_r:
            return None, "APPROVED_LEDGER_1R_INVALID"
        return {
            "status": status,
            "risk_unit_usdt": risk_unit,
            "reserved_loss_usdt": reserved,
            "remaining_before_usdt": before,
            "remaining_after_usdt": after,
            "exact_one_r": True,
            "budget_blocked": False,
        }, None

    if status == "SHADOW_BUDGET_BLOCK":
        blocked_valid = (
            abs(reserved) <= 1e-12
            and before + 1e-12 < risk_unit
            and abs(after - before) <= 1e-12
        )
        if not blocked_valid:
            return None, "APPROVED_LEDGER_BUDGET_BLOCK_INVALID"
        return {
            "status": status,
            "risk_unit_usdt": risk_unit,
            "reserved_loss_usdt": reserved,
            "remaining_before_usdt": before,
            "remaining_after_usdt": after,
            "exact_one_r": False,
            "budget_blocked": True,
        }, None

    return None, "APPROVED_LEDGER_STATUS_INVALID"


def _performance(rows):
    if not rows:
        return {
            "n": 0,
            "avg_return": None,
            "positive_rate": None,
            "avg_mfe": None,
            "avg_mae": None,
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


def _lift(approved, rejected, key):
    a, r = approved.get(key), rejected.get(key)
    if a is None or r is None:
        return None
    return float(a) - float(r)


def _concentration(approved):
    result = {}
    for dim in ("symbol", "side", "regime", "setup"):
        counts = defaultdict(int)
        for row in approved:
            counts[str(row.get(dim) or "UNKNOWN")] += 1
        ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
        top_value, top_n = ("NONE", 0)
        if ordered:
            top_value, top_n = next(iter(ordered.items()))
        result[dim] = {
            "counts": ordered,
            "distinct": len(ordered),
            "top_value": top_value,
            "top_n": top_n,
            "top_share": _rate(top_n, len(approved)),
            "approved_total": len(approved),
        }
    return result


def evaluate(
    candidates,
    outcomes60,
    outcomes240,
    ledger_entries,
    cohort_meta,
    *,
    now_epoch=None,
):
    now = _finite(now_epoch)
    if now is None:
        now = time.time()
    started = _finite(cohort_meta.get("started_epoch"))
    metadata_ok = bool(
        cohort_meta.get("cohort_id") == oos.COHORT_ID
        and started is not None
        and cohort_meta.get("hypothesis") == oos.FROZEN_HYPOTHESIS
        and cohort_meta.get("hypothesis_frozen") is True
        and cohort_meta.get("reset_allowed") is False
    )

    violations = []
    seen = set()
    records = []
    duplicate_candidate_ids = 0
    excluded_non_oos = 0
    historical_excluded = 0
    malformed_exact_oos = 0

    if not metadata_ok:
        violations.append("COHORT_METADATA_NOT_FROZEN")
        started = started or 0.0

    for raw in candidates:
        row, reason = _candidate(raw, started_epoch=started)
        if row is None:
            if reason == "NON_OOS":
                excluded_non_oos += 1
            elif reason == "BEFORE_CUTOFF":
                historical_excluded += 1
            else:
                cf = raw.get("counterfactual_nexus_v1") if isinstance(raw, dict) else None
                if isinstance(cf, dict) and cf.get("cohort") == oos.COHORT:
                    malformed_exact_oos += 1
            continue
        cid = row["candidate_id"]
        if cid in seen:
            duplicate_candidate_ids += 1
            continue
        seen.add(cid)
        records.append(row)

    if duplicate_candidate_ids:
        violations.append("DUPLICATE_CANDIDATE_ID")
    if malformed_exact_oos:
        violations.append("MALFORMED_EXACT_OOS_CANDIDATE")

    records.sort(key=lambda r: (r["captured_epoch"], r["candidate_id"]))
    approved = [r for r in records if r["allowed"]]
    rejected = [r for r in records if not r["allowed"]]
    approved_ids = {r["candidate_id"] for r in approved}

    duplicate_ledger_ids = 0
    seen_ledger = set()
    normalized_ledger = {}
    for cid, entry in ledger_entries.items():
        key = str(cid)
        if key in seen_ledger:
            duplicate_ledger_ids += 1
            continue
        seen_ledger.add(key)
        normalized_ledger[key] = entry
    if duplicate_ledger_ids:
        violations.append("DUPLICATE_LEDGER_CANDIDATE_ID")

    unexpected_ledger = [
        cid for cid in normalized_ledger
        if cid not in approved_ids
    ]
    if unexpected_ledger:
        violations.append("LEDGER_OUTSIDE_APPROVED_PROSPECTIVE_SCOPE")

    a60_rows, r60_rows, a240_rows, r240_rows = [], [], [], []
    details = []
    approved_ledger_states = {}
    for row in records:
        cid = row["candidate_id"]
        o60, v60 = _observed_outcome(
            outcomes60.get(cid),
            candidate_id=cid,
            horizon=60,
            captured_epoch=row["captured_epoch"],
            now_epoch=now,
        )
        o240, v240 = _observed_outcome(
            outcomes240.get(cid),
            candidate_id=cid,
            horizon=240,
            captured_epoch=row["captured_epoch"],
            now_epoch=now,
        )
        if v60:
            violations.append(v60)
        if v240:
            violations.append(v240)
        if o240 is not None and o60 is None:
            violations.append("OUTCOME_240M_WITHOUT_60M")

        if o60 is not None:
            (a60_rows if row["allowed"] else r60_rows).append({**row, **o60})
        if o240 is not None:
            (a240_rows if row["allowed"] else r240_rows).append({**row, **o240})

        if not row["allowed"]:
            continue

        lstate, lv = _ledger_state(normalized_ledger.get(cid), candidate_id=cid)
        if lv:
            violations.append(lv)
        if lstate is not None:
            approved_ledger_states[cid] = lstate
        details.append({
            "candidate_id": cid,
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "regime": row.get("regime"),
            "setup": row.get("setup"),
            "captured_epoch": row.get("captured_epoch"),
            "approval_state": row.get("approval_state"),
            "shadow_ledger_status": lstate.get("status") if lstate else None,
            "risk_unit_usdt": lstate.get("risk_unit_usdt") if lstate else None,
            "reserved_loss_usdt": lstate.get("reserved_loss_usdt") if lstate else None,
            "remaining_pilot_budget_usdt": (
                lstate.get("remaining_after_usdt") if lstate else None
            ),
            "ledger_exact_one_r": lstate.get("exact_one_r") if lstate else False,
            "ledger_budget_blocked": lstate.get("budget_blocked") if lstate else False,
            "return_60m": o60.get("future_return") if o60 else None,
            "mfe_60m": o60.get("MFE") if o60 else None,
            "mae_60m": o60.get("MAE") if o60 else None,
            "return_240m": o240.get("future_return") if o240 else None,
            "mfe_240m": o240.get("MFE") if o240 else None,
            "mae_240m": o240.get("MAE") if o240 else None,
        })

    # Prove cumulative budget arithmetic independent of insertion/timestamp
    # ordering. Reserved entries must form a complete 1R descending sequence
    # from the largest remaining_before; blocked entries may only exist below 1R.
    ledger_states = list(approved_ledger_states.values())
    risk_units = [x["risk_unit_usdt"] for x in ledger_states]
    reference_budget = (
        max((x["remaining_before_usdt"] for x in ledger_states), default=None)
    )
    reserved_states = [x for x in ledger_states if x["status"] == "SHADOW_RESERVED"]
    blocked_states = [x for x in ledger_states if x["status"] == "SHADOW_BUDGET_BLOCK"]
    ledger_total_reserved = sum(x["reserved_loss_usdt"] for x in reserved_states)
    ledger_remaining = (
        min((x["remaining_after_usdt"] for x in ledger_states), default=None)
    )
    if risk_units:
        base_risk = risk_units[0]
        if any(abs(x - base_risk) > 1e-12 for x in risk_units[1:]):
            violations.append("LEDGER_RISK_UNIT_DRIFT")
        ordered_before = sorted(
            (x["remaining_before_usdt"] for x in reserved_states),
            reverse=True,
        )
        if reference_budget is not None:
            for i, actual in enumerate(ordered_before):
                expected = max(0.0, reference_budget - i * base_risk)
                if abs(actual - expected) > 1e-9:
                    violations.append("LEDGER_REMAINING_SEQUENCE_INVALID")
                    break
            if ledger_total_reserved > reference_budget + 1e-9:
                violations.append("LEDGER_BUDGET_EXCEEDED")
            final_expected = max(
                0.0, reference_budget - len(reserved_states) * base_risk
            )
            for state in blocked_states:
                if (
                    state["remaining_before_usdt"] + 1e-9 >= base_risk
                    or abs(state["remaining_after_usdt"] - final_expected) > 1e-9
                ):
                    violations.append("LEDGER_BUDGET_BLOCK_SEQUENCE_INVALID")
                    break

    # Deterministic order and unique blocker names keep telemetry idempotent.
    violations = tuple(dict.fromkeys(violations))
    pa60, pr60 = _performance(a60_rows), _performance(r60_rows)
    pa240, pr240 = _performance(a240_rows), _performance(r240_rows)

    evidence_available = bool(
        pa60["n"] >= MIN_APPROVED_N
        and pa240["n"] >= MIN_APPROVED_N
        and pr60["n"] >= MIN_REJECTED_N
        and pr240["n"] >= MIN_REJECTED_N
    )
    first_seen = bool(approved)
    first_60 = bool(a60_rows)
    first_240 = bool(a240_rows)
    small_n = bool(first_seen and not evidence_available)

    markers = []
    if first_seen:
        markers.append("FIRST_APPROVAL_OBSERVED")
    if first_60:
        markers.append("FIRST_APPROVAL_60M_MATURED")
    if first_240:
        markers.append("FIRST_APPROVAL_240M_MATURED")
    if small_n:
        markers.append("APPROVED_SAMPLE_SMALL_N")
    if evidence_available:
        markers.append("APPROVED_VS_REJECTED_EVIDENCE_AVAILABLE")

    if violations:
        status = "AUDIT_FAIL_CLOSED"
    elif not first_seen:
        status = "WAITING_FOR_PROSPECTIVE_APPROVAL"
    elif evidence_available:
        status = "APPROVED_VS_REJECTED_EVIDENCE_AVAILABLE"
    elif first_240:
        status = "APPROVED_SAMPLE_SMALL_N"
    elif first_60:
        status = "FIRST_APPROVAL_60M_MATURED"
    else:
        status = "FIRST_APPROVAL_OBSERVED"

    return {
        **AUTHORITY,
        "status": status,
        "markers": tuple(markers),
        "audit_pass": not violations,
        "blockers": violations,
        "cohort_id": cohort_meta.get("cohort_id"),
        "started_epoch": started,
        "metadata_frozen": metadata_ok,
        "enrolled_candidates": len(records),
        "approved_candidates": len(approved),
        "rejected_candidates": len(rejected),
        "excluded_non_oos": excluded_non_oos,
        "historical_excluded": historical_excluded,
        "malformed_exact_oos": malformed_exact_oos,
        "duplicate_candidate_ids": duplicate_candidate_ids,
        "duplicate_ledger_ids": duplicate_ledger_ids,
        "ledger_reference_budget_usdt": reference_budget,
        "ledger_risk_unit_usdt": risk_units[0] if risk_units else None,
        "ledger_reserved_entries": len(reserved_states),
        "ledger_budget_blocked_entries": len(blocked_states),
        "ledger_total_reserved_loss_usdt": ledger_total_reserved,
        "ledger_remaining_budget_usdt": ledger_remaining,
        "approved_candidate_details": tuple(details),
        "approved_60m": pa60,
        "rejected_60m": pr60,
        "approved_240m": pa240,
        "rejected_240m": pr240,
        "mean_return_lift_60m": _lift(pa60, pr60, "avg_return"),
        "positive_rate_lift_60m": _lift(pa60, pr60, "positive_rate"),
        "mfe_lift_60m": _lift(pa60, pr60, "avg_mfe"),
        "mae_lift_60m": _lift(pa60, pr60, "avg_mae"),
        "mean_return_lift_240m": _lift(pa240, pr240, "avg_return"),
        "positive_rate_lift_240m": _lift(pa240, pr240, "positive_rate"),
        "mfe_lift_240m": _lift(pa240, pr240, "avg_mfe"),
        "mae_lift_240m": _lift(pa240, pr240, "avg_mae"),
        "approved_concentration": _concentration(approved),
        "minimum_approved_n": MIN_APPROVED_N,
        "minimum_rejected_n": MIN_REJECTED_N,
        "approved_sample_small_n": small_n,
        "evidence_available": evidence_available,
        "interpretation_guard": (
            "SMALL_N_IS_DESCRIPTIVE_ONLY_ASSOCIATION_NOT_CAUSATION_NO_PROMOTION"
        ),
    }


async def snapshot(db, oos_report):
    started = _finite(oos_report.get("started_epoch"))
    if started is None:
        raise ValueError("prospective OOS started_epoch required")

    meta_rows = await db._fetchall(
        "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=?",
        (oos.COHORT_ID,),
    )
    if not meta_rows:
        cohort_meta = dict(oos_report)
    else:
        raw = meta_rows[0]["payload"] if hasattr(meta_rows[0], "keys") else meta_rows[0][0]
        cohort_meta = json.loads(raw)

    # Read all shadow candidates and apply the exact prospective cutoff using
    # the precise JSON epoch. PostgreSQL REAL is only coarse storage/query data.
    rows = await db._fetchall(
        "SELECT candidate_id,payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? ORDER BY captured_epoch,candidate_id",
        (oos.POPULATION,),
    )
    candidates = []
    for item in rows or []:
        table_cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
        raw = item["payload"] if hasattr(item, "keys") else item[1]
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        obj["_review_table_candidate_id"] = table_cid
        candidates.append(obj)

    out_rows = await db._fetchall(
        "SELECT candidate_id,horizon,payload FROM hard_gate_shadow_outcomes_v1 "
        "WHERE population=? AND horizon IN (60,240)",
        (oos.POPULATION,),
    )
    out60, out240 = {}, {}
    for item in out_rows or []:
        if hasattr(item, "keys"):
            cid, horizon, raw = item["candidate_id"], item["horizon"], item["payload"]
        else:
            cid, horizon, raw = item[0], item[1], item[2]
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        obj.setdefault("candidate_id", str(cid))
        if int(horizon) == 60:
            out60[str(cid)] = obj
        elif int(horizon) == 240:
            out240[str(cid)] = obj

    entry_rows = await db._fetchall(
        "SELECT candidate_id,payload FROM segregated_pilot_ledger_entries_v1 "
        "WHERE ledger_id=? ORDER BY captured_epoch,candidate_id",
        (ledger.LEDGER_ID,),
    )
    entries = {}
    duplicate_entry_rows = 0
    for item in entry_rows or []:
        cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
        raw = item["payload"] if hasattr(item, "keys") else item[1]
        key = str(cid)
        if key in entries:
            duplicate_entry_rows += 1
            continue
        try:
            entries[key] = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    report = evaluate(
        candidates,
        out60,
        out240,
        entries,
        cohort_meta,
        now_epoch=time.time(),
    )
    if duplicate_entry_rows:
        blockers = list(report["blockers"])
        blockers.append("DUPLICATE_LEDGER_DB_ROWS")
        report["blockers"] = tuple(dict.fromkeys(blockers))
        report["duplicate_ledger_ids"] += duplicate_entry_rows
        report["audit_pass"] = False
        report["status"] = "AUDIT_FAIL_CLOSED"
    return report


def _fmt(value, digits=8):
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (tuple, list)):
        return ",".join(str(x) for x in value) or "NONE"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value).replace(" ", "_")


def format_log(row):
    a60, r60 = row["approved_60m"], row["rejected_60m"]
    a240, r240 = row["approved_240m"], row["rejected_240m"]
    return (
        f"[{FLAG}] status={row['status']} markers={_fmt(row['markers'])} "
        f"blockers={_fmt(row['blockers'])} audit_pass={_fmt(row['audit_pass'])} "
        f"cohort_id={row['cohort_id']} enrolled={row['enrolled_candidates']} "
        f"approved={row['approved_candidates']} rejected={row['rejected_candidates']} "
        f"ledger_reserved={row['ledger_reserved_entries']} "
        f"ledger_blocked={row['ledger_budget_blocked_entries']} "
        f"ledger_reserved_loss={_fmt(row['ledger_total_reserved_loss_usdt'])} "
        f"ledger_remaining={_fmt(row['ledger_remaining_budget_usdt'])} "
        f"approved60_n={a60['n']} rejected60_n={r60['n']} "
        f"approved60_avg={_fmt(a60['avg_return'])} rejected60_avg={_fmt(r60['avg_return'])} "
        f"approved60_pos={_fmt(a60['positive_rate'])} rejected60_pos={_fmt(r60['positive_rate'])} "
        f"approved60_mfe={_fmt(a60['avg_mfe'])} rejected60_mfe={_fmt(r60['avg_mfe'])} "
        f"approved60_mae={_fmt(a60['avg_mae'])} rejected60_mae={_fmt(r60['avg_mae'])} "
        f"mean_lift60={_fmt(row['mean_return_lift_60m'])} "
        f"positive_lift60={_fmt(row['positive_rate_lift_60m'])} "
        f"mfe_lift60={_fmt(row['mfe_lift_60m'])} "
        f"mae_lift60={_fmt(row['mae_lift_60m'])} "
        f"approved240_n={a240['n']} rejected240_n={r240['n']} "
        f"approved240_avg={_fmt(a240['avg_return'])} rejected240_avg={_fmt(r240['avg_return'])} "
        f"approved240_pos={_fmt(a240['positive_rate'])} rejected240_pos={_fmt(r240['positive_rate'])} "
        f"approved240_mfe={_fmt(a240['avg_mfe'])} rejected240_mfe={_fmt(r240['avg_mfe'])} "
        f"approved240_mae={_fmt(a240['avg_mae'])} rejected240_mae={_fmt(r240['avg_mae'])} "
        f"mean_lift240={_fmt(row['mean_return_lift_240m'])} "
        f"positive_lift240={_fmt(row['positive_rate_lift_240m'])} "
        f"mfe_lift240={_fmt(row['mfe_lift_240m'])} "
        f"mae_lift240={_fmt(row['mae_lift_240m'])} "
        f"small_n={_fmt(row['approved_sample_small_n'])} "
        f"evidence_available={_fmt(row['evidence_available'])} "
        "association_not_causation=true candidate_generation_unchanged=true "
        "thresholds_unchanged=true risk_unchanged=true sizing_unchanged=true "
        "leverage_unchanged=true historical_hwm_preserved=true "
        "lifetime_drawdown_preserved=true current_hard_gate_unchanged=true "
        "automatic_promotion=false promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


def format_candidate_rows(row):
    lines = []
    for c in row["approved_candidate_details"]:
        lines.append(
            "[PROSPECTIVE_OOS_APPROVED_CANDIDATE_V1] "
            f"candidate_id={c['candidate_id']} symbol={c['symbol']} side={c['side']} "
            f"regime={c['regime']} setup={c['setup']} "
            f"captured_epoch={_fmt(c['captured_epoch'])} "
            f"approval_state={c['approval_state']} "
            f"ledger_status={_fmt(c['shadow_ledger_status'])} "
            f"risk_unit_usdt={_fmt(c['risk_unit_usdt'])} "
            f"reserved_loss_usdt={_fmt(c['reserved_loss_usdt'])} "
            f"remaining_pilot_budget_usdt={_fmt(c['remaining_pilot_budget_usdt'])} "
            f"ledger_exact_one_r={_fmt(c['ledger_exact_one_r'])} "
            f"ledger_budget_blocked={_fmt(c['ledger_budget_blocked'])} "
            f"return60={_fmt(c['return_60m'])} mfe60={_fmt(c['mfe_60m'])} "
            f"mae60={_fmt(c['mae_60m'])} return240={_fmt(c['return_240m'])} "
            f"mfe240={_fmt(c['mfe_240m'])} mae240={_fmt(c['mae_240m'])} "
            "small_n_guard=true association_not_causation=true "
            "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
        )
    return tuple(lines)


def format_concentration(row):
    parts = []
    for dim, data in row["approved_concentration"].items():
        counts = ",".join(f"{k}:{v}" for k, v in data["counts"].items()) or "NONE"
        parts.append(
            f"{dim}={counts}:top={data['top_value']}:{data['top_n']}/{data['approved_total']}"
            f":share={_fmt(data['top_share'])}"
        )
    return (
        "[PROSPECTIVE_OOS_APPROVED_CONCENTRATION_V1] "
        + " | ".join(parts)
        + " forced_diversity=false distribution_manipulation=false "
        "association_not_causation=true execution_effect=NONE"
    )
