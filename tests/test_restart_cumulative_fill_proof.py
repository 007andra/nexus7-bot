"""F-013A restart: exposure explained by the CUMULATIVE fills of the same opening
order (late fills during downtime) is still BGX — proven read-only from
orderId/clientOid/symbol/side, OPEN lifecycle, fills continuity, exchange
cumulative filled qty, ledger VWAP and confirmed protection. Anything else
stays EXTERNAL."""
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import bot.restart_ownership_recovery as recovery
from bot import trade_lifecycle
from bot.order_state import OrderState
from tests.test_restart_ownership_recovery import _Client, _Engine

LATE_POSITION = {"symbol": "ETHUSDT", "size": 0.42, "sizeUnit": "BASE_ASSET", "sizeContracts": 42,
                 "side": "Buy", "entryPrice": 2535.0, "markPrice": 2534.0, "stopLoss": 0}


def _fill(tid, size, price, order_id="oid-1", side="buy"):
    return {"tradeId": tid, "orderId": order_id, "symbol": "ETHUSDTM", "side": side,
            "size": str(size), "price": str(price), "fee": "0", "feeCurrency": "USDT",
            "tradeTime": (int(time.time() * 1000) - 20_000) * 1_000_000, "tradeType": "trade"}


class CumulativeFillProofTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {}

        async def load(key, strict=False):
            return self.store.get(key)
        p = patch("bot.database.load_key_value", AsyncMock(side_effect=load))
        p.start()
        self.addCleanup(p.stop)

    def _engine(self, *, filled="42", late_price=2540.0, extra_fills=(), status=None, opening_qty=0.21):
        client = _Client(status=status or {
            "orderId": "oid-1", "clientOid": "bgx7-owned", "symbol": "ETHUSDTM", "side": "buy",
            "isActive": False, "cancelExist": False, "filledSize": filled},
            stops=[{"symbol": "ETHUSDTM", "side": "sell", "stopPrice": "2500", "reduceOnly": True,
                    "closeOrder": False, "size": "42", "isActive": True, "stopTriggered": False}])
        engine = _Engine(client)
        order, _ = engine.orders.get_or_create("bgx7-owned", "ETHUSDT", "Buy", 0.42)  # requested
        order.transition(OrderState.SUBMITTING, source="LOCAL")
        order.transition(OrderState.SUBMITTED, source="REST", order_id="oid-1")
        order.transition(OrderState.FILLED, source="REST", order_id="oid-1", filled_qty=0.21,
                         avg_price=2530.0)                                       # partial at the time
        engine.orders.index_order_id("oid-1", "bgx7-owned")
        self.store[trade_lifecycle._key("oid-1")] = json.dumps({
            "version": 1, "opening_order_id": "oid-1", "client_oid": "bgx7-owned", "symbol": "ETHUSDT",
            "direction": "LONG", "entry": 2530.0, "opening_qty": opening_qty,
            "confirmed_reduced_qty": 0.0, "status": "OPEN", "opened_at_ms": int(time.time() * 1000),
            "closed_at_ms": None, "close_reason": None})
        client.fills = [_fill("t1", 21, 2530.0), _fill("t2", 21, late_price), *extra_fills]
        return engine

    async def test_late_fill_during_downtime_is_proven_from_cumulative_fills(self):
        proof = await recovery.prove_restart_ownership(self._engine(), dict(LATE_POSITION))
        self.assertTrue(proof.recovered)
        self.assertEqual((proof.reason, proof.base_qty, proof.order_id),
                         ("cumulative_entry_fill_proof", 0.42, "oid-1"))

    async def test_exchange_cumulative_fill_not_explaining_exposure_is_rejected(self):
        proof = await recovery.prove_restart_ownership(self._engine(filled="21"), dict(LATE_POSITION))
        self.assertFalse(proof.recovered)

    async def test_manual_add_from_other_order_is_rejected(self):
        manual = dict(_fill("m1", 10, 2540.0, order_id="manual"))
        manual["tradeTime"] += 1_000_000_000                       # after the opening fills
        engine = self._engine(extra_fills=[manual])
        position = dict(LATE_POSITION, size=0.52, sizeContracts=52)
        proof = await recovery.prove_restart_ownership(engine, position)
        self.assertFalse(proof.recovered)

    async def test_vwap_must_match_exchange_average_entry(self):
        proof = await recovery.prove_restart_ownership(self._engine(late_price=2700.0),
                                                       dict(LATE_POSITION))
        self.assertEqual((proof.recovered, proof.reason), (False, "trade_entry_mismatch"))

    async def test_identity_mismatch_is_rejected(self):
        status = {"orderId": "oid-1", "clientOid": "bgx7-other", "symbol": "ETHUSDTM", "side": "buy",
                  "isActive": False, "cancelExist": False, "filledSize": "42"}
        proof = await recovery.prove_restart_ownership(self._engine(status=status), dict(LATE_POSITION))
        self.assertEqual((proof.recovered, proof.reason), (False, "exchange_client_oid_mismatch"))

    async def test_fill_above_requested_is_rejected(self):
        position = dict(LATE_POSITION, size=0.50, sizeContracts=50)
        proof = await recovery.prove_restart_ownership(self._engine(filled="50"), position)
        self.assertFalse(proof.recovered)


if __name__ == "__main__":
    unittest.main()
