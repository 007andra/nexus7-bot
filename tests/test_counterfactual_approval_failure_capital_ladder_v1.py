"""Tests for approval-failure analysis and capital ladder V1."""
import unittest

from bot import counterfactual_approval_failure_analysis_v1 as failure
from bot import min_order_capital_ladder_v1 as ladder


def candidate(
    cid,
    *,
    allowed,
    required,
    symbol="BTCUSDT",
    side="LONG",
    setup="MOMENTUM",
    regime="TRENDING_UP",
    rr=1.8,
    ev=0.2,
    score=70,
    confidence=65,
    entry=100.0,
    stop=98.0,
    captured=1_700_000_000,
):
    return {
        "candidate_id": cid,
        "captured_epoch": captured,
        "symbol": symbol,
        "side": side,
        "setup": setup,
        "regime": regime,
        "score": score,
        "entry": entry,
        "stop": stop,
        "pullback_pass": True,
        "production_equivalent_funnel_result": True,
        "capital_source": "CACHE",
        "shadow_min_order_feasible": False,
        "required_equity_at_min_qty": required,
        "cost_snapshot": {
            "taker_fee": 0.0005,
            "entry_slippage": 0.0004,
            "exit_slippage": 0.0004,
            "spread_bps": 2.0,
        },
        "counterfactual_nexus_v1": {
            "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
            "candidate_id": cid,
            "symbol": symbol,
            "side": side,
            "setup": setup,
            "regime": regime,
            "execution_allowed": allowed,
            "risk_reward": rr,
            "expected_value": ev,
            "confidence": confidence,
            "setup_quality": 60,
            "required_equity_at_min_qty": required,
            "score_snapshot": {
                "fusion_confidence": confidence,
                "rr_net": rr,
                "ev_pct": ev,
            },
            "reason": "approved" if allowed else "R:R below minimum",
            "reason_category": "OTHER" if allowed else "RR_BELOW_MIN",
            "risk_epoch_traversal_credit": False,
            "promotion_allowed": False,
            "live_allowed": False,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        },
    }


def outcome(cid, ret, *, mfe=0.01, mae=-0.01):
    return {
        "candidate_id": cid,
        "outcome": "OBSERVED",
        "future_return": ret,
        "MFE": mfe,
        "MAE": mae,
    }


class ApprovalFailureAnalysisTests(unittest.TestCase):
    def test_negative_selection_confirmed_both_horizons(self):
        payloads = []
        out60 = []
        out240 = []
        # 5 approved candidates perform worse than 5 rejected candidates.
        for i in range(5):
            cid = f"A{i}"
            payloads.append(candidate(cid, allowed=True, required=14 + i))
            out60.append(outcome(cid, -0.010 - i * 0.001, mae=-0.020))
            out240.append(outcome(cid, -0.015 - i * 0.001, mae=-0.025))
        for i in range(5):
            cid = f"R{i}"
            payloads.append(candidate(
                cid,
                allowed=False,
                required=20 + i,
                symbol="ETHUSDT",
                rr=1.4,
                ev=0.1,
            ))
            out60.append(outcome(cid, 0.002 + i * 0.001, mae=-0.006))
            out240.append(outcome(cid, 0.003 + i * 0.001, mae=-0.008))

        report = failure.build_report(
            payloads, out60, out240, epoch_id="E1", started_epoch=1.0
        )
        self.assertEqual(
            report["status"], "NEGATIVE_SELECTION_CONFIRMED_BOTH_HORIZONS"
        )
        self.assertLess(report["allowed_vs_rejected_60m_mean_lift"], 0)
        self.assertLess(report["allowed_vs_rejected_240m_mean_lift"], 0)
        self.assertIn(
            "APPROVED_UNDERPERFORMS_REJECTED_AT_60M_AND_240M",
            report["diagnostics"],
        )
        self.assertTrue(report["association_not_causation"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["risk_epoch_traversal_credit"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")

    def test_small_sample_does_not_claim_both_horizon_confirmation(self):
        payloads = [
            candidate("A", allowed=True, required=14),
            candidate("R", allowed=False, required=20),
        ]
        out = [outcome("A", -0.01), outcome("R", 0.01)]
        report = failure.build_report(payloads, out, out)
        self.assertNotEqual(
            report["status"], "NEGATIVE_SELECTION_CONFIRMED_BOTH_HORIZONS"
        )
        self.assertFalse(report["live_allowed"])

    def test_formatter_restates_research_boundary(self):
        payloads = [candidate("A", allowed=True, required=14)]
        out = [outcome("A", -0.01)]
        report = failure.build_report(payloads, out, out)
        line = failure.format_summary(report)
        self.assertIn("association_not_causation=true", line)
        self.assertIn("thresholds_unchanged=true", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("execution_effect=NONE", line)


class CapitalLadderTests(unittest.TestCase):
    def test_fixed_equity_ladder_counts_only_required_equity_feasibility(self):
        payloads = [
            candidate("A10", allowed=True, required=9.5),
            candidate("A14", allowed=True, required=13.5),
            candidate("R19", allowed=False, required=18.5),
            candidate("A25", allowed=True, required=24.0),
            candidate("R35", allowed=False, required=34.0),
            candidate("A50", allowed=True, required=49.0),
            candidate("X", allowed=True, required=70.0),
        ]
        out60 = [
            outcome("A10", -0.01),
            outcome("A14", 0.02),
            outcome("A25", 0.03),
            outcome("A50", -0.01),
            outcome("X", 0.10),
        ]
        out240 = [
            outcome("A10", -0.02),
            outcome("A14", 0.03),
            outcome("A25", 0.04),
            outcome("A50", -0.02),
            outcome("X", 0.20),
        ]

        report = ladder.build_report(payloads, out60, out240)
        by_level = {row["equity"]: row for row in report["ladder"]}

        self.assertEqual(by_level[10.0]["min_order_feasible_candidates"], 1)
        self.assertEqual(by_level[14.0]["min_order_feasible_candidates"], 2)
        self.assertEqual(by_level[19.0]["min_order_feasible_candidates"], 3)
        self.assertEqual(by_level[25.0]["min_order_feasible_candidates"], 4)
        self.assertEqual(by_level[35.0]["min_order_feasible_candidates"], 5)
        self.assertEqual(by_level[50.0]["min_order_feasible_candidates"], 6)
        self.assertEqual(by_level[19.0]["counterfactual_allowed"], 2)
        self.assertEqual(by_level[25.0]["allowed_60m"]["n"], 3)

        for row in report["ladder"]:
            self.assertEqual(row["canonical_pipeline_credit"], 0)
            self.assertEqual(row["lifetime_drawdown_credit"], 0)
            self.assertFalse(row["live_eligible"])

        self.assertTrue(report["external_capital_does_not_clear_drawdown"])
        self.assertTrue(report["capital_metric_is_counterfactual_not_recommendation"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["risk_epoch_traversal_credit"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")

    def test_non_min_order_rows_are_excluded(self):
        good = candidate("GOOD", allowed=True, required=14)
        already_feasible = candidate("FEASIBLE", allowed=True, required=9)
        already_feasible["shadow_min_order_feasible"] = True
        failed_funnel = candidate("FUNNEL", allowed=True, required=9)
        failed_funnel["production_equivalent_funnel_result"] = False
        report = ladder.build_report([good, already_feasible, failed_funnel])
        self.assertEqual(report["min_order_only_candidates"], 1)

    def test_ladder_formatter_never_implies_live_authority(self):
        report = ladder.build_report([
            candidate("A", allowed=True, required=14)
        ])
        line = ladder.format_ladder(report)
        self.assertIn("canonical_pipeline_credit=0", line)
        self.assertIn("lifetime_drawdown_credit=0", line)
        self.assertIn("live_eligible=false", line)
        self.assertIn("execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
