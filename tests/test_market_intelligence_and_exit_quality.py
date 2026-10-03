import unittest

from bot.exit_quality_v1 import (
    ExitEvidence,
    exit_quality_report,
    trade_attribution,
)
from bot.macro_regime_v1 import MacroZ, macro_context
from bot.news_event_study import Event, MarketPoint, event_window
from bot.order_flow_v1 import AggressorTrade, order_flow_snapshot
from bot.spoof_iceberg_evidence import (
    LevelObservation,
    analyze_level_sequence,
)


class OrderFlowTests(unittest.TestCase):
    def test_binance_aggressor_semantics(self):
        report = order_flow_snapshot([
            AggressorTrade(1, 100, 10, False),
            AggressorTrade(2, 100.01, 2, True),
        ])
        self.assertEqual(report["aggressive_buy_volume"], 10)
        self.assertEqual(report["aggressive_sell_volume"], 2)
        self.assertGreater(report["cvd"], 0)
        self.assertEqual(report["execution_effect"], "NONE")

    def test_spoof_is_only_candidate_evidence(self):
        report = analyze_level_sequence([
            LevelObservation(1, "ASK", 101, 10, 0),
            LevelObservation(2, "ASK", 101, 100, 1),
            LevelObservation(3, "ASK", 101, 5, 1),
        ])
        self.assertTrue(report["spoof_candidate"])
        self.assertEqual(report["predictive_value"], "NOT_PROVEN")
        self.assertEqual(report["execution_effect"], "NONE")


class EventAndMacroTests(unittest.TestCase):
    def test_event_study_uses_fixed_pre_post_window(self):
        market = [
            MarketPoint(0, 100, 1),
            MarketPoint(10, 101, 2),
            MarketPoint(20, 102, 3),
            MarketPoint(30, 99, 4),
        ]
        row = event_window(
            Event(10, "CPI"),
            market,
            pre_seconds=10,
            post_seconds=20,
        )
        self.assertEqual(row["event_type"], "CPI")
        self.assertAlmostEqual(row["pre_return_pct"], 1.0)
        self.assertLess(row["post_return_pct"], 0)

    def test_macro_is_context_not_direction_authority(self):
        report = macro_context(
            MacroZ(-1, -1, -1, 1, 1, -1, 1)
        )
        self.assertEqual(report["context_label"], "RISK_ON_CONTEXT")
        self.assertFalse(report["direction_authority"])
        self.assertEqual(report["execution_effect"], "NONE")


class ExitQualityTests(unittest.TestCase):
    def test_attribution_and_exit_groups(self):
        trade = ExitEvidence(
            "t1",
            "TP1_TRAILING",
            1.2,
            2.0,
            -0.5,
            12.0,
            1.0,
            -0.2,
            -0.3,
            -0.4,
        )
        attribution = trade_attribution(trade)
        self.assertLess(
            attribution["net_reconstructed_pnl"],
            attribution["gross_price_pnl"],
        )
        report = exit_quality_report([trade])
        self.assertIn("TP1_TRAILING", report["by_exit_reason"])
        self.assertAlmostEqual(
            report["by_exit_reason"]["TP1_TRAILING"][
                "average_profit_capture_ratio"
            ],
            0.6,
        )
        self.assertEqual(report["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
