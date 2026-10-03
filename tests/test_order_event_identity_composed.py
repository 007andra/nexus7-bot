"""F-011 Q — final private-WS handler on the composed LIVE-pilot runtime.

Real composition (sitecustomize), the engine's own OrderRegistry wired to the
real durable persistence callback (only the DB write is mocked) and the final
``KuCoinClient._handle_private_order_event``. Offline.
"""
import os

os.environ.update({
    "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "REAL_TRADING_PILOT": "true",
    "PILOT_ACCOUNT_CONFIRMED": "true",
    "PILOT_RELEASE_APPROVED": "I_APPROVE_TWO_LIVE_PILOT_ORDERS",
    "VALIDATION_LOCK_RELEASE_APPROVED": "I_APPROVE_CONTROLLED_LIVE_PILOT_EXECUTION",
    "KUCOIN_REST_BASE": "http://127.0.0.1:1",
    "KUCOIN_API_KEY": "", "KUCOIN_API_SECRET": "", "KUCOIN_API_PASSPHRASE": "",
    "NEXUS_TELEGRAM": "false",
})

import asyncio  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import durable_execution as durable  # noqa: E402
from bot import kucoin  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.order_state import OrderState  # noqa: E402


class ComposedOrderIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_q_final_handler_rejects_foreign_event_and_persists_nothing_bad(self):
        self.assertFalse(kucoin.PAPER_TRADE)
        raw = kucoin.KuCoinClient()
        raw._instruments = {"BTCUSDT": {"multiplier": 0.001, "lotSize": 1, "minQty": 1,
                                        "tickSize": 0.1, "minNotional": 0}}
        engine = TradingEngine(raw)
        engine._durable_order_lock = asyncio.Lock()
        engine._durable_state_errors, engine._durable_state_ok = set(), True

        async def callback(_order):
            await durable.persist_orders(engine, "private_ws_transition")
        engine.orders.persist_callback = callback
        raw._order_registry = engine.orders          # what start_private_websocket does

        order, _ = engine.orders.get_or_create("bgx7-A-client", "BTCUSDT", "Buy", 0.002)
        order.transition(OrderState.SUBMITTING, source="LOCAL")
        order.transition(OrderState.SUBMITTED, order_id="A", source="REST")
        engine.orders.index_order_id("A", order.client_oid)

        saved = []

        async def save(key, value, strict=False):
            saved.append(json.loads(value))
            return True
        ts = int(time.time() * 1e9)
        with patch.object(durable.db, "save_key_value", AsyncMock(side_effect=save)):
            await raw._handle_private_order_event({"subject": "symbolOrderChange", "data": {
                "orderId": "B", "clientOid": "bgx7-A-client", "symbol": "ETHUSDTM",
                "side": "sell", "type": "filled", "status": "done", "filledSize": "7",
                "size": "7", "ts": ts}})
            self.assertEqual((order.state, order.filled_qty), (OrderState.SUBMITTED, 0.0))
            self.assertEqual(saved, [], "rejected event never persisted")
            await raw._handle_private_order_event({"subject": "symbolOrderChange", "data": {
                "orderId": "A", "clientOid": "bgx7-A-client", "symbol": "XBTUSDTM",
                "side": "buy", "type": "filled", "status": "done", "filledSize": "2",
                "size": "2", "ts": ts}})
        self.assertEqual((order.state, order.filled_qty), (OrderState.FILLED, 2.0))
        records = saved[-1]["orders"]
        self.assertEqual([(r["symbol"], r["state"], r["filled_qty"], r["order_id"]) for r in records],
                         [("BTCUSDT", "FILLED", 2.0, "A")])
        self.assertIsNone(engine.orders.get_by_order_id("B"))


if __name__ == "__main__":
    unittest.main()
