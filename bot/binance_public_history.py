"""Binance USD-M public history from data.binance.vision; research only.

Reads the monthly kline and funding-rate archives Binance publishes for
USD-M futures. Every archive is verified against the published SHA-256
``.CHECKSUM`` before use and cached locally, so a study can be re-run offline
and its inputs audited. No API key, account, order or engine is involved.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import urllib.request
import zipfile
from pathlib import Path

BASE_URL = "https://data.binance.vision/data/futures/um/monthly"
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
USER_AGENT = "nexus7-research/1.0"


class HistoryIntegrityError(RuntimeError):
    pass


def months_between(first: str, last: str) -> list[str]:
    """Inclusive list of ``YYYY-MM`` months."""
    y, m = (int(x) for x in first.split("-"))
    ly, lm = (int(x) for x in last.split("-"))
    if (y, m) > (ly, lm):
        raise ValueError("first month is after last month")
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def kline_archive(symbol: str, interval: str, month: str) -> str:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval}")
    return f"klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def funding_archive(symbol: str, month: str) -> str:
    return f"fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def ensure_archive(cache_dir: Path, archive: str, *, fetch=_fetch) -> Path:
    """Return the cached archive, downloading and checksum-verifying it once."""
    path = Path(cache_dir) / archive
    checksum_path = path.with_name(path.name + ".CHECKSUM")
    if not checksum_path.exists():
        checksum_path.parent.mkdir(parents=True, exist_ok=True)
        checksum_path.write_bytes(fetch(f"{BASE_URL}/{archive}.CHECKSUM"))
    expected = checksum_path.read_text(encoding="ascii").split()[0].strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise HistoryIntegrityError(f"malformed checksum for {archive}")
    if not path.exists():
        data = fetch(f"{BASE_URL}/{archive}")
        if hashlib.sha256(data).hexdigest() != expected:
            raise HistoryIntegrityError(f"checksum mismatch for {archive}")
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise HistoryIntegrityError(f"cached archive corrupted: {archive}")
    return path


def _csv_rows(zip_path: Path):
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise HistoryIntegrityError(f"expected one csv in {zip_path.name}")
        text = zf.read(names[0]).decode("utf-8")
    return csv.reader(io.StringIO(text))


def _ms(value: str) -> int:
    ts = int(float(value))
    if ts > 100_000_000_000_000:  # microseconds
        ts //= 1000
    return ts


def parse_klines(rows) -> list[dict]:
    """Candles in the bot's ``{ts,o,h,l,c,v}`` shape; header rows skipped."""
    out = []
    for row in rows:
        if not row or not row[0].strip().lstrip("-").isdigit():
            continue
        candle = {"ts": _ms(row[0]), "o": float(row[1]), "h": float(row[2]),
                  "l": float(row[3]), "c": float(row[4]), "v": float(row[5])}
        prices = (candle["o"], candle["h"], candle["l"], candle["c"])
        if (not all(math.isfinite(p) and p > 0 for p in prices)
                or candle["h"] < max(candle["o"], candle["c"])
                or candle["l"] > min(candle["o"], candle["c"])
                or not math.isfinite(candle["v"]) or candle["v"] < 0):
            raise HistoryIntegrityError(f"invalid candle at {candle['ts']}")
        out.append(candle)
    return out


def parse_funding(rows) -> list[dict]:
    """Funding events in the ``{timepoint, fundingRate}`` shape the cost model reads."""
    out = []
    for row in rows:
        if not row or not row[0].strip().isdigit():
            continue
        rate = float(row[-1])
        if not math.isfinite(rate):
            raise HistoryIntegrityError(f"invalid funding rate at {row[0]}")
        out.append({"timepoint": _ms(row[0]), "fundingRate": rate})
    return out


def check_contiguous(candles: list[dict], interval: str) -> None:
    step = INTERVAL_MS[interval]
    for prev, cur in zip(candles, candles[1:]):
        if cur["ts"] - prev["ts"] != step:
            raise HistoryIntegrityError(
                f"{interval} gap or overlap between {prev['ts']} and {cur['ts']}")


def load_klines(cache_dir: Path, symbol: str, interval: str, months: list[str],
                *, fetch=_fetch) -> list[dict]:
    by_ts = {}
    for month in months:
        path = ensure_archive(cache_dir, kline_archive(symbol, interval, month), fetch=fetch)
        for candle in parse_klines(_csv_rows(path)):
            by_ts[candle["ts"]] = candle
    candles = [by_ts[ts] for ts in sorted(by_ts)]
    check_contiguous(candles, interval)
    return candles


def load_funding(cache_dir: Path, symbol: str, months: list[str], *, fetch=_fetch) -> list[dict]:
    events = {}
    for month in months:
        path = ensure_archive(cache_dir, funding_archive(symbol, month), fetch=fetch)
        for event in parse_funding(_csv_rows(path)):
            events[event["timepoint"]] = event
    return [events[ts] for ts in sorted(events)]
