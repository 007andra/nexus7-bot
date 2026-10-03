import unittest

from bot.microstructure_oos import compare_rankers_oos
from bot.opportunity_ranker import Opportunity


class MicrostructureOOSTests(unittest.TestCase):
    def test_same_population_compares_base_and_enriched_rank(self):
        items = [
            Opportunity(
                "a", "BTCUSDT", 1.0, 2.0, 80, 80, 80, 0.001,
                side="LONG", confidence=70,
                microstructure_alignment=-1.0, taker_pressure=-1.0,
                depth_notional_1pct=1_000_000,
            ),
            Opportunity(
                "b", "ETHUSDT", 0.9, 2.0, 80, 80, 80, 0.001,
                side="LONG", confidence=70,
                microstructure_alignment=1.0, taker_pressure=1.0,
                depth_notional_1pct=1_000_000,
            ),
            Opportunity(
                "c", "SOLUSDT", 0.8, 2.0, 80, 80, 80, 0.001,
                side="LONG", confidence=70,
                microstructure_alignment=0.8, taker_pressure=0.8,
                depth_notional_1pct=1_000_000,
            ),
            Opportunity(
                "d", "XRPUSDT", 0.7, 2.0, 80, 80, 80, 0.001,
                side="LONG", confidence=70,
                microstructure_alignment=-0.8, taker_pressure=-0.8,
                depth_notional_1pct=1_000_000,
            ),
        ]
        realized = {"a": -1.0, "b": 2.0, "c": 1.5, "d": -1.0}
        report = compare_rankers_oos(items, realized)
        self.assertEqual(report["observed_n"], 4)
        self.assertGreater(report["spread_delta_r"], 0)
        self.assertGreater(report["spearman_delta"], 0)
        self.assertEqual(report["execution_effect"], "NONE")
        self.assertFalse(report["promotion_authority"])

    def test_incomplete_microstructure_is_excluded_not_imputed(self):
        complete = Opportunity(
            "a", "BTCUSDT", 1, 2, 80, 80, 80, 0.001,
            microstructure_alignment=0.2, taker_pressure=0.1,
            depth_notional_1pct=1000,
        )
        incomplete = Opportunity(
            "b", "ETHUSDT", 1, 2, 80, 80, 80, 0.001,
        )
        report = compare_rankers_oos(
            [complete, incomplete],
            {"a": 1.0, "b": -1.0},
        )
        self.assertEqual(report["population_n"], 2)
        self.assertEqual(report["microstructure_complete_n"], 1)
        self.assertEqual(report["observed_n"], 1)
        self.assertIsNone(report["spread_delta_r"])

    def test_realized_outcomes_are_required_only_for_evaluation(self):
        item = Opportunity(
            "a", "BTCUSDT", 1, 2, 80, 80, 80, 0.001,
            microstructure_alignment=0.2, taker_pressure=0.1,
            depth_notional_1pct=1000,
        )
        report = compare_rankers_oos([item], {})
        self.assertEqual(report["observed_n"], 0)
        self.assertIsNone(report["spearman_delta"])

    def test_duplicate_candidate_identity_fails_closed(self):
        item = Opportunity(
            "a", "BTCUSDT", 1, 2, 80, 80, 80, 0.001,
            microstructure_alignment=0.2, taker_pressure=0.1,
            depth_notional_1pct=1000,
        )
        # The same event twice is a true duplicate observation: fail closed.
        with self.assertRaisesRegex(ValueError, "duplicate research observation"):
            compare_rankers_oos([item, item], {"a": 1.0})


if __name__ == "__main__":
    unittest.main()
