"""Passive Analyzer overlay for entry-type and HTF-transition diagnostics.

Runs only after the fully composed analyzer has already returned HOLD. It first
feeds the existing HTF transition shadow, then reconstructs the same closed-
candle 15m view and, for PULLBACK only, evaluates multi-bar expansion in shadow.
The original analyzer return value is always preserved.
"""
from __future__ import annotations

import math
import threading
from collections import deque

import numpy as np

from bot import entry_type_shadow
from bot import htf_transition_shadow
from bot.indicators import ema

_TRIAGE_LOCK = threading.Lock()
_TRIAGE_SEEN: set[tuple[str, object]] = set()
_TRIAGE_ORDER = deque(maxlen=5000)


def _bar_ts(bar):
    if not bar:
        return None
    return bar.get("ts") or bar.get("time") or bar.get("timestamp")


def _closed(kl):
    data = list(kl or [])
    return data[:-1] if len(data) > 2 else data


def _ema_state(kl) -> str:
    closed = _closed(kl)
    if len(closed) < 51:
        return "NEUTRAL"
    closes = [float(k["c"]) for k in closed]
    e20 = float(ema(closes, 20)[-1])
    e50 = float(ema(closes, 50)[-1])
    px = float(closes[-1])
    if not all(math.isfinite(v) for v in (e20, e50, px)):
        return "NEUTRAL"
    if e20 > e50 and px > e20:
        return "LONG"
    if e20 < e50 and px < e20:
        return "SHORT"
    return "NEUTRAL"


def _ema_state_live(kl) -> str:
    data = list(kl or [])
    if len(data) < 51:
        return "NEUTRAL"
    closes = [float(k["c"]) for k in data]
    e20 = float(ema(closes, 20)[-1])
    e50 = float(ema(closes, 50)[-1])
    px = float(closes[-1])
    if not all(math.isfinite(v) for v in (e20, e50, px)):
        return "NEUTRAL"
    if e20 > e50 and px > e20:
        return "LONG"
    if e20 < e50 and px < e20:
        return "SHORT"
    return "NEUTRAL"


def _remember_triage(symbol: str, k15) -> bool:
    closed15 = _closed(k15)
    if not closed15:
        return False
    key = (symbol, _bar_ts(closed15[-1]))
    with _TRIAGE_LOCK:
        if key in _TRIAGE_SEEN:
            return False
        if len(_TRIAGE_ORDER) == _TRIAGE_ORDER.maxlen:
            old = _TRIAGE_ORDER.popleft()
            _TRIAGE_SEEN.discard(old)
        _TRIAGE_SEEN.add(key)
        _TRIAGE_ORDER.append(key)
    return True


def _log_htf_transition_triage(symbol, k15, k1h, k4h, log) -> None:
    """Explain why a HOLD did or did not reach the strict HTF transition cohort.

    This is read-only observability. It deliberately mirrors only the structural
    EMA-state preconditions. The existing htf_transition_shadow remains the sole
    owner of strict score/volume/ADX/entry-type qualification and hypothetical
    outcome tracking.
    """
    if len(k15) < 60 or len(k1h) < 60 or len(k4h) < 60:
        return
    if not _remember_triage(symbol, k15):
        return

    state_15 = _ema_state(k15)
    state_1h = _ema_state(k1h)
    state_4h_closed = _ema_state(k4h)
    state_4h_live = _ema_state_live(k4h)

    reasons: list[str] = []
    if state_15 not in ("LONG", "SHORT"):
        reasons.append("15M_NOT_DIRECTIONAL")
    if state_1h not in ("LONG", "SHORT"):
        reasons.append("1H_NOT_DIRECTIONAL")
    if state_15 in ("LONG", "SHORT") and state_1h in ("LONG", "SHORT") and state_1h != state_15:
        reasons.append("15M_1H_MISALIGNED")

    direction = state_15 if state_15 in ("LONG", "SHORT") and state_1h == state_15 else "NONE"
    if direction != "NONE":
        if state_4h_closed == direction:
            reasons.append("4H_CONFIRMED_ALREADY_ALIGNED")
        if state_4h_live != direction:
            reasons.append("4H_FORMING_NOT_FLIPPED")

    structural_candidate = (
        direction != "NONE"
        and state_4h_closed != direction
        and state_4h_live == direction
    )
    log.info(
        "[HTF_TRANSITION_TRIAGE] symbol=%s state15=%s state1h=%s "
        "state4h_confirmed=%s state4h_forming=%s candidate_direction=%s "
        "structural_candidate=%s precondition_rejects=%s "
        "strict_cohort_owner=HTF_TRANSITION_SHADOW forming_4h_diagnostic_only=true "
        "shadow_only=true thresholds_unchanged=true leverage_unchanged=true "
        "decision_effect=NONE execution_effect=NONE",
        symbol, state_15, state_1h, state_4h_closed, state_4h_live, direction,
        str(structural_candidate).lower(), ",".join(reasons) or "NONE",
    )


def install(Analyzer, strategy, log) -> None:
    if getattr(Analyzer, "_entry_type_shadow_installed", False):
        return

    original = Analyzer.analyze_mtf

    def analyze_with_entry_shadow(self, symbol, k15, k1h, k4h,
                                  min_score=60, fee_mult=2.0, vol_mult=1.0):
        result = original(
            self, symbol, k15, k1h, k4h,
            min_score=min_score, fee_mult=fee_mult, vol_mult=vol_mult,
        )
        from bot.hard_gate_shadow_context import active
        if active():
            return result
        if result is not None:
            return result
        try:
            # Read-only transition audit. Forming 4H is diagnostic-only inside
            # htf_transition_shadow and can never create a Signal/NEXUS call.
            _log_htf_transition_triage(symbol, k15, k1h, k4h, log)
            htf_transition_shadow.observe(symbol, k15, k1h, k4h, result, log)

            if len(k4h) < 10 or len(k15) < 20:
                return result

            def ga(kl):
                return (
                    [float(k["c"]) for k in kl],
                    [float(k["h"]) for k in kl],
                    [float(k["l"]) for k in kl],
                    [float(k["o"]) for k in kl],
                    [k["v"] for k in kl],
                )

            c4h, h4h, l4h, _, _ = ga(k4h[:-1] if len(k4h) > 10 else k4h)
            c15, h15, l15, o15, v15 = ga(k15[:-1] if len(k15) > 20 else k15)
            a4 = strategy.atr(h4h, l4h, c4h)
            atr_4h = float(a4[-1])
            regime = strategy.detect_regime(c4h, h4h, l4h, atr_4h)
            if regime not in ("TRENDING_UP", "TRENDING_DOWN"):
                return result
            direction = "LONG" if regime == "TRENDING_UP" else "SHORT"

            a15 = strategy.atr(h15, l15, c15)
            atr_15 = float(a15[-1])
            entry_ok, current_type = strategy.detect_entry(
                c15, h15, l15, o15, v15, direction, atr_15
            )
            if not entry_ok or current_type != "PULLBACK":
                return result

            shadow = entry_type_shadow.assess(
                closes=c15, highs=h15, lows=l15, opens=o15,
                direction=direction, atr_v=atr_15, current_type=current_type,
            )
            log.info(
                "[ENTRY_TYPE_SHADOW] symbol=%s side=%s current=%s shadow=%s "
                "would_reclassify=%s body=%.2fATR net2=%.2fATR net3=%.2fATR "
                "directional_bars=%d/3 close_location=%.2f failed_reasons=%s "
                "live_result=HOLD shadow_only=true entry_type_unchanged=true "
                "nexus_called_by_shadow=false signal_created_by_shadow=false "
                "thresholds_unchanged=true leverage_unchanged=true "
                "decision_effect=NONE execution_effect=NONE",
                symbol, direction, current_type, shadow.get("shadow_type", current_type),
                str(bool(shadow.get("would_reclassify"))).lower(),
                float(shadow.get("body_atr", 0.0)), float(shadow.get("net_2bar_atr", 0.0)),
                float(shadow.get("net_3bar_atr", 0.0)), int(shadow.get("directional_bars", 0)),
                float(shadow.get("close_location", 0.0)),
                ",".join(shadow.get("failed_reasons") or []) or "NONE",
            )
        except Exception as exc:
            log.debug("[ENTRY_TYPE_SHADOW] symbol=%s diagnostic_error=%s", symbol, type(exc).__name__)
        return result

    Analyzer.analyze_mtf = analyze_with_entry_shadow
    Analyzer._entry_type_shadow_installed = True
    log.info(
        "[ENTRY_TYPE_SHADOW] installed multi_bar_displacement=true htf_transition_shadow=true "
        "htf_transition_triage=true forming_4h_diagnostic_only=true closed_candles=true "
        "shadow_only=true entry_type_unchanged=true thresholds_unchanged=true "
        "leverage_unchanged=true decision_effect=NONE execution_effect=NONE"
    )
