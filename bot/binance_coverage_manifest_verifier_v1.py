"""Independently check deterministic source coverage manifests.

This verifies arithmetic and manifest consistency, not authenticity or
completeness of exchange records. It cannot authorize live execution.
"""
from __future__ import annotations

import hashlib
import json


def verify_coverage_manifest(*, manifests, required_sources, start_ms, end_ms, records_by_source=None):
    missing = {"status": "PROOF_MISSING", "reason": "COVERAGE_MANIFEST_INVALID",
               "execution_effect": "NONE", "live_allowed": False}
    if not isinstance(manifests, dict) or not isinstance(required_sources, list):
        return missing
    if not required_sources or len(set(required_sources)) != len(required_sources):
        return missing
    if set(manifests) != set(required_sources):
        return missing
    if not isinstance(records_by_source, dict) or set(records_by_source) != set(required_sources):
        return missing
    try:
        if not isinstance(start_ms, int) or not isinstance(end_ms, int) or start_ms >= end_ms:
            return missing
        digests = {}
        for source in required_sources:
            parts = manifests[source]
            if not isinstance(parts, list) or not parts:
                return missing
            source_records = records_by_source[source]
            if not isinstance(source_records, list):
                return missing
            cursor = start_ms
            used = set()
            for part in parts:
                if not isinstance(part, dict) or part.get("start_ms") != cursor:
                    return missing
                upper = part.get("end_ms")
                if not isinstance(upper, int) or upper < cursor or upper > end_ms:
                    return missing
                count = part.get("record_count")
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    return missing
                if part.get("terminal_page_verified") is not True:
                    return missing
                indices = [i for i, row in enumerate(source_records)
                           if isinstance(row, dict) and isinstance(row.get("time"), int)
                           and cursor <= row["time"] <= upper]
                if len(indices) != count or any(i in used for i in indices):
                    return missing
                used.update(indices)
                payload = [source_records[i] for i in indices]
                actual = hashlib.sha256(json.dumps(payload, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                digest = part.get("sha256")
                if not isinstance(digest, str) or len(digest) != 64 or any(
                        ch not in "0123456789abcdef" for ch in digest):
                    return missing
                if actual != digest:
                    return missing
                cursor = upper + 1
            if len(used) != len(source_records):
                return missing
            if cursor != end_ms + 1:
                return missing
            canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
            digests[source] = hashlib.sha256(canonical.encode()).hexdigest()
        return {"status": "MANIFEST_SHAPE_VALID", "manifest_digests": digests,
                "scope": "MANIFEST_CONSISTENCY_ONLY_NOT_INDEPENDENT_EXCHANGE_PROOF",
                "execution_effect": "NONE", "live_allowed": False}
    except (TypeError, ValueError):
        return missing
