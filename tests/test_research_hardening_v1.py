import unittest

from bot.anti_overfitting_gate import (
    benjamini_hochberg,
    robustness_gate,
    threshold_sensitivity,
)
from bot.research_risk_policy import (
    DrawdownPolicy,
    classify_drawdown,
    dynamic_risk_recommendation,
)
from bot.slo_anomaly_v1 import (
    SLOThresholds,
    evaluate_slo,
    robust_anomaly_score,
)
from bot.tail_risk_scenarios import TailScenario, stress_trade


class AntiOverfitTests(unittest.TestCase):
    def test_bh_controls_multiple_testing(self):
        result = benjamini_hochberg(
            {"a": 0.001, "b": 0.02, "c": 0.8},
            false_discovery_rate=0.05,
        )
        self.assertTrue(result["a"]["discovery"])
        self.assertFalse(result["c"]["discovery"])

    def test_threshold_sensitivity_detects_stable_region(self):
        report = threshold_sensitivity(
            {60: 0.10, 61: 0.11, 62: 0.09, 63: 0.08, 64: 0.07}
        )
        self.assertTrue(report["locally_stable"])
        self.assertEqual(report["execution_effect"], "NONE")

    def test_robustness_is_evidence_only(self):
        ok, blockers = robustness_gate(
            temporal_positive_share=0.8,
            symbol_positive_share=0.7,
            side_positive_share=1.0,
            slippage_stress_positive=True,
            fee_stress_positive=True,
            delayed_entry_positive=True,
            alternative_thresholds_stable=True,
        )
        self.assertTrue(ok)
        self.assertFalse(blockers)


class RiskPolicyTests(unittest.TestCase):
    def test_drawdown_policy_is_counterfactual_only(self):
        policy = DrawdownPolicy(0.05, 0.10, 0.20)
        result = classify_drawdown(0.12, policy)
        self.assertEqual(result["regime"], "RECOVERY")
        self.assertEqual(result["execution_effect"], "NONE")
        self.assertFalse(result["runtime_policy_mutated"])

    def test_dynamic_risk_never_loss_chases(self):
        result = dynamic_risk_recommendation(
            calibrated_confidence=0.8,
            regime_quality=0.9,
            concentration=0.2,
            volatility_ratio=1.0,
            recent_variance_ratio=1.0,
            edge_proven=True,
        )
        self.assertLessEqual(result["counterfactual_risk_multiplier"], 1.0)
        self.assertFalse(result["loss_chasing"])


class TailAndSLOTests(unittest.TestCase):
    def test_tail_scenario_increases_stressed_loss(self):
        scenario = TailScenario(
            "FLASH_CRASH",
            adverse_gap_pct=3.0,
            slippage_multiplier=3.0,
            funding_shock_pct=0.2,
            partial_fill_fraction=0.5,
            extra_latency_ms=2000,
        )
        result = stress_trade(
            notional_usdt=1000,
            stop_loss_pct=1.0,
            baseline_slippage_pct=0.1,
            baseline_fee_pct=0.1,
            scenario=scenario,
        )
        self.assertGreater(result["stressed_loss_pct"], 4.0)
        self.assertEqual(result["execution_effect"], "NONE")

    def test_slo_and_robust_anomaly(self):
        thresholds = SLOThresholds(
            1000,
            500,
            100,
            1000,
            0.99,
            0.99,
            5,
        )
        report = evaluate_slo(
            {
                "market_freshness_ms": 100,
                "ack_latency_ms": 100,
                "db_latency_ms": 10,
                "protection_latency_ms": 100,
                "ws_uptime": 1.0,
                "reconciliation_success": 1.0,
                "lease_remaining_seconds": 20,
            },
            thresholds,
        )
        self.assertTrue(report["all_slos_met"])
        anomaly = robust_anomaly_score([1, 1.1, 0.9, 1.0, 1.05], 10)
        self.assertTrue(anomaly["anomaly"])


if __name__ == "__main__":
    unittest.main()
