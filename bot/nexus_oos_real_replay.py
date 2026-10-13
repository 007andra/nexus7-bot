"""Historical OOS replay for NEXUS incremental-edge evidence.

Analytics only. This module never mutates exchange state, runtime thresholds,
leverage, sizing, drawdown policy, or live release authorization.

The replay uses only candles closed at the decision timestamp, freezes NEXUS
freshness checks to that historical clock, simulates forward outcomes with the
same KuCoin market proxy assumptions used by bot.backtest, and marks historical
context parity incomplete when OI/orderbook history is unavailable. Incomplete
parity can be measured but must not be promoted to AI_EDGE_PROVEN.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from bisect import bisect_right
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import aiohttp

from bot.backtest import _closed_window_by_ts, _timestamp_index, fetch_history
from bot.config import cfg
from bot.execution_model import (
    adverse_fill,
    configured_taker_fee,
    fee_return_fraction,
    funding_return_fraction,
    slippage_rate_for_symbol,
)
from bot.kucoin_execution_model import fetch_public_funding_history
from bot.nexus_oos_edge_gate import CandidateOutcome, build_edge_report, edge_promotion_decision


class PublicKuCoinFuturesClient:
    """Minimal read-only KuCoin Futures public client for research workflows."""

    def __init__(self, base_url: str = "https://api-futures.kucoin.com") -> None:
        self.base_url = base_url.rstrip("/")
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session is not None:
            await self.session.close()

    async def _get(self, path: str, params: dict | None = None, auth: bool = False):
        if auth:
            raise RuntimeError("public replay client does not support authenticated endpoints")
        if self.session is None:
            raise RuntimeError("client session not started")
        async with self.session.get(self.base_url + path, params=params or {}) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200000":
            raise RuntimeError(f"KuCoin public response invalid for {path}: {payload}")
        return payload.get("data")


def _ts_ms(candle: dict) -> int:
    ts = int(candle.get("ts", 0) or 0)
    return ts * 1000 if ts < 100_000_000_000 else ts


def _funding_at(events: list[dict], ts_ms: int) -> float | None:
    latest = None
    for event in events:
        tp = int(event.get("timepoint", 0) or 0)
        if tp <= ts_ms:
            latest = float(event.get("fundingRate", 0.0) or 0.0)
        else:
            break
    return latest


def _price_at_ts(candles: list[dict], timestamps: list[int], target_ts: int) -> float:
    idx = bisect_right(timestamps, int(target_ts)) - 1
    if idx < 0 or idx >= len(candles):
        return 0.0
    return float(candles[idx].get("c", 0.0) or 0.0)


def _simulate_net_r(
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
    funding_events: list[dict],
    fee_rate: float,
    slippage_rate: float,
) -> float | None:
    """Mirror KUCOIN_MARKET_PROXY_V1 and return net R for one candidate."""
    market_open = float(klines_15[decision_idx]["o"])
    entry_fill = adverse_fill(market_open, direction, is_entry=True, slippage_rate=slippage_rate)
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
    tp1_ts: int | None = None
    exit_ts = decision_ts
    exit_legs: list[tuple[float, float]] = []

    for j in range(decision_idx, min(decision_idx + 40, len(klines_15))):
        future = klines_15[j]
        high = float(future["h"])
        low = float(future["l"])
        bar_exit_ts = _ts_ms(future) + candle_ms

        if direction == "LONG":
            if low <= sl:
                exit_legs.append((adverse_fill(sl, direction, is_entry=False, slippage_rate=slippage_rate), 0.5 if tp1_hit else 1.0))
                exit_ts = bar_exit_ts
                break
            if has_partial and not tp1_hit and high >= tp1:
                tp1_hit = True
                tp1_ts = bar_exit_ts
                exit_legs.append((adverse_fill(tp1, direction, is_entry=False, slippage_rate=slippage_rate), 0.5))
                sl = entry_fill
            if tp1_hit and high >= tp2:
                exit_legs.append((adverse_fill(tp2, direction, is_entry=False, slippage_rate=slippage_rate), 0.5))
                exit_ts = bar_exit_ts
                break
            if not has_partial and high >= tp:
                exit_legs.append((adverse_fill(tp, direction, is_entry=False, slippage_rate=slippage_rate), 1.0))
                exit_ts = bar_exit_ts
                break
        else:
            if high >= sl:
                exit_legs.append((adverse_fill(sl, direction, is_entry=False, slippage_rate=slippage_rate), 0.5 if tp1_hit else 1.0))
                exit_ts = bar_exit_ts
                break
            if has_partial and not tp1_hit and low <= tp1:
                tp1_hit = True
                tp1_ts = bar_exit_ts
                exit_legs.append((adverse_fill(tp1, direction, is_entry=False, slippage_rate=slippage_rate), 0.5))
                sl = entry_fill
            if tp1_hit and low <= tp2:
                exit_legs.append((adverse_fill(tp2, direction, is_entry=False, slippage_rate=slippage_rate), 0.5))
                exit_ts = bar_exit_ts
                break
            if not has_partial and low <= tp:
                exit_legs.append((adverse_fill(tp, direction, is_entry=False, slippage_rate=slippage_rate), 1.0))
                exit_ts = bar_exit_ts
                break

    if sum(weight for _, weight in exit_legs) < 0.999999:
        last_idx = min(decision_idx + 39, len(klines_15) - 1)
        last = float(klines_15[last_idx]["c"])
        exit_legs.append((adverse_fill(last, direction, is_entry=False, slippage_rate=slippage_rate), 0.5 if tp1_hit else 1.0))
        exit_ts = _ts_ms(klines_15[last_idx]) + candle_ms

    side = 1.0 if direction == "LONG" else -1.0
    gross = sum(side * ((fill - entry_fill) / entry_fill) * weight for fill, weight in exit_legs)
    fees = fee_return_fraction(entry_fill, exit_legs, fee_rate)
    funding, _ = funding_return_fraction(
        funding_events,
        direction,
        decision_ts,
        exit_ts,
        entry_fill,
        price_at_ts=lambda ts: _price_at_ts(klines_15, timestamps, ts),
        partial_after_ts_ms=tp1_ts,
    )
    return (gross - fees + funding) / risk_fraction


def _freeze_nexus_clock(nexus_ai, decision_ts_ms: int):
    class _Clock:
        def __enter__(self):
            self._old = nexus_ai.time.time
            nexus_ai.time.time = lambda: decision_ts_ms / 1000.0 + 1.0

        def __exit__(self, exc_type, exc, tb):
            nexus_ai.time.time = self._old
    return _Clock()


async def replay_symbol(client, symbol: str, *, limit_15m: int = 3000) -> dict:
    from bot.strategy import Analyzer
    from bot import nexus_ai

    k15 = await fetch_history(client, symbol, "15", limit_15m)
    k1h = await fetch_history(client, symbol, "60", max(900, limit_15m // 4 + 120))
    k4h = await fetch_history(client, symbol, "240", max(300, limit_15m // 16 + 80))
    if len(k15) < 200 or len(k1h) < 60 or len(k4h) < 30:
        return {"symbol": symbol, "error": "insufficient_history", "candidates": []}

    start_ms = _ts_ms(k15[0])
    end_ms = _ts_ms(k15[-1]) + 15 * 60 * 1000
    funding_events = await fetch_public_funding_history(client, symbol, start_ms, end_ms)
    fee_rate = configured_taker_fee()
    slippage = slippage_rate_for_symbol(symbol)

    ts15 = _timestamp_index(k15)
    ts1h = _timestamp_index(k1h)
    ts4h = _timestamp_index(k4h)
    analyzer = Analyzer()
    rows: list[CandidateOutcome] = []
    approved = rejected = 0

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
        r_multiple = _simulate_net_r(
            direction=direction,
            signal_entry=float(sig.entry),
            signal_sl=float(sig.sl),
            signal_tp=float(sig.tp),
            signal_tp1=float(getattr(sig, "tp1", sig.tp) or sig.tp),
            signal_tp2=float(getattr(sig, "tp2", sig.tp) or sig.tp),
            decision_idx=i,
            decision_ts=decision_ts,
            klines_15=k15,
            funding_events=funding_events,
            fee_rate=fee_rate,
            slippage_rate=slippage,
        )
        if r_multiple is None:
            continue

        ticker = {"lastPrice": str(float(k15[i]["o"]))}
        funding = _funding_at(funding_events, decision_ts)
        with _freeze_nexus_clock(nexus_ai, decision_ts):
            nx = nexus_ai.decide(
                symbol,
                w15,
                w1h,
                w4h,
                entry=float(sig.entry),
                sl=float(sig.sl),
                tp=float(sig.tp),
                ticker=ticker,
                funding=funding,
                oi=None,
                orderbook=None,
                min_score=float(getattr(cfg, "NEXUS_MIN_SCORE", 55)),
            )
        is_approved = getattr(nx, "execution_allowed", False) is True
        approved += int(is_approved)
        rejected += int(not is_approved)
        confidence = max(0.0, min(1.0, float(getattr(nx, "confidence", 0.0) or 0.0) / 100.0))
        rows.append(CandidateOutcome(float(decision_ts), is_approved, True, confidence, True, float(r_multiple)))

    return {
        "symbol": symbol,
        "candles_15m": len(k15),
        "funding_events": len(funding_events),
        "approved": approved,
        "rejected": rejected,
        "candidates": rows,
        "historical_context": {
            "candles": True,
            "ticker_proxy": True,
            "funding_history": bool(funding_events),
            "historical_open_interest": False,
            "historical_orderbook": False,
            "parity_complete": False,
        },
    }


async def run_real_replay(symbols: Iterable[str], *, limit_15m: int = 3000) -> dict:
    # Install the production wrapper stack in a PAPER-safe process. No exchange
    # mutation methods are called by this replay.
    from bot.runtime_bootstrap import install as install_runtime
    install_runtime()

    symbol_reports = []
    all_rows: list[CandidateOutcome] = []
    async with PublicKuCoinFuturesClient() as client:
        for symbol in symbols:
            rep = await replay_symbol(client, symbol, limit_15m=limit_15m)
            symbol_reports.append(rep)
            all_rows.extend(rep.get("candidates", []))

    edge = build_edge_report(all_rows)
    statistically_ok, blockers = edge_promotion_decision(edge)
    context_parity_complete = all(
        bool(rep.get("historical_context", {}).get("parity_complete"))
        for rep in symbol_reports if "error" not in rep
    ) and bool(symbol_reports)
    final_blockers = list(blockers)
    if not context_parity_complete:
        final_blockers.append("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")
    status = "AI_EDGE_PROVEN" if statistically_ok and context_parity_complete else "AI_EDGE_NOT_PROVEN"

    compact_symbols = []
    for rep in symbol_reports:
        compact = {k: v for k, v in rep.items() if k != "candidates"}
        compact["candidate_count"] = len(rep.get("candidates", []))
        compact_symbols.append(compact)

    return {
        "status": status,
        "blockers": sorted(set(final_blockers)),
        "report": asdict(edge),
        "symbols": compact_symbols,
        "methodology": {
            "closed_candles_only": True,
            "historical_clock_frozen": True,
            "same_bar_ambiguity": "STOP_FIRST",
            "fees_included": True,
            "slippage_included": True,
            "funding_included_when_public_history_available": True,
            "exchange_mutations": False,
            "runtime_policy_mutations": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"])
    parser.add_argument("--limit-15m", type=int, default=3000)
    parser.add_argument("--output", default="artifacts/nexus_oos_real_replay.json")
    args = parser.parse_args()
    report = asyncio.run(run_real_replay(args.symbols, limit_15m=args.limit_15m))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "blockers": report["blockers"],
        "baseline_candidates": report["report"]["baseline_candidates"],
        "approved_candidates": report["report"]["approved_candidates"],
        "rejected_candidates": report["report"]["rejected_candidates"],
        "baseline_expectancy_r": report["report"]["baseline_expectancy_r"],
        "nexus_expectancy_r": report["report"]["nexus_expectancy_r"],
        "expectancy_uplift_r": report["report"]["expectancy_uplift_r"],
        "bootstrap_ci_low_r": report["report"]["bootstrap_ci_low_r"],
        "bootstrap_ci_high_r": report["report"]["bootstrap_ci_high_r"],
        "output": str(out),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
