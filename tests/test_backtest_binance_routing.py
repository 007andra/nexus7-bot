import unittest

from bot.backtest import (
    _binance_research_taker_fee,
    _fetch_binance_funding_history,
    fetch_history,
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


if __name__ == "__main__":
    unittest.main()
