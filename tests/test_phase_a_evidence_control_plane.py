import unittest

from bot.binance_execution_parity import ExecutionPlan
from bot.candidate_outcome_v2 import (
    normalize_opportunity_row,
    normalize_opportunity_rows,
)
from bot.evidence_stores_v2 import (
    persist_execution_plan,
    persist_experiment,
    persist_release_manifest,
)
from bot.experiment_registry import ExperimentRecord
from bot.nexus_edge_dashboard import build_dashboard
from bot.release_manifest_v2 import build_release_manifest


def _row(
    key,
    *,
    approved,
    outcome,
    symbol="SOLUSDT",
    setup="PULLBACK",
    regime="TRENDING_UP",
    confidence=60,
):
    return {
        "signal_key": key,
        "created_epoch": 1700000000 + int(key[-1]),
        "symbol": symbol,
        "direction": "LONG",
        "entry_type": setup,
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
        "strategy_score": 65,
        "nexus_score": 70 if approved else 40,
        "nexus_confidence": confidence,
        "nexus_regime": regime,
        "approved": 1 if approved else 0,
        "decision_reason": "ok" if approved else "EV negativo",
        "metadata": (
            '{"blocker_class":"APPROVED"}'
            if approved
            else '{"blocker_class":"EV_RR"}'
        ),
        "p240_net_pct": outcome,
    }


class CandidateDatasetTests(unittest.TestCase):
    def test_rejected_and_approved_share_baseline_population(self):
        vals = normalize_opportunity_rows(
            [
                _row("c1", approved=True, outcome=4.0),
                _row("c2", approved=False, outcome=-2.0),
            ]
        )
        self.assertEqual(len(vals), 2)
        self.assertTrue(
            all(
                item.to_candidate_outcome().baseline_eligible
                for item in vals
            )
        )
        self.assertAlmostEqual(
            vals[0].to_candidate_outcome().r_multiple,
            2.0,
        )
        self.assertAlmostEqual(
            vals[1].to_candidate_outcome().r_multiple,
            -1.0,
        )

    def test_unknown_future_outcome_stays_unknown(self):
        item = normalize_opportunity_row(
            _row("c3", approved=False, outcome=None)
        )
        outcome = item.to_candidate_outcome()
        self.assertFalse(outcome.outcome_known)
        self.assertIsNone(outcome.r_multiple)

    def test_invalid_horizon_rejected(self):
        with self.assertRaises(ValueError):
            normalize_opportunity_row(
                _row("c4", approved=True, outcome=1.0),
                horizon="future",
            )


class DashboardTests(unittest.TestCase):
    def test_dashboard_segments_and_is_non_authoritative(self):
        vals = normalize_opportunity_rows(
            [
                _row("c1", approved=True, outcome=4.0),
                _row(
                    "c2",
                    approved=True,
                    outcome=-2.0,
                    symbol="BTCUSDT",
                    setup="BOS_BREAK",
                ),
                _row("c3", approved=False, outcome=-1.0),
            ]
        )
        dashboard = build_dashboard(vals)
        self.assertEqual(
            dashboard["scope"],
            "NEXUS_EVALUATED_BASELINE",
        )
        self.assertEqual(dashboard["execution_effect"], "NONE")
        self.assertIn("SOLUSDT", dashboard["by_symbol"])
        self.assertEqual(
            dashboard["overall"]["known_outcomes"],
            3,
        )


class _FakeDB:
    def __init__(self):
        self.calls = []

    async def _exec(self, sql, params=()):
        self.calls.append((sql, params))
        return True


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_parity_store_is_append_only(self):
        db = _FakeDB()
        plan = ExecutionPlan(
            "SOLUSDT",
            "LONG",
            1.0,
            100.0,
            98.0,
            104.0,
            0.0005,
            0.0005,
            2.0,
            50.0,
            2.0,
        )
        self.assertTrue(
            await persist_execution_plan(
                db,
                candidate_id="x",
                stage="shadow",
                plan=plan,
                timestamp=1.0,
            )
        )
        text = " ".join(sql.upper() for sql, _ in db.calls)
        self.assertIn("ON CONFLICT", text)
        self.assertNotIn("UPDATE ", text)
        self.assertNotIn("DELETE ", text)

    async def test_experiment_and_release_are_immutable(self):
        db = _FakeDB()
        experiment = ExperimentRecord(
            "h",
            "dataset",
            "sha",
            "train",
            "test",
            {},
            7,
            {},
            "NOT_PROVEN",
        )
        await persist_experiment(db, experiment)

        manifest = build_release_manifest(
            sha="abc",
            symbols=["SOLUSDT"],
            parameters={},
            risk_policy={},
            sizing_policy={},
            drawdown_policy={},
            execution_chain=["signal"],
            strategy_version="1",
            nexus_version="1",
            risk_version="1",
            execution_model_version="1",
        )
        await persist_release_manifest(
            db,
            manifest,
            created_epoch=1.0,
        )
        text = " ".join(sql.upper() for sql, _ in db.calls)
        self.assertNotIn("UPDATE ", text)
        self.assertNotIn("DELETE ", text)


if __name__ == "__main__":
    unittest.main()
