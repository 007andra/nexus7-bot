"""#609: offline Binance USD-M aggTrade event path shape validator.

Strictly research-only and network-free. A public trade-stream shape is NOT an
authenticated fill, a tamper-proof archive, a spread/fee observation, or
an exchange stop/TP event. The result MUST NOT be fed to the net evaluator
as source-authenticated evidence without independent provenance review.

No imports from trading engine, SQL, Railway, REST, websocket or secrets.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math

COHORT_ID = "SHORT_DOWN_BOS_EXEC_NET_OOS_V1"
SOURCE = "BINANCE_USDM_PUBLIC_AGGTRADE_WS"
INTERVAL_MS = 900_000
HORIZONS = (60, 240)
MAX_EVENTS = 100_000
AUTHORITY = {
    "research_only": True,
    "live_allowed": False,
    "promotion_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "source_independently_authenticated": False,
    "real_fills_proven": False,
    "protected_stop_tp_proven": False,
    "funding_proven": False,
}


def _positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _epoch_ms(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value)) * 1000
        if not d.is_finite() or d != d.to_integral_value():
            return None
        value = int(d)
    except (InvalidOperation, ValueError, OverflowError, TypeError):
        return None
    return value if value > 0 else None


def _int(value):
    return value if type(value) is int and value >= 0 else None


def _ohlc(events, start_ms, end_ms):
    prices = [price for stamp, _, price in events
              if start_ms <= stamp < end_ms]
    if not prices:
        return None
    return {
        "o": prices[0], "h": max(prices), "l": min(prices),
        "c": prices[-1],
    }


def build_path(candidate: dict, capture: dict, *, horizon: int):
    """Return shape-valid post-capture OHLC path or typed missing evidence.

    Capture can originate from a public feed but carries NO trustworthy archive
    attestation until independently anchored. Do not infer no trades from an
    empty event list or reconstruct any segment from whole parent candles.
    """
    def refuse(code):
        return {
            **AUTHORITY, "status": "NET_SOURCE_INCOMPLETE",
            "reason": code, "shape_ready": False, "evaluator_evidence": None,
        }

    if horizon not in HORIZONS:
        return refuse("INVALID_HORIZON")
    if not isinstance(candidate, dict) or not isinstance(capture, dict):
        return refuse("INVALID_INPUT")
    cid, symbol = candidate.get("candidate_id"), candidate.get("symbol")
    start_ms = _epoch_ms(candidate.get("captured_epoch"))
    if (not isinstance(cid, str) or not cid.startswith("HARD_GATE_SHADOW:")
            or not isinstance(symbol, str) or not symbol.endswith("USDT")
            or start_ms is None
            or capture.get("candidate_id") != cid
            or capture.get("symbol") != symbol
            or capture.get("source") != SOURCE
            or capture.get("cohort_id") != COHORT_ID):
        return refuse("IDENTITY_OR_SOURCE_MISMATCH")
    if (capture.get("stream_connected") is not True
            or capture.get("stream_gap_count") != 0
            or type(capture.get("stream_gap_count")) is not int
            or _int(capture.get("generation")) is None
            or capture["generation"] == 0):
        return refuse("STREAM_CONTINUITY_UNPROVEN")
    finish_ms = start_ms + horizon * 60_000
    coverage_start = _int(capture.get("observed_from_ms"))
    coverage_end = _int(capture.get("observed_until_ms"))
    if (coverage_start is None or coverage_end is None
            or coverage_start > start_ms or coverage_end < finish_ms):
        return refuse("TIME_COVERAGE_MISSING")
    events = capture.get("events")
    if not isinstance(events, list) or not 2 <= len(events) <= MAX_EVENTS:
        return refuse("TRADES_MISSING_OR_UNBOUNDED")
    parsed = []
    last_id = last_time = None
    for raw in events:
        if not isinstance(raw, dict):
            return refuse("MALFORMED_TRADE")
        stamp, trade_id = _int(raw.get("T")), _int(raw.get("a"))
        price = _positive(raw.get("p"))
        if (raw.get("e") != "aggTrade" or raw.get("s") != symbol
                or stamp is None or trade_id is None or price is None
                or (last_id is not None and trade_id != last_id + 1)
                or (last_time is not None and stamp < last_time)):
            return refuse("TRADE_ID_TIME_OR_PRICE_GAP")
        parsed.append((stamp, trade_id, price))
        last_id, last_time = trade_id, stamp
    if (parsed[0][0] > start_ms or parsed[-1][0] < finish_ms
            or parsed[0][0] < coverage_start
            or parsed[-1][0] > coverage_end):
        return refuse("BOUNDARY_WITNESS_MISSING")
    if (_int(capture.get("first_agg_id")) != parsed[0][1]
            or _int(capture.get("last_agg_id")) != parsed[-1][1]):
        return refuse("TRADE_ID_ANCHOR_MISMATCH")
    boundary = ((start_ms + INTERVAL_MS - 1) // INTERVAL_MS) * INTERVAL_MS
    unaligned = boundary > start_ms
    full_n = horizon // 15 - (1 if unaligned else 0)
    full_start = boundary
    full_end = boundary + full_n * INTERVAL_MS
    bars = []
    for i in range(full_n):
        b_start = full_start + i * INTERVAL_MS
        block = _ohlc(parsed, b_start, b_start + INTERVAL_MS)
        if block is None:
            return refuse("EMPTY_FULL_BAR")
        bars.append({"ts": b_start // 1000, **block})
    entry_partial = exit_partial = None
    if unaligned:
        first = _ohlc(parsed, start_ms, boundary)
        last = _ohlc(parsed, full_end, finish_ms)
        if first is None or last is None:
            return refuse("EMPTY_PARTIAL_BOUNDARY")
        entry_partial = {
            "start_epoch": start_ms / 1000, "end_epoch": boundary / 1000,
            **first,
        }
        exit_partial = {
            "start_epoch": full_end / 1000, "end_epoch": finish_ms / 1000,
            **last,
        }
    # The original #610 study needs independent source authenticity, real
    # executed fee/funding/quantity and protections. Never fill those fields
    # using a public trade tape or output an evaluator-ready claim.
    return {
        **AUTHORITY,
        "status": "SHAPE_VALID_SOURCE_UNATTESTED",
        "reason": "INDEPENDENT_IMMUTABLE_ARCHIVE_AND_MARKET_EXECUTION_PROOF_REQUIRED",
        "shape_ready": True,
        "candidate_id": cid,
        "symbol": symbol,
        "horizon_min": horizon,
        "first_agg_id": parsed[0][1],
        "last_agg_id": parsed[-1][1],
        "event_count": len(parsed),
        "bars": bars,
        "entry_partial_window": entry_partial,
        "exit_partial_window": exit_partial,
        "evaluator_evidence": None,
        "evidence_can_authorize_promotion": False,
    }
