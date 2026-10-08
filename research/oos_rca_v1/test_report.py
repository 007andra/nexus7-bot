import unittest
from parse_logs import parse_record
from report import audit

APPROVED = {
    "record_type": "approval", "candidate_id": "A", "captured_epoch": 1000,
    "approval_state": "NATURAL_COUNTERFACTUAL_NEXUS_APPROVED"
}
OBSERVED = {
    "record_type": "outcome", "candidate_id": "A", "horizon": 60,
    "outcome": "OBSERVED", "verified": True, "future_return": 0.02
}
COST = {
    "record_type": "cost", "candidate_id": "A", "bbo_valid": True,
    "bbo_age_ms": 20, "live_total_cost_bps": 14, "event_epoch": 1020
}

class ResearchTests(unittest.TestCase):
    def test_real_log_marker_and_secret_drop(self):
        raw = {
            "timestamp": "2026-10-08T16:00:00Z",
            "message": "[COST_SHADOW_BBO_V3_COST_ONLY] candidate_id=A "
                       "bbo_age_ms=8 live_total_cost_bps=14 bbo_valid=true private_key=secret",
        }
        row = parse_record(raw)
        self.assertEqual(row["record_type"], "cost")
        self.assertNotIn("private_key", row)
        self.assertIsNotNone(row["event_epoch"])

    def test_outcome_and_aligned_bbo(self):
        rows, _ = audit([APPROVED, OBSERVED, COST], 7000)
        self.assertAlmostEqual(rows[0]["illustrative_net_return"], 0.0186)
        self.assertEqual(rows[0]["cost_status"], "ALIGNED_ESTIMATE_NOT_FILL")

    def test_misaligned_cost_cannot_net(self):
        rows, _ = audit([APPROVED, OBSERVED, {**COST, "event_epoch": 9000}], 7000)
        self.assertIsNone(rows[0]["illustrative_net_return"])

    def test_unverified_approval_return_not_counted(self):
        rows, _ = audit([{**APPROVED, "return60": "0.8"}], 7000)
        self.assertFalse(rows[0]["verified"])
        self.assertEqual(rows[0]["missing_reason"], "MATURED_UNVERIFIED")

    def test_not_matured(self):
        rows, _ = audit([APPROVED], 1010)
        self.assertEqual(rows[0]["missing_reason"], "NOT_MATURED")

    def test_missing_not_zero(self):
        rows, summary = audit([APPROVED], 7000)
        self.assertIsNone(rows[0]["gross_return"])
        self.assertIsNone(summary["horizons"]["60"]["mean_verified_gross"])

    def test_later_unknown_does_not_replace_verified(self):
        later_unknown = {**OBSERVED, "verified": False, "outcome": "OUTCOME_NOT_PROVEN"}
        rows, _ = audit([APPROVED, OBSERVED, later_unknown], 7000)
        self.assertTrue(rows[0]["verified"])

    def test_rejected_distinct_from_not_called(self):
        rejected = {"record_type": "terminal", "candidate_id": "B",
                    "nexus_called": True, "nexus_allowed": False}
        not_called = {"record_type": "terminal", "candidate_id": "C",
                      "nexus_called": False, "nexus_allowed": False}
        rows, _ = audit([rejected, not_called], 7000)
        self.assertEqual(rows[0]["decision"], "NEXUS_REJECTED_TERMINAL")
        self.assertEqual(rows[2]["decision"], "NOT_CALLED_IN_TERMINAL")

    def test_cache_gap(self):
        gap = {"record_type": "outcome", "candidate_id": "A", "horizon": 60,
               "outcome": "UNKNOWN_CACHE_GAP", "verified": False}
        rows, _ = audit([APPROVED, gap], 7000)
        self.assertEqual(rows[0]["missing_reason"], "UNKNOWN_CACHE_GAP")

    def test_real_outcome_marker(self):
        row = parse_record({
            "message": "[SHORT_DOWN_BOS_V2_MEMBER_PROOF] candidate_id=ABC "
                       "horizon=60 outcome=OBSERVED verified=true "
                       "future_return=0.0100 observation_start=1000"
        })
        self.assertEqual(row["horizon"], "60")
        self.assertEqual(row["record_type"], "outcome")

if __name__ == "__main__":
    unittest.main()
