import unittest

from bot.trade_attribution import TradeAttributionInput, attribute_trade


class TradeAttributionTests(unittest.TestCase):
    def test_long_attribution_reconciles_to_net(self):
        result = attribute_trade(TradeAttributionInput(
            side="LONG", qty=2, decision_entry=100, actual_entry=101,
            decision_exit=110, actual_exit=109, fees=1.0, funding=-0.5,
            regime="TREND", exit_reason="TP",
        ))
        self.assertEqual(result["market_pnl_at_decision_prices"], 20)
        self.assertEqual(result["entry_execution_effect"], -2)
        self.assertEqual(result["exit_execution_effect"], -2)
        self.assertEqual(result["gross_pnl"], 16)
        self.assertEqual(result["net_pnl"], 14.5)
        self.assertTrue(result["reconciles"])

    def test_short_attribution_handles_better_entry_and_exit(self):
        result = attribute_trade(TradeAttributionInput(
            side="SHORT", qty=1, decision_entry=100, actual_entry=101,
            decision_exit=90, actual_exit=89, fees=0.5,
        ))
        self.assertGreater(result["entry_execution_effect"], 0)
        self.assertGreater(result["exit_execution_effect"], 0)
        self.assertGreater(
            result["net_pnl"], result["market_pnl_at_decision_prices"]
        )

    def test_invalid_side_rejected(self):
        with self.assertRaises(ValueError):
            TradeAttributionInput("FLAT", 1, 1, 1, 1, 1)


if __name__ == "__main__":
    unittest.main()
