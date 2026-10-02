"""Read-only Binance USD-M historical OOS replay for market-language research."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time
from typing import Iterable

import aiohttp

from bot.market_language_oos import evaluate_market_language_oos, market_language_promotion_decision


BINANCE_FAPI = "https://fapi.binance.com"


class PublicBinanceFuturesClient:
    """Public GET-only Binance USD-M client. No keys and no mutation methods."""

    def __init__(self, base_url: str = BINANCE_FAPI) -> None:
        self.base_url = base_url.rstrip("/")
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session is not None:
            await self.session.close()

    async def get(self, path: str, params: dict) -> object:
        if self.session is None:
            raise RuntimeError("client session not started")
        if not path.startswith("/fapi/v1/"):
            raise ValueError("only Binance USD-M public v1 endpoints are allowed")
        async with self.session.get(self.base_url + path, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()


def _normalize_klines(rows: object, *, now_ms: int) -> list[dict]:
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            open_ts = int(row[0])
            close_ts = int(row[6])
            candle = {
                "ts": open_ts,
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
                "close_ts": close_ts,
            }
        except (IndexError, TypeError, ValueError):
            continue
        if close_ts > int(now_ms):
            continue
        if min(candle["o"], candle["h"], candle["l"], candle["c"]) <= 0:
            continue
        if candle["h"] < candle["l"]:
            continue
        out.append(candle)
    dedup = {row["ts"]: row for row in out}
    return [dedup[k] for k in sorted(dedup)]


async def fetch_binance_klines(
    client,
    symbol: str,
    interval: str = "15m",
    *,
    limit: int = 6000,
    now_ms: int | None = None,
) -> list[dict]:
    """Page backwards through Binance public klines and return closed candles."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    collected: dict[int, dict] = {}
    end_time: int | None = now_ms

    while len(collected) < int(limit):
        page_limit = min(1500, int(limit) - len(collected))
        params = {
            "symbol": str(symbol).upper(),
            "interval": str(interval),
            "limit": page_limit,
        }
        if end_time is not None:
            params["endTime"] = end_time
        raw = await client.get("/fapi/v1/klines", params)
        page = _normalize_klines(raw, now_ms=now_ms)
        if not page:
            break
        before = len(collected)
        for row in page:
            collected[row["ts"]] = row
        earliest = min(row["ts"] for row in page)
        end_time = earliest - 1
        if len(collected) == before:
            break

    rows = [collected[k] for k in sorted(collected)]
    return rows[-int(limit):]


async def run_binance_market_language_oos(
    symbols: Iterable[str],
    *,
    limit_15m: int = 6000,
    warmup: int = 240,
    horizon: int = 4,
) -> dict:
    reports = []
    async with PublicBinanceFuturesClient() as client:
        for symbol in symbols:
            candles = await fetch_binance_klines(client, symbol, "15m", limit=limit_15m)
            if len(candles) < warmup + horizon:
                reports.append({
                    "symbol": str(symbol),
                    "status": "INSUFFICIENT_HISTORY",
                    "candles": len(candles),
                })
                continue
            report = evaluate_market_language_oos(
                candles,
                symbol=str(symbol),
                warmup=warmup,
                horizon=horizon,
            )
            ok, blockers = market_language_promotion_decision(report)
            summary = report.summary_dict()
            summary.update({
                "candles": len(candles),
                "status": "OOS_EDGE_PROVEN" if ok else "OOS_EDGE_NOT_PROVEN",
                "blockers": list(blockers),
            })
            reports.append(summary)

    eligible = [r for r in reports if r.get("status") != "INSUFFICIENT_HISTORY"]
    all_proven = bool(eligible) and all(r["status"] == "OOS_EDGE_PROVEN" for r in eligible)
    return {
        "status": "MARKET_LANGUAGE_OOS_PROVEN" if all_proven else "MARKET_LANGUAGE_OOS_NOT_PROVEN",
        "symbols": reports,
        "methodology": {
            "exchange": "BINANCE_USDM",
            "endpoint": "/fapi/v1/klines",
            "public_read_only": True,
            "closed_candles_only": True,
            "prefix_only": True,
            "non_overlapping_labels": True,
            "default_horizon_15m_candles": horizon,
            "round_trip_cost": "NEXUS static conservative execution-cost model",
            "funding": "excluded; default horizon is below one 8h funding interval",
            "runtime_mutation": False,
            "exchange_mutation": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    )
    parser.add_argument("--limit-15m", type=int, default=6000)
    parser.add_argument("--warmup", type=int, default=240)
    parser.add_argument("--horizon", type=int, default=4)
    parser.add_argument("--output", default="artifacts/market_language_binance_oos.json")
    args = parser.parse_args()
    report = asyncio.run(
        run_binance_market_language_oos(
            args.symbols,
            limit_15m=args.limit_15m,
            warmup=args.warmup,
            horizon=args.horizon,
        )
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "MARKET_LANGUAGE_OOS_PROVEN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
