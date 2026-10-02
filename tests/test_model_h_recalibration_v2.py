import unittest

from bot.model_h_recalibration_v2 import (
    PROFILES,
    _bootstrap_ci,
    evaluate_indices,
    learn_context_whitelist,
    metrics,
    split_bounds,
)


def _candles(n=700, drift=0.0004):
    rows = []
    p = 100.0
    for i in range(n):
        o = p
        cyc = ((i % 19) - 9) * 0.00003
        c = o * (1.0 + drift + cyc)
        rows.append({
            "o": o,
            "h": max(o, c) * 1.001,
            "l": min(o, c) * 0.999,
            "c": c,
            "v": 1000.0 + (i % 13) * 15.0,
            "ts": (1_700_000_000 + i * 900) * 1000,
        })
        p = c
    return rows


class ModelHRecalibrationV2Tests(unittest.TestCase):
    def test_split_reserves_untouched_suffix(self):
        warm, train_end, val_end = split_bounds(1000)
        self.assertGreaterEqual(train_end, warm)
        self.assertGreater(val_end, train_end)
        self.assertLess(val_end, 1000)

    def test_bootstrap_is_deterministic(self):
        vals = [0.01, -0.02, 0.03, 0.01, 0.02]
        self.assertEqual(_bootstrap_ci(vals), _bootstrap_ci(vals))

    def test_cost_reduces_signal_net_return(self):
        rows = _candles()
        idx = list(range(400, 560, 32))
        free = evaluate_indices(
            "TEST", rows, idx, horizon=4, profile=PROFILES[0],
            round_trip_cost=0.0, sample_count=16,
        )
        costly = evaluate_indices(
            "TEST", rows, idx, horizon=4, profile=PROFILES[0],
            round_trip_cost=0.002, sample_count=16,
        )
        free_m, costly_m = metrics(free), metrics(costly)
        self.assertEqual(free_m["signals"], costly_m["signals"])
        self.assertLessEqual(costly_m["mean_net_return"], free_m["mean_net_return"])

    def test_test_suffix_cannot_change_validation_rows(self):
        rows = _candles()
        idx = list(range(400, 520, 32))
        a = evaluate_indices(
            "TEST", rows[:600], idx, horizon=4, profile=PROFILES[0],
            round_trip_cost=0.001, sample_count=16,
        )
        mutated = [dict(x) for x in rows]
        for x in mutated[600:]:
            x["o"] *= 4
            x["h"] *= 4
            x["l"] *= 4
            x["c"] *= 4
        b = evaluate_indices(
            "TEST", mutated[:600], idx, horizon=4, profile=PROFILES[0],
            round_trip_cost=0.001, sample_count=16,
        )
        self.assertEqual(a, b)

    def test_context_whitelist_uses_only_signaled_positive_groups(self):
        rows = _candles()
        idx = list(range(400, 640, 16))
        obs = evaluate_indices(
            "TEST", rows, idx, horizon=4, profile=PROFILES[0],
            round_trip_cost=0.0, sample_count=16,
        )
        allowed = learn_context_whitelist(obs, min_signals=1)
        for key in allowed:
            vals = [r.net_return for r in obs if r.signaled and r.context == key]
            self.assertTrue(vals)
            self.assertGreater(sum(vals) / len(vals), 0.0)


if __name__ == "__main__":
    unittest.main()
