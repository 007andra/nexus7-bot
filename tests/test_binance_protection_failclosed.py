import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot import binance_protection_failclosed as protection


def _position():
    return {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": 0.01,
        "sizeUnit": "BASE_ASSET",
        "entryPrice": 60000,
        "markPrice": 60100,
        "stopLoss": 0,
    }


def _stop():
    return {
        "symbol": "BTCUSDT",
        "side": "sell",
        "status": "NEW",
        "stopPrice": 59000,
        "closeOrder": True,
        "reduceOnly": False,
        "size": 0,
        "sizeUnit": "BASE_ASSET",
    }


class DummyEngine:
    async def _open(self, sig, *args, **kwargs):
        return "open-original"

    async def _guard_naked_positions(self):
        return "guard-original"


class BinanceProtectionFailClosedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = Mock()
        protection.install(DummyEngine, self.log)
        self.engine = DummyEngine()
        self.engine.paper_trade = False
        self.engine._durable_state_enforced = False
        self.engine._validation_safety_lock_active = False
        self.engine._external_position_symbols = set()
        self.engine._unprotected_symbols = set()
        self.engine.instruments = {
            "BTCUSDT": {
                "multiplier": 1.0,
                "minQty": 0.001,
                "qtyStep": 0.001,
                "tickSize": 0.1,
            }
        }
        self.engine.positions = {
            "BTCUSDT": SimpleNamespace(
                qty=0.01,
                direction="LONG",
                sl=59000,
                tp=62000,
            )
        }
        self.engine.pilot = SimpleNamespace(enabled=True)
        self.engine.client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[_position()]),
            get_stop_orders=AsyncMock(return_value=[_stop()]),
            set_position_stops=AsyncMock(return_value=True),
            place_order=AsyncMock(return_value={"orderId": "777"}),
            wait_for_fill=AsyncMock(return_value={"filled": True}),
        )

    async def test_confirmed_conditional_stop_requires_no_mutation(self):
        result = await self.engine._guard_naked_positions()
        self.assertIsNone(result)
        self.assertNotIn("BTCUSDT", self.engine._unprotected_symbols)
        self.engine.client.set_position_stops.assert_not_awaited()
        self.engine.client.place_order.assert_not_awaited()

    async def test_unprotected_owned_position_is_repaired_and_read_back(self):
        self.engine.client.get_stop_orders.side_effect = [[], [_stop()]]
        result = await self.engine._guard_naked_positions()
        self.assertIsNone(result)
        self.engine.client.set_position_stops.assert_awaited_once_with(
            "BTCUSDT",
            sl=59000.0,
            tp=62000.0,
        )
        self.assertEqual(self.engine.client.get_stop_orders.await_count, 2)
        self.engine.client.place_order.assert_not_awaited()
        self.assertNotIn("BTCUSDT", self.engine._unprotected_symbols)

    async def test_failed_repair_closes_only_after_fill_and_flat_confirmation(self):
        rows = [_position()]
        self.engine.client.get_positions.side_effect = [rows, rows, []]
        self.engine.client.get_stop_orders.return_value = []
        self.engine.client.set_position_stops.return_value = False

        result = await self.engine._guard_naked_positions()

        self.assertIsNone(result)
        self.engine.client.place_order.assert_awaited_once()
        kwargs = self.engine.client.place_order.await_args.kwargs
        self.assertTrue(kwargs["reduce_only"])
        self.assertTrue(kwargs["single_submission"])
        self.assertEqual(kwargs["side"], "Sell")
        self.engine.client.wait_for_fill.assert_awaited_once_with(
            "777", timeout_s=8.0
        )
        self.assertNotIn("BTCUSDT", self.engine.positions)
        self.assertNotIn("BTCUSDT", self.engine._unprotected_symbols)

    async def test_external_position_is_never_repaired_or_closed(self):
        self.engine._external_position_symbols = {"BTCUSDT"}
        result = await self.engine._guard_naked_positions()
        self.assertIsNone(result)
        self.engine.client.get_stop_orders.assert_not_awaited()
        self.engine.client.set_position_stops.assert_not_awaited()
        self.engine.client.place_order.assert_not_awaited()

    async def test_post_open_paper_path_is_inert(self):
        self.engine.paper_trade = True
        sig = SimpleNamespace(symbol="BTCUSDT", sl=59000, tp=62000)
        result = await self.engine._open(sig)
        self.assertEqual(result, "open-original")
        self.engine.client.set_position_stops.assert_not_awaited()
        self.engine.client.place_order.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
