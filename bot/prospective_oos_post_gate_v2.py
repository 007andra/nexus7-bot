"""Prospective OOS Post-Gate V2.

Independent research-only cohort collected after the LIVE drawdown gate is clear.
It never feeds the LIVE funnel and never mutates risk, sizing, thresholds,
positions, orders, HWM/drawdown, recovery, or exchange state.

V1 remains immutable and is never extended by this module.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
import os
import time

from bot.logger import shadow_log as log

FLAG = "PROSPECTIVE_OOS_POST_GATE_V2"
POPULATION = "POST_GATE_SHADOW_V2"
COHORT = "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS"
COHORT_ID = "CALIBRATION_POST_GATE_V2"

AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "prospective_only": True,
    "post_gate_v2": True,
    "population": POPULATION,
    "live_entries_blocked": False,
    "live_eligible": False,
    "live_candidate": False,
    "candidate_generation_effect": "RESEARCH_ONLY",
    "thresholds_unchanged": True,
    "risk_unchanged": True,
    "sizing_unchanged": True,
    "leverage_unchanged": True,
    "historical_hwm_preserved": True,
    "lifetime_drawdown_preserved": True,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}

_META = """CREATE TABLE IF NOT EXISTS prospective_oos_post_gate_v2 (
 cohort_id TEXT PRIMARY KEY,
 started_epoch REAL NOT NULL,
 payload TEXT NOT NULL
)"""
_CANDIDATES = """CREATE TABLE IF NOT EXISTS post_gate_shadow_candidates_v2 (
 candidate_id TEXT PRIMARY KEY,
 captured_epoch REAL NOT NULL,
 symbol TEXT NOT NULL,
 population TEXT NOT NULL,
 payload TEXT NOT NULL
)"""
_OUTCOMES = """CREATE TABLE IF NOT EXISTS post_gate_shadow_outcomes_v2 (
 candidate_id TEXT NOT NULL,
 horizon INTEGER NOT NULL,
 population TEXT NOT NULL,
 payload TEXT NOT NULL,
 PRIMARY KEY(candidate_id,horizon)
)"""

_TASKS = set()
_RUNNING_ENGINE_IDS = set()
_LAST_RUN_MONO = 0.0
_CURSOR = 0


def enabled() -> bool:
    return os.environ.get(FLAG, "true").strip().lower() in {"1", "true", "yes", "on"}


def _interval_s() -> float:
    try:
        value = float(os.environ.get("PROSPECTIVE_OOS_POST_GATE_V2_INTERVAL_S", "45"))
    except (TypeError, ValueError):
        return 45.0
    return max(15.0, min(value, 900.0))


def _batch_symbols() -> int:
    try:
        value = int(os.environ.get("PROSPECTIVE_OOS_POST_GATE_V2_SYMBOL_BATCH", "4"))
    except (TypeError, ValueError):
        return 4
    return max(1, min(value, 8))


def _emit(tag, values):
    def fmt(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (tuple, list)):
            return ",".join(map(str, value)) or "NONE"
        return str(value).replace(" ", "_")
    try:
        log.info("[%s] %s", tag, " ".join(f"{k}={fmt(v)}" for k, v in values.items()))
    except Exception:
        pass


async def _ensure_schema(db):
    await db._exec(_META)
    await db._exec(_CANDIDATES)
    await db._exec(_OUTCOMES)


async def ensure_cohort(db, *, started_epoch=None):
    await _ensure_schema(db)
    rows = await db._fetchall(
        "SELECT payload FROM prospective_oos_post_gate_v2 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)

    from bot import prospective_oos_cohort_v1 as v1

    start = float(time.time() if started_epoch is None else started_epoch)
    baseline = {
        **AUTHORITY,
        "cohort_id": COHORT_ID,
        "started_epoch": start,
        "discovery_cutoff_epoch": start,
        "hypothesis": {
            **v1.FROZEN_HYPOTHESIS,
            "hypothesis_id": COHORT_ID,
            "selection": "CURRENT_COUNTERFACTUAL_NEXUS_APPROVAL_FUNCTION_UNCHANGED_POST_GATE_V2",
            "production_threshold_changes": False,
        },
        "hypothesis_frozen": True,
        "reset_allowed": False,
        "v1_population_untouched": True,
        "v1_cohort_id_untouched": True,
    }
    await db._exec(
        "INSERT INTO prospective_oos_post_gate_v2 (cohort_id,started_epoch,payload) "
        "VALUES (?,?,?) ON CONFLICT(cohort_id) DO NOTHING",
        (COHORT_ID, start, json.dumps(baseline, sort_keys=True, allow_nan=False)),
    )
    rows = await db._fetchall(
        "SELECT payload FROM prospective_oos_post_gate_v2 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        return json.loads(raw)
    return baseline


def _apply_authority(signal):
    for key, value in AUTHORITY.items():
        setattr(signal, key, value)
    signal.evaluation_context = POPULATION
    return signal


async def _existing(db, candidate_id):
    rows = await db._fetchall(
        "SELECT payload FROM post_gate_shadow_candidates_v2 "
        "WHERE candidate_id=? AND population=?",
        (candidate_id, POPULATION),
    )
    if not rows:
        return None
    raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
    row = json.loads(raw)
    if (
        row.get("population") != POPULATION
        or row.get("post_gate_v2") is not True
        or row.get("live_eligible") is not False
        or row.get("execution_effect") != "NONE"
    ):
        raise ValueError("invalid V2 research authority")
    return row


async def _persist_candidate(db, row):
    if (
        row.get("population") != POPULATION
        or row.get("post_gate_v2") is not True
        or row.get("shadow_only") is not True
        or row.get("live_eligible") is not False
        or row.get("decision_effect") != "NONE"
        or row.get("execution_effect") != "NONE"
    ):
        raise ValueError("POST_GATE_SHADOW_V2 authority required")
    await db._exec(
        "INSERT INTO post_gate_shadow_candidates_v2 "
        "(candidate_id,captured_epoch,symbol,population,payload) VALUES (?,?,?,?,?) "
        "ON CONFLICT(candidate_id) DO NOTHING",
        (
            row["candidate_id"],
            row["captured_epoch"],
            row["symbol"],
            POPULATION,
            json.dumps(row, sort_keys=True, default=str, allow_nan=False),
        ),
    )


def _post_gate_active(engine):
    from bot import hard_gate_shadow_scan as legacy
    return not legacy.gate_snapshot(engine)["live_entries_blocked"]


def _eligible(minimum_order, *, pullback_pass, funnel):
    return (
        pullback_pass
        and funnel
        and minimum_order.get("capital_source") not in (None, "UNCONFIRMED")
        and minimum_order.get("shadow_min_order_feasible") is False
    )


async def _collect_batch(engine, db):
    global _CURSOR

    from bot.config import cfg
    from bot.strategy import Analyzer
    from bot.hard_gate_shadow_context import scope
    from bot import hard_gate_shadow_scan as legacy
    from bot import min_order_counterfactual_nexus_v1 as cf_nexus

    symbols = tuple(engine.viable_symbols or ())
    if not symbols:
        return {"symbols_examined": 0, "signals": 0, "eligible": 0, "persisted": 0}

    batch = min(_batch_symbols(), len(symbols))
    start = _CURSOR % len(symbols)
    selected = [symbols[(start + i) % len(symbols)] for i in range(batch)]
    _CURSOR = (start + batch) % len(symbols)

    analyzer = Analyzer()
    minimum = (
        cfg.POST_TARGET_SCORE if getattr(engine, "daily_target_hit", False)
        else cfg.MIN_ENTRY_SCORE
    )
    stats = {"symbols_examined": 0, "signals": 0, "eligible": 0, "persisted": 0}

    for symbol in selected:
        if not _post_gate_active(engine):
            _emit(
                "PROSPECTIVE_OOS_POST_GATE_V2",
                {
                    "status": "GATE_REBLOCKED_STOP_COLLECTION",
                    "new_candidates_after_reblock": 0,
                    **AUTHORITY,
                },
            )
            break
        stats["symbols_examined"] += 1
        try:
            klines = [
                deepcopy(engine.client.get_cached_klines(symbol, iv, limit))
                for iv, limit in (("15", 200), ("60", 100), ("240", 120))
            ]
            if any(len(rows) < need for rows, need in zip(klines, (60, 40, 20))):
                continue

            with scope() as context:
                sig = await legacy._compute(
                    analyzer.analyze_mtf,
                    symbol,
                    *klines,
                    min_score=minimum,
                    fee_mult=cfg.FEE_MULTIPLIER,
                    vol_mult=cfg.MIN_VOLUME_MULT,
                )
                sig = sig if sig is not None else context.signal
                if sig is None:
                    continue
                sig = _apply_authority(deepcopy(sig))
                stats["signals"] += 1

                captured = time.time()
                formation = (
                    getattr(sig, "_bgx_formation_bucket", None)
                    or int(captured // 900)
                )
                sig.candidate_id = (
                    f"{POPULATION}:{symbol}:{sig.direction}:{sig.entry_type}:{formation}"
                )
                sig._bgx_setup_id = sig.candidate_id
                if await _existing(db, sig.candidate_id) is not None:
                    continue

                pullback = context.pullback
                pullback_pass = pullback != "BLOCKED"
                ticker = deepcopy(engine.client.get_cached_ticker(symbol)) or None
                snap = legacy._cost(sig, ticker)
                features = legacy._cached_optional_features(symbol, snap)
                minimum_order = legacy.counterfactual_min_order(engine, sig, snap)
                minimum_order["live_risk_authority"] = "UNCHANGED_EXTERNAL_LIVE_AUTHORITY"
                minimum_order["post_gate_v2"] = True
                adjusted = engine._session_score_adjustment(symbol, sig.score)
                funnel = (
                    adjusted >= minimum
                    and sig.expected_pnl > 0
                    and engine._regime_allows_direction(
                        getattr(sig, "regime", "RANGING"), sig.direction
                    )
                )
                sig.score = adjusted
                if not _eligible(
                    minimum_order, pullback_pass=pullback_pass, funnel=funnel
                ):
                    continue
                stats["eligible"] += 1

                try:
                    decision = await legacy._compute(
                        legacy._decide, sig, klines, ticker, snap, features
                    )
                    obs = cf_nexus.build_observation(
                        sig, decision, minimum_order, captured_epoch=captured
                    )
                except Exception as exc:
                    obs = cf_nexus.error_observation(
                        sig,
                        minimum_order,
                        captured_epoch=captured,
                        error=type(exc).__name__,
                    )

                row = {
                    **AUTHORITY,
                    "candidate_id": sig.candidate_id,
                    "captured_epoch": captured,
                    "symbol": symbol,
                    "side": sig.direction,
                    "setup": sig.entry_type,
                    "regime": getattr(sig, "regime", "UNKNOWN"),
                    "score": sig.score,
                    "entry": sig.entry,
                    "stop": sig.sl,
                    "target": sig.tp,
                    "pullback_pass": pullback_pass,
                    "production_equivalent_pullback_result": pullback,
                    "production_equivalent_funnel_result": funnel,
                    "counterfactual_nexus_v1": obs,
                    "min_order_binding": minimum_order.get("binding"),
                    "required_equity_at_min_qty": minimum_order.get(
                        "required_equity_at_min_qty"
                    ),
                    "capital_source": minimum_order.get("capital_source"),
                    "production_sha": os.environ.get(
                        "RAILWAY_GIT_COMMIT_SHA", "UNKNOWN"
                    ) or "UNKNOWN",
                    "gate_clear_observed_at_capture": True,
                    "evaluation_fidelity": features.get("evaluation_fidelity"),
                    "missing_features": features.get("missing_features"),
                    "cost_snapshot": {
                        "taker_fee": snap.taker_fee,
                        "entry_slippage": snap.entry_slippage,
                        "exit_slippage": snap.exit_slippage,
                        "spread_bps": snap.spread_bps,
                        "fee_source": snap.fee_source,
                        "slippage_source": snap.slippage_source,
                    },
                }
                if not _post_gate_active(engine):
                    _emit(
                        "PROSPECTIVE_OOS_POST_GATE_V2",
                        {
                            "status": "GATE_REBLOCKED_BEFORE_PERSIST",
                            "candidate_id": row["candidate_id"],
                            "persisted": False,
                            **AUTHORITY,
                        },
                    )
                    break
                await _persist_candidate(db, row)
                stats["persisted"] += 1
                _emit(
                    "PROSPECTIVE_OOS_POST_GATE_V2_CANDIDATE",
                    {
                        "candidate_id": row["candidate_id"],
                        "symbol": row["symbol"],
                        "side": row["side"],
                        "setup": row["setup"],
                        "allowed": obs.get("execution_allowed") is True,
                        "population": POPULATION,
                        **AUTHORITY,
                    },
                )
        except Exception as exc:
            _emit(
                "PROSPECTIVE_OOS_POST_GATE_V2",
                {
                    "status": "CANDIDATE_ERROR",
                    "symbol": symbol,
                    "error": type(exc).__name__,
                    **AUTHORITY,
                },
            )
        await asyncio.sleep(0)

    return stats


async def _mature_outcomes(engine, db, *, batch_limit=12):
    from bot import hard_gate_shadow_scan as legacy

    limit = max(1, min(int(batch_limit), 50))
    stats = {"examined": 0, "written": 0, "cache_gap_deferred": 0}
    for horizon in (60, 240):
        rows = await db._fetchall(
            "SELECT c.candidate_id,c.payload FROM post_gate_shadow_candidates_v2 c "
            "LEFT JOIN post_gate_shadow_outcomes_v2 o ON "
            "o.candidate_id=c.candidate_id AND o.horizon=? "
            "WHERE c.population=? AND o.candidate_id IS NULL "
            "ORDER BY c.captured_epoch,c.candidate_id LIMIT ?",
            (horizon, POPULATION, limit),
        )
        for item in rows or []:
            cid = str(item["candidate_id"] if hasattr(item, "keys") else item[0])
            raw = item["payload"] if hasattr(item, "keys") else item[1]
            try:
                row = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                row.get("population") != POPULATION
                or row.get("live_eligible") is not False
                or row.get("post_gate_v2") is not True
            ):
                continue
            stats["examined"] += 1
            bars = deepcopy(
                engine.client.get_cached_klines(row["symbol"], "15", 200)
            )
            outcome = legacy.outcome_from_cache(
                row, bars, horizon, time.time()
            )
            if outcome is None:
                continue
            if outcome.get("outcome") != "OBSERVED":
                stats["cache_gap_deferred"] += 1
                continue
            payload = {
                **outcome,
                **AUTHORITY,
                "candidate_id": cid,
                "horizon": horizon,
                "population": POPULATION,
            }
            await db._exec(
                "INSERT INTO post_gate_shadow_outcomes_v2 "
                "(candidate_id,horizon,population,payload) VALUES (?,?,?,?) "
                "ON CONFLICT(candidate_id,horizon) DO NOTHING",
                (
                    cid,
                    horizon,
                    POPULATION,
                    json.dumps(payload, sort_keys=True, allow_nan=False),
                ),
            )
            stats["written"] += 1
    return stats


async def snapshot(db):
    from bot import prospective_oos_cohort_v1 as v1

    baseline = await ensure_cohort(db)
    started = float(baseline["started_epoch"])

    rows = await db._fetchall(
        "SELECT payload FROM post_gate_shadow_candidates_v2 "
        "WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch",
        (POPULATION, started),
    )
    payloads = []
    invalid_candidate_rows = 0
    candidate_ids = set()
    for item in rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[0]
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_candidate_rows += 1
            continue
        obs = obj.get("counterfactual_nexus_v1")
        cid = str(obj.get("candidate_id") or "")
        if (
            not cid
            or obj.get("population") != POPULATION
            or obj.get("post_gate_v2") is not True
            or obj.get("shadow_only") is not True
            or obj.get("live_eligible") is not False
            or obj.get("execution_effect") != "NONE"
            or not isinstance(obs, dict)
            or str(obs.get("candidate_id") or "") != cid
            or obs.get("cohort") != COHORT
            or obs.get("risk_epoch_traversal_credit") is not False
        ):
            invalid_candidate_rows += 1
            continue
        candidate_ids.add(cid)
        payloads.append(obj)

    out_rows = await db._fetchall(
        "SELECT candidate_id,horizon,payload FROM post_gate_shadow_outcomes_v2 "
        "WHERE population=? AND horizon IN (?,?)",
        (POPULATION, 60, 240),
    )
    out60, out240 = [], []
    invalid_outcome_rows = 0
    for item in out_rows or []:
        cid = str(item["candidate_id"] if hasattr(item, "keys") else item[0])
        horizon = int(item["horizon"] if hasattr(item, "keys") else item[1])
        raw = item["payload"] if hasattr(item, "keys") else item[2]
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_outcome_rows += 1
            continue
        if (
            cid not in candidate_ids
            or obj.get("population") != POPULATION
            or obj.get("post_gate_v2") is not True
            or obj.get("live_eligible") is not False
            or obj.get("execution_effect") != "NONE"
            or int(obj.get("horizon", -1)) != horizon
            or obj.get("outcome") != "OBSERVED"
        ):
            invalid_outcome_rows += 1
            continue
        obj["candidate_id"] = cid
        (out60 if horizon == 60 else out240).append(obj)

    baseline_valid = (
        baseline.get("cohort_id") == COHORT_ID
        and baseline.get("population") == POPULATION
        and baseline.get("hypothesis_frozen") is True
        and baseline.get("reset_allowed") is False
        and baseline.get("v1_population_untouched") is True
        and baseline.get("v1_cohort_id_untouched") is True
        and isinstance(baseline.get("hypothesis"), dict)
        and baseline["hypothesis"].get("hypothesis_id") == COHORT_ID
        and baseline["hypothesis"].get("production_threshold_changes") is False
    )

    report = v1.build_report(payloads, out60, out240, baseline=baseline)
    report.update(AUTHORITY)
    report["population"] = POPULATION
    report["cohort_id"] = COHORT_ID
    report["v1_untouched"] = True
    report["invalid_candidate_rows"] = invalid_candidate_rows
    report["invalid_outcome_rows"] = invalid_outcome_rows
    report["cohort_metadata_frozen"] = baseline_valid
    report["audit_pass"] = (
        baseline_valid
        and invalid_candidate_rows == 0
        and invalid_outcome_rows == 0
    )
    return report


def format_summary(report):
    a60, r60 = report["allowed_60m"], report["rejected_60m"]
    a240, r240 = report["allowed_240m"], report["rejected_240m"]
    return (
        "[PROSPECTIVE_OOS_POST_GATE_V2] "
        f"cohort_id={report['cohort_id']} population={POPULATION} "
        f"status={report['status']} "
        f"blockers={','.join(report['blockers']) or 'NONE'} "
        f"enrolled={report['enrolled_candidates']}/50 "
        f"obs60={report['observed_60m']}/30 "
        f"obs240={report['observed_240m']}/30 "
        f"allowed60_n={a60['n']} rejected60_n={r60['n']} "
        f"allowed240_n={a240['n']} rejected240_n={r240['n']} "
        f"sample_complete={str(report['sample_complete']).lower()} "
        f"audit_pass={str(report['audit_pass']).lower()} "
        f"invalid_candidates={report['invalid_candidate_rows']} "
        f"invalid_outcomes={report['invalid_outcome_rows']} "
        "v1_untouched=true research_only=true shadow_only=true "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


async def _run(engine):
    from bot import database as db

    await ensure_cohort(db)
    mature = await _mature_outcomes(engine, db)
    collect = await _collect_batch(engine, db)
    report = await snapshot(db)
    log.warning("%s", format_summary(report))
    _emit(
        "PROSPECTIVE_OOS_POST_GATE_V2_RUN",
        {
            **collect,
            **{f"outcome_{k}": v for k, v in mature.items()},
            "enrolled": report.get("enrolled_candidates"),
            "observed_60m": report.get("observed_60m"),
            "observed_240m": report.get("observed_240m"),
            "v1_untouched": True,
            **AUTHORITY,
        },
    )
    return report


def _task_done(task, engine_id):
    _TASKS.discard(task)
    _RUNNING_ENGINE_IDS.discard(engine_id)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _emit(
            "PROSPECTIVE_OOS_POST_GATE_V2",
            {"status": "ERROR", "error": type(exc).__name__, **AUTHORITY},
        )


def schedule_if_enabled(engine):
    """Schedule bounded V2 research without delaying the LIVE entry loop."""
    global _LAST_RUN_MONO

    if not enabled():
        return False

    from bot import hard_gate_shadow_scan as legacy

    if legacy.gate_snapshot(engine)["live_entries_blocked"]:
        return False

    now = time.monotonic()
    if _LAST_RUN_MONO > 0.0 and now - _LAST_RUN_MONO < _interval_s():
        return False

    engine_id = id(engine)
    if engine_id in _RUNNING_ENGINE_IDS:
        return False

    _LAST_RUN_MONO = now
    _RUNNING_ENGINE_IDS.add(engine_id)
    task = asyncio.create_task(_run(engine))
    _TASKS.add(task)
    task.add_done_callback(lambda t: _task_done(t, engine_id))
    return True


__all__ = [
    "AUTHORITY",
    "COHORT_ID",
    "FLAG",
    "POPULATION",
    "enabled",
    "ensure_cohort",
    "format_summary",
    "schedule_if_enabled",
    "snapshot",
]
