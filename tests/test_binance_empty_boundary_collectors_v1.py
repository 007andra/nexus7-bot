"""Offline boundary and empty-page tests for read-only collectors."""
import asyncio
import unittest

from bot.binance_readonly_fills_history_collector_v1 import collect_symbol_fills
from bot.binance_readonly_income_history_collector_v1 import collect_income_window
from bot.binance_readonly_fills_windows_v1 import collect_fills_windows


class EmptyClient:
    def __init__(self):
        self.calls = []

    async def _get(self, path, params, auth=False):
        self.calls.append((path, dict(params), auth))
        return []


class BoundaryTests(unittest.TestCase):
    def test_empty_fills_single_millisecond(self):
        client = EmptyClient()
        result = asyncio.run(collect_symbol_fills(
            client=client, symbol="BTCUSDT", start_ms=1000, end_ms=1000))
        self.assertEqual(result["status"], "FILLS_WINDOW_SHAPE_VALID")
        self.assertEqual(result["records"], [])
        self.assertTrue(result["terminal_page_verified"])

    def test_empty_income_window(self):
        client = EmptyClient()
        result = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=1010))
        self.assertEqual(result["status"], "INCOME_WINDOW_SHAPE_VALID")
        self.assertEqual(result["records"], [])
        self.assertTrue(result["terminal_page_verified"])

    def test_empty_fills_multiple_windows(self):
        client = EmptyClient()
        result = asyncio.run(collect_fills_windows(
            client=client, symbol="BTCUSDT", start_ms=1000, end_ms=1010,
            window_ms=5))
        self.assertEqual(result["status"], "FILLS_WINDOWS_SHAPE_VALID")
        self.assertEqual(result["window_count"], 3)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[-1][1]["startTime"], 1010)
        self.assertEqual(client.calls[-1][1]["endTime"], 1010)

    def test_empty_reads_are_authenticated_get_only(self):
        client = EmptyClient()
        asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=1010))
        self.assertEqual([p for p, _, _ in client.calls], ["/fapi/v1/income"])
        self.assertTrue(all(auth for _, _, auth in client.calls))


if __name__ == "__main__":
    unittest.main()
