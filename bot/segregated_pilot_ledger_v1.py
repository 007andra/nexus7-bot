"""SEGREGATED_PILOT_LEDGER_V1.

Append-only SHADOW ledger for a future segregated pilot. It enrolls only
prospective-OOS candidates that the unchanged counterfactual NEXUS would allow.
Each enrolled candidate reserves one immutable baseline risk unit in a 5R
research reference budget. Once exhausted, later candidates are recorded as
SHADOW_BUDGET_BLOCK.

This ledger is independent of the production trade/risk ledgers and has no
exchange, sizing, dispatch, recovery, HWM, drawdown or LIVE authority.
"""
from __future__ import annotations

import json
import math
import time

LEDGER_ID = "SEGREGATED_PILOT_SHADOW_V1"

AUTHORITY = {
    "research_only": True,
    "shadow_only": True,
    "append_only": True,
    "historical_loss_ledger_untouched": True,
    "historical_hwm_preserved": True,
    "lifetime_drawdown_preserved": True,
    "current_hard_gate_unchanged": True,
    "fund_movement_authorized": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}

_META = """CREATE TABLE IF NOT EXISTS segregated_pilot_ledger_v1 (
 ledger_id TEXT PRIMARY KEY,
 started_epoch REAL NOT NULL,
 payload TEXT NOT NULL
)"""
_ENTRIES = """CREATE TABLE IF NOT EXISTS segregated_pilot_ledger_entries_v1 (
 ledger_id TEXT NOT NULL,
 candidate_id TEXT NOT NULL,
 captured_epoch REAL NOT NULL,
 payload TEXT NOT NULL,
 PRIMARY KEY (ledger_id, candidate_id)
)"""


def _finite(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


async def ensure_ledger(db, readiness: dict, budget_study: dict, oos: dict):
    await db._exec(_META)
    await db._exec(_ENTRIES)
    rows = await db._fetchall(
        "SELECT payload FROM segregated_pilot_ledger_v1 WHERE ledger_id=?",
        (LEDGER_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)

    risk_unit = _finite(budget_study.get("risk_unit_usdt"))
    budget = _finite(budget_study.get("shadow_reference_budget_usdt"))
    if risk_unit is None or budget is None or risk_unit <= 0 or budget <= 0:
        raise ValueError("invalid shadow budget baseline")
    row = {
        **AUTHORITY,
        "ledger_id": LEDGER_ID,
        "started_epoch": time.time(),
        "oos_started_epoch": _finite(oos.get("started_epoch")),
        "baseline_equity": _finite(readiness.get("equity")),
        "baseline_peak_equity": _finite(readiness.get("peak_equity")),
        "baseline_drawdown": _finite(readiness.get("drawdown")),
        "baseline_drawdown_limit": _finite(readiness.get("configured_limit")),
        "baseline_risk_pct": _finite(readiness.get("max_risk_pct")),
        "risk_unit_usdt": risk_unit,
        "reference_budget_r": budget_study.get("shadow_reference_r"),
        "reference_budget_usdt": budget,
        "reset_allowed": False,
    }
    await db._exec(
        "INSERT INTO segregated_pilot_ledger_v1 (ledger_id,started_epoch,payload) "
        "VALUES (?,?,?) ON CONFLICT(ledger_id) DO NOTHING",
        (LEDGER_ID, row["started_epoch"], json.dumps(row, sort_keys=True)),
    )
    return row


async def snapshot(db, readiness: dict, budget_study: dict, oos: dict):
    base = await ensure_ledger(db, readiness, budget_study, oos)
    oos_start = float(base.get("oos_started_epoch") or oos.get("started_epoch") or 0.0)
    rows = await db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch",
        ("HARD_GATE_SHADOW", oos_start),
    )
    candidates = []
    for item in rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[0]
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        cf = obj.get("counterfactual_nexus_v1")
        cid = str(obj.get("candidate_id") or "")
        if (
            isinstance(cf, dict)
            and cid
            and str(cf.get("candidate_id") or "") == cid
            and cf.get("cohort") == "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS"
            and cf.get("risk_epoch_traversal_credit") is False
            and cf.get("execution_allowed") is True
            and obj.get("shadow_only") is True
            and obj.get("live_eligible") is False
        ):
            candidates.append(obj)

    existing_rows = await db._fetchall(
        "SELECT candidate_id,payload FROM segregated_pilot_ledger_entries_v1 "
        "WHERE ledger_id=? ORDER BY captured_epoch",
        (LEDGER_ID,),
    )
    entries = {}
    for item in existing_rows or []:
        cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
        raw = item["payload"] if hasattr(item, "keys") else item[1]
        try:
            entries[str(cid)] = json.loads(raw)
        except Exception:
            continue

    risk_unit = float(base["risk_unit_usdt"])
    budget = float(base["reference_budget_usdt"])
    reserved = sum(float(e.get("reserved_loss_usdt") or 0.0) for e in entries.values())

    for obj in candidates:
        cid = str(obj.get("candidate_id") or "")
        if not cid or cid in entries:
            continue
        remaining_before = max(0.0, budget - reserved)
        if remaining_before + 1e-12 >= risk_unit:
            status = "SHADOW_RESERVED"
            amount = risk_unit
        else:
            status = "SHADOW_BUDGET_BLOCK"
            amount = 0.0
        entry = {
            **AUTHORITY,
            "ledger_id": LEDGER_ID,
            "candidate_id": cid,
            "captured_epoch": float(obj.get("captured_epoch") or 0.0),
            "symbol": obj.get("symbol"),
            "side": obj.get("side"),
            "setup": obj.get("setup"),
            "status": status,
            "risk_unit_usdt": risk_unit,
            "reserved_loss_usdt": amount,
            "remaining_before_usdt": remaining_before,
            "remaining_after_usdt": max(0.0, remaining_before - amount),
            "production_order_created": False,
            "prospective_oos_cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
            "oos_enrollment_credit": False,
            "canonical_pipeline_credit": False,
        }
        await db._exec(
            "INSERT INTO segregated_pilot_ledger_entries_v1 "
            "(ledger_id,candidate_id,captured_epoch,payload) VALUES (?,?,?,?) "
            "ON CONFLICT(ledger_id,candidate_id) DO NOTHING",
            (LEDGER_ID, cid, entry["captured_epoch"], json.dumps(entry, sort_keys=True)),
        )
        entries[cid] = entry
        reserved += amount

    ordered = sorted(entries.values(), key=lambda e: (e.get("captured_epoch", 0), e.get("candidate_id", "")))
    blocked = sum(1 for e in ordered if e.get("status") == "SHADOW_BUDGET_BLOCK")
    reserved_n = sum(1 for e in ordered if e.get("status") == "SHADOW_RESERVED")
    remaining = max(0.0, budget - sum(float(e.get("reserved_loss_usdt") or 0.0) for e in ordered))
    return {
        **AUTHORITY,
        "ledger_id": LEDGER_ID,
        "status": "SHADOW_LEDGER_ACTIVE",
        "baseline": base,
        "reference_budget_usdt": budget,
        "risk_unit_usdt": risk_unit,
        "reserved_entries": reserved_n,
        "budget_blocked_entries": blocked,
        "total_entries": len(ordered),
        "reserved_loss_usdt": budget - remaining,
        "remaining_budget_usdt": remaining,
        "budget_guard_configured": True,
        "isolation_contract_active": True,
        "enrollment_scope": "EXACT_PROSPECTIVE_OOS_COHORT",
        "reset_allowed": False,
    }


def format_log(row: dict) -> str:
    return (
        "[SEGREGATED_PILOT_LEDGER_V1] "
        f"status={row.get('status')} ledger_id={row.get('ledger_id')} "
        f"reference_budget_usdt={row.get('reference_budget_usdt')} "
        f"risk_unit_usdt={row.get('risk_unit_usdt')} "
        f"reserved_entries={row.get('reserved_entries')} "
        f"budget_blocked_entries={row.get('budget_blocked_entries')} "
        f"total_entries={row.get('total_entries')} "
        f"reserved_loss_usdt={row.get('reserved_loss_usdt')} "
        f"remaining_budget_usdt={row.get('remaining_budget_usdt')} "
        "budget_guard_configured=true isolation_contract_active=true "
        "enrollment_scope=EXACT_PROSPECTIVE_OOS_COHORT "
        "historical_loss_ledger_untouched=true historical_hwm_preserved=true "
        "lifetime_drawdown_preserved=true current_hard_gate_unchanged=true "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE"
    )
