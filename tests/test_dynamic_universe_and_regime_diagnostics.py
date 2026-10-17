import unittest

from bot.candidate_outcome_v2 import normalize_opportunity_rows
from bot.dynamic_universe_v1 import MarketQuality, rank_universe
from bot.edge_diagnostics_v1 import build_edge_diagnostics
from bot.regime_transition_dataset import (
    RegimePoint,
    build_transition_rows,
)


class DynamicUniverseTests(unittest.TestCase):
    def test_liquid_reliable_market_ranks_above_weak_market(self):
        strong = MarketQuality(
            "SOLUSDT",
            1_000_000_000,
            1,
            50_000_000,
            500_000_000,
            0.0001,
            3,
            0.95,
            1.0,
            365,
        )
        weak = MarketQuality(
            "XYZUSDT",
            1_000_000,
            20,
            10_000,
            100_000,
            0.01,
            20,
            0.3,
            0.7,
            5,
        )
        rows = rank_universe(
            [weak, strong],
            min_history_days=30,
            min_data_reliability=0.95,
        )
        self.assertEqual(rows[0]["symbol"], "SOLUSDT")
        self.assertTrue(rows[0]["eligible_for_research"])
        self.assertFalse(rows[1]["eligible_for_research"])
        self.assertEqual(rows[0]["execution_effect"], "NONE")


class EdgeDiagnosticTests(unittest.TestCase):
    def _row(self, key, *, setup, regime, symbol, side, approved, outcome):
        return {
            "signal_key": key,
            "created_epoch": 1700000000 + int(key[-1]),
            "symbol": symbol,
            "direction": side,
            "entry_type": setup,
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "strategy_score": 65,
            "nexus_score": 70 if approved else 40,
            "nexus_confidence": 60,
            "nexus_regime": regime,
            "approved": int(approved),
            "decision_reason": "x",
            "metadata": "{}",
            "p240_net_pct": outcome,
        }

    def test_setup_regime_symbol_and_side_are_separate(self):
        rows = normalize_opportunity_rows([
            self._row(
                "x1",
                setup="PULLBACK",
                regime="TREND",
                symbol="SOLUSDT",
                side="LONG",
                approved=True,
                outcome=2.0,
            ),
            self._row(
                "x2",
                setup="BOS_BREAK",
                regime="RANGE",
                symbol="BTCUSDT",
                side="SHORT",
                approved=False,
                outcome=-1.0,
            ),
        ])
        report = build_edge_diagnostics(rows, min_known_n=1)
        self.assertIn("PULLBACK", report["setup"])
        self.assertIn("RANGE", report["regime"])
        self.assertIn("BTCUSDT|SHORT", report["symbol_x_side"])
        self.assertEqual(report["execution_effect"], "NONE")


class RegimeTransitionTests(unittest.TestCase):
    def test_only_lagged_and_current_features_are_emitted(self):
        rows = build_transition_rows([
            RegimePoint(1, 20, 0.1, 1.0, 0.2, "RANGE"),
            RegimePoint(2, 30, 0.3, 1.5, 0.6, "TREND"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["regime_changed"])
        self.assertEqual(rows[0]["adx_delta"], 10)
        self.assertFalse(rows[0]["future_features_used"])
        self.assertNotIn("future_return", rows[0])
        self.assertEqual(rows[0]["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
