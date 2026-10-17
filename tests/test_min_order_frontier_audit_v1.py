"""MIN_ORDER Frontier Audit V1 research-only tests."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from bot import min_order_frontier_audit_v1 as audit


def row(
    cid="c1", *,
    entry=100.0, stop=99.0, risk_budget=1.0, min_qty=1.0,
    risk_at_min=1.22, risk_pct=0.0025, feasible=False,
    binding="MIN_NOTIONAL_BINDING", capital_source="AUTHENTICATED_ACCOUNT_CACHE",
    captured=2000.0, margin_at=None, margin_cap=None,
):
    return {
        "candidate_id": cid,
        "captured_epoch": captured,
        "symbol": "SOLUSDT",
        "side": "LONG",
        "setup": "MOMENTUM",
        "regime": "TRENDING_UP",
        "entry": entry,
        "stop": stop,
        "counterfactual_risk_pct": risk_pct,
        "risk_budget": risk_budget,
        "min_valid_qty": min_qty,
        "risk_at_min_qty": risk_at_min,
        "shadow_min_order_feasible": feasible,
        "binding": binding,
        "capital_source": capital_source,
        "margin_at_min_qty": margin_at,
        "margin_cap": margin_cap,
        "cost_snapshot": {
            "taker_fee": 0.0006,
            "entry_slippage": 0.0005,
            "exit_slippage": 0.0005,
        },
    }


class FrontierMath(unittest.TestCase):
    def test_feasible_stays_feasible_and_never_authorizes_live(self):
        rec = audit.classify_candidate(row(feasible=True, stop=99.5, risk_at_min=0.72))
        self.assertEqual(rec["classification"], "FEASIBLE")
        self.assertFalse(rec["promotion_allowed"])
        self.assertFalse(rec["live_allowed"])
        self.assertEqual(rec["decision_effect"], "NONE")
        self.assertEqual(rec["execution_effect"], "NONE")

    def test_near_feasible_is_within_pre_registered_25_percent_frontier(self):
        # fixed cost = 0.22 USDT/unit; risk budget 1.0 => max stop 0.78%.
        rec = audit.classify_candidate(row(stop=99.2, risk_at_min=1.02))
        self.assertAlmostEqual(rec["actual_stop_pct"], 0.8)
        self.assertAlmostEqual(rec["max_stop_pct"], 0.78)
        self.assertAlmostEqual(rec["stop_gap_pct"], 0.02)
        self.assertEqual(rec["classification"], "NEAR_FEASIBLE")
        self.assertTrue(rec["would_pass_if_stop_narrowed"])

    def test_large_stop_gap_is_structurally_blocked_at_current_geometry(self):
        rec = audit.classify_candidate(row(stop=99.0, risk_at_min=1.22))
        self.assertEqual(rec["classification"], "STRUCTURALLY_BLOCKED")
        self.assertEqual(rec["classification_reason"], "STOP_WIDTH_ABOVE_FRONTIER")
        self.assertTrue(rec["would_pass_if_stop_narrowed"])
        self.assertGreater(rec["required_stop_reduction_pct_of_current"], 20.0)

    def test_fixed_cost_floor_can_make_any_positive_stop_impossible(self):
        rec = audit.classify_candidate(row(risk_budget=0.10, stop=99.9, risk_at_min=0.32))
        self.assertEqual(rec["max_stop_pct"], 0.0)
        self.assertEqual(rec["classification"], "STRUCTURALLY_BLOCKED")
        self.assertEqual(
            rec["classification_reason"],
            "FIXED_COST_FLOOR_EXCEEDS_RISK_BUDGET",
        )
        self.assertFalse(rec["would_pass_if_stop_narrowed"])

    def test_unconfirmed_capital_is_unknown_not_pass(self):
        rec = audit.classify_candidate(row(
            risk_budget=None, min_qty=None, risk_at_min=None,
            feasible=None, capital_source="UNCONFIRMED",
        ))
        self.assertEqual(rec["classification"], "UNKNOWN")
        self.assertEqual(rec["classification_reason"], "CAPITAL_UNCONFIRMED")

    def test_exact_margin_context_is_counted_when_present(self):
        report = audit.build_report([
            row("a", margin_at=0.12, margin_cap=2.18),
            row("b", margin_at=None, margin_cap=None),
        ], epoch_id="R2", started_epoch=1000)
        self.assertEqual(report["candidates"], 2)
        self.assertEqual(report["exact_margin_context_candidates"], 1)
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])


class FakeDB:
    def __init__(self):
        self.calls = []

    async def _fetchall(self, sql, args=()):
        self.calls.append((sql, args))
        if "risk_epoch_shadow_v1" in sql:
            return [{"payload": json.dumps({
                "epoch_id": "REENTRY_V1_20261004_R2",
                "started_epoch": 1500.0,
            })}]
        if "hard_gate_shadow_candidates_v1" in sql:
            self.last_candidate_args = args
            return [
                {"payload": json.dumps(row("in-epoch", captured=2000.0))},
            ]
        raise AssertionError(sql)


class SnapshotIsolation(unittest.TestCase):
    def test_snapshot_is_bounded_to_active_epoch(self):
        db = FakeDB()
        with patch.dict(os.environ, {
            "RISK_EPOCH_SHADOW_ID": "REENTRY_V1_20261004_R2",
        }):
            report = asyncio.run(audit.snapshot(db))
        self.assertEqual(report["epoch_id"], "REENTRY_V1_20261004_R2")
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(
            db.last_candidate_args,
            ("HARD_GATE_SHADOW", 1500.0),
        )
        self.assertIn("captured_epoch>=?", db.calls[-1][0])

    def test_summary_is_explicitly_non_authoritative(self):
        report = audit.build_report([row()], epoch_id="R2", started_epoch=1000)
        line = audit.format_summary(report)
        self.assertIn("[MIN_ORDER_FRONTIER_AUDIT_V1]", line)
        self.assertIn("promotion_allowed=false", line)
        self.assertIn("live_allowed=false", line)
        self.assertIn("decision_effect=NONE execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
