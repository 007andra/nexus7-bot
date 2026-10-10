import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import prelive_readonly_probe as probe


class PreliveReadonlyProbeTests(unittest.TestCase):
    def test_active_order_normalization(self):
        self.assertEqual(len(probe._active_orders([{"id": "1"}])), 1)
        self.assertEqual(len(probe._active_orders({"items": [{"id": "1"}]})), 1)
        self.assertEqual(probe._active_orders({}), [])

    def test_probe_contains_no_exchange_mutation_calls(self):
        src = inspect.getsource(probe)
        forbidden = (
            "place_order(", "cancel_all_orders(", "set_leverage(",
            "set_position_stops(", "set_sl(", "trading-stop",
            "closeOrder", "reduceOnly",
        )
        for token in forbidden:
            self.assertNotIn(token, src)

    def test_private_probe_keeps_kucoin_private_channel(self):
        src = inspect.getsource(probe._kucoin_private_ws_probe)
        self.assertIn('/api/v1/bullet-private', src)
        self.assertIn('"privateChannel": True', src)
        self.assertIn('/contractMarket/tradeOrders:', src)

    def test_private_probe_dispatches_binance_by_listen_key_capability(self):
        client = SimpleNamespace(
            _listen_key_request=AsyncMock(return_value={"listenKey": "lk"})
        )
        with patch.object(
            probe,
            "_binance_private_ws_probe",
            AsyncMock(return_value=True),
        ) as binance_probe, patch.object(
            probe,
            "_kucoin_private_ws_probe",
            AsyncMock(return_value=False),
        ) as kucoin_probe:
            result = asyncio.run(
                probe._private_ws_probe(client, "BTCUSDT")
            )
        self.assertTrue(result)
        binance_probe.assert_awaited_once_with(client, "BTCUSDT")
        kucoin_probe.assert_not_awaited()

    def test_private_probe_dispatches_kucoin_without_listen_key_capability(self):
        client = SimpleNamespace()
        with patch.object(
            probe,
            "_binance_private_ws_probe",
            AsyncMock(return_value=False),
        ) as binance_probe, patch.object(
            probe,
            "_kucoin_private_ws_probe",
            AsyncMock(return_value=True),
        ) as kucoin_probe:
            result = asyncio.run(
                probe._private_ws_probe(client, "BTCUSDT")
            )
        self.assertTrue(result)
        kucoin_probe.assert_awaited_once_with(client, "BTCUSDT")
        binance_probe.assert_not_awaited()

    def test_binance_probe_contains_no_order_submission(self):
        src = inspect.getsource(probe._binance_private_ws_probe)
        self.assertIn('_listen_key_request("POST")', src)
        self.assertIn("ws.ping()", src)
        self.assertNotIn("place_order", src)
        self.assertNotIn("/fapi/v1/order", src)


if __name__ == "__main__":
    unittest.main()
