"""Tests for counterfactual threshold sensitivity V1."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from bot import min_order_counterfactual_threshold_sensitivity_v1 as sensitivity


def payload(cid, rr, ev, reason="RR_BELOW_MIN", allowed=False):
    return {
        "candidate_id": cid,
        "counterfactual_nexus_v1": {
            "cohort": sensitivity.COHORT,
            "candidate_id": cid,
            "symbol": "SOLUSDT",
            "setup": "MOMENTUM",
            "regime": "TRENDING_UP",
            "execution_allowed": allowed,
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


def outcome(cid, ret, mfe=0.03, mae=-0.01):
    return {
        "candidate_id": cid,
        "outcome": "OBSERVED",
        "future_return": ret,
        "MFE": mfe,
        "MAE": mae,
    }


class SensitivityTests(unittest.TestCase):
    def test_rr_scenarios_only_change_rr_floor(self):
        rows = [
            payload("a", 1.62, 0.10),
            payload("b", 1.52, 0.20),
            payload("c", 1.42, 0.30),
            payload("d", 1.32, 0.40),
            payload("evneg", 2.00, -0.10, reason="EV_NEGATIVE"),
        ]
        report = sensitivity.build_report(rows)
        by_rr = {s["rr_floor"]: s for s in report["scenarios"]}
        self.assertEqual(by_rr[1.60]["selected"], 1)
        self.assertEqual(by_rr[1.50]["selected"], 2)
        self.assertEqual(by_rr[1.40]["selected"], 3)
        self.assertEqual(by_rr[1.30]["selected"], 4)
        self.assertTrue(report["production_thresholds_unchanged"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])

    def test_outcomes_are_hypothetical_shadow_only(self):
        rows = [
            payload("a", 1.62, 0.10),
            payload("b", 1.52, 0.20),
            payload("c", 1.42, 0.30),
        ]
        outcomes = [
            outcome("a", 0.02),
            outcome("b", -0.01),
            outcome("c", 0.03),
        ]
        report = sensitivity.build_report(rows, outcomes)
        by_rr = {s["rr_floor"]: s for s in report["scenarios"]}
        self.assertEqual(by_rr[1.60]["observed_60m"], 1)
        self.assertAlmostEqual(by_rr[1.60]["avg_return"], 0.02)
        self.assertEqual(by_rr[1.50]["observed_60m"], 2)
        self.assertAlmostEqual(by_rr[1.50]["avg_return"], 0.005)
        self.assertEqual(by_rr[1.40]["observed_60m"], 3)
        self.assertAlmostEqual(by_rr[1.40]["avg_return"], 0.013333333333333334)
        self.assertFalse(by_rr[1.40]["statistical_claims_allowed"])
        self.assertEqual(
            report["interpretation_guard"],
            "SHADOW_SELECTION_SENSITIVITY_IS_NOT_EVIDENCE_TO_CHANGE_LIVE_THRESHOLD",
        )

    def test_non_rr_veto_never_enters_lower_rr_scenario(self):
        rows = [
            payload("ev", 1.55, -0.1, reason="EV_NEGATIVE"),
            payload("other", 1.55, 0.2, reason="OTHER"),
        ]
        report = sensitivity.build_report(rows)
        self.assertTrue(all(s["selected"] == 0 for s in report["scenarios"]))

    def test_flag_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sensitivity.enabled())


class FakeDB:
    async def _fetchall(self, sql, args=()):
        if "risk_epoch_shadow_v1" in sql:
            return [{"payload": json.dumps({"started_epoch": 1500.0})}]
        if "hard_gate_shadow_candidates_v1" in sql and "SELECT payload" in sql:
            self.candidate_args = args
            return [{"payload": json.dumps(payload("db1", 1.5, 0.1))}]
        if "hard_gate_shadow_outcomes_v1" in sql:
            self.outcome_args = args
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
    def test_active_epoch_and_60m_only(self):
        db = FakeDB()
        with patch.dict(os.environ, {
            "RISK_EPOCH_SHADOW_ID": "REENTRY_V1_20261004_R2",
        }):
            report = asyncio.run(sensitivity.snapshot(db))
        self.assertEqual(report["epoch_id"], "REENTRY_V1_20261004_R2")
        self.assertEqual(report["evaluated"], 1)
        self.assertEqual(db.candidate_args, ("HARD_GATE_SHADOW", 1500.0))
        self.assertEqual(db.outcome_args, ("HARD_GATE_SHADOW", 1500.0, 60))

    def test_summary_restates_no_authority(self):
        report = sensitivity.build_report([payload("x", 1.5, 0.1)])
        line = sensitivity.format_summary(report)
        self.assertIn("production_thresholds_unchanged=true", line)
        self.assertIn("risk_epoch_traversal_credit=false", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("decision_effect=NONE execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
