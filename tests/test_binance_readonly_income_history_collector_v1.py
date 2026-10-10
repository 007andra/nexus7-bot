"""No-network regression tests for income-history pagination."""
import asyncio
import unittest

from bot.binance_readonly_income_history_collector_v1 import collect_income_window


class Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append((endpoint, dict(params), auth))
        return self.pages.pop(0)


def event(identity, time):
    return {"tranId": identity, "time": time, "incomeType": "FUNDING_FEE",
            "income": "0", "asset": "USDT"}


class IncomeHistoryCollectorTests(unittest.TestCase):
    def test_contiguous_two_pages(self):
        client = Client([[event(1, 1100), event(2, 1200)], [event(3, 1300)]])
        result = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=2000, limit=2))
        self.assertEqual(result["status"], "INCOME_WINDOW_SHAPE_VALID")
        self.assertEqual(len(result["records"]), 3)
        self.assertTrue(all(call[2] for call in client.calls))
        self.assertEqual(client.calls[1][1]["startTime"], 1201)

    def test_same_timestamp_page_boundary_fails_closed(self):
        client = Client([[event(1, 1200), event(2, 1200)]])
        result = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=2000, limit=2))
        self.assertEqual(result["status"], "PROOF_MISSING")

    def test_exhausted_page_budget_fails_closed(self):
        client = Client([[event(1, 1100), event(2, 1200)]])
        result = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=2000, limit=2, max_pages=1))
        self.assertEqual(result["status"], "PROOF_MISSING")

    def test_read_error_fails_closed(self):
        client = Client([])
        result = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=2000, limit=2))
        self.assertEqual(result["status"], "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
