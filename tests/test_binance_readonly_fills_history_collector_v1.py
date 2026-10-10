"""Offline tests for Binance fills history collector."""
import asyncio
import unittest

from bot.binance_readonly_fills_history_collector_v1 import collect_symbol_fills


def trade(id, time=1500):
    return {"id": id, "time": time, "symbol": "BTCUSDT", "commission": "0",
            "commissionAsset": "USDT"}


class Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append((endpoint, dict(params), auth))
        return self.pages.pop(0)


def run(client, **kwargs):
    return asyncio.run(collect_symbol_fills(
        client=client, symbol="BTCUSDT", start_ms=1000, end_ms=2000,
        limit=2, **kwargs))


class FillsHistoryTests(unittest.TestCase):
    def test_two_pages_and_cursor(self):
        client = Client([[trade(1), trade(2, 1600)], [trade(3, 1700)]])
        result = run(client)
        self.assertEqual(result["status"], "PROOF_MISSING")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all(call[2] for call in client.calls))

    def test_duplicate_id_fails_closed(self):
        self.assertEqual(run(Client([[trade(1), trade(2)], [trade(2)]]))["status"],
                         "PROOF_MISSING")

    def test_page_budget_exhaustion(self):
        self.assertEqual(run(Client([[trade(1), trade(2)]]), max_pages=1)["status"],
                         "PROOF_MISSING")

    def test_symbol_mismatch_fails_closed(self):
        row = {**trade(1), "symbol": "ETHUSDT"}
        self.assertEqual(run(Client([[row]]))["status"], "PROOF_MISSING")

    def test_error_fails_closed(self):
        self.assertEqual(run(Client([]))["status"], "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
