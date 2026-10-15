"""Byte-bound Binance USDM exchangeInfo filter parser for research #609.

A current exchangeInfo response is NOT proof of historical filters at decision
time. Require independently archived acquisition time and matching source bytes.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation


def _positive(value):
    try:
        x = Decimal(str(value))
        return x if x.is_finite() and x > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def verify_exchange_filters(*, raw: bytes, sha256: str, symbol: str,
                            acquired_ms: int, decision_ms: int) -> dict:
    blockers = []
    if not isinstance(raw, bytes) or not raw:
        blockers.append("RAW_EXCHANGE_INFO_MISSING")
    if not isinstance(sha256, str) or len(sha256) != 64 or (
            isinstance(raw, bytes) and hashlib.sha256(raw).hexdigest() != sha256.lower()):
        blockers.append("EXCHANGE_INFO_SHA256_MISMATCH")
    if not isinstance(acquired_ms, int) or not isinstance(decision_ms, int) or (
            isinstance(acquired_ms, int) and isinstance(decision_ms, int)
            and acquired_ms > decision_ms):
        blockers.append("HISTORICAL_FILTER_SNAPSHOT_MISSING")
    try:
        payload = json.loads(raw.decode("utf-8")) if isinstance(raw, bytes) else {}
    except (UnicodeError, ValueError):
        payload = {}
        blockers.append("INVALID_EXCHANGE_INFO_JSON")
    matches = [s for s in payload.get("symbols", []) if isinstance(s, dict)
               and s.get("symbol") == symbol] if isinstance(payload, dict) and isinstance(payload.get("symbols"), list) else []
    if len(matches) != 1:
        blockers.append("SYMBOL_NOT_UNIQUE_OR_MISSING")
    values = {}
    if len(matches) == 1:
        sym = matches[0]
        if sym.get("status") != "TRADING":
            blockers.append("SYMBOL_NOT_TRADING")
        filters = sym.get("filters", [])
        for filter_type, fields in (
            ("LOT_SIZE", ("minQty", "stepSize")),
            ("MARKET_LOT_SIZE", ("minQty", "stepSize")),
            ("MIN_NOTIONAL", ("notional",)),
            ("PRICE_FILTER", ("tickSize",)),
        ):
            found = [f for f in filters if isinstance(f, dict) and f.get("filterType") == filter_type] if isinstance(filters, list) else []
            if len(found) != 1:
                blockers.append("FILTER_MISSING_" + filter_type)
                continue
            for field in fields:
                value = _positive(found[0].get(field))
                if value is None:
                    blockers.append("INVALID_" + filter_type + "_" + field)
                else:
                    values[filter_type + "_" + field] = str(value)
    if blockers:
        return {"verified": False, "blockers": blockers, "live_allowed": False,
                "execution_effect": "NONE"}
    return {"verified": True, "symbol": symbol,
            "min_qty": values["MARKET_LOT_SIZE_minQty"],
            "step_size": values["MARKET_LOT_SIZE_stepSize"],
            "min_notional": values["MIN_NOTIONAL_notional"],
            "tick_size": values["PRICE_FILTER_tickSize"],
            "lot_min_qty": values["LOT_SIZE_minQty"],
            "source_sha256": sha256.lower(), "acquired_ms": acquired_ms,
            "historical_snapshot_claim_only": True,
            "live_allowed": False, "execution_effect": "NONE"}
