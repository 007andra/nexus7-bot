"""MIN_ORDER Counterfactual NEXUS V1 tests."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from bot import min_order_counterfactual_nexus_v1 as study


def signal():
    return NS(
        candidate_id="HARD_GATE_SHADOW:SOLUSDT:LONG:BOS_BREAK:1",
        symbol="SOLUSDT",
        direction="LONG",
        entry_type="BOS_BREAK",
        regime="TRENDING_UP",
    )


def decision(allowed=True):
    return NS(
        execution_allowed=allowed,
        decision="LONG" if allowed else "WAIT",
        confidence=72,
        setup_quality=68,
        risk_reward=1.8,
        expected_value=0.14,
        reasoning=["counterfactual result"],
        _bgx_score_snapshot={
            "fusion_confidence": 72,
            "rr_net": 1.8,
            "ev_pct": 0.14,
        },
    )


def minimum_order():
    return {
        "required_equity_at_min_qty": 14.0,
        "counterfactual_risk_pct": 0.0025,
    }


def payload(cid="c1", allowed=True):
    obs = study.build_observation(
        NS(
            candidate_id=cid, symbol="SOLUSDT", direction="LONG",
            entry_type="BOS_BREAK", regime="TRENDING_UP",
        ),
        decision(allowed),
        minimum_order(),
        captured_epoch=2000,
    )
    return {"candidate_id": cid, "counterfactual_nexus_v1": obs}


class ObservationTests(unittest.TestCase):
    def test_observation_has_zero_canonical_or_epoch_authority(self):
        row = study.build_observation(
            signal(), decision(True), minimum_order(), captured_epoch=2000
        )
        self.assertTrue(row["execution_allowed"])
        self.assertFalse(row["canonical_nexus_called"])
        self.assertFalse(row["canonical_nexus_allowed"])
        self.assertFalse(row["risk_epoch_traversal_credit"])
        self.assertFalse(row["promotion_allowed"])
        self.assertFalse(row["live_allowed"])
        self.assertEqual(row["decision_effect"], "NONE")
        self.assertEqual(row["execution_effect"], "NONE")

    def test_prefinal_snapshot_overrides_zeroed_post_veto_metrics(self):
        vetoed = NS(
            execution_allowed=False,
            decision="WAIT",
            confidence=0.0,
            setup_quality=0.0,
            risk_reward=0.0,
            expected_value=0.0,
            reasoning=[
                "R:R líquido 1.05 < mínimo líquido 1.60 (bruto exigido: 2.0)"
            ],
            _bgx_score_snapshot={
                "fusion_confidence": 45.36,
                "rr_net": 1.049,
                "ev_pct": 0.023,
            },
        )
        row = study.build_observation(
            signal(), vetoed, minimum_order(), captured_epoch=2000
        )
        self.assertAlmostEqual(row["confidence"], 45.36)
        self.assertAlmostEqual(row["risk_reward"], 1.049)
        self.assertAlmostEqual(row["expected_value"], 0.023)
        self.assertEqual(row["reason_category"], "RR_BELOW_MIN")
        self.assertEqual(row["metric_source"], "SCORE_SNAPSHOT_PRE_VETO")
        self.assertFalse(row["execution_allowed"])
        self.assertFalse(row["risk_epoch_traversal_credit"])

    def test_report_normalizes_legacy_zeroed_record_without_rewriting_history(self):
        raw = payload("legacy", False)
        obs = raw["counterfactual_nexus_v1"]
        obs["risk_reward"] = 0.0
        obs["expected_value"] = 0.0
        obs["confidence"] = 0.0
        obs["reason"] = "EV negativo após custos: -0.184% (R:R líquido 0.82)"
        obs.pop("reason_category", None)
        obs.pop("metric_source", None)
        obs["score_snapshot"] = {
            "fusion_confidence": 15.3,
            "rr_net": 0.822,
            "ev_pct": -0.1837,
        }
        report = study.build_report([raw])
        self.assertAlmostEqual(report["avg_risk_reward"], 0.822)
        self.assertAlmostEqual(report["avg_expected_value"], -0.1837)
        self.assertEqual(report["rejection_reasons"], {"EV_NEGATIVE": 1})
        # Source payload remains historical and untouched.
        self.assertEqual(obs["risk_reward"], 0.0)
        self.assertEqual(obs["expected_value"], 0.0)

    def test_report_keeps_counterfactual_outcomes_separate(self):
        outcomes = [
            {
                "candidate_id": "c1", "outcome": "OBSERVED",
                "future_return": 0.02, "MFE": 0.03, "MAE": -0.01,
            },
            {
                "candidate_id": "c2", "outcome": "OBSERVED",
                "future_return": -0.01, "MFE": 0.01, "MAE": -0.02,
            },
        ]
        report = study.build_report(
            [payload("c1", True), payload("c2", False)], outcomes
        )
        self.assertEqual(report["evaluated"], 2)
        self.assertEqual(report["allowed"], 1)
        self.assertEqual(report["rejected"], 1)
        self.assertEqual(report["observed_60m"], 2)
        self.assertEqual(report["approved_observed_60m"], 1)
        self.assertAlmostEqual(report["approved_60m_avg_gross_return"], 0.02)
        self.assertFalse(report["risk_epoch_traversal_credit"])
        self.assertFalse(report["automatic_promotion"])

    def test_twenty_evaluations_still_wait_for_twenty_outcomes(self):
        rows = [payload(f"c{i}", i % 2 == 0) for i in range(20)]
        report = study.build_report(rows)
        self.assertEqual(
            report["status"], "EVALUATION_SAMPLE_COMPLETE_OUTCOMES_PENDING"
        )
        outcomes = [
            {
                "candidate_id": f"c{i}", "outcome": "OBSERVED",
                "future_return": 0.0, "MFE": 0.01, "MAE": -0.01,
            }
            for i in range(20)
        ]
        report = study.build_report(rows, outcomes)
        self.assertEqual(
            report["status"], "EVIDENCE_SAMPLE_COMPLETE_RESEARCH_ONLY"
        )
        self.assertFalse(report["promotion_allowed"])

    def test_flag_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(study.enabled())


class FakeDB:
    async def _fetchall(self, sql, args=()):
        if "risk_epoch_shadow_v1" in sql:
            return [{"payload": json.dumps({"started_epoch": 1500.0})}]
        if "hard_gate_shadow_candidates_v1" in sql and "SELECT payload" in sql:
            self.candidate_args = args
            return [{"payload": json.dumps(payload("db1", True))}]
        if "hard_gate_shadow_outcomes_v1" in sql:
            self.outcome_args = args
            return [{
                "candidate_id": "db1",
                "payload": json.dumps({
                    "outcome": "OBSERVED",
                    "future_return": 0.01,
                    "MFE": 0.02,
                    "MAE": -0.005,
                }),
            }]
        raise AssertionError(sql)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_active_epoch_and_60m_only(self):
        db = FakeDB()
        with patch.dict(os.environ, {
            "RISK_EPOCH_SHADOW_ID": "REENTRY_V1_20261004_R2",
        }):
            report = asyncio.run(study.snapshot(db))
        self.assertEqual(report["evaluated"], 1)
        self.assertEqual(report["observed_60m"], 1)
        self.assertEqual(
            db.candidate_args, ("HARD_GATE_SHADOW", 1500.0)
        )
        self.assertEqual(
            db.outcome_args, ("HARD_GATE_SHADOW", 1500.0, 60)
        )

    def test_summary_explicitly_denies_traversal_and_live_authority(self):
        report = study.build_report([payload()])
        line = study.format_summary(report)
        self.assertIn("risk_epoch_traversal_credit=false", line)
        self.assertIn("automatic_promotion=false", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("decision_effect=NONE execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
