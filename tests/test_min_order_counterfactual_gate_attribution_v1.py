"""Tests for counterfactual NEXUS gate attribution V1."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from bot import min_order_counterfactual_gate_attribution_v1 as attribution


def payload(cid, *, reason, rr, ev, allowed=False, symbol="SOLUSDT"):
    return {
        "candidate_id": cid,
        "counterfactual_nexus_v1": {
            "cohort": attribution.COHORT,
            "candidate_id": cid,
            "symbol": symbol,
            "setup": "MOMENTUM",
            "regime": "TRENDING_UP",
            "execution_allowed": allowed,
            "risk_reward": rr,
            "expected_value": ev,
            "reason": reason,
            "reason_category": (
                "EV_NEGATIVE" if "EV negativo" in reason
                else "RR_BELOW_MIN"
            ),
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


class AttributionTests(unittest.TestCase):
    def test_separates_ev_negative_from_positive_ev_rr_veto(self):
        rows = [
            payload(
                "ev1",
                reason="EV negativo após custos: -0.18% (R:R líquido 0.82)",
                rr=0.82,
                ev=-0.18,
            ),
            payload(
                "rr1",
                reason="R:R líquido 1.36 < mínimo líquido 1.60",
                rr=1.36,
                ev=0.22,
            ),
            payload(
                "rr2",
                reason="R:R líquido 1.55 < mínimo líquido 1.60",
                rr=1.55,
                ev=0.08,
            ),
        ]
        with patch.dict(os.environ, {"NEXUS_MIN_RR_NET": "1.60"}):
            report = attribution.build_report(rows)
        self.assertEqual(report["evaluated"], 3)
        self.assertEqual(report["rejected"], 3)
        self.assertEqual(report["ev_negative_rejected"], 1)
        self.assertEqual(report["rr_below_min_positive_ev_rejected"], 2)
        self.assertAlmostEqual(
            report["rr_positive_ev_shortfall"]["median"], 0.145
        )
        self.assertAlmostEqual(
            report["rr_positive_ev_shortfall"]["min"], 0.05
        )
        self.assertEqual(report["closest_rr_rejects"][0]["candidate_id"], "rr2")
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["risk_epoch_traversal_credit"])
        self.assertTrue(report["thresholds_unchanged"])

    def test_attribution_does_not_claim_threshold_change(self):
        rows = [
            payload(
                "rr1",
                reason="R:R líquido 1.59 < mínimo líquido 1.60",
                rr=1.59,
                ev=0.10,
            )
        ]
        with patch.dict(os.environ, {"NEXUS_MIN_RR_NET": "1.60"}):
            report = attribution.build_report(rows)
        self.assertEqual(
            report["interpretation_guard"],
            "DISTANCE_TO_THRESHOLD_IS_DIAGNOSTIC_ONLY_NOT_THRESHOLD_CHANGE_EVIDENCE",
        )
        self.assertFalse(report["automatic_promotion"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")

    def test_outcomes_are_grouped_by_original_veto_reason(self):
        rows = [
            payload(
                "ev1",
                reason="EV negativo após custos",
                rr=0.8,
                ev=-0.2,
            ),
            payload(
                "rr1",
                reason="R:R líquido 1.4 < mínimo líquido 1.60",
                rr=1.4,
                ev=0.1,
            ),
        ]
        outcomes = [outcome("ev1", -0.03), outcome("rr1", 0.04)]
        with patch.dict(os.environ, {"NEXUS_MIN_RR_NET": "1.60"}):
            report = attribution.build_report(rows, outcomes)
        by_reason = {r["reason"]: r for r in report["outcome_reason_rows"]}
        self.assertEqual(report["observed_60m"], 2)
        self.assertAlmostEqual(by_reason["EV_NEGATIVE"]["avg_return"], -0.03)
        self.assertAlmostEqual(by_reason["RR_BELOW_MIN"]["avg_return"], 0.04)
        self.assertAlmostEqual(
            by_reason["RR_BELOW_MIN"]["positive_rate"], 1.0
        )

    def test_flag_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(attribution.enabled())


class FakeDB:
    async def _fetchall(self, sql, args=()):
        if "risk_epoch_shadow_v1" in sql:
            return [{"payload": json.dumps({"started_epoch": 1500.0})}]
        if "hard_gate_shadow_candidates_v1" in sql and "SELECT payload" in sql:
            self.candidate_args = args
            return [{
                "payload": json.dumps(payload(
                    "db1",
                    reason="R:R líquido 1.5 < mínimo líquido 1.60",
                    rr=1.5,
                    ev=0.1,
                ))
            }]
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
    def test_snapshot_active_epoch_and_60m_only(self):
        db = FakeDB()
        with patch.dict(os.environ, {
            "RISK_EPOCH_SHADOW_ID": "REENTRY_V1_20261004_R2",
            "NEXUS_MIN_RR_NET": "1.60",
        }):
            report = asyncio.run(attribution.snapshot(db))
        self.assertEqual(report["epoch_id"], "REENTRY_V1_20261004_R2")
        self.assertEqual(report["evaluated"], 1)
        self.assertEqual(report["observed_60m"], 1)
        self.assertEqual(db.candidate_args, ("HARD_GATE_SHADOW", 1500.0))
        self.assertEqual(db.outcome_args, ("HARD_GATE_SHADOW", 1500.0, 60))

    def test_summary_restates_diagnostic_only_authority(self):
        with patch.dict(os.environ, {"NEXUS_MIN_RR_NET": "1.60"}):
            report = attribution.build_report([
                payload(
                    "rr1",
                    reason="R:R líquido 1.4 < mínimo líquido 1.60",
                    rr=1.4,
                    ev=0.1,
                )
            ])
        line = attribution.format_summary(report)
        self.assertIn("diagnostic_only=true", line)
        self.assertIn("thresholds_unchanged=true", line)
        self.assertIn("risk_epoch_traversal_credit=false", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("live_allowed=false", line)


if __name__ == "__main__":
    unittest.main()
