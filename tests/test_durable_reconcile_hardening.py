import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    async def test_audited_link_recovery_requires_exact_identity_and_exchange_evidence(self):
        from bot.binance import BinanceClient, BinanceAPIError
        engine = self._engine()
        engine.paper_trade = False
        engine.positions = {}
        client = BinanceClient()
        engine.client = client
        order, _ = engine.orders.get_or_create(hardening._LINK_REJECTED_OID, "LINKUSDT", "Buy", 19.34)
        order.created_at = 1790667918
        order.transition(OrderState.SUBMITTING)
        responses = {
            "/fapi/v1/order": BinanceAPIError("GET", "/fapi/v1/order", 400, -2013, "Order does not exist"),
            "/fapi/v3/positionRisk": [], "/fapi/v1/openOrders": [], "/fapi/v1/openAlgoOrders": [],
        }
        async def read(endpoint, *args, **kwargs):
            result = responses[endpoint]
            if isinstance(result, Exception):
                raise result
            return result
        client._get = AsyncMock(side_effect=read)
        client.get_symbol_config = AsyncMock(return_value={"symbol": "LINKUSDT", "marginType": "CROSSED", "leverage": 20})
        client._post = AsyncMock(side_effect=AssertionError("no exchange mutation"))
        with patch.dict("os.environ", {"RAILWAY_PROJECT_ID": "1443ec46-186f-497b-9e61-4b69446d35c3",
                                       "RAILWAY_ENVIRONMENT_ID": "e07566a4-170b-418d-9c91-b19604db8b3e"}), patch(
            "bot.durable_live_reconciliation._owner_valid", AsyncMock(return_value=True)
        ) as owner:
            owner.return_value = False
            self.assertFalse(await hardening._recover_audited_link_rejection(engine, order, _Log()))
            owner.return_value = True
            for field, bad in (("qty", 19.35), ("filled_qty", 0.1), ("order_id", "123"),
                               ("created_at", 1790667800), ("client_oid", "bgx7-other")):
                original = getattr(order, field)
                setattr(order, field, bad)
                self.assertFalse(await hardening._recover_audited_link_rejection(engine, order, _Log()))
                setattr(order, field, original)
            for endpoint, bad in (
                ("/fapi/v1/order", {}), ("/fapi/v1/order", TimeoutError()),
                ("/fapi/v1/order", BinanceAPIError("GET", "/fapi/v1/order", 500, -2013, "unknown")),
                ("/fapi/v3/positionRisk", [{"positionAmt": "nan"}]),
                ("/fapi/v3/positionRisk", [{"positionAmt": "1"}]),
                ("/fapi/v1/openOrders", {}), ("/fapi/v1/openAlgoOrders", [{"algoId": 1}]),
            ):
                original = responses[endpoint]
                responses[endpoint] = bad
                self.assertFalse(await hardening._recover_audited_link_rejection(engine, order, _Log()))
                self.assertEqual(order.state, OrderState.SUBMITTING)
                responses[endpoint] = original
            durable = self._durable()
            hardening.install(durable, order_state, _Log())
            self.assertTrue(await durable.reconcile_orders(engine))
            self.assertEqual(order.state, OrderState.REJECTED)
            self.assertIn("startup_reconcile_hardened", engine.persist_reasons)
            self.assertFalse(await hardening._recover_audited_link_rejection(engine, order, _Log()))
            order.state = OrderState.SUBMITTING
            engine.errors.add("orders")
            engine._durable_state_ok = False
            durable.persist_orders = AsyncMock(return_value=False)
            self.assertFalse(await durable.reconcile_orders(engine))
            self.assertFalse(engine._durable_state_ok)
        client._post.assert_not_awaited()

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
        )

    def _engine(self):
        engine = SimpleNamespace()
        engine.orders = OrderRegistry()
        engine.errors = {"orders"}
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
