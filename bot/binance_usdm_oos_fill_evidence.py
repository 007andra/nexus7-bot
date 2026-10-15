"""Fail-closed independent hypothetical fill evidence contract for #609.

Historical bar touches do not establish fills. This validates evidence records,
not actual exchange execution, and grants no LIVE permissions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def _finite_positive(value) -> bool:
    try:
        x = Decimal(str(value))
        return x.is_finite() and x > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def validate_fill_evidence(*, candidate_id: str, decision_epoch_ms: int,
                           horizon_end_ms: int, entry: dict, exit: dict) -> dict:
    missing = []
    if not candidate_id or not isinstance(decision_epoch_ms, int) or not isinstance(horizon_end_ms, int):
        missing.append("INVALID_CANDIDATE_OR_CLOCK")
    elif decision_epoch_ms >= horizon_end_ms:
        missing.append("INVALID_HORIZON")
    for label, record in (("ENTRY", entry), ("EXIT", exit)):
        if not isinstance(record, dict):
            missing.append(label + "_RECORD_MISSING")
            continue
        if record.get("candidate_id") != candidate_id:
            missing.append(label + "_CANDIDATE_MISMATCH")
        if record.get("venue") != "BINANCE_USDM":
            missing.append(label + "_VENUE_UNVERIFIED")
        if record.get("evidence_kind") not in ("ORDERBOOK_SNAPSHOT", "TRADES_REPLAY"):
            missing.append(label + "_INDEPENDENT_TICK_DATA_MISSING")
        if not record.get("source_sha256") or len(str(record["source_sha256"])) != 64:
            missing.append(label + "_SOURCE_DIGEST_MISSING")
        if not _finite_positive(record.get("price")):
            missing.append(label + "_PRICE_INVALID")
        if not _finite_positive(record.get("quantity")):
            missing.append(label + "_QUANTITY_INVALID")
        ts = record.get("timestamp_ms")
        if not isinstance(ts, int) or not isinstance(decision_epoch_ms, int) or not isinstance(horizon_end_ms, int) or not decision_epoch_ms <= ts <= horizon_end_ms:
            missing.append(label + "_TIME_OUTSIDE_DECISION_HORIZON")
        if record.get("independently_verified") is not True:
            missing.append(label + "_INDEPENDENT_VERIFICATION_MISSING")
    if isinstance(entry, dict) and isinstance(exit, dict):
        a, b = entry.get("timestamp_ms"), exit.get("timestamp_ms")
        if isinstance(a, int) and isinstance(b, int) and b < a:
            missing.append("EXIT_BEFORE_ENTRY")
        if entry.get("quantity") != exit.get("quantity"):
            missing.append("QUANTITY_MISMATCH")
    return {"status": "FILL_EVIDENCE_VERIFIED" if not missing else "NET_PROOF_MISSING",
            "missing": missing, "candidate_id": candidate_id,
            "live_allowed": False, "promotion_allowed": False,
            "execution_effect": "NONE"}
