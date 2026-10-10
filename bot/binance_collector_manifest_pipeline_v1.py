"""Offline manifest round-trip for already collected Binance financial history.

No HTTP calls, exchange credentials, order submission or LIVE state changes.
The caller's terminal evidence is not independently authenticated.
"""
from __future__ import annotations

from bot.binance_coverage_manifest_builder_v1 import build_manifest
from bot.binance_coverage_manifest_verifier_v1 import verify_coverage_manifest


def validate_collector_receipts(*, records_by_source, required_sources,
                                start_ms, end_ms, terminal_by_source):
    missing = {"status": "PROOF_MISSING", "live_allowed": False,
               "execution_effect": "NONE"}
    if (not isinstance(required_sources, list) or not required_sources
            or len(set(required_sources)) != len(required_sources)
            or not isinstance(records_by_source, dict)
            or not isinstance(terminal_by_source, dict)
            or set(records_by_source) != set(required_sources)
            or set(terminal_by_source) != set(required_sources)):
        return missing
    manifests = {}
    for source in required_sources:
        built = build_manifest(records=records_by_source[source],
                               start_ms=start_ms, end_ms=end_ms,
                               terminal_page_verified=terminal_by_source[source])
        if built.get("status") != "MANIFEST_BUILT":
            return missing
        manifests[source] = built["parts"]
    result = verify_coverage_manifest(
        manifests=manifests, required_sources=required_sources,
        start_ms=start_ms, end_ms=end_ms,
        records_by_source=records_by_source)
    if result.get("status") != "MANIFEST_SHAPE_VALID":
        return missing
    return {"status": "COLLECTOR_RECEIPTS_SHAPE_VALID",
            "manifests": manifests, "manifest_digests": result["manifest_digests"],
            "scope": "OFFLINE_INTEGRITY_ONLY_NOT_EXCHANGE_COMPLETENESS",
            "live_allowed": False, "execution_effect": "NONE"}
