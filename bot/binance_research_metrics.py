"""Causal Binance USD-M metrics archive adapter.

Binance USD-M daily metrics archives changed create_time semantics on
2026-06-25. Before that date rows are end-labelled and stock fields are known at
T. From that date rows are start-labelled but stock fields correspond to T+5m.
Research therefore exposes post-change rows only at T+5m. The duplicated
transition label 2026-06-25 00:00 UTC is deliberately rejected.

Research-only: public Binance Vision data, verified by official SHA256 sidecars.
"""
from __future__ import annotations

import csv
import io
import math
import zipfile
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from bot.binance_research_data import VerifiedArchive, download_archive_verified

_DAILY_METRICS_BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
_TRANSITION_MS = int(datetime(2026, 6, 25, tzinfo=timezone.utc).timestamp() * 1000)
_PERIOD_MS = 5 * 60 * 1000


def _symbol(value: str) -> str:
    out = str(value or "").upper()
    if not out or not out.isalnum() or not out.endswith("USDT"):
        raise ValueError("invalid Binance metrics symbol")
    return out


def daily_metrics_url(symbol: str, day: date) -> str:
    sym = _symbol(symbol)
    if not isinstance(day, date):
        raise ValueError("metrics day must be date")
    stamp = day.isoformat()
    return f"{_DAILY_METRICS_BASE}/{sym}/{sym}-metrics-{stamp}.zip"


def _parse_create_time(value: str) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing metrics create_time")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return int(parsed.timestamp() * 1000)


@dataclass(frozen=True)
class MetricObservation:
    label_ts_ms: int
    available_at_ms: int
    symbol: str
    open_interest: float
    open_interest_value: float
    toptrader_account_ls_ratio: float
    toptrader_position_ls_ratio: float
    global_account_ls_ratio: float
    taker_long_short_ratio: float

    def __post_init__(self) -> None:
        if self.label_ts_ms <= 0 or self.available_at_ms < self.label_ts_ms:
            raise ValueError("invalid metrics timestamps")
        if not self.symbol:
            raise ValueError("missing metrics symbol")
        values = (
            self.open_interest,
            self.open_interest_value,
            self.toptrader_account_ls_ratio,
            self.toptrader_position_ls_ratio,
            self.global_account_ls_ratio,
            self.taker_long_short_ratio,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("non-finite metrics value")
        if self.open_interest < 0 or self.open_interest_value < 0:
            raise ValueError("negative open interest")

    @property
    def oi_payload(self) -> dict:
        return {
            "openInterest": self.open_interest,
            "openInterestValue": self.open_interest_value,
            "timestamp": self.available_at_ms,
        }


def _available_at(label_ts_ms: int) -> int | None:
    if label_ts_ms == _TRANSITION_MS:
        return None
    if label_ts_ms < _TRANSITION_MS:
        return label_ts_ms
    return label_ts_ms + _PERIOD_MS


def parse_metrics_archive(payload: bytes) -> tuple[MetricObservation, ...]:
    if not payload:
        raise ValueError("empty Binance metrics archive")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise ValueError("metrics archive must contain exactly one CSV")
            raw = zf.read(names[0]).decode("utf-8-sig")
    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
        raise ValueError("invalid Binance metrics archive") from exc

    reader = csv.DictReader(io.StringIO(raw))
    required = {
        "create_time",
        "symbol",
        "sum_open_interest",
        "sum_open_interest_value",
        "count_toptrader_long_short_ratio",
        "sum_toptrader_long_short_ratio",
        "count_long_short_ratio",
        "sum_taker_long_short_vol_ratio",
    }
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise ValueError("unexpected Binance metrics schema")

    out: list[MetricObservation] = []
    previous_available = -1
    for row in reader:
        label = _parse_create_time(row["create_time"])
        available = _available_at(label)
        if available is None:
            continue
        item = MetricObservation(
            label_ts_ms=label,
            available_at_ms=available,
            symbol=_symbol(row["symbol"]),
            open_interest=float(row["sum_open_interest"]),
            open_interest_value=float(row["sum_open_interest_value"]),
            toptrader_account_ls_ratio=float(row["count_toptrader_long_short_ratio"]),
            toptrader_position_ls_ratio=float(row["sum_toptrader_long_short_ratio"]),
            global_account_ls_ratio=float(row["count_long_short_ratio"]),
            taker_long_short_ratio=float(row["sum_taker_long_short_vol_ratio"]),
        )
        if item.available_at_ms <= previous_available:
            raise ValueError("non-monotonic Binance metrics availability")
        previous_available = item.available_at_ms
        out.append(item)
    return tuple(out)


def merge_metrics(
    archives: Sequence[tuple[VerifiedArchive, tuple[MetricObservation, ...]]],
) -> tuple[MetricObservation, ...]:
    """Merge daily files without silently resolving conflicting timestamps."""
    grouped: dict[int, list[MetricObservation]] = {}
    for _, rows in archives:
        for row in rows:
            grouped.setdefault(row.label_ts_ms, []).append(row)

    result: list[MetricObservation] = []
    for label in sorted(grouped):
        rows = grouped[label]
        if len(rows) != 1:
            continue
        result.append(rows[0])

    previous = -1
    for row in result:
        if row.available_at_ms <= previous:
            raise ValueError("merged metrics availability is not monotonic")
        previous = row.available_at_ms
    return tuple(result)


@dataclass(frozen=True)
class MetricIndex:
    rows: tuple[MetricObservation, ...]
    available_at: tuple[int, ...]

    @classmethod
    def build(cls, rows: Sequence[MetricObservation]) -> "MetricIndex":
        ordered = tuple(rows)
        times = tuple(int(row.available_at_ms) for row in ordered)
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("metric index requires strictly increasing availability")
        return cls(ordered, times)

    def position(self, decision_ts_ms: int) -> int:
        return bisect_right(self.available_at, int(decision_ts_ms)) - 1

    def latest(self, decision_ts_ms: int) -> MetricObservation | None:
        index = self.position(decision_ts_ms)
        return self.rows[index] if index >= 0 else None

    def oi_delta(self, decision_ts_ms: int) -> float | None:
        index = self.position(decision_ts_ms)
        if index < 1:
            return None
        previous, current = self.rows[index - 1], self.rows[index]
        if previous.open_interest <= 0:
            return None
        return current.open_interest / previous.open_interest - 1.0


def latest_metric(
    rows: Sequence[MetricObservation],
    decision_ts_ms: int,
) -> MetricObservation | None:
    return MetricIndex.build(rows).latest(decision_ts_ms)


def oi_delta(
    rows: Sequence[MetricObservation],
    decision_ts_ms: int,
) -> float | None:
    return MetricIndex.build(rows).oi_delta(decision_ts_ms)


async def load_daily_metrics(
    symbol: str,
    days: Sequence[date],
    *,
    cache_dir: str | Path | None = None,
) -> tuple[tuple[MetricObservation, ...], tuple[VerifiedArchive, ...]]:
    archives = []
    for day in days:
        url = daily_metrics_url(symbol, day)
        cache_path = Path(cache_dir) / Path(url).name if cache_dir is not None else None
        verified = await download_archive_verified(url, cache_path=cache_path)
        rows = parse_metrics_archive(verified.payload)
        archives.append((verified, rows))
    merged = merge_metrics(tuple(archives))
    return merged, tuple(item[0] for item in archives)
