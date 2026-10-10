"""Bounded, GET-only Binance USD-M user-trade history by symbol.

Uses fromId for deterministic cursor progression, with startTime/endTime
on initial query. This is opt-in audit plumbing, not an engine hook.
"""
from __future__ import annotations


async def collect_symbol_fills(*, client, symbol: str, start_ms: int,
                               end_ms: int, limit: int = 1000,
                               max_pages: int = 50) -> dict:
    missing = {"status": "PROOF_MISSING", "reason": "FILLS_HISTORY_INCOMPLETE",
               "execution_effect": "NONE", "live_allowed": False}
    if not (isinstance(symbol, str) and symbol.isalnum() and symbol.endswith("USDT")
            and isinstance(start_ms, int) and isinstance(end_ms, int)
            and 0 < start_ms < end_ms and end_ms - start_ms <= 7 * 24 * 60 * 60 * 1000 and 1 <= limit <= 1000
            and isinstance(max_pages, int) and max_pages > 0):
        return missing
    records = []
    seen = set()
    cursor = None
    try:
        for _ in range(max_pages):
            params = {"symbol": symbol, "limit": limit}
            if cursor is None:
                params.update({"startTime": start_ms, "endTime": end_ms})
            else:
                # fromId continuation may not support combined time bounds.
                # Until endpoint compatibility is verified, fail closed
                # instead of asserting complete multi-page coverage.
                return missing
            page = await client._get("/fapi/v1/userTrades", params, auth=True)
            if not isinstance(page, list) or len(page) > limit:
                return missing
            last_id = None
            for row in page:
                if not isinstance(row, dict) or row.get("symbol") != symbol:
                    return missing
                identity = int(row["id"])
                timestamp = int(row["time"])
                if identity < 0 or identity in seen or (last_id is not None and identity <= last_id):
                    return missing
                # fromId continuation can extend beyond the window. Stop only
                # after verifying monotonically increasing time and ID.
                if records and timestamp < int(records[-1]["time"]):
                    return missing
                if timestamp < start_ms:
                    return missing
                if timestamp > end_ms:
                    return {"status": "FILLS_WINDOW_SHAPE_VALID", "records": records,
                            "start_ms": start_ms, "end_ms": end_ms,
                            "terminal_page_verified": True,
                            "scope": "BOUNDED_SHAPE_ONLY_NOT_EXCHANGE_COMPLETENESS",
                            "execution_effect": "NONE", "live_allowed": False}
                seen.add(identity)
                last_id = identity
                records.append(row)
            if len(page) < limit:
                return {"status": "FILLS_WINDOW_SHAPE_VALID", "records": records,
                        "scope": "BOUNDED_SHAPE_ONLY_NOT_EXCHANGE_COMPLETENESS",
                        "execution_effect": "NONE", "live_allowed": False}
            if last_id is None:
                return missing
            cursor = last_id + 1
        return missing
    except Exception:
        return missing
