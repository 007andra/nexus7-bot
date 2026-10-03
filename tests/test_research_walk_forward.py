import unittest

from bot.research_walk_forward import (
    assert_no_window_overlap,
    purged_walk_forward,
    validate_temporal_contract,
)


class ResearchWalkForwardTests(unittest.TestCase):
    def test_purged_walk_forward_enforces_gap_and_embargo(self):
        windows = purged_walk_forward(
            1000, train_size=400, test_size=100, purge=20, embargo=10,
            step_size=110,
        )
        self.assertGreaterEqual(len(windows), 4)
        self.assertEqual(windows[0].train_end, 400)
        self.assertEqual(windows[0].test_start, 420)
        self.assertGreaterEqual(
            windows[1].test_start, windows[0].test_end + 10
        )
        assert_no_window_overlap(windows)

    def test_expanding_window_keeps_train_origin(self):
        windows = purged_walk_forward(
            900, train_size=300, test_size=100, purge=10, embargo=20,
            expanding=True,
        )
        self.assertEqual(windows[0].train_start, 0)
        self.assertEqual(windows[1].train_start, 0)
        self.assertGreater(windows[1].train_end, windows[0].train_end)

    def test_temporal_contract_rejects_future_feature(self):
        rows = [{"decision_ts": 100, "feature_ts": 101, "label_ts": 120}]
        with self.assertRaisesRegex(ValueError, "lookahead"):
            validate_temporal_contract(rows)

    def test_temporal_contract_requires_future_label(self):
        rows = [{"decision_ts": 100, "feature_ts": 100, "label_ts": 100}]
        with self.assertRaisesRegex(ValueError, "non-future"):
            validate_temporal_contract(rows)


if __name__ == "__main__":
    unittest.main()
