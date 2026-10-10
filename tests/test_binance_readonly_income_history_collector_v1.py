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
    def test_saturated_first_page_never_advances(self):
        client = Client([[event(1, 1100), event(2, 1200)], [event(3, 1300)]])
        result = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=2000, limit=2))
        self.assertEqual(result["status"], "PROOF_MISSING")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][2])

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


    def test_full_page_unique_timestamp_still_fails_closed(self):
        class Client:
            def __init__(self):
                self.calls = 0

            async def _get(self, route, params, auth=False):
                self.calls += 1
                return [{"tranId": 1, "time": 1001, "incomeType": "FUNDING_FEE",
                         "income": "0.01", "asset": "USDT"}]

        client = Client()
        result = asyncio.run(collect_income_window(
            client=client, start_ms=1000, end_ms=2000, limit=1))
        self.assertEqual(result["status"], "PROOF_MISSING")
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
