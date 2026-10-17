import unittest

from bot.disaster_recovery_drills import (
    DrillEvidence,
    DrillScenario,
    drill_matrix,
    evaluate_drill,
)
from bot.release_governance_v1 import (
    PromotionEvidence,
    ReleaseStage,
    recommend_next_stage,
    rollback_recommendation,
)


class ReleaseGovernanceTests(unittest.TestCase):
    def _evidence(self):
        return PromotionEvidence(
            expectancy_r=0.2,
            ci95_low_r=0.05,
            sample_n=200,
            costs_included=True,
            forward_shadow_consistent=True,
            execution_parity_passed=True,
            robustness_passed=True,
            slo_passed=True,
            concentration_ok=True,
        )

    def test_promotion_requires_explicit_id(self):
        blocked = recommend_next_stage(
            current_stage=ReleaseStage.SHADOW_ONLY,
            evidence=self._evidence(),
            promotion_id=None,
        )
        self.assertFalse(blocked["promotable"])
        self.assertIn("PROMOTION_ID_REQUIRED", blocked["blockers"])
        self.assertFalse(blocked["runtime_mutated"])

        allowed = recommend_next_stage(
            current_stage=ReleaseStage.SHADOW_ONLY,
            evidence=self._evidence(),
            promotion_id="exp-123",
        )
        self.assertEqual(
            allowed["recommended_stage"],
            ReleaseStage.SCORE_ENABLED.value,
        )
        self.assertEqual(allowed["execution_effect"], "NONE")

    def test_rollback_combines_performance_and_ops_evidence(self):
        result = rollback_recommendation(
            expected_expectancy_r=0.2,
            live_expectancy_r=-0.2,
            max_gap_r=0.2,
            severe_anomaly=False,
            slo_breached=True,
            parity_breached=False,
        )
        self.assertTrue(result["rollback_recommended"])
        self.assertIn("LIVE_EXPECTANCY_DIVERGENCE", result["reasons"])
        self.assertIn("SLO_BREACH", result["reasons"])
        self.assertFalse(result["runtime_mutated"])


class DisasterDrillTests(unittest.TestCase):
    def _pass(self, scenario):
        return DrillEvidence(
            scenario=scenario,
            duplicate_orders=0,
            unintended_extra_exposure_usdt=0.0,
            durable_state_recovered=True,
            ownership_fencing_valid=True,
            reconciliation_success=True,
            protection_restored_or_preserved=True,
            close_path_available=True,
        )

    def test_failed_protection_fails_drill(self):
        evidence = DrillEvidence(
            scenario=DrillScenario.RESTART_OPEN_POSITION,
            duplicate_orders=0,
            unintended_extra_exposure_usdt=0.0,
            durable_state_recovered=True,
            ownership_fencing_valid=True,
            reconciliation_success=True,
            protection_restored_or_preserved=False,
            close_path_available=True,
        )
        self.assertFalse(evaluate_drill(evidence)["pass"])

    def test_full_matrix_requires_all_scenarios(self):
        evidence = [self._pass(scenario) for scenario in DrillScenario]
        report = drill_matrix(evidence)
        self.assertTrue(report["all_pass"])
        self.assertTrue(report["coverage_complete"])
        self.assertEqual(report["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
