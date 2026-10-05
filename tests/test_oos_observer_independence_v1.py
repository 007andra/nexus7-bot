"""Regression proof: prospective OOS observers do not depend on legacy review success."""
import inspect
import unittest

from bot import hard_gate_shadow_scan as shadow


class ProspectiveOosObserverIndependenceTests(unittest.TestCase):
    def test_oos_ledger_and_maturation_execute_before_legacy_release_prerequisites(self):
        src = inspect.getsource(shadow.scan)
        prospective = src.index(
            "prospective_oos_report = await _maybe_emit_prospective_oos_cohort(db)"
        )
        independent = src.index(
            "if prospective_oos_report is not None:", prospective
        )
        ledger = src.index("_pilot_ledger_v1.snapshot(", independent)
        maturation = src.index("_maturation_review_v1.snapshot(", independent)
        legacy_gate = src.index(
            "if epoch_row is not None and validation_report is not None and review_report is not None:",
            independent,
        )

        self.assertLess(prospective, independent)
        self.assertLess(independent, ledger)
        self.assertLess(ledger, legacy_gate)
        self.assertLess(maturation, legacy_gate)

        independent_segment = src[independent:legacy_gate]
        self.assertNotIn("review_report is not None", independent_segment)
        self.assertIn('"promotion_allowed": False', independent_segment)
        self.assertIn('"live_allowed": False', independent_segment)
        self.assertIn('"decision_effect": "NONE"', independent_segment)
        self.assertIn('"execution_effect": "NONE"', independent_segment)

    def test_legacy_release_block_reuses_independent_research_outputs(self):
        src = inspect.getsource(shadow.scan)
        self.assertIn("independent_budget_study", src)
        self.assertIn("independent_shadow_ledger", src)
        self.assertIn("independent_first_approval_review", src)
        self.assertIn("independent_maturation_review", src)
        self.assertIn("shadow_ledger = independent_shadow_ledger", src)
        self.assertIn(
            "first_approval_review = independent_first_approval_review",
            src,
        )


if __name__ == "__main__":
    unittest.main()
