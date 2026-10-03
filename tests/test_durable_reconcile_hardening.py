import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import durable_execution
from bot import durable_reconcile_hardening as hardening
from bot import order_state
from bot import pilot_submission_counter as provenance
from bot.order_state import OrderRegistry, OrderState


class _Log:
    info = staticmethod(lambda *a, **k: None)
    warning = staticmethod(lambda *a, **k: None)
    critical = staticmethod(lambda *a, **k: None)
    error = staticmethod(lambda *a, **k: None)


class DurableReconcileHardeningTests(unittest.IsolatedAsyncioTestCase):
    def _durable(self):
        async def original(engine):
            return False

        async def persist(engine, reason, strict=False):
            engine.persist_reasons.append(reason)
            return True

        def clear(engine, reason):
            engine.errors.discard(reason)
            engine._durable_state_ok = not engine.errors

        def block(engine, reason):
            engine.errors.add(reason)
            engine._durable_state_ok = False

        def advance(order, state, **info):
            if state == OrderState.FILLED and order.state == OrderState.SUBMITTING:
                order.transition(OrderState.SUBMITTED, **info)
            order.transition(state, **info)

        return SimpleNamespace(
            reconcile_orders=original,
            persist_orders=persist,
            _clear=clear,
            _block=block,
            _advance=advance,
            ORDERS_UNRESOLVED=durable_execution.ORDERS_UNRESOLVED,
        )

    def _engine(self):
        engine = SimpleNamespace()
        engine.orders = OrderRegistry()
        # Restored unresolved intents: the original reconcile left this reason.
        engine.errors = {durable_execution.ORDERS_UNRESOLVED}
        engine._durable_state_ok = False
        engine.persist_reasons = []
        engine.client = SimpleNamespace(
            get_order_status=AsyncMock(return_value={}),
            get_order_by_client_oid=AsyncMock(return_value={}),
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        return engine

    async def test_restored_created_legacy_intent_remains_fail_closed_without_provenance(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        order, _ = engine.orders.get_or_create("bgx7-created", "XRPUSDT", "Sell", 10.0)

        ok = await durable.reconcile_orders(engine)

        self.assertFalse(ok)
        self.assertEqual(order.state, OrderState.CREATED)
        self.assertFalse(order.is_terminal)
        engine.client.get_order_status.assert_not_awaited()
        engine.client.get_order_by_client_oid.assert_not_awaited()
        self.assertFalse(engine._durable_state_ok)
        self.assertEqual(len(engine.orders.pending_orders()), 1)

    async def test_created_with_canonical_proven_not_dispatched_terminalizes_failed(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        order, _ = engine.orders.get_or_create("bgx7-created-proven", "XRPUSDT", "Sell", 10.0)
        provenance._record_same_state(
            order,
            dispatch_attempted=False,
            predispatch_abort_reason="TEST_AUTHORITATIVE_PRE_DISPATCH_DENIAL",
            exchange_dispatch="NONE",
        )

        ok = await durable.reconcile_orders(engine)

        self.assertTrue(ok)
        self.assertEqual(order.state, OrderState.FAILED)
        self.assertTrue(order.is_terminal)
        engine.client.get_order_status.assert_not_awaited()
        engine.client.get_order_by_client_oid.assert_not_awaited()
        self.assertTrue(engine._durable_state_ok)
        self.assertEqual(len(engine.orders.pending_orders()), 0)

    async def test_order_id_recovers_filled_submitting_intent(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        order, _ = engine.orders.get_or_create("bgx7-filled", "XRPUSDT", "Sell", 10.0)
        order.transition(OrderState.SUBMITTING, source="TEST")
        order.order_id = "kc-123"
        engine.client.get_order_status.return_value = {
            "isActive": False,
            "cancelExist": False,
            "filledSize": "1",
        }

        ok = await durable.reconcile_orders(engine)

        self.assertTrue(ok)
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_qty, 1.0)
        engine.client.get_order_status.assert_awaited_once_with("kc-123")

    async def test_fresh_ambiguous_submitting_without_order_id_remains_fail_closed(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        order, _ = engine.orders.get_or_create("bgx7-ambiguous", "SOLUSDT", "Buy", 1.0)
        order.transition(OrderState.SUBMITTING, source="TEST")

        ok = await durable.reconcile_orders(engine)

        self.assertFalse(ok)
        self.assertEqual(order.state, OrderState.SUBMITTING)
        engine.client.get_order_by_client_oid.assert_not_awaited()
        self.assertFalse(engine._durable_state_ok)

    async def test_stale_absent_submitting_without_provenance_remains_fail_closed_when_flat(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        order, _ = engine.orders.get_or_create("bgx7-stale", "SOLUSDT", "Buy", 1.0)
        order.transition(OrderState.SUBMITTING, source="TEST")
        order.created_at = time.time() - hardening.STALE_SUBMITTING_AGE_S - 30

        ok = await durable.reconcile_orders(engine)

        self.assertFalse(ok)
        self.assertEqual(order.state, OrderState.SUBMITTING)
        self.assertFalse(order.is_terminal)
        engine.client.get_order_by_client_oid.assert_awaited_once_with("bgx7-stale")
        engine.client.get_positions.assert_awaited_once()
        engine.client._get.assert_awaited_once_with(
            "/api/v1/orders", {"status": "active"}, auth=True
        )
        self.assertFalse(engine._durable_state_ok)
        self.assertEqual(len(engine.orders.pending_orders()), 1)

    async def test_stale_submitting_with_position_stays_fail_closed(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        engine.client.get_positions.return_value = [{"symbol": "SOLUSDT", "size": "1"}]
        order, _ = engine.orders.get_or_create("bgx7-stale-pos", "SOLUSDT", "Buy", 1.0)
        order.transition(OrderState.SUBMITTING, source="TEST")
        order.created_at = time.time() - hardening.STALE_SUBMITTING_AGE_S - 30

        ok = await durable.reconcile_orders(engine)

        self.assertFalse(ok)
        self.assertEqual(order.state, OrderState.SUBMITTING)
        self.assertFalse(engine._durable_state_ok)

    async def test_stale_submitting_read_error_stays_fail_closed(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        engine.client.get_positions.side_effect = RuntimeError("exchange read failed")
        order, _ = engine.orders.get_or_create("bgx7-stale-read", "SOLUSDT", "Buy", 1.0)
        order.transition(OrderState.SUBMITTING, source="TEST")
        order.created_at = time.time() - hardening.STALE_SUBMITTING_AGE_S - 30

        ok = await durable.reconcile_orders(engine)

        self.assertFalse(ok)
        self.assertEqual(order.state, OrderState.SUBMITTING)
        self.assertFalse(engine._durable_state_ok)


if __name__ == "__main__":
    unittest.main()
