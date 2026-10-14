import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import conditional_stop_lifecycle as lifecycle
from bot import durable_execution as durable
from bot.durable_live_reconciliation import reconcile_pending
from bot.order_state import OrderRegistry, OrderState


class DurableTerminalizationTests(unittest.IsolatedAsyncioTestCase):
    def make_engine(self, qty=10.0, filled=5.0):
        orders = OrderRegistry()
        order, _ = orders.get_or_create(
            "bgx7-test-partial", "ATOMUSDT", "Sell", qty
        )
        order.reduce_only = True
        order.exposure_intent = "REDUCE"
        order.previous_position_qty = 20.0
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="oid-1", source="REST")
        order.transition(
            OrderState.PARTIALLY_FILLED,
            order_id="oid-1",
            filled_qty=filled,
            source="WS",
        )
        client = SimpleNamespace(
            _execution_ownership=object(),
            get_order_by_client_oid=AsyncMock(),
        )
        engine = SimpleNamespace(
            paper_trade=False,
            connected=True,
            client=client,
            orders=orders,
            positions={},
            _execution_ownership_valid=True,
            _durable_state_errors=set(),
            _durable_state_ok=True,
            _durable_live_reconcile_last=0.0,
        )
        return engine, order

    async def reconcile(self, engine):
        with patch(
            "bot.execution_ownership.validate_execution_ownership",
            new=AsyncMock(return_value=None),
        ), patch(
            "bot.durable_execution.persist_orders",
            new=AsyncMock(return_value=True),
        ):
            return await reconcile_pending(engine, min_interval_s=0)

    async def startup_reconcile(self, engine):
        with patch(
            "bot.durable_execution.persist_orders",
            new=AsyncMock(return_value=True),
        ):
            return await durable.reconcile_orders(engine)

    async def test_lost_ws_done_recovers_partial_to_filled_without_restart(self):
        engine, order = self.make_engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 10.0,
        }
        self.assertTrue(await self.reconcile(engine))
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(engine.orders.pending_orders(), [])

    async def test_position_flat_alone_is_not_terminal_authority(self):
        engine, order = self.make_engine()
        engine.positions = {}
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": True,
            "filledSize": 5.0,
        }
        self.assertFalse(await self.reconcile(engine))
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

    async def test_incomplete_rest_truth_fails_closed(self):
        engine, order = self.make_engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "filledSize": 5.0,
        }
        self.assertFalse(await self.reconcile(engine))
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

    async def test_rest_then_duplicate_ws_done_is_idempotent(self):
        engine, order = self.make_engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 10.0,
        }
        self.assertTrue(await self.reconcile(engine))
        history_len = len(order.history)
        order.transition(OrderState.FILLED, filled_qty=10.0, source="WS")
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(len(order.history), history_len)

    async def test_partial_execution_cancelled_remainder_is_not_full_fill(self):
        engine, order = self.make_engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "cancelExist": True,
            "status": "done",
            "filledSize": 5.0,
        }
        self.assertTrue(await self.reconcile(engine))
        self.assertEqual(order.state, OrderState.CANCELLED)

    async def test_identity_mismatch_fails_closed(self):
        engine, order = self.make_engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": "bgx7-other",
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 10.0,
        }
        self.assertFalse(await self.reconcile(engine))
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

    async def test_startup_position_presence_does_not_fake_fill(self):
        engine, order = self.make_engine()
        engine.positions = {"ATOMUSDT": SimpleNamespace(qty=10.0)}
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": True,
            "filledSize": 5.0,
        }
        self.assertFalse(await self.startup_reconcile(engine))
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

    async def test_startup_partial_cancelled_remainder_is_cancelled_not_filled(self):
        engine, order = self.make_engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "cancelExist": True,
            "status": "done",
            "filledSize": 5.0,
        }
        self.assertTrue(await self.startup_reconcile(engine))
        self.assertEqual(order.state, OrderState.CANCELLED)

    async def test_startup_full_exchange_truth_terminalizes_filled(self):
        engine, order = self.make_engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 10.0,
        }
        self.assertTrue(await self.startup_reconcile(engine))
        self.assertEqual(order.state, OrderState.FILLED)


class _StopClient:
    def __init__(self, orders=None, post_mode="accept"):
        self.orders = list(orders or [])
        self.post_mode = post_mode
        self.post_calls = []
        self.cancelled = []
        self.stop_reads = 0
        self._instruments = {
            "ATOMUSDT": {
                "multiplier": "0.1",
                "lotSize": "1",
                "minQty": "1",
                "tickSize": "0.001",
            }
        }

    async def get_positions(self):
        return [{
            "symbol": "ATOMUSDT",
            "size": 1574,
            "sizeUnit": "CONTRACTS",
            "side": "Buy",
            "entryPrice": 1.786,
            "markPrice": 1.800,
        }]

    def get_instruments(self):
        return self._instruments

    def _round_price(self, price, symbol):
        return str(round(float(price), 3))

    async def get_stop_orders(self, symbol):
        self.stop_reads += 1
        return list(self.orders)

    async def get_order_by_client_oid(self, client_oid):
        for row in self.orders:
            if row.get("clientOid") == client_oid:
                return dict(row)
        return {}

    async def _post(self, endpoint, body, **kwargs):
        self.post_calls.append(dict(body))
        if self.post_mode == "timeout":
            raise TimeoutError("ambiguous")
        if self.post_mode == "300004":
            raise RuntimeError("300004 stop-order quantity limit exceeded")
        row = dict(body)
        row.update({
            "id": f"new-{len(self.post_calls)}",
            "isActive": True,
            "status": "open",
            "stopTriggered": False,
        })
        self.orders.append(row)
        return {"orderId": row["id"]}

    async def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)
        self.orders = [
            row for row in self.orders
            if str(row.get("id") or row.get("orderId") or "") != str(order_id)
        ]
        return True


class LifecycleTopologyReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lifecycle._CACHE.clear()
        lifecycle._LOCKS.clear()

    def obsolete_stop(self, oid):
        return {
            "id": oid,
            "clientOid": f"bgx-stop-{oid}",
            "symbol": "ATOMUSDTM",
            "side": "sell",
            "stop": "down",
            "stopPrice": "1.775",
            "stopPriceType": "MP",
            "closeOrder": True,
            "reduceOnly": True,
            "isActive": True,
            "status": "open",
        }

    async def terminalize_partial(self):
        orders = OrderRegistry()
        order, _ = orders.get_or_create("bgx7-replay", "ATOMUSDT", "Sell", 5.0)
        order.reduce_only = True
        order.exposure_intent = "REDUCE"
        order.previous_position_qty = 10.0
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="partial", source="REST")
        order.transition(OrderState.PARTIALLY_FILLED, filled_qty=2.0, source="WS")
        client = _StopClient([self.obsolete_stop("obsolete")])
        client._execution_ownership = object()
        client.get_order_by_client_oid = AsyncMock(return_value={
            "clientOid": order.client_oid,
            "orderId": "partial",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 5.0,
        })
        engine = SimpleNamespace(
            paper_trade=False,
            connected=True,
            client=client,
            orders=orders,
            positions={},
            _execution_ownership_valid=True,
            _durable_state_errors=set(),
            _durable_state_ok=True,
            _durable_live_reconcile_last=0.0,
        )
        with patch(
            "bot.execution_ownership.validate_execution_ownership",
            new=AsyncMock(return_value=None),
        ), patch(
            "bot.durable_execution.persist_orders",
            new=AsyncMock(return_value=True),
        ):
            self.assertTrue(await reconcile_pending(engine, min_interval_s=0))
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(orders.mark_filled_exposure_reconciled("ATOMUSDT"), 1)
        return engine

    async def assert_partial_final_topology(self, reason):
        engine = await self.terminalize_partial()
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine, "ATOMUSDT", exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ), reason)
        self.assertEqual(engine.orders.pending_orders(), [], reason)
        self.assertFalse(any(
            lifecycle.is_bgx_owned(row) for row in engine.client.orders
        ), reason)

    async def test_partial_to_final_sl(self):
        await self.assert_partial_final_topology("SL")

    async def test_partial_to_final_tp(self):
        await self.assert_partial_final_topology("TP")

    async def test_partial_to_strategy_exit(self):
        await self.assert_partial_final_topology("STRATEGY_EXIT")

    async def test_full_sl_converges_cleanly(self):
        client = _StopClient([self.obsolete_stop("full-sl")])
        engine = SimpleNamespace(client=client, positions={}, orders=OrderRegistry())
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine, "ATOMUSDT", exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ))

    async def test_full_tp_converges_cleanly(self):
        client = _StopClient([self.obsolete_stop("full-tp")])
        engine = SimpleNamespace(client=client, positions={}, orders=OrderRegistry())
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine, "ATOMUSDT", exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ))

    async def test_restart_stale_partial_and_obsolete_stops_converge_no_resubmit(self):
        engine = await self.terminalize_partial()
        self.assertEqual(engine.client.post_calls, [])
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine, "ATOMUSDT", exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ))
        self.assertEqual(engine.client.post_calls, [])


if __name__ == "__main__":
    unittest.main()
