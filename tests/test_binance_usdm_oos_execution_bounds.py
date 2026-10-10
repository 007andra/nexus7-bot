import unittest
from decimal import Decimal
from bot.binance_usdm_oos_execution_bounds import estimate_market_entry

QUOTE = {"timestamp_ms": 1791504000100, "bid": "100", "ask": "100.1",
         "independent_source_verified": True}


class TestExecutionBounds(unittest.TestCase):
    def test_short_uses_bid_with_adverse_slippage(self):
        r = estimate_market_entry(side="SHORT", decision_ms=1791504000000,
                                  quote=QUOTE, quantity="1", slippage_bps="2")
        self.assertEqual(r["status"], "HYPOTHETICAL_ENTRY_BOUND")
        self.assertLess(Decimal(r["price"]), Decimal("100"))
        self.assertFalse(r["hypothetical_fill_proven"])

    def test_long_uses_ask_with_adverse_slippage(self):
        r = estimate_market_entry(side="LONG", decision_ms=1791504000000,
                                  quote=QUOTE, quantity="1", slippage_bps="2")
        self.assertGreater(Decimal(r["price"]), Decimal("100.1"))

    def test_predecision_quote_rejected(self):
        r = estimate_market_entry(side="SHORT", decision_ms=1791504000200,
                                  quote=QUOTE, quantity="1", slippage_bps="2")
        self.assertIn("STALE_OR_PREDECISION_QUOTE", r["blockers"])

    def test_unverified_quote_rejected(self):
        q = dict(QUOTE, independent_source_verified=False)
        r = estimate_market_entry(side="SHORT", decision_ms=1791504000000,
                                  quote=q, quantity="1", slippage_bps="2")
        self.assertEqual(r["status"], "NET_PROOF_MISSING")

    def test_crossed_book_rejected(self):
        q = dict(QUOTE, bid="101")
        r = estimate_market_entry(side="SHORT", decision_ms=1791504000000,
                                  quote=q, quantity="1", slippage_bps="2")
        self.assertIn("INVALID_BBO", r["blockers"])


if __name__ == "__main__":
    unittest.main()
