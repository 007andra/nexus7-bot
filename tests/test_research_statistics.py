import unittest

from bot.research_statistics import (
    bootstrap_mean_ci,
    max_drawdown,
    monte_carlo_trade_paths,
    performance_metrics,
)


class ResearchStatisticsTests(unittest.TestCase):
    def test_bootstrap_is_deterministic(self):
        values = [0.01, -0.005, 0.02, -0.01, 0.015] * 10
        a = bootstrap_mean_ci(values, n_bootstrap=1000, seed=7)
        b = bootstrap_mean_ci(values, n_bootstrap=1000, seed=7)
        self.assertEqual(a, b)
        self.assertLessEqual(a["low"], a["mean"])
        self.assertLessEqual(a["mean"], a["high"])

    def test_monte_carlo_is_deterministic_and_bounded(self):
        values = [0.01, -0.005, 0.02, -0.01] * 20
        a = monte_carlo_trade_paths(values, paths=500, seed=9)
        b = monte_carlo_trade_paths(values, paths=500, seed=9)
        self.assertEqual(a, b)
        self.assertGreaterEqual(a.median_max_drawdown, 0.0)
        self.assertLess(a.median_max_drawdown, 1.0)
        self.assertLessEqual(a.p05_terminal_return, a.median_terminal_return)
        self.assertLessEqual(a.median_terminal_return, a.p95_terminal_return)

    def test_monte_carlo_counts_first_trade_drawdown_from_initial_equity(self):
        summary = monte_carlo_trade_paths([-0.50], paths=10, seed=1)
        self.assertAlmostEqual(summary.median_max_drawdown, 0.50)

    def test_performance_metrics_include_tail_and_drawdown(self):
        metrics = performance_metrics([0.10, -0.05, 0.02, -0.03])
        self.assertEqual(metrics["trades"], 4)
        self.assertGreater(metrics["profit_factor"], 1.0)
        self.assertGreater(metrics["max_drawdown"], 0.0)
        self.assertLessEqual(metrics["cvar_5"], 0.0)

    def test_return_below_minus_100_rejected(self):
        with self.assertRaises(ValueError):
            max_drawdown([0.1, -1.1])


if __name__ == "__main__":
    unittest.main()
