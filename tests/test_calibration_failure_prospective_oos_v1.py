"""Tests for calibration failure analysis and prospective OOS V1."""
import unittest

from bot import calibration_failure_analysis_v1 as calibration
from bot import prospective_oos_cohort_v1 as oos


def candidate(
    cid,
    *,
    allowed,
    score,
    confidence,
    rr,
    ev,
    side="LONG",
    regime="TRENDING_UP",
    setup="BOS_BREAK",
):
    return {
        "candidate_id": cid,
        "captured_epoch": 1_800_000_000.0,
        "symbol": "BTCUSDT",
        "side": side,
        "setup": setup,
        "regime": regime,
        "score": score,
        "entry": 100.0,
        "stop": 99.0,
        "cost_snapshot": {
            "taker_fee": 0.0005,
            "entry_slippage": 0.0004,
            "exit_slippage": 0.0004,
        },
        "counterfactual_nexus_v1": {
            "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
            "candidate_id": cid,
            "symbol": "BTCUSDT",
            "side": side,
            "setup": setup,
            "regime": regime,
            "execution_allowed": allowed,
            "confidence": confidence,
            "setup_quality": confidence,
            "risk_reward": rr,
            "expected_value": ev,
            "score_snapshot": {
                "fusion_confidence": confidence,
                "rr_net": rr,
                "ev_pct": ev,
            },
            "reason_category": "OTHER" if allowed else "RR_BELOW_MIN",
            "risk_epoch_traversal_credit": False,
        },
    }


def outcome(cid, ret, *, mae=-0.01, mfe=0.02):
    return {
        "candidate_id": cid,
        "outcome": "OBSERVED",
        "future_return": ret,
        "MFE": mfe,
        "MAE": mae,
    }


def baseline():
    return {
        "cohort_id": oos.COHORT_ID,
        "started_epoch": 1_800_000_100.0,
        "discovery_cutoff_epoch": 1_800_000_100.0,
        "hypothesis": oos.FROZEN_HYPOTHESIS,
        "hypothesis_frozen": True,
        "reset_allowed": False,
    }


class CalibrationFailureTests(unittest.TestCase):
    def test_fixed_bins_detect_inversion_both_horizons(self):
        payloads = []
        out60 = []
        out240 = []
        for i in range(10):
            cid = f"L{i}"
            payloads.append(candidate(
                cid, allowed=False, score=62, confidence=35, rr=1.4, ev=0.1
            ))
            out60.append(outcome(cid, 0.010))
            out240.append(outcome(cid, 0.012))
        for i in range(10):
            cid = f"H{i}"
            payloads.append(candidate(
                cid, allowed=True, score=78, confidence=80, rr=2.2, ev=1.2
            ))
            out60.append(outcome(cid, -0.010))
            out240.append(outcome(cid, -0.015))

        report = calibration.build_report(payloads, out60, out240)
        self.assertEqual(
            report["status"], "CALIBRATION_INVERSION_CONFIRMED_BOTH_HORIZONS"
        )
        self.assertGreater(report["inversions_60m"], 0)
        self.assertGreater(report["inversions_240m"], 0)
        self.assertTrue(report["prospective_hypothesis_frozen"])
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["risk_epoch_traversal_credit"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")

    def test_small_sample_is_not_overclaimed(self):
        payloads = [candidate(
            "A", allowed=True, score=80, confidence=80, rr=2.0, ev=1.0
        )]
        out = [outcome("A", -0.01)]
        report = calibration.build_report(payloads, out, out)
        self.assertEqual(report["status"], "INSUFFICIENT_MATCHED_EVIDENCE")
        self.assertFalse(report["live_allowed"])


class ProspectiveOOSTests(unittest.TestCase):
    def _sample(self, *, concentrated=False):
        payloads = []
        out60 = []
        out240 = []
        for i in range(10):
            if concentrated or i < 5:
                side, regime, setup = "LONG", "TRENDING_UP", "BOS_BREAK"
            else:
                side, regime, setup = "SHORT", "TRENDING_DOWN", "MOMENTUM"
            cid = f"A{i}"
            payloads.append(candidate(
                cid, allowed=True, score=75, confidence=70, rr=1.9, ev=0.8,
                side=side, regime=regime, setup=setup,
            ))
            out60.append(outcome(cid, 0.010, mae=-0.005))
            out240.append(outcome(cid, 0.015, mae=-0.006))

        for i in range(40):
            cid = f"R{i}"
            payloads.append(candidate(
                cid, allowed=False, score=65, confidence=40, rr=1.4, ev=0.1,
                side="LONG" if i % 2 == 0 else "SHORT",
                regime="TRENDING_UP" if i % 2 == 0 else "RANGING",
                setup="MOMENTUM",
            ))
            ret60 = 0.004 if i % 2 == 0 else -0.004
            ret240 = 0.005 if i % 2 == 0 else -0.005
            out60.append(outcome(cid, ret60, mae=-0.020))
            out240.append(outcome(cid, ret240, mae=-0.022))
        return payloads, out60, out240

    def test_complete_positive_oos_is_manual_review_only(self):
        payloads, out60, out240 = self._sample()
        report = oos.build_report(payloads, out60, out240, baseline=baseline())
        self.assertEqual(report["status"], "READY_FOR_MANUAL_REVIEW")
        self.assertTrue(report["sample_complete"])
        self.assertTrue(report["performance_criteria_pass"])
        self.assertTrue(report["concentration_criteria_pass"])
        self.assertGreater(report["allowed_vs_rejected_mean_lift_60m"], 0)
        self.assertGreater(report["allowed_vs_rejected_mean_lift_240m"], 0)
        self.assertTrue(report["hypothesis_frozen"])
        self.assertFalse(report["reset_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["automatic_promotion"])
        self.assertFalse(report["risk_epoch_traversal_credit"])
        self.assertTrue(report["explicit_live_authorization_required"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")

    def test_concentrated_approvals_fail_even_with_positive_returns(self):
        payloads, out60, out240 = self._sample(concentrated=True)
        report = oos.build_report(payloads, out60, out240, baseline=baseline())
        self.assertEqual(report["status"], "EVIDENCE_FAIL")
        self.assertIn("CONCENTRATION_CRITERIA", report["blockers"])
        self.assertFalse(report["concentration_criteria_pass"])
        self.assertFalse(report["live_allowed"])

    def test_incomplete_oos_remains_collecting(self):
        payloads = [candidate(
            "A", allowed=True, score=75, confidence=70, rr=1.9, ev=0.8
        )]
        report = oos.build_report(payloads, [], [], baseline=baseline())
        self.assertEqual(report["status"], "COLLECTING_PROSPECTIVE_OOS")
        self.assertIn("TARGET_CANDIDATES", report["blockers"])
        self.assertFalse(report["live_allowed"])

    def test_formatter_never_implies_live_permission(self):
        payloads, out60, out240 = self._sample()
        report = oos.build_report(payloads, out60, out240, baseline=baseline())
        line = oos.format_summary(report)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("decision_effect=NONE", line)
        self.assertIn("execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
