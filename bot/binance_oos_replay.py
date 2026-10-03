"""Binance USD-M historical OOS replay for NEXUS evidence.

Research-only and public-data-only. This path deliberately avoids the legacy
KuCoin replay client and models Binance USD-M candles, funding settlements,
fees and adverse slippage while reusing the canonical Analyzer and NEXUS
decision logic. It never authenticates or sends an exchange mutation.
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import os
import time
from bisect import bisect_right
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Sequence

from bot import nexus_ai
from bot.backtest import (
    _binance_research_taker_fee,
    _closed_window_by_ts,
    _timestamp_index,
)
from bot.binance_research_data import (
    FundingObservation,
    agg_trade_gap_diagnostics,
    daily_agg_trades_url,
    daily_book_depth_url,
    daily_metrics_url,
    download_archive_verified,
    monthly_funding_url,
    monthly_kline_url,
    parse_agg_trades_archive,
    parse_funding_archive,
    parse_kline_archive,
)
from bot.binance_historical_context import (
    AggTradeTimeline,
    BookDepthSnapshot,
    BookDepthTimeline,
    MetricsObservation,
    MetricsTimeline,
    parse_book_depth_archive,
    parse_metrics_archive,
    parse_metrics_archive_with_provenance,
    shadow_microstructure_context,
)
from bot.candidate_trace import ensure_candidate_id
from bot.research_observation_identity import build_observation_id
from bot.config import cfg
from bot.execution_cost import static_slippage_rate
from bot.nexus_oos_edge_gate import (
    CandidateOutcome,
    build_edge_report,
    edge_promotion_decision,
)
from bot.research_manifest import ResearchArtifact, ResearchManifest


def month_range(start: str, end: str) -> tuple[tuple[int, int], ...]:
    """Inclusive YYYY-MM month sequence."""
    def parse(value: str) -> tuple[int, int]:
        parts = str(value).split("-")
        if len(parts) != 2:
            raise ValueError("month must be YYYY-MM")
        year, month = int(parts[0]), int(parts[1])
        if year < 2019 or not 1 <= month <= 12:
            raise ValueError("invalid month")
        return year, month

    sy, sm = parse(start)
    ey, em = parse(end)
    if (ey, em) < (sy, sm):
        raise ValueError("end month precedes start month")
    out = []
    year, month = sy, sm
    while (year, month) <= (ey, em):
        out.append((year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(out)


def dates_for_months(months: Sequence[tuple[int, int]]) -> tuple[str, ...]:
    """Expand complete research months to ISO dates."""
    out = []
    for year, month in months:
        last_day = calendar.monthrange(int(year), int(month))[1]
        out.extend(
            f"{int(year):04d}-{int(month):02d}-{day:02d}"
            for day in range(1, last_day + 1)
        )
    return tuple(out)


def _archive_cache_path(
    cache_dir: str | Path | None,
    url: str,
) -> Path | None:
    if cache_dir is None:
        return None
    return Path(cache_dir) / Path(url).name


async def load_verified_klines(
    symbol: str,
    interval: str,
    months: Sequence[tuple[int, int]],
    *,
    cache_dir: str | Path | None = None,
) -> tuple[list[dict], tuple[ResearchArtifact, ...]]:
    """Load, verify, merge and deduplicate Binance USD-M monthly klines."""
    by_ts: dict[int, dict] = {}
    artifacts = []
    for year, month in months:
        url = monthly_kline_url(symbol, interval, year, month)
        archive = await download_archive_verified(
            url, cache_path=_archive_cache_path(cache_dir, url)
        )
        rows = parse_kline_archive(archive.payload)
        for row in rows:
            candle = row.as_project_candle()
            ts = int(candle["ts"])
            if ts in by_ts and by_ts[ts] != candle:
                raise ValueError("conflicting Binance kline duplicate")
            by_ts[ts] = candle
        artifacts.append(ResearchArtifact(
            source=url,
            dataset="klines",
            symbol=str(symbol).upper(),
            interval=str(interval),
            sha256=archive.sha256,
            rows=len(rows),
            first_ts=rows[0].open_time if rows else None,
            last_ts=rows[-1].open_time if rows else None,
        ))
    result = sorted(by_ts.values(), key=lambda row: int(row["ts"]))
    return result, tuple(artifacts)


async def load_verified_funding(
    symbol: str,
    months: Sequence[tuple[int, int]],
    *,
    cache_dir: str | Path | None = None,
) -> tuple[list[FundingObservation], tuple[ResearchArtifact, ...]]:
    """Load verified Binance USD-M funding settlements."""
    by_ts: dict[int, FundingObservation] = {}
    artifacts = []
    for year, month in months:
        url = monthly_funding_url(symbol, year, month)
        archive = await download_archive_verified(
            url, cache_path=_archive_cache_path(cache_dir, url)
        )
        rows = parse_funding_archive(archive.payload)
        for row in rows:
            if row.timestamp in by_ts and by_ts[row.timestamp] != row:
                raise ValueError("conflicting Binance funding duplicate")
            by_ts[row.timestamp] = row
        artifacts.append(ResearchArtifact(
            source=url,
            dataset="fundingRate",
            symbol=str(symbol).upper(),
            interval=None,
            sha256=archive.sha256,
            rows=len(rows),
            first_ts=rows[0].timestamp if rows else None,
            last_ts=rows[-1].timestamp if rows else None,
        ))
    return (
        [by_ts[key] for key in sorted(by_ts)],
        tuple(artifacts),
    )


async def load_verified_metrics(
    symbol: str,
    dates: Sequence[str],
    *,
    cache_dir: str | Path | None = None,
    concurrency: int = 8,
    provenance_out: list | None = None,
) -> tuple[list[MetricsObservation], tuple[ResearchArtifact, ...]]:
    """Load checksum-verified daily 5m metrics with bounded concurrency.

    ``provenance_out`` (owned by the caller, i.e. scoped to one replay)
    receives one record per physical archive in date order.

    Network completion order never affects the result: merge order follows the
    caller's date sequence, and duplicate effective timestamps fail closed when
    their payload differs.
    """
    limit = int(concurrency)
    if limit < 1 or limit > 32:
        raise ValueError("metrics concurrency must be in [1,32]")
    semaphore = asyncio.Semaphore(limit)

    async def load_one(source_date: str):
        async with semaphore:
            url = daily_metrics_url(symbol, source_date)
            archive = await download_archive_verified(
                url, cache_path=_archive_cache_path(cache_dir, url)
            )
            rows, provenance = parse_metrics_archive_with_provenance(
                archive.payload,
                source_date=source_date,
                expected_symbol=str(symbol).upper(),
            )
            provenance = {**provenance, "source": url,
                          "archive_sha256": archive.sha256}
            artifact = ResearchArtifact(
                source=url,
                dataset="metrics",
                symbol=str(symbol).upper(),
                interval="5m",
                sha256=archive.sha256,
                rows=len(rows),
                first_ts=rows[0].effective_ts_ms if rows else None,
                last_ts=rows[-1].effective_ts_ms if rows else None,
            )
            return source_date, rows, artifact, provenance

    loaded = await asyncio.gather(
        *(load_one(source_date) for source_date in dates)
    )
    by_date = {
        source_date: (rows, artifact)
        for source_date, rows, artifact, _provenance in loaded
    }
    if provenance_out is not None:
        provenance_by_date = {item[0]: item[3] for item in loaded}
        provenance_out.extend(provenance_by_date[day] for day in dates)

    by_ts: dict[int, MetricsObservation] = {}
    artifacts: list[ResearchArtifact] = []
    for source_date in dates:
        rows, artifact = by_date[source_date]
        for row in rows:
            key = int(row.effective_ts_ms)
            if key in by_ts and by_ts[key] != row:
                raise ValueError(
                    "conflicting Binance metrics effective timestamp"
                )
            by_ts[key] = row
        artifacts.append(artifact)

    return [by_ts[key] for key in sorted(by_ts)], tuple(artifacts)


async def load_verified_book_depth(
    symbol: str,
    dates: Sequence[str],
    *,
    cache_dir: str | Path | None = None,
    concurrency: int = 8,
) -> tuple[
    list[BookDepthSnapshot],
    tuple[ResearchArtifact, ...],
    tuple[str, ...],
]:
    """Load candidate-day Binance bookDepth with bounded concurrency.

    Missing daily archives are observational gaps. Corrupt archives, checksum
    mismatches and schema violations still fail closed.
    """
    limit = int(concurrency)
    if limit < 1 or limit > 32:
        raise ValueError("bookDepth concurrency must be in [1,32]")
    semaphore = asyncio.Semaphore(limit)

    async def load_one(source_date: str):
        async with semaphore:
            url = daily_book_depth_url(symbol, source_date)
            try:
                archive = await download_archive_verified(
                    url, cache_path=_archive_cache_path(cache_dir, url)
                )
            except RuntimeError as exc:
                if "HTTP 404" in str(exc):
                    return source_date, None, None
                raise
            rows = parse_book_depth_archive(
                archive.payload, source_date=source_date
            )
            artifact = ResearchArtifact(
                source=url,
                dataset="bookDepth",
                symbol=str(symbol).upper(),
                interval="5m",
                sha256=archive.sha256,
                rows=len(rows),
                first_ts=rows[0].timestamp_ms if rows else None,
                last_ts=rows[-1].timestamp_ms if rows else None,
            )
            return source_date, rows, artifact

    loaded = await asyncio.gather(
        *(load_one(source_date) for source_date in dates)
    )
    by_ts: dict[int, BookDepthSnapshot] = {}
    artifacts: list[ResearchArtifact] = []
    missing: list[str] = []
    for source_date, rows, artifact in loaded:
        if rows is None or artifact is None:
            missing.append(source_date)
            continue
        for row in rows:
            key = int(row.timestamp_ms)
            if key in by_ts and by_ts[key] != row:
                raise ValueError("conflicting Binance bookDepth timestamp")
            by_ts[key] = row
        artifacts.append(artifact)

    return (
        [by_ts[key] for key in sorted(by_ts)],
        tuple(artifacts),
        tuple(sorted(missing)),
    )


async def load_verified_agg_trades(
    symbol: str,
    dates: Sequence[str],
    *,
    cache_dir: str | Path | None = None,
    concurrency: int = 3,
):
    """Load checksum-verified candidate-day USD-M aggTrades.

    Daily aggTrades can be large, so concurrency is deliberately lower than
    metrics/bookDepth. HTTP 404 is an observational gap; checksum/schema errors
    remain fatal. Missing ids are measured but never interpolated.
    """
    limit = int(concurrency)
    if limit < 1 or limit > 8:
        raise ValueError("aggTrades concurrency must be in [1,8]")
    semaphore = asyncio.Semaphore(limit)

    async def load_one(source_date: str):
        async with semaphore:
            url = daily_agg_trades_url(symbol, source_date)
            try:
                archive = await download_archive_verified(
                    url, cache_path=_archive_cache_path(cache_dir, url)
                )
            except RuntimeError as exc:
                if "HTTP 404" in str(exc):
                    return source_date, None, None, None
                raise
            rows = parse_agg_trades_archive(archive.payload)
            gaps = agg_trade_gap_diagnostics(rows)
            artifact = ResearchArtifact(
                source=url,
                dataset="aggTrades",
                symbol=str(symbol).upper(),
                interval="tick",
                sha256=archive.sha256,
                rows=len(rows),
                first_ts=rows[0].timestamp if rows else None,
                last_ts=rows[-1].timestamp if rows else None,
            )
            return source_date, rows, artifact, gaps

    loaded = await asyncio.gather(
        *(load_one(source_date) for source_date in dates)
    )
    rows = []
    artifacts = []
    missing = []
    gap_reports = {}
    for source_date, day_rows, artifact, gaps in loaded:
        if day_rows is None or artifact is None or gaps is None:
            missing.append(source_date)
            continue
        rows.extend(day_rows)
        artifacts.append(artifact)
        gap_reports[source_date] = gaps

    rows.sort(key=lambda row: (int(row.timestamp), int(row.aggregate_trade_id)))
    seen = set()
    for row in rows:
        key = int(row.aggregate_trade_id)
        if key in seen:
            raise ValueError("duplicate aggregate trade id across daily archives")
        seen.add(key)
    return (
        rows,
        tuple(artifacts),
        tuple(sorted(missing)),
        gap_reports,
    )


def _utc_date_from_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(
        int(timestamp_ms) / 1000.0,
        tz=timezone.utc,
    ).date().isoformat()


def derivatives_context_at(
    timeline: MetricsTimeline,
    decision_ts_ms: int,
    *,
    previous_candidate_oi: float | None = None,
    max_age_ms: int = 15 * 60 * 1000,
) -> tuple[dict, bool]:
    """Return causal derivatives with LIVE-equivalent OI-delta semantics.

    LIVE records OI when a candidate reaches NEXUS and the next candidate
    compares against that stored value. Replay must mirror that cadence.
    """
    current, _previous_metrics_row = timeline.asof(decision_ts_ms)
    if current is None:
        return {
            "oi": None,
            "oi_delta": None,
            "ls_ratio": None,
            "metrics_age_ms": None,
            "current_oi": None,
            "oi_delta_reference": "PREVIOUS_NEXUS_CANDIDATE",
        }, False
    age = int(decision_ts_ms) - int(current.effective_ts_ms)
    if age < 0 or age > int(max_age_ms):
        return {
            "oi": None,
            "oi_delta": None,
            "ls_ratio": None,
            "metrics_age_ms": age,
            "current_oi": None,
            "oi_delta_reference": "PREVIOUS_NEXUS_CANDIDATE",
        }, False

    current_oi = float(current.sum_open_interest)
    oi_delta = None
    if previous_candidate_oi is not None:
        previous = float(previous_candidate_oi)
        if previous > 0 and current_oi > 0:
            oi_delta = current_oi / previous - 1.0

    inputs = {
        "oi": {
            "openInterest": current_oi,
            "openInterestValue": current.sum_open_interest_value,
            "timestamp": current.effective_ts_ms,
        },
        "oi_delta": oi_delta,
        "ls_ratio": current.top_position_ls_ratio,
        "taker_ls_ratio": current.taker_ls_volume_ratio,
        "metrics_age_ms": age,
        "current_oi": current_oi,
        "oi_delta_reference": "PREVIOUS_NEXUS_CANDIDATE",
    }
    # A missing first delta mirrors a fresh LIVE process and is not data loss.
    complete = current_oi > 0 and inputs["ls_ratio"] is not None
    return inputs, bool(complete)

def adverse_fill(
    price: float,
    direction: str,
    *,
    is_entry: bool,
    slippage_rate: float,
) -> float:
    """Apply one-way adverse slippage to a Binance market-style fill."""
    px = float(price)
    slip = float(slippage_rate)
    side = str(direction).upper()
    if px <= 0 or not 0 <= slip < 0.05 or side not in {"LONG", "SHORT"}:
        raise ValueError("invalid adverse-fill input")
    if side == "LONG":
        return px * (1.0 + slip if is_entry else 1.0 - slip)
    return px * (1.0 - slip if is_entry else 1.0 + slip)


def fee_return_fraction(
    entry_fill: float,
    exit_legs: Sequence[tuple[float, float]],
    fee_rate: float,
) -> float:
    """Entry + weighted exit taker fees as return fraction of entry notional."""
    entry = float(entry_fill)
    fee = float(fee_rate)
    if entry <= 0 or not 0 <= fee < 0.02:
        raise ValueError("invalid fee input")
    exit_fraction = sum(
        max(0.0, float(weight)) * float(fill) / entry
        for fill, weight in exit_legs
    )
    return fee * (1.0 + exit_fraction)


def _price_at_ts(candles: list[dict], timestamps: list[int], ts_ms: int) -> float:
    index = bisect_right(timestamps, int(ts_ms)) - 1
    if index < 0:
        return 0.0
    return float(candles[index].get("c", 0.0) or 0.0)


def funding_return_fraction(
    events: Sequence[FundingObservation],
    *,
    direction: str,
    entry_ts_ms: int,
    exit_ts_ms: int,
    entry_fill: float,
    candles: list[dict],
    timestamps: list[int],
    partial_after_ts_ms: int | None = None,
) -> tuple[float, int]:
    """Funding PnL as entry-notional return, respecting post-partial weight."""
    side = str(direction).upper()
    if side not in {"LONG", "SHORT"} or entry_fill <= 0:
        raise ValueError("invalid funding simulation input")
    sign = 1.0 if side == "LONG" else -1.0
    total = 0.0
    count = 0
    for event in events:
        ts = int(event.timestamp)
        if ts < int(entry_ts_ms):
            continue
        if ts > int(exit_ts_ms):
            break
        weight = (
            0.5
            if partial_after_ts_ms is not None and ts >= int(partial_after_ts_ms)
            else 1.0
        )
        mark = _price_at_ts(candles, timestamps, ts)
        if mark <= 0:
            mark = float(entry_fill)
        # Positive funding: LONG pays, SHORT receives.
        total += -sign * float(event.funding_rate) * weight * mark / float(entry_fill)
        count += 1
    return total, count


def latest_funding(
    events: Sequence[FundingObservation],
    ts_ms: int,
) -> float | None:
    value = None
    for event in events:
        if int(event.timestamp) <= int(ts_ms):
            value = float(event.funding_rate)
        else:
            break
    return value


def _freeze_full_clock(decision_ts_ms: int):
    """Freeze stdlib time so NEXUS freshness checks see the historical decision."""
    class _Clock:
        def __enter__(self):
            self._old = time.time
            self._frozen = lambda: decision_ts_ms / 1000.0 + 1.0
            time.time = self._frozen
            return self

        def __exit__(self, exc_type, exc, tb):
            time.time = self._old

    return _Clock()


def simulate_net_r(
    *,
    direction: str,
    signal_entry: float,
    signal_sl: float,
    signal_tp: float,
    signal_tp1: float,
    signal_tp2: float,
    decision_idx: int,
    decision_ts: int,
    klines_15: list[dict],
    funding_events: Sequence[FundingObservation],
    fee_rate: float,
    slippage_rate: float,
) -> tuple[float, dict] | None:
    """Conservative stop-first Binance USD-M candidate outcome."""
    market_open = float(klines_15[decision_idx]["o"])
    entry_fill = adverse_fill(
        market_open, direction, is_entry=True, slippage_rate=slippage_rate
    )
    if entry_fill <= 0 or signal_entry <= 0:
        return None

    delta = entry_fill - signal_entry
    sl = float(signal_sl) + delta
    tp = float(signal_tp) + delta
    tp1 = float(signal_tp1) + delta
    tp2 = float(signal_tp2) + delta
    risk_fraction = abs(entry_fill - sl) / entry_fill
    if risk_fraction <= 0:
        return None

    timestamps = _timestamp_index(klines_15)
    candle_ms = 15 * 60 * 1000
    has_partial = abs(tp1 - tp2) > max(abs(entry_fill), 1.0) * 1e-12
    tp1_hit = False
    tp1_ts = None
    exit_ts = decision_ts
    exit_legs: list[tuple[float, float]] = []
    ambiguous = 0

    for j in range(decision_idx, min(decision_idx + 40, len(klines_15))):
        bar = klines_15[j]
        high, low = float(bar["h"]), float(bar["l"])
        bar_exit_ts = int(bar["ts"]) + candle_ms
        target = tp2 if tp1_hit else (tp1 if has_partial else tp)
        if direction == "LONG":
            if low <= sl and high >= target:
                ambiguous += 1
            if low <= sl:
                exit_legs.append((
                    adverse_fill(sl, direction, is_entry=False, slippage_rate=slippage_rate),
                    0.5 if tp1_hit else 1.0,
                ))
                exit_ts = bar_exit_ts
                break
            if has_partial and not tp1_hit and high >= tp1:
                tp1_hit = True
                tp1_ts = bar_exit_ts
                exit_legs.append((
                    adverse_fill(tp1, direction, is_entry=False, slippage_rate=slippage_rate),
                    0.5,
                ))
                sl = entry_fill
            if tp1_hit and high >= tp2:
                exit_legs.append((
                    adverse_fill(tp2, direction, is_entry=False, slippage_rate=slippage_rate),
                    0.5,
                ))
                exit_ts = bar_exit_ts
                break
            if not has_partial and high >= tp:
                exit_legs.append((
                    adverse_fill(tp, direction, is_entry=False, slippage_rate=slippage_rate),
                    1.0,
                ))
                exit_ts = bar_exit_ts
                break
        else:
            if high >= sl and low <= target:
                ambiguous += 1
            if high >= sl:
                exit_legs.append((
                    adverse_fill(sl, direction, is_entry=False, slippage_rate=slippage_rate),
                    0.5 if tp1_hit else 1.0,
                ))
                exit_ts = bar_exit_ts
                break
            if has_partial and not tp1_hit and low <= tp1:
                tp1_hit = True
                tp1_ts = bar_exit_ts
                exit_legs.append((
                    adverse_fill(tp1, direction, is_entry=False, slippage_rate=slippage_rate),
                    0.5,
                ))
                sl = entry_fill
            if tp1_hit and low <= tp2:
                exit_legs.append((
                    adverse_fill(tp2, direction, is_entry=False, slippage_rate=slippage_rate),
                    0.5,
                ))
                exit_ts = bar_exit_ts
                break
            if not has_partial and low <= tp:
                exit_legs.append((
                    adverse_fill(tp, direction, is_entry=False, slippage_rate=slippage_rate),
                    1.0,
                ))
                exit_ts = bar_exit_ts
                break

    closed = sum(weight for _, weight in exit_legs)
    if closed < 0.999999:
        last_idx = min(decision_idx + 39, len(klines_15) - 1)
        last = float(klines_15[last_idx]["c"])
        exit_legs.append((
            adverse_fill(last, direction, is_entry=False, slippage_rate=slippage_rate),
            0.5 if tp1_hit else 1.0,
        ))
        exit_ts = int(klines_15[last_idx]["ts"]) + candle_ms

    side = 1.0 if direction == "LONG" else -1.0
    gross = sum(
        side * ((fill - entry_fill) / entry_fill) * weight
        for fill, weight in exit_legs
    )
    fees = fee_return_fraction(entry_fill, exit_legs, fee_rate)
    funding, funding_count = funding_return_fraction(
        funding_events,
        direction=direction,
        entry_ts_ms=decision_ts,
        exit_ts_ms=exit_ts,
        entry_fill=entry_fill,
        candles=klines_15,
        timestamps=timestamps,
        partial_after_ts_ms=tp1_ts,
    )
    net = gross - fees + funding
    return (
        net / risk_fraction,
        {
            "gross_return": gross,
            "net_return": net,
            "fee_drag": fees,
            "funding_pnl": funding,
            "funding_events": funding_count,
            "entry_fill": entry_fill,
            "exit_ts": exit_ts,
            "intrabar_ambiguous": ambiguous,
            "risk_fraction": risk_fraction,
        },
    )


async def replay_symbol(
    symbol: str,
    *,
    months: Sequence[tuple[int, int]],
    cache_dir: str | Path | None = None,
    include_agg_trades: bool = False,
) -> tuple[dict, tuple[ResearchArtifact, ...]]:
    """Replay one symbol from verified Binance Vision monthly archives."""
    from bot.strategy import Analyzer

    k15, a15 = await load_verified_klines(
        symbol, "15m", months, cache_dir=cache_dir
    )
    k1h, a1h = await load_verified_klines(
        symbol, "1h", months, cache_dir=cache_dir
    )
    k4h, a4h = await load_verified_klines(
        symbol, "4h", months, cache_dir=cache_dir
    )
    funding, af = await load_verified_funding(
        symbol, months, cache_dir=cache_dir
    )
    metrics_provenance: list[dict] = []          # scoped to this replay only
    metric_rows, am = await load_verified_metrics(
        symbol, dates_for_months(months), cache_dir=cache_dir,
        provenance_out=metrics_provenance,
    )
    metrics_timeline = MetricsTimeline(metric_rows)
    artifacts = a15 + a1h + a4h + af + am
    if len(k15) < 200 or len(k1h) < 60 or len(k4h) < 30:
        return (
            {"symbol": symbol, "error": "insufficient_history", "candidates": [],
             "metrics_parse_provenance": list(metrics_provenance)},
            artifacts,
        )

    fee_rate = _binance_research_taker_fee()
    slippage = static_slippage_rate(symbol)
    ts15 = _timestamp_index(k15)
    ts1h = _timestamp_index(k1h)
    ts4h = _timestamp_index(k4h)
    analyzer = Analyzer()
    rows: list[CandidateOutcome] = []
    diagnostics = []
    approved = rejected = 0
    derivative_context_complete = 0
    derivative_context_missing = 0
    previous_candidate_oi: float | None = None

    for i in range(80, len(k15) - 40):
        decision_ts = ts15[i]
        w15 = _closed_window_by_ts(k15, ts15, decision_ts, 15, 80)
        w1h = _closed_window_by_ts(k1h, ts1h, decision_ts, 60, 50)
        w4h = _closed_window_by_ts(k4h, ts4h, decision_ts, 240, 30)
        if len(w15) < 60 or len(w1h) < 40 or len(w4h) < 20:
            continue
        try:
            sig = analyzer.analyze_mtf(
                symbol,
                w15,
                w1h,
                w4h,
                min_score=int(getattr(cfg, "MIN_ENTRY_SCORE", 65)),
                fee_mult=getattr(cfg, "FEE_MULTIPLIER", 2.0),
                vol_mult=getattr(cfg, "MIN_VOLUME_MULT", 1.2),
            )
        except Exception:
            continue
        if not sig or float(sig.rr) < float(getattr(cfg, "MIN_RR_RATIO", 2.0)):
            continue

        direction = str(sig.direction).upper()
        candidate_id = ensure_candidate_id(sig)
        outcome = simulate_net_r(
            direction=direction,
            signal_entry=float(sig.entry),
            signal_sl=float(sig.sl),
            signal_tp=float(sig.tp),
            signal_tp1=float(getattr(sig, "tp1", sig.tp) or sig.tp),
            signal_tp2=float(getattr(sig, "tp2", sig.tp) or sig.tp),
            decision_idx=i,
            decision_ts=decision_ts,
            klines_15=k15,
            funding_events=funding,
            fee_rate=fee_rate,
            slippage_rate=slippage,
        )
        if outcome is None:
            continue
        r_multiple, diag = outcome
        ticker = {"lastPrice": str(float(k15[i]["o"]))}
        funding_now = latest_funding(funding, decision_ts)
        derivatives, context_complete = derivatives_context_at(
            metrics_timeline,
            decision_ts,
            previous_candidate_oi=previous_candidate_oi,
        )
        derivative_context_complete += int(context_complete)
        derivative_context_missing += int(not context_complete)
        if derivatives["current_oi"] is not None:
            previous_candidate_oi = float(derivatives["current_oi"])
        with _freeze_full_clock(decision_ts):
            nx = nexus_ai.decide(
                symbol,
                w15,
                w1h,
                w4h,
                entry=float(sig.entry),
                sl=float(sig.sl),
                tp=float(sig.tp),
                ticker=ticker,
                funding=funding_now,
                oi=derivatives["oi"],
                oi_delta=derivatives["oi_delta"],
                orderbook=None,
                ls_ratio=derivatives["ls_ratio"],
                min_score=float(getattr(cfg, "NEXUS_MIN_SCORE", 55)),
            )
        is_approved = getattr(nx, "execution_allowed", False) is True
        approved += int(is_approved)
        rejected += int(not is_approved)
        confidence = max(
            0.0,
            min(1.0, float(getattr(nx, "confidence", 0.0) or 0.0) / 100.0),
        )
        rows.append(CandidateOutcome(
            float(decision_ts),
            is_approved,
            True,
            confidence,
            True,
            float(r_multiple),
        ).validate())
        diagnostics.append({
            "candidate_id": candidate_id,
            "observation_id": build_observation_id(
                candidate_id, int(decision_ts), symbol, direction
            ),
            "timestamp": decision_ts,
            "direction": direction,
            "signal_score": float(getattr(sig, "score", 0.0) or 0.0),
            "nexus_expected_value_pct": float(
                getattr(nx, "expected_value", 0.0) or 0.0
            ),
            "nexus_rr_net": float(
                getattr(nx, "risk_reward", 0.0) or 0.0
            ),
            "nexus_setup_quality": float(
                getattr(nx, "setup_quality", 0.0) or 0.0
            ),
            "nexus_regime_compat": float(
                getattr(nx, "regime_compat", 0.0) or 0.0
            ),
            "nexus_confidence": float(
                getattr(nx, "confidence", 0.0) or 0.0
            ),
            "nexus_data_quality": float(
                getattr(nx, "data_quality", 0.0) or 0.0
            ),
            "nexus_market_regime": str(
                getattr(nx, "market_regime", "UNKNOWN") or "UNKNOWN"
            ),
            "round_trip_cost": 2.0 * fee_rate + 2.0 * slippage,
            "r_multiple": float(r_multiple),
            "derivatives_context_complete": context_complete,
            "metrics_age_ms": derivatives["metrics_age_ms"],
            "oi_delta_reference": derivatives["oi_delta_reference"],
            "oi_delta": derivatives["oi_delta"],
            **diag,
        })

    candidate_dates = tuple(sorted({
        _utc_date_from_ms(item["timestamp"])
        for item in diagnostics
    }))
    book_depth_rows: list[BookDepthSnapshot] = []
    book_depth_artifacts: tuple[ResearchArtifact, ...] = ()
    book_depth_missing_dates: tuple[str, ...] = ()
    if candidate_dates:
        (
            book_depth_rows,
            book_depth_artifacts,
            book_depth_missing_dates,
        ) = await load_verified_book_depth(
            symbol,
            candidate_dates,
            cache_dir=cache_dir,
        )
        artifacts = artifacts + book_depth_artifacts

    agg_trade_rows = []
    agg_trade_artifacts: tuple[ResearchArtifact, ...] = ()
    agg_trade_missing_dates: tuple[str, ...] = ()
    agg_trade_gap_reports = {}
    if include_agg_trades and candidate_dates:
        (
            agg_trade_rows,
            agg_trade_artifacts,
            agg_trade_missing_dates,
            agg_trade_gap_reports,
        ) = await load_verified_agg_trades(
            symbol,
            candidate_dates,
            cache_dir=cache_dir,
        )
        artifacts = artifacts + agg_trade_artifacts

    depth_timeline = BookDepthTimeline(book_depth_rows)
    agg_timeline = AggTradeTimeline(agg_trade_rows)
    micro_available = 0
    micro_missing = 0
    micro_quarantined = 0
    agg_pressure_available = 0
    for item in diagnostics:
        ts = int(item["timestamp"])
        current_metrics, previous_metrics = metrics_timeline.asof(ts)
        depth_snapshot = depth_timeline.asof(ts)
        agg_pressure = agg_timeline.pressure(ts)
        agg_override = (
            agg_pressure.get("taker_pressure")
            if agg_pressure.get("available") is True
            else None
        )
        if agg_override is not None:
            agg_pressure_available += 1
        micro = shadow_microstructure_context(
            depth_snapshot,
            current_metrics,
            previous_metrics,
            decision_ts_ms=ts,
            side=str(item["direction"]),
            oi_delta_override=item.get("oi_delta"),
            agg_trade_pressure_override=agg_override,
        )
        item["agg_trade_pressure"] = agg_pressure
        item["shadow_microstructure"] = micro
        if depth_snapshot is not None:
            summary = depth_snapshot.summary()
            bid = summary.get("bid_notional_1pct")
            ask = summary.get("ask_notional_1pct")
            item["depth_notional_1pct"] = (
                float(bid) + float(ask)
                if bid is not None and ask is not None
                else None
            )
            item["book_depth_source_date"] = depth_snapshot.source_date
        else:
            item["depth_notional_1pct"] = None
            item["book_depth_source_date"] = None

        if micro.get("available") is True:
            micro_available += 1
        else:
            micro_missing += 1
        if (
            depth_snapshot is not None
            and depth_snapshot.quality != "OK"
        ):
            micro_quarantined += 1

    return (
        {
            "symbol": symbol,
            "metrics_parse_provenance": list(metrics_provenance),
            "candles_15m": len(k15),
            "candles_1h": len(k1h),
            "candles_4h": len(k4h),
            "funding_events": len(funding),
            "metrics_rows": len(metric_rows),
            "book_depth_rows": len(book_depth_rows),
            "book_depth_candidate_dates": len(candidate_dates),
            "book_depth_missing_dates": list(book_depth_missing_dates),
            "agg_trade_rows": len(agg_trade_rows),
            "agg_trade_mode": (
                "CANDIDATE_DAY_REAL"
                if include_agg_trades else "DISABLED"
            ),
            "agg_trade_candidate_dates": (
                len(candidate_dates) if include_agg_trades else 0
            ),
            "agg_trade_missing_dates": list(agg_trade_missing_dates),
            "agg_trade_gap_reports": agg_trade_gap_reports,
            "agg_trade_pressure_available": agg_pressure_available,
            "shadow_microstructure_available": micro_available,
            "shadow_microstructure_missing": micro_missing,
            "shadow_microstructure_quarantined": micro_quarantined,
            "derivative_context_complete": derivative_context_complete,
            "derivative_context_missing": derivative_context_missing,
            "approved": approved,
            "rejected": rejected,
            "candidates": rows,
            "candidate_diagnostics": diagnostics,
            "execution_model": "BINANCE_USDM_RESEARCH_PROXY_V1",
            "fee_rate": fee_rate,
            "slippage_rate": slippage,
            "historical_context": {
                "venue": "BINANCE_USDM",
                "candles": True,
                "ticker_proxy": True,
                "funding_history": bool(funding),
                "historical_open_interest": bool(metric_rows),
                "historical_long_short_ratio": bool(metric_rows),
                "metrics_label_shift_normalized": True,
                "oi_delta_semantics": "PREVIOUS_NEXUS_CANDIDATE",
                "historical_orderbook": bool(book_depth_rows),
                "historical_agg_trades": bool(agg_trade_rows),
                "agg_trade_mode": (
                    "CANDIDATE_DAY_REAL"
                    if include_agg_trades else "DISABLED"
                ),
                "agg_trade_candidate_day_sampling": bool(include_agg_trades),
                "agg_trade_missing_dates": list(agg_trade_missing_dates),
                "agg_trade_interpolation_applied": False,
                "book_depth_candidate_day_sampling": True,
                "book_depth_missing_dates": list(book_depth_missing_dates),
                "book_depth_score_effect": "NONE",
                "orderbook_affects_current_score": False,
                "checksums_verified": True,
                "parity_complete": (
                    derivative_context_missing == 0
                    and derivative_context_complete > 0
                    and bool(funding)
                ),
                "parity_blockers": (
                    ["DERIVATIVES_CONTEXT_INCOMPLETE"]
                    if derivative_context_missing > 0
                    or derivative_context_complete == 0
                    else []
                ) + (
                    ["FUNDING_HISTORY_EMPTY"] if not funding else []
                ),
            },
        },
        artifacts,
    )


async def run(
    symbols: Iterable[str],
    *,
    start_month: str,
    end_month: str,
    cache_dir: str | Path | None = None,
    code_sha: str = "UNSPECIFIED",
    include_agg_trades: bool = False,
) -> dict:
    """Generate Binance-native primary edge evidence plus immutable manifest."""
    from bot.runtime_bootstrap import install as install_runtime

    install_runtime()
    months = month_range(start_month, end_month)
    reports = []
    all_rows = []
    artifacts = []
    for symbol in symbols:
        report, symbol_artifacts = await replay_symbol(
            str(symbol).upper(),
            months=months,
            cache_dir=cache_dir,
            include_agg_trades=include_agg_trades,
        )
        reports.append(report)
        artifacts.extend(symbol_artifacts)
        all_rows.extend(report.get("candidates", []))

    edge = build_edge_report(all_rows)
    statistically_ok, blockers = edge_promotion_decision(edge)
    context_complete = bool(reports) and all(
        bool(rep.get("historical_context", {}).get("parity_complete"))
        for rep in reports if not rep.get("error")
    )
    final_blockers = list(blockers)
    if not context_complete:
        final_blockers.append("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")
    manifest = ResearchManifest(
        version="BINANCE_USDM_OOS_V1",
        code_sha=str(code_sha),
        created_at_ms=int(time.time() * 1000),
        artifacts=tuple(artifacts),
    )
    compact = []
    for report in reports:
        item = {
            key: value
            for key, value in report.items()
            if key not in {"candidates", "candidate_diagnostics"}
        }
        item["candidate_count"] = len(report.get("candidates", []))
        compact.append(item)

    return {
        "status": (
            "AI_EDGE_PROVEN"
            if statistically_ok and context_complete
            else "AI_EDGE_NOT_PROVEN"
        ),
        "blockers": sorted(set(final_blockers)),
        "report": asdict(edge),
        "symbols": compact,
        "manifest": manifest.canonical_dict(),
        "manifest_hash": manifest.fingerprint,
        "methodology": {
            "venue": "BINANCE_USDM",
            "source": "data.binance.vision",
            "archive_checksums_verified": True,
            "closed_candles_only": True,
            "historical_clock_frozen": True,
            "same_bar_ambiguity": "STOP_FIRST",
            "fees_included": True,
            "slippage_included": True,
            "funding_settlements_included": True,
            "authenticated_api": False,
            "exchange_mutations": False,
            "runtime_policy_mutations": False,
            "promotion_authority": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols", nargs="+",
        default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    )
    parser.add_argument("--start-month", required=True)
    parser.add_argument("--end-month", required=True)
    parser.add_argument("--cache-dir", default="artifacts/binance_research_cache")
    parser.add_argument(
        "--include-agg-trades",
        action="store_true",
        help="Download candidate-day USD-M aggTrades for microstructure OOS.",
    )
    parser.add_argument(
        "--code-sha",
        default=os.environ.get("RAILWAY_GIT_COMMIT_SHA", "UNSPECIFIED"),
    )
    parser.add_argument(
        "--output", default="artifacts/nexus_oos_binance_usdm.json"
    )
    args = parser.parse_args()
    result = asyncio.run(run(
        args.symbols,
        start_month=args.start_month,
        end_month=args.end_month,
        cache_dir=args.cache_dir,
        code_sha=args.code_sha,
        include_agg_trades=args.include_agg_trades,
    ))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(json.dumps({
        "status": result["status"],
        "blockers": result["blockers"],
        "baseline_candidates": result["report"]["baseline_candidates"],
        "approved_candidates": result["report"]["approved_candidates"],
        "expectancy_uplift_r": result["report"]["expectancy_uplift_r"],
        "manifest_hash": result["manifest_hash"],
        "output": str(out),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
