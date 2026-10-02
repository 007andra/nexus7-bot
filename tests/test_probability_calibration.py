import unittest

from bot.oos_model_validation import ValidationRow
from bot.probability_calibration import (
    apply_platt,
    fit_platt,
    walk_forward_platt_report,
)


def _rows(n=240):
    rows = []
    for i in range(n):
        # Deliberately miscalibrated but informative confidence.
        outcome = 1 if i % 4 in (0, 1) else 0
        confidence = 0.85 if outcome else 0.35
        # Add deterministic variation so fitting is not degenerate.
        confidence += ((i % 7) - 3) * 0.005
        confidence = min(0.98, max(0.02, confidence))
        rows.append(ValidationRow(
            timestamp=float(i),
            confidence=confidence,
            outcome=outcome,
            r_multiple=1.2 if outcome else -1.0,
        ))
    return rows


class ProbabilityCalibrationTests(unittest.TestCase):
    def test_fit_requires_both_classes(self):
        rows = [
            ValidationRow(float(i), 0.8, 1, 1.0)
            for i in range(30)
        ]
        with self.assertRaises(ValueError):
            fit_platt(rows)

    def test_test_labels_do_not_affect_calibrated_predictions(self):
        train = _rows(80)
        model = fit_platt(train)
        test_a = [
            ValidationRow(100.0 + i, 0.65, i % 2, 1.0 if i % 2 else -1.0)
            for i in range(20)
        ]
        test_b = [
            ValidationRow(row.timestamp, row.confidence, 1 - row.outcome, row.r_multiple)
            for row in test_a
        ]
        pred_a = [row.confidence for row in apply_platt(model, test_a)]
        pred_b = [row.confidence for row in apply_platt(model, test_b)]
        self.assertEqual(pred_a, pred_b)

    def test_walk_forward_uses_four_oos_folds_without_live_effect(self):
        result = walk_forward_platt_report(
            _rows(260),
            train_size=80,
            test_size=40,
            purge_size=5,
            minimum_folds=4,
        )
        self.assertGreaterEqual(result["fold_count"], 4)
        self.assertTrue(result["evidence_complete"])
        self.assertEqual(result["live_probability_effect"], "NONE")
        self.assertFalse(result["promotion_authority"])
        for fold in result["folds"]:
            self.assertLess(fold["train_end_ts"], fold["test_start_ts"])


if __name__ == "__main__":
    unittest.main()
