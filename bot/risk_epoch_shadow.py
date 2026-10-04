"""Persistent research-only cohort for a future NEXUS re-entry epoch.

This module never authorizes LIVE execution. It only aggregates the independent
HARD_GATE_SHADOW candidate/outcome tables while the lifetime drawdown hard gate
remains authoritative.

The epoch preserves:
- historical HWM and lifetime drawdown as immutable reference values;
- authenticated starting equity as a research baseline;
- unique candidate counts and production-equivalent funnel progression;
- observed 60m/240m gross hypothetical outcomes.

No simulated realized equity is claimed because the outcome dataset does not
preserve intrabar stop/target ordering or actual fills.
"""
from __future__ import annotations

import json
import math
import os
import time

from bot.hard_gate_shadow_context import AUTHORITY as HARD_GATE_AUTHORITY, POPULATION

EPOCH_AUTHORITY = {
    "shadow_only": True,
    "research_only": True,
    "live_eligible": False,
    "live_candidate": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}

_META = """CREATE TABLE IF NOT EXISTS risk_epoch_shadow_v1 (
 epoch_id TEXT PRIMARY KEY,
 started_epoch REAL NOT NULL,
 start_equity REAL NOT NULL,
 historical_hwm REAL NOT NULL,
 lifetime_drawdown REAL NOT NULL,
 payload TEXT NOT NULL
)"""

_CANDIDATES = "hard_gate_shadow_candidates_v1"
_OUTCOMES = "hard_gate_shadow_outcomes_v1"


def enabled() -> bool:
    return os.environ.get("RISK_EPOCH_SHADOW_V1", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def epoch_id() -> str:
    value = os.environ.get("RISK_EPOCH_SHADOW_ID", "REENTRY_V1").strip()
    return value or "REENTRY_V1"


def target_candidates() -> int:
    try:
        value = int(os.environ.get("RISK_EPOCH_SHADOW_TARGET_CANDIDATES", "20"))
    except (TypeError, ValueError):
        return 20
    return max(1, min(value, 10000))


def _loads(raw):
    if raw is None:
        return {}
    if hasattr(raw, "keys"):
        raw = raw["payload"]
    elif isinstance(raw, (tuple, list)):
        raw = raw[0]
    return json.loads(raw)


async def _ensure_epoch(db, engine, start_equity: float, *, started_epoch: float | None = None):
    await db._exec(_META)
    eid = epoch_id()
    rows = await db._fetchall(
        "SELECT payload FROM risk_epoch_shadow_v1 WHERE epoch_id=?",
        (eid,),
    )
    if rows:
        return _loads(rows[0])

    risk = getattr(engine, "risk", None)
    hwm = float(
        getattr(risk, "peak_equity", 0.0)
        or getattr(risk, "peak_balance", 0.0)
        or 0.0
    )
    dd = float(getattr(risk, "drawdown", float("nan")))
    if not all(math.isfinite(v) for v in (start_equity, hwm, dd)):
        raise ValueError("invalid risk epoch baseline")
    if start_equity <= 0 or hwm <= 0 or dd < 0:
        raise ValueError("invalid risk epoch baseline")

    row = {
        "epoch_id": eid,
        "started_epoch": float(time.time() if started_epoch is None else started_epoch),
        "start_equity": start_equity,
        "historical_hwm": hwm,
        "lifetime_drawdown": dd,
        "historical_hwm_immutable": True,
        "lifetime_drawdown_immutable": True,
        "hypothetical_realized_equity": None,
        "hypothetical_realized_equity_reason": "OUTCOME_PATH_ORDERING_UNAVAILABLE",
        **EPOCH_AUTHORITY,
    }
    await db._exec(
        "INSERT INTO risk_epoch_shadow_v1 "
        "(epoch_id,started_epoch,start_equity,historical_hwm,lifetime_drawdown,payload) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(epoch_id) DO NOTHING",
        (
            eid, row["started_epoch"], start_equity, hwm, dd,
            json.dumps(row, sort_keys=True, allow_nan=False),
        ),
    )
    rows = await db._fetchall(
        "SELECT payload FROM risk_epoch_shadow_v1 WHERE epoch_id=?",
        (eid,),
    )
    return _loads(rows[0]) if rows else row


async def ensure_epoch(db, engine, *, start_equity: float, started_epoch: float | None = None) -> dict:
    """Create/read the immutable epoch baseline before candidate enrollment."""
    return await _ensure_epoch(
        db, engine, float(start_equity), started_epoch=started_epoch
    )


def _candidate_flags(row: dict):
    capital_confirmed = row.get("capital_source") not in (None, "UNCONFIRMED")
    min_order = row.get("shadow_min_order_feasible") is True
    pullback = row.get("pullback_pass") is True
    funnel = row.get("production_equivalent_funnel_result") is True
    nexus_called = row.get("nexus_called") is True
    nexus_allowed = row.get("nexus_allowed") is True
    traversed_to_nexus = pullback and funnel and capital_confirmed and min_order and nexus_called
    return {
        "capital_confirmed": capital_confirmed,
        "min_order_feasible": min_order,
        "traversed_to_nexus": traversed_to_nexus,
        "nexus_allowed": nexus_allowed,
    }


async def snapshot(db, engine, *, start_equity: float) -> dict:
    if not enabled():
        return {"status": "DISABLED", **EPOCH_AUTHORITY}

    baseline = await _ensure_epoch(db, engine, float(start_equity))
    started = float(baseline["started_epoch"])
    await db._exec(
        """CREATE TABLE IF NOT EXISTS hard_gate_shadow_candidates_v1 (
         candidate_id TEXT PRIMARY KEY, captured_epoch REAL NOT NULL,
         symbol TEXT NOT NULL, population TEXT NOT NULL, payload TEXT NOT NULL
        )"""
    )
    await db._exec(
        """CREATE TABLE IF NOT EXISTS hard_gate_shadow_outcomes_v1 (
         candidate_id TEXT NOT NULL, horizon INTEGER NOT NULL,
         population TEXT NOT NULL, payload TEXT NOT NULL,
         PRIMARY KEY(candidate_id,horizon)
        )"""
    )
    rows = await db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch",
        (POPULATION, started),
    )

    candidates = [_loads(raw) for raw in (rows or [])]
    flags = [_candidate_flags(row) for row in candidates]
    ids = {row.get("candidate_id") for row in candidates if row.get("candidate_id")}
    target = target_candidates()

    outcomes = {}
    if ids:
        out_rows = await db._fetchall(
            "SELECT o.candidate_id,o.horizon,o.payload "
            "FROM hard_gate_shadow_outcomes_v1 o "
            "JOIN hard_gate_shadow_candidates_v1 c ON c.candidate_id=o.candidate_id "
            "WHERE o.population=? AND c.population=? AND c.captured_epoch>=?",
            (POPULATION, POPULATION, started),
        )
        for raw in out_rows or []:
            if hasattr(raw, "keys"):
                cid, horizon, payload = raw["candidate_id"], int(raw["horizon"]), raw["payload"]
            else:
                cid, horizon, payload = raw[0], int(raw[1]), raw[2]
            if cid not in ids:
                continue
            row = json.loads(payload)
            row["candidate_id"] = cid
            if row.get("outcome") == "OBSERVED":
                outcomes.setdefault(horizon, []).append(row)

    observed60 = outcomes.get(60, [])
    observed240 = outcomes.get(240, [])
    approved_ids = {
        row.get("candidate_id") for row, f in zip(candidates, flags)
        if f["nexus_allowed"] and row.get("candidate_id")
    }
    approved60 = [
        row for row in observed60
        if row.get("candidate_id") in approved_ids
    ]

    def avg(rows, key):
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    unique_count = len(ids)
    traversed_count = sum(f["traversed_to_nexus"] for f in flags)
    observed_60m_count = len(observed60)
    if unique_count < target:
        status = "COLLECTING"
    elif traversed_count < target:
        status = "CANDIDATE_SAMPLE_COMPLETE_PIPELINE_BLOCKED"
    elif observed_60m_count < target:
        status = "PIPELINE_SAMPLE_COMPLETE_OUTCOMES_PENDING"
    else:
        status = "EVIDENCE_SAMPLE_COMPLETE_MANUAL_REVIEW_ONLY"
    return {
        **baseline,
        "status": status,
        "target_candidates": target,
        "unique_candidates": unique_count,
        "capital_confirmed_candidates": sum(f["capital_confirmed"] for f in flags),
        "min_order_feasible_candidates": sum(f["min_order_feasible"] for f in flags),
        "traversed_to_nexus": traversed_count,
        "nexus_allowed": sum(f["nexus_allowed"] for f in flags),
        "observed_60m": observed_60m_count,
        "observed_240m": len(observed240),
        "approved_observed_60m": len(approved60),
        "approved_60m_avg_gross_return": avg(approved60, "future_return"),
        "approved_60m_avg_mfe": avg(approved60, "MFE"),
        "approved_60m_avg_mae": avg(approved60, "MAE"),
        "promotion_allowed": False,
        "promotion_reason": "RESEARCH_EVIDENCE_NEVER_AUTHORIZES_LIVE",
        **EPOCH_AUTHORITY,
    }


def format_log(row: dict) -> str:
    def fmt(v):
        if isinstance(v, bool):
            return str(v).lower()
        if v is None:
            return "NA"
        if isinstance(v, float):
            return f"{v:.12g}"
        return str(v).replace(" ", "_")
    keys = (
        "epoch_id", "status", "start_equity", "historical_hwm", "lifetime_drawdown",
        "target_candidates", "unique_candidates", "capital_confirmed_candidates",
        "min_order_feasible_candidates", "traversed_to_nexus", "nexus_allowed",
        "observed_60m", "observed_240m", "approved_observed_60m",
        "approved_60m_avg_gross_return", "approved_60m_avg_mfe",
        "approved_60m_avg_mae", "promotion_allowed", "promotion_reason",
        "decision_effect", "execution_effect",
    )
    return "[RISK_EPOCH_SHADOW_V1] " + " ".join(
        f"{key}={fmt(row.get(key))}" for key in keys
    )
