"""PAPER mode must stop every Binance order/position mutation before transport.

Port of the KuCoin-only test_paper_mutations to the production venue. The
HTTP session factory is replaced by one that fails the test, so any mutation
that slips past the PAPER guard and reaches the network is caught, not just
calls to a particular helper.
"""
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("EXCHANGE", "binance")

from bot import binance  # noqa: E402
from bot.binance import BinanceClient  # noqa: E402


class BinancePaperMutationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch.object(binance, "PAPER_TRADE", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = BinanceClient()
        self.client._ensure_session = AsyncMock(
            side_effect=AssertionError("PAPER mutation reached HTTP transport")
        )
        self.client._instruments = {"TESTUSDT": {
            "minQty": 0.001, "lotSize": 0.001, "qtyStep": 0.001, "tickSize": 0.01,
            "multiplier": 1.0, "minBaseQty": 0.001, "minNotional": 5.0,
            "quantityUnit": "BASE_ASSET",
        }}

    async def asyncTearDown(self):
        self.client._ensure_session.assert_not_awaited()

    async def test_place_order_is_synthetic(self):
        result = await self.client.place_order(
            symbol="TESTUSDT", side="Buy", qty=1.0, sl=99.0, tp=103.0,
            instruments=self.client._instruments,
        )
        self.assertTrue(str(result.get("orderId", "")).startswith("paper"))

    async def test_set_sl(self):
        await self.client.set_sl("TESTUSDT", 99.0)

    async def test_set_position_stops(self):
        await self.client.set_position_stops("TESTUSDT", sl=99.0, tp=103.0)

    async def test_take_profit_only_and_trailing_update(self):
        await self.client.set_position_stops("TESTUSDT", tp=103.0)
        await self.client.set_sl("TESTUSDT", 100.5)

    async def test_cancel_all_orders(self):
        await self.client.cancel_all_orders("TESTUSDT")

    async def test_set_leverage(self):
        await self.client.set_leverage("TESTUSDT", 10)


if __name__ == "__main__":
    unittest.main()
