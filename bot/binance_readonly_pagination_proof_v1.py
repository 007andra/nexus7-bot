"""Fail-closed completeness proof for paginated Binance USD-M historical reads.

Pure offline verifier: the caller supplies authenticated page records and
documented query boundaries. No network I/O, order placement or ledger writes.
"""
from __future__ import annotations


def validate_pages(*, pages, window_start_ms, window_end_ms,
                   page_limit, id_field, time_field="time"):
    """Return only bounded evidence; never infer completeness from a full page.

    Each page is an ordered list of records. A terminal page must contain
    fewer than page_limit records, and non-terminal pages must have full size.
    Overlap, missing identity, nonmonotonic order, or out-of-window events
    invalidate proof. Empty terminal page is allowed.
    """
    missing = {"status": "PROOF_MISSING", "reason": "PAGINATION_UNVERIFIED",
               "execution_effect": "NONE", "live_allowed": False}
    try:
        start, end, limit = int(window_start_ms), int(window_end_ms), int(page_limit)
        if start <= 0 or end <= start or limit < 1 or not isinstance(pages, list) or not pages:
            return missing
        if not isinstance(id_field, str) or not id_field:
            return missing
        ids = set()
        last = None
        count = 0
        for index, page in enumerate(pages):
            if not isinstance(page, list) or len(page) > limit:
                return missing
            if index < len(pages) - 1 and len(page) != limit:
                return missing
            for record in page:
                if not isinstance(record, dict) or id_field not in record or time_field not in record:
                    return missing
                identity = str(record[id_field])
                ts = int(record[time_field])
                if not identity or identity in ids or not start <= ts <= end:
                    return missing
                if last is not None and ts < last:
                    return missing
                ids.add(identity)
                last = ts
                count += 1
        if len(pages[-1]) >= limit:
            return missing
        return {"status": "PAGINATION_SHAPE_VALID",
                "scope": "PAGE_SHAPE_ONLY_NOT_EXCHANGE_COMPLETENESS_ATTESTATION",
                "record_count": count, "page_count": len(pages),
                "execution_effect": "NONE", "live_allowed": False}
    except (TypeError, ValueError, KeyError, OverflowError):
        return missing
