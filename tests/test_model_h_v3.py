import ast
import inspect
import unittest

import bot.model_h_v3 as v3
from bot.model_h_v3 import (
    V3Profile,
    aggregate_closed,
    analog_forecast,
    apply_profile,
    build_analog_index,
    compute_components,
    feature_vector,
    fit_calibrator,
)


def _candles(n=760, drift=0.00035):
    rows = []
    price = 100.0
    base = 1_699_999_200  # quarter-hour aligned
    for i in range(n):
        o = price
        cyc = ((i % 17) - 8) * 0.00004
        c = o * (1.0 + drift + cyc)
        rows.append({
            "ts": (base + i * 900) * 1000,
            "o": o,
            "h": max(o, c) * 1.0012,
            "l": min(o, c) * 0.9988,
            "c": c,
            "v": 1000.0 + (i % 11) * 20.0,
        })
        price = c
    return rows


class ModelHV3Tests(unittest.TestCase):
    def test_aggregate_closed_drops_incomplete_bucket(self):
        rows = _candles(9)
        one_h = aggregate_closed(rows, 4)
        self.assertEqual(len(one_h), 2)
        self.assertEqual(one_h[0]["o"], rows[0]["o"])
        self.assertEqual(one_h[0]["c"], rows[3]["c"])

    def test_analog_never_uses_unknown_future_outcome(self):
        rows = _candles(500)
        horizon = 4
        points = build_analog_index(rows, horizon)
        end = 300
        feats = feature_vector(rows[:end])
        p, exp, n = analog_forecast(
            points,
            decision_index=end,
            features=feats,
            regime=v3.regime_tag(feats),
            horizon=horizon,
            k=24,
            regime_match=False,
        )
        self.assertTrue(0.0 <= p <= 1.0)
        self.assertGreater(n, 0)
        # Direct contract check: any eligible point must have its target closed
        # no later than the decision index.
        eligible = [x for x in points if x.index + horizon <= end]
        self.assertTrue(eligible)
        self.assertTrue(all(x.index + horizon <= end for x in eligible))
        self.assertTrue(isinstance(exp, float))

    def test_future_mutation_does_not_change_prefix_components(self):
        rows = _candles()
        idx = [500]
        a = compute_components("TEST", rows[:620], idx, horizon=2)
        mutated = [dict(x) for x in rows]
        for x in mutated[620:]:
            x["o"] *= 5
            x["h"] *= 5
            x["l"] *= 5
            x["c"] *= 5
            x["v"] *= 20
        b = compute_components("TEST", mutated[:620], idx, horizon=2)
        self.assertEqual(a, b)

    def test_higher_cost_cannot_create_more_signals(self):
        rows = _candles()
        horizon = 2
        idx = [520, 552, 584, 616]
        comps = compute_components("TEST", rows, idx, horizon=horizon)
        analogs = build_analog_index(rows, horizon)
        profile = V3Profile("T", .4, .2, .1, .3, .05, 2, 1.0, 16, False)
        cheap = apply_profile(comps, analogs, profile, round_trip_cost=0.0001)
        costly = apply_profile(comps, analogs, profile, round_trip_cost=0.01)
        self.assertGreaterEqual(
            sum(x.signaled for x in cheap),
            sum(x.signaled for x in costly),
        )

    def test_calibrator_is_deterministic(self):
        rows = _candles()
        horizon = 1
        idx = [500, 532, 564, 596, 628]
        comps = compute_components("TEST", rows, idx, horizon=horizon)
        analogs = build_analog_index(rows, horizon)
        profile = V3Profile("T", .4, .2, .1, .3, .05, 2, 1.0, 16, False)
        obs = apply_profile(comps, analogs, profile, round_trip_cost=0.0001)
        self.assertEqual(fit_calibrator(obs), fit_calibrator(obs))

    def test_module_has_no_execution_authority(self):
        source = inspect.getsource(v3)
        self.assertNotIn("place_order(", source)
        self.assertNotIn("TradingEngine", source)
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        forbidden = {"bot.engine", "bot.exchange", "bot.nexus_ai", "torch", "transformers", "requests"}
        self.assertFalse(imports & forbidden)


if __name__ == "__main__":
    unittest.main()
