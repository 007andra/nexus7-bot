"""Nonblocking LIVE shadow capture for NEXUS research evidence.

This wrapper observes the final NEXUS decision returned by TradingEngine and
schedules best-effort persistence after that decision exists. It does not wait
for research IO, does not call exchange mutation endpoints, does not change the
signal or decision object, and never changes execution authority.

Candidate outcomes continue through the existing opportunity_audit telemetry,
which is also scheduled off the critical decision-return path.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any

from bot.research_authority import shadow_authority


_FLAG = "NEXUS_RESEARCH_SHADOW_CAPTURE"
_SCHEMA_VERSION = 1

_TABLE_SQL = """CREATE TABLE IF NOT EXISTS nexus_shadow_research_candidates (
    candidate_id TEXT PRIMARY KEY,
    captured_epoch REAL NOT NULL,
    schema_version INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    setup TEXT,
    strategy_score REAL,
    champion_decision TEXT,
    champion_allowed INTEGER,
    nexus_score REAL,
    confidence REAL,
    expected_value REAL,
    risk_reward REAL,
    regime TEXT,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    execution_cost_json TEXT,
    challenger_status TEXT NOT NULL,
    authority_json TEXT NOT NULL
)"""


def _enabled() -> bool:
    return os.environ.get(_FLAG, "true").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _candidate_id(sig) -> str:
    setup_id = str(getattr(sig, "_bgx_setup_id", "") or "").strip()
    if setup_id:
        return setup_id
    symbol = str(getattr(sig, "symbol", "UNKNOWN"))
    side = str(getattr(sig, "direction", "UNKNOWN")).upper()
    entry = _finite(getattr(sig, "entry", 0.0))
    bucket = int(time.time() // 900)
    return f"{symbol}:{side}:{entry:.10g}:{bucket}"


def _decision_value(decision) -> str:
    raw = getattr(decision, "decision", "UNKNOWN")
    return str(getattr(raw, "value", raw))


def _cost_payload(sig) -> dict[str, Any] | None:
    try:
        from bot.execution_cost import attached_snapshot

        snap = attached_snapshot(sig)
        if snap is None:
            return None
        return {
            "snapshot_id": snap.snapshot_id,
            "exchange": snap.exchange,
            "symbol": snap.symbol,
            "entry_reference": snap.entry_reference,
            "taker_fee": snap.taker_fee,
            "maker_fee": snap.maker_fee,
            "entry_slippage": snap.entry_slippage,
            "exit_slippage": snap.exit_slippage,
            "spread_bps": snap.spread_bps,
            "fee_source": snap.fee_source,
            "slippage_source": snap.slippage_source,
            "observed_at": snap.observed_at,
            "funding_assumption": snap.funding_assumption,
            "funding_rate": snap.funding_rate,
        }
    except Exception:
        return None


def build_snapshot(sig, decision, *, captured_epoch: float | None = None) -> dict[str, Any]:
    """Build immutable scalar research evidence without mutating inputs."""
    authority = shadow_authority("nexus_live_shadow_research_v1").telemetry()
    costs = _cost_payload(sig)
    return {
        "candidate_id": _candidate_id(sig),
        "captured_epoch": float(time.time() if captured_epoch is None else captured_epoch),
        "schema_version": _SCHEMA_VERSION,
        "symbol": str(getattr(sig, "symbol", "")).upper(),
        "side": str(getattr(sig, "direction", "")).upper(),
        "setup": str(getattr(sig, "entry_type", "UNKNOWN")).upper(),
        "strategy_score": _finite(getattr(sig, "score", 0.0)),
        "champion_decision": _decision_value(decision),
        "champion_allowed": 1 if getattr(decision, "execution_allowed", False) is True else 0,
        "nexus_score": _finite(getattr(decision, "setup_quality", 0.0)),
        "confidence": _finite(getattr(decision, "confidence", 0.0)),
        "expected_value": _finite(getattr(decision, "expected_value", 0.0)),
        "risk_reward": _finite(getattr(decision, "risk_reward", 0.0)),
        "regime": str(getattr(decision, "market_regime", "UNKNOWN")),
        "entry": _finite(getattr(sig, "entry", 0.0)),
        "stop_loss": _finite(getattr(sig, "sl", 0.0)),
        "take_profit": _finite(getattr(sig, "tp", 0.0)),
        "execution_cost_json": (
            json.dumps(costs, sort_keys=True, separators=(",", ":"))
            if costs is not None
            else None
        ),
        # Challenger models remain non-authoritative until enough OOS outcomes
        # exist for calibration and prospective paired comparison.
        "challenger_status": "AWAITING_OOS_OUTCOME_AND_CALIBRATION",
        "authority_json": json.dumps(
            authority, sort_keys=True, separators=(",", ":")
        ),
    }


async def persist_snapshot(db, row: dict[str, Any]) -> bool:
    """Append one immutable candidate snapshot. Existing IDs are never rewritten."""
    if not isinstance(row, dict) or not row.get("candidate_id"):
        raise ValueError("valid candidate snapshot required")
    await db._exec(_TABLE_SQL)
    sql = """INSERT INTO nexus_shadow_research_candidates (
        candidate_id,captured_epoch,schema_version,symbol,side,setup,
        strategy_score,champion_decision,champion_allowed,nexus_score,
        confidence,expected_value,risk_reward,regime,entry,stop_loss,
        take_profit,execution_cost_json,challenger_status,authority_json
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(candidate_id) DO NOTHING"""
    keys = (
        "candidate_id", "captured_epoch", "schema_version", "symbol", "side",
        "setup", "strategy_score", "champion_decision", "champion_allowed",
        "nexus_score", "confidence", "expected_value", "risk_reward", "regime",
        "entry", "stop_loss", "take_profit", "execution_cost_json",
        "challenger_status", "authority_json",
    )
    return bool(await db._exec(sql, tuple(row[key] for key in keys)))


async def _capture_safe(engine, sig, decision, log) -> None:
    if getattr(sig, "population", None) == "HARD_GATE_SHADOW":
        return  # isolated hard_gate_shadow_candidates_v1 owns this population
    candidate = _candidate_id(sig)
    persisted = False
    audit_observed = False
    challenger_observed = False

    try:
        from bot import database as db

        row = build_snapshot(sig, decision)
        persisted = await persist_snapshot(db, row)
    except Exception as exc:  # noqa: BLE001 - research IO must never affect trading
        log.warning(
            "[NEXUS_SHADOW_RESEARCH] candidate=%s snapshot_error=%s "
            "decision_effect=NONE execution_effect=NONE",
            candidate, type(exc).__name__,
        )

    try:
        from bot import missed_opportunity_audit

        await missed_opportunity_audit.observe(engine, sig, decision, log)
        audit_observed = True
    except Exception as exc:  # noqa: BLE001 - observational outcome path is isolated
        log.warning(
            "[NEXUS_SHADOW_RESEARCH] candidate=%s outcome_audit_error=%s "
            "decision_effect=NONE execution_effect=NONE",
            candidate, type(exc).__name__,
        )

    try:
        from bot import database as db
        from bot import champion_challenger_forward_v1 as cc_forward

        await cc_forward.observe(db, sig, decision, log)
        challenger_observed = True
    except Exception as exc:  # noqa: BLE001 - challenger is research-only
        log.warning(
            "[NEXUS_SHADOW_RESEARCH] candidate=%s challenger_forward_error=%s "
            "decision_effect=NONE execution_effect=NONE",
            candidate, type(exc).__name__,
        )

    log.info(
        "[NEXUS_SHADOW_RESEARCH] candidate=%s symbol=%s side=%s "
        "champion_allowed=%s persisted=%s opportunity_audit=%s challenger_forward=%s "
        "challenger_status=FORWARD_V1_ACTIVE_UNCALIBRATED "
        "shadow_only=true decision_effect=NONE execution_effect=NONE",
        candidate,
        getattr(sig, "symbol", "UNKNOWN"),
        getattr(sig, "direction", "UNKNOWN"),
        getattr(decision, "execution_allowed", False) is True,
        persisted,
        audit_observed,
        challenger_observed,
    )


def _consume_task(task: asyncio.Task, log) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        return
    except Exception as exc:  # pragma: no cover - final containment only
        log.warning(
            "[NEXUS_SHADOW_RESEARCH] task_error=%s "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )


def install(TradingEngine, log) -> None:
    if getattr(TradingEngine, "_nexus_shadow_research_capture_installed", False):
        return

    if not _enabled():
        TradingEngine._nexus_shadow_research_capture_installed = True
        log.info(
            "[NEXUS_SHADOW_RESEARCH] enabled=false feature_flag=%s "
            "decision_effect=NONE execution_effect=NONE",
            _FLAG,
        )
        return

    original = TradingEngine._nexus_validate

    async def _nexus_validate_with_shadow_capture(self, sig, *args, **kwargs):
        # The champion remains the sole decision authority.
        decision = await original(self, sig, *args, **kwargs)
        try:
            task = asyncio.create_task(
                _capture_safe(self, sig, decision, log),
                name=f"nexus-shadow-research-{getattr(sig, 'symbol', 'unknown')}",
            )
            task.add_done_callback(lambda done: _consume_task(done, log))
        except Exception as exc:  # noqa: BLE001 - scheduling cannot alter champion
            log.warning(
                "[NEXUS_SHADOW_RESEARCH] schedule_error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )
        # Identity-preserving contract: return the exact champion object.
        return decision

    TradingEngine._nexus_validate = _nexus_validate_with_shadow_capture
    TradingEngine._nexus_shadow_research_capture_installed = True
    log.info(
        "[NEXUS_SHADOW_RESEARCH] installed=true async_capture=true "
        "exchange_mutations=false champion_authority_unchanged=true "
        "challenger_authority=false feature_flag=%s "
        "decision_effect=NONE execution_effect=NONE",
        _FLAG,
    )
