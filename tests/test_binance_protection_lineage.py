"""NOVO-F013A-1c (Binance): a BGX algo order is reused as protection only by
the opening lineage that created it — prefix/side/type/price never prove it."""
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("EXCHANGE", "binance")

from bot import binance  # noqa: E402

INFO = {"ETHUSDT": {"quantityUnit": "BASE_ASSET", "qtyStep": "0.001", "minQty": "0.001",
                    "minNotional": "0", "tickSize": "0.01", "multiplier": 1.0}}


class ProtectionLineageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [patch.object(binance, "PAPER_TRADE", False),
                        patch.object(binance, "_live_migration_ready", lambda: True)]
        for p in self.patches:
            p.start()
        self.client = binance.BinanceClient()
        self.client._instruments = INFO
        self.algos = []
        self.client._active_position_for_symbol = AsyncMock(return_value={"symbol": "ETHUSDT", "side": "Buy"})
        self.client.get_stop_orders = AsyncMock(side_effect=lambda s: [dict(a) for a in self.algos])

        async def post(endpoint, params, single_attempt=False):
            self.algos.append({"symbol": "ETHUSDT", "side": "sell", "type": params["type"],
                               "stopPrice": params["triggerPrice"], "closeOrder": True,
                               "isActive": True, "clientOid": params["clientAlgoId"]})
            return {"algoId": str(len(self.algos))}
        self.client._post = AsyncMock(side_effect=post)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    async def test_stale_stop_of_previous_trade_is_never_reused(self):
        self.client._protection_lineage["ETHUSDT"] = "bgx7-trade-a"
        self.assertTrue(await self.client.set_position_stops("ETHUSDT", sl=99.5, tp=104))
        self.assertEqual(self.client._post.await_count, 2)
        self.client._protection_lineage["ETHUSDT"] = "bgx7-trade-b"    # new opening order
        self.assertTrue(await self.client.set_position_stops("ETHUSDT", sl=99.5, tp=104))
        self.assertEqual(self.client._post.await_count, 4, "same trigger, other lineage: new orders")

    async def test_retry_within_the_same_lineage_reuses_its_own_orders(self):
        self.client._protection_lineage["ETHUSDT"] = "bgx7-trade-a"
        await self.client.set_position_stops("ETHUSDT", sl=99.5, tp=104)
        await self.client.set_position_stops("ETHUSDT", sl=99.5, tp=104)
        self.assertEqual(self.client._post.await_count, 2, "idempotent retry, no duplicate")

    async def test_unknown_bgx_order_after_restart_is_not_adopted(self):
        self.algos.append({"symbol": "ETHUSDT", "side": "sell", "type": "STOP_MARKET", "stopPrice": "99.5",
                           "closeOrder": True, "isActive": True, "clientOid": "bgx7-unknown"})
        await self.client.set_position_stops("ETHUSDT", sl=99.5)
        self.assertEqual(self.client._post.await_count, 1)


if __name__ == "__main__":
    unittest.main()
