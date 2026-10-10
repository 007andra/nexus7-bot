"""No-network checks for financial history bridge coverage gating."""
import unittest

from bot.binance_financial_history_bridge_v1 import reconcile_collected_history

COVERAGE = {
    "window_start_ms": 1000, "window_end_ms": 2000,
    "trades_complete": True, "income_complete": True, "internal_complete": True,
    "symbol_universe_verified": True,
    "historical_endpoint_limits_reviewed": True,
    "source_completeness_independently_verified": True,
}
FILL = {"symbol": "BTCUSDT", "id": 1, "time": 1500,
        "commissionAsset": "USDT", "commission": "0.02"}
INTERNAL = {"fill_count": 1, "fees_usdt": "0.02",
            "funding_usdt": "0", "transfers_usdt": "0"}


def run(**changes):
    args = dict(
        fills_by_symbol={"BTCUSDT": {"status": "FILLS_WINDOW_SHAPE_VALID",
                                     "records": [FILL]}},
        income_result={"status": "INCOME_WINDOW_SHAPE_VALID", "records": []},
        internal=INTERNAL, coverage=COVERAGE, required_symbols=["BTCUSDT"])
    args.update(changes)
    return reconcile_collected_history(**args)


class BridgeTests(unittest.TestCase):
    def test_independently_attested_bounded_aggregate(self):
        result = run()
        self.assertEqual(result["status"], "EVENTS_MATCH")
        self.assertFalse(result["live_allowed"])

    def test_missing_attestation_fails_closed(self):
        result = run(coverage={**COVERAGE,
                               "source_completeness_independently_verified": False})
        self.assertEqual(result["status"], "PROOF_MISSING")

    def test_missing_symbol_fails_closed(self):
        self.assertEqual(run(required_symbols=["BTCUSDT", "ETHUSDT"])["status"],
                         "PROOF_MISSING")

    def test_unverified_endpoint_limits_fails_closed(self):
        self.assertEqual(run(coverage={**COVERAGE,
                                       "historical_endpoint_limits_reviewed": False})["status"],
                         "PROOF_MISSING")

    def test_income_failure_fails_closed(self):
        self.assertEqual(run(income_result={"status": "PROOF_MISSING"})["status"],
                         "PROOF_MISSING")


    def test_time_sliced_fills_bridge(self):
        result = run(fills_by_symbol={
            "BTCUSDT": {"status": "FILLS_WINDOWS_SHAPE_VALID",
                        "window_count": 2, "records": [FILL]}})
        self.assertEqual(result["status"], "EVENTS_MATCH")
        self.assertFalse(result["live_allowed"])

    def test_time_sliced_fills_missing_rows_fails_closed(self):
        result = run(fills_by_symbol={
            "BTCUSDT": {"status": "FILLS_WINDOWS_SHAPE_VALID",
                        "window_count": 2}})
        self.assertEqual(result["status"], "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
