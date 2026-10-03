import unittest

from bot.oos_model_validation import (
    ValidationRow,
    aggregate_oos_report,
    brier_score,
    expected_calibration_error,
    label_aware_purged_embargo_walk_forward,
    log_loss,
    promotion_decision,
    purged_walk_forward,
    reliability_bins,
    report,
)


def _rows(n=240):
    rows = []
    for i in range(n):
        # Deliberately calibrated-ish sample: higher confidence wins more often.
        confidence = 0.2 + 0.6 * ((i % 10) / 9)
        outcome = 1 if (i % 10) >= 5 else 0
        r = 1.2 if outcome else -0.8
        rows.append(ValidationRow(float(i), confidence, outcome, r))
    return rows


class OOSModelValidationTests(unittest.TestCase):
    def test_walk_forward_is_chronological_and_purged(self):
        folds = purged_walk_forward(_rows(80), train_size=30, test_size=10, purge_size=5)
        self.assertTrue(folds)
        for fold in folds:
            self.assertEqual(len(fold.train), 30)
            self.assertEqual(len(fold.test), 10)
            self.assertLess(fold.train[-1].timestamp, fold.test[0].timestamp)
            self.assertGreaterEqual(fold.test[0].timestamp - fold.train[-1].timestamp, 6)

    def test_label_aware_split_excludes_train_labels_crossing_test_start(self):
        rows = []
        for i in range(40):
            # Row 8 is decided before the first possible test but its outcome
            # is not known there, so the splitter must advance and exclude it
            # until a clean train window exists.
            label_end = float(i)
            if i == 8:
                label_end = 12.0
            rows.append(ValidationRow(
                float(i),
                0.6,
                1 if i % 2 else 0,
                1.0 if i % 2 else -1.0,
                label_end_timestamp=label_end,
            ))
        folds = label_aware_purged_embargo_walk_forward(
            rows,
            train_size=9,
            test_size=5,
            embargo_size=2,
        )
        self.assertTrue(folds)
        first = folds[0]
        self.assertEqual(first.test[0].timestamp, 10.0)
        self.assertNotIn(8.0, [row.timestamp for row in first.train])
        self.assertTrue(all(
            (row.label_end_timestamp or row.timestamp)
            < first.test[0].timestamp
            for row in first.train
        ))

    def test_label_aware_split_rejects_overlapping_step_and_embargo(self):
        with self.assertRaises(ValueError):
            label_aware_purged_embargo_walk_forward(
                _rows(80),
                train_size=30,
                test_size=10,
                embargo_size=3,
                step_size=10,
            )

    def test_brier_and_ece_are_bounded(self):
        vals = _rows(100)
        self.assertGreaterEqual(brier_score(vals), 0.0)
        self.assertLessEqual(brier_score(vals), 1.0)
        ece = expected_calibration_error(vals)
        self.assertGreaterEqual(ece, 0.0)
        self.assertLessEqual(ece, 1.0)

    def test_report_tracks_expectancy_in_r(self):
        rep = report(_rows(100))
        self.assertGreater(rep.expectancy_r, 0.0)
        self.assertAlmostEqual(rep.win_rate, 0.5)
        self.assertGreaterEqual(rep.log_loss, 0.0)

    def test_log_loss_and_reliability_bins_are_auditable(self):
        vals = _rows(100)
        self.assertGreaterEqual(log_loss(vals), 0.0)
        bins = reliability_bins(vals, bins=5)
        self.assertTrue(bins)
        self.assertEqual(sum(bucket["n"] for bucket in bins), len(vals))

    def test_aggregate_uses_only_test_rows(self):
        folds = purged_walk_forward(_rows(220), train_size=80, test_size=20, purge_size=5)
        rep = aggregate_oos_report(folds)
        self.assertEqual(rep.n, sum(len(f.test) for f in folds))

    def test_promotion_fails_closed_on_small_sample(self):
        rep = report(_rows(20))
        ok, blockers = promotion_decision(rep, min_samples=100)
        self.assertFalse(ok)
        self.assertIn("INSUFFICIENT_OOS_SAMPLE", blockers)

    def test_bad_confidence_is_rejected(self):
        bad = [ValidationRow(float(i), 0.95, 0, -1.0) for i in range(150)]
        rep = report(bad)
        ok, blockers = promotion_decision(rep)
        self.assertFalse(ok)
        self.assertIn("BRIER_TOO_HIGH", blockers)
        self.assertIn("NON_POSITIVE_EXPECTANCY", blockers)

    def test_invalid_probability_rejected(self):
        with self.assertRaises(ValueError):
            ValidationRow(1.0, 1.2, 1, 1.0).validate()


if __name__ == "__main__":
    unittest.main()
