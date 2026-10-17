"""Read-only counterfactual for the canonical 15m volume gate.

Production keeps the existing ``vol_r >= 0.40`` requirement. This observer only
studies setups that would otherwise survive the canonical strategy path, then
tracks their forward OHLC outcome for alternative minimum-volume policies
0.25/0.30/0.35/0.40.

It never returns a Signal, calls NEXUS, changes a threshold, sizes a position,
or touches the exchange. Same-bar TP+SL ambiguity is resolved stop-first so the
counterfactual stays conservative.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import Counter, deque

import numpy as np

from bot.execution_model import estimated_round_trip_cost_pct


_THRESHOLDS = (0.25, 0.30, 0.35, 0.40)
_MAX_BARS = 16  # four hours of confirmed 15m bars
_MAX_ACTIVE = 1000
_LOCK = threading.Lock()
_SEEN: set[tuple] = set()
_SEEN_ORDER = deque(maxlen=5000)
_ACTIVE: dict[str, dict] = {}
_METRICS = {
    "unique": 0,
    "eligible": 0,
    "resolved": 0,
    "outcomes": Counter(),
    "thresholds": {
        t: {"eligible": 0, "resolved": 0, "tp": 0, "sl": 0,
            "timeout": 0, "net_sum": 0.0}
        for t in _THRESHOLDS
    },
}


def _closed(klines):
    data = list(klines or [])
    return data[:-1] if len(data) > 20 else data


def _ts(bar):
    if not isinstance(bar, dict):
        return None
    return bar.get("ts") or bar.get("time") or bar.get("timestamp")


def _key(symbol, direction, closed_ts):
    return (str(symbol), str(direction), closed_ts)


def _state_id(key):
    return hashlib.sha256("|".join(map(str, key)).encode()).hexdigest()[:16]


def _remember(key):
    with _LOCK:
        if key in _SEEN:
            return False
        if len(_SEEN_ORDER) == _SEEN_ORDER.maxlen:
            old = _SEEN_ORDER.popleft()
            _SEEN.discard(old)
        _SEEN.add(key)
        _SEEN_ORDER.append(key)
        _METRICS["unique"] += 1
        return True


def _ga(kl):
    return (
        [float(k["c"]) for k in kl],
        [float(k["h"]) for k in kl],
        [float(k["l"]) for k in kl],
        [float(k["o"]) for k in kl],
        [float(k.get("v", 0) or 0) for k in kl],
    )


def _atr_pair(strategy, highs, lows, closes):
    arr = strategy.atr(highs, lows, closes)
    now = float(arr[-1])
    avg = float(np.mean(arr[-20:])) if len(arr) >= 20 else now
    return now, avg


def _emit(log, tag, payload):
    log.info("[%s] %s", tag, json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _update_outcomes(symbol, k15, log):
    closed = _closed(k15)
    if not closed:
        return
    bar = closed[-1]
    bar_ts = _ts(bar)
    try:
        hi, lo, close = float(bar["h"]), float(bar["l"]), float(bar["c"])
    except (TypeError, ValueError, KeyError):
        return

    with _LOCK:
        ids = [sid for sid, state in _ACTIVE.items() if state["symbol"] == symbol]

    for sid in ids:
        with _LOCK:
            state = _ACTIVE.get(sid)
            if not state or state.get("last_bar_ts") == bar_ts:
                continue
            if state.get("opened_bar_ts") == bar_ts:
                state["last_bar_ts"] = bar_ts
                continue
            state["last_bar_ts"] = bar_ts
            state["bars"] += 1
            entry, sl, tp = state["entry"], state["sl"], state["tp"]
            if state["direction"] == "LONG":
                favorable = max(0.0, hi - entry)
                adverse = max(0.0, entry - lo)
                hit_tp, hit_sl = hi >= tp, lo <= sl
            else:
                favorable = max(0.0, entry - lo)
                adverse = max(0.0, hi - entry)
                hit_tp, hit_sl = lo <= tp, hi >= sl
            state["mfe_pct"] = max(state["mfe_pct"], favorable / entry * 100.0)
            state["mae_pct"] = max(state["mae_pct"], adverse / entry * 100.0)
            bars = state["bars"]

        outcome = None
        if hit_sl and hit_tp:
            outcome = "AMBIGUOUS_STOP_FIRST"
        elif hit_sl:
            outcome = "SL"
        elif hit_tp:
            outcome = "TP"
        elif bars >= _MAX_BARS:
            outcome = "TIMEOUT_4H"
        if outcome is None:
            continue

        exit_px = sl if outcome in {"SL", "AMBIGUOUS_STOP_FIRST"} else tp if outcome == "TP" else close
        side = 1.0 if state["direction"] == "LONG" else -1.0
        gross_pct = side * ((exit_px - entry) / entry) * 100.0
        cost_pct = estimated_round_trip_cost_pct(symbol)
        net_pct = gross_pct - cost_pct
        payload = {
            "state_id": sid,
            "symbol": symbol,
            "direction": state["direction"],
            "outcome": outcome,
            "volume_ratio_15m": round(state["volume_ratio_15m"], 4),
            "thresholds_that_would_admit": state["thresholds_that_would_admit"],
            "entry": round(entry, 10),
            "exit": round(exit_px, 10),
            "mfe_pct": round(state["mfe_pct"], 5),
            "mae_pct": round(state["mae_pct"], 5),
            "gross_pct": round(gross_pct, 5),
            "net_pct_after_current_cost_model": round(net_pct, 5),
            "bars": bars,
            "execution_effect": "NONE",
        }
        with _LOCK:
            _ACTIVE.pop(sid, None)
            _METRICS["resolved"] += 1
            _METRICS["outcomes"][outcome] += 1
            for threshold in state["thresholds_that_would_admit"]:
                bucket = _METRICS["thresholds"][float(threshold)]
                bucket["resolved"] += 1
                if outcome == "TP":
                    bucket["tp"] += 1
                elif outcome in {"SL", "AMBIGUOUS_STOP_FIRST"}:
                    bucket["sl"] += 1
                else:
                    bucket["timeout"] += 1
                bucket["net_sum"] += net_pct
        _emit(log, "VOLUME_GATE_OUTCOME", payload)


def observe(symbol, k15, k1h, k4h, production_result, min_score, fee_mult, log):
    """Observe one canonical analyzer evaluation without changing its result."""
    try:
        _update_outcomes(symbol, k15, log)
        if production_result is not None:
            return

        from bot import strategy

        c15v = _closed(k15)
        c1hv = list(k1h or [])[:-1] if len(k1h or []) > 15 else list(k1h or [])
        c4hv = list(k4h or [])[:-1] if len(k4h or []) > 10 else list(k4h or [])
        if len(c15v) < 20 or len(c1hv) < 15 or len(c4hv) < 10:
            return

        c15, h15, l15, o15, v15 = _ga(c15v)
        c1, h1, l1, o1, v1 = _ga(c1hv)
        c4, h4, l4, o4, v4 = _ga(c4hv)
        a15, aa15 = _atr_pair(strategy, h15, l15, c15)
        a1, aa1 = _atr_pair(strategy, h1, l1, c1)
        a4, aa4 = _atr_pair(strategy, h4, l4, c4)

        regime = strategy.detect_regime(c4, h4, l4, a4)
        if regime not in ("TRENDING_UP", "TRENDING_DOWN"):
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
        if combined < int(min_score):
            return
        rsi_v = float(s15.get("rsi_v", 50.0))
        if rsi_v > 92 or rsi_v < 8:
            return

        vol_r = float(s15.get("vol_r", 0.0))
        if not math.isfinite(vol_r) or not (0.0 <= vol_r < 0.40):
            return
        admitted = [t for t in _THRESHOLDS if t < 0.40 and vol_r >= t]
        if not admitted:
            return

        entry_ok, entry_type = strategy.detect_entry(c15, h15, l15, o15, v15, direction, a15)
        adjusted_score = combined
        if not entry_ok:
            adjusted_score = max(0, combined - 5)
            if adjusted_score < int(min_score):
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
        move_to_tp = abs(tp - price) / price * 100.0
        if move_to_tp < strategy.TOTAL_COST * 100.0 * float(fee_mult):
            return

        closed_ts = _ts(c15v[-1])
        key = _key(symbol, direction, closed_ts)
        if not _remember(key):
            return
        sid = _state_id(key)
        payload = {
            "state_id": sid,
            "symbol": symbol,
            "direction": direction,
            "score": adjusted_score,
            "score_before_entry_penalty": combined,
            "volume_ratio_15m": round(vol_r, 4),
            "production_min_volume": 0.40,
            "thresholds_that_would_admit": admitted,
            "entry_type": entry_type,
            "entry": round(price, 10),
            "sl": round(sl, 10),
            "tp": round(tp, 10),
            "rr": round(rr, 4),
            "policy": "SHADOW_ONLY_VOLUME_GATE",
            "execution_effect": "NONE",
        }
        with _LOCK:
            _METRICS["eligible"] += 1
            for threshold in admitted:
                _METRICS["thresholds"][threshold]["eligible"] += 1
            if len(_ACTIVE) >= _MAX_ACTIVE:
                oldest = next(iter(_ACTIVE))
                _ACTIVE.pop(oldest, None)
            _ACTIVE[sid] = {
                **payload,
                "opened_bar_ts": closed_ts,
                "last_bar_ts": closed_ts,
                "bars": 0,
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
            }
        _emit(log, "VOLUME_GATE_SHADOW", payload)
    except Exception as exc:
        log.debug(
            "[VOLUME_GATE_SHADOW] observer_error=%s execution_effect=NONE",
            type(exc).__name__,
        )


def snapshot():
    with _LOCK:
        thresholds = {}
        for threshold, bucket in _METRICS["thresholds"].items():
            resolved = int(bucket["resolved"])
            thresholds[str(threshold)] = {
                "eligible": int(bucket["eligible"]),
                "resolved": resolved,
                "tp": int(bucket["tp"]),
                "sl": int(bucket["sl"]),
                "timeout": int(bucket["timeout"]),
                "avg_net_pct": round(bucket["net_sum"] / resolved, 5) if resolved else None,
            }
        return {
            "unique": int(_METRICS["unique"]),
            "eligible": int(_METRICS["eligible"]),
            "active": len(_ACTIVE),
            "resolved": int(_METRICS["resolved"]),
            "outcomes": dict(_METRICS["outcomes"]),
            "thresholds": thresholds,
        }
