"""Build deterministic offline manifests from collector-returned raw records.

This is a reproducible integrity receipt, not an independent attestation of
exchange origin or pagination completeness. Never authorizes live trading.
"""
from __future__ import annotations

import hashlib
import json


def build_manifest(*, records, start_ms, end_ms, terminal_page_verified):
    missing = {"status": "PROOF_MISSING", "live_allowed": False}
    if (not isinstance(records, list) or not isinstance(start_ms, int)
            or isinstance(start_ms, bool) or not isinstance(end_ms, int)
            or isinstance(end_ms, bool) or start_ms >= end_ms
            or terminal_page_verified is not True):
        return missing
    if any(not isinstance(row, dict) or not isinstance(row.get("time"), int)
           or isinstance(row.get("time"), bool)
           or not start_ms <= row["time"] <= end_ms for row in records):
        return missing
    try:
        payload = json.dumps(records, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return missing
    return {"status": "MANIFEST_BUILT",
            "parts": [{"start_ms": start_ms, "end_ms": end_ms,
                       "record_count": len(records),
                       "terminal_page_verified": True,
                       "sha256": hashlib.sha256(payload.encode()).hexdigest()}],
            "scope": "COLLECTOR_RECORD_INTEGRITY_NOT_EXCHANGE_COMPLETENESS",
            "live_allowed": False, "execution_effect": "NONE"}
