"""No database or exchange dependencies: strict exact-cohort horizon analysis tests."""
import unittest

from research.oos_rca_v1.horizon_diagnostics import report, APPROVED, REJECTED


def record(cid, h, decision=APPROVED, value=0.01, verified=True, symbol="SOLUSDT"):
    return {
        "candidate_id": cid, "horizon": h, "cohort_decision": decision,
        "symbol": symbol, "side": "SHORT", "regime": "TRENDING_DOWN",
        "setup": "BOS_BREAK", "captured_epoch": 1000,
        "verified": verified,
        "future_return_gross": value if verified else None,
        "MAE": -0.01 if verified else None,
        "missing_reason": None if verified else "UNKNOWN_CACHE_GAP",
    }


class HorizonDiagnosticsTests(unittest.TestCase):
    def test_matched_member_delta_not_unpaired_mean(self):
        data = [
            record("a", 60, value=0.01), record("a", 240, value=0.03),
            record("b", 60, value=-0.2), record("b", 240, verified=False),
        ]
        r = report(data)
        p = r["paired_horizons"][APPROVED]
        self.assertEqual(p["both_verified"], 1)
        self.assertAlmostEqual(p["delta_240_minus_60_mean_pp"], 2.0)
        self.assertAlmostEqual(p["mean_60_gross_pct_same_members"], 1)
        self.assertAlmostEqual(p["mean_240_gross_pct_same_members"], 3)
        self.assertEqual(r["by_decision"][APPROVED]["240"]["observed"], 1)

    def test_approved_rejected_are_separate_populations(self):
        data = [
            record("a", 60, APPROVED, -0.02), record("a", 240, APPROVED, -0.05),
            record("b", 60, REJECTED, 0.01), record("b", 240, REJECTED, 0.02),
        ]
        r = report(data)
        self.assertEqual(r["by_decision"][APPROVED]["240"]["mean_gross_pct"], -5)
        self.assertEqual(r["by_decision"][REJECTED]["240"]["mean_gross_pct"], 2)
        self.assertEqual(r["paired_horizons"][APPROVED]["240m_better_count"], 0)
        self.assertEqual(r["paired_horizons"][REJECTED]["240m_better_count"], 1)

    def test_nan_and_missing_never_zero(self):
        data = [record("a", 60, verified=False), record("a", 240, verified=False)]
        r = report(data)
        self.assertIsNone(r["by_decision"][APPROVED]["60"]["mean_gross_pct"])
        self.assertEqual(r["by_decision"][APPROVED]["60"]["missing_reasons"], {"UNKNOWN_CACHE_GAP": 1})
        self.assertIsNone(r["paired_horizons"][APPROVED]["delta_240_minus_60_mean_pp"])

    def test_duplicate_identity_fails(self):
        with self.assertRaisesRegex(ValueError, "DUPLICATE_CANDIDATE_HORIZON"):
            report([record("a", 60), record("a", 60), record("a", 240)])

    def test_inconsistent_side_rejected(self):
        later = record("a", 240)
        later["side"] = "LONG"
        with self.assertRaisesRegex(ValueError, "CANDIDATE_METADATA_CONFLICT"):
            report([record("a", 60), later])

    def test_missing_horizon_row_fails(self):
        with self.assertRaisesRegex(ValueError, "MISSING_HORIZON_ROW"):
            report([record("a", 60)])

    def test_subgroups_are_descriptive_no_promotion(self):
        r = report([record("a", 60), record("a", 240)])
        self.assertFalse(r["promotion_allowed"])
        self.assertFalse(r["live_allowed"])
        self.assertEqual(r["subgroups_240m"]["symbol"]["SOLUSDT"][APPROVED]["observed"], 1)

    def test_zero_return_is_observed_but_not_positive(self):
        r = report([record("a", 60, value=0), record("a", 240, value=0)])
        self.assertEqual(r["by_decision"][APPROVED]["60"]["observed"], 1)
        self.assertEqual(r["by_decision"][APPROVED]["60"]["positive_rate"], 0)
        self.assertEqual(r["paired_horizons"][APPROVED]["equal_count"], 1)


if __name__ == "__main__":
    unittest.main()
