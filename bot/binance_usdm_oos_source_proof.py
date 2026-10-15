"""Research-only byte-level evidence provenance verification for #609.

Never trust a boolean self-attestation or a digest string without source bytes.
No orders, exchange credentials, database writes, or LIVE permissions.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation


def verify_source_bytes(*, raw: bytes, expected_sha256: str,
                        expected_venue: str = "BINANCE_USDM") -> dict:
    blockers = []
    if expected_venue != "BINANCE_USDM":
        blockers.append("VENUE_MISMATCH")
    if not isinstance(raw, bytes) or not raw:
        blockers.append("RAW_BYTES_MISSING")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        blockers.append("DIGEST_INVALID")
    if blockers:
        return {"verified": False, "blockers": blockers, "live_allowed": False}
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256.lower():
        blockers.append("SHA256_MISMATCH")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        blockers.append("INVALID_SOURCE_JSON")
        payload = None
    if not isinstance(payload, list):
        blockers.append("UNEXPECTED_SOURCE_SHAPE")
    return {"verified": not blockers, "blockers": blockers,
            "sha256": actual, "record_count": len(payload) if isinstance(payload, list) else 0,
            "live_allowed": False, "execution_effect": "NONE"}


def verify_trade_record(*, record: dict, raw: bytes, expected_sha256: str,
                        candidate_id: str, decision_epoch_ms: int,
                        horizon_end_ms: int) -> dict:
    """Bind a candidate tick to exact source bytes, not a claimed verification flag.

    Raw JSON must be a list of trades; matching trade ID, price, qty and time
    is necessary but does not prove hypothetical order fillability.
    """
    source = verify_source_bytes(raw=raw, expected_sha256=expected_sha256)
    blockers = list(source["blockers"])
    try:
        trades = json.loads(raw.decode("utf-8")) if source["verified"] else []
        if not isinstance(record, dict) or not isinstance(decision_epoch_ms, int) or not isinstance(horizon_end_ms, int):
            raise ValueError("INVALID_RECORD")
        if record.get("candidate_id") != candidate_id or record.get("venue") != "BINANCE_USDM":
            blockers.append("CANDIDATE_OR_VENUE_MISMATCH")
        ts = record.get("timestamp_ms")
        if not isinstance(ts, int) or not decision_epoch_ms <= ts <= horizon_end_ms:
            blockers.append("TICK_OUTSIDE_HORIZON")
        price = Decimal(str(record.get("price")))
        qty = Decimal(str(record.get("quantity")))
        if not price.is_finite() or not qty.is_finite() or price <= 0 or qty <= 0:
            blockers.append("INVALID_PRICE_OR_QTY")
        match = any(
            isinstance(t, dict)
            and t.get("id") == record.get("trade_id")
            and t.get("time") == ts
            and str(t.get("price")) == str(record.get("price"))
            and str(t.get("qty")) == str(record.get("quantity"))
            for t in trades
        )
        if not match:
            blockers.append("TRADE_NOT_IN_AUTHENTIC_SOURCE")
    except (UnicodeError, ValueError, InvalidOperation, TypeError):
        blockers.append("INVALID_TRADE_RECORD")
    return {"verified": not blockers, "blockers": blockers,
            "candidate_id": candidate_id, "historical_trade_observed": not blockers,
            "hypothetical_fill_proven": False, "live_allowed": False,
            "execution_effect": "NONE"}
