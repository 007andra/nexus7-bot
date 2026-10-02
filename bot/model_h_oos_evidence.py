"""MODEL H historical OOS evidence runner.

Research only. Uses closed public candles and the native market-language
forecaster. It never installs runtime overlays, calls NEXUS execution, or
mutates exchange state.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean

from bot.backtest import fetch_history
from bot.kucoin_execution_model import configured_taker_fee, slippage_rate_for_symbol
from bot.market_language import forecast_market_language


@dataclass(frozen=True)
class ModelHEvidence:
    symbol: str
    samples: int
    covered: int
    coverage: float
    directional_accuracy: float
    brier_up: float
    mean_gross_return: float
    mean_net_return: float
    positive_net_fraction: float


class PublicKuCoinFuturesClient:
    def __init__(self, base_url: str = "https://api-futures.kucoin.com") -> None:
        self.base_url = base_url.rstrip("/")
        self.session = None

    async def __aenter__(self):
        import aiohttp
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session is not None:
            await self.session.close()

    async def _get(self, path: str, params: dict | None = None, auth: bool = False):
        if auth:
            raise RuntimeError("MODEL H OOS client is public/read-only")
        if self.session is None:
            raise RuntimeError("client session not started")
        async with self.session.get(self.base_url + path, params=params or {}) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200000":
            raise RuntimeError(f"invalid public response for {path}")
        return payload.get("data")


def evaluate_candles(
    symbol: str,
    candles: list[dict],
    *,
    warmup: int = 160,
    horizon: int = 4,
    step: int = 4,
    sample_count: int = 48,
    round_trip_cost: float,
) -> ModelHEvidence:
    predictions = 0
    covered = 0
    hits = 0
    brier_sum = 0.0
    gross: list[float] = []
    net: list[float] = []

    end = warmup
    while end + horizon <= len(candles):
        prefix = candles[:end]
        forecast = forecast_market_language(
            prefix, horizon=horizon, sample_count=sample_count
        )
        start = float(prefix[-1]["c"])
        finish = float(candles[end + horizon - 1]["c"])
        actual_return = finish / start - 1.0
        actual_up = actual_return > 0.0
        predictions += 1
        brier_sum += (forecast.probability_up - float(actual_up)) ** 2

        if forecast.available:
            covered += 1
            predicted_up = forecast.probability_up > forecast.probability_down
            hits += int(predicted_up == actual_up)
            signed = actual_return if predicted_up else -actual_return
            gross.append(signed)
            net.append(signed - round_trip_cost)
        end += step

    return ModelHEvidence(
        symbol=symbol,
        samples=predictions,
        covered=covered,
        coverage=covered / predictions if predictions else 0.0,
        directional_accuracy=hits / covered if covered else 0.0,
        brier_up=brier_sum / predictions if predictions else 0.0,
        mean_gross_return=mean(gross) if gross else 0.0,
        mean_net_return=mean(net) if net else 0.0,
        positive_net_fraction=sum(x > 0 for x in net) / len(net) if net else 0.0,
    )


async def run(symbols: list[str], *, limit_15m: int, horizon: int, step: int) -> dict:
    reports = []
    fee = configured_taker_fee()
    async with PublicKuCoinFuturesClient() as client:
        for symbol in symbols:
            candles = await fetch_history(client, symbol, "15", limit_15m)
            # Conservative two-sided market execution cost: entry + exit, each
            # paying taker fee and the existing symbol slippage assumption.
            one_way = fee + slippage_rate_for_symbol(symbol)
            reports.append(evaluate_candles(
                symbol, candles, horizon=horizon, step=step,
                round_trip_cost=2.0 * one_way,
            ))

    pooled_samples = sum(x.samples for x in reports)
    pooled_covered = sum(x.covered for x in reports)
    weighted_brier = (
        sum(x.brier_up * x.samples for x in reports) / pooled_samples
        if pooled_samples else 0.0
    )
    weighted_accuracy = (
        sum(x.directional_accuracy * x.covered for x in reports) / pooled_covered
        if pooled_covered else 0.0
    )
    weighted_net = (
        sum(x.mean_net_return * x.covered for x in reports) / pooled_covered
        if pooled_covered else 0.0
    )
    return {
        "status": "EVIDENCE_ONLY_NO_PROMOTION_AUTHORITY",
        "model": "MARKET_LANGUAGE_MODEL_H",
        "runtime_effect": False,
        "methodology": {
            "closed_candles_only": True,
            "strict_prefix_only": True,
            "horizon_15m_bars": horizon,
            "step_15m_bars": step,
            "fees_included": True,
            "slippage_included": True,
            "funding_included": False,
            "execution_mutations": False,
        },
        "pooled": {
            "samples": pooled_samples,
            "covered": pooled_covered,
            "coverage": pooled_covered / pooled_samples if pooled_samples else 0.0,
            "directional_accuracy": weighted_accuracy,
            "brier_up": weighted_brier,
            "mean_net_return": weighted_net,
        },
        "symbols": [asdict(x) for x in reports],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"])
    p.add_argument("--limit-15m", type=int, default=2500)
    p.add_argument("--horizon", type=int, default=4)
    p.add_argument("--step", type=int, default=4)
    p.add_argument("--output", default="artifacts/model_h_oos_evidence.json")
    a = p.parse_args()
    report = asyncio.run(run(a.symbols, limit_15m=a.limit_15m, horizon=a.horizon, step=a.step))
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["pooled"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
