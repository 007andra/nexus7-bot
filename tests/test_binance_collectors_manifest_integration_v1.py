"""Integrated offline tests with simulated signed GET Binance responses.

No real network, credentials, production mutations, or orders.
"""
import asyncio
import unittest

from bot.binance_readonly_fills_windows_v1 import collect_fills_windows
from bot.binance_readonly_income_history_collector_v1 import collect_income_window
from bot.binance_collector_output_receipts_v1 import validate_collector_outputs


class FakeBinance:
    def __init__(self, fills, income):
        self.fills = fills
        self.income = income
        self.calls = []

    async def _get(self, route, params, auth=False):
        self.calls.append((route, dict(params), auth))
        if not auth:
            raise AssertionError("authenticated read required")
        if route == "/fapi/v1/userTrades":
            return list(self.fills)
        if route == "/fapi/v1/income":
            return list(self.income)
        raise AssertionError("unexpected endpoint")


class IntegratedCollectorTests(unittest.TestCase):
    def test_collect_manifest_verify_roundtrip(self):
        client = FakeBinance(
            [{"symbol": "BTCUSDT", "id": 12, "time": 1002,
              "commission": "0.01", "commissionAsset": "USDT"}],
            [{"tranId": 55, "time": 1003, "incomeType": "FUNDING_FEE",
              "income": "-0.01", "asset": "USDT"}])
        fills = asyncio.run(collect_fills_windows(
            client=client, symbol="BTCUSDT", start_ms=1000,
            end_ms=1010, window_ms=11))
        income = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=1010))
        self.assertEqual(fills["status"], "FILLS_WINDOWS_SHAPE_VALID")
        self.assertEqual(income["status"], "INCOME_WINDOW_SHAPE_VALID")
        receipt = validate_collector_outputs(
            fills_by_symbol={"BTCUSDT": fills}, income_result=income,
            required_symbols=["BTCUSDT"], start_ms=1000, end_ms=1010)
        self.assertEqual(receipt["status"], "COLLECTOR_RECEIPTS_SHAPE_VALID")
        self.assertFalse(receipt["live_allowed"])
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(auth for _, _, auth in client.calls))

    def test_saturated_fills_fail_closed(self):
        client = FakeBinance(
            [{"symbol": "BTCUSDT", "id": 12, "time": 1002}], [])
        fills = asyncio.run(collect_fills_windows(
            client=client, symbol="BTCUSDT", start_ms=1000,
            end_ms=1010, window_ms=11, limit=1))
        self.assertEqual(fills["status"], "PROOF_MISSING")

    def test_outside_window_fills_fail_closed(self):
        client = FakeBinance(
            [{"symbol": "BTCUSDT", "id": 12, "time": 1011}], [])
        fills = asyncio.run(collect_fills_windows(
            client=client, symbol="BTCUSDT", start_ms=1000,
            end_ms=1010, window_ms=11))
        self.assertEqual(fills["status"], "PROOF_MISSING")

    def test_income_saturated_page_fails_closed(self):
        client = FakeBinance([], [
            {"tranId": 1, "time": 1003, "incomeType": "FUNDING_FEE",
             "income": "-0.01", "asset": "USDT"}])
        income = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=1010, limit=1,
            max_pages=1))
        self.assertEqual(income["status"], "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
