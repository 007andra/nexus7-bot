"""Deterministic research-only tests for selection-strata overlap diagnostic."""
import unittest

from bot import selection_bias_stratified_v1 as analysis


def row(side, regime, setup, approved, ret, *, status="ALLOWED"):
    return {
        "side": side, "regime": regime, "setup": setup,
        "allowed": approved, "future_return": ret, "status": status,
        "candidate_id": "PRIVATE_SAMPLE_MUST_NOT_LEAK",
    }


class StratifiedSelectionTests(unittest.TestCase):
    def test_simpson_reversal_is_diagnostic_not_causal(self):
        # High-return stratum is mostly rejected; low-return mostly approved.
        # In each stratum the approved arm is +1ppt better, yet the pooled
        # average is worse for approved because the group composition differs.
        records = (
            [row("SHORT", "TRENDING_DOWN", "BOS_BREAK", True, 0.09)] * 5
            + [row("SHORT", "TRENDING_DOWN", "BOS_BREAK", False, 0.08)] * 10
            + [row("LONG", "TRENDING_UP", "MOMENTUM", True, 0.01)] * 10
            + [row("LONG", "TRENDING_UP", "MOMENTUM", False, 0.00)] * 5
        )
        result = analysis.evaluate(records)
        self.assertEqual(result["status"], "COMPARABLE_OVERLAP")
        self.assertEqual(result["eligible_strata_n"], 2)
        self.assertEqual(result["eligible_allowed_n"], 15)
        self.assertEqual(result["eligible_rejected_n"], 15)
        self.assertLess(result["overall_mean_lift"], 0)
        self.assertAlmostEqual(result["within_strata_weighted_lift"], 0.01)
        self.assertTrue(result["possible_simpson_reversal"])
        self.assertFalse(result["live_allowed"])
        self.assertFalse(result["promotion_allowed"])
        self.assertNotIn("PRIVATE_SAMPLE_MUST_NOT_LEAK", analysis.format_log(result,horizon=60))

    def test_no_matched_control_must_not_claim_positive_edge(self):
        rows = [row("SHORT", "TRENDING_DOWN", "BOS_BREAK", True, 0.02)] * 60
        rows += [row("LONG", "TRENDING_UP", "MOMENTUM", False, -0.02)] * 100
        result = analysis.evaluate(rows)
        self.assertEqual(result["status"], "INSUFFICIENT_OVERLAP")
        self.assertEqual(result["eligible_strata_n"], 0)
        self.assertIsNone(result["within_strata_weighted_lift"])
        self.assertIsNotNone(result["overall_mean_lift"])
        self.assertFalse(result["possible_simpson_reversal"])
        self.assertIn("within_strata_weighted_lift=NA", analysis.format_log(result,horizon=240))

    def test_minimum_arm_count_and_weighted_average(self):
        rows = [row("SHORT", "DOWN", "BOS", True, 0.0)] * 6
        rows += [row("SHORT", "DOWN", "BOS", False, 0.02)] * 5
        rows += [row("LONG", "UP", "PULLBACK", True, 0.04)] * 5
        rows += [row("LONG", "UP", "PULLBACK", False, 0.02)] * 5
        rows += [row("SHORT", "DOWN", "MOMENTUM", True, 0.90)] * 4
        rows += [row("SHORT", "DOWN", "MOMENTUM", False, 0.10)] * 5
        result = analysis.evaluate(rows)
        self.assertEqual(result["eligible_strata_n"], 2)
        self.assertEqual(result["total_strata_n"], 3)
        self.assertEqual(result["eligible_allowed_n"], 11)
        self.assertEqual(result["observed_allowed_n"], 15)
        self.assertAlmostEqual(result["within_strata_weighted_lift"], 0.0)
        self.assertAlmostEqual(result["eligible_allowed_share"], 11/15)

    def test_missing_error_nonfinite_and_invalid_flags_ignored(self):
        valid = [row("SHORT", "DOWN", "BOS", True, 0.01)]*5
        valid += [row("SHORT", "DOWN", "BOS", False, 0)]*5
        valid += [
            row("SHORT", "DOWN", "BOS", True, float("nan")),
            row("SHORT", "DOWN", "BOS", False, float("inf")),
            row("SHORT", "DOWN", "BOS", True, 100, status="ERROR"),
            {**row("SHORT", "DOWN", "BOS", True, 100), "allowed": "true"},
            None,
        ]
        result = analysis.evaluate(valid)
        self.assertEqual(result["observed_allowed_n"], 5)
        self.assertEqual(result["observed_rejected_n"], 5)
        self.assertAlmostEqual(result["within_strata_weighted_lift"], 0.01)
        self.assertEqual(result["execution_effect"], "NONE")

    def test_fail_closed_on_misconfigured_guards_or_horizon(self):
        for limit in (None, 0, 1, -3, True, 3.2):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                analysis.evaluate([], min_per_arm=limit)
        with self.assertRaises(ValueError):
            analysis.format_log(analysis.evaluate([]), horizon=30)


if __name__ == "__main__":
    unittest.main()
