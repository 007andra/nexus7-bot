"""Read-only Binance USD-M historical OOS replay for market-language research."""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import time
from typing import Iterable
import zipfile

import aiohttp

from bot.market_language_oos import evaluate_market_language_oos, market_language_promotion_decision


BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_VISION = "https://data.binance.vision"


class PublicBinanceFuturesClient:
    """Public GET-only Binance USD-M REST client. No keys or mutation methods."""

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


class PublicBinanceVisionClient:
    """Public read-only client for official Binance historical ZIP archives."""

    def __init__(self, base_url: str = BINANCE_VISION) -> None:
        self.base_url = base_url.rstrip("/")
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session is not None:
            await self.session.close()

    async def get_bytes(self, path: str) -> bytes:
        if self.session is None:
            raise RuntimeError("client session not started")
        if not path.startswith("/data/futures/um/"):
            raise ValueError("only Binance USD-M public archive paths are allowed")
        async with self.session.get(self.base_url + path) as resp:
            resp.raise_for_status()
            return await resp.read()


def _epoch_ms(value: object) -> int:
    ts = int(value)
    if ts > 10**15:
        ts //= 1000
    elif ts < 10**11:
        ts *= 1000
    return ts


def _normalize_klines(rows: object, *, now_ms: int) -> list[dict]:
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            open_ts = _epoch_ms(row[0])
            close_ts = _epoch_ms(row[6])
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


def _parse_vision_zip(payload: bytes, *, now_ms: int) -> list[dict]:
    rows: list[list[str]] = []
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError("Binance Vision archive must contain exactly one CSV")
        with archive.open(names[0], "r") as raw:
            with TextIOWrapper(raw, encoding="utf-8", newline="") as text:
                rows.extend(csv.reader(text))
    return _normalize_klines(rows, now_ms=now_ms)


def safe_archive_months(
    *,
    count: int = 3,
    lag_months: int = 1,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Completed months, skipping the newest one to allow archive publication."""
    if count < 1 or lag_months < 0:
        raise ValueError("invalid archive month request")
    current = now or datetime.now(timezone.utc)
    current_idx = current.year * 12 + (current.month - 1)
    newest_idx = current_idx - (lag_months + 1)
    vals = []
    for offset in range(count - 1, -1, -1):
        idx = newest_idx - offset
        year, month0 = divmod(idx, 12)
        vals.append(f"{year:04d}-{month0 + 1:02d}")
    return tuple(vals)


async def fetch_binance_klines(
    client,
    symbol: str,
    interval: str = "15m",
    *,
    limit: int = 6000,
    now_ms: int | None = None,
) -> list[dict]:
    """Page backwards through Binance REST klines and return closed candles."""
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
        end_time = min(row["ts"] for row in page) - 1
        if len(collected) == before:
            break

    rows = [collected[k] for k in sorted(collected)]
    return rows[-int(limit):]


async def fetch_binance_vision_klines(
    client,
    symbol: str,
    interval: str = "15m",
    *,
    months: Iterable[str],
    limit: int = 6000,
    now_ms: int | None = None,
) -> list[dict]:
    """Load official Binance Vision USD-M monthly kline archives."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    symbol = str(symbol).upper()
    interval = str(interval)
    collected: dict[int, dict] = {}
    requested = tuple(str(month) for month in months)
    if not requested:
        raise ValueError("at least one archive month is required")

    for month in requested:
        path = (
            f"/data/futures/um/monthly/klines/{symbol}/{interval}/"
            f"{symbol}-{interval}-{month}.zip"
        )
        payload = await client.get_bytes(path)
        page = _parse_vision_zip(payload, now_ms=now_ms)
        if not page:
            raise RuntimeError(f"empty Binance Vision archive: {symbol} {month}")
        for row in page:
            collected[row["ts"]] = row

    rows = [collected[k] for k in sorted(collected)]
    return rows[-int(limit):]


async def run_binance_market_language_oos(
    symbols: Iterable[str],
    *,
    limit_15m: int = 3000,
    warmup: int = 240,
    horizon: int = 4,
    source: str = "vision",
    archive_month_count: int = 3,
    archive_lag_months: int = 1,
) -> dict:
    source = str(source).lower()
    months = safe_archive_months(
        count=archive_month_count,
        lag_months=archive_lag_months,
    ) if source == "vision" else ()

    reports = []
    client_cls = PublicBinanceVisionClient if source == "vision" else PublicBinanceFuturesClient
    async with client_cls() as client:
        for symbol in symbols:
            if source == "vision":
                candles = await fetch_binance_vision_klines(
                    client,
                    symbol,
                    "15m",
                    months=months,
                    limit=limit_15m,
                )
            elif source == "rest":
                candles = await fetch_binance_klines(
                    client, symbol, "15m", limit=limit_15m
                )
            else:
                raise ValueError("source must be vision or rest")

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
            "source": source,
            "archive_months": list(months),
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
    parser.add_argument("--limit-15m", type=int, default=3000)
    parser.add_argument("--warmup", type=int, default=240)
    parser.add_argument("--horizon", type=int, default=4)
    parser.add_argument("--source", choices=("vision", "rest"), default="vision")
    parser.add_argument("--archive-month-count", type=int, default=3)
    parser.add_argument("--archive-lag-months", type=int, default=1)
    parser.add_argument("--output", default="artifacts/market_language_binance_oos.json")
    args = parser.parse_args()
    report = asyncio.run(
        run_binance_market_language_oos(
            args.symbols,
            limit_15m=args.limit_15m,
            warmup=args.warmup,
            horizon=args.horizon,
            source=args.source,
            archive_month_count=args.archive_month_count,
            archive_lag_months=args.archive_lag_months,
        )
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "MARKET_LANGUAGE_OOS_PROVEN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
