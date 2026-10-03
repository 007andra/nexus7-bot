import unittest
from unittest.mock import AsyncMock, patch

from bot.backtest import (
    _binance_research_taker_fee,
    _fetch_binance_funding_history,
    fetch_history,
    run_backtest,
)


class FakeBinanceClient:
    exchange_name = "binance"

    def __init__(self):
        self.calls = []

    async def _get(self, path, params=None, auth=False):
        self.calls.append((path, dict(params or {}), auth))
        if path == "/fapi/v1/klines":
            base = 1_700_000_000_000
            step = 15 * 60 * 1000
            return [
                [base + i * step, "100", "110", "90", "105", "10"]
                for i in range(3)
            ]
        if path == "/fapi/v1/fundingRate":
            return [
                {"fundingTime": 1000, "fundingRate": "0.0001"},
                {"fundingTime": 2000, "fundingRate": "-0.0002"},
            ]
        raise AssertionError(f"unexpected path {path}")


class BacktestBinanceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_history_never_calls_kucoin_endpoint_for_binance(self):
        client = FakeBinanceClient()
        rows = await fetch_history(client, "BTCUSDT", "15", 3)
        self.assertEqual(len(rows), 3)
        paths = [call[0] for call in client.calls]
        self.assertIn("/fapi/v1/klines", paths)
        self.assertNotIn("/api/v1/kline/query", paths)

    async def test_binance_funding_history_is_canonicalized(self):
        client = FakeBinanceClient()
        rows = await _fetch_binance_funding_history(
            client, "BTCUSDT", 1, 3000
        )
        self.assertEqual(
            rows,
            [
                {"timepoint": 1000, "fundingRate": 0.0001},
                {"timepoint": 2000, "fundingRate": -0.0002},
            ],
        )
        self.assertEqual(client.calls[0][0], "/fapi/v1/fundingRate")

    def test_binance_research_fee_has_conservative_floor(self):
        self.assertGreaterEqual(_binance_research_taker_fee(), 0.0006)

    async def test_run_backtest_preserves_binance_execution_model(self):
        base = 1_700_000_000_000
        step = 15 * 60 * 1000
        candles = [
            {
                "ts": base + i * step,
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.5,
                "v": 10.0,
            }
            for i in range(120)
        ]
        metrics = {
            "strategy": "TEST",
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "expectancy_pct": 0.0,
            "total_trades": 0,
        }
        client = FakeBinanceClient()

        with (
            patch(
                "bot.backtest.fetch_history",
                new=AsyncMock(side_effect=[candles, candles, candles]),
            ),
            patch(
                "bot.backtest._fetch_binance_funding_history",
                new=AsyncMock(return_value=[]),
            ),
            patch("bot.backtest._run_strategy", return_value=[]),
            patch("bot.backtest._calc_metrics", return_value=dict(metrics)),
            patch("bot.backtest._walk_forward", return_value={"windows": []}),
            patch("bot.backtest.monte_carlo_permutation", return_value={}),
            patch("bot.backtest.check_strategy_validity", return_value={"valid": True}),
        ):
            result = await run_backtest(client, "BTCUSDT")

        self.assertEqual(
            result["execution_model"],
            "BINANCE_USDM_MARKET_PROXY_V1",
        )


if __name__ == "__main__":
    unittest.main()
