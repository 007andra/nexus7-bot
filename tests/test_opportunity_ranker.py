import unittest

from bot.opportunity_ranker import (
    Opportunity,
    apply_cross_sectional_liquidity,
    evaluate_ranked_outcomes,
    rank_opportunities,
)


class OpportunityRankerTests(unittest.TestCase):
    def test_better_ev_rr_and_liquidity_rank_higher(self):
        better = Opportunity(
            "a", "BTCUSDT", 1.2, 2.2, 80, 95, 85, 0.001,
            confidence=75,
        )
        worse = Opportunity(
            "b", "ALTUSDT", 0.2, 1.2, 65, 50, 60, 0.004,
            confidence=55,
        )
        ranked = rank_opportunities([worse, better])
        self.assertEqual(ranked[0][0].candidate_id, "a")
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_tie_break_is_deterministic(self):
        a = Opportunity("a", "BTCUSDT", 1.0, 2, 80, 80, 80, 0.001)
        b = Opportunity("b", "ETHUSDT", 1.0, 2, 80, 80, 80, 0.001)
        self.assertEqual(
            [x[0].candidate_id for x in rank_opportunities([b, a])],
            ["a", "b"],
        )

    def test_aligned_microstructure_improves_shadow_rank(self):
        aligned = Opportunity(
            "a", "BTCUSDT", 1.0, 2, 80, 50, 80, 0.001,
            side="LONG",
            confidence=70,
            microstructure_alignment=0.8,
            taker_pressure=0.6,
        )
        opposed = Opportunity(
            "b", "ETHUSDT", 1.0, 2, 80, 50, 80, 0.001,
            side="LONG",
            confidence=70,
            microstructure_alignment=-0.8,
            taker_pressure=-0.6,
        )
        ranked = rank_opportunities([opposed, aligned])
        self.assertEqual(ranked[0][0].candidate_id, "a")

    def test_cross_sectional_liquidity_uses_same_batch_depth(self):
        items = [
            Opportunity(
                "a", "BTCUSDT", 1.0, 2, 80, 50, 80, 0.001,
                depth_notional_1pct=5_000_000,
            ),
            Opportunity(
                "b", "ALTUSDT", 1.0, 2, 80, 50, 80, 0.001,
                depth_notional_1pct=100_000,
            ),
        ]
        rescored = apply_cross_sectional_liquidity(items)
        by_id = {item.candidate_id: item for item in rescored}
        self.assertEqual(by_id["a"].liquidity_score, 100.0)
        self.assertEqual(by_id["b"].liquidity_score, 0.0)

    def test_realized_outcome_never_changes_pretrade_rank(self):
        a = Opportunity(
            "a", "BTCUSDT", 1.5, 2.2, 85, 80, 90, 0.001,
            side="LONG", confidence=80,
        )
        b = Opportunity(
            "b", "ETHUSDT", 0.5, 1.7, 70, 70, 70, 0.001,
            side="LONG", confidence=60,
        )
        before = [item.candidate_id for item, _ in rank_opportunities([a, b])]
        evaluation = evaluate_ranked_outcomes(
            [a, b], {"a": -1.0, "b": 3.0}
        )
        after = [item.candidate_id for item, _ in rank_opportunities([a, b])]
        self.assertEqual(before, after)
        self.assertEqual(before[0], "a")
        self.assertEqual(evaluation["n"], 2)
        self.assertEqual(evaluation["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
