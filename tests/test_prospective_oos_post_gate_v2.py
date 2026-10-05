"""Regression and isolation proof for prospective post-gate OOS V2."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from bot import prospective_oos_post_gate_v2 as v2
from bot import hard_gate_shadow_scan as legacy


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "bot"
    / "prospective_oos_post_gate_v2.py"
).read_text(encoding="utf-8")


class DB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    async def _exec(self, sql, args=()):
        self.conn.execute(sql, args)
        self.conn.commit()
        return True

    async def _fetchall(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()


class Client:
    def get_cached_klines(self, symbol, interval, limit):
        step = {"15": 900, "60": 3600, "240": 14400}[interval]
        return [
            {
                "ts": (1_800_000_000 - (limit - i) * step) * 1000,
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.5,
                "v": 1000.0,
            }
            for i in range(limit)
        ]

    def get_cached_ticker(self, symbol):
        return {"bid1Price": "100", "ask1Price": "100.01", "lastPrice": "100"}


def engine():
    return NS(
        client=Client(),
        viable_symbols=["FILUSDT"],
        daily_target_hit=False,
        _session_score_adjustment=lambda symbol, score: score,
        _regime_allows_direction=lambda regime, direction: True,
    )


class PostGateV2(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB()
        self.addCleanup(self.db.conn.close)
        await v2._ensure_schema(self.db)

    async def test_candidate_persistence_is_separate_from_v1(self):
        row = {
            **v2.AUTHORITY,
            "candidate_id": "POST_GATE_SHADOW_V2:FILUSDT:LONG:BOS_BREAK:1",
            "captured_epoch": 1234.0,
            "symbol": "FILUSDT",
            "side": "LONG",
            "entry": 100.0,
            "counterfactual_nexus_v1": {
                "cohort": v2.COHORT,
                "candidate_id": "POST_GATE_SHADOW_V2:FILUSDT:LONG:BOS_BREAK:1",
                "risk_epoch_traversal_credit": False,
                "execution_allowed": True,
            },
        }
        await v2._persist_candidate(self.db, row)

        own = await self.db._fetchall(
            "SELECT candidate_id,population,payload FROM post_gate_shadow_candidates_v2"
        )
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["population"], v2.POPULATION)
        payload = json.loads(own[0]["payload"])
        self.assertTrue(payload["post_gate_v2"])
        self.assertFalse(payload["live_eligible"])
        self.assertEqual(payload["execution_effect"], "NONE")

        tables = {
            r["name"]
            for r in await self.db._fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertNotIn("hard_gate_shadow_candidates_v1", tables)

    async def test_collect_batch_can_only_persist_research_candidate(self):
        sig = NS(
            symbol="FILUSDT",
            direction="LONG",
            entry_type="BOS_BREAK",
            regime="TRENDING_UP",
            score=80,
            expected_pnl=1.0,
            entry=100.0,
            sl=98.0,
            tp=104.0,
        )
        decision = NS(
            execution_allowed=True,
            decision="LONG",
            reasoning=["approved"],
            confidence=80.0,
            setup_quality=80.0,
            risk_reward=2.0,
            expected_value=0.5,
            _bgx_score_snapshot={"fusion_confidence": 80.0, "rr_net": 2.0},
        )
        snap = NS(
            taker_fee=0.0005,
            entry_slippage=0.001,
            exit_slippage=0.001,
            spread_bps=1.0,
            fee_source="test",
            slippage_source="test",
        )
        features = {
            "funding": None,
            "oi": None,
            "oi_delta": None,
            "news_score": None,
            "evaluation_fidelity": "DEGRADED",
            "missing_features": ["FUNDING"],
        }
        minimum_order = {
            "capital_source": "RISK_V3_CONFIRMED",
            "shadow_min_order_feasible": False,
            "binding": "MIN_NOTIONAL_BINDING",
            "required_equity_at_min_qty": 40.0,
            "counterfactual_risk_pct": 0.0025,
        }

        compute = AsyncMock(side_effect=[sig, decision])
        with patch.object(v2, "_post_gate_active", return_value=True), \
             patch.object(legacy, "_compute", compute), \
             patch.object(legacy, "_cost", return_value=snap), \
             patch.object(legacy, "_cached_optional_features", return_value=features), \
             patch.object(legacy, "counterfactual_min_order", return_value=minimum_order):
            stats = await v2._collect_batch(engine(), self.db)

        self.assertEqual(stats["symbols_examined"], 1)
        self.assertEqual(stats["signals"], 1)
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["persisted"], 1)

        rows = await self.db._fetchall(
            "SELECT payload FROM post_gate_shadow_candidates_v2"
        )
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["population"], v2.POPULATION)
        self.assertTrue(payload["research_only"])
        self.assertTrue(payload["shadow_only"])
        self.assertFalse(payload["live_eligible"])
        self.assertFalse(payload["live_candidate"])
        self.assertEqual(payload["decision_effect"], "NONE")
        self.assertEqual(payload["execution_effect"], "NONE")

    async def test_reblocked_gate_prevents_candidate_persist(self):
        sig = NS(
            symbol="FILUSDT",
            direction="LONG",
            entry_type="BOS_BREAK",
            regime="TRENDING_UP",
            score=80,
            expected_pnl=1.0,
            entry=100.0,
            sl=98.0,
            tp=104.0,
        )
        decision = NS(
            execution_allowed=True,
            decision="LONG",
            reasoning=["approved"],
            confidence=80.0,
            setup_quality=80.0,
            risk_reward=2.0,
            expected_value=0.5,
            _bgx_score_snapshot={"fusion_confidence": 80.0, "rr_net": 2.0},
        )
        snap = NS(
            taker_fee=0.0005,
            entry_slippage=0.001,
            exit_slippage=0.001,
            spread_bps=1.0,
            fee_source="test",
            slippage_source="test",
        )
        features = {
            "funding": None,
            "oi": None,
            "oi_delta": None,
            "news_score": None,
            "evaluation_fidelity": "DEGRADED",
            "missing_features": [],
        }
        minimum_order = {
            "capital_source": "RISK_V3_CONFIRMED",
            "shadow_min_order_feasible": False,
            "binding": "MIN_NOTIONAL_BINDING",
            "required_equity_at_min_qty": 40.0,
            "counterfactual_risk_pct": 0.0025,
        }
        compute = AsyncMock(side_effect=[sig, decision])

        with patch.object(v2, "_post_gate_active", side_effect=[True, False]), \
             patch.object(legacy, "_compute", compute), \
             patch.object(legacy, "_cost", return_value=snap), \
             patch.object(legacy, "_cached_optional_features", return_value=features), \
             patch.object(legacy, "counterfactual_min_order", return_value=minimum_order):
            stats = await v2._collect_batch(engine(), self.db)

        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["persisted"], 0)
        rows = await self.db._fetchall(
            "SELECT candidate_id FROM post_gate_shadow_candidates_v2"
        )
        self.assertEqual(rows, [])

    async def test_v2_snapshot_has_distinct_frozen_cohort(self):
        baseline = await v2.ensure_cohort(self.db, started_epoch=1000.0)
        self.assertEqual(baseline["cohort_id"], v2.COHORT_ID)
        self.assertEqual(baseline["population"], v2.POPULATION)
        self.assertTrue(baseline["hypothesis_frozen"])
        self.assertFalse(baseline["reset_allowed"])
        self.assertTrue(baseline["v1_population_untouched"])
        self.assertTrue(baseline["v1_cohort_id_untouched"])

        report = await v2.snapshot(self.db)
        self.assertEqual(report["cohort_id"], v2.COHORT_ID)
        self.assertEqual(report["population"], v2.POPULATION)
        self.assertTrue(report["v1_untouched"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_allowed"])

    async def test_snapshot_fails_closed_on_invalid_authority_row(self):
        await v2.ensure_cohort(self.db, started_epoch=1000.0)
        await self.db._exec(
            "INSERT INTO post_gate_shadow_candidates_v2 "
            "(candidate_id,captured_epoch,symbol,population,payload) VALUES (?,?,?,?,?)",
            (
                "bad-row",
                1100.0,
                "FILUSDT",
                v2.POPULATION,
                json.dumps(
                    {
                        "candidate_id": "bad-row",
                        "captured_epoch": 1100.0,
                        "symbol": "FILUSDT",
                        "population": v2.POPULATION,
                        "post_gate_v2": True,
                        "shadow_only": True,
                        "live_eligible": True,
                        "execution_effect": "NONE",
                    }
                ),
            ),
        )
        report = await v2.snapshot(self.db)
        self.assertFalse(report["audit_pass"])
        self.assertEqual(report["invalid_candidate_rows"], 1)
        self.assertEqual(report["enrolled_candidates"], 0)

    def test_scheduler_refuses_when_live_gate_is_blocked(self):
        e = engine()
        with patch.object(v2, "enabled", return_value=True), \
             patch.object(
                 legacy,
                 "gate_snapshot",
                 return_value={"live_entries_blocked": True},
             ):
            self.assertFalse(v2.schedule_if_enabled(e))


class PostGateV2Static(unittest.TestCase):
    def test_module_has_no_live_execution_path(self):
        forbidden = (
            ".place_order(",
            "dispatch_order(",
            "submission_committed",
            "final_sizing_invariants",
            "binance_cross_portfolio_stress",
            "LIVE_RISK_OVERRIDE_APPROVED",
        )
        for marker in forbidden:
            self.assertNotIn(marker, SOURCE)

    def test_v1_and_v2_storage_and_identity_are_distinct(self):
        self.assertIn('POPULATION = "POST_GATE_SHADOW_V2"', SOURCE)
        self.assertIn('COHORT_ID = "CALIBRATION_POST_GATE_V2"', SOURCE)
        self.assertIn("post_gate_shadow_candidates_v2", SOURCE)
        self.assertIn("post_gate_shadow_outcomes_v2", SOURCE)
        self.assertNotIn(
            'INSERT INTO hard_gate_shadow_candidates_v1',
            SOURCE,
        )
        self.assertNotIn(
            'INSERT INTO hard_gate_shadow_outcomes_v1',
            SOURCE,
        )

    def test_reblock_guard_and_provenance_are_explicit(self):
        self.assertIn("GATE_REBLOCKED_STOP_COLLECTION", SOURCE)
        self.assertIn("GATE_REBLOCKED_BEFORE_PERSIST", SOURCE)
        self.assertIn('"production_sha": os.environ.get(', SOURCE)
        self.assertIn('"gate_clear_observed_at_capture": True', SOURCE)

    def test_scheduler_is_background_only(self):
        self.assertIn("asyncio.create_task(_run(engine))", SOURCE)
        self.assertIn('"candidate_generation_effect": "RESEARCH_ONLY"', SOURCE)
        self.assertIn('"execution_effect": "NONE"', SOURCE)
        self.assertIn('"live_allowed": False', SOURCE)


if __name__ == "__main__":
    unittest.main()
