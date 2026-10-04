"""Min-Order Universe Efficiency V1 research-only tests."""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import min_order_universe_efficiency_v1 as eff


def frontier_record(
    symbol, setup, classification, *, actual=1.0, maximum=0.2,
    gap=0.8, risk_gap=0.03, narrowed=True,
):
    return {
        "symbol": symbol,
        "setup": setup,
        "classification": classification,
        "actual_stop_pct": actual,
        "max_stop_pct": maximum,
        "stop_gap_pct": gap,
        "risk_gap_usdt": risk_gap,
        "would_pass_if_stop_narrowed": narrowed,
    }


def universe_row(symbol, status, max_stop, *, binding="MIN_NOTIONAL_BINDING"):
    return {
        "symbol": symbol,
        "status": status,
        "binding": binding,
        "price": 1.0,
        "min_qty": 1.0,
        "min_notional": 5.0,
        "min_valid_qty": 5.0,
        "min_order_notional": 5.0,
        "risk_budget": 0.02,
        "margin_at_min": 0.1,
        "margin_cap": 2.0,
        "max_stop_pct": max_stop,
        "fee_rate_per_side": 0.0006,
        "slippage_pct": 0.001,
    }


class ReportClassification(unittest.TestCase):
    def test_separates_cost_stop_width_observed_compatible_and_no_candidate(self):
        universe = [
            universe_row("BTCUSDT", "COST_BLOCK", 0.0, binding="MIN_QTY_BINDING"),
            universe_row("SOLUSDT", "CONDITIONAL", 0.14),
            universe_row("XRPUSDT", "CONDITIONAL", 0.21),
            universe_row("ADAUSDT", "CONDITIONAL", 0.20),
        ]
        frontier = {
            "epoch_id": "REENTRY_V1_20261004_R2",
            "started_epoch": 1000.0,
            "feasible": 1,
            "near_feasible": 0,
            "structurally_blocked": 2,
            "unknown": 0,
            "records": [
                frontier_record("BTCUSDT", "BOS", "STRUCTURALLY_BLOCKED",
                                maximum=0.0, gap=1.0, narrowed=False),
                frontier_record("SOLUSDT", "MOMENTUM", "STRUCTURALLY_BLOCKED",
                                actual=1.0, maximum=0.14, gap=0.86),
                frontier_record("XRPUSDT", "PULLBACK", "FEASIBLE",
                                actual=0.18, maximum=0.21, gap=-0.03, risk_gap=-0.002,
                                narrowed=False),
            ],
        }
        report = eff.build_report(universe, frontier)
        by_symbol = {r["symbol"]: r for r in report["symbol_rows"]}
        self.assertEqual(by_symbol["BTCUSDT"]["efficiency_class"], "COST_BLOCK")
        self.assertEqual(by_symbol["SOLUSDT"]["efficiency_class"], "STOP_WIDTH_BLOCK")
        self.assertEqual(
            by_symbol["XRPUSDT"]["efficiency_class"],
            "CAPITAL_COMPATIBLE_OBSERVED",
        )
        self.assertEqual(
            by_symbol["ADAUSDT"]["efficiency_class"],
            "CONDITIONAL_NO_ACTIVE_CANDIDATE",
        )
        self.assertEqual(report["active_epoch_candidates"], 3)
        self.assertEqual(report["active_setup_groups"], 3)
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")

    def test_setup_groups_rank_observed_compatibility_without_policy_changes(self):
        universe = [
            universe_row("SOLUSDT", "CONDITIONAL", 0.2),
            universe_row("XRPUSDT", "CONDITIONAL", 0.2),
        ]
        frontier = {
            "records": [
                frontier_record("SOLUSDT", "MOMENTUM", "STRUCTURALLY_BLOCKED",
                                actual=1.0, maximum=0.1),
                frontier_record("SOLUSDT", "MOMENTUM", "STRUCTURALLY_BLOCKED",
                                actual=0.8, maximum=0.1),
                frontier_record("XRPUSDT", "PULLBACK", "FEASIBLE",
                                actual=0.15, maximum=0.2, gap=-0.05,
                                risk_gap=-0.001, narrowed=False),
            ]
        }
        report = eff.build_report(universe, frontier)
        self.assertEqual(report["setup_rows"][0]["symbol"], "XRPUSDT")
        self.assertEqual(
            report["setup_rows"][0]["observed_class"],
            "CAPITAL_COMPATIBLE_OBSERVED",
        )
        sol = next(r for r in report["setup_rows"] if r["symbol"] == "SOLUSDT")
        self.assertEqual(sol["observed_class"], "STOP_WIDTH_BLOCK")
        self.assertAlmostEqual(sol["median_frontier_ratio"], 0.1125)

    def test_near_feasible_is_kept_distinct(self):
        universe = [universe_row("ARBUSDT", "CONDITIONAL", 0.21)]
        frontier = {
            "records": [
                frontier_record("ARBUSDT", "PULLBACK", "NEAR_FEASIBLE",
                                actual=0.22, maximum=0.20, gap=0.02),
            ]
        }
        report = eff.build_report(universe, frontier)
        self.assertEqual(
            report["symbol_rows"][0]["efficiency_class"],
            "STOP_WIDTH_BLOCK_NEAR",
        )


class SnapshotIsolation(unittest.TestCase):
    def test_snapshot_uses_only_cached_prices_and_existing_research(self):
        from bot import min_order_feasibility_matrix as matrix
        from bot import min_order_frontier_audit_v1 as frontier

        class Client:
            def __init__(self):
                self.calls = []
            def get_cached_ticker(self, symbol):
                self.calls.append(symbol)
                return {"lastPrice": "123.45"}

        engine = SimpleNamespace(client=Client())
        front = {
            "epoch_id": "REENTRY_V1_20261004_R2",
            "started_epoch": 1000.0,
            "records": [],
            "feasible": 0,
            "near_feasible": 0,
            "structurally_blocked": 0,
            "unknown": 0,
        }
        matrix_rows = [universe_row("BTCUSDT", "COST_BLOCK", 0.0)]
        with patch.object(frontier, "snapshot", new_callable=AsyncMock,
                          return_value=front) as fs, \
             patch.object(matrix, "build_matrix", return_value=matrix_rows) as bm, \
             patch("bot.config.cfg.SYMBOLS", ["BTCUSDT"]):
            report = asyncio.run(eff.snapshot(object(), engine))
        fs.assert_awaited_once()
        bm.assert_called_once()
        _, price_map = bm.call_args.args
        self.assertEqual(price_map, {"BTCUSDT": 123.45})
        self.assertEqual(engine.client.calls, ["BTCUSDT"])
        self.assertEqual(report["universe_symbols"], 1)

    def test_formatters_restate_non_authority(self):
        report = eff.build_report(
            [universe_row("SOLUSDT", "CONDITIONAL", 0.14)],
            {"epoch_id": "R2", "records": []},
        )
        for line in (
            eff.format_summary(report),
            eff.format_top_symbols(report),
            eff.format_top_setups(report),
        ):
            self.assertIn("decision_effect=NONE", line)
            self.assertIn("execution_effect=NONE", line)

    def test_flag_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(eff.enabled())


if __name__ == "__main__":
    unittest.main()
