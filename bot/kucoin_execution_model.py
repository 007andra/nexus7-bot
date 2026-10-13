"""KuCoin Futures readers for the execution-cost model.

The venue-neutral formulas live in bot.execution_model.
"""
from __future__ import annotations

import math

from bot.execution_model import _finite, configured_taker_fee
from bot.logger import log

_FUNDING_CHUNK_MS = 90 * 24 * 60 * 60 * 1000


async def fetch_actual_taker_fee(client, symbol: str) -> tuple[float, str]:
    """Read the account-specific KuCoin Futures taker fee, with safe fallback."""
    fallback = configured_taker_fee()
    if not hasattr(client, "_get"):
        return fallback, "env_fallback"
    try:
        from bot.kucoin import to_kucoin
        data = await client._get(
            "/api/v1/trade-fees",
            {"symbol": to_kucoin(symbol)},
            auth=True,
        )
        rate = _finite(data.get("takerFeeRate")) if isinstance(data, dict) else 0.0
        if 0 <= rate < 0.02 and (rate > 0 or str(data.get("takerFeeRate", "")) in ("0", "0.0")):
            return rate, "kucoin_actual_fee"
    except Exception as exc:
        log.warning(f"[BACKTEST_FEE] {symbol}: actual fee unavailable ({type(exc).__name__}); fallback")
    return fallback, "env_fallback"


async def fetch_public_funding_history(
    client,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Fetch/dedupe KuCoin public funding settlement events for a time range."""
    if not hasattr(client, "_get") or end_ms <= start_ms:
        return []
    try:
        from bot.kucoin import to_kucoin
        kucoin_symbol = to_kucoin(symbol)
        by_ts: dict[int, dict] = {}
        cursor = int(start_ms)
        final = int(end_ms)
        while cursor <= final:
            chunk_end = min(final, cursor + _FUNDING_CHUNK_MS - 1)
            data = await client._get(
                "/api/v1/contract/funding-rates",
                {"symbol": kucoin_symbol, "from": str(cursor), "to": str(chunk_end)},
                auth=False,
            )
            rows = data if isinstance(data, list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tp = int(row.get("timepoint", 0) or 0)
                rate = _finite(row.get("fundingRate"))
                if tp > 0 and math.isfinite(rate):
                    by_ts[tp] = {"timepoint": tp, "fundingRate": rate}
            cursor = chunk_end + 1
        return [by_ts[k] for k in sorted(by_ts)]
    except Exception as exc:
        log.warning(f"[BACKTEST_FUNDING] {symbol}: history unavailable ({type(exc).__name__}); no synthetic charge")
        return []
