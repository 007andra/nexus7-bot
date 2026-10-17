import unittest

from bot import reentry_promotion_study as study


def readiness(**overrides):
    row = {
        "blockers": ("DRAWDOWN_ABOVE_LIMIT",),
        "max_risk_pct": 0.0025,
        "margin_fraction": 0.25,
        "max_positions": 1,
        "pilot_max_positions": 1,
        "pilot_max_submissions": 1,
        "recovery_authorized": False,
        "override": False,
        "preflight_ready": True,
        "positions": 0,
        "pending_orders": 0,
    }
    row.update(overrides)
    return row


class ReentryPromotionStudyTests(unittest.TestCase):
    def test_nineteen_candidates_remains_collecting(self):
        epoch = {
            "status": "COLLECTING",
            "traversed_to_nexus": 19,
            "observed_60m": 19,
            "nexus_allowed": 4,
        }
        row = study.evaluate(epoch, readiness(), target_pipeline=20)
        self.assertEqual(row["status"], "COLLECTING_OR_BLOCKED")
        self.assertIn("MARKET_PIPELINE_SAMPLE_INCOMPLETE", row["blockers"])
        self.assertIn("OUTCOME_60M_SAMPLE_INCOMPLETE", row["blockers"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["automatic_promotion"])
        self.assertEqual(row["execution_effect"], "NONE")

    def test_twenty_candidates_only_allows_manual_review_not_live(self):
        epoch = {
            "status": "SAMPLE_COMPLETE_RESEARCH_ONLY",
            "traversed_to_nexus": 20,
            "observed_60m": 20,
            "nexus_allowed": 5,
        }
        row = study.evaluate(epoch, readiness(), target_pipeline=20)
        self.assertEqual(row["status"], "EVIDENCE_SUFFICIENT_FOR_MANUAL_REVIEW")
        self.assertEqual(row["blockers"], ())
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])
        self.assertTrue(row["manual_loss_acceptance_required"])
        self.assertTrue(row["explicit_live_authorization_required"])
        self.assertEqual(
            row["execution_proof_status"],
            "REQUIRES_CURRENT_SHA_CI_AND_RUNTIME_REVIEW",
        )

    def test_any_extra_runtime_blocker_prevents_review_readiness(self):
        epoch = {
            "status": "SAMPLE_COMPLETE_RESEARCH_ONLY",
            "traversed_to_nexus": 20,
            "observed_60m": 20,
            "nexus_allowed": 5,
        }
        row = study.evaluate(
            epoch,
            readiness(blockers=("DRAWDOWN_ABOVE_LIMIT", "PREFLIGHT_NOT_READY")),
            target_pipeline=20,
        )
        self.assertEqual(row["status"], "COLLECTING_OR_BLOCKED")
        self.assertTrue(
            any("RUNTIME_READINESS_BLOCKERS:PREFLIGHT_NOT_READY" == b for b in row["blockers"])
        )
        self.assertFalse(row["live_allowed"])

    def test_cleared_lifetime_gate_is_not_an_automatic_promotion_signal(self):
        epoch = {
            "status": "SAMPLE_COMPLETE_RESEARCH_ONLY",
            "traversed_to_nexus": 20,
            "observed_60m": 20,
            "nexus_allowed": 5,
        }
        row = study.evaluate(epoch, readiness(blockers=()), target_pipeline=20)
        self.assertIn("LIFETIME_DRAWDOWN_GATE_NOT_PRESENT", row["blockers"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])


if __name__ == "__main__":
    unittest.main()
