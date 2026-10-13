"""Pure offline tests: no production DB, Binance key, calls or orders."""
import unittest

from bot import oos_setup_exclusion_sensitivity_v1 as audit


def report():
    # Baseline: approved n=10 / mean -0.002; rejected n=20 / mean +0.005
    # MOMENTUM approved n=3 mean -0.03, rejected n=5 mean +0.02
    # BOS_BREAK approved n=7 mean +0.01, rejected n=15 mean 0.
    # Repeated at both horizons to ensure horizon-specific output.
    groups = [
        {"dimension": "setup", "value": "MOMENTUM",
         "allowed_n": 3, "allowed_avg_return": -0.03,
         "rejected_n": 5, "rejected_avg_return": 0.02},
        {"dimension": "setup", "value": "BOS_BREAK",
         "allowed_n": 7, "allowed_avg_return": 0.01,
         "rejected_n": 15, "rejected_avg_return": 0.0},
    ]
    return {
        "status": "NEGATIVE_SELECTION_CONFIRMED_BOTH_HORIZONS",
        "allowed_60m": {"n": 10, "avg_return": -0.002},
        "rejected_60m": {"n": 20, "avg_return": 0.005},
        "allowed_240m": {"n": 10, "avg_return": -0.002},
        "rejected_240m": {"n": 20, "avg_return": 0.005},
        "dimensions_60m": {"setup": groups},
        "dimensions_240m": {"setup": groups},
        "candidate_ids": ["SENSITIVE_DO_NOT_ECHO"],
    }


class SetupSensitivityTests(unittest.TestCase):
    def test_momentum_exclusion_is_only_a_retrospective_hypothesis(self):
        x = audit.evaluate(report())
        self.assertEqual(x["status"], "HISTORICAL_SETUP_SENSITIVITY_ONLY")
        self.assertFalse(x["live_allowed"])
        self.assertFalse(x["promotion_allowed"])
        self.assertFalse(x["real_execution_net_proof"])
        self.assertTrue(x["prospective_confirmation_required"])
        m60 = next(r for r in x["results"]
                   if r["horizon_min"] == 60 and r["excluded_setup"] == "MOMENTUM")
        self.assertEqual(m60["status"], "DESCRIPTIVE_ONLY")
        self.assertEqual(m60["remaining_allowed_n"], 7)
        self.assertEqual(m60["remaining_rejected_n"], 15)
        self.assertAlmostEqual(m60["baseline_lift"], -0.007)
        self.assertAlmostEqual(m60["remaining_lift"], 0.01)
        self.assertAlmostEqual(m60["change_in_lift"], 0.017)
        self.assertEqual(len(x["results"]), 6)
        self.assertNotIn("SENSITIVE_DO_NOT_ECHO", str(audit.public_summary(report())))

    def test_excluding_bos_with_tiny_remnant_does_not_claim_edge(self):
        x = audit.evaluate(report())
        b = next(r for r in x["results"]
                 if r["horizon_min"] == 240 and r["excluded_setup"] == "BOS_BREAK")
        self.assertEqual(b["remaining_allowed_n"], 3)
        self.assertEqual(b["remaining_rejected_n"], 5)
        self.assertEqual(b["status"], "INSUFFICIENT_REMAINDER")
        self.assertIsNone(b["remaining_lift"])

    def test_comparison_must_exclude_same_setup_from_both_arms(self):
        x = audit.evaluate(report())
        row = next(r for r in x["results"] if r["horizon_min"] == 60
                   and r["excluded_setup"] == "MOMENTUM")
        self.assertEqual(row["excluded_rejected_n"], 5)
        self.assertEqual(row["excluded_allowed_n"], 3)

    def test_missing_setup_is_not_treated_as_unobserved_winner(self):
        x = audit.evaluate(report())
        pullback = next(r for r in x["results"]
                        if r["horizon_min"] == 60 and r["excluded_setup"] == "PULLBACK")
        self.assertEqual(pullback["status"], "NOT_OBSERVED")
        self.assertIsNone(pullback["remaining_lift"])

    def test_unpartitioned_or_nonfinite_data_fail_closed(self):
        bad = report()
        bad["dimensions_60m"]["setup"] = bad["dimensions_60m"]["setup"][:1]
        with self.assertRaisesRegex(ValueError, "RCA_GROUP_COUNTS_DO_NOT_PARTITION"):
            audit.evaluate(bad)
        bad = report()
        bad["allowed_60m"] = {"n": 10, "avg_return": float("nan")}
        with self.assertRaisesRegex(ValueError, "RCA_INVALID_MEAN"):
            audit.evaluate(bad)
        bad = report()
        bad["rejected_240m"] = {"n": 20, "avg_return": True}
        with self.assertRaisesRegex(ValueError, "RCA_INVALID_MEAN"):
            audit.evaluate(bad)

    def test_malformed_minimum_and_duplicate_aggregates_are_refused(self):
        for v in (0, 1, -2, True, 2.0):
            with self.subTest(minimum=v), self.assertRaisesRegex(ValueError, "RCA_INVALID_MINIMUM"):
                audit.evaluate(report(), min_remainder=v)
        bad = report()
        bad["dimensions_60m"]["setup"] = bad["dimensions_60m"]["setup"] + [
            dict(bad["dimensions_60m"]["setup"][0])
        ]
        with self.assertRaisesRegex(ValueError, "RCA_INVALID_SETUP_LABEL"):
            audit.evaluate(bad)


if __name__ == "__main__":
    unittest.main()
