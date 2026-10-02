import ast
import inspect
import unittest

import bot.model_h_v4 as v4
from bot.model_h_v4 import (
    Sample,
    align_panel,
    evaluate,
    fit_ridge,
    invariant_features,
)


def _rows(n=260, drift=0.0003, offset=0.0):
    out = []
    price = 100.0 + offset
    base = 1_700_000_000
    for i in range(n):
        o = price
        cyc = ((i % 13) - 6) * 0.00004
        c = o * (1 + drift + cyc)
        out.append({
            "ts": (base + i * 900) * 1000,
            "o": o,
            "h": max(o, c) * 1.001,
            "l": min(o, c) * 0.999,
            "c": c,
            "v": 1000 + (i % 7) * 20 + offset,
        })
        price = c
    return out


class ModelHV4Tests(unittest.TestCase):
    def test_panel_alignment_uses_exact_timestamp_intersection(self):
        a = _rows()
        b = _rows(offset=10)
        b.pop(100)
        panel = align_panel({"A": a, "B": b})
        self.assertEqual(len(panel["A"]), len(panel["B"]))
        self.assertEqual(len(panel["A"]), len(a) - 1)
        self.assertEqual(
            [x["ts"] for x in panel["A"]],
            [x["ts"] for x in panel["B"]],
        )

    def test_future_mutation_cannot_change_features(self):
        panel = align_panel({
            "A": _rows(offset=0),
            "B": _rows(drift=0.0001, offset=10),
            "C": _rows(drift=-0.0001, offset=20),
        })
        end = 180
        a, scale_a = invariant_features(panel, "A", end, horizon=2)
        mutated = {s: [dict(x) for x in rows] for s, rows in panel.items()}
        for rows in mutated.values():
            for x in rows[end:]:
                x["o"] *= 4
                x["h"] *= 4
                x["l"] *= 4
                x["c"] *= 4
                x["v"] *= 20
        b, scale_b = invariant_features(mutated, "A", end, horizon=2)
        self.assertEqual(a, b)
        self.assertEqual(scale_a, scale_b)

    def test_ridge_is_deterministic_and_zero_intercept(self):
        samples = [
            Sample("A", i, (float(i % 3), float((i + 1) % 4)), 0.2 * (i % 2 * 2 - 1),
                   0.001, True, 0.01, 0.001)
            for i in range(1, 20)
        ]
        a = fit_ridge(samples, 1.0)
        b = fit_ridge(samples, 1.0)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a.predict(a.means), 0.0, places=12)

    def test_cost_gate_cannot_create_more_signals(self):
        samples = [
            Sample("A", i, (float(i), float(i % 2)), 0.5, 0.01, True, 0.01, 0.0001)
            for i in range(1, 20)
        ]
        model = fit_ridge(samples, 0.1)
        cheap = evaluate(model, samples, threshold_z=0.0, cost_multiple=1.0)
        expensive_samples = [
            Sample(s.symbol, s.decision_ts, s.features, s.target_z, s.future_return,
                   s.actual_up, s.return_scale, 0.10)
            for s in samples
        ]
        costly = evaluate(model, expensive_samples, threshold_z=0.0, cost_multiple=1.0)
        self.assertGreaterEqual(sum(x.signaled for x in cheap), sum(x.signaled for x in costly))

    def test_module_has_no_execution_authority_or_prevalence_calibrator(self):
        source = inspect.getsource(v4)
        self.assertNotIn("place_order(", source)
        self.assertNotIn("TradingEngine", source)
        self.assertNotIn("Calibrator", source)
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
