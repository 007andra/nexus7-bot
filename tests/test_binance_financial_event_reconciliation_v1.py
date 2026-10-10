"""Offline tests for financial event reconciliation; no Binance credentials."""
import unittest
from bot.binance_financial_event_reconciliation_v1 import reconcile_financial_events as check

COVERAGE = dict(window_start_ms=1000, window_end_ms=2000,
                trades_complete=True, income_complete=True, internal_complete=True)
INTERNAL = dict(fill_count=1, fees_usdt="0.02", funding_usdt="-0.01",
                transfers_usdt="2.5")
TRADES = [dict(time=1500, commission="0.02")]
INCOME = [dict(time=1600, incomeType="FUNDING_FEE", income="-0.01"),
          dict(time=1700, incomeType="TRANSFER", income="2.5")]


def run(**overrides):
    data = dict(trades=TRADES, income=INCOME, internal=INTERNAL, coverage=COVERAGE)
    data.update(overrides)
    return check(**data)


class FinancialEventReconciliationTests(unittest.TestCase):
    def test_exact_aggregate_match_never_authorizes_live(self):
        result = run()
        self.assertEqual(result["status"], "EVENTS_MATCH")
        self.assertFalse(result["live_allowed"])

    def test_missing_completeness_fails_closed(self):
        self.assertEqual(run(coverage={**COVERAGE, "income_complete": False})["status"],
                         "PROOF_MISSING")

    def test_fee_discrepancy_is_reported(self):
        result = run(internal={**INTERNAL, "fees_usdt": "0.03"})
        self.assertIn("FEES_USDT_MISMATCH", result["discrepancies"])

    def test_unknown_income_type_fails_closed(self):
        result = run(income=[{"time": 1600, "incomeType": "UNRECOGNIZED", "income": "1"}])
        self.assertEqual(result["status"], "PROOF_MISSING")

    def test_outside_window_fails_closed(self):
        self.assertEqual(run(trades=[{"time": 3000, "commission": "0.02"}])["status"],
                         "PROOF_MISSING")

    def test_nonfinite_fee_fails_closed(self):
        self.assertEqual(run(trades=[{"time": 1500, "commission": "NaN"}])["status"],
                         "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
