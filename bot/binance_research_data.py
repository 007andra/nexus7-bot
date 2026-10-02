"""Binance USD-M public research archive adapter.

Research-only. Uses public data.binance.vision archives and never authenticates,
places orders, changes leverage, or reads private account state.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp

_ROOT = "https://data.binance.vision/data/futures/um"
_BASE = _ROOT + "/monthly"
_DAILY = _ROOT + "/daily"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,30}$")
_INTERVAL_RE = re.compile(r"^(1m|3m|5m|15m|30m|1h|2h|4h|6h|8h|12h|1d)$")
_SHA_RE = re.compile(r"^[a-fA-F0-9]{64}$")


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


def _date_parts(value: str) -> tuple[int, int, int]:
    import datetime as _dt

    try:
        day = _dt.date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if day.year < 2019:
        raise ValueError("date predates Binance futures archive")
    return day.year, day.month, day.day


def daily_metrics_url(symbol: str, date: str) -> str:
    sym = _symbol(symbol)
    y, m, d = _date_parts(date)
    stamp = f"{y:04d}-{m:02d}-{d:02d}"
    name = f"{sym}-metrics-{stamp}.zip"
    return f"{_DAILY}/metrics/{sym}/{name}"


def daily_book_depth_url(symbol: str, date: str) -> str:
    sym = _symbol(symbol)
    y, m, d = _date_parts(date)
    stamp = f"{y:04d}-{m:02d}-{d:02d}"
    name = f"{sym}-bookDepth-{stamp}.zip"
    return f"{_DAILY}/bookDepth/{sym}/{name}"


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
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("non-finite kline value")
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

    def __post_init__(self) -> None:
        if self.timestamp <= 0:
            raise ValueError("invalid funding timestamp")
        if self.funding_interval_hours <= 0:
            raise ValueError("invalid funding interval")
        if not math.isfinite(float(self.funding_rate)):
            raise ValueError("non-finite funding rate")


@dataclass(frozen=True)
class VerifiedArchive:
    url: str
    sha256: str
    bytes_size: int
    payload: bytes


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


def parse_checksum_text(text: str, *, expected_filename: str | None = None) -> str:
    """Parse Binance Vision sha256 sidecar format and optionally bind filename."""
    fields = str(text or "").strip().split()
    if not fields or not _SHA_RE.fullmatch(fields[0]):
        raise ValueError("invalid Binance checksum sidecar")
    if expected_filename is not None and len(fields) >= 2:
        named = fields[1].lstrip("*")
        if Path(named).name != Path(expected_filename).name:
            raise ValueError("checksum filename mismatch")
    return fields[0].lower()


def verify_archive_checksum(
    payload: bytes,
    checksum_text: str,
    *,
    expected_filename: str | None = None,
) -> str:
    expected = parse_checksum_text(
        checksum_text, expected_filename=expected_filename
    )
    actual = archive_sha256(payload)
    if actual != expected:
        raise ValueError("Binance archive checksum mismatch")
    return actual


async def _http_get_bytes(url: str, timeout_s: float) -> bytes:
    timeout = aiohttp.ClientTimeout(total=float(timeout_s))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise RuntimeError(f"Binance research archive HTTP {response.status}")
            payload = await response.read()
    if not payload:
        raise RuntimeError("Binance research archive returned empty body")
    return payload


async def download_archive(
    url: str,
    *,
    cache_path: str | Path | None = None,
    timeout_s: float = 30.0,
) -> bytes:
    """Download one allowlisted Binance Vision zip with optional atomic cache."""
    if not str(url).startswith(_ROOT + "/"):
        raise ValueError("research archive host/path not allowlisted")
    path = Path(cache_path) if cache_path is not None else None
    if path is not None and path.exists():
        return await asyncio.to_thread(path.read_bytes)

    payload = await _http_get_bytes(url, timeout_s)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        await asyncio.to_thread(tmp.write_bytes, payload)
        await asyncio.to_thread(tmp.replace, path)
    return payload


async def download_archive_verified(
    url: str,
    *,
    cache_path: str | Path | None = None,
    timeout_s: float = 30.0,
) -> VerifiedArchive:
    """Download/cache one archive and fail closed unless official checksum matches."""
    if not str(url).startswith(_ROOT + "/") or not str(url).endswith(".zip"):
        raise ValueError("research archive host/path not allowlisted")
    path = Path(cache_path) if cache_path is not None else None
    checksum_path = (
        path.with_suffix(path.suffix + ".CHECKSUM") if path is not None else None
    )

    if path is not None and path.exists() and checksum_path and checksum_path.exists():
        payload = await asyncio.to_thread(path.read_bytes)
        checksum_text = await asyncio.to_thread(
            checksum_path.read_text, encoding="utf-8"
        )
    else:
        payload, checksum_bytes = await asyncio.gather(
            _http_get_bytes(url, timeout_s),
            _http_get_bytes(url + ".CHECKSUM", timeout_s),
        )
        checksum_text = checksum_bytes.decode("utf-8")
        # Verify before any bytes are admitted into the deterministic cache.
        digest = verify_archive_checksum(
            payload,
            checksum_text,
            expected_filename=Path(url).name,
        )
        if path is not None and checksum_path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            checksum_tmp = checksum_path.with_suffix(
                checksum_path.suffix + ".tmp"
            )
            await asyncio.to_thread(tmp.write_bytes, payload)
            await asyncio.to_thread(
                checksum_tmp.write_text, checksum_text, encoding="utf-8"
            )
            await asyncio.to_thread(tmp.replace, path)
            await asyncio.to_thread(checksum_tmp.replace, checksum_path)

    digest = verify_archive_checksum(
        payload,
        checksum_text,
        expected_filename=Path(url).name,
    )
    return VerifiedArchive(
        url=str(url),
        sha256=digest,
        bytes_size=len(payload),
        payload=payload,
    )
