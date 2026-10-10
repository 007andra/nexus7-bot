"""Read-only Binance USDM public historical evidence importer for #609.

No exchange credentials, orders, database writes, or LIVE side effects.
Fetch via injected transport for deterministic tests. Public URLs only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import urlopen

BASE_URL = "https://fapi.binance.com"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
MAX_LIMIT = 1500


class EvidenceError(ValueError):
    pass


def _http_get(url: str) -> bytes:
    with urlopen(url, timeout=15) as response:
        return response.read()


def _utc_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise EvidenceError("UTC_OFFSET_REQUIRED")
    return int(dt.timestamp() * 1000)


def _get(endpoint: str, params: dict, transport) -> tuple[object, dict]:
    url = BASE_URL + endpoint + "?" + urlencode(params)
    raw = transport(url)
    if not isinstance(raw, bytes):
        raise EvidenceError("TRANSPORT_MUST_RETURN_BYTES")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvidenceError("INVALID_JSON") from exc
    if not isinstance(obj, list):
        raise EvidenceError("UNEXPECTED_API_RESPONSE")
    return obj, {"endpoint": endpoint, "params": params.copy(),
                 "sha256": digest, "byte_length": len(raw)}


def fetch_klines(*, symbol: str, start_utc: str, end_utc: str,
                 interval: str = "1m", transport=_http_get) -> dict:
    """Closed historical klines only, exact requested UTC grid, fail closed.

    start inclusive and end exclusive; both must align to candle boundaries.
    This is a research fetch, not a proof of fillability or market impact.
    """
    if not symbol.isascii() or not symbol.isalnum() or symbol.upper() != symbol:
        raise EvidenceError("INVALID_SYMBOL")
    if interval not in INTERVAL_MS:
        raise EvidenceError("UNSUPPORTED_INTERVAL")
    step = INTERVAL_MS[interval]
    start, end = _utc_ms(start_utc), _utc_ms(end_utc)
    if start >= end or start % step or end % step:
        raise EvidenceError("INVALID_OR_UNALIGNED_WINDOW")
    if end > int(datetime.now(timezone.utc).timestamp() * 1000) - step:
        raise EvidenceError("WINDOW_NOT_FINALIZED")
    candles, requests = [], []
    cursor = start
    while cursor < end:
        limit = min(MAX_LIMIT, (end - cursor) // step)
        params = {"symbol": symbol, "interval": interval,
                  "startTime": cursor, "endTime": cursor + limit * step - 1,
                  "limit": limit}
        batch, provenance = _get("/fapi/v1/klines", params, transport)
        requests.append(provenance)
        if len(batch) != limit:
            raise EvidenceError("MISSING_KLINES")
        for index, item in enumerate(batch):
            if not isinstance(item, list) or len(item) < 7:
                raise EvidenceError("MALFORMED_KLINE")
            opening = int(item[0])
            expected = cursor + index * step
            if opening != expected or int(item[6]) != expected + step - 1:
                raise EvidenceError("KLINE_TIME_GAP_OR_MISMATCH")
            op, high, low, close = (Decimal(str(item[i])) for i in (1, 2, 3, 4))
            if min(op, high, low, close) <= 0 or high < max(op, close) or low > min(op, close):
                raise EvidenceError("INVALID_OHLC")
            candles.append({"open_time_ms": opening, "close_time_ms": int(item[6]),
                            "open": str(op), "high": str(high),
                            "low": str(low), "close": str(close)})
        cursor += limit * step
    return {"source": "BINANCE_USDM_PUBLIC_KLINES", "symbol": symbol,
            "interval": interval, "start_utc": start_utc, "end_utc": end_utc,
            "candles": candles, "request_provenance": requests,
            "complete": len(candles) == (end - start) // step,
            "live_allowed": False, "execution_effect": "NONE"}


def fetch_funding_events(*, symbol: str, start_utc: str, end_utc: str,
                         transport=_http_get) -> dict:
    """Retrieve settled funding events; pagination with strict monotonic timestamps.

    A funding event alone does not establish whether a hypothetical position was
    held through settlement, or what notional should be charged.
    """
    start, end = _utc_ms(start_utc), _utc_ms(end_utc)
    if start >= end:
        raise EvidenceError("INVALID_WINDOW")
    events, requests = [], []
    cursor = start
    while cursor < end:
        params = {"symbol": symbol, "startTime": cursor,
                  "endTime": end - 1, "limit": 1000}
        batch, provenance = _get("/fapi/v1/fundingRate", params, transport)
        requests.append(provenance)
        if not batch:
            break
        last = None
        for item in batch:
            ts = int(item["fundingTime"])
            if not cursor <= ts < end or (last is not None and ts <= last):
                raise EvidenceError("FUNDING_ORDER_OR_WINDOW_INVALID")
            rate = Decimal(str(item["fundingRate"]))
            events.append({"funding_time_ms": ts, "funding_rate": str(rate),
                           "mark_price": str(item.get("markPrice", ""))})
            last = ts
        if len(batch) < 1000:
            break
        cursor = last + 1
    return {"source": "BINANCE_USDM_PUBLIC_FUNDING", "symbol": symbol,
            "events": events, "request_provenance": requests,
            "live_allowed": False, "execution_effect": "NONE"}
