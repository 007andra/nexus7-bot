import asyncio
import os
import time
import unittest

os.environ.setdefault("PAPER_TRADE", "true")
os.environ.setdefault("LOG_LEVEL", "ERROR")

from bot.kucoin import KuCoinClient
from bot.order_state import OrderRegistry, OrderState


class PrivateWsRaceHardeningTests(unittest.IsolatedAsyncioTestCase):
    def _client_and_order(self, coid="bgx7-race", symbol="BTCUSDT", qty=1.0):
        client = KuCoinClient()
        registry = OrderRegistry()
        client._order_registry = registry
        order, _ = registry.get_or_create(coid, symbol, "Buy", qty)
        order.transition(OrderState.SUBMITTING, source="REST")
        return client, registry, order

    async def test_match_before_open_advances_to_partial(self):
        client, registry, order = self._client_and_order()
        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-race-partial",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "match",
                "status": "match",
                "filledSize": "0.4",
                "matchSize": "0.4",
                "matchPrice": "100.5",
                "ts": int(time.time() * 1e9),
            },
        })
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_qty, 0.4)
        self.assertEqual(order.avg_price, 100.5)
        self.assertIs(registry.get_by_order_id("oid-race-partial"), order)

    async def test_done_fill_before_open_advances_to_filled(self):
        client, registry, order = self._client_and_order("bgx7-directfill")
        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-direct-fill",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "match",
                "status": "done",
                "filledSize": "1.0",
                "matchSize": "1.0",
                "matchPrice": "101.25",
                "ts": int(time.time() * 1e9),
            },
        })
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertTrue(order.is_terminal)
        self.assertEqual(order.filled_qty, 1.0)
        self.assertIs(registry.get_by_order_id("oid-direct-fill"), order)

    async def test_late_open_cannot_regress_partial(self):
        client, _, order = self._client_and_order("bgx7-lateopen")
        now = time.time()
        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-late-open",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "match",
                "status": "match",
                "filledSize": "0.2",
                "matchPrice": "99.0",
                "ts": int(now * 1e9),
            },
        })
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-late-open",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "open",
                "status": "open",
                "filledSize": "0",
                "ts": int((now - 5) * 1e9),
            },
        })
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

    async def test_duplicate_partial_is_monotonic(self):
        client, _, order = self._client_and_order("bgx7-duppartial")
        now = time.time()
        evt = {
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-dup-partial",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "match",
                "status": "match",
                "filledSize": "0.5",
                "matchPrice": "100.0",
                "ts": int(now * 1e9),
            },
        }
        await client._handle_private_order_event(evt)
        await client._handle_private_order_event(evt)
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_qty, 0.5)

    async def test_partial_then_cancel_is_valid(self):
        client, _, order = self._client_and_order("bgx7-partcancel")
        now = time.time()
        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-part-cancel",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "match",
                "status": "match",
                "filledSize": "0.3",
                "matchPrice": "100.0",
                "ts": int(now * 1e9),
            },
        })
        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-part-cancel",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "canceled",
                "status": "done",
                "filledSize": "0.3",
                "ts": int((now + 1) * 1e9),
            },
        })
        self.assertEqual(order.state, OrderState.CANCELLED)
        self.assertTrue(order.is_terminal)

    async def test_terminal_fill_ignores_late_cancel(self):
        client, _, order = self._client_and_order("bgx7-fillcancel")
        now = time.time()
        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-fill-cancel",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "match",
                "status": "done",
                "filledSize": "1.0",
                "matchPrice": "100.0",
                "ts": int(now * 1e9),
            },
        })
        await client._handle_private_order_event({
            "subject": "symbolOrderChange",
            "data": {
                "orderId": "oid-fill-cancel",
                "clientOid": order.client_oid,
                "symbol": "XBTUSDTM",  # real tradeOrders payloads carry symbol
                "size": str(order.qty),  # and the order size (contracts)
                "type": "canceled",
                "status": "done",
                "filledSize": "0",
                "ts": int((now + 1) * 1e9),
            },
        })
        self.assertEqual(order.state, OrderState.FILLED)

    def test_restart_restore_preserves_terminal_and_index(self):
        registry = OrderRegistry()
        order, _ = registry.get_or_create("bgx7-restart", "ETHUSDT", "Sell", 2.0)
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.FILLED, order_id="oid-restart", filled_qty=2.0,
                         avg_price=2500.0, source="WS")
        registry.index_order_id("oid-restart", order.client_oid)

        restored = OrderRegistry()
        restored.restore(registry.snapshot())
        recovered = restored.get_by_order_id("oid-restart")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.state, OrderState.FILLED)
        self.assertEqual(recovered.filled_qty, 2.0)
        self.assertTrue(recovered.is_terminal)


if __name__ == "__main__":
    unittest.main()
