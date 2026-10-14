"""Market-data integrity policy shared by LIVE strategy and NEXUS.

This module is intentionally fail-closed around data correctness (2026-09-11
audit; the KuCoin-only volume rewrite was removed with the KuCoin adapter):

* A candle is considered closed from its timestamp and timeframe boundary, not
  from its position in a list.
* NEXUS gets an independent hard freshness/gap check so a numerically acceptable
  DataQuality score can never authorize a trade on critically stale candles.

No threshold, leverage, risk budget, release token, or exchange-order permission
is changed here.
"""
from __future__ import annotations

import math
import time
from typing import Iterable


_INTERVAL_MINUTES = {
    "1": 1, "1m": 1, "1min": 1,
    "3": 3, "3m": 3, "3min": 3,
    "5": 5, "5m": 5, "5min": 5,
    "15": 15, "15m": 15, "15min": 15,
    "30": 30, "30m": 30, "30min": 30,
    "60": 60, "1h": 60, "1hour": 60,
    "120": 120, "2h": 120, "2hour": 120,
    "240": 240, "4h": 240, "4hour": 240,
    "480": 480, "8h": 480, "8hour": 480,
    "720": 720, "12h": 720, "12hour": 720,
    "D": 1440, "1d": 1440, "1day": 1440,
    "W": 10080, "1w": 10080, "1week": 10080,
}

# Timestamps earlier than year 2000 are treated as synthetic/test placeholders,
# not as trustworthy exchange time. This preserves isolated unit fixtures while
# production KuCoin timestamps (seconds or milliseconds) remain strict.
_MIN_REAL_TS_MS = 946684800000


def interval_ms(timeframe: str) -> int:
    minutes = _INTERVAL_MINUTES.get(str(timeframe))
    if minutes is None:
        try:
            minutes = int(timeframe)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported timeframe: {timeframe}") from exc
    if minutes <= 0:
        raise ValueError(f"invalid timeframe: {timeframe}")
    return int(minutes) * 60_000


def candle_ts_ms(candle: dict) -> int | None:
    try:
        raw = candle.get("ts")
        if isinstance(raw, bool):
            return None
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            return None
        ts = int(value)
        if ts < 100_000_000_000:
            ts *= 1000
        if ts < _MIN_REAL_TS_MS:
            return None
        return ts
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def has_usable_timestamps(candles: Iterable[dict] | None) -> bool:
    data = list(candles or [])
    return bool(data) and all(candle_ts_ms(c) is not None for c in data)


def _ordered_unique(candles: Iterable[dict]) -> list:
    """Return strict chronological candles, keeping the newest duplicate."""
    by_ts = {}
    for candle in candles:
        ts = candle_ts_ms(candle)
        if ts is None:
            continue
        by_ts[ts] = candle
    return [by_ts[ts] for ts in sorted(by_ts)]


def closed_candles(candles: Iterable[dict] | None, timeframe: str,
                   *, now_ms: int | None = None,
                   require_timestamps: bool = False) -> list:
    """Select bars whose timestamp + interval is at or before ``now``.

    If timestamps are synthetic/missing and strict timestamps are not required,
    the input is returned unchanged so legacy unit fixtures can still exercise
    unrelated logic. Production/NEXUS hard gates call this with
    ``require_timestamps=True``.
    """
    data = list(candles or [])
    if not data:
        return []
    if not has_usable_timestamps(data):
        return [] if require_timestamps else data

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    width = interval_ms(timeframe)
    ordered = _ordered_unique(data)
    return [c for c in ordered if candle_ts_ms(c) + width <= now]


def prepare_strategy_series(candles: Iterable[dict] | None, timeframe: str,
                            minimum: int, *, now_ms: int | None = None) -> list:
    """Prepare inputs for legacy analyzers that still drop ``[-1]``.

    The real closed/open decision is made here from timestamps. A sentinel is
    appended only so the existing analyzer's historical ``[:-1]`` operation
    removes the sentinel rather than a confirmed bar. If timestamps are not
    usable (old unit fixtures), inputs are left untouched.
    """
    data = list(candles or [])
    if not has_usable_timestamps(data):
        return data

    closed = closed_candles(data, timeframe, now_ms=now_ms, require_timestamps=True)
    if len(closed) < minimum:
        return closed

    closed_ts = {candle_ts_ms(c) for c in closed}
    open_bars = [c for c in _ordered_unique(data) if candle_ts_ms(c) not in closed_ts]
    if open_bars:
        sentinel = dict(open_bars[-1])
    else:
        # No forming bar was supplied (common for REST responses immediately
        # after a boundary). Duplicate the last confirmed bar as a disposable
        # sentinel; the legacy analyzer drops it before indicator calculation.
        sentinel = dict(closed[-1])
        sentinel["_closed_bar_sentinel"] = True
    return closed + [sentinel]


def validate_nexus_candles(k15: list, k1h: list, k4h: list,
                           *, now_ms: int | None = None,
                           max_stale_intervals: float = 2.0) -> tuple[bool, str, tuple[list, list, list]]:
    """Hard fail-closed integrity check for the NEXUS decision boundary."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    specs = (
        ("15m", k15, "15", 60),
        ("1h", k1h, "60", 40),
        ("4h", k4h, "240", 20),
    )
    views = []
    for name, raw, interval, minimum in specs:
        if not has_usable_timestamps(raw):
            return False, f"{name}:missing_or_invalid_timestamp", ([], [], [])
        view = closed_candles(raw, interval, now_ms=now, require_timestamps=True)
        if len(view) < minimum:
            return False, f"{name}:closed_history_{len(view)}<{minimum}", ([], [], [])

        width = interval_ms(interval)
        last_ts = candle_ts_ms(view[-1])
        last_close = last_ts + width
        age = max(0, now - last_close)
        if age > width * float(max_stale_intervals):
            return False, f"{name}:hard_stale_age_s={age/1000:.0f}", ([], [], [])

        # Recent discontinuities are a data-integrity failure for the highly
        # liquid futures universe used by NEXUS. A single missing bucket is
        # tolerated (delta <= 2x interval); larger holes fail closed.
        recent = view[-min(len(view), 40):]
        ts_values = [candle_ts_ms(c) for c in recent]
        for prev, cur in zip(ts_values, ts_values[1:]):
            if cur <= prev:
                return False, f"{name}:non_monotonic_timestamp", ([], [], [])
            if cur - prev > width * 2:
                return False, f"{name}:abnormal_gap_s={(cur-prev)/1000:.0f}", ([], [], [])
        views.append(view)

    return True, "ok", (views[0], views[1], views[2])
