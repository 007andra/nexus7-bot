"""Research-only quality frontier for low-capital Binance USD-M markets.

Builds on LOW_CAPITAL_UNIVERSE_V1 and ranks only CONDITIONAL contracts outside
cfg.SYMBOLS using public market-quality evidence. The first stage uses bulk
24h volume, executable BBO spread and funding; only a bounded shortlist is
enriched with open interest, depth and daily history.

This module has no LIVE authority and never mutates cfg.SYMBOLS,
engine.instruments, engine.viable_symbols, risk, sizing, leverage, thresholds,
dispatch, positions or orders.
"""
from __future__ import annotations

import asyncio
import math
import os
import statistics
from typing import Any

from bot import dynamic_universe_v1 as dynamic_universe
from bot import liquidity_depth_model
from bot import low_capital_universe_v1 as low_capital


FLAG = "LOW_CAPITAL_QUALITY_FRONTIER_V1"
DEFAULT_ENRICH_LIMIT = 36
DEFAULT_CONCURRENCY = 4

AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "live_eligible": False,
    "live_candidate": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}


def enabled() -> bool:
    return os.environ.get(FLAG, "true").lower() == "true"


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(high, max(low, value))


def _percentiles(
    rows: list[dict[str, object]],
    key: str,
    *,
    higher_is_better: bool = True,
) -> dict[str, float]:
    ordered = sorted(
        rows,
        key=lambda row: (float(row[key]), str(row["symbol"])),
    )
    if not ordered:
        return {}
    if len(ordered) == 1:
        return {str(ordered[0]["symbol"]): 1.0}
    out: dict[str, float] = {}
    denominator = len(ordered) - 1
    for index, row in enumerate(ordered):
        rank = index / denominator
        out[str(row["symbol"])] = rank if higher_is_better else 1.0 - rank
    return out


def _book_rows(raw: Any) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        try:
            bid = float(row.get("bidPrice", 0) or 0)
            ask = float(row.get("askPrice", 0) or 0)
        except (TypeError, ValueError):
            continue
        if (
            symbol
            and math.isfinite(bid)
            and math.isfinite(ask)
            and 0 < bid <= ask
        ):
            out[symbol] = (bid, ask)
    return out


def _funding_rows(raw: Any) -> dict[str, float]:
    rows = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        try:
            rate = float(row.get("lastFundingRate", 0) or 0)
        except (TypeError, ValueError):
            continue
        if symbol and math.isfinite(rate):
            out[symbol] = rate
    return out


def build_prefilter(
    base_rows: tuple[dict[str, object], ...] | list[dict[str, object]],
    book_tickers: Any,
    premium_index: Any,
) -> tuple[dict[str, object], ...]:
    books = _book_rows(book_tickers)
    funding = _funding_rows(premium_index)
    rows: list[dict[str, object]] = []

    for base in base_rows:
        if base.get("status") != "CONDITIONAL":
            continue
        if bool(base.get("configured_live_universe")):
            continue
        symbol = str(base.get("symbol", "")).upper()
        if not symbol or symbol not in books or symbol not in funding:
            continue
        bid, ask = books[symbol]
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 10_000.0
        quote_volume = float(base.get("quote_volume_usdt", 0) or 0)
        max_stop_pct = float(base.get("max_stop_pct", 0) or 0)
        if (
            not math.isfinite(spread_bps)
            or not math.isfinite(quote_volume)
            or not math.isfinite(max_stop_pct)
            or quote_volume < 0
            or max_stop_pct < 0
        ):
            continue
        rows.append({
            **base,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_bps": spread_bps,
            "funding_rate": funding[symbol],
            "funding_rate_abs": abs(funding[symbol]),
            **AUTHORITY,
        })

    if not rows:
        return ()

    volume_rank = _percentiles(rows, "quote_volume_usdt")
    spread_rank = _percentiles(rows, "spread_bps", higher_is_better=False)
    funding_rank = _percentiles(rows, "funding_rate_abs", higher_is_better=False)
    stop_rank = _percentiles(rows, "max_stop_pct")

    for row in rows:
        symbol = str(row["symbol"])
        # Fixed research triage weights. They only choose which markets receive
        # deeper public-data enrichment; they never authorize a LIVE symbol.
        row["prefilter_score"] = (
            0.45 * volume_rank[symbol]
            + 0.30 * spread_rank[symbol]
            + 0.15 * funding_rank[symbol]
            + 0.10 * stop_rank[symbol]
        )

    rows.sort(
        key=lambda row: (
            -float(row["prefilter_score"]),
            -float(row["quote_volume_usdt"]),
            str(row["symbol"]),
        )
    )
    for index, row in enumerate(rows, 1):
        row["prefilter_rank"] = index
    return tuple(rows)


def _depth_10bps_usdt(orderbook: dict, mid: float) -> float:
    if mid <= 0 or not math.isfinite(mid):
        return 0.0

    def side_notional(key: str, *, bid_side: bool) -> float:
        total = 0.0
        for level in orderbook.get(key, []) if isinstance(orderbook, dict) else []:
            try:
                price = float(level[0])
                qty = float(level[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not (math.isfinite(price) and math.isfinite(qty) and price > 0 and qty > 0):
                continue
            within = (
                price >= mid * 0.999 if bid_side
                else price <= mid * 1.001
            )
            if within:
                total += price * qty
        return total

    return min(
        side_notional("b", bid_side=True),
        side_notional("a", bid_side=False),
    )


def _history_metrics(klines: Any) -> tuple[float, float]:
    closes: list[float] = []
    for row in klines if isinstance(klines, list) else []:
        try:
            close = float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(close) and close > 0:
            closes.append(close)
    returns = [
        closes[index] / closes[index - 1] - 1.0
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    realized_vol_pct = (
        statistics.pstdev(returns) * 100.0 if len(returns) >= 2 else 0.0
    )
    return float(max(0, len(closes) - 1)), max(0.0, realized_vol_pct)


def _execution_quality(orderbook: dict, min_valid_qty: float) -> tuple[float, float | None]:
    if min_valid_qty <= 0 or not math.isfinite(min_valid_qty):
        return 0.0, None
    impacts: list[float] = []
    for side in ("BUY", "SELL"):
        snapshot = liquidity_depth_model.fill_snapshot(
            orderbook,
            order_side=side,
            qty=min_valid_qty,
        )
        if bool(snapshot.get("partial")):
            return 0.0, None
        impact = snapshot.get("impact_bps")
        if impact is None or not math.isfinite(float(impact)):
            return 0.0, None
        impacts.append(max(0.0, float(impact)))
    worst = max(impacts)
    return 1.0 / (1.0 + worst / 10.0), worst


def build_enriched_row(
    base: dict[str, object],
    *,
    open_interest: Any,
    orderbook: Any,
    klines: Any,
) -> dict[str, object]:
    symbol = str(base["symbol"])
    mid = float(base["mid"])
    try:
        oi_qty = float(
            open_interest.get("openInterest", 0) if isinstance(open_interest, dict) else 0
        )
    except (TypeError, ValueError):
        oi_qty = 0.0
    open_interest_usdt = (
        oi_qty * mid if math.isfinite(oi_qty) and oi_qty > 0 and mid > 0 else 0.0
    )
    depth_10bps_usdt = _depth_10bps_usdt(orderbook, mid)
    history_days, realized_vol_pct = _history_metrics(klines)
    execution_quality, min_order_impact_bps = _execution_quality(
        orderbook,
        float(base.get("min_valid_qty", 0) or 0),
    )

    evidence = (
        float(base.get("quote_volume_usdt", 0) or 0) > 0,
        float(base.get("spread_bps", 0) or 0) >= 0,
        math.isfinite(float(base.get("funding_rate", 0) or 0)),
        open_interest_usdt > 0,
        depth_10bps_usdt > 0,
        history_days >= 2,
    )
    data_reliability = sum(1 for value in evidence if value) / len(evidence)

    return {
        **base,
        "depth_10bps_usdt": depth_10bps_usdt,
        "open_interest_usdt": open_interest_usdt,
        "history_days": history_days,
        "realized_vol_pct": realized_vol_pct,
        "execution_quality": execution_quality,
        "min_order_impact_bps": min_order_impact_bps,
        "data_reliability": data_reliability,
        **AUTHORITY,
    }


async def _enrich_one(getter, row: dict[str, object], semaphore: asyncio.Semaphore):
    symbol = str(row["symbol"])
    async with semaphore:
        oi, depth, klines = await asyncio.gather(
            asyncio.wait_for(
                getter("/fapi/v1/openInterest", {"symbol": symbol}),
                timeout=8.0,
            ),
            asyncio.wait_for(
                getter("/fapi/v1/depth", {"symbol": symbol, "limit": 20}),
                timeout=8.0,
            ),
            asyncio.wait_for(
                getter(
                    "/fapi/v1/klines",
                    {"symbol": symbol, "interval": "1d", "limit": 90},
                ),
                timeout=8.0,
            ),
        )
    return build_enriched_row(
        row,
        open_interest=oi,
        orderbook={
            "b": depth.get("bids", []) if isinstance(depth, dict) else [],
            "a": depth.get("asks", []) if isinstance(depth, dict) else [],
        },
        klines=klines,
    )


async def collect(engine) -> tuple[dict[str, object], ...]:
    client = getattr(engine, "client", None)
    getter = getattr(client, "_get", None)
    if getter is None:
        raise RuntimeError("binance public getter unavailable")

    base_rows, book_tickers, premium_index = await asyncio.gather(
        low_capital.collect(engine),
        asyncio.wait_for(getter("/fapi/v1/ticker/bookTicker"), timeout=8.0),
        asyncio.wait_for(getter("/fapi/v1/premiumIndex"), timeout=8.0),
    )
    prefiltered = build_prefilter(base_rows, book_tickers, premium_index)
    limit = _bounded_int(
        "LOW_CAPITAL_QUALITY_ENRICH_LIMIT",
        DEFAULT_ENRICH_LIMIT,
        1,
        60,
    )
    concurrency = _bounded_int(
        "LOW_CAPITAL_QUALITY_CONCURRENCY",
        DEFAULT_CONCURRENCY,
        1,
        8,
    )
    selected = prefiltered[:limit]
    semaphore = asyncio.Semaphore(concurrency)
    enriched = await asyncio.gather(
        *(_enrich_one(getter, row, semaphore) for row in selected)
    )

    markets = [
        dynamic_universe.MarketQuality(
            symbol=str(row["symbol"]),
            volume_usdt=float(row["quote_volume_usdt"]),
            spread_bps=float(row["spread_bps"]),
            depth_10bps_usdt=float(row["depth_10bps_usdt"]),
            open_interest_usdt=float(row["open_interest_usdt"]),
            funding_rate_abs=float(row["funding_rate_abs"]),
            realized_vol_pct=float(row["realized_vol_pct"]),
            execution_quality=float(row["execution_quality"]),
            data_reliability=float(row["data_reliability"]),
            history_days=float(row["history_days"]),
        )
        for row in enriched
    ]
    ranked = dynamic_universe.rank_universe(markets)
    quality_by_symbol = {str(row["symbol"]): row for row in ranked}
    merged: list[dict[str, object]] = []
    for row in enriched:
        quality = quality_by_symbol[str(row["symbol"])]
        merged.append({
            **row,
            "market_quality_score": quality["market_quality_score"],
            "eligible_for_research": quality["eligible_for_research"],
            "quality_rank": quality["research_rank"],
            **AUTHORITY,
        })
    merged.sort(
        key=lambda row: (
            not bool(row["eligible_for_research"]),
            -float(row["market_quality_score"]),
            int(row["prefilter_rank"]),
            str(row["symbol"]),
        )
    )
    for index, row in enumerate(merged, 1):
        row["frontier_rank"] = index
    return tuple(merged)


def _fmt(row: dict[str, object]) -> str:
    impact = row.get("min_order_impact_bps")
    impact_text = "NA" if impact is None else f"{float(impact):.3f}"
    return (
        f"{row['symbol']}:rank={row['frontier_rank']}"
        f":score={float(row['market_quality_score']):.6f}"
        f":eligible={str(bool(row['eligible_for_research'])).lower()}"
        f":vol={float(row['quote_volume_usdt']):.0f}"
        f":spread_bps={float(row['spread_bps']):.3f}"
        f":depth10={float(row['depth_10bps_usdt']):.0f}"
        f":oi={float(row['open_interest_usdt']):.0f}"
        f":funding_abs={float(row['funding_rate_abs']):.8f}"
        f":impact_bps={impact_text}"
        f":history_d={float(row['history_days']):.0f}"
        f":min_notional={float(row['min_order_notional']):.6f}"
        f":max_stop_pct={float(row['max_stop_pct']):.6f}"
    )


async def _run(engine, log) -> None:
    try:
        rows = await collect(engine)
        eligible = [row for row in rows if row["eligible_for_research"]]
        authority = " ".join(
            f"{key}={str(value).lower() if isinstance(value, bool) else value}"
            for key, value in AUTHORITY.items()
        )
        log.warning(
            "[LOW_CAPITAL_QUALITY_FRONTIER_V1] enriched=%d eligible=%d "
            "enrich_limit=%d top=%s %s",
            len(rows),
            len(eligible),
            _bounded_int(
                "LOW_CAPITAL_QUALITY_ENRICH_LIMIT",
                DEFAULT_ENRICH_LIMIT,
                1,
                60,
            ),
            "|".join(_fmt(row) for row in rows[:12]) or "NONE",
            authority,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "[LOW_CAPITAL_QUALITY_FRONTIER_V1] status=DEFER reason=%s "
            "research_only=true observability_only=true live_allowed=false "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )


def schedule_if_enabled(engine, log) -> bool:
    if not enabled():
        return False
    task = getattr(engine, "_low_capital_quality_frontier_v1_task", None)
    if task is not None and not task.done():
        return False
    starter = getattr(engine, "_start_background", None)
    if not callable(starter):
        raise RuntimeError("engine background manager unavailable")
    task = starter(_run(engine, log))
    engine._low_capital_quality_frontier_v1_task = task
    log.warning(
        "[LOW_CAPITAL_QUALITY_FRONTIER_V1] status=SCHEDULED "
        "scheduler=ENGINE_BACKGROUND_MANAGER "
        "research_only=true observability_only=true shadow_only=true "
        "live_allowed=false decision_effect=NONE execution_effect=NONE"
    )
    return True
