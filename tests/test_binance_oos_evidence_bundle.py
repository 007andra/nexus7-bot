import unittest

from bot.binance_oos_evidence_bundle import (
    assert_population_parity,
    build_opportunity_ranking_report,
    build_primary_report,
    build_robustness_report,
    build_sensitivity_report,
)
from bot.nexus_oos_edge_gate import CandidateOutcome


def _candidate(ts, approved, r):
    return CandidateOutcome(
        timestamp=float(ts),
        approved=bool(approved),
        baseline_eligible=True,
        confidence=0.7 if approved else 0.4,
        outcome_known=True,
        r_multiple=float(r),
    ).validate()


def _reports():
    return [
        {
            "symbol": "BTCUSDT",
            "candidates": [
                _candidate(1, True, 1.0),
                _candidate(2, False, -1.0),
                _candidate(3, True, 0.5),
                _candidate(4, False, -0.5),
            ],
            "historical_context": {"parity_complete": True},
        },
        {
            "symbol": "ETHUSDT",
            "candidates": [
                _candidate(1, True, 0.8),
                _candidate(2, False, -0.7),
                _candidate(3, True, 0.3),
                _candidate(4, False, -0.2),
            ],
            "historical_context": {"parity_complete": True},
        },
    ]



def _ranking_reports():
    base = {
        "timestamp": 1_700_000_000_000,
        "direction": "LONG",
        "nexus_rr_net": 2.0,
        "round_trip_cost": 0.001,
        "nexus_confidence": 70.0,
        "nexus_regime_compat": 80.0,
        "shadow_microstructure": {
            "available": True,
            "directional_alignment": 0.8,
            "taker_pressure": 0.7,
            "execution_effect": "NONE",
            "score_effect": "NONE",
            "promotion_authority": False,
        },
    }
    return [
        {
            "symbol": "BTCUSDT",
            "candidate_diagnostics": [{
                **base,
                "candidate_id": "btc-a",
                "nexus_expected_value_pct": 1.4,
                "nexus_setup_quality": 85.0,
                "depth_notional_1pct": 10_000_000.0,
                "r_multiple": 1.5,
            }],
        },
        {
            "symbol": "ETHUSDT",
            "candidate_diagnostics": [{
                **base,
                "candidate_id": "eth-b",
                "nexus_expected_value_pct": 1.5,
                "nexus_setup_quality": 90.0,
                "depth_notional_1pct": 2_000_000.0,
                "shadow_microstructure": {
                    "available": True,
                    "directional_alignment": -0.8,
                    "taker_pressure": -0.7,
                    "execution_effect": "NONE",
                    "score_effect": "NONE",
                    "promotion_authority": False,
                },
                "r_multiple": -0.5,
            }],
        },
    ]

class BinanceOOSEvidenceBundleTests(unittest.TestCase):
    def test_primary_and_robustness_share_exact_population(self):
        reports = _reports()
        primary = build_primary_report(reports)
        robustness = build_robustness_report(reports)
        assert_population_parity(primary, robustness)
        self.assertEqual(
            primary["report"]["baseline_candidates"],
            robustness["robustness"]["pooled"]["baseline_candidates"],
        )

    def test_population_drift_fails_closed(self):
        reports = _reports()
        primary = build_primary_report(reports)
        robustness = build_robustness_report(reports)
        robustness["robustness"]["pooled"]["baseline_candidates"] += 1
        with self.assertRaises(RuntimeError):
            assert_population_parity(primary, robustness)

    def test_sensitivity_reports_cost_stress_but_not_fake_parameter_grid(self):
        reports = _reports()
        for report in reports:
            report["candidate_diagnostics"] = [
                {"net_return": 0.01},
                {"net_return": -0.005},
                {"net_return": 0.004},
                {"net_return": -0.002},
            ]
        sensitivity = build_sensitivity_report(reports)
        self.assertEqual(
            sensitivity["parameter"]["status"],
            "NOT_RUN",
        )
        self.assertEqual(
            sensitivity["parameter"]["parameter_sets"],
            0,
        )
        self.assertTrue(sensitivity["execution_cost"]["points"])
        self.assertEqual(sensitivity["execution_effect"], "NONE")
        self.assertFalse(sensitivity["promotion_authority"])

    def test_opportunity_ranking_is_cross_symbol_and_shadow_only(self):
        report = build_opportunity_ranking_report(_ranking_reports())
        self.assertEqual(report["status"], "EVIDENCE_AVAILABLE")
        self.assertEqual(report["cross_sections"], 1)
        self.assertEqual(report["ranked_candidates"], 2)
        self.assertGreater(report["top1_uplift_r"], 0)
        self.assertGreater(report["incremental_top_pick_uplift_r"], 0)
        self.assertEqual(
            report["details"][0]["base_top_candidate_id"],
            "eth-b",
        )
        self.assertEqual(
            report["details"][0]["enriched_top_candidate_id"],
            "btc-a",
        )
        self.assertFalse(report["outcome_used_in_rank"])
        self.assertEqual(report["execution_effect"], "NONE")
        self.assertEqual(report["score_effect"], "NONE")
        self.assertFalse(report["promotion_authority"])
        self.assertEqual(
            report["details"][0]["top_candidate_id"],
            "btc-a",
        )

    def test_realized_result_flip_does_not_change_top_pretrade_candidate(self):
        reports = _ranking_reports()
        first = build_opportunity_ranking_report(reports)
        reports[0]["candidate_diagnostics"][0]["r_multiple"] = -4.0
        reports[1]["candidate_diagnostics"][0]["r_multiple"] = 4.0
        second = build_opportunity_ranking_report(reports)
        self.assertEqual(
            first["details"][0]["top_candidate_id"],
            second["details"][0]["top_candidate_id"],
        )
        self.assertNotEqual(
            first["top1_uplift_r"],
            second["top1_uplift_r"],
        )

    def test_duplicate_candidate_id_fails_closed(self):
        reports = _ranking_reports()
        reports[1]["candidate_diagnostics"][0]["candidate_id"] = "btc-a"
        with self.assertRaises(RuntimeError):
            build_opportunity_ranking_report(reports)

    def test_incomplete_context_cannot_claim_edge_proven(self):
        reports = _reports()
        reports[0]["historical_context"]["parity_complete"] = False
        primary = build_primary_report(reports)
        self.assertEqual(primary["status"], "AI_EDGE_NOT_PROVEN")
        self.assertIn(
            "HISTORICAL_CONTEXT_PARITY_INCOMPLETE",
            primary["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
