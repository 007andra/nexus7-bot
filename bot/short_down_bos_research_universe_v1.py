"""#609 public-cache-only symbol qualification without a financial account.

Pure, offline, no imports from the engine, exchange, risk manager, DB,
configuration, or order dispatch. Its output is market-data readiness ONLY,
NEVER minimum-order feasibility, canonical NEXUS approval, or #609 enrollment.
"""
from __future__ import annotations

import math
import re

INTERVAL_MS = {"15": 900_000, "60": 3_600_000, "240": 14_400_000}
REQUIRED_CLOSED = {"15": 60, "60": 40, "240": 20}
MAX_SYMBOLS = 100
AUTHORITY = {
    "research_only": True,
    "market_data_only": True,
    "balance_required": False,
    "live_tradable": False,
    "canonical_nexus_approval_proven": False,
    "eligible_for_609_enrollment": False,
    "min_order_feasibility_proven": False,
    "live_allowed": False,
    "promotion_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _valid_window(rows, interval, now_ms):
    """Return a reason; never reinterpret the current open kline as closed."""
    width, required = INTERVAL_MS[interval], REQUIRED_CLOSED[interval]
    if not isinstance(rows, (tuple, list)):
        return "CACHE_MISSING"
    closed = []
    for row in rows:
        if not isinstance(row, dict):
            return "MALFORMED_CANDLE"
        timestamp = row.get("ts")
        if type(timestamp) is not int or timestamp <= 0:
            return "BAD_TIMESTAMP"
        # An in-progress candle is excluded. A future candle is also excluded.
        if timestamp + width > now_ms:
            continue
        if timestamp % width:
            return "UNALIGNED_TIMESTAMP"
        prices = [_positive(row.get(k)) for k in ("o", "h", "l", "c")]
        if any(v is None for v in prices):
            return "INVALID_OHLC"
        o, h, l, c = prices
        if not l <= min(o, c) <= max(o, c) <= h:
            return "INVALID_OHLC"
        closed.append(timestamp)
    if len(closed) < required:
        return "INSUFFICIENT_CLOSED_CANDLES"
    # Only the freshest required window is relevant, but duplicate timestamps,
    # reorder or noncontiguity inside it are not silently deduplicated.
    latest = closed[-required:]
    if any(b - a != width for a, b in zip(latest, latest[1:])):
        return "NONCONTIGUOUS_CACHE"
    if now_ms - (latest[-1] + width) > width:
        return "STALE_CLOSED_CANDLE"
    return "PASS"


def qualify(configured_symbols, snapshots, *, now_ms):
    """Strictly MARKET observation-ready symbols, not exchange-viable symbols.

    snapshots is a caller-supplied map with (symbol, interval) tuple keys
    containing Binance-shaped cached OHLC rows; this module has NO access
    to the bot, risk.balance, Redis or public/private WebSockets.
    """
    if (type(now_ms) is not int or now_ms <= 0
            or not isinstance(configured_symbols, (tuple, list))
            or not isinstance(snapshots, dict)
            or len(configured_symbols) > MAX_SYMBOLS):
        return {**AUTHORITY, "status": "NOT_PROVEN",
                "reason": "INVALID_SNAPSHOT_ENVELOPE",
                "market_ready_symbols": (), "evaluated": 0,
                "reasons": {}}
    reasons = {}
    ready = []
    seen = set()
    for symbol in configured_symbols:
        if (not isinstance(symbol, str)
                or not re.fullmatch(r"[A-Z0-9]{2,20}USDT", symbol)):
            return {**AUTHORITY, "status": "NOT_PROVEN",
                    "reason": "INVALID_CONFIGURED_SYMBOL",
                    "market_ready_symbols": (), "evaluated": 0,
                    "reasons": {}}
        if symbol in seen:
            return {**AUTHORITY, "status": "NOT_PROVEN",
                    "reason": "DUPLICATE_CONFIGURED_SYMBOL",
                    "market_ready_symbols": (), "evaluated": 0,
                    "reasons": {}}
        seen.add(symbol)
        outcome = "PASS"
        for interval in ("15", "60", "240"):
            code = _valid_window(
                snapshots.get((symbol, interval)), interval, now_ms
            )
            if code != "PASS":
                outcome = f"{interval}m:{code}"
                break
        reasons[symbol] = outcome
        if outcome == "PASS":
            ready.append(symbol)
    return {
        **AUTHORITY,
        "status": "MARKET_OBSERVATION_READY" if ready else "NO_MARKET_READY_SYMBOLS",
        "reason": "CACHE_ONLY_NO_FINANCIAL_ELIGIBILITY",
        "market_ready_symbols": tuple(ready),
        "evaluated": len(configured_symbols),
        "reasons": reasons,
    }
