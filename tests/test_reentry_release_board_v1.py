"""Tests for REENTRY_RELEASE_BOARD_V1."""
import unittest

from bot import reentry_release_board_v1 as board


def readiness(*, ready=False, preflight=True):
    return {
        "status": "READY" if ready else "BLOCKED",
        "blockers": () if ready else ("DRAWDOWN_ABOVE_LIMIT",),
        "drawdown": 0.10 if ready else 0.6158,
        "configured_limit": 0.17,
        "equity": 20.0 if ready else 8.7583,
        "required_equity_for_limit": 18.9229,
        "equity_gap_to_limit": 0.0 if ready else 10.1646,
        "external_capital_flow_preserves_drawdown": True,
        "recovery_authorized": False,
        "override": False,
        "preflight_ready": preflight,
        "positions": 0,
        "pending_orders": 0,
    }


def epoch(*, complete=False):
    return {
        "traversed_to_nexus": 20 if complete else 0,
        "observed_60m": 20 if complete else 0,
    }


def oos(*, ready=False):
    return {
        "status": "READY_FOR_MANUAL_REVIEW" if ready else "COLLECTING_PROSPECTIVE_OOS",
        "enrolled_candidates": 50 if ready else 0,
        "observed_60m": 30 if ready else 0,
        "observed_240m": 30 if ready else 0,
    }


def validation(*, complete=True, positive=False):
    return {
        "evaluated": 40 if complete else 5,
        "observed_60m": 35 if complete else 3,
        "observed_240m": 30 if complete else 1,
        "allowed": {
            "n": 12 if complete else 1,
            "avg_return": 0.004 if positive else -0.008,
            "positive_rate": 0.58 if positive else 0.22,
        },
        "horizon_240m": {
            "allowed": {
                "n": 10 if complete else 1,
                "avg_return": 0.006 if positive else -0.011,
                "positive_rate": 0.60 if positive else 0.25,
            }
        },
    }


class ReleaseBoardTests(unittest.TestCase):
    def test_current_like_state_is_blocked_fail_closed(self):
        row = board.evaluate(
            readiness(),
            epoch(),
            validation(),
            {"recommendation": "DISCARD_RR_RELAXATION_IN_THIS_SAMPLE"},
        )
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIn("M1_RISK_GATE", row["blockers"])
        self.assertIn("M2_CANONICAL_PIPELINE", row["blockers"])
        self.assertIn("M3_EDGE_EVIDENCE", row["blockers"])
        self.assertNotIn("M4_RUNTIME_PRECHECK", row["blockers"])
        self.assertEqual(row["m4_runtime_precheck"], "PASS")
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["automatic_promotion"])
        self.assertEqual(row["decision_effect"], "NONE")
        self.assertEqual(row["execution_effect"], "NONE")

    def test_all_milestones_only_reach_manual_review(self):
        row = board.evaluate(
            readiness(ready=True),
            epoch(complete=True),
            validation(positive=True),
            {"recommendation": "KEEP_RR_1_60_PENDING_MORE_EVIDENCE"},
            prospective_oos=oos(ready=True),
        )
        self.assertEqual(row["status"], "MANUAL_REVIEW_READY")
        self.assertEqual(row["blockers"], ())
        self.assertEqual(row["m1_risk_gate"], "PASS")
        self.assertEqual(row["m2_canonical_pipeline"], "PASS")
        self.assertEqual(
            row["m3_edge_evidence"], "PROSPECTIVE_OOS_PASS_MANUAL_REVIEW"
        )
        self.assertEqual(row["m4_runtime_precheck"], "PASS")
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["promotion_allowed"])
        self.assertTrue(row["explicit_live_authorization_required"])


    def test_historical_evidence_alone_cannot_clear_m3(self):
        row = board.evaluate(
            readiness(ready=True),
            epoch(complete=True),
            validation(positive=True),
            {"recommendation": "KEEP_RR_1_60_PENDING_MORE_EVIDENCE"},
            prospective_oos=oos(ready=False),
        )
        self.assertEqual(row["status"], "BLOCKED")
        self.assertEqual(row["m3_edge_evidence"], "HISTORICAL_MANUAL_REVIEW_ONLY")
        self.assertIn("M3_EDGE_EVIDENCE", row["blockers"])
        self.assertFalse(row["live_allowed"])

    def test_runtime_preflight_failure_blocks_m4(self):
        row = board.evaluate(
            readiness(ready=True, preflight=False),
            epoch(complete=True),
            validation(positive=True),
            {"recommendation": "STUDY_RR_1_50_MANUAL_REVIEW"},
        )
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIn("M4_RUNTIME_PRECHECK", row["blockers"])
        self.assertEqual(row["m4_runtime_precheck"], "BLOCKED")

    def test_discard_recommendation_never_counts_as_edge(self):
        row = board.evaluate(
            readiness(ready=True),
            epoch(complete=True),
            validation(positive=True),
            {"recommendation": "DISCARD_RR_RELAXATION_IN_THIS_SAMPLE"},
        )
        self.assertEqual(row["m3_edge_evidence"], "NEGATIVE_EVIDENCE")
        self.assertIn("M3_EDGE_EVIDENCE", row["blockers"])
        self.assertFalse(row["live_allowed"])

    def test_formatter_restates_authority_boundaries(self):
        row = board.evaluate(
            readiness(),
            epoch(),
            validation(),
            {"recommendation": "DISCARD_RR_RELAXATION_IN_THIS_SAMPLE"},
        )
        line = board.format_log(row)
        self.assertIn("[REENTRY_RELEASE_BOARD_V1]", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("automatic_promotion=false", line)
        self.assertIn("decision_effect=NONE", line)
        self.assertIn("execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
