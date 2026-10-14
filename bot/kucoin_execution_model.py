"""KuCoin Futures public market-data readers for research replays.

The historical OOS replay evidence was built on KuCoin public data; these
readers keep it reproducible. They never touch the trading adapter (removed)
and never place, change or close orders. The venue-neutral cost formulas live
in bot.execution_model.
"""
from __future__ import annotations

import math

from bot.execution_model import _finite, configured_taker_fee
from bot.logger import log

_FUNDING_CHUNK_MS = 90 * 24 * 60 * 60 * 1000
_BASE_ALIAS = {"BTC": "XBT"}  # KuCoin names Bitcoin XBT


def to_kucoin(symbol: str) -> str:
    """BTCUSDT -> XBTUSDTM, the static rule of the former trading adapter."""
    if not symbol.endswith("USDT"):
        return symbol
    base = symbol[:-4]
    return f"{_BASE_ALIAS.get(base, base)}USDTM"


async def fetch_actual_taker_fee(client, symbol: str) -> tuple[float, str]:
    """Read the account-specific KuCoin Futures taker fee, with safe fallback."""
    fallback = configured_taker_fee()
    if not hasattr(client, "_get"):
        return fallback, "env_fallback"
    try:
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
