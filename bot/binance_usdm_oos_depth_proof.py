"""Raw-byte-bound Binance USDM orderbook depth evidence, research only.

A snapshot is evidence of displayed liquidity, not historical queue priority
or a guaranteed executable fill. Never use a client supplied verified flag.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation


def _positive(x):
    try:
        value = Decimal(str(x))
        return value if value.is_finite() and value > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def assess_depth(*, side: str, quantity: str, raw: bytes,
                 sha256: str, snapshot_ms: int, decision_ms: int,
                 max_lag_ms: int = 1000) -> dict:
    blockers = []
    if not isinstance(raw, bytes) or not raw:
        blockers.append("RAW_SNAPSHOT_MISSING")
    if not isinstance(sha256, str) or len(sha256) != 64 or (
            isinstance(raw, bytes) and hashlib.sha256(raw).hexdigest() != sha256.lower()):
        blockers.append("RAW_SHA256_MISMATCH")
    if not isinstance(snapshot_ms, int) or not isinstance(decision_ms, int) or (
            isinstance(snapshot_ms, int) and isinstance(decision_ms, int)
            and not decision_ms <= snapshot_ms <= decision_ms + max_lag_ms):
        blockers.append("SNAPSHOT_OUTSIDE_DECISION_WINDOW")
    if side not in ("SHORT", "LONG"):
        blockers.append("INVALID_SIDE")
    remaining = _positive(quantity)
    if remaining is None:
        blockers.append("INVALID_QUANTITY")
    try:
        book = json.loads(raw.decode("utf-8")) if isinstance(raw, bytes) else {}
    except (UnicodeError, ValueError):
        book = {}
        blockers.append("INVALID_SNAPSHOT_JSON")
    if not isinstance(book, dict) or not isinstance(book.get("bids"), list) or not isinstance(book.get("asks"), list):
        blockers.append("INVALID_DEPTH_SHAPE")
    if blockers:
        return {"status": "NET_PROOF_MISSING", "blockers": blockers,
                "live_allowed": False, "execution_effect": "NONE"}
    levels = book["bids"] if side == "SHORT" else book["asks"]
    previous = None
    total = Decimal("0")
    notional = Decimal("0")
    for level in levels:
        if not isinstance(level, list) or len(level) < 2:
            blockers.append("INVALID_LEVEL")
            break
        price, available = _positive(level[0]), _positive(level[1])
        if price is None or available is None:
            blockers.append("INVALID_LEVEL_VALUE")
            break
        if previous is not None and ((side == "SHORT" and price >= previous) or
                                     (side == "LONG" and price <= previous)):
            blockers.append("UNSORTED_OR_DUPLICATE_DEPTH")
            break
        previous = price
        take = min(remaining, available)
        notional += take * price
        total += take
        remaining -= take
        if remaining == 0:
            break
    if remaining != 0:
        blockers.append("INSUFFICIENT_DISPLAYED_DEPTH")
    if blockers:
        return {"status": "NET_PROOF_MISSING", "blockers": blockers,
                "live_allowed": False, "execution_effect": "NONE"}
    return {"status": "DISPLAYED_DEPTH_BOUND", "weighted_price": str(notional / total),
            "quantity": str(total), "snapshot_sha256": sha256.lower(),
            "snapshot_timestamp_ms": snapshot_ms,
            "hypothetical_fill_proven": False, "promotion_allowed": False,
            "live_allowed": False, "execution_effect": "NONE"}
