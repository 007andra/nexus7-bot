"""MIN_ORDER Capital Adequacy V1 tests."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from bot import min_order_capital_adequacy_v1 as capital


def candidate(
    cid="c1", *, symbol="SOLUSDT", setup="BOS_BREAK",
    entry=100.0, stop=99.5, risk_pct=0.0025,
    risk_budget=0.02, risk_at_min=0.10, required_equity=40.0,
    pullback=True, funnel=True, feasible=False,
    binding="MIN_NOTIONAL_BINDING", captured=2000.0,
):
    return {
        "candidate_id": cid,
        "captured_epoch": captured,
        "symbol": symbol,
        "side": "LONG",
        "setup": setup,
        "regime": "TRENDING_UP",
        "entry": entry,
        "stop": stop,
        "counterfactual_risk_pct": risk_pct,
        "risk_budget": risk_budget,
        "min_valid_qty": 1.0,
        "risk_at_min_qty": risk_at_min,
        "required_equity_at_min_qty": required_equity,
        "shadow_min_order_feasible": feasible,
        "binding": binding,
        "capital_source": "AUTHENTICATED_ACCOUNT_CACHE",
        "pullback_pass": pullback,
        "production_equivalent_funnel_result": funnel,
        "cost_snapshot": {
            "taker_fee": 0.0006,
            "entry_slippage": 0.0005,
            "exit_slippage": 0.0005,
        },
    }


class CapitalMath(unittest.TestCase):
    def test_quantifies_pipeline_min_order_only_blocker(self):
        report = capital.build_report([candidate()])
        self.assertEqual(report["status"], "MIN_ORDER_ONLY_BLOCKERS_QUANTIFIED")
        self.assertEqual(report["pipeline_before_min_order"], 1)
        self.assertEqual(report["min_order_only_blocked"], 1)
        self.assertAlmostEqual(report["current_equity"], 8.0)
        self.assertAlmostEqual(report["required_equity"]["min"], 40.0)
        self.assertAlmostEqual(report["equity_gap"]["min"], 32.0)
        self.assertAlmostEqual(report["required_equity_multiple"]["min"], 5.0)
        self.assertTrue(report["external_capital_does_not_clear_drawdown"])
        self.assertTrue(report["capital_metric_is_counterfactual_not_recommendation"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])

    def test_earlier_gate_failure_is_not_counted_as_min_order_only(self):
        rows = [
            candidate("pullback", pullback=False),
            candidate("funnel", funnel=False),
        ]
        report = capital.build_report(rows)
        self.assertEqual(report["min_order_only_blocked"], 0)
        self.assertEqual(
            report["status"],
            "AWAITING_PIPELINE_MIN_ORDER_ONLY_CANDIDATE",
        )

    def test_feasible_candidate_is_not_a_blocker(self):
        report = capital.build_report([candidate("pass", feasible=True)])
        self.assertEqual(report["pipeline_before_min_order"], 1)
        self.assertEqual(report["min_order_only_blocked"], 0)

    def test_falls_back_to_risk_at_min_divided_by_risk_pct(self):
        raw = candidate(required_equity=None, risk_at_min=0.125, risk_pct=0.0025)
        report = capital.build_report([raw])
        self.assertAlmostEqual(report["required_equity"]["min"], 50.0)

    def test_groups_and_ranks_lowest_required_equity(self):
        rows = [
            candidate("a", symbol="ADAUSDT", setup="MOMENTUM", required_equity=60),
            candidate("b", symbol="SOLUSDT", setup="BOS_BREAK", required_equity=24),
            candidate("c", symbol="SOLUSDT", setup="BOS_BREAK", required_equity=32),
        ]
        report = capital.build_report(rows)
        self.assertEqual(report["closest_candidate"]["candidate_id"], "b")
        self.assertEqual(report["setup_rows"][0]["symbol"], "SOLUSDT")
        self.assertEqual(report["setup_rows"][0]["setup"], "BOS_BREAK")
        self.assertEqual(report["setup_rows"][0]["candidates"], 2)
        self.assertAlmostEqual(report["setup_rows"][0]["required_equity"]["median"], 28.0)

    def test_formatters_restate_research_only_authority(self):
        report = capital.build_report([candidate()])
        summary = capital.format_summary(report)
        top = capital.format_top_setups(report)
        self.assertIn("[MIN_ORDER_CAPITAL_ADEQUACY_V1]", summary)
        self.assertIn("capital_metric_is_counterfactual_not_recommendation=true", summary)
        self.assertIn("promotion_allowed=false", summary)
        self.assertIn("live_allowed=false", summary)
        self.assertIn("decision_effect=NONE execution_effect=NONE", summary)
        self.assertIn("observability_only=true", top)

    def test_flag_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(capital.enabled())


class FakeDB:
    async def _fetchall(self, sql, args=()):
        if "risk_epoch_shadow_v1" in sql:
            return [{"payload": json.dumps({
                "epoch_id": "REENTRY_V1_20261004_R2",
                "started_epoch": 1500.0,
            })}]
        if "hard_gate_shadow_candidates_v1" in sql:
            assert args == ("HARD_GATE_SHADOW", 1500.0)
            return [{"payload": json.dumps(candidate("db", captured=2000.0))}]
        raise AssertionError(sql)


class SnapshotIsolation(unittest.TestCase):
    def test_snapshot_is_active_epoch_only(self):
        with patch.dict(os.environ, {
            "RISK_EPOCH_SHADOW_ID": "REENTRY_V1_20261004_R2",
        }):
            report = asyncio.run(capital.snapshot(FakeDB()))
        self.assertEqual(report["epoch_id"], "REENTRY_V1_20261004_R2")
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(report["min_order_only_blocked"], 1)


if __name__ == "__main__":
    unittest.main()
