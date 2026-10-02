import unittest

from bot.binance_oos_evidence_bundle import (
    assert_population_parity,
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
