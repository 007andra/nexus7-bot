"""Prospective Champion × Challenger Forward Test v1.

Hypothesis
----------
After a candidate has reached the final NEXUS score stage and already passed
positive expected value plus the configured minimum net R:R, does the final
NEXUS score threshold add incremental edge?

Champion:
    The production NEXUS decision exactly as returned by the runtime.

Challenger v1 ("economic-only"):
    Approves every candidate that demonstrably reached the final score stage,
    has EV > 0 and net R:R >= the same configured NEXUS floor, while ignoring
    only the final NEXUS score cut.

This is intentionally narrow. It does NOT bypass MTF, regime, data-quality,
news, EV or net-R:R checks because candidates rejected before the final score
stage are not eligible for challenger approval.

Research only. It never mutates a signal/decision, never calls the exchange,
and has no authority over LIVE scoring, sizing, risk or dispatch.
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Mapping

from bot.champion_challenger import ChallengerDecision, paired_report
from bot.research_authority import shadow_authority


STUDY_ID = "nexus-cc-forward-v1-20261003"
MODEL_VERSION = "economic_only_after_nexus_economic_gate_v1"
PRIMARY_HORIZON = "240m"
SECONDARY_HORIZON = "60m"

# Readiness floors are intentionally not promotion criteria. They only prevent
# interpreting tiny prospective samples as evidence.
MIN_CANDIDATES_FOR_READOUT = 100
MIN_KNOWN_OUTCOMES_FOR_READOUT = 60
MIN_DECISION_DISAGREEMENTS_FOR_READOUT = 20

_OUTCOME_COLUMNS = {
    "60m": "p60_net_pct",
    "240m": "p240_net_pct",
}

_TABLE_SQL = """CREATE TABLE IF NOT EXISTS nexus_challenger_forward_v1 (
    candidate_id TEXT PRIMARY KEY,
    captured_epoch REAL NOT NULL,
    study_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    production_sha TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    champion_allowed INTEGER NOT NULL,
    champion_score REAL NOT NULL,
    challenger_eligible INTEGER NOT NULL,
    challenger_approved INTEGER NOT NULL,
    challenger_score_r REAL NOT NULL,
    expected_value_pct REAL NOT NULL,
    risk_reward_net REAL NOT NULL,
    rr_floor REAL NOT NULL,
    risk_pct REAL NOT NULL,
    selection_reason TEXT NOT NULL,
    authority_json TEXT NOT NULL
)"""


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _candidate_id(sig) -> str:
    setup_id = str(getattr(sig, "_bgx_setup_id", "") or "").strip()
    if setup_id:
        return setup_id
    symbol = str(getattr(sig, "symbol", "UNKNOWN")).upper()
    side = str(getattr(sig, "direction", "UNKNOWN")).upper()
    entry = _finite(getattr(sig, "entry", 0.0))
    return f"{symbol}:{side}:{entry:.10g}"


def configured_rr_floor() -> float:
    """Mirror the NEXUS net-R:R floor without changing it."""
    try:
        from bot.config import cfg

        default = round(float(cfg.MIN_RR_RATIO) * 0.80, 2)
    except Exception:
        default = 1.60
    raw = _finite(os.environ.get("NEXUS_MIN_RR_NET", default), default)
    return raw if 0.0 < raw < 20.0 else default


def build_forward_record(
    sig,
    decision,
    *,
    captured_epoch: float | None = None,
) -> dict[str, Any]:
    """Build the prospective paired decision record without mutating inputs."""
    entry = _finite(getattr(sig, "entry", 0.0))
    stop = _finite(getattr(sig, "sl", 0.0))
    risk_pct = abs(entry - stop) / entry * 100.0 if entry > 0 and stop > 0 else 0.0

    ev_pct = _finite(getattr(decision, "expected_value", 0.0))
    rr_net = _finite(getattr(decision, "risk_reward", 0.0))
    champion_score = _finite(getattr(decision, "setup_quality", 0.0))
    champion_allowed = getattr(decision, "execution_allowed", False) is True
    rr_floor = configured_rr_floor()

    # Only decisions that contain the final-stage score and valid economics are
    # eligible. Early MTF/regime/data/news/EV rejects therefore remain rejected.
    eligible = (
        champion_score > 0.0
        and risk_pct > 0.0
        and ev_pct > 0.0
        and rr_net >= rr_floor
    )
    challenger_approved = bool(eligible)
    challenger_score_r = ev_pct / risk_pct if risk_pct > 0 else 0.0

    if eligible and champion_allowed:
        reason = "BOTH_APPROVE_FINAL_SCORE_PASSED"
    elif eligible:
        reason = "CHALLENGER_ONLY_FINAL_SCORE_IGNORED"
    else:
        reason = "BOTH_REJECT_NOT_FINAL_ECONOMICALLY_ELIGIBLE"

    authority = shadow_authority("champion_challenger_forward_v1").telemetry()
    return {
        "candidate_id": _candidate_id(sig),
        "captured_epoch": float(time.time() if captured_epoch is None else captured_epoch),
        "study_id": STUDY_ID,
        "model_version": MODEL_VERSION,
        "production_sha": str(os.environ.get("RAILWAY_GIT_COMMIT_SHA", "UNKNOWN")),
        "symbol": str(getattr(sig, "symbol", "")).upper(),
        "side": str(getattr(sig, "direction", "")).upper(),
        "champion_allowed": 1 if champion_allowed else 0,
        "champion_score": champion_score,
        "challenger_eligible": 1 if eligible else 0,
        "challenger_approved": 1 if challenger_approved else 0,
        "challenger_score_r": challenger_score_r,
        "expected_value_pct": ev_pct,
        "risk_reward_net": rr_net,
        "rr_floor": rr_floor,
        "risk_pct": risk_pct,
        "selection_reason": reason,
        "authority_json": json.dumps(
            authority, sort_keys=True, separators=(",", ":")
        ),
    }


async def persist_forward_record(db, row: Mapping[str, Any]) -> bool:
    """Append the first prospective observation for each candidate."""
    candidate_id = str(row.get("candidate_id") or "")
    if not candidate_id:
        raise ValueError("candidate_id required")

    await db._exec(_TABLE_SQL)
    sql = """INSERT INTO nexus_challenger_forward_v1 (
        candidate_id,captured_epoch,study_id,model_version,production_sha,
        symbol,side,champion_allowed,champion_score,challenger_eligible,
        challenger_approved,challenger_score_r,expected_value_pct,
        risk_reward_net,rr_floor,risk_pct,selection_reason,authority_json
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(candidate_id) DO NOTHING"""
    keys = (
        "candidate_id", "captured_epoch", "study_id", "model_version",
        "production_sha", "symbol", "side", "champion_allowed",
        "champion_score", "challenger_eligible", "challenger_approved",
        "challenger_score_r", "expected_value_pct", "risk_reward_net",
        "rr_floor", "risk_pct", "selection_reason", "authority_json",
    )
    return bool(await db._exec(sql, tuple(row[key] for key in keys)))


async def observe(db, sig, decision, log) -> dict[str, Any]:
    """Persist one paired forward decision and emit research-only telemetry."""
    row = build_forward_record(sig, decision)
    persisted = await persist_forward_record(db, row)
    log.info(
        "[NEXUS_CC_FORWARD_V1] candidate=%s symbol=%s side=%s "
        "champion=%s challenger=%s eligible=%s score_r=%.6f reason=%s "
        "persisted=%s study_id=%s model=%s shadow_only=true "
        "decision_effect=NONE execution_effect=NONE",
        row["candidate_id"],
        row["symbol"],
        row["side"],
        bool(row["champion_allowed"]),
        bool(row["challenger_approved"]),
        bool(row["challenger_eligible"]),
        row["challenger_score_r"],
        row["selection_reason"],
        persisted,
        STUDY_ID,
        MODEL_VERSION,
    )
    return row


def _row_value(row, key: str, index: int):
    if hasattr(row, "keys"):
        return row[key]
    return row[index]


async def prospective_report(
    db,
    *,
    horizon: str = PRIMARY_HORIZON,
) -> dict[str, Any]:
    """Read the prospective paired study without changing runtime authority."""
    column = _OUTCOME_COLUMNS.get(horizon)
    if column is None:
        raise ValueError("unsupported forward-test horizon")

    sql = f"""SELECT f.candidate_id,f.captured_epoch,f.champion_allowed,
                     f.champion_score,f.challenger_approved,
                     f.challenger_score_r,o.{column}
              FROM nexus_challenger_forward_v1 f
              LEFT JOIN opportunity_audit o ON o.signal_key=f.candidate_id
              WHERE f.study_id=?
              ORDER BY f.captured_epoch ASC,f.candidate_id ASC"""
    rows = await db._fetchall(sql, (STUDY_ID,))

    champion: list[ChallengerDecision] = []
    challenger: list[ChallengerDecision] = []
    outcomes: dict[str, float | None] = {}
    for row in rows or []:
        cid = str(_row_value(row, "candidate_id", 0))
        ts = float(_row_value(row, "captured_epoch", 1))
        champion.append(
            ChallengerDecision(
                candidate_id=cid,
                timestamp=ts,
                approved=bool(int(_row_value(row, "champion_allowed", 2))),
                score=float(_row_value(row, "champion_score", 3)),
            )
        )
        challenger.append(
            ChallengerDecision(
                candidate_id=cid,
                timestamp=ts,
                approved=bool(int(_row_value(row, "challenger_approved", 4))),
                score=float(_row_value(row, "challenger_score_r", 5)),
            )
        )
        raw = _row_value(row, column, 6)
        outcomes[cid] = None if raw is None else float(raw)

    report = paired_report(champion, challenger, outcomes)
    report.update(
        {
            "study_id": STUDY_ID,
            "model_version": MODEL_VERSION,
            "horizon": horizon,
            "primary_horizon": PRIMARY_HORIZON,
            "prospective_only": True,
            "readout_ready": (
                int(report["candidate_population"]) >= MIN_CANDIDATES_FOR_READOUT
                and int(report["known_outcomes"]) >= MIN_KNOWN_OUTCOMES_FOR_READOUT
                and int(report["decision_disagreements"])
                >= MIN_DECISION_DISAGREEMENTS_FOR_READOUT
            ),
            "readiness_floors": {
                "candidate_population": MIN_CANDIDATES_FOR_READOUT,
                "known_outcomes": MIN_KNOWN_OUTCOMES_FOR_READOUT,
                "decision_disagreements": MIN_DECISION_DISAGREEMENTS_FOR_READOUT,
            },
            "promotion_authority": False,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        }
    )
    return report
