import unittest
from unittest.mock import AsyncMock, patch

from bot import private_ws_readonly_observability as obs


class _Client:
    def __init__(self, positions, active_orders=None, stop_orders=None):
        self.get_positions = AsyncMock(return_value=positions)
        self._active_orders = active_orders or []
        self._stop_orders = stop_orders

    async def get_open_orders(self):
        return list(self._active_orders)

    async def get_stop_orders(self, symbol):
        if isinstance(self._stop_orders, Exception):
            raise self._stop_orders
        return list(self._stop_orders or [])


class _Log:
    warning = staticmethod(lambda *args, **kwargs: None)
    info = staticmethod(lambda *args, **kwargs: None)


class ProtectedExternalPreliveTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, client):
        with patch("bot.prelive_readonly_probe._private_ws_probe", AsyncMock(return_value=True)):
            return await obs.run(client, {"ETHUSDT": {}}, _Log())

    async def test_conditional_close_order_allows_readonly_prelive(self):
        position = {
            "symbol": "XRPUSDT",
            "size": "10",
            "side": "Buy",
            "markPrice": "2.0",
            "stopLoss": "0",
        }
        stop = {
            "symbol": "XRPUSDTM",
            "side": "sell",
            "stopPrice": "1.8",
            "closeOrder": True,
            "stopTriggered": False,
        }
        client = _Client([position], stop_orders=[stop])
        self.assertTrue(await self._run(client))
        self.assertTrue(client._prelive_account_exposure_clear)
        self.assertEqual(client._prelive_protected_positions, 1)
        self.assertEqual(client._prelive_unprotected_positions, 0)

    async def test_unprotected_position_blocks(self):
        position = {
            "symbol": "XRPUSDT",
            "size": "10",
            "side": "Buy",
            "markPrice": "2.0",
            "stopLoss": "0",
        }
        client = _Client([position], stop_orders=[])
        self.assertFalse(await self._run(client))
        self.assertFalse(client._prelive_account_exposure_clear)
        self.assertEqual(client._prelive_unprotected_positions, 1)

    async def test_stop_read_failure_blocks_fail_closed(self):
        position = {
            "symbol": "XRPUSDT",
            "size": "10",
            "side": "Buy",
            "markPrice": "2.0",
            "stopLoss": "0",
        }
        client = _Client([position], stop_orders=RuntimeError("read failed"))
        self.assertFalse(await self._run(client))
        self.assertFalse(client._prelive_account_exposure_clear)
        self.assertEqual(client._prelive_unprotected_positions, 1)

    async def test_active_normal_order_blocks_even_when_position_protected(self):
        position = {
            "symbol": "XRPUSDT",
            "size": "10",
            "side": "Buy",
            "markPrice": "2.0",
            "stopLoss": "1.8",
        }
        client = _Client([position], active_orders=[{"id": "normal-order"}])
        self.assertFalse(await self._run(client))
        self.assertFalse(client._prelive_account_exposure_clear)

    async def test_zero_exposure_passes(self):
        client = _Client([])
        self.assertTrue(await self._run(client))
        self.assertTrue(client._prelive_account_exposure_clear)
        self.assertEqual(client._prelive_protected_positions, 0)
        self.assertEqual(client._prelive_unprotected_positions, 0)


if __name__ == "__main__":
    unittest.main()
