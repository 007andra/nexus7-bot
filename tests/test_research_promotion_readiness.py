import unittest

from bot.research_promotion_readiness import (
    evaluate_readiness,
    evidence_gate_flags,
)


def _bundle():
    return {
        "manifest_hash": "a" * 64,
        "dataset_fingerprint": "b" * 64,
        "manifest": {"artifacts": [{"dataset": "klines"}]},
        "primary": {
            "status": "AI_EDGE_PROVEN",
            "blockers": [],
            "context_parity_complete": True,
        },
        "robustness": {
            "robustness": {
                "summary": {
                    "temporal_folds_evaluated": 4,
                    "stable_positive_point_estimate": True,
                    "leave_one_symbol_out_evaluated": 5,
                }
            }
        },
        "calibration": {
            "evidence_complete": True,
            "fold_count": 4,
            "fit_scope": "TRAIN_ONLY",
            "evaluation_scope": "OOS_ONLY",
            "purge_basis": "ACTUAL_LABEL_END_TIMESTAMP",
            "embargo_rows": 4,
            "missing_label_end": 0,
            "live_probability_effect": "NONE",
            "methods": {
                "platt": {"status": "OK"},
                "isotonic": {"status": "OK"},
            },
        },
        "methodology": {
            "venue": "BINANCE_USDM",
            "source": "data.binance.vision",
            "archive_checksums_verified": True,
            "shared_candidate_population": True,
            "closed_candles_only": True,
            "historical_clock_frozen": True,
            "metrics_label_shift_normalized": True,
            "oi_delta_semantics": "PREVIOUS_NEXUS_CANDIDATE",
            "same_bar_ambiguity": "STOP_FIRST",
            "fees_included": True,
            "slippage_included": True,
            "funding_included": True,
            "authenticated_api": False,
            "exchange_mutations": False,
            "runtime_policy_mutations": False,
            "promotion_authority": False,
        },
    }


def _drift(status="STABLE"):
    return {
        "status": status,
        "baseline_rows": 100,
        "current_rows": 40,
        "execution_effect": "NONE",
    }


def _sensitivity():
    return {
        "parameter": {
            "parameter_sets": 4,
            "promotion_effect": "NONE",
        },
        "execution_cost": {
            "points": [{"fee_bps": 10.0}],
            "promotion_effect": "NONE",
        },
    }


class ResearchPromotionReadinessTests(unittest.TestCase):
    def test_complete_evidence_only_reaches_operator_review(self):
        result = evaluate_readiness(
            _bundle(),
            ci_green=True,
            shadow_drift=_drift(),
            sensitivity=_sensitivity(),
        )
        self.assertTrue(result.ready_for_operator_review)
        self.assertEqual(result.blockers, ())
        self.assertFalse(result.promotion_authority)
        self.assertEqual(result.execution_effect, "NONE")

        flags = evidence_gate_flags(result)
        self.assertTrue(flags["ci_green"])
        self.assertTrue(flags["oos_green"])
        self.assertTrue(flags["shadow_green"])
        self.assertFalse(flags["operator_approved"])

    def test_missing_dataset_fingerprint_fails_closed(self):
        bundle = _bundle()
        bundle.pop("dataset_fingerprint")
        result = evaluate_readiness(
            bundle,
            ci_green=True,
            shadow_drift=_drift(),
            sensitivity=_sensitivity(),
        )
        self.assertFalse(result.ready_for_operator_review)
        self.assertIn("MANIFEST_VALID", result.blockers)

    def test_ci_false_fails_closed(self):
        result = evaluate_readiness(
            _bundle(),
            ci_green=False,
            shadow_drift=_drift(),
            sensitivity=_sensitivity(),
        )
        self.assertFalse(result.ready_for_operator_review)
        self.assertIn("CI_GREEN", result.blockers)

    def test_incomplete_historical_parity_fails_closed(self):
        bundle = _bundle()
        bundle["primary"]["context_parity_complete"] = False
        result = evaluate_readiness(
            bundle,
            ci_green=True,
            shadow_drift=_drift(),
            sensitivity=_sensitivity(),
        )
        self.assertFalse(result.ready_for_operator_review)
        self.assertIn("PRIMARY_EDGE_GREEN", result.blockers)

    def test_watch_or_drift_is_not_shadow_green(self):
        for status in ("WATCH", "DRIFT", "SEVERE", "INSUFFICIENT_HISTORY"):
            result = evaluate_readiness(
                _bundle(),
                ci_green=True,
                shadow_drift=_drift(status),
                sensitivity=_sensitivity(),
            )
            self.assertFalse(result.ready_for_operator_review)
            self.assertIn("SHADOW_DRIFT_GREEN", result.blockers)

    def test_missing_sensitivity_fails_closed(self):
        result = evaluate_readiness(
            _bundle(),
            ci_green=True,
            shadow_drift=_drift(),
            sensitivity=None,
        )
        self.assertFalse(result.ready_for_operator_review)
        self.assertIn("SENSITIVITY_PRESENT", result.blockers)

    def test_calibration_without_label_end_purge_fails_closed(self):
        bundle = _bundle()
        bundle["calibration"]["purge_basis"] = "CANDIDATE_COUNT"
        result = evaluate_readiness(
            bundle,
            ci_green=True,
            shadow_drift=_drift(),
            sensitivity=_sensitivity(),
        )
        self.assertFalse(result.ready_for_operator_review)
        self.assertIn("CALIBRATION_GREEN", result.blockers)

    def test_oi_delta_semantic_drift_fails_closed(self):
        bundle = _bundle()
        bundle["methodology"]["oi_delta_semantics"] = "PREVIOUS_5M_METRICS_ROW"
        result = evaluate_readiness(
            bundle,
            ci_green=True,
            shadow_drift=_drift(),
            sensitivity=_sensitivity(),
        )
        self.assertFalse(result.ready_for_operator_review)
        self.assertIn("METHODOLOGY_GREEN", result.blockers)

    def test_truthy_strings_never_count_as_methodology_proof(self):
        bundle = _bundle()
        bundle["methodology"]["archive_checksums_verified"] = "true"
        result = evaluate_readiness(
            bundle,
            ci_green=True,
            shadow_drift=_drift(),
            sensitivity=_sensitivity(),
        )
        self.assertFalse(result.ready_for_operator_review)
        self.assertIn("METHODOLOGY_GREEN", result.blockers)


if __name__ == "__main__":
    unittest.main()
