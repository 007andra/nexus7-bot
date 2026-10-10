"""Offline contract tests; no Binance network, keys, or production writes."""
import unittest

from bot.binance_readonly_reconciliation_proof_v1 import compare_readonly_snapshots

SOURCES = {k: "2026-10-10T14:00:00Z" for k in
           ("balance", "positions", "orders", "algo_orders", "internal")}


def check(**overrides):
    payload = dict(
        balance_rows=[{"asset": "USDT", "balance": "5.5",
                       "availableBalance": "5.0", "crossUnPnl": "-0.2"}],
        position_rows=[], order_rows=[], algo_order_rows=[],
        internal={"equity_usdt": "5.3", "active_positions": 0,
                  "open_orders": 0, "algo_orders": 0},
        sources=SOURCES,
    )
    payload.update(overrides)
    return compare_readonly_snapshots(**payload)


class ReadonlyReconciliationProofTests(unittest.TestCase):
    def test_basic_match_is_not_live_approval(self):
        result = check()
        self.assertEqual(result["status"], "BASIC_SNAPSHOT_MATCH")
        self.assertFalse(result["live_allowed"])
        self.assertEqual(result["execution_effect"], "NONE")

    def test_available_balance_not_equity(self):
        result = check()
        self.assertEqual(result["exchange_equity_usdt"], "5.3")
        self.assertEqual(result["available_usdt"], "5.0")

    def test_missing_timestamp_fails_closed(self):
        self.assertEqual(check(sources={})["status"], "PROOF_MISSING")

    def test_missing_algo_snapshot_fails_closed(self):
        self.assertEqual(check(algo_order_rows=None)["status"], "PROOF_MISSING")

    def test_position_mismatch_blocks(self):
        rows = [{"positionAmt": "1", "symbol": "BTCUSDT"}]
        self.assertIn("POSITION_COUNT_MISMATCH", check(position_rows=rows)["discrepancies"])

    def test_conditional_order_mismatch_blocks(self):
        self.assertIn("ALGO_ORDER_COUNT_MISMATCH",
                      check(algo_order_rows=[{"symbol": "BTCUSDT"}])["discrepancies"])

    def test_equity_mismatch_blocks(self):
        self.assertEqual(check(internal={"equity_usdt": "6", "active_positions": 0,
                                         "open_orders": 0, "algo_orders": 0})["status"],
                         "RECONCILIATION_FAIL")

    def test_malformed_value_fails_closed(self):
        self.assertEqual(check(balance_rows=[{"asset": "USDT", "balance": "NaN",
                      "availableBalance": "1", "crossUnPnl": "0"}])["status"],
                         "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
