import unittest

from bot.model_h_oos_evidence import evaluate_candles


def _candles(n=220, drift=0.001):
    out = []
    p = 100.0
    for i in range(n):
        o = p
        c = o * (1.0 + drift + ((i % 7) - 3) * 0.00005)
        out.append({
            "o": o,
            "h": max(o, c) * 1.001,
            "l": min(o, c) * 0.999,
            "c": c,
            "v": 1000.0 + (i % 9) * 10.0,
            "ts": (1_700_000_000 + i * 900) * 1000,
        })
        p = c
    return out


class ModelHOosEvidenceTests(unittest.TestCase):
    def test_evidence_is_deterministic(self):
        rows = _candles()
        a = evaluate_candles("TEST", rows, warmup=120, horizon=4, step=8, sample_count=32, round_trip_cost=0.001)
        b = evaluate_candles("TEST", rows, warmup=120, horizon=4, step=8, sample_count=32, round_trip_cost=0.001)
        self.assertEqual(a, b)
        self.assertGreater(a.samples, 0)
        self.assertTrue(0.0 <= a.coverage <= 1.0)
        self.assertTrue(0.0 <= a.brier_up <= 1.0)

    def test_costs_can_only_reduce_return(self):
        rows = _candles()
        gross = evaluate_candles("TEST", rows, warmup=120, horizon=4, step=8, sample_count=32, round_trip_cost=0.0)
        costly = evaluate_candles("TEST", rows, warmup=120, horizon=4, step=8, sample_count=32, round_trip_cost=0.002)
        self.assertEqual(gross.covered, costly.covered)
        self.assertLessEqual(costly.mean_net_return, gross.mean_net_return)

    def test_future_mutation_does_not_change_prior_evidence(self):
        rows = _candles()
        prefix = rows[:180]
        a = evaluate_candles("TEST", prefix, warmup=120, horizon=4, step=8, sample_count=32, round_trip_cost=0.001)
        mutated = [dict(x) for x in rows]
        for x in mutated[180:]:
            x["o"] *= 3
            x["h"] *= 3
            x["l"] *= 3
            x["c"] *= 3
        b = evaluate_candles("TEST", mutated[:180], warmup=120, horizon=4, step=8, sample_count=32, round_trip_cost=0.001)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
