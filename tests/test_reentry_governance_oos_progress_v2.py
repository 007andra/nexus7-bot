"""Tests for OOS Progress Board V2 and REENTRY_GOVERNANCE_V2."""
import unittest

from bot import oos_progress_board_v2 as progress
from bot import reentry_governance_v2 as governance


def readiness(*, ready=False):
    return {
        "status": "READY" if ready else "BLOCKED",
        "drawdown": 0.10 if ready else 0.615842012018,
        "configured_limit": 0.17,
        "equity": 20.0 if ready else 8.75830036,
        "max_risk_pct": 0.0025,
        "positions": 0,
        "pending_orders": 0,
    }


def release(*, m1="BLOCKED", m2="BLOCKED", m3="NEGATIVE_EVIDENCE", m4="PASS"):
    return {
        "m1_risk_gate": m1,
        "m2_canonical_pipeline": m2,
        "m3_edge_evidence": m3,
        "m4_runtime_precheck": m4,
    }


def oos(*, status="COLLECTING_PROSPECTIVE_OOS", enrolled=1, obs60=0, obs240=0):
    return {
        "status": status,
        "cohort_id": "CALIBRATION_GENERALIZATION_V1",
        "started_epoch": 1_791_211_311.675,
        "enrolled_candidates": enrolled,
        "observed_60m": obs60,
        "observed_240m": obs240,
        "allowed_60m": {"n": 0, "avg_return": None},
        "rejected_60m": {"n": 0, "avg_return": None},
        "allowed_240m": {"n": 0, "avg_return": None},
        "rejected_240m": {"n": 0, "avg_return": None},
        "allowed_vs_rejected_mean_lift_60m": None,
        "allowed_vs_rejected_mean_lift_240m": None,
        "sample_complete": status != "COLLECTING_PROSPECTIVE_OOS",
        "performance_criteria_pass": status == "READY_FOR_MANUAL_REVIEW",
        "concentration_criteria_pass": status == "READY_FOR_MANUAL_REVIEW",
        "hypothesis_frozen": True,
        "discovery_sample_excluded": True,
    }


class OOSProgressBoardV2Tests(unittest.TestCase):
    def test_collecting_progress_is_truthful_and_non_authoritative(self):
        row = progress.evaluate(oos(enrolled=10, obs60=4, obs240=1), readiness(), release())
        self.assertEqual(row["phase"], "COLLECTING")
        self.assertEqual(row["enrolled_candidates"], 10)
        self.assertIn("CANDIDATES:40", row["missing_evidence"])
        self.assertIn("OUTCOMES_60M:26", row["missing_evidence"])
        self.assertIn("OUTCOMES_240M:29", row["missing_evidence"])
        self.assertAlmostEqual(row["candidate_progress"], 0.2)
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["promotion_allowed"])
        self.assertTrue(row["candidate_generation_unchanged"])
        self.assertEqual(row["decision_effect"], "NONE")
        self.assertEqual(row["execution_effect"], "NONE")

    def test_ready_oos_is_still_observability_only(self):
        sample = oos(
            status="READY_FOR_MANUAL_REVIEW",
            enrolled=50,
            obs60=30,
            obs240=30,
        )
        row = progress.evaluate(
            sample,
            readiness(ready=True),
            release(m1="PASS", m2="PASS", m3="PROSPECTIVE_OOS_PASS_MANUAL_REVIEW"),
        )
        self.assertEqual(row["phase"], "EVIDENCE_READY_FOR_MANUAL_REVIEW")
        self.assertEqual(row["missing_evidence"], ())
        self.assertEqual(row["composite_progress"], 1.0)
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["automatic_promotion"])

    def test_formatter_restates_authority(self):
        row = progress.evaluate(oos(), readiness(), release())
        line = progress.format_log(row)
        self.assertIn("[OOS_PROGRESS_BOARD_V2]", line)
        self.assertIn("candidate_generation_unchanged=true", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("execution_effect=NONE", line)


class ReentryGovernanceV2Tests(unittest.TestCase):
    def test_current_blocked_state_preserves_historical_risk_ledger(self):
        row = governance.evaluate(readiness(), oos(), release())
        self.assertEqual(row["status"], "DESIGN_ONLY_AWAITING_EVIDENCE")
        self.assertEqual(row["current_account_status"], "HARD_GATE_BLOCKED")
        self.assertTrue(row["current_account_entries_blocked"])
        self.assertFalse(row["current_account_gate_bypass_proposed"])
        self.assertAlmostEqual(
            row["current_account_inherited_single_trade_stop_budget"],
            8.75830036 * 0.0025,
        )
        self.assertTrue(row["historical_loss_ledger_preserved"])
        self.assertTrue(row["historical_hwm_preserved"])
        self.assertTrue(row["lifetime_drawdown_preserved"])
        self.assertTrue(row["current_hard_gate_unchanged"])
        self.assertTrue(row["external_capital_does_not_clear_drawdown"])
        self.assertFalse(row["fund_movement_authorized"])
        self.assertFalse(row["account_creation_authorized"])
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["execution_effect"], "NONE")

    def test_oos_ready_only_reaches_governance_design_review(self):
        sample = oos(
            status="READY_FOR_MANUAL_REVIEW",
            enrolled=50,
            obs60=30,
            obs240=30,
        )
        board = release(
            m1="BLOCKED",
            m2="BLOCKED",
            m3="PROSPECTIVE_OOS_PASS_MANUAL_REVIEW",
            m4="PASS",
        )
        row = governance.evaluate(readiness(), sample, board)
        self.assertEqual(row["status"], "DESIGN_READY_FOR_GOVERNANCE_REVIEW")
        self.assertIn("ABSOLUTE_LOSS_BUDGET_UNSET", row["blockers"])
        self.assertIn("DISTINCT_PILOT_LEDGER_NOT_PROVEN", row["blockers"])
        self.assertIn("INDEPENDENT_CAPITAL_PROOF_NOT_PROVEN", row["blockers"])
        self.assertIn("EXPLICIT_LIVE_AUTHORIZATION_NOT_GRANTED", row["blockers"])
        self.assertFalse(row["current_account_gate_bypass_proposed"])
        self.assertIsNone(row["segregated_pilot"]["absolute_loss_budget_usdt"])
        self.assertEqual(row["segregated_pilot"]["max_positions"], 1)
        self.assertEqual(row["segregated_pilot"]["max_new_submissions"], 1)
        self.assertFalse(row["segregated_pilot"]["averaging_down_allowed"])
        self.assertFalse(row["segregated_pilot"]["martingale_allowed"])
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["promotion_allowed"])

    def test_oos_failure_prevents_advancement(self):
        row = governance.evaluate(
            readiness(),
            oos(status="EVIDENCE_FAIL", enrolled=50, obs60=30, obs240=30),
            release(),
        )
        self.assertEqual(row["status"], "DO_NOT_ADVANCE_EDGE_FAILED")
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["promotion_allowed"])

    def test_formatter_never_implies_execution_authority(self):
        row = governance.evaluate(readiness(), oos(), release())
        line = governance.format_log(row)
        self.assertIn("[REENTRY_GOVERNANCE_V2]", line)
        self.assertIn("historical_loss_ledger_preserved=true", line)
        self.assertIn("current_hard_gate_unchanged=true", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("decision_effect=NONE", line)
        self.assertIn("execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
