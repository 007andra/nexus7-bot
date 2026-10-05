"""Tests for MIN_ORDER Counterfactual Validation V1."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from bot import min_order_counterfactual_validation_v1 as validation


def candidate(cid, allowed):
    return {
        "candidate_id": cid,
        "counterfactual_nexus_v1": {
            "cohort": validation.COHORT,
            "candidate_id": cid,
            "symbol": "SOLUSDT",
            "setup": "BOS_BREAK",
            "regime": "TRENDING_UP",
            "execution_allowed": allowed,
            "risk_reward": 1.8,
            "expected_value": 0.2,
            "required_equity_at_min_qty": 20.0,
            "risk_epoch_traversal_credit": False,
        },
    }


def outcome(cid, ret, mfe=0.03, mae=-0.01):
    return {
        "candidate_id": cid,
        "outcome": "OBSERVED",
        "future_return": ret,
        "MFE": mfe,
        "MAE": mae,
    }


class ValidationMath(unittest.TestCase):
    def test_allowed_vs_rejected_lift_and_confusion(self):
        payloads = [
            candidate("a1", True),
            candidate("a2", True),
            candidate("r1", False),
            candidate("r2", False),
        ]
        outcomes = [
            outcome("a1", 0.04),
            outcome("a2", -0.01),
            outcome("r1", 0.01),
            outcome("r2", -0.03),
        ]
        report = validation.build_report(payloads, outcomes)
        self.assertEqual(report["evaluated"], 4)
        self.assertEqual(report["observed_60m"], 4)
        self.assertEqual(report["allowed"]["n"], 2)
        self.assertEqual(report["rejected"]["n"], 2)
        self.assertAlmostEqual(report["allowed"]["avg_return"], 0.015)
        self.assertAlmostEqual(report["rejected"]["avg_return"], -0.01)
        self.assertAlmostEqual(
            report["allowed_vs_rejected_mean_return_lift"], 0.025
        )
        self.assertAlmostEqual(report["allowed"]["positive_rate"], 0.5)
        self.assertAlmostEqual(report["rejected"]["positive_rate"], 0.5)
        self.assertAlmostEqual(
            report["allowed_vs_rejected_positive_rate_lift"], 0.0
        )
        self.assertEqual(report["confusion"]["true_positive"], 1)
        self.assertEqual(report["confusion"]["false_positive"], 1)
        self.assertEqual(report["confusion"]["false_negative"], 1)
        self.assertEqual(report["confusion"]["true_negative"], 1)
        self.assertAlmostEqual(
            report["confusion"]["precision_on_positive_return"], 0.5
        )
        self.assertAlmostEqual(
            report["confusion"]["positive_capture_rate"], 0.5
        )

    def test_only_observed_complete_outcomes_are_used(self):
        report = validation.build_report(
            [candidate("a", True), candidate("b", False)],
            [
                outcome("a", 0.01),
                {
                    "candidate_id": "b",
                    "outcome": "UNKNOWN_CACHE_GAP",
                    "future_return": None,
                    "MFE": None,
                    "MAE": None,
                },
            ],
        )
        self.assertEqual(report["evaluated"], 2)
        self.assertEqual(report["observed_60m"], 1)
        self.assertEqual(report["unobserved_60m"], 1)

    def test_twenty_twenty_is_still_research_only(self):
        payloads = []
        outcomes = []
        for i in range(20):
            allowed = i % 2 == 0
            cid = f"c{i}"
            payloads.append(candidate(cid, allowed))
            outcomes.append(outcome(cid, 0.01 if i % 3 else -0.01))
        report = validation.build_report(payloads, outcomes)
        self.assertEqual(
            report["status"], "EVIDENCE_SAMPLE_COMPLETE_RESEARCH_ONLY"
        )
        self.assertTrue(report["statistical_claims_allowed"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["risk_epoch_traversal_credit"])
        self.assertFalse(report["automatic_promotion"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")

    def test_group_imbalance_never_promotes(self):
        payloads = [candidate(f"c{i}", True) for i in range(20)]
        outcomes = [outcome(f"c{i}", 0.01) for i in range(20)]
        report = validation.build_report(payloads, outcomes)
        self.assertEqual(
            report["status"], "EVIDENCE_SAMPLE_COMPLETE_GROUP_IMBALANCE"
        )
        self.assertFalse(report["promotion_allowed"])

    def test_flag_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(validation.enabled())


class FakeDB:
    async def _fetchall(self, sql, args=()):
        if "risk_epoch_shadow_v1" in sql:
            return [{"payload": json.dumps({"started_epoch": 1500.0})}]
        if "hard_gate_shadow_candidates_v1" in sql and "SELECT payload" in sql:
            self.candidate_args = args
            return [{"payload": json.dumps(candidate("db1", True))}]
        if "hard_gate_shadow_outcomes_v1" in sql:
            self.outcome_args = args
            return [{
                "candidate_id": "db1",
                "payload": json.dumps({
                    "outcome": "OBSERVED",
                    "future_return": 0.02,
                    "MFE": 0.04,
                    "MAE": -0.01,
                }),
            }]
        raise AssertionError(sql)


class SnapshotIsolation(unittest.TestCase):
    def test_snapshot_uses_active_epoch_and_60m_only(self):
        db = FakeDB()
        with patch.dict(os.environ, {
            "RISK_EPOCH_SHADOW_ID": "REENTRY_V1_20261004_R2",
        }):
            report = asyncio.run(validation.snapshot(db))
        self.assertEqual(report["epoch_id"], "REENTRY_V1_20261004_R2")
        self.assertEqual(report["evaluated"], 1)
        self.assertEqual(report["observed_60m"], 1)
        self.assertEqual(
            db.candidate_args, ("HARD_GATE_SHADOW", 1500.0)
        )
        self.assertEqual(
            db.outcome_args, ("HARD_GATE_SHADOW", 1500.0, 60)
        )

    def test_summary_restates_no_live_or_epoch_authority(self):
        report = validation.build_report(
            [candidate("a", True)],
            [outcome("a", 0.01)],
        )
        line = validation.format_summary(report)
        self.assertIn("all_median_return=", line)
        self.assertIn("all_avg_mfe=", line)
        self.assertIn("all_avg_mae=", line)
        self.assertIn("allowed_median_return=", line)
        self.assertIn("rejected_median_return=", line)
        self.assertIn("risk_epoch_traversal_credit=false", line)
        self.assertIn("automatic_promotion=false", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("decision_effect=NONE execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
