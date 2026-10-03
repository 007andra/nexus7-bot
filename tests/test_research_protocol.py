import unittest

from bot.research_protocol import (
    ResearchObservation,
    segmented_report,
    walk_forward_oos_report,
)


def _rows(n=80):
    out = []
    for i in range(n):
        out.append(ResearchObservation(
            timestamp=1000 + i,
            symbol="BTCUSDT" if i % 2 == 0 else "ETHUSDT",
            side="LONG" if i % 3 else "SHORT",
            regime="TREND" if i % 4 else "RANGE",
            volatility=float(i % 10) / 100.0,
            net_return=0.009 if i % 3 else -0.006,
            gross_return=0.01 if i % 3 else -0.005,
            fee_drag=0.0005,
            slippage_drag=0.0005,
            funding_pnl=0.0,
            turnover=1.0,
            exposure_fraction=0.5,
        ))
    return out


class ResearchProtocolTests(unittest.TestCase):
    def test_segmented_report_has_required_dimensions(self):
        report = segmented_report(_rows(30), bootstrap_samples=100)
        self.assertEqual(report["n"], 30)
        self.assertIn("symbol", report["segments"])
        self.assertIn("side", report["segments"])
        self.assertIn("regime", report["segments"])
        self.assertIn("volatility_tercile", report["segments"])

    def test_walk_forward_report_is_chronological_and_purged(self):
        report = walk_forward_oos_report(
            _rows(), train_size=30, test_size=10, purge=2, embargo=2,
            bootstrap_samples=100, monte_carlo_paths=100,
        )
        self.assertGreaterEqual(report["fold_count"], 3)
        self.assertTrue(report["protocol"]["chronological"])
        self.assertTrue(report["protocol"]["purged"])
        self.assertFalse(report["protocol"]["shuffled"])
        self.assertGreater(report["oos_n"], 0)
        self.assertGreaterEqual(report["fold_count"], 4)
        self.assertTrue(report["evidence_complete"])
        self.assertEqual(report["evidence_blockers"], ())


if __name__ == "__main__":
    unittest.main()
