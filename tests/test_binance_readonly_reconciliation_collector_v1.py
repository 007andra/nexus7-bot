"""Offline collector tests with a fake client; no exchange or production calls."""
import asyncio
import unittest

from bot.binance_readonly_reconciliation_collector_v1 import collect_reconciliation


class FakeClient:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    async def _get(self, endpoint, auth=False):
        self.calls.append((endpoint, auth))
        if endpoint == self.fail:
            raise RuntimeError("secret-bearing simulated failure")
        if endpoint == "/fapi/v3/balance":
            return [{"asset": "USDT", "balance": "5.5",
                     "availableBalance": "5", "crossUnPnl": "-0.2"}]
        return []


class ReadonlyCollectorTests(unittest.TestCase):
    def test_four_authenticated_gets_no_mutation(self):
        client = FakeClient()
        report = asyncio.run(collect_reconciliation(
            client=client,
            internal={"equity_usdt": "5.3", "active_positions": 0,
                      "open_orders": 0, "algo_orders": 0},
            internal_captured_at="2026-10-10T14:00:00Z",
        ))
        self.assertEqual(report["status"], "BASIC_SNAPSHOT_MATCH")
        self.assertEqual(len(client.calls), 4)
        self.assertIn(("/fapi/v1/openAlgoOrders", True), client.calls)
        self.assertTrue(all(auth for _, auth in client.calls))
        self.assertFalse(report["live_allowed"])

    def test_read_exception_fails_closed_without_leaking_details(self):
        report = asyncio.run(collect_reconciliation(
            client=FakeClient(fail="/fapi/v1/openOrders"),
            internal={}, internal_captured_at="2026-10-10T14:00:00Z"))
        self.assertEqual(report["status"], "PROOF_MISSING")
        self.assertNotIn("secret-bearing", str(report))

    def test_internal_timestamp_required_before_exchange_reads(self):
        client = FakeClient()
        report = asyncio.run(collect_reconciliation(
            client=client, internal={}, internal_captured_at=""))
        self.assertEqual(report["status"], "PROOF_MISSING")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
