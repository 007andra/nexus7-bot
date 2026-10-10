"""Explicit, bounded, read-only Binance USD-M income history collector.

This is research evidence plumbing, not a runtime engine hook. It never
submits/cancels orders or writes financial state. The caller supplies a
read-only authenticated client. Incomplete windows fail closed.
"""
from __future__ import annotations

from bot.binance_readonly_pagination_proof_v1 import validate_pages


async def collect_income_window(*, client, start_ms: int, end_ms: int,
                                limit: int = 1000, max_pages: int = 50) -> dict:
    """Fetch income history by timestamp, requiring a provable terminal page.

    Timestamp cursor uses +1 ms after the final timestamp. Binance may return
    multiple income records at the same millisecond: if a full page ends at
    the same timestamp as any prior item in that page, do not advance, since
    advancing could silently drop additional records.
    """
    fail = {"status": "PROOF_MISSING", "reason": "INCOME_HISTORY_INCOMPLETE",
            "execution_effect": "NONE", "live_allowed": False}
    if not (isinstance(start_ms, int) and isinstance(end_ms, int) and
            0 < start_ms < end_ms and 1 <= limit <= 1000 and max_pages > 0):
        return fail
    pages = []
    cursor = start_ms
    try:
        for _ in range(max_pages):
            result = await client._get("/fapi/v1/income",
                                       {"startTime": cursor, "endTime": end_ms,
                                        "limit": limit}, auth=True)
            if not isinstance(result, list) or len(result) > limit:
                return fail
            pages.append(result)
            if len(result) < limit:
                proof = validate_pages(pages=pages, window_start_ms=start_ms,
                                       window_end_ms=end_ms, page_limit=limit,
                                       id_field="tranId")
                if proof["status"] != "PAGINATION_SHAPE_VALID":
                    return fail
                return {"status": "INCOME_WINDOW_SHAPE_VALID",
                        "scope": "PAGINATED_READ_SHAPE_ONLY_NOT_FULL_LEDGER_PROOF",
                        "records": result if len(pages) == 1 else [r for p in pages for r in p],
                        "page_count": len(pages), "execution_effect": "NONE",
                        "live_allowed": False}
            final_ts = int(result[-1]["time"])
            if any(int(row["time"]) == final_ts for row in result[:-1]):
                return fail
            if final_ts < cursor or final_ts >= end_ms:
                return fail
            cursor = final_ts + 1
        return fail
    except Exception:
        return fail
