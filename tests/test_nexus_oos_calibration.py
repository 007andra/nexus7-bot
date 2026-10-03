import unittest

from bot.nexus_oos_calibration import (
    calibrate_walk_forward,
    fit_isotonic,
    fit_platt,
)
from bot.oos_model_validation import ValidationRow, purged_walk_forward


def _rows(n=160):
    rows = []
    for i in range(n):
        bucket = i % 10
        confidence = 0.10 + 0.08 * bucket
        outcome = 1 if bucket >= 5 else 0
        rows.append(
            ValidationRow(
                timestamp=float(i),
                confidence=confidence,
                outcome=outcome,
                r_multiple=1.0 if outcome else -1.0,
            )
        )
    return rows


class NexusOOSCalibrationTests(unittest.TestCase):
    def test_platt_fit_is_deterministic(self):
        train = _rows(80)
        a = fit_platt(train)
        b = fit_platt(train)
        self.assertAlmostEqual(a.intercept, b.intercept, places=12)
        self.assertAlmostEqual(a.slope, b.slope, places=12)
        self.assertTrue(0.0 < a.predict(0.5) < 1.0)

    def test_isotonic_fit_is_monotone(self):
        calibrator = fit_isotonic(_rows(80))
        preds = [calibrator.predict(x / 20) for x in range(21)]
        self.assertTrue(all(a <= b for a, b in zip(preds, preds[1:])))

    def test_walk_forward_fits_train_and_evaluates_oos_only(self):
        folds = purged_walk_forward(
            _rows(),
            train_size=60,
            test_size=20,
            purge_size=5,
            step_size=20,
        )
        report = calibrate_walk_forward(folds, method="platt", bins=5)
        self.assertEqual(report["fit_scope"], "TRAIN_ONLY")
        self.assertEqual(report["evaluation_scope"], "OOS_ONLY")
        self.assertEqual(report["execution_effect"], "NONE")
        self.assertTrue(report["folds"])
        for fold in report["folds"]:
            self.assertLess(fold["train_end_ts"], fold["test_start_ts"])

    def test_future_test_mutation_cannot_change_fitted_calibrator(self):
        rows = _rows(100)
        train = rows[:60]
        original = fit_platt(train)
        mutated = list(rows)
        for i in range(60, len(mutated)):
            row = mutated[i]
            mutated[i] = ValidationRow(
                row.timestamp,
                row.confidence,
                1 - row.outcome,
                -row.r_multiple,
            )
        after = fit_platt(mutated[:60])
        self.assertEqual(original, after)

    def test_single_class_training_fails_closed(self):
        rows = [
            ValidationRow(float(i), 0.7, 1, 1.0)
            for i in range(20)
        ]
        with self.assertRaises(ValueError):
            fit_platt(rows)
        with self.assertRaises(ValueError):
            fit_isotonic(rows)


if __name__ == "__main__":
    unittest.main()
