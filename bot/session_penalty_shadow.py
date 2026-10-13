"""Read-only counterfactual calibration for the pre-NEXUS session score penalty.

Production remains authoritative. This module observes only Analyzer signals that
would be discarded by TradingEngine's existing session penalty, asks the exact
production NEXUS validator what it would have decided, and follows the subsequent
confirmed 15m path. Shadow evidence is durably persisted across process restarts.
It never creates/returns a trading Signal, calls an exchange mutation route,
changes a threshold, or mutates risk/execution state.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections import Counter

from bot.execution_model import estimated_round_trip_cost_pct
from bot.nexus_decision_consistency import closed_mtf
from bot import session_penalty_persistence as persistence

_MAX_BARS = 16
_LOCK = threading.Lock()
_SEEN: set[str] = set()
_ACTIVE: dict[str, dict] = {}
_DEFERRED_NEXUS: dict[str, tuple] = {}
_RESTORE_SCHEDULED = False
_RESTORE_COMPLETE = False
_METRICS = {
    "unique": 0,
    "eligible": 0,
    "resolved": 0,
    "restored": 0,
    "outcomes": Counter(),
    "sessions": Counter(),
    "symbols": Counter(),
    "nexus_counterfactuals": 0,
    "nexus_approved": 0,
    "nexus_vetoed": 0,
    "nexus_timeout": 0,
    "nexus_error": 0,
    "nexus_schedule_deferred": 0,
    "nexus_schedule_recovered": 0,
    "nexus_schedule_error": 0,
}


def _finite(value, default=0.0):
    try:
        out = float(value)
        return out if out == out and abs(out) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _closed_15m(k15, k1h=None, k4h=None):
    closed, _, _ = closed_mtf(k15, k1h or [], k4h or [])
    return closed


def _state_key(symbol: str, direction: str, bar_ts) -> str:
    return f"{symbol}:{str(direction).upper()}:{bar_ts}"


def _decision_reason(decision) -> str:
    reasoning = getattr(decision, "reasoning", None) or []
    if reasoning:
        return str(reasoning[-1])[:500]
    warnings = getattr(decision, "warnings", None) or []
    if warnings:
        return str(warnings[-1])[:500]
    return str(getattr(decision, "decision", "UNKNOWN"))[:500]


def _runtime_engine():
    """Return the already-created production engine without importing main."""
    main_module = sys.modules.get("main")
    app = getattr(main_module, "app", None) if main_module else None
    state = getattr(app, "state", None) if app is not None else None
    return getattr(state, "engine", None) if state is not None else None


def _schedule_persist(key: str, state: dict, status: str, log) -> None:
    """Best-effort durable write; absence of a loop is explicit and non-trading."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug(
            "[SESSION_PENALTY_PERSISTENCE] deferred_no_running_loop key=%s "
            "trading_effect=NONE execution_effect=NONE",
            key,
        )
        return

    try:
        snapshot_state = dict(state)
        loop.create_task(
            persistence.save_state(key, snapshot_state, status=status, log=log)
        )
    except Exception as exc:
        log.debug(
            "[SESSION_PENALTY_PERSISTENCE] schedule_failed error=%s "
            "trading_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )


async def _restore_persisted(log) -> None:
    """Restore unresolved observational cohorts without touching production state."""
    global _RESTORE_SCHEDULED, _RESTORE_COMPLETE
    try:
        rows = await persistence.load_open(log)
        if rows is None:
            _RESTORE_SCHEDULED = False
            return
        restored = 0
        with _LOCK:
            for key, state in rows:
                if key in _ACTIVE:
                    continue
                if int(_finite(state.get("bars"), 0)) >= _MAX_BARS:
                    continue
                restored_state = dict(state)
                restored_state.setdefault("nexus_schedule_status", "RESTORED")
                restored_state.setdefault("nexus_schedule_reason", "")
                _SEEN.add(key)
                _ACTIVE[key] = restored_state
                restored += 1
            _METRICS["restored"] += restored
        _RESTORE_COMPLETE = True
        log.info(
            "[SESSION_PENALTY_PERSISTENCE] restore_complete=true restored=%d "
            "active=%d production_policy_unchanged=true trading_effect=NONE "
            "execution_effect=NONE",
            restored, len(_ACTIVE),
        )
    except Exception as exc:
        _RESTORE_SCHEDULED = False
        log.debug(
            "[SESSION_PENALTY_PERSISTENCE] restore_failed error=%s "
            "trading_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )


def _schedule_restore(log) -> None:
    global _RESTORE_SCHEDULED
    if _RESTORE_COMPLETE or _RESTORE_SCHEDULED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _RESTORE_SCHEDULED = True
    loop.create_task(_restore_persisted(log))


def _directional_excursions(state: dict, high: float, low: float) -> tuple[float, float]:
    entry = _finite(state.get("entry"))
    if entry <= 0:
        return 0.0, 0.0
    if state.get("direction") == "LONG":
        favorable = (high - entry) / entry * 100.0
        adverse = (low - entry) / entry * 100.0
    else:
        favorable = (entry - low) / entry * 100.0
        adverse = (entry - high) / entry * 100.0
    return favorable, adverse


def _resolve(key: str, state: dict, outcome: str, exit_price: float, log) -> None:
    entry = _finite(state.get("entry"))
    if entry <= 0:
        net_pct = 0.0
    else:
        raw = (exit_price - entry) / entry * 100.0
        if state.get("direction") == "SHORT":
            raw = -raw
        net_pct = raw - estimated_round_trip_cost_pct(state.get("symbol", "BTCUSDT"))

    state["outcome"] = outcome
    state["net_pct"] = round(net_pct, 5)
    _METRICS["resolved"] += 1
    _METRICS["outcomes"][outcome] += 1
    _DEFERRED_NEXUS.pop(key, None)
    _schedule_persist(key, state, "RESOLVED", log)
    log.info(
        "[SESSION_PENALTY_SHADOW_OUTCOME] symbol=%s side=%s session=%s "
        "penalty=%+d score=%s->%s nexus_status=%s nexus_approved=%s "
        "outcome=%s net_pct=%.4f mfe=%.4f mae=%.4f bars=%d durable=true "
        "decision_effect=NONE execution_effect=NONE",
        state.get("symbol"), state.get("direction"), state.get("session"),
        int(state.get("penalty", 0)), state.get("base_score"),
        state.get("adjusted_score"), state.get("nexus_status"),
        state.get("nexus_approved"), outcome, state.get("net_pct", 0.0),
        state.get("mfe_pct", 0.0), state.get("mae_pct", 0.0),
        int(state.get("bars", 0)),
    )


def _update_outcomes(symbol: str, k15, k1h, k4h, log) -> None:
    closed = _closed_15m(k15, k1h, k4h)
    if not closed:
        return
    with _LOCK:
        for key, state in list(_ACTIVE.items()):
            if state.get("symbol") != symbol:
                continue
            last_ts = state.get("last_bar_ts")
            for bar in closed:
                ts = bar.get("ts")
                if ts is None or (last_ts is not None and ts <= last_ts):
                    continue
                high = _finite(bar.get("h"))
                low = _finite(bar.get("l"))
                if high <= 0 or low <= 0:
                    continue
                state["last_bar_ts"] = ts
                state["bars"] += 1
                favorable, adverse = _directional_excursions(state, high, low)
                state["mfe_pct"] = max(_finite(state.get("mfe_pct")), favorable)
                state["mae_pct"] = min(_finite(state.get("mae_pct")), adverse)

                if state.get("direction") == "LONG":
                    tp_hit = high >= state["tp"]
                    sl_hit = low <= state["sl"]
                else:
                    tp_hit = low <= state["tp"]
                    sl_hit = high >= state["sl"]

                # Conservative same-bar ordering: stop first.
                if tp_hit and sl_hit:
                    _resolve(key, state, "AMBIGUOUS_STOP_FIRST", state["sl"], log)
                    _ACTIVE.pop(key, None)
                    break
                if sl_hit:
                    _resolve(key, state, "SL", state["sl"], log)
                    _ACTIVE.pop(key, None)
                    break
                if tp_hit:
                    _resolve(key, state, "TP", state["tp"], log)
                    _ACTIVE.pop(key, None)
                    break
                if state["bars"] >= _MAX_BARS:
                    last = _finite(bar.get("c"), state["entry"])
                    _resolve(key, state, "TIMEOUT", last, log)
                    _ACTIVE.pop(key, None)
                    break
                _schedule_persist(key, state, "OPEN", log)


async def observe_nexus_counterfactual(engine, sig, k15, k1h, k4h, log,
                                        timeout_s: float = 10.0) -> None:
    """Ask the exact production NEXUS validator about a session-rejected signal."""
    direction = str(getattr(sig, "direction", "")).upper()
    closed = _closed_15m(k15, k1h, k4h)
    if direction not in {"LONG", "SHORT"} or not closed or closed[-1].get("ts") is None:
        return
    key = _state_key(str(getattr(sig, "symbol", "")), direction, closed[-1]["ts"])

    with _LOCK:
        state = _ACTIVE.get(key)
        if not state or state.get("nexus_status") != "NOT_CHECKED":
            return
        state["nexus_status"] = "IN_PROGRESS"
        state["nexus_schedule_status"] = "RUNNING"
        state["nexus_schedule_reason"] = ""
        _METRICS["nexus_counterfactuals"] += 1
        persist_state = dict(state)
    _DEFERRED_NEXUS.pop(key, None)
    _schedule_persist(key, persist_state, "OPEN", log)

    try:
        decision = await asyncio.wait_for(
            engine._nexus_validate(sig), timeout=max(0.1, float(timeout_s))
        )
        from bot.nexus_types import decision_validation_error
        schema_error = decision_validation_error(
            decision,
            sig.symbol,
            sig.direction,
            sig.entry,
            sig.sl,
            sig.tp,
        )
        approved = schema_error is None and decision.execution_allowed is True
        reason = schema_error or _decision_reason(decision)
        with _LOCK:
            state = _ACTIVE.get(key)
            if not state:
                return
            state["nexus_status"] = "APPROVED" if approved else "VETOED"
            state["nexus_approved"] = bool(approved)
            state["nexus_reason"] = reason
            state["nexus_setup_quality"] = _finite(getattr(decision, "setup_quality", 0.0))
            state["nexus_confidence"] = _finite(getattr(decision, "confidence", 0.0))
            state["nexus_regime"] = str(getattr(decision, "market_regime", "UNKNOWN"))
            _METRICS["nexus_approved" if approved else "nexus_vetoed"] += 1
            persist_state = dict(state)
        _schedule_persist(key, persist_state, "OPEN", log)

        log.info(
            "[SESSION_PENALTY_NEXUS_SHADOW] symbol=%s side=%s session=%s "
            "penalty=%+d score=%s->%s nexus_status=%s setup_quality=%.2f "
            "confidence=%.2f nexus_regime=%s reason=%s exact_live_validator=true "
            "durable=true production_policy_unchanged=true decision_effect=NONE "
            "execution_effect=NONE",
            state.get("symbol"), state.get("direction"), state.get("session"),
            int(state.get("penalty", 0)), state.get("base_score"),
            state.get("adjusted_score"), state.get("nexus_status"),
            state.get("nexus_setup_quality", 0.0), state.get("nexus_confidence", 0.0),
            state.get("nexus_regime"), str(reason)[:240],
        )
    except asyncio.TimeoutError:
        with _LOCK:
            state = _ACTIVE.get(key)
            if state:
                state["nexus_status"] = "TIMEOUT"
                state["nexus_approved"] = False
                state["nexus_reason"] = "ai_timeout"
                _METRICS["nexus_timeout"] += 1
                persist_state = dict(state)
            else:
                persist_state = None
        if persist_state:
            _schedule_persist(key, persist_state, "OPEN", log)
        log.info(
            "[SESSION_PENALTY_NEXUS_SHADOW] symbol=%s side=%s nexus_status=TIMEOUT "
            "exact_live_validator=true durable=true production_policy_unchanged=true "
            "decision_effect=NONE execution_effect=NONE",
            getattr(sig, "symbol", "UNKNOWN"), direction,
        )
    except Exception as exc:
        with _LOCK:
            state = _ACTIVE.get(key)
            if state:
                state["nexus_status"] = "ERROR"
                state["nexus_approved"] = False
                state["nexus_reason"] = type(exc).__name__
                _METRICS["nexus_error"] += 1
                persist_state = dict(state)
            else:
                persist_state = None
        if persist_state:
            _schedule_persist(key, persist_state, "OPEN", log)
        log.info(
            "[SESSION_PENALTY_NEXUS_SHADOW] symbol=%s side=%s nexus_status=ERROR "
            "error=%s exact_live_validator=true durable=true "
            "production_policy_unchanged=true decision_effect=NONE execution_effect=NONE",
            getattr(sig, "symbol", "UNKNOWN"), direction, type(exc).__name__,
        )


def _defer_nexus_schedule(key: str, sig, k15, k1h, k4h, reason: str, log) -> None:
    """Keep an unscheduled counterfactual recoverable in memory and telemetry."""
    first_defer = key not in _DEFERRED_NEXUS
    _DEFERRED_NEXUS[key] = (
        sig,
        list(k15 or []),
        list(k1h or []),
        list(k4h or []),
    )
    with _LOCK:
        state = _ACTIVE.get(key)
        if state:
            state["nexus_schedule_status"] = (
                "DEFERRED_NO_LOOP" if reason == "no_running_loop"
                else "DEFERRED_ENGINE_UNAVAILABLE" if reason == "engine_unavailable"
                else "DEFERRED_SCHEDULE_ERROR"
            )
            state["nexus_schedule_reason"] = reason
            persist_state = dict(state)
        else:
            persist_state = None
        if first_defer:
            _METRICS["nexus_schedule_deferred"] += 1
    if persist_state:
        _schedule_persist(key, persist_state, "OPEN", log)
    log.warning(
        "[SESSION_PENALTY_NEXUS_SCHEDULE] candidate=%s status=DEFERRED reason=%s "
        "recoverable=true production_policy_unchanged=true decision_effect=NONE "
        "execution_effect=NONE",
        key, reason,
    )


def _schedule_nexus_counterfactual(key: str, sig, k15, k1h, k4h, log,
                                   *, recovery: bool = False) -> bool:
    """Schedule the read-only NEXUS counterfactual or retain it for retry."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _defer_nexus_schedule(key, sig, k15, k1h, k4h, "no_running_loop", log)
        return False

    engine = _runtime_engine()
    if engine is None:
        _defer_nexus_schedule(key, sig, k15, k1h, k4h, "engine_unavailable", log)
        return False

    coro = observe_nexus_counterfactual(
        engine, sig, k15, k1h, k4h, log, timeout_s=10.0
    )
    try:
        loop.create_task(coro)
    except Exception as exc:
        coro.close()
        with _LOCK:
            _METRICS["nexus_schedule_error"] += 1
        _defer_nexus_schedule(
            key, sig, k15, k1h, k4h,
            f"schedule_error:{type(exc).__name__}", log,
        )
        return False

    _DEFERRED_NEXUS.pop(key, None)
    with _LOCK:
        state = _ACTIVE.get(key)
        if state:
            state["nexus_schedule_status"] = (
                "RECOVERED_SCHEDULED" if recovery else "SCHEDULED"
            )
            state["nexus_schedule_reason"] = ""
            persist_state = dict(state)
        else:
            persist_state = None
        if recovery:
            _METRICS["nexus_schedule_recovered"] += 1
    if persist_state:
        _schedule_persist(key, persist_state, "OPEN", log)
    log.info(
        "[SESSION_PENALTY_NEXUS_SCHEDULE] candidate=%s status=%s "
        "recoverable=true production_policy_unchanged=true decision_effect=NONE "
        "execution_effect=NONE",
        key, "RECOVERED_SCHEDULED" if recovery else "SCHEDULED",
    )
    return True


def _drain_deferred_nexus(log) -> None:
    """Retry previously deferred counterfactuals once runtime scheduling is available."""
    if not _DEFERRED_NEXUS:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if _runtime_engine() is None:
        return

    for key, payload in list(_DEFERRED_NEXUS.items()):
        with _LOCK:
            state = _ACTIVE.get(key)
            pending = bool(state and state.get("nexus_status") == "NOT_CHECKED")
        if not pending:
            _DEFERRED_NEXUS.pop(key, None)
            continue
        sig, k15, k1h, k4h = payload
        _schedule_nexus_counterfactual(
            key, sig, k15, k1h, k4h, log, recovery=True
        )


def observe(symbol: str, k15, k1h, k4h, *, production_result,
            min_score: int, log) -> None:
    """Observe one canonical Analyzer evaluation and return no trading value."""
    _schedule_restore(log)
    _drain_deferred_nexus(log)
    _update_outcomes(symbol, k15, k1h, k4h, log)
    if production_result is None:
        return

    try:
        from bot.engine import TradingEngine
        session = TradingEngine._get_market_session()
        penalty = int(TradingEngine._SESSION_PENALTY.get(session, {}).get(symbol, 0))
    except Exception as exc:
        log.debug(
            "[SESSION_PENALTY_SHADOW] policy_read_failed error=%s "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )
        return

    base_score = int(getattr(production_result, "score", 0) or 0)
    adjusted = max(0, base_score + penalty)
    if penalty >= 0 or base_score < int(min_score) or adjusted >= int(min_score):
        return

    direction = str(getattr(production_result, "direction", "")).upper()
    entry = _finite(getattr(production_result, "entry", 0.0))
    sl = _finite(getattr(production_result, "sl", 0.0))
    tp = _finite(getattr(production_result, "tp", 0.0))
    if direction not in {"LONG", "SHORT"} or min(entry, sl, tp) <= 0:
        return

    closed = _closed_15m(k15, k1h, k4h)
    if not closed or closed[-1].get("ts") is None:
        return
    bar_ts = closed[-1]["ts"]
    key = _state_key(symbol, direction, bar_ts)

    with _LOCK:
        if key in _SEEN:
            return
        _SEEN.add(key)
        state = {
            "symbol": symbol,
            "direction": direction,
            "session": session,
            "penalty": penalty,
            "base_score": base_score,
            "adjusted_score": adjusted,
            "min_score": int(min_score),
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "opened_bar_ts": bar_ts,
            "last_bar_ts": bar_ts,
            "created_epoch": time.time(),
            "bars": 0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "entry_type": str(getattr(production_result, "entry_type", "UNKNOWN")),
            "regime": str(getattr(production_result, "regime", "UNKNOWN")),
            "nexus_status": "NOT_CHECKED",
            "nexus_approved": None,
            "nexus_reason": "",
            "nexus_setup_quality": 0.0,
            "nexus_confidence": 0.0,
            "nexus_regime": "UNKNOWN",
            "nexus_schedule_status": "PENDING",
            "nexus_schedule_reason": "",
        }
        _ACTIVE[key] = state
        _METRICS["unique"] += 1
        _METRICS["eligible"] += 1
        _METRICS["sessions"][session] += 1
        _METRICS["symbols"][symbol] += 1

    _schedule_persist(key, state, "OPEN", log)
    log.info(
        "[SESSION_PENALTY_SHADOW] symbol=%s side=%s session=%s penalty=%+d "
        "score=%d->%d min=%d entry_type=%s regime=%s tracking_bars=%d "
        "durable=true production_policy_unchanged=true decision_effect=NONE "
        "execution_effect=NONE",
        symbol, direction, session, penalty, base_score, adjusted, int(min_score),
        state["entry_type"], state["regime"], _MAX_BARS,
    )

    _schedule_nexus_counterfactual(
        key, production_result, k15, k1h, k4h, log
    )


def snapshot() -> dict:
    with _LOCK:
        return {
            "unique": int(_METRICS["unique"]),
            "eligible": int(_METRICS["eligible"]),
            "resolved": int(_METRICS["resolved"]),
            "restored": int(_METRICS["restored"]),
            "active": len(_ACTIVE),
            "deferred_nexus": len(_DEFERRED_NEXUS),
            "outcomes": dict(_METRICS["outcomes"]),
            "sessions": dict(_METRICS["sessions"]),
            "symbols": dict(_METRICS["symbols"]),
            "nexus_counterfactuals": int(_METRICS["nexus_counterfactuals"]),
            "nexus_approved": int(_METRICS["nexus_approved"]),
            "nexus_vetoed": int(_METRICS["nexus_vetoed"]),
            "nexus_timeout": int(_METRICS["nexus_timeout"]),
            "nexus_error": int(_METRICS["nexus_error"]),
            "nexus_schedule_deferred": int(_METRICS["nexus_schedule_deferred"]),
            "nexus_schedule_recovered": int(_METRICS["nexus_schedule_recovered"]),
            "nexus_schedule_error": int(_METRICS["nexus_schedule_error"]),
            "persistence_restore_complete": bool(_RESTORE_COMPLETE),
        }
