import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import durable_execution as durable
from bot.order_state import ManagedOrder, OrderRegistry, OrderState


def valid_record():
    order = ManagedOrder("bgx7-valid-opening", "BTCUSDT", "Buy", 0.01)
    order.transition(OrderState.SUBMITTING, source="REST")
    order.transition(OrderState.SUBMITTED, order_id="111", source="REST")
    order.transition(
        OrderState.FILLED, order_id="111", filled_qty=0.01,
        avg_price=60000.0, source="REST",
    )
    order.exposure_reconciliation_complete = True
    return order.to_record()


def legacy_atom_child():
    now = time.time()
    return {
        "client_oid": "bgx7-bdbb57f3420960dbe6eda47f5bc8bd",
        "symbol": "ATOMUSDT",
        "side": "SELL",
        "qty": 570.55,
        "state": "FILLED",
        "order_id": "24873239201",
        "filled_qty": 570.55,
        "avg_price": 1.709,
        "created_at": now - 60,
        "updated_at": now,
        "last_source": "LIVE_REST",
        "history": [
            [now - 2, "CREATED", "SUBMITTING", {"source": "LIVE_REST"}],
            [now - 1, "SUBMITTING", "SUBMITTED", {"source": "LIVE_REST"}],
            [now, "SUBMITTED", "FILLED", {"source": "LIVE_REST"}],
        ],
        "exposure_reconciliation_complete": True,
        "reduce_only": False,
        "exposure_intent": "INCREASE",
        "previous_position_qty": None,
        # Deliberately absent protection_plan: this predates PR #458 and was
        # manufactured by the old Binance private-WS child-order path.
    }


class LegacyBinanceWsArtifactRecognition(unittest.TestCase):
    def test_exact_terminal_uppercase_ws_artifact_is_quarantined(self):
        registry = OrderRegistry()
        count = registry.restore([valid_record(), legacy_atom_child()])
        self.assertEqual(count, 1)
        self.assertEqual(len(registry), 1)
        self.assertIsNotNone(registry.get("bgx7-valid-opening"))
        self.assertIsNone(registry.get("bgx7-bdbb57f3420960dbe6eda47f5bc8bd"))
        self.assertEqual(
            registry.legacy_quarantined_records()[0]["reason"],
            "legacy_binance_algo_child_ws_artifact",
        )

    def test_uppercase_nonterminal_remains_fail_closed(self):
        record = legacy_atom_child()
        record["state"] = "SUBMITTED"
        with self.assertRaisesRegex(ValueError, "invalid managed order identity"):
            OrderRegistry().restore([record])

    def test_uppercase_record_with_protection_plan_remains_fail_closed(self):
        record = legacy_atom_child()
        record["protection_plan"] = {
            "direction": "SHORT", "entry": 1.7, "sl": 1.72, "tp": 1.65
        }
        with self.assertRaisesRegex(ValueError, "invalid managed order identity"):
            OrderRegistry().restore([record])

    def test_uppercase_terminal_without_ws_or_rest_lineage_remains_fail_closed(self):
        record = legacy_atom_child()
        record["last_source"] = ""
        record["history"] = []
        with self.assertRaisesRegex(ValueError, "invalid managed order identity"):
            OrderRegistry().restore([record])

    def test_other_invalid_side_remains_fail_closed(self):
        record = legacy_atom_child()
        record["side"] = "SHORT"
        with self.assertRaisesRegex(ValueError, "invalid managed order identity"):
            OrderRegistry().restore([record])

    def test_duplicate_order_id_still_fails_entire_restore(self):
        one = valid_record()
        two = valid_record()
        two["client_oid"] = "bgx7-another-valid"
        with self.assertRaisesRegex(ValueError, "duplicate order_id"):
            OrderRegistry().restore([one, two])


class DurableLegacySnapshotMigration(unittest.IsolatedAsyncioTestCase):
    def engine(self):
        return SimpleNamespace(
            orders=OrderRegistry(),
            paper_trade=False,
            client=SimpleNamespace(),
            entries_paused=False,
            _daily_pnl_ok=True,
        )

    async def test_quarantine_is_persisted_as_clean_snapshot_before_authorization(self):
        engine = self.engine()
        payload = json.dumps({
            "version": 1,
            "reason": "historical",
            "orders": [valid_record(), legacy_atom_child()],
        })
        save = AsyncMock(return_value=True)
        with (
            patch.object(durable.db, "configured_postgres_unavailable", return_value=False),
            patch.object(durable.db, "load_key_value", new=AsyncMock(return_value=payload)),
            patch.object(durable.db, "save_key_value", new=save),
        ):
            ok = await durable.restore_engine_state(engine)

        self.assertTrue(ok)
        self.assertTrue(engine._durable_state_ok)
        self.assertNotIn("orders", engine._durable_state_errors)
        self.assertEqual(len(engine.orders), 1)
        self.assertGreaterEqual(save.await_count, 1)
        cleaned = json.loads(save.await_args_list[0].args[1])
        self.assertEqual(cleaned["reason"], "legacy_binance_ws_artifact_quarantined")
        self.assertEqual(len(cleaned["orders"]), 1)
        self.assertEqual(cleaned["orders"][0]["client_oid"], "bgx7-valid-opening")

    async def test_cleanup_persistence_failure_stays_fail_closed(self):
        engine = self.engine()
        payload = json.dumps({
            "version": 1,
            "reason": "historical",
            "orders": [valid_record(), legacy_atom_child()],
        })
        with (
            patch.object(durable.db, "configured_postgres_unavailable", return_value=False),
            patch.object(durable.db, "load_key_value", new=AsyncMock(return_value=payload)),
            patch.object(
                durable.db, "save_key_value",
                new=AsyncMock(side_effect=RuntimeError("storage unavailable")),
            ),
        ):
            ok = await durable.restore_engine_state(engine)

        self.assertFalse(ok)
        self.assertFalse(engine._durable_state_ok)
        self.assertIn("orders", engine._durable_state_errors)


if __name__ == "__main__":
    unittest.main()
