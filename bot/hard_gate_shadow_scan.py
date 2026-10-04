"""Cache-only, separately persisted research while the drawdown hard gate holds.

No LIVE validator, sizing authority, exchange REST endpoint or execution method
is reachable from this module. The only client capability used is cache reads.
The feature flag defaults off. Research approval is evidence, never permission.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
import math
import os
import sys
import time
import weakref

from bot.hard_gate_shadow_context import AUTHORITY, POPULATION, mark, scope
from bot.logger import shadow_log as log

# Separate lifecycle state, never attached to TradingEngine or RiskManager.
_RUNNING = weakref.WeakSet()
_PERSISTENCE_LOGGED = set()
_OUTCOME_BATCH_LIMIT = 8
_TABLE = """CREATE TABLE IF NOT EXISTS hard_gate_shadow_candidates_v1 (
 candidate_id TEXT PRIMARY KEY, captured_epoch REAL NOT NULL,
 symbol TEXT NOT NULL, population TEXT NOT NULL, payload TEXT NOT NULL
)"""
_OUTCOMES = """CREATE TABLE IF NOT EXISTS hard_gate_shadow_outcomes_v1 (
 candidate_id TEXT NOT NULL, horizon INTEGER NOT NULL,
 population TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(candidate_id,horizon)
)"""


class GateCleared(Exception):
    pass


def enabled():
    return os.environ.get("HARD_GATE_SHADOW_SCAN", "false").strip().lower() == "true"


def _bbo_persistence_enabled():
    """Fail-closed persistence authority without importing the BBO runtime."""
    return os.environ.get(
        "NEXUS_BBO_COST_SHADOW_PERSIST", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _candidate_for_persistence(row):
    """Return a detached payload honoring the BBO persistence kill switch."""
    stored = deepcopy(row)
    if not _bbo_persistence_enabled():
        stored.pop("bbo_cost_observation", None)
    return stored


def gate_snapshot(engine):
    """Read the exact threshold/override predicates; never call can_open/update."""
    from bot.config import cfg
    from bot.drawdown_recovery import threshold_decision
    from bot.operator_runtime_policy import _risk_override_enabled

    dd = float(engine.risk.drawdown)
    limit = float(cfg.MAX_DRAWDOWN)
    allowed, reason, _ = threshold_decision(dd)
    blocked = (math.isfinite(dd) and math.isfinite(limit) and dd >= limit
               and not _risk_override_enabled() and not allowed)
    return {"drawdown": dd, "configured_limit": limit,
            "recovery_reason": reason, "live_entries_blocked": blocked}


def _check(engine):
    if not gate_snapshot(engine)["live_entries_blocked"]:
        raise GateCleared("LIVE_HARD_GATE_CLEARED")


def _emit(tag, values):
    def fmt(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (list, tuple)):
            return ",".join(map(str, value)) or "NONE"
        return str(value).replace(" ", "_")
    try:
        log.info("[%s] %s", tag, " ".join(f"{k}={fmt(v)}" for k, v in values.items()))
    except Exception:
        pass  # telemetry cannot change either research progression or LIVE authority


async def _compute(func, *args, **kwargs):
    # Cancellation cannot release single-flight while a worker is still using
    # the contextual analyzer. to_thread copies ContextVars into the worker.
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def _cost(sig, ticker):
    from bot import execution_cost as costs
    slip, spread = costs.ticker_slippage(ticker, sig.symbol)
    source = "ticker_half_spread_plus_impact"
    if slip is None:
        slip, source = costs.static_slippage_rate(sig.symbol), "static_symbol_fallback"
    cached_fee = costs.cached_taker_fee(sig.symbol)
    if cached_fee is None:
        taker, maker, fee_source = costs.fallback_taker_fee(), None, "hard_gate_cache_miss_fallback"
    else:
        taker, maker, fee_source, _ = cached_fee
    snap = costs.ExecutionCostSnapshot(
        snapshot_id=f"hard-gate-cost:{sig.candidate_id}", candidate_id=sig.candidate_id,
        exchange=costs.exchange_name(), symbol=sig.symbol, entry_reference=float(sig.entry),
        taker_fee=taker, maker_fee=maker,
        entry_slippage=slip, exit_slippage=slip, spread_bps=spread,
        fee_source=fee_source, slippage_source=source,
        observed_at=time.time(),
    )
    costs.attach_snapshot(sig, snap)
    return snap


def _confirmed_shadow_capital(engine):
    """Return read-only confirmed capital without performing exchange I/O.

    Prefer the authenticated account snapshot already published by the LIVE
    runtime. It carries an observation timestamp and is subject to the same
    freshness ceiling used by the pilot exposure-capacity gate. RiskManagerV3
    remains an acceptable fallback for offline/test contexts when it is already
    confirmed. Nothing here updates risk state or authorizes execution.
    """
    raw = getattr(getattr(engine, "client", None), "_last_account_overview_snapshot", None)
    if isinstance(raw, dict):
        try:
            observed_at = float(raw.get("_observed_at"))
            max_age = float(os.environ.get("PILOT_MAX_ACCOUNT_SNAPSHOT_AGE_S", "60"))
            age = time.time() - observed_at
            if (
                math.isfinite(observed_at)
                and math.isfinite(max_age)
                and max_age > 0
                and math.isfinite(age)
                and 0 <= age <= max_age
            ):
                from bot.professional_risk import capital_state_from_account_overview
                capital = capital_state_from_account_overview(raw)
                equity = float(capital.equity)
                available = float(capital.available_collateral)
                if all(math.isfinite(x) and x > 0 for x in (equity, available)):
                    return equity, available, "AUTHENTICATED_ACCOUNT_CACHE", age * 1000.0
        except (TypeError, ValueError, ArithmeticError):
            pass

    cached = getattr(getattr(engine, "risk", None), "professional_snapshot", None)
    if cached is not None and getattr(cached, "confirmed", False):
        try:
            equity = float(cached.capital.equity)
            available = float(cached.capital.available_collateral)
            if all(math.isfinite(x) and x > 0 for x in (equity, available)):
                return equity, available, "RISK_V3_CONFIRMED", None
        except (TypeError, ValueError, ArithmeticError, AttributeError):
            pass
    return None


def counterfactual_min_order(engine, sig, snap):
    from bot.config import cfg
    from bot.sizing_decomposition import decompose

    # Normal configured hypothetical budget, explicitly NOT recovery-adjusted
    # LIVE sizing. Missing confirmed cached capital is UNKNOWN, never a PASS.
    risk_pct = float(cfg.POST_TARGET_RISK if getattr(engine, "daily_target_hit", False)
                     else cfg.MAX_RISK_PCT)
    result = {"counterfactual": True, "counterfactual_risk_pct": risk_pct,
              "live_risk_authority": "BLOCKED_BY_DRAWDOWN_HARD_GATE",
              "shadow_min_order_feasible": None, "binding": "CAPITAL_UNCONFIRMED",
              "risk_budget": None, "min_valid_qty": None, "risk_at_min_qty": None,
              "capital_source": "UNCONFIRMED", "capital_age_ms": None}
    capital = _confirmed_shadow_capital(engine)
    if capital is None:
        return result
    equity, available, source, age_ms = capital
    result.update(capital_source=source, capital_age_ms=age_ms)
    if not all(math.isfinite(x) and x > 0 for x in (equity, available, risk_pct)) or risk_pct > 1:
        return result
    detail = decompose(
        info=deepcopy((engine.instruments or {}).get(sig.symbol, {})),
        equity=equity, available=available, entry=sig.entry, stop=sig.sl,
        risk_pct=risk_pct, leverage=cfg.LEVERAGE, max_margin_pct=cfg.MAX_MARGIN_PCT,
        fee_rate_per_side=snap.taker_fee,
        slippage_pct=max(float(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001")),
                         snap.slippage_allowance),
    )
    result.update(shadow_min_order_feasible=detail.get("result") == "PASS",
                  binding=detail.get("binding"), risk_budget=detail.get("risk_budget"),
                  min_valid_qty=detail.get("min_valid_qty"),
                  risk_at_min_qty=detail.get("risk_at_min_valid_qty"))
    return result


def _cached_optional_features(symbol, snap):
    """Reuse only already-populated process caches; never perform I/O."""
    funding = oi = oi_delta = news_score = None
    sources = {}

    try:
        from bot import market_data as mdata
        sentiment = mdata.get_market_sentiment()
        if isinstance(sentiment, dict) and sentiment.get("score") is not None:
            value = float(sentiment["score"])
            if math.isfinite(value):
                news_score = value
                sources["NEWS_SCORE"] = "MARKET_SENTIMENT_CACHE"
    except Exception:
        pass

    # The existing market-risk fallback is BTC-specific. Never project BTC
    # derivatives evidence onto another symbol.
    if str(symbol).upper() == "BTCUSDT":
        try:
            from bot import market_risk_runtime
            risk = market_risk_runtime.snapshot()
            signals = risk.get("signals", {}) if isinstance(risk, dict) else {}
            value = signals.get("funding_rate_pct")
            if value is not None and math.isfinite(float(value)):
                funding = float(value) / 100.0
                sources["FUNDING"] = "MARKET_RISK_CACHE_BTC"
            value = signals.get("open_interest_change_pct")
            if value is not None and math.isfinite(float(value)):
                oi_delta = float(value) / 100.0
                sources["OI_DELTA"] = "MARKET_RISK_CACHE_BTC"
        except Exception:
            pass

    if snap.fee_source in {"binance_commission_rate", "kucoin_actual_fee"}:
        sources["PRIVATE_FEE"] = snap.fee_source

    missing = []
    if funding is None:
        missing.append("FUNDING")
    if oi is None:
        missing.append("OPEN_INTEREST")
    if oi_delta is None:
        missing.append("OI_DELTA")
    if news_score is None:
        missing.append("NEWS_SCORE")
    if "PRIVATE_FEE" not in sources:
        missing.append("PRIVATE_FEE")
    fidelity = "FULL_CACHE_EQUIVALENT" if not missing else (
        "CACHE_ENRICHED" if len(missing) < 5 else "DEGRADED"
    )
    return {
        "funding": funding, "oi": oi, "oi_delta": oi_delta, "news_score": news_score,
        "missing_features": missing, "evaluation_fidelity": fidelity,
        "optional_feature_sources": sources,
    }


def _decide(sig, klines, ticker, snap, features):
    from bot import nexus_ai
    from bot.nexus_live_cost_calibration import NexusCostContext, _COST_CONTEXT
    ctx = NexusCostContext(sig.symbol, snap.taker_fee, snap.one_way_slippage,
                           snap.fee_source, snap.slippage_source, snap.spread_bps, snap)
    token = _COST_CONTEXT.set(ctx)
    try:
        return nexus_ai.decide(symbol=sig.symbol, k15=klines[0], k1h=klines[1], k4h=klines[2],
                               entry=sig.entry, sl=sig.sl, tp=sig.tp, ticker=ticker,
                               funding=features["funding"], oi=features["oi"],
                               oi_delta=features["oi_delta"], news_score=features["news_score"])
    finally:
        _COST_CONTEXT.reset(token)


def _executability_frontier(minimum_order, *, pullback_pass, funnel, decision):
    """Classify the first blocker in production-equivalent pipeline order."""
    if not pullback_pass:
        return "PULLBACK", "PULLBACK_BLOCKED"
    if not funnel:
        return "FUNNEL", "PRODUCTION_EQUIVALENT_FUNNEL_BLOCKED"
    if minimum_order.get("capital_source") == "UNCONFIRMED":
        return "CAPITAL", "CAPITAL_UNCONFIRMED"
    if minimum_order.get("shadow_min_order_feasible") is False:
        return "MIN_ORDER", str(minimum_order.get("binding") or "MINIMUM_ORDER")
    if decision is None:
        return "NEXUS", "NOT_EVALUATED"
    if getattr(decision, "execution_allowed", False) is True:
        return "SHADOW_APPROVED", "NEXUS_ALLOWED_RESEARCH_ONLY"

    reasoning = list(getattr(decision, "reasoning", None) or [])
    reason = str(reasoning[-1] if reasoning else "NEXUS_REJECTED")
    upper = reason.upper()
    if "R:R" in upper or "RR " in upper:
        stage = "NEXUS_RR"
    elif "EV " in upper or "EXPECTED VALUE" in upper:
        stage = "NEXUS_EV"
    elif "SCORE" in upper or "THRESHOLD" in upper:
        stage = "NEXUS_SCORE"
    elif "DADO" in upper or "DATA" in upper or "QUALIDADE" in upper:
        stage = "NEXUS_DATA"
    else:
        stage = "NEXUS_OTHER"
    return stage, reason[:240]


def _bbo_observation(sig, decision, view, *, client=None):
    # Optional compatibility, no import/runtime dependency on unmerged #494.
    # Pure build only: never uses its shared persistence population or feed start.
    runtime = sys.modules.get("bot.bbo_cost_shadow_runtime")
    if runtime is None or not runtime.enabled():
        return None
    try:
        # Snapshot acquired after NEXUS: reconnect generations are checked by
        # the owner at observation time, without re-running any decision.
        if view is None:
            view = runtime.snapshot_for_research(client, sig.symbol)
        record = runtime.build_record(sig, decision, view, floor=runtime.rr_floor())
        payload = {**asdict(record), **AUTHORITY}
        try:
            authority = " ".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}"
                                 for k, v in AUTHORITY.items())
            log.info("%s %s evaluation_context=HARD_GATE_SHADOW", runtime.model.format_record(record), authority)
        except Exception:
            pass
        return payload
    except Exception as exc:
        payload = {**AUTHORITY, "candidate_id": sig.candidate_id,
                   "status": "SHADOW_DATA_UNAVAILABLE", "error": type(exc).__name__}
        _emit("COST_SHADOW_BBO", payload)
        return payload


async def existing_candidate(db, candidate_id, *, guard):
    """Reuse durable research evidence on replay; never repeat its decision/observers."""
    guard()
    await db._exec(_TABLE)
    guard()
    rows = await db._fetchall(
        "SELECT payload FROM hard_gate_shadow_candidates_v1 WHERE candidate_id=? AND population=?",
        (candidate_id, POPULATION))
    guard()
    if not rows:
        return None
    raw = rows[0]
    row = json.loads(raw["payload"] if hasattr(raw, "keys") else raw[0])
    if any(row.get(k) != v for k, v in AUTHORITY.items()):
        raise ValueError("invalid stored research authority")
    if not _bbo_persistence_enabled() and candidate_id not in _PERSISTENCE_LOGGED:
        _PERSISTENCE_LOGGED.add(candidate_id)
        _emit("HARD_GATE_SHADOW_PERSISTENCE", {
            "candidate_id": candidate_id,
            "persist_enabled": False,
            "stored_bbo_present": "bbo_cost_observation" in row,
            "proof_source": "DB_READBACK_DEDUPE",
            **AUTHORITY,
        })
    # Historical rows created before persistence isolation may contain BBO data.
    # Preserve the stored audit record, but never surface/reuse that data while
    # the BBO persistence kill switch is disabled.
    if not _bbo_persistence_enabled():
        row.pop("bbo_cost_observation", None)
    return row


def _terminal_observation(row):
    from bot import candidate_terminal_telemetry as terminal
    from bot import min_order_feasibility_matrix as matrix
    for module, tag in ((terminal, "CANDIDATE_TERMINAL"), (matrix, "MIN_ORDER_FEASIBILITY_MATRIX")):
        try:
            if module.enabled():
                _emit(tag, module.shadow_record(row))
        except Exception:
            pass  # observers cannot alter candidate or LIVE authority


async def persist_candidate(db, row, *, guard=lambda: None):
    if row.get("population") != POPULATION or not row.get("shadow_only") or row.get("live_eligible") is not False:
        raise ValueError("research population required")
    guard()
    await db._exec(_TABLE)
    guard()
    stored = _candidate_for_persistence(row)
    result = await db._exec(
        "INSERT INTO hard_gate_shadow_candidates_v1 "
        "(candidate_id,captured_epoch,symbol,population,payload) VALUES (?,?,?,?,?) "
        "ON CONFLICT(candidate_id) DO NOTHING",
        (stored["candidate_id"], stored["captured_epoch"], stored["symbol"], POPULATION,
         json.dumps(stored, sort_keys=True, default=str, allow_nan=False)),
    )

    guard()
    return result


def outcome_from_cache(row, bars, horizon, now):
    """Only a complete, closed 15m path is eligible; gaps stay UNKNOWN.

    Entry is hypothetical at captured_epoch. To avoid using a pre-entry high/
    low, enrollment starts on the next 15m boundary, recorded in every outcome.
    This is a delayed observation, not a simulated LIVE fill.
    """
    start = math.ceil(float(row["captured_epoch"]) / 900) * 900
    end = start + horizon * 60
    if now < end:
        return None
    path = {}
    for bar in bars:
        ts = float(bar.get("ts", 0))
        ts = ts / 1000 if ts > 1e11 else ts
        if start <= ts < end and ts + 900 <= now:
            path[ts] = bar
    expected = list(range(int(start), int(end), 900))
    if not expected or any(ts not in path for ts in expected):
        return {**AUTHORITY, "horizon": horizon, "outcome": "UNKNOWN_CACHE_GAP",
                "future_return": None, "MFE": None, "MAE": None,
                "observation_start": start}
    entry = float(row["entry"])
    sign = 1 if row["side"] == "LONG" else -1
    candles = [path[ts] for ts in expected]
    returns = [sign * (float(b[k]) / entry - 1) for b in candles for k in ("h", "l")]
    future = sign * (float(candles[-1]["c"]) / entry - 1)
    return {**AUTHORITY, "horizon": horizon, "outcome": "OBSERVED",
            "future_return": future, "MFE": max(0, max(returns)), "MAE": min(0, min(returns)),
            "observation_start": start, "return_basis": "hypothetical_entry_gross"}


async def observe_outcomes(engine, db, *, batch_limit=_OUTCOME_BATCH_LIMIT):
    """Incrementally mature outcomes so one scan cannot inherit a large backlog."""
    limit = max(1, min(int(batch_limit), 50))
    stats = {"batch_limit": limit, "examined": 0, "written": 0, "cache_gap": 0}
    _check(engine)
    await db._exec(_TABLE)
    _check(engine)
    await db._exec(_OUTCOMES)
    for horizon in (60, 240):
        _check(engine)
        rows = await db._fetchall(
            "SELECT c.payload FROM hard_gate_shadow_candidates_v1 c "
            "LEFT JOIN hard_gate_shadow_outcomes_v1 o ON "
            "o.candidate_id=c.candidate_id AND o.horizon=? "
            "WHERE c.population=? AND o.candidate_id IS NULL "
            "ORDER BY c.captured_epoch LIMIT ?", (horizon, POPULATION, limit))
        _check(engine)
        for raw in rows or []:
            _check(engine)
            stats["examined"] += 1
            row = json.loads(raw["payload"] if hasattr(raw, "keys") else raw[0])
            if row.get("population") != POPULATION:
                continue
            bars = deepcopy(engine.client.get_cached_klines(row["symbol"], "15", 200))
            outcome = outcome_from_cache(row, bars, horizon, time.time())
            if outcome is not None:
                _check(engine)
                await db._exec(
                    "INSERT INTO hard_gate_shadow_outcomes_v1 "
                    "(candidate_id,horizon,population,payload) VALUES (?,?,?,?) "
                    "ON CONFLICT(candidate_id,horizon) DO NOTHING",
                    (row["candidate_id"], horizon, POPULATION, json.dumps(outcome, allow_nan=False)))
                stats["written"] += 1
                stats["cache_gap"] += int(outcome.get("outcome") == "UNKNOWN_CACHE_GAP")
                _check(engine)
    return stats


async def scan_if_enabled(engine):
    """Contained entrypoint called before the original LIVE skip. Returns evidence only."""
    if not enabled():
        return None
    try:
        return await scan(engine)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _emit("HARD_GATE_SHADOW_SCAN", {"error": type(exc).__name__, **AUTHORITY})
        return None


async def scan(engine, *, db=None, bbo_views=None):
    if not enabled():
        return None
    initial = gate_snapshot(engine)
    if not initial["live_entries_blocked"]:
        return None
    if engine in _RUNNING:
        _emit("HARD_GATE_SHADOW_SCAN", {"shadow_scan_skipped_reason": "PREVIOUS_SCAN_RUNNING", **AUTHORITY})
        return None
    _RUNNING.add(engine)
    started = time.perf_counter()
    summary = {**initial, **AUTHORITY, "shadow_scan_enabled": True,
               **dict.fromkeys(("symbols_scanned", "strategy_signals", "pullback_pass",
                                "min_order_feasible", "nexus_evaluated", "nexus_approved",
                                "nexus_rejected", "additional_rest_calls_per_scan"), 0)}
    records = []
    try:
        from bot.config import cfg
        from bot.strategy import Analyzer
        from bot.champion_challenger_forward_v1 import build_forward_record
        if db is None:
            from bot import database as db
        analyzer = Analyzer()  # research-owned instance; no engine/analyzer state
        symbols = tuple(engine.viable_symbols or ())
        minimum = cfg.POST_TARGET_SCORE if getattr(engine, "daily_target_hit", False) else cfg.MIN_ENTRY_SCORE
        for symbol in symbols:
            _check(engine)
            klines = [deepcopy(engine.client.get_cached_klines(symbol, iv, limit))
                      for iv, limit in (("15", 200), ("60", 100), ("240", 120))]
            summary["symbols_scanned"] += 1
            if any(len(rows) < limit for rows, limit in zip(klines, (60, 40, 20))):
                continue  # no REST fallback
            try:
                with scope() as context:
                    _check(engine)
                    sig = await _compute(analyzer.analyze_mtf, symbol, *klines,
                                         min_score=minimum, fee_mult=cfg.FEE_MULTIPLIER,
                                         vol_mult=cfg.MIN_VOLUME_MULT)
                    _check(engine)
                    sig = sig if sig is not None else context.signal
                    if sig is None:
                        continue
                    sig = mark(deepcopy(sig))
                    summary["strategy_signals"] += 1
                    captured = time.time()
                    formation = getattr(sig, "_bgx_formation_bucket", None) or int(captured // 900)
                    sig.candidate_id = f"{POPULATION}:{symbol}:{sig.direction}:{sig.entry_type}:{formation}"
                    sig._bgx_setup_id = sig.candidate_id
                    try:
                        existing = await existing_candidate(
                            db, sig.candidate_id, guard=lambda: _check(engine))
                    except GateCleared:
                        raise
                    except Exception as exc:
                        existing = None
                        _emit("HARD_GATE_SHADOW_SCAN", {"dedup_read_error": type(exc).__name__, **AUTHORITY})
                    _check(engine)
                    if existing is not None:
                        records.append(existing)
                        continue
                    pullback = context.pullback
                    passed = pullback != "BLOCKED"
                    summary["pullback_pass"] += int(passed)
                    ticker = deepcopy(engine.client.get_cached_ticker(symbol)) or None
                    snap = _cost(sig, ticker)
                    features = _cached_optional_features(symbol, snap)
                    minimum_order = counterfactual_min_order(engine, sig, snap)
                    summary["min_order_feasible"] += int(minimum_order["shadow_min_order_feasible"] is True)
                    # Read-only production funnel math. None of the LIVE scan's
                    # score history, cooldown or duplicate caches are touched.
                    adjusted = engine._session_score_adjustment(symbol, sig.score)
                    funnel = (adjusted >= minimum and sig.expected_pnl > 0 and
                              engine._regime_allows_direction(getattr(sig, "regime", "RANGING"), sig.direction))
                    sig.score = adjusted
                    decision = None
                    _check(engine)
                    if passed and funnel:
                        decision = await _compute(_decide, sig, klines, ticker, snap, features)
                        _check(engine)
                        summary["nexus_evaluated"] += 1
                        allowed = getattr(decision, "execution_allowed", False) is True
                        summary["nexus_approved" if allowed else "nexus_rejected"] += 1
                    frontier_stage, frontier_reason = _executability_frontier(
                        minimum_order, pullback_pass=passed, funnel=funnel, decision=decision
                    )
                    row = {
                        **AUTHORITY, "candidate_id": sig.candidate_id,
                        "captured_epoch": captured, "symbol": symbol, "side": sig.direction,
                        "setup": sig.entry_type, "score": sig.score, "entry": sig.entry,
                        "stop": sig.sl, "target": sig.tp, "pullback_pass": passed,
                        "production_equivalent_pullback_result": pullback,
                        "production_equivalent_funnel_result": funnel,
                        **minimum_order, "min_order_binding": minimum_order["binding"],
                        "nexus_called": decision is not None,
                        "nexus_allowed": getattr(decision, "execution_allowed", False) is True,
                        "nexus_net_rr": getattr(decision, "risk_reward", None),
                        "nexus_ev": getattr(decision, "expected_value", None),
                        "evaluation_context": POPULATION,
                        "evaluation_fidelity": features["evaluation_fidelity"],
                        "missing_features": features["missing_features"],
                        "optional_feature_sources": features["optional_feature_sources"],
                        "cached_funding": features["funding"],
                        "cached_oi_delta": features["oi_delta"],
                        "cached_news_score": features["news_score"],
                        "frontier_stage": frontier_stage,
                        "frontier_reason": frontier_reason,
                        "cost_snapshot": asdict(snap),
                    }
                    if decision is not None:
                        # Pure builder only; isolated table, no LIVE study counters.
                        row["champion_challenger"] = {**build_forward_record(sig, decision, captured_epoch=captured),
                                                       **AUTHORITY, "evaluation_fidelity": features["evaluation_fidelity"]}
                        row["bbo_cost_observation"] = _bbo_observation(
                            sig, decision, deepcopy((bbo_views or {}).get(symbol)), client=engine.client)
                    _check(engine)
                    _emit("SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED", {k: v for k, v in row.items()
                          if k not in {"cost_snapshot", "champion_challenger", "bbo_cost_observation"}})
                    _terminal_observation(row)
                    _emit("HARD_GATE_SHADOW_FRONTIER", {
                        "candidate_id": row["candidate_id"], "symbol": row["symbol"],
                        "side": row["side"], "stage": frontier_stage,
                        "reason": frontier_reason, "binding": row.get("binding"),
                        "capital_source": row.get("capital_source"),
                        **AUTHORITY,
                    })
                    _check(engine)
                    # Persist in the independent research dataset; failure never
                    # changes LIVE state. Bound IO to keep the engine responsive.
                    try:
                        await asyncio.wait_for(persist_candidate(db, row, guard=lambda: _check(engine)), timeout=1.0)
                    except GateCleared:
                        raise
                    except Exception as exc:
                        _emit("HARD_GATE_SHADOW_SCAN", {"persistence_error": type(exc).__name__, **AUTHORITY})
                    _check(engine)
                    records.append(row)
            except GateCleared:
                raise
            except Exception as exc:
                _emit("HARD_GATE_SHADOW_SCAN", {"symbol": symbol, "candidate_error": type(exc).__name__, **AUTHORITY})
            await asyncio.sleep(0)
        _check(engine)
        try:
            outcome_stats = await asyncio.wait_for(observe_outcomes(engine, db), timeout=2.0)
            _emit("HARD_GATE_SHADOW_OUTCOMES", {**outcome_stats, **AUTHORITY})
        except GateCleared:
            raise
        except Exception as exc:
            _emit("HARD_GATE_SHADOW_SCAN", {"outcome_error": type(exc).__name__, **AUTHORITY})
        _check(engine)
    except GateCleared:
        summary["shadow_scan_aborted_reason"] = "LIVE_HARD_GATE_CLEARED"
    finally:
        _RUNNING.discard(engine)
        summary["elapsed_ms"] = (time.perf_counter() - started) * 1000
        _emit("HARD_GATE_SHADOW_SCAN", summary)
    return {"summary": summary, "candidates": records}
