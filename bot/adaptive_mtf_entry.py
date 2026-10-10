"""Conservative adaptive MTF entry rescue for fast trend expansion.

The canonical strategy intentionally requires strict 4H+1H EMA alignment. That
is a good default for ordinary trend-following, but it can enter too late when a
strong expansion starts in 15M/1H before both higher-timeframe EMA states have
fully crossed.

This module adds a *narrow* second chance only after the canonical analyzer
returns HOLD. It never bypasses NEXUS, risk, portfolio, exchange, protection,
news, drawdown, or pilot gates. It also refuses to trade against an explicitly
opposite 4H or 1H state.

Adaptive entry is restricted to BOS_BREAK/MOMENTUM with strong multi-timeframe
scores, volume confirmation, 15M alignment, non-extreme RSI and anti-chase
extension control. The final signal then follows the normal downstream path.
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np

from bot import adaptive_mtf_calibration
from bot import adaptive_mtf_dedup_shadow


_ENABLED = os.environ.get("ADAPTIVE_MTF_ENTRY", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
_MIN_COMBINED = float(os.environ.get("ADAPTIVE_MTF_MIN_SCORE", "72"))
_MIN_4H = float(os.environ.get("ADAPTIVE_MTF_MIN_4H_SCORE", "60"))
_MIN_1H = float(os.environ.get("ADAPTIVE_MTF_MIN_1H_SCORE", "65"))
_MIN_15M = float(os.environ.get("ADAPTIVE_MTF_MIN_15M_SCORE", "80"))
_MIN_VOL = float(os.environ.get("ADAPTIVE_MTF_MIN_VOL", "1.20"))
_MIN_ADX = float(os.environ.get("ADAPTIVE_MTF_MIN_ADX", "18"))
_MAX_EXTENSION_ATR = float(os.environ.get("ADAPTIVE_MTF_MAX_EXTENSION_ATR", "2.50"))


def _gate(*, direction: str, regime: str, bull_4h: bool, bear_4h: bool,
          bull_1h: bool, bear_1h: bool, s4h: dict, s1h: dict, s15: dict,
          combined: float, entry_type: str, extension_atr: float) -> tuple[bool, str]:
    """Pure gate used by runtime and regression tests."""
    if direction not in ("LONG", "SHORT"):
        return False, "invalid_direction"
    if regime != ("TRENDING_UP" if direction == "LONG" else "TRENDING_DOWN"):
        return False, "regime_direction_mismatch"

    # Never rescue a setup that is explicitly opposed by either HTF.
    if direction == "LONG" and (bear_4h or bear_1h):
        return False, "higher_timeframe_opposition"
    if direction == "SHORT" and (bull_4h or bull_1h):
        return False, "higher_timeframe_opposition"

    # Strict alignment belongs to the canonical path; if it was already strict,
    # an original HOLD happened for another reason and must not be bypassed here.
    strict = (bull_4h and bull_1h) if direction == "LONG" else (bear_4h and bear_1h)
    if strict:
        return False, "canonical_alignment_already_present"

    if not all(bool(x.get("ok")) for x in (s4h, s1h, s15)):
        return False, "invalid_score"
    if float(s4h.get("total", 0)) < _MIN_4H:
        return False, "weak_4h"
    if float(s1h.get("total", 0)) < _MIN_1H:
        return False, "weak_1h"
    if float(s15.get("total", 0)) < _MIN_15M:
        return False, "weak_15m"
    if float(combined) < _MIN_COMBINED:
        return False, "weak_combined"
    if not bool(s15.get("aligned")):
        return False, "15m_not_aligned"
    if entry_type not in ("BOS_BREAK", "MOMENTUM"):
        return False, "entry_not_expansion"
    if float(s15.get("vol_r", 0)) < _MIN_VOL:
        return False, "volume_not_confirmed"
    if entry_type != "BOS_BREAK" and float(s15.get("adx_v", 0)) < _MIN_ADX:
        return False, "adx_too_low"
    rsi_v = float(s15.get("rsi_v", 50))
    if direction == "LONG" and rsi_v > 90:
        return False, "long_rsi_overextended"
    if direction == "SHORT" and rsi_v < 10:
        return False, "short_rsi_overextended"
    if extension_atr > _MAX_EXTENSION_ATR:
        return False, "price_too_extended"
    return True, "adaptive_expansion_confirmed"


def install(Analyzer, strategy, log) -> None:
    if getattr(Analyzer, "_adaptive_mtf_entry_installed", False):
        return

    original = Analyzer.analyze_mtf

    def analyze_mtf_adaptive(self, symbol, k15, k1h, k4h,
                             min_score=60, fee_mult=2.0, vol_mult=1.0):
        canonical = original(
            self, symbol, k15, k1h, k4h,
            min_score=min_score, fee_mult=fee_mult, vol_mult=vol_mult,
        )
        if canonical is not None or not _ENABLED:
            return canonical

        # Same closed-candle semantics as the canonical strategy.
        if len(k4h) < 10 or len(k1h) < 15 or len(k15) < 20:
            return None

        def ga(kl):
            return (
                [float(k["c"]) for k in kl],
                [float(k["h"]) for k in kl],
                [float(k["l"]) for k in kl],
                [float(k["o"]) for k in kl],
                [k["v"] for k in kl],
            )

        try:
            c4h, h4h, l4h, o4h, v4h = ga(k4h[:-1] if len(k4h) > 10 else k4h)
            c1h, h1h, l1h, o1h, v1h = ga(k1h[:-1] if len(k1h) > 15 else k1h)
            c15, h15, l15, o15, v15 = ga(k15[:-1] if len(k15) > 20 else k15)

            def get_atr(h, l, c):
                a = strategy.atr(h, l, c)
                return float(a[-1]), float(np.mean(a[-20:])) if len(a) >= 20 else float(a[-1])

            atr_4h, avg_4h = get_atr(h4h, l4h, c4h)
            atr_1h, avg_1h = get_atr(h1h, l1h, c1h)
            atr_15, avg_15 = get_atr(h15, l15, c15)
            regime = strategy.detect_regime(c4h, h4h, l4h, atr_4h)
            if regime not in ("TRENDING_UP", "TRENDING_DOWN"):
                return None
            direction = "LONG" if regime == "TRENDING_UP" else "SHORT"

            e20_4h = float(strategy.ema(c4h, 20)[-1])
            e50_4h = float(strategy.ema(c4h, 50)[-1])
            e20_1h = float(strategy.ema(c1h, 20)[-1])
            e50_1h = float(strategy.ema(c1h, 50)[-1])
            bull_4h = e20_4h > e50_4h and c4h[-1] > e20_4h
            bear_4h = e20_4h < e50_4h and c4h[-1] < e20_4h
            bull_1h = e20_1h > e50_1h and c1h[-1] > e20_1h
            bear_1h = e20_1h < e50_1h and c1h[-1] < e20_1h

            s4h = strategy.score_tf(c4h, h4h, l4h, o4h, v4h, direction, atr_4h, avg_4h)
            s1h = strategy.score_tf(c1h, h1h, l1h, o1h, v1h, direction, atr_1h, avg_1h)
            s15 = strategy.score_tf(c15, h15, l15, o15, v15, direction, atr_15, avg_15)
            if not all(bool(x.get("ok")) for x in (s4h, s1h, s15)):
                return None

            combined = round(
                float(s4h["total"]) * 0.25
                + float(s1h["total"]) * 0.30
                + float(s15["total"]) * 0.45
            )
            entry_ok, entry_type = strategy.detect_entry(
                c15, h15, l15, o15, v15, direction, atr_15
            )
            if not entry_ok:
                return None

            e20_15 = float(strategy.ema(c15, 20)[-1])
            extension_atr = abs(float(c15[-1]) - e20_15) / atr_15 if atr_15 > 0 else 999.0
            allowed, reason = _gate(
                direction=direction, regime=regime,
                bull_4h=bull_4h, bear_4h=bear_4h,
                bull_1h=bull_1h, bear_1h=bear_1h,
                s4h=s4h, s1h=s1h, s15=s15,
                combined=combined, entry_type=entry_type,
                extension_atr=extension_atr,
            )
            if not allowed:
                from bot.hard_gate_shadow_context import active
                if active():
                    return None
                thresholds = {
                    "min_combined": _MIN_COMBINED,
                    "min_4h": _MIN_4H,
                    "min_1h": _MIN_1H,
                    "min_15m": _MIN_15M,
                    "min_vol": _MIN_VOL,
                    "min_adx": _MIN_ADX,
                    "max_extension_atr": _MAX_EXTENSION_ATR,
                }
                adaptive_mtf_calibration.observe_reject(
                    symbol=symbol, k15=k15, direction=direction, reason=reason,
                    bull_4h=bull_4h, bear_4h=bear_4h,
                    bull_1h=bull_1h, bear_1h=bear_1h,
                    s4h=s4h, s1h=s1h, s15=s15,
                    combined=combined, entry_type=entry_type,
                    extension_atr=extension_atr,
                    thresholds=thresholds,
                    log=log,
                )
                adaptive_mtf_dedup_shadow.observe_reject(
                    symbol=symbol, k15=k15, direction=direction,
                    bull_4h=bull_4h, bear_4h=bear_4h,
                    bull_1h=bull_1h, bear_1h=bear_1h,
                    s4h=s4h, s1h=s1h, s15=s15,
                    combined=combined, entry_type=entry_type,
                    extension_atr=extension_atr,
                    thresholds=thresholds,
                    log=log,
                )
                log.debug("[ADAPTIVE_MTF] symbol=%s result=HOLD reason=%s", symbol, reason)
                return None

            price = float(c15[-1])
            if entry_type == "BOS_BREAK":
                sl_mult, tp_mult = 1.2, 3.6
            else:  # MOMENTUM only; PULLBACK is deliberately not rescued.
                sl_mult, tp_mult = 1.5, 3.0
            sl_atr = max(atr_15, atr_1h * 0.5)
            if direction == "LONG":
                raw_sl = price - sl_atr * sl_mult
                raw_tp = price + sl_atr * tp_mult
            else:
                raw_sl = price + sl_atr * sl_mult
                raw_tp = price - sl_atr * tp_mult

            rr = strategy._rr_from_unrounded_levels(price, raw_sl, raw_tp)
            if rr < strategy.cfg.MIN_RR_RATIO:
                return None
            sl = round(raw_sl, 6)
            tp = round(raw_tp, 6)

            cost_pct = strategy.TOTAL_COST * 100
            move_to_tp = abs(tp - price) / price * 100
            min_move = cost_pct * fee_mult
            if move_to_tp < min_move:
                return None
            expected_net = move_to_tp - cost_pct

            reasons = [
                "MTF_ADAPTIVE",
                f"4H:{s4h['total']}", f"1H:{s1h['total']}", f"15M:{s15['total']}",
                f"ADX{s15['adx_v']:.0f}", f"VOL{s15['vol_r']:.2f}x",
                f"RR{rr:.1f}", f"ENTRY:{entry_type}",
            ]
            if s15.get("bos"):
                reasons.append("BOS✓")
            if s15.get("vwap_ok"):
                reasons.append("VWAP✓")
            if not s15.get("ci_chop"):
                reasons.append("CI✓")

            log.warning(
                "[ADAPTIVE_MTF] symbol=%s result=PASS direction=%s regime=%s "
                "score=%s 4h=%s 1h=%s 15m=%s entry=%s vol=%.2fx adx=%.1f "
                "extension_atr=%.2f; downstream_NEXUS_and_risk_gates_required=true",
                symbol, direction, regime, combined, s4h["total"], s1h["total"],
                s15["total"], entry_type, float(s15["vol_r"]), float(s15["adx_v"]),
                extension_atr,
            )
            return strategy.Signal(
                symbol=symbol, direction=direction,
                entry=price, sl=sl, tp=tp,
                confidence=min(0.97, combined / 100),
                reason=" | ".join(reasons), score=int(combined),
                tf_4h=s4h["summary"], tf_1h=s1h["summary"], tf_15m=s15["summary"],
                expected_pnl=round(expected_net, 3), total_fees=round(cost_pct, 4),
                entry_type=entry_type, regime=regime,
            )
        except Exception as exc:
            # A rescue path must always fail closed. Canonical HOLD remains HOLD.
            log.warning(
                "[ADAPTIVE_MTF] symbol=%s result=HOLD reason=exception type=%s",
                symbol, type(exc).__name__,
            )
            return None

    Analyzer.analyze_mtf = analyze_mtf_adaptive
    Analyzer._adaptive_mtf_entry_installed = True
    log.warning(
        "[ADAPTIVE_MTF] installed enabled=%s min_combined=%.0f min_4h=%.0f "
        "min_1h=%.0f min_15m=%.0f min_vol=%.2fx min_adx=%.0f max_extension=%.2fATR; "
        "strict_opposite_HTF=BLOCK; NEXUS/risk/execution gates unchanged",
        _ENABLED, _MIN_COMBINED, _MIN_4H, _MIN_1H, _MIN_15M,
        _MIN_VOL, _MIN_ADX, _MAX_EXTENSION_ATR,
    )
