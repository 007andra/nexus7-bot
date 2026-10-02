"""Binance USD-M public research archive adapter.

Research-only. Uses public data.binance.vision archives and never authenticates,
places orders, changes leverage, or reads private account state.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiohttp

_BASE = "https://data.binance.vision/data/futures/um/monthly"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,30}$")
_INTERVAL_RE = re.compile(r"^(1m|3m|5m|15m|30m|1h|2h|4h|6h|8h|12h|1d)$")


def _symbol(value: str) -> str:
    out = str(value or "").upper()
    if not _SYMBOL_RE.fullmatch(out):
        raise ValueError("invalid Binance research symbol")
    return out


def _interval(value: str) -> str:
    out = str(value or "")
    if not _INTERVAL_RE.fullmatch(out):
        raise ValueError("invalid Binance research interval")
    return out


def _ym(year: int, month: int) -> tuple[int, int]:
    y, m = int(year), int(month)
    if y < 2019 or not 1 <= m <= 12:
        raise ValueError("invalid archive year/month")
    return y, m


def monthly_kline_url(symbol: str, interval: str, year: int, month: int) -> str:
    sym, tf = _symbol(symbol), _interval(interval)
    y, m = _ym(year, month)
    name = f"{sym}-{tf}-{y:04d}-{m:02d}.zip"
    return f"{_BASE}/klines/{sym}/{tf}/{name}"


def monthly_funding_url(symbol: str, year: int, month: int) -> str:
    sym = _symbol(symbol)
    y, m = _ym(year, month)
    name = f"{sym}-fundingRate-{y:04d}-{m:02d}.zip"
    return f"{_BASE}/fundingRate/{sym}/{name}"


@dataclass(frozen=True)
class ResearchKline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    def __post_init__(self) -> None:
        if self.open_time < 0 or self.close_time <= self.open_time:
            raise ValueError("invalid kline timestamps")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("invalid kline price")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("kline high invariant violated")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("kline low invariant violated")
        if self.volume < 0:
            raise ValueError("negative kline volume")

    def as_project_candle(self) -> dict:
        return {
            "ts": self.open_time,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
        }


@dataclass(frozen=True)
class FundingObservation:
    timestamp: int
    funding_interval_hours: int
    funding_rate: float


def _csv_rows_from_zip(payload: bytes) -> list[list[str]]:
    if not payload:
        raise ValueError("empty Binance archive")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                raise ValueError("archive must contain exactly one CSV")
            raw = zf.read(names[0]).decode("utf-8-sig")
    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
        raise ValueError("invalid Binance archive") from exc
    return [row for row in csv.reader(io.StringIO(raw)) if row]


def _looks_header(row: list[str]) -> bool:
    if not row:
        return False
    try:
        float(row[0])
        return False
    except (TypeError, ValueError):
        return True


def parse_kline_archive(payload: bytes) -> tuple[ResearchKline, ...]:
    rows = _csv_rows_from_zip(payload)
    if rows and _looks_header(rows[0]):
        rows = rows[1:]
    out: list[ResearchKline] = []
    previous = -1
    for row in rows:
        if len(row) < 7:
            raise ValueError("short Binance kline row")
        item = ResearchKline(
            open_time=int(float(row[0])),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(float(row[6])),
        )
        if item.open_time <= previous:
            raise ValueError("non-monotonic or duplicate Binance kline")
        previous = item.open_time
        out.append(item)
    return tuple(out)


def parse_funding_archive(payload: bytes) -> tuple[FundingObservation, ...]:
    rows = _csv_rows_from_zip(payload)
    if rows and _looks_header(rows[0]):
        rows = rows[1:]
    out: list[FundingObservation] = []
    previous = -1
    for row in rows:
        if len(row) < 3:
            raise ValueError("short Binance funding row")
        item = FundingObservation(
            timestamp=int(float(row[0])),
            funding_interval_hours=int(float(row[1])),
            funding_rate=float(row[2]),
        )
        if item.timestamp <= previous:
            raise ValueError("non-monotonic or duplicate Binance funding row")
        if item.funding_interval_hours <= 0:
            raise ValueError("invalid funding interval")
        previous = item.timestamp
        out.append(item)
    return tuple(out)


def archive_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def download_archive(
    url: str,
    *,
    cache_path: str | Path | None = None,
    timeout_s: float = 30.0,
) -> bytes:
    """Download one allowlisted Binance Vision zip with optional deterministic cache."""
    if not str(url).startswith(_BASE + "/"):
        raise ValueError("research archive host/path not allowlisted")
    path = Path(cache_path) if cache_path is not None else None
    if path is not None and path.exists():
        return await asyncio.to_thread(path.read_bytes)

    timeout = aiohttp.ClientTimeout(total=float(timeout_s))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise RuntimeError(f"Binance research archive HTTP {response.status}")
            payload = await response.read()
    if not payload:
        raise RuntimeError("Binance research archive returned empty body")

    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        await asyncio.to_thread(tmp.write_bytes, payload)
        await asyncio.to_thread(tmp.replace, path)
    return payload
