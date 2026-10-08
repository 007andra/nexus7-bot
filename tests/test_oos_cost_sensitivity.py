"""Fail-closed cost sensitivity: scenarios never actual execution evidence."""
import unittest

from research.oos_rca_v1.cost_sensitivity import analyze, APP, REJ


def row(cid, decision, value, h=240, date=1791211515.0, sym="BTCUSDT"):
    return {"candidate_id":cid, "cohort_decision":decision,
            "return_gross_reference":value, "horizon":h,
            "captured_epoch":date, "symbol":sym, "side":"SHORT"}


class SensitivityTests(unittest.TestCase):
    def test_zero_cost_equal_gross_not_actual(self):
        r=analyze([row("A",APP,-.003),row("R",REJ,.001)])
        self.assertFalse(r["realized_pnl_proven"])
        self.assertFalse(r["live_allowed"])
        self.assertEqual(r["horizons"]["240"][APP]["cost_sensitivity"]["0"]["hypothetical_mean_ex_funding"],-.003)

    def test_cost_deducted_once_and_negative_keeps_negative(self):
        r=analyze([row("A",APP,-.003)])
        self.assertAlmostEqual(r["horizons"]["240"][APP]["cost_sensitivity"]["15"]["hypothetical_mean_ex_funding"],-.0045)

    def test_stratified_same_symbol_side_day(self):
        r=analyze([row("A",APP,-.003),row("R",REJ,.001)])
        x=r["matched_strata"]["240"]
        self.assertEqual((x["matched_symbol_side_day_strata"],x["approved_covered"]), (1,1))
        self.assertAlmostEqual(x["approved_weighted_mean_lift_fraction"],-.004)

    def test_missing_or_different_symbol_not_mixed(self):
        r=analyze([row("A",APP,None),row("B",APP,-.003),row("R",REJ,.001,sym="ETHUSDT")])
        self.assertEqual(r["matched_strata"]["240"]["approved_unmatched"],1)
        self.assertIsNone(r["matched_strata"]["240"]["approved_weighted_mean_lift_fraction"])

    def test_duplicate_candidate_horizon_fails(self):
        with self.assertRaisesRegex(ValueError,"DUPLICATE_CANDIDATE_HORIZON"):
            analyze([row("A",APP,-.003),row("A",APP,.001)])

    def test_null_horizon_emits_none(self):
        r=analyze([row("A",APP,None)])
        self.assertIsNone(r["horizons"]["240"][APP]["mean_gross_fraction"])
        self.assertIsNone(r["horizons"]["240"][APP]["cost_sensitivity"]["50"]["hypothetical_mean_ex_funding"])


if __name__=="__main__":
    unittest.main()
