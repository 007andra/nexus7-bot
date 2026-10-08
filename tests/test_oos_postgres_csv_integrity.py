"""Offline regression checks for frozen-cohort PostgreSQL CSV integrity."""
import unittest
from research.oos_rca_v1.verify_export import validate, APPROVED, REJECTED


def sample(cid="HARD_GATE_SHADOW:BTCUSDT:SHORT:BOS_BREAK:1", h=60,
           decision=APPROVED, state="OBSERVED", gain="0.01"):
    return {
        "cohort_id":"CALIBRATION_GENERALIZATION_V1",
        "snapshot_as_of_epoch":"20000", "frozen_started_epoch":"1000",
        "candidate_id":cid, "captured_epoch":"1001", "symbol":"BTCUSDT",
        "side":"SHORT", "regime":"TRENDING_DOWN", "setup":"BOS_BREAK",
        "decision_state":decision, "outcome_horizon_minutes":str(h),
        "outcome_state":state, "return_basis":"hypothetical_entry_gross",
        "hypothetical_gross_return":gain, "hypothetical_mfe":"0.02",
        "hypothetical_mae":"-0.01", "observation_start_epoch":"1800",
        "outcome_identity_ok":"true", "outcome_horizon_ok":"true",
    }


class TestPostgresCsvIntegrity(unittest.TestCase):
    def test_approved_rejected_same_cohort(self):
        cid="HARD_GATE_SHADOW:ETHUSDT:SHORT:BOS_BREAK:1"
        result=validate([sample(),sample(h=240),
                         sample(cid=cid,decision=REJECTED),
                         sample(cid=cid,h=240,decision=REJECTED)])
        self.assertTrue(result["passed_schema_and_identity"])
        self.assertEqual(result["unique_candidates"],2)
        self.assertEqual(result["outcomes_matching_production_validator"]["60"][APPROVED]["n"],1)
        self.assertEqual(result["outcomes_matching_production_validator"]["60"][REJECTED]["n"],1)

    def test_missing_not_imputed_zero(self):
        result=validate([sample(state="UNKNOWN_CACHE_GAP"),
                         sample(h=240,state="MISSING")])
        self.assertIsNone(result["outcomes_matching_production_validator"]["60"][APPROVED]["mean_gross_fraction"])

    def test_duplicate_horizon_fails(self):
        self.assertFalse(validate([sample(),sample(),sample(h=240)])["passed_schema_and_identity"])

    def test_future_outcome_not_observed(self):
        later=sample(h=240)
        later["observation_start_epoch"]="10000"
        result=validate([sample(),later])
        self.assertEqual(result["outcomes_matching_production_validator"]["240"][APPROVED]["n"],0)

    def test_snapshots_are_atomic(self):
        later=sample(h=240);later["snapshot_as_of_epoch"]="21000"
        self.assertFalse(validate([sample(),later])["passed_schema_and_identity"])

    def test_candidate_meta_conflict_fails(self):
        later=sample(h=240);later["side"]="LONG"
        self.assertFalse(validate([sample(),later])["passed_schema_and_identity"])

    def test_outcome_identity_mismatch_fails(self):
        wrong=sample();wrong["outcome_identity_ok"]="false"
        self.assertFalse(validate([wrong,sample(h=240)])["passed_schema_and_identity"])

    def test_zero_is_not_positive(self):
        result=validate([sample(gain="0"),sample(h=240,state="MISSING")])
        self.assertEqual(result["outcomes_matching_production_validator"]["60"][APPROVED]["positive_fraction"],0)

    def test_wrong_cohort_fails(self):
        wrong=sample();wrong["cohort_id"]="OTHER"
        self.assertFalse(validate([wrong,sample(h=240)])["passed_schema_and_identity"])

    def test_missing_gross_basis_flagged(self):
        wrong=sample();wrong["return_basis"]=""
        result=validate([wrong,sample(h=240)])
        self.assertEqual(result["observed_missing_or_different_return_basis"],1)


if __name__=="__main__":
    unittest.main()
