import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot import binance_protection_failclosed as protection
from bot import pilot_external_position_guard as guard


class DummyBinanceEngine:
    async def _open(self, sig, *args, **kwargs):
        return "open-original"

    async def _load_existing_positions(self):
        rows = await self.client.get_positions()
        for row in rows:
            if abs(float(row.get("size", 0) or 0)) <= 0:
                continue
            self.positions[row["symbol"]] = SimpleNamespace(
                qty=abs(float(row["size"])),
                direction="LONG" if row.get("side") == "Buy" else "SHORT",
            )
        return "loaded"

    async def _guard_naked_positions(self):
        return "guard-original"

    async def _sync_positions(self):
        return "sync-original"

    async def _reconcile_exchange_positions(self, only_symbol=None):
        return [f"reconcile:{only_symbol}"]


class BinanceExternalPositionGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = Mock()
        protection.install(DummyBinanceEngine, self.log)
        guard.install(
            DummyBinanceEngine,
            self.log,
            exchange_name="binance",
        )
        self.engine = DummyBinanceEngine()
        self.engine.paper_trade = False
        self.engine.positions = {}
        self.engine._trade_ids = {}
        self.engine._unprotected_symbols = set()
        self.engine.client = SimpleNamespace(
            _instruments={
                "BTCUSDT": {
                    "multiplier": 1.0,
                    "minQty": 0.001,
                    "qtyStep": 0.001,
                    "tickSize": 0.1,
                }
            },
            get_positions=AsyncMock(return_value=[
                {
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": 0.01,
                    "sizeUnit": "BASE_ASSET",
                    "entryPrice": 60000,
                    "markPrice": 60100,
                    "stopLoss": 0,
                }
            ]),
            get_stop_orders=AsyncMock(return_value=[]),
        )
        self.engine.orders = SimpleNamespace(snapshot=lambda: [])

    async def test_manual_binance_position_is_not_adopted_on_startup(self):
        result = await self.engine._load_existing_positions()
        self.assertEqual(result, "loaded")
        self.assertNotIn("BTCUSDT", self.engine.positions)
        self.assertIn("BTCUSDT", self.engine._external_position_symbols)
        self.assertIn("BTCUSDT", self.engine._restart_ownership_proofs)
        proof = self.engine._restart_ownership_proofs["BTCUSDT"]
        self.assertFalse(proof.recovered)

    async def test_external_binance_position_is_never_scoped_reconciled(self):
        await self.engine._load_existing_positions()
        result = await self.engine._reconcile_exchange_positions(
            only_symbol="BTCUSDT"
        )
        self.assertIsNone(result)
        self.assertNotIn("BTCUSDT", self.engine.positions)

    async def test_external_unprotected_binance_position_blocks_without_mutation(self):
        await self.engine._load_existing_positions()
        result = await self.engine._guard_naked_positions()
        self.assertIsNone(result)
        self.assertTrue(self.engine._pilot_external_position_guard_blocked)
        self.assertIn("BTCUSDT", self.engine._unprotected_symbols)
        self.engine.client.get_stop_orders.assert_awaited()


if __name__ == "__main__":
    unittest.main()
