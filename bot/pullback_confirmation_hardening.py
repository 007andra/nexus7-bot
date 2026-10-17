"""Require reversal evidence before emitting PULLBACK entries.

The canonical path remains closed-candle and requires >=4/5 reversal votes.
To reduce avoidable latency, a blocked PULLBACK may use the current 15m bar only
as a stricter acceleration path: 5/5 votes, price on the correct EMA20 side,
and no opposite structure/BOS. BOS_BREAK and MOMENTUM remain unchanged.

The intrabar path never bypasses NEXUS, risk, ownership, market-risk, sizing,
liquidation, TPSL, idempotency or final pre-dispatch market checks.
"""
from __future__ import annotations

import time
from typing import Any

from bot.indicators import ema, macd, rsi, smc_analysis


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _direction(value: Any) -> str:
    return str(getattr(value, "value", value)).upper()


def _confirmed_15m(k15: list) -> list:
    """Drop the disposable/current final bar used by the strategy stack."""
    data = list(k15 or [])
    return data[:-1] if len(data) > 20 else data


def _metrics_from_data(data: list, direction: str) -> dict:
    if len(data) < 30:
        return {"ok": False, "reason": "insufficient_15m_history"}

    closes = [_safe_float(k.get("c")) for k in data]
    opens = [_safe_float(k.get("o")) for k in data]
    highs = [_safe_float(k.get("h")) for k in data]
    lows = [_safe_float(k.get("l")) for k in data]

    c0, c1 = closes[-1], closes[-2]
    o0 = opens[-1]
    e9 = float(ema(closes, 9)[-1])
    e20 = float(ema(closes, 20)[-1])
    r = float(rsi(closes)[-1])
    _, _, hist = macd(closes)
    h0 = float(hist[-1])
    h1 = float(hist[-2])
    smc = smc_analysis(highs, lows, closes)

    d = _direction(direction)
    if d == "LONG":
        body_aligned = c0 > o0
        progress = c0 > c1
        fast_reclaim = c0 >= e9
        macd_turn = h0 > h1
        rsi_ok = r >= 40.0
        opposite_structure = str(smc.get("structure", "UNKNOWN")).upper() == "DOWNTREND"
        opposite_bos = bool(smc.get("bos")) and str(smc.get("bos_dir", "NONE")).upper() == "BEARISH"
        aligned_bos = bool(smc.get("bos")) and str(smc.get("bos_dir", "NONE")).upper() == "BULLISH"
        ema20_side = c0 >= e20
    elif d == "SHORT":
        body_aligned = c0 < o0
        progress = c0 < c1
        fast_reclaim = c0 <= e9
        macd_turn = h0 < h1
        rsi_ok = r <= 60.0
        opposite_structure = str(smc.get("structure", "UNKNOWN")).upper() == "UPTREND"
        opposite_bos = bool(smc.get("bos")) and str(smc.get("bos_dir", "NONE")).upper() == "BULLISH"
        aligned_bos = bool(smc.get("bos")) and str(smc.get("bos_dir", "NONE")).upper() == "BEARISH"
        ema20_side = c0 <= e20
    else:
        return {"ok": False, "reason": "invalid_direction"}

    votes = {
        "body_aligned": body_aligned,
        "price_progress": progress,
        "ema9_reclaim": fast_reclaim,
        "macd_turn": macd_turn,
        "rsi_recovered": rsi_ok,
    }
    vote_count = sum(bool(v) for v in votes.values())

    if opposite_bos:
        passed = False
        reason = "opposite_bos"
    elif opposite_structure and not aligned_bos:
        passed = False
        reason = "opposite_structure_not_reversed"
    else:
        passed = vote_count >= 4
        reason = "confirmed" if passed else "insufficient_reversal_votes"

    return {
        "ok": passed,
        "reason": reason,
        "votes": votes,
        "vote_count": vote_count,
        "structure": str(smc.get("structure", "UNKNOWN")),
        "opposite_structure": bool(opposite_structure),
        "opposite_bos": bool(opposite_bos),
        "aligned_bos": bool(aligned_bos),
        "bos": bool(smc.get("bos", False)),
        "bos_dir": str(smc.get("bos_dir", "NONE")),
        "choch": bool(smc.get("choch", False)),
        "rsi": round(r, 2),
        "macd_hist": round(h0, 8),
        "macd_prev": round(h1, 8),
        "close": c0,
        "ema9": round(e9, 8),
        "ema20": round(e20, 8),
        "ema20_side": bool(ema20_side),
    }


def _pullback_metrics(k15: list, direction: str) -> dict:
    return _metrics_from_data(_confirmed_15m(k15), direction)


def _intrabar_fast_metrics(k15: list, direction: str) -> dict:
    """Use current 15m bar only for a stricter 5/5 acceleration decision."""
    data = list(k15 or [])
    # A current/disposable bar must actually exist in addition to enough history.
    if len(data) < 31:
        return {"ok": False, "reason": "insufficient_intrabar_history"}
    metrics = _metrics_from_data(data, direction)
    if not metrics.get("ok"):
        return metrics

    strict = (
        int(metrics.get("vote_count", 0)) == 5
        and bool(metrics.get("ema20_side"))
        and not bool(metrics.get("opposite_structure"))
        and not bool(metrics.get("opposite_bos"))
    )
    out = dict(metrics)
    out["ok"] = strict
    out["reason"] = "intrabar_5of5_confirmed" if strict else "intrabar_not_strict_enough"
    return out


_STRATEGY_STOP_GEOMETRY_LAST: dict[tuple[str, str, str], str] = {}


def _strategy_setup_id(signal, epoch: float | None = None) -> str:
    existing = str(getattr(signal, "_bgx_setup_id", "") or "")
    if existing:
        return existing
    formation_timestamp = float(
        getattr(signal, "_bgx_formation_timestamp", 0.0) or 0.0
    )
    formation_bucket = int(
        getattr(signal, "_bgx_formation_bucket", 0) or 0
    )
    if formation_timestamp <= 0 or formation_bucket <= 0:
        formation_timestamp = float(time.time() if epoch is None else epoch)
        formation_bucket = int(formation_timestamp // 900)
    setup_id = (
        f"{getattr(signal, 'symbol', 'UNKNOWN')}:"
        f"{str(getattr(signal, 'direction', 'UNKNOWN')).upper()}:"
        f"{getattr(signal, 'entry_type', 'UNKNOWN')}:{formation_bucket}"
    )
    signal._bgx_setup_id = setup_id
    signal._bgx_formation_timestamp = formation_timestamp
    signal._bgx_formation_bucket = formation_bucket
    return setup_id


def _emit_strategy_stop_geometry(signal, log, epoch: float | None = None) -> str | None:
    """Best-effort pre-NEXUS telemetry; never changes the returned signal."""
    if signal is None:
        return None
    setup_id = _strategy_setup_id(signal, epoch)
    dedup_key = (
        str(getattr(signal, "symbol", "UNKNOWN")),
        str(getattr(signal, "direction", "UNKNOWN")).upper(),
        str(getattr(signal, "entry_type", "UNKNOWN")),
    )
    if _STRATEGY_STOP_GEOMETRY_LAST.get(dedup_key) == setup_id:
        return setup_id
    try:
        entry = float(signal.entry)
        sl = float(signal.sl)
        tp = float(signal.tp)
        atr_15m = float(signal._bgx_atr_15m)
        atr_1h = float(signal._bgx_atr_1h)
        adjusted_atr = float(signal._bgx_adjusted_atr)
        if entry <= 0:
            raise ValueError("invalid_entry")
        stop_abs = abs(entry - sl)
        target_abs = abs(tp - entry)
        stop_pct = stop_abs / entry * 100.0
        target_pct = target_abs / entry * 100.0
        log.info(
            "[STRATEGY_STOP_GEOMETRY] setup_id=%s symbol=%s direction=%s "
            "entry_type=%s formation_timestamp=%.6f formation_bucket=%s "
            "entry=%.12g sl=%.12g tp=%.12g atr_15m=%.12g atr_1h=%.12g "
            "adjusted_atr=%.12g original_stop_abs=%.12g original_stop_pct=%.8f "
            "target_abs=%.12g target_pct=%.8f strategy_score=%s regime=%s "
            "4h_bias=%s 1h_bias=%s 15m_bias=%s decision_effect=NONE execution_effect=NONE",
            setup_id,
            getattr(signal, "symbol", "UNKNOWN"),
            getattr(signal, "direction", "UNKNOWN"),
            getattr(signal, "entry_type", "UNKNOWN"),
            float(getattr(signal, "_bgx_formation_timestamp")),
            int(getattr(signal, "_bgx_formation_bucket")),
            entry, sl, tp, atr_15m, atr_1h, adjusted_atr,
            stop_abs, stop_pct, target_abs, target_pct,
            getattr(signal, "score", "UNKNOWN"),
            getattr(signal, "regime", "UNKNOWN"),
            getattr(signal, "_bgx_4h_bias", "UNKNOWN"),
            getattr(signal, "_bgx_1h_bias", "UNKNOWN"),
            getattr(signal, "_bgx_15m_bias", "UNKNOWN"),
        )
        _STRATEGY_STOP_GEOMETRY_LAST[dedup_key] = setup_id
    except Exception as exc:
        try:
            log.warning(
                "[STRATEGY_STOP_GEOMETRY] setup_id=%s telemetry_error=%s "
                "decision_effect=NONE execution_effect=NONE",
                setup_id, type(exc).__name__,
            )
        except Exception:
            pass
    return setup_id


def install(Analyzer, log) -> None:
    if getattr(Analyzer, "_pullback_confirmation_installed", False):
        return

    original = Analyzer.analyze_mtf

    def analyze_mtf_confirmed_pullback(self, symbol, k15, k1h, k4h, *args, **kwargs):
        signal = original(self, symbol, k15, k1h, k4h, *args, **kwargs)
        from bot.hard_gate_shadow_context import observation, mark
        research = observation()
        if research is not None:
            if signal is not None:
                mark(signal)
                research.signal = signal
                if str(getattr(signal, "entry_type", "")).upper() == "PULLBACK":
                    metrics = _pullback_metrics(k15, signal.direction)
                    passed = bool(metrics.get("ok"))
                    if not passed and metrics.get("reason") == "insufficient_reversal_votes":
                        passed = bool(_intrabar_fast_metrics(k15, signal.direction).get("ok"))
                    research.pullback = "PASS" if passed else "BLOCKED"
                    # Retain blocked candidates only in the contextual observer.
                    return signal if passed else None
            return signal
        setup_id = _emit_strategy_stop_geometry(signal, log)
        if signal is None or str(getattr(signal, "entry_type", "")).upper() != "PULLBACK":
            return signal

        direction = getattr(signal, "direction", "")
        metrics = _pullback_metrics(k15, direction)
        if metrics.get("ok"):
            log.info(
                "[PULLBACK_CONFIRMATION] setup_id=%s symbol=%s side=%s result=PASS path=closed_15m "
                "votes=%s vote_count=%s structure=%s bos=%s bos_dir=%s rsi=%s "
                "execution_effect=SIGNAL_FILTER_ONLY",
                setup_id, symbol, direction, metrics.get("votes", {}), metrics.get("vote_count", 0),
                metrics.get("structure", "UNKNOWN"), metrics.get("bos", False),
                metrics.get("bos_dir", "NONE"), metrics.get("rsi", "N/A"),
            )
            return signal

        # Acceleration is intentionally narrower than the canonical 4/5 rule.
        # Never use it to bypass an explicit opposite structure/BOS diagnosis.
        fast = {"ok": False, "reason": "not_eligible"}
        if metrics.get("reason") == "insufficient_reversal_votes":
            fast = _intrabar_fast_metrics(k15, direction)
            if fast.get("ok"):
                log.warning(
                    "[PULLBACK_CONFIRMATION] setup_id=%s symbol=%s side=%s result=PASS path=intrabar_fast "
                    "closed_votes=%s intrabar_votes=%s ema20_side=true structure=%s "
                    "strict=5/5 NEXUS_and_risk_gates_preserved=true execution_effect=SIGNAL_FILTER_ONLY",
                    setup_id, symbol, direction, metrics.get("vote_count", 0), fast.get("vote_count", 0),
                    fast.get("structure", "UNKNOWN"),
                )
                return signal

        log.info(
            "[PULLBACK_CONFIRMATION] setup_id=%s symbol=%s side=%s result=BLOCKED reason=%s "
            "votes=%s vote_count=%s structure=%s bos=%s bos_dir=%s rsi=%s "
            "macd=%s/%s ema20_side=%s intrabar_reason=%s intrabar_votes=%s "
            "execution_effect=SIGNAL_FILTER_ONLY",
            setup_id,
            symbol,
            direction or "UNKNOWN",
            metrics.get("reason"),
            metrics.get("votes", {}),
            metrics.get("vote_count", 0),
            metrics.get("structure", "UNKNOWN"),
            metrics.get("bos", False),
            metrics.get("bos_dir", "NONE"),
            metrics.get("rsi", "N/A"),
            metrics.get("macd_hist", "N/A"),
            metrics.get("macd_prev", "N/A"),
            metrics.get("ema20_side", "N/A"),
            fast.get("reason", "not_evaluated"),
            fast.get("vote_count", 0),
        )
        return None

    Analyzer.analyze_mtf = analyze_mtf_confirmed_pullback
    Analyzer._pullback_confirmation_installed = True
    log.warning(
        "[PULLBACK_CONFIRMATION] installed closed_15m_min_votes=4/5 "
        "intrabar_fast_path=5/5_plus_ema20_no_opposite_structure_or_bos "
        "opposite_structure_requires_aligned_bos=true opposite_bos=BLOCK "
        "BOS_BREAK_and_MOMENTUM_unchanged=true NEXUS_and_risk_gates_unchanged=true"
    )
