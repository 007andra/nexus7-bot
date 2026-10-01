import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import binance
from bot.order_state import OrderRegistry


class BinanceReduceLineageTests(unittest.IsolatedAsyncioTestCase):
    async def test_reduce_only_captures_previous_position_qty_before_submission(self):
        with patch.object(binance, "PAPER_TRADE", False), patch.object(
            binance, "_assert_signing_credentials_available", return_value=None
        ):
            client = binance.BinanceClient()
            registry = OrderRegistry()
            client._order_registry = registry
            client._engine = SimpleNamespace(
                positions={"AAVEUSDT": SimpleNamespace(qty=2.8)}
            )
            client._assert_live_account_mode = AsyncMock(return_value=None)
            client._post = AsyncMock(
                return_value={"orderId": 12345, "clientOrderId": "bgx7-close-test"}
            )
            client._round_qty = lambda qty, symbol: "2.8"
            client.build_client_oid = lambda *args, **kwargs: "bgx7-close-test"

            result = await client.place_order(
                "AAVEUSDT", "Sell", 2.8, reduce_only=True
            )

        order = registry.get("bgx7-close-test")
        self.assertIsNotNone(order)
        self.assertTrue(order.reduce_only)
        self.assertEqual(order.exposure_intent, "REDUCE")
        self.assertAlmostEqual(order.previous_position_qty, 2.8)
        self.assertEqual(result["orderId"], "12345")
        submitted = client._post.await_args.args[1]
        self.assertEqual(submitted["reduceOnly"], "true")

    async def test_reduce_only_lineage_failure_never_blocks_risk_reducing_order(self):
        with patch.object(binance, "PAPER_TRADE", False), patch.object(
            binance, "_assert_signing_credentials_available", return_value=None
        ):
            client = binance.BinanceClient()
            registry = OrderRegistry()
            client._order_registry = registry
            client._engine = None
            client._assert_live_account_mode = AsyncMock(return_value=None)
            client._post = AsyncMock(
                return_value={"orderId": 54321, "clientOrderId": "bgx7-close-no-engine"}
            )
            client._round_qty = lambda qty, symbol: "1.0"
            client.build_client_oid = lambda *args, **kwargs: "bgx7-close-no-engine"

            result = await client.place_order(
                "AAVEUSDT", "Sell", 1.0, reduce_only=True
            )

        order = registry.get("bgx7-close-no-engine")
        self.assertIsNone(order.previous_position_qty)
        self.assertEqual(result["orderId"], "54321")
        client._post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
