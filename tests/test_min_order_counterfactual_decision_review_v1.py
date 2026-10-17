"""Tests for Counterfactual Decision Review V1."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from bot import min_order_counterfactual_decision_review_v1 as review


def candidate(cid, reason, rr, ev):
    return {
        "candidate_id": cid,
        "counterfactual_nexus_v1": {
            "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
            "candidate_id": cid,
            "symbol": "SOLUSDT",
            "setup": "MOMENTUM",
            "regime": "TRENDING_UP",
            "execution_allowed": False,
            "risk_reward": rr,
            "expected_value": ev,
            "reason_category": reason,
            "score_snapshot": {
                "rr_net": rr,
                "ev_pct": ev,
                "fusion_confidence": 60,
            },
            "risk_epoch_traversal_credit": False,
        },
    }


def outcome(cid, ret):
    return {
        "candidate_id": cid,
        "outcome": "OBSERVED",
        "future_return": ret,
        "MFE": max(ret, 0.02),
        "MAE": min(ret, -0.01),
    }


class ReviewTests(unittest.TestCase):
    def test_waits_for_twenty_twenty(self):
        rows = [candidate(f"c{i}", "RR_BELOW_MIN", 1.5, 0.1) for i in range(12)]
        report = review.build_report(rows, [])
        self.assertEqual(report["status"], "WAIT_FOR_20_20")
        self.assertEqual(report["recommendation"], "WAIT_FOR_20_20")
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertTrue(report["production_thresholds_unchanged"])

    def test_negative_rr_reject_outcomes_discard_relaxation_in_sample(self):
        rows = []
        outcomes = []
        for i in range(10):
            cid = f"rr{i}"
            rows.append(candidate(cid, "RR_BELOW_MIN", 1.5, 0.1))
            outcomes.append(outcome(cid, -0.01))
        for i in range(10):
            cid = f"ev{i}"
            rows.append(candidate(cid, "EV_NEGATIVE", 0.9, -0.1))
            outcomes.append(outcome(cid, -0.02))
        report = review.build_report(rows, outcomes)
        self.assertEqual(report["status"], "EVIDENCE_READY_FOR_MANUAL_REVIEW")
        self.assertEqual(
            report["recommendation"], "DISCARD_RR_RELAXATION_IN_THIS_SAMPLE"
        )
        self.assertTrue(report["manual_review_required"])
        self.assertFalse(report["automatic_promotion"])

    def test_can_only_recommend_study_150_for_manual_review(self):
        rows = []
        outcomes = []
        for i in range(10):
            cid = f"rr{i}"
            rows.append(candidate(cid, "RR_BELOW_MIN", 1.52, 0.2))
            outcomes.append(outcome(cid, 0.02 if i < 7 else -0.01))
        for i in range(10):
            cid = f"ev{i}"
            rows.append(candidate(cid, "EV_NEGATIVE", 0.8, -0.2))
            outcomes.append(outcome(cid, -0.01))
        report = review.build_report(rows, outcomes)
        self.assertEqual(
            report["recommendation"], "STUDY_RR_1_50_MANUAL_REVIEW"
        )
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertTrue(report["production_thresholds_unchanged"])

    def test_flag_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(review.enabled())


class FakeDB:
    async def _fetchall(self, sql, args=()):
        if "risk_epoch_shadow_v1" in sql:
            return [{"payload": json.dumps({"started_epoch": 1500.0})}]
        if "hard_gate_shadow_candidates_v1" in sql and "SELECT payload" in sql:
            return [{"payload": json.dumps(candidate("db1", "RR_BELOW_MIN", 1.5, 0.1))}]
        if "hard_gate_shadow_outcomes_v1" in sql:
            return [{
                "candidate_id": "db1",
                "payload": json.dumps({
                    "outcome": "OBSERVED",
                    "future_return": 0.02,
                    "MFE": 0.03,
                    "MAE": -0.01,
                }),
            }]
        raise AssertionError(sql)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_active_epoch_only(self):
        with patch.dict(os.environ, {
            "RISK_EPOCH_SHADOW_ID": "REENTRY_V1_20261004_R2",
        }):
            report = asyncio.run(review.snapshot(FakeDB()))
        self.assertEqual(report["epoch_id"], "REENTRY_V1_20261004_R2")
        self.assertEqual(report["evaluated"], 1)
        self.assertEqual(report["observed_60m"], 1)

    def test_summary_restates_manual_only_authority(self):
        report = review.build_report([], [])
        line = review.format_summary(report)
        self.assertIn("manual_review_required=true", line)
        self.assertIn("production_thresholds_unchanged=true", line)
        self.assertIn("automatic_promotion=false", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("live_allowed=false", line)


if __name__ == "__main__":
    unittest.main()
