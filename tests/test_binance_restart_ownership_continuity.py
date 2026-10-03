"""NOVO-02 (Binance port) — historical FILLED evidence never grants ownership of
current exposure without trade-ledger continuity from the opening order.
"""
import os
import time
import unittest

os.environ.setdefault("EXCHANGE", "binance")

import bot.restart_ownership_recovery as recovery  # noqa: E402
from bot.binance import BinanceClient  # noqa: E402
from tests.test_restart_ownership_recovery import POSITION, _Engine, _filled_order  # noqa: E402

INFO = {"ETHUSDT": {"quantityUnit": "BASE_ASSET", "qtyStep": "0.001", "minQty": "0.001",
                    "minNotional": "0", "multiplier": 1.0}}


class _LedgerClient(BinanceClient):
    def __init__(self, trades, *, fail=False):
        super().__init__()
        self._instruments = INFO
        self.trades, self.fail = trades, fail

    def get_instruments(self):
        return self._instruments

    async def get_order_status(self, order_id):
        return {"orderId": "oid-1", "clientOid": "bgx7-owned", "symbol": "ETHUSDT", "side": "buy",
                "isActive": False, "cancelExist": False, "filledSize": "0.21"}

    async def get_stop_orders(self, symbol):
        return [{"symbol": "ETHUSDT", "side": "sell", "stopPrice": "2500", "closeOrder": True,
                 "isActive": True, "type": "STOP_MARKET", "clientOid": "x"}]

    async def _get(self, endpoint, params=None, auth=False):
        if endpoint != "/fapi/v1/userTrades":
            return {}
        if self.fail:
            raise RuntimeError("offline ledger outage")
        lo, hi = params["startTime"], params["endTime"]
        return [t for t in self.trades if lo <= t["time"] <= hi]


def _trade(tid, order_id, side, qty, offset_s):
    return {"symbol": "ETHUSDT", "id": tid, "orderId": order_id, "side": side, "qty": str(qty),
            "price": "2530", "time": int((time.time() + offset_s) * 1000)}


class BinanceOwnershipContinuityTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, trades, **kw):
        engine = _Engine(_LedgerClient(trades, **kw))
        order = _filled_order(engine)
        order.created_at = time.time() - 3600
        return engine

    async def test_open_lineage_with_continuous_ledger_is_recovered(self):
        engine = self._engine([_trade(1, "oid-1", "BUY", 0.21, -3500)])
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertTrue(proof.recovered, proof.reason)

    async def test_closed_trade_record_never_owns_a_new_same_qty_position(self):
        engine = self._engine([
            _trade(1, "oid-1", "BUY", 0.21, -3500),          # BGX trade A opened
            _trade(2, "oid-close", "SELL", 0.21, -3000),     # ... and closed
            _trade(3, "manual-9", "BUY", 0.21, -100),        # manual position, same qty
        ])
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "lineage_flat_in_trade_ledger")

    async def test_exposure_added_by_another_order_is_rejected(self):
        engine = self._engine([
            _trade(1, "oid-1", "BUY", 0.11, -3500),
            _trade(2, "manual-9", "BUY", 0.10, -100),
        ])
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertEqual((proof.recovered, proof.reason), (False, "exposure_added_after_open"))

    async def test_unreadable_ledger_fails_closed(self):
        engine = self._engine([], fail=True)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertEqual((proof.recovered, proof.reason), (False, "trade_ledger_unconfirmed"))

    async def test_opening_fill_missing_fails_closed(self):
        engine = self._engine([_trade(3, "manual-9", "BUY", 0.21, -100)])
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertEqual((proof.recovered, proof.reason), (False, "opening_fill_missing_in_ledger"))


class RestartGeometryTests(unittest.IsolatedAsyncioTestCase):
    """Q-01 (Binance): recovered positions never manage from the startup estimate."""

    def _engine(self, orders):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        pos = SimpleNamespace(symbol="ETHUSDT", direction="LONG", entry=2530.0, sl=2503.4, tp=2583.1,
                              trailing_sl=2503.4, qty=0.21, current_price=2600.0, tp1_hit=False,
                              _forensic_lineage={"opening_order_id": "oid-1"})
        client = SimpleNamespace(get_stop_orders=AsyncMock(return_value=orders))
        return SimpleNamespace(positions={"ETHUSDT": pos}, client=client, paper_trade=False), pos

    def _order(self, kind, price):
        return {"symbol": "ETHUSDT", "side": "sell", "type": kind, "stopPrice": str(price),
                "closeOrder": True, "isActive": True, "clientOid": "bgx7-a"}

    async def test_exchange_protection_replaces_startup_estimate(self):
        from bot.logger import log
        from bot.restart_opening_order_lineage import restore_exchange_geometry
        engine, pos = self._engine([self._order("STOP_MARKET", 2480), self._order("TAKE_PROFIT_MARKET", 2650)])
        self.assertTrue(await restore_exchange_geometry(engine, "ETHUSDT", log))
        self.assertEqual((pos.sl, pos.trailing_sl, pos.tp, pos._geometry_unproven), (2480.0, 2480.0, 2650.0, False))

    async def test_missing_target_keeps_r_exits_disabled(self):
        from unittest.mock import AsyncMock, patch
        from bot import confirmed_rr_exit
        from bot.logger import log
        from bot.restart_opening_order_lineage import restore_exchange_geometry
        engine, pos = self._engine([self._order("STOP_MARKET", 2480)])
        self.assertFalse(await restore_exchange_geometry(engine, "ETHUSDT", log))
        self.assertTrue(pos._geometry_unproven)
        identity = AsyncMock()
        with patch.object(confirmed_rr_exit, "durable_identity", identity):
            await confirmed_rr_exit.check(engine)          # price is far beyond 2R of the estimate
        identity.assert_not_awaited()

    async def test_unreadable_protection_is_unproven(self):
        from unittest.mock import AsyncMock
        from bot.logger import log
        from bot.restart_opening_order_lineage import restore_exchange_geometry
        engine, pos = self._engine([])
        engine.client.get_stop_orders = AsyncMock(side_effect=RuntimeError("down"))
        self.assertFalse(await restore_exchange_geometry(engine, "ETHUSDT", log))
        self.assertTrue(pos._geometry_unproven)
        self.assertEqual(pos.sl, 2503.4, "nothing invented, nothing changed")


if __name__ == "__main__":
    unittest.main()
