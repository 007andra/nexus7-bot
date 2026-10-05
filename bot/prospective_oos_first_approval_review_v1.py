"""PROSPECTIVE_OOS_FIRST_APPROVAL_REVIEW_V1.

Read-only lifecycle review for the first naturally approved candidate in the
immutable prospective OOS cohort.

The review never creates candidates, changes selection, accelerates collection,
mutates thresholds/risk/sizing, or authorizes LIVE execution. It only proves:
- the first approved candidate belongs to the exact frozen OOS cohort;
- its shadow-ledger enrollment is exact and isolated;
- its 60m/240m outcomes mature naturally;
- its realized return can be compared with the rejected OOS benchmark.
"""
from __future__ import annotations

import json
import math
import time

from bot import prospective_oos_cohort_v1 as oos
from bot import segregated_pilot_ledger_v1 as ledger

AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "research_only": True,
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
    "collection_acceleration_authorized": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}

STATUSES = (
    "WAITING_FOR_FIRST_APPROVAL",
    "FIRST_APPROVAL_CAPTURED_AWAITING_60M",
    "FIRST_APPROVAL_60M_OBSERVED_AWAITING_240M",
    "FIRST_APPROVAL_MATURED_240M",
    "FIRST_APPROVAL_AUDIT_FAIL",
)


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _candidate_record(raw):
    if not isinstance(raw, dict):
        return None
    cf = raw.get("counterfactual_nexus_v1")
    if not isinstance(cf, dict):
        return None
    if cf.get("cohort") != oos.COHORT:
        return None
    if cf.get("risk_epoch_traversal_credit") is not False:
        return None
    cid = str(raw.get("candidate_id") or "")
    cf_cid = str(cf.get("candidate_id") or "")
    captured_epoch = _finite(raw.get("captured_epoch"))
    table_cid = raw.get("_review_table_candidate_id")
    if not cid or cf_cid != cid or captured_epoch is None:
        return None
    if table_cid is not None and str(table_cid) != cid:
        return None
    if raw.get("shadow_only") is not True or raw.get("live_eligible") is not False:
        return None
    return {
        "candidate_id": cid,
        "captured_epoch": captured_epoch,
        "symbol": cf.get("symbol") or raw.get("symbol"),
        "side": cf.get("side") or raw.get("side"),
        "regime": cf.get("regime") or raw.get("regime"),
        "setup": cf.get("setup") or raw.get("setup"),
        "allowed": cf.get("execution_allowed") is True,
        "risk_reward": _finite(cf.get("risk_reward")),
        "expected_value": _finite(cf.get("expected_value")),
        "confidence": _finite(cf.get("confidence")),
    }


def _ceil_15m(epoch):
    return math.ceil(float(epoch) / 900.0) * 900.0


def _observed_outcome(raw, *, horizon, captured_epoch, now_epoch):
    """Validate one observed outcome independently of the producer.

    Existing UNKNOWN/cache-gap rows are simply not mature observations. A row
    claiming OBSERVED must prove its declared horizon, non-early observation
    start, finite metrics and elapsed wall-clock maturity before this review
    will consume it.
    """
    if not isinstance(raw, dict) or raw.get("outcome") != "OBSERVED":
        return None, None

    try:
        payload_horizon = int(raw.get("horizon"))
    except (TypeError, ValueError):
        payload_horizon = None
    if payload_horizon != int(horizon):
        return None, f"OUTCOME_{horizon}M_HORIZON_MISMATCH"

    obs_start = _finite(raw.get("observation_start"))
    captured = _finite(captured_epoch)
    ret = _finite(raw.get("future_return"))
    mfe = _finite(raw.get("MFE"))
    mae = _finite(raw.get("MAE"))
    if obs_start is None or captured is None or ret is None or mfe is None or mae is None:
        return None, f"OUTCOME_{horizon}M_MALFORMED"

    expected_start = _ceil_15m(captured)
    if obs_start + 1e-9 < expected_start:
        return None, f"OUTCOME_{horizon}M_EARLY_OBSERVATION_START"

    maturity_epoch = obs_start + float(horizon) * 60.0
    if float(now_epoch) + 1e-9 < maturity_epoch:
        return None, f"OUTCOME_{horizon}M_NOT_MATURE"

    return {
        "future_return": ret,
        "MFE": mfe,
        "MAE": mae,
        "observation_start": obs_start,
        "maturity_epoch": maturity_epoch,
    }, None


def evaluate(
    candidates,
    outcomes60,
    outcomes240,
    ledger_entries,
    oos_report,
    *,
    now_epoch=None,
):
    now = _finite(now_epoch)
    if now is None:
        now = time.time()
    records = []
    malformed_eligible = 0
    cohort_start = _finite(oos_report.get("started_epoch"))
    for raw in candidates:
        row = _candidate_record(raw)
        if row is None:
            # Non-OOS candidates are outside this review. Rows claiming the OOS
            # cohort but failing identity are counted fail-closed.
            cf = raw.get("counterfactual_nexus_v1") if isinstance(raw, dict) else None
            if isinstance(cf, dict) and cf.get("cohort") == oos.COHORT:
                malformed_eligible += 1
            continue
        # PostgreSQL REAL is single precision; the table timestamp is suitable
        # for coarse query pruning but not exact identity. Use the durable JSON
        # timestamp for the immutable prospective cutoff and maturation proof.
        if cohort_start is None or row["captured_epoch"] + 1e-9 < cohort_start:
            malformed_eligible += 1
            continue
        records.append(row)

    records.sort(key=lambda r: (
        r.get("captured_epoch") if r.get("captured_epoch") is not None else float("inf"),
        r["candidate_id"],
    ))
    approved = [r for r in records if r["allowed"]]

    base = {
        **AUTHORITY,
        "cohort_id": oos_report.get("cohort_id"),
        "enrolled_candidates": len(records),
        "approved_candidates": len(approved),
        "malformed_eligible_candidates": malformed_eligible,
        "first_approval_seen": bool(approved),
        "first_approval_candidate_id": None,
        "first_approval_symbol": None,
        "first_approval_side": None,
        "first_approval_regime": None,
        "first_approval_setup": None,
        "first_approval_captured_epoch": None,
        "ledger_entry_present": False,
        "ledger_status": None,
        "ledger_reserved_loss_usdt": None,
        "ledger_exact_scope": False,
        "outcome_60m_observed": False,
        "outcome_240m_observed": False,
        "first_approval_return_60m": None,
        "first_approval_return_240m": None,
        "rejected_avg_return_60m": (
            (oos_report.get("rejected_60m") or {}).get("avg_return")
        ),
        "rejected_avg_return_240m": (
            (oos_report.get("rejected_240m") or {}).get("avg_return")
        ),
        "first_vs_rejected_delta_60m": None,
        "first_vs_rejected_delta_240m": None,
        "audit_pass": False,
    }

    if malformed_eligible:
        return {
            **base,
            "status": "FIRST_APPROVAL_AUDIT_FAIL",
            "blockers": ("MALFORMED_OOS_IDENTITY",),
        }

    if not approved:
        return {
            **base,
            "status": "WAITING_FOR_FIRST_APPROVAL",
            "blockers": ("FIRST_NATURAL_APPROVAL_NOT_SEEN",),
            "audit_pass": True,
        }

    first = approved[0]
    cid = first["candidate_id"]
    le = ledger_entries.get(cid)
    ledger_present = isinstance(le, dict)
    ledger_risk_unit = _finite(le.get("risk_unit_usdt")) if ledger_present else None
    ledger_reserved = _finite(le.get("reserved_loss_usdt")) if ledger_present else None
    remaining_before = _finite(le.get("remaining_before_usdt")) if ledger_present else None
    remaining_after = _finite(le.get("remaining_after_usdt")) if ledger_present else None
    exact_one_r = bool(
        ledger_present
        and le.get("status") == "SHADOW_RESERVED"
        and ledger_risk_unit is not None
        and ledger_risk_unit > 0.0
        and ledger_reserved is not None
        and abs(ledger_reserved - ledger_risk_unit) <= 1e-12
        and remaining_before is not None
        and remaining_before + 1e-12 >= ledger_risk_unit
        and remaining_after is not None
        and abs(
            remaining_after - max(0.0, remaining_before - ledger_reserved)
        ) <= 1e-12
    )
    exact_scope = bool(
        ledger_present
        and le.get("ledger_id") == ledger.LEDGER_ID
        and le.get("prospective_oos_cohort") == oos.COHORT
        and le.get("research_only") is True
        and le.get("shadow_only") is True
        and le.get("oos_enrollment_credit") is False
        and le.get("canonical_pipeline_credit") is False
        and le.get("production_order_created") is False
        and le.get("live_allowed") is False
        and le.get("decision_effect") == "NONE"
        and le.get("execution_effect") == "NONE"
        and exact_one_r
    )

    o60, o60_violation = _observed_outcome(
        outcomes60.get(cid),
        horizon=60,
        captured_epoch=first.get("captured_epoch"),
        now_epoch=now,
    )
    o240, o240_violation = _observed_outcome(
        outcomes240.get(cid),
        horizon=240,
        captured_epoch=first.get("captured_epoch"),
        now_epoch=now,
    )
    rej60 = _finite(base["rejected_avg_return_60m"])
    rej240 = _finite(base["rejected_avg_return_240m"])

    row = {
        **base,
        "first_approval_candidate_id": cid,
        "first_approval_symbol": first.get("symbol"),
        "first_approval_side": first.get("side"),
        "first_approval_regime": first.get("regime"),
        "first_approval_setup": first.get("setup"),
        "first_approval_captured_epoch": first.get("captured_epoch"),
        "first_approval_risk_reward": first.get("risk_reward"),
        "first_approval_expected_value": first.get("expected_value"),
        "first_approval_confidence": first.get("confidence"),
        "ledger_entry_present": ledger_present,
        "ledger_status": le.get("status") if ledger_present else None,
        "ledger_risk_unit_usdt": ledger_risk_unit,
        "ledger_reserved_loss_usdt": ledger_reserved,
        "ledger_exact_one_r": exact_one_r,
        "ledger_remaining_before_usdt": remaining_before,
        "ledger_remaining_after_usdt": remaining_after,
        "ledger_exact_scope": exact_scope,
        "outcome_60m_observed": o60 is not None,
        "outcome_240m_observed": o240 is not None,
        "first_approval_return_60m": (
            o60.get("future_return") if o60 else None
        ),
        "first_approval_return_240m": (
            o240.get("future_return") if o240 else None
        ),
        "first_approval_mfe_60m": o60.get("MFE") if o60 else None,
        "first_approval_mae_60m": o60.get("MAE") if o60 else None,
        "first_approval_mfe_240m": o240.get("MFE") if o240 else None,
        "first_approval_mae_240m": o240.get("MAE") if o240 else None,
        "first_vs_rejected_delta_60m": (
            o60["future_return"] - rej60 if o60 and rej60 is not None else None
        ),
        "first_vs_rejected_delta_240m": (
            o240["future_return"] - rej240 if o240 and rej240 is not None else None
        ),
    }

    blockers = []
    if not ledger_present:
        blockers.append("FIRST_APPROVAL_NOT_IN_SHADOW_LEDGER")
    elif not exact_one_r:
        blockers.append("FIRST_APPROVAL_NOT_EXACTLY_1R_RESERVED")
    elif not exact_scope:
        blockers.append("FIRST_APPROVAL_LEDGER_SCOPE_INVALID")
    if o60_violation:
        blockers.append(o60_violation)
    if o240_violation:
        blockers.append(o240_violation)

    if blockers:
        return {
            **row,
            "status": "FIRST_APPROVAL_AUDIT_FAIL",
            "blockers": tuple(blockers),
            "audit_pass": False,
        }

    if o240 is not None and o60 is None:
        return {
            **row,
            "status": "FIRST_APPROVAL_AUDIT_FAIL",
            "blockers": ("OUTCOME_240M_WITHOUT_60M",),
            "audit_pass": False,
        }

    if o240 is not None:
        status = "FIRST_APPROVAL_MATURED_240M"
        blockers = ()
    elif o60 is not None:
        status = "FIRST_APPROVAL_60M_OBSERVED_AWAITING_240M"
        blockers = ("FIRST_APPROVAL_240M_NOT_MATURED",)
    else:
        status = "FIRST_APPROVAL_CAPTURED_AWAITING_60M"
        blockers = ("FIRST_APPROVAL_60M_NOT_MATURED",)

    return {
        **row,
        "status": status,
        "blockers": tuple(blockers),
        "audit_pass": True,
    }


async def snapshot(db, oos_report):
    start = float(oos_report.get("started_epoch") or 0.0)
    rows = await db._fetchall(
        "SELECT candidate_id,payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch,candidate_id",
        (oos.POPULATION, start),
    )
    candidates = []
    for item in rows or []:
        if hasattr(item, "keys"):
            table_cid = item["candidate_id"]
            raw = item["payload"]
        else:
            table_cid, raw = item[0], item[1]
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        obj["_review_table_candidate_id"] = table_cid
        candidates.append(obj)

    out_rows = await db._fetchall(
        "SELECT candidate_id,horizon,payload FROM hard_gate_shadow_outcomes_v1 "
        "WHERE population=? AND horizon IN (?,?) ORDER BY candidate_id,horizon",
        (oos.POPULATION, 60, 240),
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

    ledger_rows = await db._fetchall(
        "SELECT candidate_id,payload FROM segregated_pilot_ledger_entries_v1 "
        "WHERE ledger_id=? ORDER BY captured_epoch,candidate_id",
        (ledger.LEDGER_ID,),
    )
    entries = {}
    for item in ledger_rows or []:
        cid = str(item["candidate_id"] if hasattr(item, "keys") else item[0])
        raw = item["payload"] if hasattr(item, "keys") else item[1]
        try:
            entries[cid] = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    return evaluate(
        candidates,
        out60,
        out240,
        entries,
        oos_report,
        now_epoch=time.time(),
    )


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, (tuple, list)):
        return ",".join(map(str, value)) or "NONE"
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value).replace(" ", "_")


def format_log(row: dict) -> str:
    keys = (
        "status", "blockers", "cohort_id", "enrolled_candidates",
        "approved_candidates", "first_approval_seen",
        "first_approval_candidate_id", "first_approval_symbol",
        "first_approval_side", "first_approval_regime", "first_approval_setup",
        "ledger_entry_present", "ledger_status", "ledger_risk_unit_usdt",
        "ledger_reserved_loss_usdt", "ledger_exact_one_r",
        "ledger_remaining_before_usdt", "ledger_remaining_after_usdt",
        "ledger_exact_scope", "outcome_60m_observed", "outcome_240m_observed",
        "first_approval_return_60m", "rejected_avg_return_60m",
        "first_vs_rejected_delta_60m", "first_approval_return_240m",
        "rejected_avg_return_240m", "first_vs_rejected_delta_240m",
        "audit_pass", "research_only", "shadow_only",
        "association_not_causation", "candidate_generation_unchanged",
        "thresholds_unchanged", "risk_unchanged", "sizing_unchanged",
        "leverage_unchanged", "historical_hwm_preserved",
        "lifetime_drawdown_preserved", "current_hard_gate_unchanged",
        "collection_acceleration_authorized", "promotion_allowed",
        "live_allowed", "decision_effect", "execution_effect",
    )
    return "[PROSPECTIVE_OOS_FIRST_APPROVAL_REVIEW_V1] " + " ".join(
        f"{key}={_fmt(row.get(key))}" for key in keys
    )
