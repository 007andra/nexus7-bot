"""Read-only calibration for canonical strategy scores just below the live floor.

Production keeps its score floor unchanged. This observer studies only canonical
HOLDs with scores in [floor-5, floor-1] that would pass every *other* strategy
filter (RSI, volume, 15m alignment, entry trigger, R:R and costs). It reconstructs
the canonical Signal, asks the exact production NEXUS validator what it would
have decided, persists the evidence, and follows confirmed 15m TP/SL outcomes.
It never returns a Signal to the engine or calls an execution/order route.
"""
from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from collections import Counter

import numpy as np

from bot.execution_model import estimated_round_trip_cost_pct
from bot.nexus_decision_consistency import closed_mtf
from bot import score_floor_shadow_persistence as persistence

_MAX_BARS = 16
_LOCK = threading.Lock()
_SEEN: set[str] = set()
_ACTIVE: dict[str, dict] = {}
_RESTORE_SCHEDULED = False
_RESTORE_COMPLETE = False
_METRICS = {
    "unique": 0,
    "eligible": 0,
    "resolved": 0,
    "restored": 0,
    "outcomes": Counter(),
    "scores": Counter(),
    "nexus_approved": 0,
    "nexus_vetoed": 0,
    "nexus_timeout": 0,
    "nexus_error": 0,
}


def _finite(value, default=0.0):
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _state_key(symbol: str, direction: str, bar_ts) -> str:
    return f"{symbol}:{str(direction).upper()}:{bar_ts}"


def _ts(bar):
    if not isinstance(bar, dict):
        return None
    return bar.get("ts") or bar.get("time") or bar.get("timestamp")


def _runtime_engine():
    main_module = sys.modules.get("main")
    app = getattr(main_module, "app", None) if main_module else None
    state = getattr(app, "state", None) if app is not None else None
    return getattr(state, "engine", None) if state is not None else None


def _decision_reason(decision) -> str:
    reasoning = getattr(decision, "reasoning", None) or []
    if reasoning:
        return str(reasoning[-1])[:500]
    warnings = getattr(decision, "warnings", None) or []
    if warnings:
        return str(warnings[-1])[:500]
    return str(getattr(decision, "decision", "UNKNOWN"))[:500]


def _ga(klines):
    return (
        [float(k["c"]) for k in klines],
        [float(k["h"]) for k in klines],
        [float(k["l"]) for k in klines],
        [float(k["o"]) for k in klines],
        [float(k.get("v", 0) or 0) for k in klines],
    )


def _atr_pair(strategy, highs, lows, closes):
    arr = strategy.atr(highs, lows, closes)
    now = float(arr[-1])
    avg = float(np.mean(arr[-20:])) if len(arr) >= 20 else now
    return now, avg


def _schedule_persist(key: str, state: dict, status: str, log) -> None:
    try:
        asyncio.get_running_loop().create_task(
            persistence.save_state(key, dict(state), status=status, log=log)
        )
    except RuntimeError as exc:
        log.debug(
            "[SCORE_FLOOR_SHADOW_PERSISTENCE] schedule_skipped reason=no_running_loop "
            "error=%s execution_effect=NONE",
            type(exc).__name__,
        )
        return
    except Exception as exc:
        log.debug(
            "[SCORE_FLOOR_SHADOW_PERSISTENCE] schedule_failed error=%s "
            "execution_effect=NONE",
            type(exc).__name__,
        )


async def _restore(log) -> None:
    global _RESTORE_COMPLETE, _RESTORE_SCHEDULED
    try:
        rows = await persistence.load_open(log)
        if rows is None:
            _RESTORE_SCHEDULED = False
            return
        restored = 0
        with _LOCK:
            for key, state in rows:
                if key in _ACTIVE or int(_finite(state.get("bars"), 0)) >= _MAX_BARS:
                    continue
                _SEEN.add(key)
                _ACTIVE[key] = dict(state)
                restored += 1
            _METRICS["restored"] += restored
        _RESTORE_COMPLETE = True
        log.info(
            "[SCORE_FLOOR_SHADOW_PERSISTENCE] restore_complete=true restored=%d "
            "active=%d production_floor_unchanged=true execution_effect=NONE",
            restored, len(_ACTIVE),
        )
    except Exception as exc:
        _RESTORE_SCHEDULED = False
        log.debug(
            "[SCORE_FLOOR_SHADOW_PERSISTENCE] restore_failed error=%s "
            "execution_effect=NONE",
            type(exc).__name__,
        )


def _schedule_restore(log) -> None:
    global _RESTORE_SCHEDULED
    if _RESTORE_COMPLETE or _RESTORE_SCHEDULED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        log.debug(
            "[SCORE_FLOOR_SHADOW_PERSISTENCE] restore_skipped reason=no_running_loop "
            "error=%s execution_effect=NONE",
            type(exc).__name__,
        )
        return
    _RESTORE_SCHEDULED = True
    loop.create_task(_restore(log))


def _resolve(key: str, state: dict, outcome: str, exit_price: float, log) -> None:
    entry = _finite(state.get("entry"))
    side = 1.0 if state.get("direction") == "LONG" else -1.0
    gross_pct = side * ((float(exit_price) - entry) / entry) * 100.0 if entry > 0 else 0.0
    net_pct = gross_pct - estimated_round_trip_cost_pct(state.get("symbol", "BTCUSDT"))
    risk = abs(entry - _finite(state.get("sl")))
    signed_move = side * (float(exit_price) - entry)
    r_multiple = signed_move / risk if risk > 0 else 0.0
    state["outcome"] = outcome
    state["exit"] = float(exit_price)
    state["gross_pct"] = round(gross_pct, 5)
    state["net_pct"] = round(net_pct, 5)
    state["r_multiple"] = round(r_multiple, 4)
    _METRICS["resolved"] += 1
    _METRICS["outcomes"][outcome] += 1
    _schedule_persist(key, state, "RESOLVED", log)
    log.info(
        "[SCORE_FLOOR_SHADOW_OUTCOME] symbol=%s side=%s score=%s floor=%s "
        "nexus_status=%s nexus_approved=%s outcome=%s net_pct=%.4f r=%.3f "
        "mfe=%.4f mae=%.4f bars=%d durable=true production_floor_unchanged=true "
        "execution_effect=NONE",
        state.get("symbol"), state.get("direction"), state.get("score"),
        state.get("production_floor"), state.get("nexus_status"),
        state.get("nexus_approved"), outcome, state.get("net_pct", 0.0),
        state.get("r_multiple", 0.0), state.get("mfe_pct", 0.0),
        state.get("mae_pct", 0.0), int(state.get("bars", 0)),
    )


def _update_outcomes(symbol, k15, k1h, k4h, log) -> None:
    closed15, _, _ = closed_mtf(k15, k1h, k4h)
    if not closed15:
        return
    with _LOCK:
        keys = [key for key, state in _ACTIVE.items() if state.get("symbol") == symbol]
    for key in keys:
        with _LOCK:
            state = _ACTIVE.get(key)
            if not state:
                continue
            last_ts = state.get("last_bar_ts")
        for bar in closed15:
            bar_ts = _ts(bar)
            if bar_ts is None or (last_ts is not None and bar_ts <= last_ts):
                continue
            try:
                hi, lo, close = float(bar["h"]), float(bar["l"]), float(bar["c"])
            except (TypeError, ValueError, KeyError):
                continue
            with _LOCK:
                state = _ACTIVE.get(key)
                if not state:
                    break
                state["last_bar_ts"] = bar_ts
                state["bars"] = int(state.get("bars", 0)) + 1
                entry, sl, tp = float(state["entry"]), float(state["sl"]), float(state["tp"])
                if state["direction"] == "LONG":
                    favorable = max(0.0, hi - entry) / entry * 100.0
                    adverse = max(0.0, entry - lo) / entry * 100.0
                    hit_tp, hit_sl = hi >= tp, lo <= sl
                else:
                    favorable = max(0.0, entry - lo) / entry * 100.0
                    adverse = max(0.0, hi - entry) / entry * 100.0
                    hit_tp, hit_sl = lo <= tp, hi >= sl
                state["mfe_pct"] = max(_finite(state.get("mfe_pct")), favorable)
                state["mae_pct"] = max(_finite(state.get("mae_pct")), adverse)
                bars = state["bars"]
                snapshot = dict(state)
            last_ts = bar_ts
            if hit_sl and hit_tp:
                _resolve(key, snapshot, "AMBIGUOUS_STOP_FIRST", sl, log)
            elif hit_sl:
                _resolve(key, snapshot, "SL", sl, log)
            elif hit_tp:
                _resolve(key, snapshot, "TP", tp, log)
            elif bars >= _MAX_BARS:
                _resolve(key, snapshot, "TIMEOUT_4H", close, log)
            else:
                _schedule_persist(key, snapshot, "OPEN", log)
                continue
            with _LOCK:
                _ACTIVE.pop(key, None)
            break


async def _nexus_counterfactual(engine, sig, key: str, log, timeout_s: float = 10.0) -> None:
    with _LOCK:
        state = _ACTIVE.get(key)
        if not state or state.get("nexus_status") != "NOT_CHECKED":
            return
        state["nexus_status"] = "IN_PROGRESS"
        snapshot = dict(state)
    _schedule_persist(key, snapshot, "OPEN", log)
    try:
        decision = await asyncio.wait_for(
            engine._nexus_validate(sig), timeout=max(0.1, float(timeout_s))
        )
        from bot.nexus_types import decision_validation_error
        schema_error = decision_validation_error(
            decision, sig.symbol, sig.direction, sig.entry, sig.sl, sig.tp
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
            state["nexus_rr"] = _finite(getattr(decision, "risk_reward", 0.0))
            state["nexus_ev"] = _finite(getattr(decision, "expected_value", 0.0))
            _METRICS["nexus_approved" if approved else "nexus_vetoed"] += 1
            snapshot = dict(state)
        _schedule_persist(key, snapshot, "OPEN", log)
        log.info(
            "[SCORE_FLOOR_NEXUS_SHADOW] symbol=%s side=%s score=%s floor=%s "
            "nexus_status=%s setup_quality=%.2f confidence=%.2f rr=%.4f ev=%.4f "
            "reason=%s exact_live_validator=true production_floor_unchanged=true "
            "execution_effect=NONE",
            state.get("symbol"), state.get("direction"), state.get("score"),
            state.get("production_floor"), state.get("nexus_status"),
            state.get("nexus_setup_quality", 0.0), state.get("nexus_confidence", 0.0),
            state.get("nexus_rr", 0.0), state.get("nexus_ev", 0.0), str(reason)[:240],
        )
    except asyncio.TimeoutError:
        with _LOCK:
            state = _ACTIVE.get(key)
            if state:
                state["nexus_status"] = "TIMEOUT"
                state["nexus_approved"] = False
                state["nexus_reason"] = "ai_timeout"
                _METRICS["nexus_timeout"] += 1
                snapshot = dict(state)
            else:
                snapshot = None
        if snapshot:
            _schedule_persist(key, snapshot, "OPEN", log)
    except Exception as exc:
        with _LOCK:
            state = _ACTIVE.get(key)
            if state:
                state["nexus_status"] = "ERROR"
                state["nexus_approved"] = False
                state["nexus_reason"] = type(exc).__name__
                _METRICS["nexus_error"] += 1
                snapshot = dict(state)
            else:
                snapshot = None
        if snapshot:
            _schedule_persist(key, snapshot, "OPEN", log)


def observe(symbol, k15, k1h, k4h, *, production_result, min_score, fee_mult, log) -> None:
    """Observe one strategy call and never alter/return its production result."""
    _schedule_restore(log)
    _update_outcomes(symbol, k15, k1h, k4h, log)
    if production_result is not None:
        return
    try:
        from bot import strategy

        closed15, closed1h, closed4h = closed_mtf(k15, k1h, k4h)
        if len(closed15) < 20 or len(closed1h) < 15 or len(closed4h) < 10:
            return
        c15, h15, l15, o15, v15 = _ga(closed15)
        c1, h1, l1, o1, v1 = _ga(closed1h)
        c4, h4, l4, o4, v4 = _ga(closed4h)
        a15, aa15 = _atr_pair(strategy, h15, l15, c15)
        a1, aa1 = _atr_pair(strategy, h1, l1, c1)
        a4, aa4 = _atr_pair(strategy, h4, l4, c4)

        regime = strategy.detect_regime(c4, h4, l4, a4)
        if regime in ("COMPRESSED", "RANGING", "CHOPPY"):
            return
        e20_4 = float(strategy.ema(c4, 20)[-1]); e50_4 = float(strategy.ema(c4, 50)[-1])
        e20_1 = float(strategy.ema(c1, 20)[-1]); e50_1 = float(strategy.ema(c1, 50)[-1])
        bull4 = e20_4 > e50_4 and c4[-1] > e20_4
        bear4 = e20_4 < e50_4 and c4[-1] < e20_4
        bull1 = e20_1 > e50_1 and c1[-1] > e20_1
        bear1 = e20_1 < e50_1 and c1[-1] < e20_1
        if bull4 and bull1:
            direction = "LONG"
        elif bear4 and bear1:
            direction = "SHORT"
        else:
            return

        s4 = strategy.score_tf(c4, h4, l4, o4, v4, direction, a4, aa4)
        s1 = strategy.score_tf(c1, h1, l1, o1, v1, direction, a1, aa1)
        s15 = strategy.score_tf(c15, h15, l15, o15, v15, direction, a15, aa15)
        if not all(bool(s.get("ok")) for s in (s4, s1, s15)):
            return
        combined = round(s4["total"] * 0.25 + s1["total"] * 0.30 + s15["total"] * 0.45)
        floor = int(min_score)
        study_floor = max(0, floor - 5)
        if not (study_floor <= combined < floor):
            return

        # From this point onward every canonical downstream gate must pass.
        # Otherwise score is not the sole production blocker and the setup is
        # intentionally excluded from this cohort.
        rsi_v = float(s15.get("rsi_v", 50.0))
        vol_r = float(s15.get("vol_r", 0.0))
        if rsi_v > 92 or rsi_v < 8 or not math.isfinite(vol_r) or vol_r < 0.40:
            return
        if not s15.get("aligned") and regime not in ("TRENDING_UP", "TRENDING_DOWN"):
            return
        entry_ok, entry_type = strategy.detect_entry(c15, h15, l15, o15, v15, direction, a15)
        if not entry_ok:
            return

        if entry_type == "BOS_BREAK":
            sl_mult, tp_mult = 1.2, 3.6
        elif entry_type == "MOMENTUM":
            sl_mult, tp_mult = 1.5, 3.0
        else:
            sl_mult, tp_mult = 2.0, 4.0
        price = float(c15[-1])
        sl_atr = max(a15, a1 * 0.5)
        if direction == "LONG":
            raw_sl, raw_tp = price - sl_atr * sl_mult, price + sl_atr * tp_mult
        else:
            raw_sl, raw_tp = price + sl_atr * sl_mult, price - sl_atr * tp_mult
        rr = strategy._rr_from_unrounded_levels(price, raw_sl, raw_tp)
        if rr < strategy.cfg.MIN_RR_RATIO:
            return
        sl, tp = round(raw_sl, 6), round(raw_tp, 6)
        cost_pct = strategy.TOTAL_COST * 100.0
        move_to_tp = abs(tp - price) / price * 100.0
        if move_to_tp < cost_pct * float(fee_mult):
            return
        expected_net = move_to_tp - cost_pct

        reasons = [
            f"4H:{s4['total']}", f"1H:{s1['total']}", f"15M:{s15['total']}",
            f"ADX{s15['adx_v']:.0f}", f"RR{rr:.1f}", f"ENTRY:{entry_type}",
            "SCORE_FLOOR_SHADOW",
        ]
        sig = strategy.Signal(
            symbol=symbol, direction=direction, entry=price, sl=sl, tp=tp,
            confidence=min(0.97, combined / 100.0), reason=" | ".join(reasons),
            score=int(combined), tf_4h=s4["summary"], tf_1h=s1["summary"],
            tf_15m=s15["summary"], expected_pnl=round(expected_net, 3),
            total_fees=round(cost_pct, 4), entry_type=entry_type, regime=regime,
        )
        bar_ts = _ts(closed15[-1])
        if bar_ts is None:
            return
        key = _state_key(symbol, direction, bar_ts)
        with _LOCK:
            if key in _SEEN:
                return
            _SEEN.add(key)
            state = {
                "symbol": symbol,
                "direction": direction,
                "score": int(combined),
                "production_floor": floor,
                "floors_that_would_admit": list(range(study_floor, int(combined) + 1)),
                "score_4h": int(s4["total"]),
                "score_1h": int(s1["total"]),
                "score_15m": int(s15["total"]),
                "rsi_15m": round(rsi_v, 4),
                "volume_ratio_15m": round(vol_r, 4),
                "entry_type": entry_type,
                "regime": regime,
                "entry": price,
                "sl": sl,
                "tp": tp,
                "rr": round(rr, 4),
                "expected_net_pct": round(expected_net, 5),
                "opened_bar_ts": bar_ts,
                "last_bar_ts": bar_ts,
                "created_epoch": time.time(),
                "bars": 0,
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "nexus_status": "NOT_CHECKED",
                "nexus_approved": None,
                "nexus_reason": "",
            }
            _ACTIVE[key] = state
            _METRICS["unique"] += 1
            _METRICS["eligible"] += 1
            _METRICS["scores"][int(combined)] += 1
        _schedule_persist(key, state, "OPEN", log)
        log.info(
            "[SCORE_FLOOR_SHADOW] symbol=%s side=%s score=%d production_floor=%d "
            "vol=%.3fx entry_type=%s rr=%.3f expected_net=%.4f "
            "other_strategy_gates=PASS durable=true production_floor_unchanged=true "
            "decision_effect=NONE execution_effect=NONE",
            symbol, direction, int(combined), floor, vol_r, entry_type, rr,
            expected_net,
        )

        engine = _runtime_engine()
        if engine is not None:
            asyncio.get_running_loop().create_task(
                _nexus_counterfactual(engine, sig, key, log, timeout_s=10.0)
            )
    except Exception as exc:
        log.debug(
            "[SCORE_FLOOR_SHADOW] observer_error=%s production_floor_unchanged=true "
            "execution_effect=NONE",
            type(exc).__name__,
        )


def snapshot() -> dict:
    with _LOCK:
        return {
            "unique": int(_METRICS["unique"]),
            "eligible": int(_METRICS["eligible"]),
            "resolved": int(_METRICS["resolved"]),
            "restored": int(_METRICS["restored"]),
            "active": len(_ACTIVE),
            "outcomes": dict(_METRICS["outcomes"]),
            "scores": dict(_METRICS["scores"]),
            "nexus_approved": int(_METRICS["nexus_approved"]),
            "nexus_vetoed": int(_METRICS["nexus_vetoed"]),
            "nexus_timeout": int(_METRICS["nexus_timeout"]),
            "nexus_error": int(_METRICS["nexus_error"]),
            "persistence_restore_complete": bool(_RESTORE_COMPLETE),
        }
