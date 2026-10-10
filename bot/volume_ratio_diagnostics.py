"""Passive diagnostics for strategy volume-ratio inputs.

This module never changes a signal, threshold, candle, or order decision. It
recomputes the 15m closed-candle activity ratio from the normalized KuCoin
transaction-amount series and emits one line per confirmed candle/symbol. The
purpose is to distinguish a genuinely quiet candle from REST/WS/cache unit or
boundary mistakes.
"""
from __future__ import annotations

import math


_LAST_LOGGED = {}


def _closed_ratio(k15, integrity):
    closed = integrity.closed_candles(
        k15, "15", require_timestamps=True
    )
    if len(closed) < 21:
        return None
    sample = closed[-21:]
    try:
        current = float(sample[-1]["v"])
        prior = [float(c["v"]) for c in sample[:-1]]
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not math.isfinite(current) or current < 0
        or any(not math.isfinite(v) or v < 0 for v in prior)
    ):
        return None
    avg20 = sum(prior) / len(prior)
    if avg20 <= 0:
        return None
    ts = integrity.candle_ts_ms(sample[-1])
    if ts is None:
        return None
    return current, avg20, current / avg20, ts


def install(Analyzer, integrity, log):
    if getattr(Analyzer, "_volume_ratio_diagnostics_installed", False):
        return
    original = Analyzer.analyze_mtf

    def analyze_with_volume_diag(self, symbol, k15, k1h, k4h, *args, **kwargs):
        from bot.hard_gate_shadow_context import active
        if active():
            return original(self, symbol, k15, k1h, k4h, *args, **kwargs)
        try:
            result = _closed_ratio(k15, integrity)
            if result is not None:
                current, avg20, ratio, ts = result
                key = (str(symbol), int(ts))
                if _LAST_LOGGED.get(str(symbol)) != key:
                    _LAST_LOGGED[str(symbol)] = key
                    log.info(
                        "[VOLUME_RATIO_DIAG] symbol=%s timeframe=15m "
                        "current_activity=%.12g avg20_activity=%.12g ratio=%.4fx "
                        "last_closed_ts=%s unit=transaction_amount_index_6 "
                        "closed_candle_source=timestamp_boundary decision_effect=NONE "
                        "execution_effect=NONE",
                        symbol, current, avg20, ratio, ts,
                    )
        except Exception as exc:
            log.warning(
                "[VOLUME_RATIO_DIAG] symbol=%s diagnostic_error=%s "
                "decision_effect=NONE execution_effect=NONE",
                symbol, type(exc).__name__,
            )
        return original(self, symbol, k15, k1h, k4h, *args, **kwargs)

    Analyzer.analyze_mtf = analyze_with_volume_diag
    Analyzer._volume_ratio_diagnostics_installed = True
