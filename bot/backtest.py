"""BGX Capital — Backtesting Engine.

Quant-audit hardening:
- KuCoin historical pagination uses explicit millisecond time windows.
- Candles are uniquely keyed by timestamp, never by price.
- Historical integrity checks detect duplicates, non-monotonic data and gaps.
- Trade hour/day are derived from the real candle timestamp.
- Same-bar SL/TP ambiguity is resolved conservatively (stop first).
- Backtests never mutate shared runtime configuration.
- MTF context is aligned by real candle close timestamps, never index ratios.
- KuCoin market fills, taker fees and discrete funding settlements are modeled.
"""
from __future__ import annotations

import asyncio
import os
import time
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Dict, List

import numpy as np

from bot.config import cfg
from bot.kucoin_execution_model import (
    adverse_fill,
    configured_taker_fee,
    fee_return_fraction,
    fetch_actual_taker_fee,
    fetch_public_funding_history,
    funding_return_fraction,
    slippage_rate_for_symbol,
)
from bot.execution_cost import LEGACY_CONSERVATIVE_TAKER_FEE, static_slippage_rate
from bot.logger import log


def _interval_minutes(interval: str) -> int:
    s = str(interval)
    if s == "D":
        return 1440
    if s == "W":
        return 10080
    return int(s)


def _ts_ms(candle: dict) -> int:
    """Normalize a project candle timestamp to milliseconds."""
    ts = int(candle.get("ts", 0) or 0)
    if ts < 1e11:
        ts *= 1000
    return ts


def _timestamp_index(candles: list) -> list[int]:
    return [_ts_ms(c) for c in candles]


def _closed_window_by_ts(
    candles: list,
    open_timestamps_ms: list[int],
    decision_ts_ms: int,
    interval_min: int,
    lookback: int,
) -> list:
    """Return only candles fully closed at ``decision_ts_ms``."""
    if not candles or not open_timestamps_ms or lookback <= 0:
        return []
    interval_ms = int(interval_min) * 60 * 1000
    latest_closed_open = int(decision_ts_ms) - interval_ms
    end = bisect_right(open_timestamps_ms, latest_closed_open)
    start = max(0, end - int(lookback))
    return candles[start:end]


def _normalize_kline(raw) -> dict | None:
    try:
        ts = int(float(raw[0]))
        if ts < 1e11:
            ts *= 1000
        candle = {
            "ts": ts,
            "o": float(raw[1]),
            "h": float(raw[2]),
            "l": float(raw[3]),
            "c": float(raw[4]),
            "v": float(raw[5]),
        }
        if candle["h"] < candle["l"] or not (candle["l"] <= candle["c"] <= candle["h"]):
            return None
        if min(candle["o"], candle["h"], candle["l"], candle["c"]) <= 0:
            return None
        return candle
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        return None


def _historical_integrity(candles: list, interval: str) -> dict:
    if not candles:
        return {"ok": False, "duplicates": 0, "non_monotonic": 0, "gaps": 0}
    expected = _interval_minutes(interval) * 60 * 1000
    timestamps = [int(c["ts"]) for c in candles]
    duplicates = len(timestamps) - len(set(timestamps))
    non_monotonic = sum(1 for a, b in zip(timestamps, timestamps[1:]) if b <= a)
    gaps = sum(1 for a, b in zip(timestamps, timestamps[1:]) if b - a > expected * 1.10)
    return {
        "ok": duplicates == 0 and non_monotonic == 0,
        "duplicates": duplicates,
        "non_monotonic": non_monotonic,
        "gaps": gaps,
    }


async def _kucoin_page(client, symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    from bot.kucoin import to_kucoin

    data = await client._get(
        "/api/v1/kline/query",
        {
            "symbol": to_kucoin(symbol),
            "granularity": str(_interval_minutes(interval)),
            "from": str(int(start_ms)),
            "to": str(int(end_ms)),
        },
    )
    raw = data if isinstance(data, list) else []
    out = []
    for item in raw:
        candle = _normalize_kline(item)
        if candle is not None:
            out.append(candle)
    out.sort(key=lambda c: c["ts"])
    return out


async def _binance_page(
    client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int,
) -> list:
    from bot.binance import INTERVAL_MAP, to_binance

    mapped = INTERVAL_MAP.get(str(interval))
    if not mapped:
        raise ValueError(f"unsupported Binance interval: {interval}")
    data = await client._get(
        "/fapi/v1/klines",
        {
            "symbol": to_binance(symbol),
            "interval": mapped,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "limit": min(1500, max(1, int(limit))),
        },
        auth=False,
    )
    raw = data if isinstance(data, list) else []
    out = []
    for item in raw:
        candle = _normalize_kline(item)
        if candle is not None:
            out.append(candle)
    out.sort(key=lambda candle: candle["ts"])
    return out


def _client_exchange_name(client) -> str:
    explicit = str(getattr(client, "exchange_name", "") or "").strip().lower()
    if explicit:
        return explicit
    name = type(client).__name__.lower()
    if "binance" in name:
        return "binance"
    if "kucoin" in name:
        return "kucoin"
    return "unknown"


def _binance_research_taker_fee() -> float:
    from bot.binance import TAKER_FEE as BINANCE_TAKER_FEE

    try:
        configured = float(BINANCE_TAKER_FEE)
    except (TypeError, ValueError, OverflowError):
        configured = LEGACY_CONSERVATIVE_TAKER_FEE
    if configured < 0 or configured >= 0.02:
        configured = LEGACY_CONSERVATIVE_TAKER_FEE
    return max(configured, LEGACY_CONSERVATIVE_TAKER_FEE)


async def _fetch_binance_funding_history(
    client,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Fetch public Binance USD-M settlement history in canonical event shape."""
    if not hasattr(client, "_get") or int(end_ms) <= int(start_ms):
        return []
    from bot.binance import to_binance

    by_ts: dict[int, dict] = {}
    cursor = int(start_ms)
    final = int(end_ms)
    previous_cursor = -1
    while cursor <= final:
        data = await client._get(
            "/fapi/v1/fundingRate",
            {
                "symbol": to_binance(symbol),
                "startTime": cursor,
                "endTime": final,
                "limit": 1000,
            },
            auth=False,
        )
        rows = data if isinstance(data, list) else []
        if not rows:
            break
        max_ts = cursor
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = int(row.get("fundingTime", 0) or 0)
                rate = float(row.get("fundingRate", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if ts > 0 and np.isfinite(rate):
                by_ts[ts] = {"timepoint": ts, "fundingRate": rate}
                max_ts = max(max_ts, ts)
        next_cursor = max_ts + 1
        if next_cursor <= cursor or next_cursor == previous_cursor:
            raise RuntimeError("Binance funding pagination made no progress")
        previous_cursor = cursor
        cursor = next_cursor
        if len(rows) < 1000:
            break
        await asyncio.sleep(0.05)
    return [by_ts[key] for key in sorted(by_ts)]


async def fetch_history(client, symbol: str, interval: str, limit: int = 1000) -> list:
    if limit <= 0:
        return []
    try:
        venue = _client_exchange_name(client)
        if not hasattr(client, "_get"):
            rows = await client.get_klines(symbol, interval, limit)
            unique = {int(c["ts"]): c for c in rows if c and c.get("ts")}
            result = sorted(unique.values(), key=lambda c: c["ts"])[-limit:]
            integ = _historical_integrity(result, interval)
            if not integ["ok"]:
                raise RuntimeError(f"historical integrity failed: {integ}")
            return result

        interval_ms = _interval_minutes(interval) * 60 * 1000
        page_size = 1500 if venue == "binance" else 500
        cursor_end = int(time.time() * 1000)
        by_ts: dict[int, dict] = {}
        previous_oldest: int | None = None
        max_pages = max(2, (limit + page_size - 1) // page_size + 3)

        for _ in range(max_pages):
            if len(by_ts) >= limit:
                break
            remaining = limit - len(by_ts)
            requested = min(page_size, remaining)
            span = interval_ms * (requested + 1)
            cursor_start = max(0, cursor_end - span)
            if venue == "binance":
                page = await _binance_page(
                    client, symbol, interval, cursor_start, cursor_end, requested
                )
            elif venue in {"kucoin", "unknown"}:
                page = await _kucoin_page(
                    client, symbol, interval, cursor_start, cursor_end
                )
            else:
                raise RuntimeError(f"unsupported historical venue: {venue}")
            if not page:
                break
            for candle in page:
                by_ts[int(candle["ts"])] = candle
            oldest = min(int(c["ts"]) for c in page)
            if previous_oldest is not None and oldest >= previous_oldest:
                raise RuntimeError(
                    f"historical pagination made no progress: oldest={oldest} previous={previous_oldest}"
                )
            previous_oldest = oldest
            cursor_end = oldest - 1
            await asyncio.sleep(0.05)

        result = sorted(by_ts.values(), key=lambda c: c["ts"])[-limit:]
        integrity = _historical_integrity(result, interval)
        if not integrity["ok"]:
            raise RuntimeError(f"historical integrity failed: {integrity}")
        if integrity["gaps"]:
            log.warning(f"[BACKTEST_DATA] {symbol} {interval}m: {integrity['gaps']} historical gaps detected")
        if len(result) < min(limit, 100):
            log.warning(f"[BACKTEST_DATA] {symbol} {interval}m: requested={limit} received={len(result)}")
        log.info(
            f"fetch_history {symbol} {interval}: {len(result)} candles | "
            f"unique_ts={len({c['ts'] for c in result})} gaps={integrity['gaps']}"
        )
        return result
    except Exception as exc:
        log.error(f"backtest fetch {symbol} {interval}: {type(exc).__name__}: {exc}")
        return []


def _execution_context_defaults(symbol: str, execution_context: dict | None) -> dict:
    ctx = dict(execution_context or {})
    ctx.setdefault("taker_fee_rate", configured_taker_fee())
    ctx.setdefault("fee_source", "configured_fallback")
    ctx.setdefault("slippage_rate", slippage_rate_for_symbol(symbol))
    ctx.setdefault("funding_events", [])
    ctx.setdefault("execution_model", "KUCOIN_MARKET_PROXY_V1")
    return ctx


def _run_strategy(
    klines_15: list,
    klines_1h: list,
    klines_4h: list,
    min_score: int = 75,
    min_rr: float = 2.0,
    sl_mult: float | None = None,
    tp_mult: float | None = None,
    symbol: str = "",
    execution_context: dict | None = None,
) -> List[dict]:
    """Replay production MTF logic with a venue-neutral market execution proxy.

    ``pnl_pct`` remains a return fraction on initial notional (historical API
    compatibility); leverage is intentionally NOT multiplied into this metric.
    """
    from bot.strategy import Analyzer

    if sl_mult is not None or tp_mult is not None:
        log.debug(
            "[BACKTEST_H07] legacy sl_mult/tp_mult ignored; production strategy "
            "owns protective-level geometry and shared cfg is immutable"
        )

    analyzer = Analyzer()
    trades: list[dict] = []
    analysis_errors = 0
    window = 60
    ctx = _execution_context_defaults(symbol, execution_context)
    fee_rate = float(ctx["taker_fee_rate"])
    slip = float(ctx["slippage_rate"])
    funding_events = list(ctx.get("funding_events") or [])
    execution_model = str(
        ctx.get("execution_model") or "KUCOIN_MARKET_PROXY_V1"
    )

    ts15 = _timestamp_index(klines_15)
    ts1h = _timestamp_index(klines_1h)
    ts4h = _timestamp_index(klines_4h)
    candle_ms = 15 * 60 * 1000

    def price_at_ts(target_ts: int) -> float:
        idx = bisect_right(ts15, int(target_ts)) - 1
        if idx < 0 or idx >= len(klines_15):
            return 0.0
        return float(klines_15[idx].get("c", 0.0) or 0.0)

    for i in range(window, len(klines_15) - 1):
        decision_ts = ts15[i]
        k15 = _closed_window_by_ts(klines_15, ts15, decision_ts, 15, window)
        k1h = _closed_window_by_ts(klines_1h, ts1h, decision_ts, 60, 20)
        k4h = _closed_window_by_ts(klines_4h, ts4h, decision_ts, 240, 15)
        if len(k15) < 30 or len(k1h) < 10 or len(k4h) < 5:
            continue

        try:
            sig = analyzer.analyze_mtf(
                symbol or "BT",
                k15,
                k1h,
                k4h,
                min_score=int(min_score),
                fee_mult=getattr(cfg, "FEE_MULTIPLIER", 2.0),
                vol_mult=getattr(cfg, "MIN_VOLUME_MULT", 1.2),
            )
        except Exception as exc:
            analysis_errors += 1
            if analysis_errors % 100 == 1:
                log.debug(f"backtest analysis error candle={i}: {exc}")
            continue

        if not sig or sig.rr < float(min_rr):
            continue

        direction = str(sig.direction).upper()
        signal_entry = float(sig.entry)
        market_open = float(klines_15[i]["o"])
        entry_fill = adverse_fill(market_open, direction, is_entry=True, slippage_rate=slip)
        if signal_entry <= 0 or entry_fill <= 0:
            continue

        # Live BGX rebases protection geometry after the confirmed market fill.
        fill_delta = entry_fill - signal_entry
        sl = float(sig.sl) + fill_delta
        tp = float(sig.tp) + fill_delta
        tp1 = float(getattr(sig, "tp1", sig.tp) or sig.tp) + fill_delta
        tp2 = float(getattr(sig, "tp2", sig.tp) or sig.tp) + fill_delta
        has_partial = abs(tp1 - tp2) > max(abs(entry_fill), 1.0) * 1e-12

        result = None
        hold = 0
        tp1_hit = False
        tp1_ts: int | None = None
        ambiguous_bars = 0
        exit_legs: list[tuple[float, float]] = []
        exit_ts = decision_ts

        # Execution starts in the decision candle. The signal consumed only
        # candles closed before decision_ts, so candle i is the first tradable bar.
        for j in range(i, min(i + 40, len(klines_15))):
            future = klines_15[j]
            hold += 1
            high, low = float(future["h"]), float(future["l"])
            bar_exit_ts = _ts_ms(future) + candle_ms

            if direction == "LONG":
                target_now = tp2 if tp1_hit else (tp1 if has_partial else tp)
                if low <= sl and high >= target_now:
                    ambiguous_bars += 1
                if low <= sl:
                    stop_fill = adverse_fill(sl, direction, is_entry=False, slippage_rate=slip)
                    exit_legs.append((stop_fill, 0.5 if tp1_hit else 1.0))
                    result = "PARTIAL_WIN" if tp1_hit else "LOSS"
                    exit_ts = bar_exit_ts
                    break
                if has_partial and not tp1_hit and high >= tp1:
                    tp1_hit = True
                    tp1_ts = bar_exit_ts
                    exit_legs.append((adverse_fill(tp1, direction, is_entry=False, slippage_rate=slip), 0.5))
                    sl = entry_fill
                if tp1_hit and high >= tp2:
                    exit_legs.append((adverse_fill(tp2, direction, is_entry=False, slippage_rate=slip), 0.5))
                    result = "WIN"
                    exit_ts = bar_exit_ts
                    break
                if not has_partial and high >= tp:
                    exit_legs.append((adverse_fill(tp, direction, is_entry=False, slippage_rate=slip), 1.0))
                    result = "WIN"
                    exit_ts = bar_exit_ts
                    break
            else:
                target_now = tp2 if tp1_hit else (tp1 if has_partial else tp)
                if high >= sl and low <= target_now:
                    ambiguous_bars += 1
                if high >= sl:
                    stop_fill = adverse_fill(sl, direction, is_entry=False, slippage_rate=slip)
                    exit_legs.append((stop_fill, 0.5 if tp1_hit else 1.0))
                    result = "PARTIAL_WIN" if tp1_hit else "LOSS"
                    exit_ts = bar_exit_ts
                    break
                if has_partial and not tp1_hit and low <= tp1:
                    tp1_hit = True
                    tp1_ts = bar_exit_ts
                    exit_legs.append((adverse_fill(tp1, direction, is_entry=False, slippage_rate=slip), 0.5))
                    sl = entry_fill
                if tp1_hit and low <= tp2:
                    exit_legs.append((adverse_fill(tp2, direction, is_entry=False, slippage_rate=slip), 0.5))
                    result = "WIN"
                    exit_ts = bar_exit_ts
                    break
                if not has_partial and low <= tp:
                    exit_legs.append((adverse_fill(tp, direction, is_entry=False, slippage_rate=slip), 1.0))
                    result = "WIN"
                    exit_ts = bar_exit_ts
                    break

        if result is None:
            result = "TIMEOUT"
            last_idx = min(i + 39, len(klines_15) - 1)
            last = float(klines_15[last_idx]["c"])
            timeout_fill = adverse_fill(last, direction, is_entry=False, slippage_rate=slip)
            exit_legs.append((timeout_fill, 0.5 if tp1_hit else 1.0))
            exit_ts = _ts_ms(klines_15[last_idx]) + candle_ms

        # Defensive normalization: every completed simulated trade must close 100%.
        closed_weight = sum(weight for _, weight in exit_legs)
        if closed_weight < 0.999999:
            continue

        side = 1.0 if direction == "LONG" else -1.0
        gross_return = sum(
            side * ((fill - entry_fill) / entry_fill) * weight
            for fill, weight in exit_legs
        )
        fee_fraction = fee_return_fraction(entry_fill, exit_legs, fee_rate)
        funding_fraction, funding_count = funding_return_fraction(
            funding_events,
            direction,
            decision_ts,
            exit_ts,
            entry_fill,
            price_at_ts=price_at_ts,
            partial_after_ts_ms=tp1_ts,
        )
        pnl_pct = gross_return - fee_fraction + funding_fraction
        weighted_exit = sum(fill * weight for fill, weight in exit_legs)

        dt = datetime.fromtimestamp(decision_ts / 1000, tz=timezone.utc)
        trades.append(
            {
                "result": result,
                "pnl_pct": pnl_pct,
                "gross_pnl_pct": gross_return,
                "fee_pct": fee_fraction,
                "funding_pct": funding_fraction,
                "hold": hold,
                "direction": direction,
                "score": sig.score,
                "hour_utc": dt.hour,
                "day_of_week": dt.weekday(),
                "opened_at": dt.isoformat(),
                "ts": decision_ts,
                "exit_ts": exit_ts,
                "rr": sig.rr,
                "regime": getattr(sig, "regime", "UNKNOWN"),
                "entry_type": getattr(sig, "entry_type", "UNKNOWN"),
                "intrabar_ambiguous": ambiguous_bars,
                "signal_entry": signal_entry,
                "market_open": market_open,
                "entry_fill": entry_fill,
                "exit_fill": weighted_exit,
                "entry_slippage_pct": (entry_fill - market_open) / market_open,
                "funding_events_charged": funding_count,
                "taker_fee_rate": fee_rate,
                "fee_source": ctx.get("fee_source", "unknown"),
                "execution_model": execution_model,
            }
        )

    return trades


def _calc_metrics(trades: List[dict], strategy: str = "MTF") -> dict:
    if not trades:
        return {}
    pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    total = len(pnls)
    win_rate = len(wins) / total * 100 if total else 0.0
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(abs(losses.sum())) if len(losses) else 1e-9
    pf = gross_profit / gross_loss
    std = float(pnls.std())
    sharpe = float(pnls.mean() / std) if std > 0 and total > 1 else 0.0
    neg_std = float(losses.std()) if len(losses) > 1 else 0.0
    sortino = float(pnls.mean() / neg_std) if neg_std > 0 else 0.0
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    max_dd = float((peak - cum).max()) if len(cum) else 0.0

    hour_pnl: Dict[int, list] = {}
    day_pnl: Dict[int, list] = {}
    for trade in trades:
        hour_pnl.setdefault(int(trade["hour_utc"]), []).append(trade["pnl_pct"])
        day_pnl.setdefault(int(trade["day_of_week"]), []).append(trade["pnl_pct"])
    hour_avg = {h: float(np.mean(v)) for h, v in hour_pnl.items()}
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_avg = {days[d]: float(np.mean(v)) for d, v in day_pnl.items() if 0 <= d <= 6}
    ambiguous = sum(int(t.get("intrabar_ambiguous", 0)) for t in trades)

    return {
        "strategy": strategy,
        "total_trades": total,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(pf, 2),
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "expectancy_pct": round(float(pnls.mean()) * 100, 4),
        "avg_hold_candles": round(float(np.mean([t["hold"] for t in trades])), 1),
        "best_hour_utc": max(hour_avg, key=hour_avg.get) if hour_avg else None,
        "worst_hour_utc": min(hour_avg, key=hour_avg.get) if hour_avg else None,
        "best_day": max(day_avg, key=day_avg.get) if day_avg else None,
        "worst_day": min(day_avg, key=day_avg.get) if day_avg else None,
        "gross_profit_pct": round(gross_profit * 100, 2),
        "gross_loss_pct": round(gross_loss * 100, 2),
        "intrabar_ambiguous_bars": ambiguous,
        "total_fee_pct": round(sum(float(t.get("fee_pct", 0)) for t in trades) * 100, 4),
        "total_funding_pct": round(sum(float(t.get("funding_pct", 0)) for t in trades) * 100, 4),
    }


run_strategy_public = _run_strategy


def monte_carlo_permutation(returns: list, n_simulations: int = 5000, random_seed: int = 42) -> dict:
    if not returns or len(returns) < 10:
        return {"error": "Mínimo 10 trades para Monte Carlo", "edge_significant": False}
    arr = np.array(returns, dtype=float)
    rng = np.random.default_rng(random_seed)
    real_sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0
    sims = np.zeros(n_simulations)
    for i in range(n_simulations):
        sample = rng.choice(arr, size=len(arr), replace=True)
        std = sample.std()
        sims[i] = float(sample.mean() / std) if std > 0 else 0.0
    p_value = float((sims <= 0.0).mean())
    return {
        "real_sharpe": round(real_sharpe, 4),
        "p_value": round(p_value, 4),
        "confidence_level": "HIGH" if p_value < 0.05 else "MEDIUM" if p_value < 0.10 else "NONE",
        "verdict": "Edge bootstrap positivo" if p_value < 0.10 else "Sem evidência bootstrap suficiente",
        "edge_significant": p_value < 0.05,
        "sharpe_percentile": round(float((sims < real_sharpe).mean() * 100), 1),
        "random_sharpe_mean": round(float(sims.mean()), 4),
        "random_sharpe_std": round(float(sims.std()), 4),
        "n_simulations": n_simulations,
        "n_trades": len(returns),
    }


def _walk_forward(
    klines_15: list,
    klines_1h: list,
    klines_4h: list,
    n_windows: int = 5,
    train_ratio: float = 0.70,
    symbol: str = "",
    rolling: bool = True,
    execution_context: dict | None = None,
) -> dict:
    if len(klines_15) < 200:
        return {"error": "Dados insuficientes para walk-forward (min 200 candles 15M)"}
    n = len(klines_15)
    results = []
    if rolling and n_windows > 1:
        window_size = int(n / ((n_windows + 1) / 2))
        step = max(1, window_size // 2)
    else:
        window_size = n // n_windows
        step = window_size

    for w in range(n_windows):
        start_idx = w * step
        end_idx = min(start_idx + window_size, n)
        if end_idx - start_idx < 120 or start_idx >= n:
            continue
        w15 = klines_15[start_idx:end_idx]
        split = int(len(w15) * train_ratio)
        train_15, test_15 = w15[:split], w15[split:]
        train_trades = (
            _run_strategy(train_15, klines_1h, klines_4h, symbol=symbol, execution_context=execution_context)
            if len(train_15) >= 60 else []
        )
        test_trades = (
            _run_strategy(test_15, klines_1h, klines_4h, symbol=symbol, execution_context=execution_context)
            if len(test_15) >= 30 else []
        )
        train_m = _calc_metrics(train_trades, "train") if train_trades else {}
        test_m = _calc_metrics(test_trades, "test") if test_trades else {}
        results.append({
            "window": w + 1,
            "candles_train": len(train_15),
            "candles_test": len(test_15),
            "train": {
                "win_rate": train_m.get("win_rate", 0),
                "profit_factor": train_m.get("profit_factor", 0),
                "sharpe": train_m.get("sharpe_ratio", 0),
                "total_trades": train_m.get("total_trades", 0),
            },
            "test": {
                "win_rate": test_m.get("win_rate", 0),
                "profit_factor": test_m.get("profit_factor", 0),
                "sharpe": test_m.get("sharpe_ratio", 0),
                "total_trades": test_m.get("total_trades", 0),
            },
        })

    if not results:
        return {"error": "Nenhuma janela com dados suficientes"}
    tests = [r["test"] for r in results if r["test"]["total_trades"] > 0]
    trains = [r["train"] for r in results if r["train"]["total_trades"] > 0]
    oos_wr = float(np.mean([r["win_rate"] for r in tests] or [0]))
    oos_pf = float(np.mean([r["profit_factor"] for r in tests] or [0]))
    is_wr = float(np.mean([r["win_rate"] for r in trains] or [0]))
    is_pf = float(np.mean([r["profit_factor"] for r in trains] or [0]))
    wr_deg = round((is_wr - oos_wr) / max(is_wr, 1) * 100, 1)
    pf_deg = round((is_pf - oos_pf) / max(is_pf, 0.01) * 100, 1)
    avg_deg = (wr_deg + pf_deg) / 2
    return {
        "windows": results,
        "oos_win_rate": round(oos_wr, 1),
        "oos_pf": round(oos_pf, 2),
        "is_win_rate": round(is_wr, 1),
        "is_pf": round(is_pf, 2),
        "wr_degradation_pct": wr_deg,
        "pf_degradation_pct": pf_deg,
        "overfit_risk": "HIGH" if avg_deg > 40 else "MEDIUM" if avg_deg > 20 else "LOW",
        "n_windows": len(results),
    }


async def run_backtest(client, symbol: str = "BTCUSDT") -> dict:
    log.info(f"🔬 Iniciando backtest histórico {symbol}...")
    start = time.time()
    k15 = await fetch_history(client, symbol, "15", 70080)
    k1h = await fetch_history(client, symbol, "60", 17520)
    k4h = await fetch_history(client, symbol, "240", 4380)
    if len(k15) < 100:
        return {"error": "Dados históricos insuficientes (min 100 candles 15M)"}

    start_ms = _ts_ms(k15[0])
    end_ms = _ts_ms(k15[-1]) + 15 * 60 * 1000
    venue = _client_exchange_name(client)
    if venue == "binance":
        taker_fee = _binance_research_taker_fee()
        fee_source = "binance_config_conservative_floor"
        funding_events = await _fetch_binance_funding_history(
            client, symbol, start_ms, end_ms
        )
        slippage_rate = static_slippage_rate(symbol)
        execution_model = "BINANCE_USDM_MARKET_PROXY_V1"
    else:
        taker_fee, fee_source = await fetch_actual_taker_fee(client, symbol)
        funding_events = await fetch_public_funding_history(
            client, symbol, start_ms, end_ms
        )
        slippage_rate = slippage_rate_for_symbol(symbol)
        execution_model = "KUCOIN_MARKET_PROXY_V1"
    funding_required = (end_ms - start_ms) > 8 * 60 * 60 * 1000
    execution_context = {
        "taker_fee_rate": taker_fee,
        "fee_source": fee_source,
        "slippage_rate": slippage_rate,
        "funding_events": funding_events,
        "execution_model": execution_model,
    }

    trades = _run_strategy(k15, k1h, k4h, symbol=symbol, execution_context=execution_context)
    metrics = _calc_metrics(trades, "MTF-4H-1H-15M")
    wf = _walk_forward(
        k15, k1h, k4h, n_windows=6, train_ratio=0.70,
        symbol=symbol, rolling=True, execution_context=execution_context,
    )
    mc = monte_carlo_permutation([t["pnl_pct"] for t in trades])
    cost_data_complete = bool(funding_events) or not funding_required
    metrics.update({
        "elapsed_seconds": round(time.time() - start, 1),
        "symbol": symbol,
        "candles_analyzed": len(k15),
        "days_analyzed": round(len(k15) * 15 / (60 * 24), 1),
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "walk_forward": wf,
        "monte_carlo": mc,
        "execution_model": "KUCOIN_MARKET_PROXY_V1",
        "taker_fee_rate": taker_fee,
        "fee_source": fee_source,
        "slippage_rate": execution_context["slippage_rate"],
        "funding_events_loaded": len(funding_events),
        "execution_cost_data_complete": cost_data_complete,
    })
    metrics["strategy_validity"] = check_strategy_validity(metrics, wf, mc)

    try:
        from bot import database as dbase
        await dbase._exec(
            """INSERT INTO performance
               (periodo,strategy,win_rate,profit_factor,sharpe_ratio,sortino_ratio,
                max_drawdown,expectancy_por_trade,total_trades,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).date().isoformat(),
                metrics.get("strategy", "MTF"), metrics.get("win_rate", 0),
                metrics.get("profit_factor", 0), metrics.get("sharpe_ratio", 0),
                metrics.get("sortino_ratio", 0), metrics.get("max_drawdown_pct", 0) / 100,
                metrics.get("expectancy_pct", 0) / 100, metrics.get("total_trades", 0),
                metrics["ran_at"],
            ),
        )
    except Exception as exc:
        log.error(f"backtest persist: {exc}")
    return metrics


_backtest_lock = asyncio.Lock()


async def weekly_backtest_loop(client):
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() == 6 and now.hour == 3 and now.minute < 5:
                if not _backtest_lock.locked():
                    async with _backtest_lock:
                        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
                            try:
                                await run_backtest(client, sym)
                                await asyncio.sleep(30)
                            except Exception as exc:
                                log.error(f"weekly_backtest {sym}: {exc}")
                await asyncio.sleep(3600)
        except Exception as exc:
            log.error(f"weekly_backtest_loop: {exc}")
        await asyncio.sleep(60)


def check_strategy_validity(metrics: dict, wf: dict, mc: dict) -> dict:
    warnings, critical = [], []
    pf_oos = wf.get("oos_pf", 0)
    wr_oos = wf.get("oos_win_rate", 0)
    test_windows = [w.get("test", {}) for w in wf.get("windows", []) if w.get("test", {}).get("total_trades", 0)]
    sharpe_oos = float(np.mean([w.get("sharpe", 0) for w in test_windows] or [0]))
    overfit = wf.get("overfit_risk", "UNKNOWN")
    p_value = mc.get("p_value", 1.0)
    total_trades = metrics.get("total_trades", 0)
    if metrics.get("execution_cost_data_complete") is False:
        critical.append("CRÍTICO: histórico de funding indisponível; custos de execução incompletos")
    if total_trades < 100:
        warnings.append(f"Amostra pequena: {total_trades} trades")
    if total_trades < 50:
        critical.append(f"CRÍTICO: {total_trades} trades insuficientes")
    if pf_oos < 1.0:
        critical.append(f"CRÍTICO: PF OOS={pf_oos:.2f} < 1.0")
    elif pf_oos < 1.10:
        warnings.append(f"PF OOS={pf_oos:.2f} marginal")
    if wr_oos < 35:
        critical.append(f"CRÍTICO: WR OOS={wr_oos:.1f}% < 35%")
    if p_value >= 0.10:
        critical.append(f"CRÍTICO: bootstrap p-value={p_value:.4f} >= 0.10")
    elif p_value >= 0.05:
        warnings.append(f"bootstrap p-value={p_value:.4f} — edge fraco")
    if sharpe_oos < 0:
        critical.append(f"CRÍTICO: Sharpe OOS={sharpe_oos:.2f} < 0")
    elif sharpe_oos < 0.5:
        warnings.append(f"Sharpe OOS={sharpe_oos:.2f} baixo")
    if overfit == "HIGH":
        critical.append("CRÍTICO: Overfit risk=HIGH")
    elif overfit == "MEDIUM":
        warnings.append("Overfit risk=MEDIUM")

    if critical:
        valid, verdict, action = False, "INVALIDADA", "SUSPENDER escala live; revisar e re-testar."
    elif len(warnings) >= 3:
        valid, verdict, action = False, "QUESTIONÁVEL", "Manter apenas paper/shadow/pilot mínimo."
    elif warnings:
        valid, verdict, action = True, "ACEITÁVEL_COM_RESSALVAS", "Manter piloto mínimo e coletar mais OOS."
    else:
        valid, verdict, action = True, "VÁLIDA", "Elegível para revisão de release, não garantia de lucro."
    return {
        "valid": valid,
        "verdict": verdict,
        "action": action,
        "warnings": warnings,
        "critical": critical,
        "pf_oos": pf_oos,
        "wr_oos": wr_oos,
        "sharpe_oos": round(sharpe_oos, 3),
        "p_value": p_value,
        "overfit": overfit,
    }
