"""Prospective research-only cohort for high-quality low-capital markets.

The cohort is frozen once from LOW_CAPITAL_QUALITY_FRONTIER_V1, then evaluated
with public Binance market data through the strategy Analyzer and NEXUS AI.
It never changes cfg.SYMBOLS, viable_symbols, risk, sizing, leverage,
thresholds, dispatch, positions or orders.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
import os
import time
from typing import Any

from bot.config import cfg
from bot.logger import shadow_log as log

FLAG = "LOW_CAPITAL_SHADOW_COHORT_V1"
POPULATION = "LOW_CAPITAL_SHADOW_COHORT_V1"
COHORT_ID = "LOW_CAPITAL_SHADOW_COHORT_V1_20261006"

AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "prospective_only": True,
    "live_eligible": False,
    "live_candidate": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "candidate_generation_effect": "RESEARCH_ONLY",
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}

_SELECTION = """CREATE TABLE IF NOT EXISTS low_capital_shadow_cohort_v1 (
 cohort_id TEXT PRIMARY KEY,
 started_epoch REAL NOT NULL,
 payload TEXT NOT NULL
)"""
_CANDIDATES = """CREATE TABLE IF NOT EXISTS low_capital_shadow_candidates_v1 (
 candidate_id TEXT PRIMARY KEY,
 captured_epoch REAL NOT NULL,
 symbol TEXT NOT NULL,
 population TEXT NOT NULL,
 payload TEXT NOT NULL
)"""
_OUTCOMES = """CREATE TABLE IF NOT EXISTS low_capital_shadow_outcomes_v1 (
 candidate_id TEXT NOT NULL,
 horizon INTEGER NOT NULL,
 population TEXT NOT NULL,
 payload TEXT NOT NULL,
 PRIMARY KEY(candidate_id,horizon)
)"""

_CURSOR = 0


def enabled() -> bool:
    return os.environ.get(FLAG, "true").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _interval_s() -> float:
    try:
        value = float(os.environ.get("LOW_CAPITAL_SHADOW_INTERVAL_S", "90"))
    except (TypeError, ValueError):
        value = 90.0
    return max(30.0, min(900.0, value))


def _selection_limits() -> dict[str, float]:
    return {
        "max_symbols": float(_bounded_int("LOW_CAPITAL_SHADOW_MAX_SYMBOLS", 10, 4, 12)),
        "min_score": float(os.environ.get("LOW_CAPITAL_SHADOW_MIN_QUALITY", "0.68")),
        "min_volume_usdt": float(os.environ.get("LOW_CAPITAL_SHADOW_MIN_VOLUME_USDT", "50000000")),
        "max_spread_bps": float(os.environ.get("LOW_CAPITAL_SHADOW_MAX_SPREAD_BPS", "2.5")),
        "min_depth10_usdt": float(os.environ.get("LOW_CAPITAL_SHADOW_MIN_DEPTH10_USDT", "10000")),
        "min_oi_usdt": float(os.environ.get("LOW_CAPITAL_SHADOW_MIN_OI_USDT", "20000000")),
        "min_history_days": float(os.environ.get("LOW_CAPITAL_SHADOW_MIN_HISTORY_DAYS", "60")),
        "max_min_notional": float(os.environ.get("LOW_CAPITAL_SHADOW_MAX_MIN_NOTIONAL", "6.0")),
        "min_max_stop_pct": float(os.environ.get("LOW_CAPITAL_SHADOW_MIN_MAX_STOP_PCT", "0.10")),
    }


def select_frontier_rows(rows) -> tuple[dict[str, object], ...]:
    limits = _selection_limits()
    selected = []
    for row in rows or ():
        try:
            if not bool(row.get("eligible_for_research")):
                continue
            if bool(row.get("configured_live_universe")):
                continue
            checks = (
                float(row["market_quality_score"]) >= limits["min_score"],
                float(row["quote_volume_usdt"]) >= limits["min_volume_usdt"],
                float(row["spread_bps"]) <= limits["max_spread_bps"],
                float(row["depth_10bps_usdt"]) >= limits["min_depth10_usdt"],
                float(row["open_interest_usdt"]) >= limits["min_oi_usdt"],
                float(row["history_days"]) >= limits["min_history_days"],
                float(row["min_order_notional"]) <= limits["max_min_notional"],
                float(row["max_stop_pct"]) >= limits["min_max_stop_pct"],
            )
        except (KeyError, TypeError, ValueError):
            continue
        if all(checks):
            selected.append({
                **row,
                "cohort_selection_reason": "QUALITY_AND_LOW_CAPITAL_FILTERS_PASS",
                **AUTHORITY,
            })
    selected.sort(
        key=lambda row: (
            -float(row["market_quality_score"]),
            int(row.get("frontier_rank", 999999)),
            str(row["symbol"]),
        )
    )
    return tuple(selected[: int(limits["max_symbols"])])


async def _ensure_schema(db) -> None:
    await db._exec(_SELECTION)
    await db._exec(_CANDIDATES)
    await db._exec(_OUTCOMES)


async def _frontier_snapshot(engine, timeout_s: float = 45.0):
    deadline = time.monotonic() + max(1.0, timeout_s)
    while time.monotonic() < deadline:
        rows = getattr(engine, "_low_capital_quality_frontier_v1_snapshot", None)
        if rows:
            return tuple(deepcopy(rows))
        await asyncio.sleep(0.5)
    raise RuntimeError("quality frontier snapshot unavailable")


async def ensure_frozen_cohort(engine, db) -> dict[str, object]:
    await _ensure_schema(db)
    rows = await db._fetchall(
        "SELECT payload FROM low_capital_shadow_cohort_v1 WHERE cohort_id=?",
        (COHORT_ID,),
    )
    if rows:
        raw = rows[0]["payload"] if hasattr(rows[0], "keys") else rows[0][0]
        payload = json.loads(raw)
        if (
            payload.get("population") != POPULATION
            or payload.get("frozen") is not True
            or payload.get("promotion_allowed") is not False
            or payload.get("execution_effect") != "NONE"
        ):
            raise ValueError("invalid frozen cohort authority")
        return payload

    frontier = await _frontier_snapshot(engine)
    selected = select_frontier_rows(frontier)
    if not selected:
        raise RuntimeError("no low-capital quality rows pass cohort filters")
    started = time.time()
    payload = {
        **AUTHORITY,
        "cohort_id": COHORT_ID,
        "population": POPULATION,
        "started_epoch": started,
        "frozen": True,
        "reset_allowed": False,
        "selection_limits": _selection_limits(),
        "symbols": [str(row["symbol"]) for row in selected],
        "selection": [
            {
                "symbol": row["symbol"],
                "frontier_rank": row.get("frontier_rank"),
                "market_quality_score": row["market_quality_score"],
                "quote_volume_usdt": row["quote_volume_usdt"],
                "spread_bps": row["spread_bps"],
                "depth_10bps_usdt": row["depth_10bps_usdt"],
                "open_interest_usdt": row["open_interest_usdt"],
                "history_days": row["history_days"],
                "min_order_notional": row["min_order_notional"],
                "max_stop_pct": row["max_stop_pct"],
                "funding_rate": row.get("funding_rate"),
            }
            for row in selected
        ],
    }
    await db._exec(
        "INSERT INTO low_capital_shadow_cohort_v1 (cohort_id,started_epoch,payload) "
        "VALUES (?,?,?) ON CONFLICT(cohort_id) DO NOTHING",
        (COHORT_ID, started, json.dumps(payload, sort_keys=True, allow_nan=False)),
    )
    return payload


def _parse_klines(raw: Any) -> list[dict[str, float]]:
    out = []
    for row in raw if isinstance(raw, list) else []:
        try:
            out.append({
                "ts": int(row[0]),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    out.sort(key=lambda item: item["ts"])
    return out


async def _public_klines(getter, symbol: str, interval: str, limit: int):
    raw = await asyncio.wait_for(
        getter(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        ),
        timeout=8.0,
    )
    return _parse_klines(raw)


async def _public_context(getter, symbol: str):
    ticker, book, premium, oi = await asyncio.gather(
        asyncio.wait_for(getter("/fapi/v1/ticker/24hr", {"symbol": symbol}), timeout=8.0),
        asyncio.wait_for(getter("/fapi/v1/ticker/bookTicker", {"symbol": symbol}), timeout=8.0),
        asyncio.wait_for(getter("/fapi/v1/premiumIndex", {"symbol": symbol}), timeout=8.0),
        asyncio.wait_for(getter("/fapi/v1/openInterest", {"symbol": symbol}), timeout=8.0),
    )
    bid = float(book.get("bidPrice", 0) or 0)
    ask = float(book.get("askPrice", 0) or 0)
    if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
        raise RuntimeError("invalid public BBO")
    normalized_ticker = {
        "symbol": symbol,
        "lastPrice": float(ticker.get("lastPrice", 0) or 0),
        "bid": bid,
        "ask": ask,
        "volume": float(ticker.get("volume", 0) or 0),
        "turnover": float(ticker.get("quoteVolume", 0) or 0),
    }
    funding = float(premium.get("lastFundingRate", 0) or 0)
    return normalized_ticker, funding, oi


def _news_score() -> float | None:
    try:
        from bot import market_data
        sentiment = market_data.get_market_sentiment()
        if isinstance(sentiment, dict) and sentiment.get("score") is not None:
            value = float(sentiment["score"])
            if math.isfinite(value):
                return value
    except Exception:
        return None
    return None


def _selection_by_symbol(cohort: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(row["symbol"]): row
        for row in cohort.get("selection", [])
        if isinstance(row, dict) and row.get("symbol")
    }


async def _candidate_exists(db, candidate_id: str) -> bool:
    rows = await db._fetchall(
        "SELECT candidate_id FROM low_capital_shadow_candidates_v1 WHERE candidate_id=?",
        (candidate_id,),
    )
    return bool(rows)


async def _persist_candidate(db, row: dict[str, object]) -> None:
    if (
        row.get("population") != POPULATION
        or row.get("shadow_only") is not True
        or row.get("live_eligible") is not False
        or row.get("promotion_allowed") is not False
        or row.get("execution_effect") != "NONE"
    ):
        raise ValueError("research-only cohort authority required")
    await db._exec(
        "INSERT INTO low_capital_shadow_candidates_v1 "
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


async def evaluate_symbol(engine, db, cohort: dict[str, object], symbol: str) -> dict[str, object]:
    from bot.strategy import Analyzer
    from bot.hard_gate_shadow_context import scope
    from bot import hard_gate_shadow_scan as legacy

    getter = getattr(getattr(engine, "client", None), "_get", None)
    if getter is None:
        raise RuntimeError("binance public getter unavailable")
    k15, k1h, k4h, context = await asyncio.gather(
        _public_klines(getter, symbol, "15m", 200),
        _public_klines(getter, symbol, "1h", 100),
        _public_klines(getter, symbol, "4h", 120),
        _public_context(getter, symbol),
    )
    ticker, funding, oi = context
    if len(k15) < 60 or len(k1h) < 40 or len(k4h) < 20:
        return {"symbol": symbol, "status": "INSUFFICIENT_CANDLES", "persisted": False}

    analyzer = Analyzer()
    minimum = cfg.POST_TARGET_SCORE if getattr(engine, "daily_target_hit", False) else cfg.MIN_ENTRY_SCORE
    with scope():
        signal = await legacy._compute(
            analyzer.analyze_mtf,
            symbol,
            k15,
            k1h,
            k4h,
            min_score=minimum,
            fee_mult=cfg.FEE_MULTIPLIER,
            vol_mult=cfg.MIN_VOLUME_MULT,
        )
    if signal is None:
        return {"symbol": symbol, "status": "NO_STRATEGY_SIGNAL", "persisted": False}

    captured = time.time()
    formation = int(captured // 900)
    candidate_id = (
        f"{POPULATION}:{symbol}:{signal.direction}:{signal.entry_type}:{formation}"
    )
    if await _candidate_exists(db, candidate_id):
        return {"symbol": symbol, "status": "DUPLICATE", "persisted": False}

    signal.candidate_id = candidate_id
    signal._bgx_setup_id = candidate_id
    cost = legacy._cost(signal, ticker)
    features = {
        "funding": funding if math.isfinite(funding) else None,
        "oi": oi if isinstance(oi, dict) else None,
        "oi_delta": None,
        "news_score": _news_score(),
    }
    decision = await legacy._compute(
        legacy._decide,
        signal,
        [k15, k1h, k4h],
        ticker,
        cost,
        features,
    )
    selection = _selection_by_symbol(cohort)[symbol]
    stop_width_pct = abs(float(signal.entry) - float(signal.sl)) / float(signal.entry)
    capital_fit = stop_width_pct <= float(selection["max_stop_pct"])
    round_trip_cost_pct = (
        2.0 * float(cost.taker_fee)
        + float(cost.entry_slippage)
        + float(cost.exit_slippage)
    )
    row = {
        **AUTHORITY,
        "candidate_id": candidate_id,
        "cohort_id": COHORT_ID,
        "population": POPULATION,
        "captured_epoch": captured,
        "symbol": symbol,
        "side": signal.direction,
        "setup": signal.entry_type,
        "regime": getattr(signal, "regime", "UNKNOWN"),
        "strategy_score": float(signal.score),
        "entry": float(signal.entry),
        "stop": float(signal.sl),
        "target": float(signal.tp),
        "stop_width_pct": stop_width_pct,
        "capital_fit_at_frontier_snapshot": capital_fit,
        "frontier_rank": selection.get("frontier_rank"),
        "market_quality_score": selection["market_quality_score"],
        "min_order_notional": selection["min_order_notional"],
        "max_stop_pct": selection["max_stop_pct"],
        "round_trip_cost_pct": round_trip_cost_pct,
        "nexus": decision.to_dict(),
        "shadow_approved": bool(capital_fit and decision.execution_allowed is True),
        "pipeline_fidelity": "STRATEGY_ANALYZER_PLUS_NEXUS_PUBLIC_REST",
        "production_thresholds_unchanged": True,
        "production_universe_unchanged": True,
    }
    await _persist_candidate(db, row)
    return {
        "symbol": symbol,
        "status": "PERSISTED",
        "persisted": True,
        "candidate_id": candidate_id,
        "shadow_approved": row["shadow_approved"],
    }


async def _collect_batch(engine, db, cohort: dict[str, object]) -> dict[str, int]:
    global _CURSOR
    symbols = tuple(str(item) for item in cohort.get("symbols", []))
    if not symbols:
        return {"examined": 0, "signals": 0, "persisted": 0}
    batch = min(_bounded_int("LOW_CAPITAL_SHADOW_SYMBOL_BATCH", 3, 1, 5), len(symbols))
    start = _CURSOR % len(symbols)
    selected = [symbols[(start + offset) % len(symbols)] for offset in range(batch)]
    _CURSOR = (start + batch) % len(symbols)
    stats = {"examined": 0, "signals": 0, "persisted": 0}
    for symbol in selected:
        stats["examined"] += 1
        try:
            result = await evaluate_symbol(engine, db, cohort, symbol)
            if result["status"] not in {"NO_STRATEGY_SIGNAL", "INSUFFICIENT_CANDLES"}:
                stats["signals"] += 1
            stats["persisted"] += int(bool(result.get("persisted")))
            if result.get("persisted"):
                log.info(
                    "[LOW_CAPITAL_SHADOW_CANDIDATE_V1] symbol=%s candidate_id=%s "
                    "shadow_approved=%s research_only=true live_allowed=false "
                    "decision_effect=NONE execution_effect=NONE",
                    symbol,
                    result["candidate_id"],
                    str(bool(result["shadow_approved"])).lower(),
                )
        except Exception as exc:
            log.warning(
                "[LOW_CAPITAL_SHADOW_COHORT_V1] status=CANDIDATE_DEFER symbol=%s "
                "reason=%s research_only=true live_allowed=false "
                "decision_effect=NONE execution_effect=NONE",
                symbol,
                type(exc).__name__,
            )
        await asyncio.sleep(0)
    return stats


async def _outcome_bars(getter, symbol: str, start: float, end: float):
    raw = await asyncio.wait_for(
        getter(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": "15m",
                "startTime": int(start * 1000),
                "endTime": int(end * 1000 - 1),
                "limit": 32,
            },
        ),
        timeout=8.0,
    )
    return _parse_klines(raw)


async def mature_outcomes(engine, db, *, batch_limit: int = 12) -> dict[str, int]:
    from bot import hard_gate_shadow_scan as legacy

    getter = getattr(getattr(engine, "client", None), "_get", None)
    if getter is None:
        raise RuntimeError("binance public getter unavailable")
    limit = max(1, min(int(batch_limit), 30))
    stats = {"examined": 0, "written": 0}
    now = time.time()
    for horizon in (60, 240):
        rows = await db._fetchall(
            "SELECT c.candidate_id,c.payload FROM low_capital_shadow_candidates_v1 c "
            "LEFT JOIN low_capital_shadow_outcomes_v1 o ON "
            "o.candidate_id=c.candidate_id AND o.horizon=? "
            "WHERE c.population=? AND o.candidate_id IS NULL "
            "ORDER BY c.captured_epoch,c.candidate_id LIMIT ?",
            (horizon, POPULATION, limit),
        )
        for item in rows or []:
            raw = item["payload"] if hasattr(item, "keys") else item[1]
            row = json.loads(raw)
            end = math.ceil(float(row["captured_epoch"]) / 900) * 900 + horizon * 60
            if now < end:
                continue
            stats["examined"] += 1
            start = math.ceil(float(row["captured_epoch"]) / 900) * 900
            try:
                bars = await _outcome_bars(getter, row["symbol"], start, end)
                outcome = legacy.outcome_from_cache(row, bars, horizon, now)
            except Exception:
                continue
            if outcome is None or outcome.get("outcome") != "OBSERVED":
                continue
            future_gross = float(outcome["future_return"])
            outcome.update({
                **AUTHORITY,
                "candidate_id": row["candidate_id"],
                "population": POPULATION,
                "horizon": horizon,
                "future_return_net": future_gross - float(row.get("round_trip_cost_pct", 0) or 0),
                "return_basis": "hypothetical_entry_net_of_captured_cost_snapshot",
            })
            await db._exec(
                "INSERT INTO low_capital_shadow_outcomes_v1 "
                "(candidate_id,horizon,population,payload) VALUES (?,?,?,?) "
                "ON CONFLICT(candidate_id,horizon) DO NOTHING",
                (
                    row["candidate_id"],
                    horizon,
                    POPULATION,
                    json.dumps(outcome, sort_keys=True, allow_nan=False),
                ),
            )
            stats["written"] += 1
    return stats


async def snapshot(db) -> dict[str, object]:
    await _ensure_schema(db)
    rows = await db._fetchall(
        "SELECT payload FROM low_capital_shadow_candidates_v1 WHERE population=?",
        (POPULATION,),
    )
    candidates = []
    for item in rows or []:
        raw = item["payload"] if hasattr(item, "keys") else item[0]
        try:
            candidates.append(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    outcomes = await db._fetchall(
        "SELECT horizon,payload FROM low_capital_shadow_outcomes_v1 WHERE population=?",
        (POPULATION,),
    )
    observed = {60: [], 240: []}
    for item in outcomes or []:
        horizon = int(item["horizon"] if hasattr(item, "keys") else item[0])
        raw = item["payload"] if hasattr(item, "keys") else item[1]
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if horizon in observed and obj.get("outcome") == "OBSERVED":
            observed[horizon].append(obj)

    approved = [row for row in candidates if row.get("shadow_approved") is True]
    rejected = [row for row in candidates if row.get("shadow_approved") is False]

    def avg(rows_, key):
        values = [float(row[key]) for row in rows_ if row.get(key) is not None]
        return sum(values) / len(values) if values else None

    result = {
        **AUTHORITY,
        "population": POPULATION,
        "candidates": len(candidates),
        "approved": len(approved),
        "rejected": len(rejected),
        "observed_60": len(observed[60]),
        "observed_240": len(observed[240]),
        "avg_net_60": avg(observed[60], "future_return_net"),
        "avg_net_240": avg(observed[240], "future_return_net"),
    }
    return result


async def _run_loop(engine, runtime_log) -> None:
    from bot import database as db

    cohort = await ensure_frozen_cohort(engine, db)
    runtime_log.warning(
        "[LOW_CAPITAL_SHADOW_COHORT_V1] status=COHORT_FROZEN symbols=%s "
        "cohort_id=%s research_only=true prospective_only=true "
        "promotion_allowed=false live_allowed=false decision_effect=NONE execution_effect=NONE",
        ",".join(cohort["symbols"]),
        COHORT_ID,
    )
    while True:
        try:
            batch = await _collect_batch(engine, db, cohort)
            outcomes = await mature_outcomes(engine, db)
            snap = await snapshot(db)
            runtime_log.warning(
                "[LOW_CAPITAL_SHADOW_COHORT_V1] examined=%d persisted=%d "
                "outcomes_written=%d candidates=%d approved=%d rejected=%d "
                "observed60=%d observed240=%d avg_net60=%s avg_net240=%s "
                "research_only=true prospective_only=true live_allowed=false "
                "decision_effect=NONE execution_effect=NONE",
                batch["examined"],
                batch["persisted"],
                outcomes["written"],
                snap["candidates"],
                snap["approved"],
                snap["rejected"],
                snap["observed_60"],
                snap["observed_240"],
                "NA" if snap["avg_net_60"] is None else f"{float(snap['avg_net_60']):.8f}",
                "NA" if snap["avg_net_240"] is None else f"{float(snap['avg_net_240']):.8f}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime_log.warning(
                "[LOW_CAPITAL_SHADOW_COHORT_V1] status=DEFER reason=%s "
                "research_only=true live_allowed=false decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )
        await asyncio.sleep(_interval_s())


def schedule_if_enabled(engine, runtime_log) -> bool:
    if not enabled():
        return False
    task = getattr(engine, "_low_capital_shadow_cohort_v1_task", None)
    if task is not None and not task.done():
        return False
    starter = getattr(engine, "_start_background", None)
    if not callable(starter):
        raise RuntimeError("engine background manager unavailable")
    task = starter(_run_loop(engine, runtime_log))
    engine._low_capital_shadow_cohort_v1_task = task
    runtime_log.warning(
        "[LOW_CAPITAL_SHADOW_COHORT_V1] status=SCHEDULED "
        "scheduler=ENGINE_BACKGROUND_MANAGER research_only=true "
        "prospective_only=true shadow_only=true live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )
    return True
