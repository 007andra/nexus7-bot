"""Fail-closed time-window orchestrator for Binance USD-M userTrades.

No exchange writes, no automatic execution. Each interval is independently
queried using the bounded GET-only collector. Saturated pages never count as
complete; there is no fromId/time parameter combination.
"""
from __future__ import annotations

from bot.binance_readonly_fills_history_collector_v1 import collect_symbol_fills

MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


async def collect_fills_windows(*, client, symbol: str, start_ms: int,
                                end_ms: int, window_ms: int = MAX_WINDOW_MS,
                                limit: int = 1000, max_windows: int = 100) -> dict:
    missing = {"status": "PROOF_MISSING", "reason": "FILLS_WINDOW_COVERAGE_UNPROVEN",
               "execution_effect": "NONE", "live_allowed": False}
    if not (isinstance(start_ms, int) and isinstance(end_ms, int)
            and isinstance(window_ms, int) and isinstance(max_windows, int)
            and 0 < start_ms < end_ms and 0 < window_ms <= MAX_WINDOW_MS
            and max_windows > 0):
        return missing
    # Inclusive timestamps: adjacent intervals share no milliseconds.
    cursor = start_ms
    records = []
    identities = set()
    windows = 0
    while cursor <= end_ms:
        if windows >= max_windows:
            return missing
        upper = min(end_ms, cursor + window_ms - 1)
        if upper <= cursor:
            # Single-millisecond windows are not supported by the underlying
            # bounded collector (end must exceed start).
            return missing
        result = await collect_symbol_fills(
            client=client, symbol=symbol, start_ms=cursor, end_ms=upper,
            limit=limit)
        if result.get("status") != "FILLS_WINDOW_SHAPE_VALID":
            return missing
        rows = result.get("records")
        if not isinstance(rows, list):
            return missing
        for row in rows:
            key = (row.get("symbol"), row.get("id"))
            if key in identities:
                return missing
            identities.add(key)
            records.append(row)
        windows += 1
        cursor = upper + 1
    return {"status": "FILLS_WINDOWS_SHAPE_VALID", "records": records,
            "window_count": windows,
            "scope": "WINDOW_SHAPE_ONLY_NOT_EXCHANGE_COMPLETENESS",
            "execution_effect": "NONE", "live_allowed": False}
