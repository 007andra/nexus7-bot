import ast
import inspect
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import bot.restart_ownership_recovery as recovery
from bot.order_state import OrderRegistry, OrderState


POSITION = {
    "symbol": "ETHUSDT",
    "size": 0.21,
    "sizeUnit": "BASE_ASSET",
    "sizeContracts": 21,
    "side": "Buy",
    "entryPrice": 2530.0,
    "markPrice": 2525.0,
    "stopLoss": 0,
}

AVAX_POSITION = {
    "symbol": "AVAXUSDT",
    "size": 30.0,
    "sizeUnit": "BASE_ASSET",
    "sizeContracts": 300,
    "side": "Sell",
    "entryPrice": 27.0,
    "markPrice": 26.8,
    "stopLoss": 0,
}


class _Client:
    def __init__(self, *, status=None, stops=None, instruments=None):
        self._instruments = instruments if instruments is not None else {
            "ETHUSDT": {"multiplier": "0.01", "lotSize": "1", "minQty": "1"}
        }
        self.status = status if status is not None else {
            "orderId": "oid-1",
            "clientOid": "bgx7-owned",
            "symbol": "ETHUSDTM",
            "side": "buy",
            "isActive": False,
            "cancelExist": False,
            "filledSize": "21",
        }
        self.stops = stops if stops is not None else [{
            "symbol": "ETHUSDTM",
            "side": "sell",
            "stopPrice": "2500",
            "reduceOnly": True,
            "closeOrder": False,
            "size": "21",
            "isActive": True,
            "stopTriggered": False,
        }]

        self.fills = []   # exchange fill ledger (NOVO-02 continuity proof)

    def get_instruments(self):
        return self._instruments

    async def _get(self, endpoint, params=None, auth=False):
        if endpoint != "/api/v1/fills":
            return {}
        return {"currentPage": 1, "totalPage": 1, "totalNum": len(self.fills), "items": list(self.fills)}

    async def get_order_status(self, order_id):
        return dict(self.status)

    async def get_stop_orders(self, symbol):
        return list(self.stops)


class _Engine:
    def __init__(self, client=None):
        self.client = client or _Client()
        self.orders = OrderRegistry()


_STORE = {}
_ENTRY = {"ETHUSDT": 2530.0, "AVAXUSDT": 27.0}


def _open_lineage(engine, *, order_id, client_oid, symbol, side, qty):
    """What a still-OPEN BGX trade leaves behind (NOVO-02): an OPEN durable
    lifecycle and the opening fill in the exchange ledger."""
    from bot import trade_lifecycle
    _STORE[trade_lifecycle._key(order_id)] = json.dumps({
        "version": 1, "opening_order_id": order_id, "client_oid": client_oid, "symbol": symbol,
        "direction": "LONG" if side == "Buy" else "SHORT", "entry": _ENTRY.get(symbol, 2530.0),
        "opening_qty": qty, "confirmed_reduced_qty": 0.0, "status": "OPEN",
        "opened_at_ms": int(time.time() * 1000), "closed_at_ms": None, "close_reason": None})
    multiplier = float((engine.client._instruments.get(symbol) or {}).get("multiplier", 0.01))
    engine.client.fills.append({
        "tradeId": f"t-{order_id}", "orderId": order_id, "symbol": symbol + "M", "side": side.lower(),
        "size": str(round(qty / multiplier)), "price": "1", "fee": "0", "feeCurrency": "USDT",
        "tradeTime": (int(time.time() * 1000) - 30_000) * 1_000_000, "tradeType": "trade"})


def _filled_order(engine, *, client_oid="bgx7-owned", order_id="oid-1",
                  side="Buy", qty=0.21, filled_qty=0.21, symbol="ETHUSDT"):
    order, _ = engine.orders.get_or_create(client_oid, symbol, side, qty)
    order.transition(OrderState.SUBMITTING, source="LOCAL")
    order.transition(OrderState.SUBMITTED, source="REST", order_id=order_id)
    order.transition(
        OrderState.FILLED,
        source="REST",
        order_id=order_id,
        filled_qty=filled_qty,
        avg_price=2530.0,
    )
    engine.orders.index_order_id(order_id, client_oid)
    _open_lineage(engine, order_id=order_id, client_oid=client_oid, symbol=symbol, side=side, qty=qty)
    return order


class RestartOwnershipRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _STORE.clear()

        async def load(key, strict=False):
            return _STORE.get(key)
        p = patch("bot.database.load_key_value", AsyncMock(side_effect=load))
        p.start()
        self.addCleanup(p.stop)

    async def test_exact_durable_exchange_and_protection_proof_recovers(self):
        engine = _Engine()
        _filled_order(engine)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertTrue(proof.recovered)
        self.assertEqual(proof.reason, "exact_durable_exchange_proof")
        self.assertEqual(proof.base_qty, 0.21)
        self.assertEqual(proof.client_oid, "bgx7-owned")

    async def test_legacy_kucoin_contract_filled_qty_recovers_exact_avax_position(self):
        client = _Client(
            instruments={
                "AVAXUSDT": {"multiplier": "0.1", "lotSize": "1", "minQty": "1"}
            },
            status={
                "orderId": "oid-avax",
                "clientOid": "bgx7-avax-owned",
                "symbol": "AVAXUSDTM",
                "side": "sell",
                "isActive": False,
                "cancelExist": False,
                "filledSize": "300",
            },
            stops=[{
                "symbol": "AVAXUSDTM",
                "side": "buy",
                "stopPrice": "27.5",
                "reduceOnly": True,
                "closeOrder": False,
                "size": "300",
                "isActive": True,
                "stopTriggered": False,
            }],
        )
        engine = _Engine(client)
        # Historical production representation: qty is 30 AVAX base, while
        # filled_qty was persisted directly from KuCoin filledSize=300 contracts.
        _filled_order(
            engine,
            client_oid="bgx7-avax-owned",
            order_id="oid-avax",
            side="Sell",
            qty=30.0,
            filled_qty=300.0,
            symbol="AVAXUSDT",
        )

        proof = await recovery.prove_restart_ownership(engine, dict(AVAX_POSITION))

        self.assertTrue(proof.recovered)
        self.assertEqual(proof.reason, "exact_durable_exchange_proof")
        self.assertEqual(proof.base_qty, 30.0)
        self.assertEqual(proof.order_id, "oid-avax")

    async def test_legacy_contract_filled_qty_must_convert_exactly(self):
        client = _Client(
            instruments={
                "AVAXUSDT": {"multiplier": "0.1", "lotSize": "1", "minQty": "1"}
            },
            status={
                "orderId": "oid-avax",
                "clientOid": "bgx7-avax-owned",
                "symbol": "AVAXUSDTM",
                "side": "sell",
                "isActive": False,
                "cancelExist": False,
                "filledSize": "300",
            },
            stops=[{
                "symbol": "AVAXUSDTM",
                "side": "buy",
                "stopPrice": "27.5",
                "reduceOnly": True,
                "closeOrder": False,
                "size": "300",
                "isActive": True,
                "stopTriggered": False,
            }],
        )
        engine = _Engine(client)
        _filled_order(
            engine,
            client_oid="bgx7-avax-owned",
            order_id="oid-avax",
            side="Sell",
            qty=30.0,
            filled_qty=299.0,
            symbol="AVAXUSDT",
        )

        proof = await recovery.prove_restart_ownership(engine, dict(AVAX_POSITION))

        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "no_exact_durable_fill")

    async def test_symbol_side_size_without_durable_fill_is_rejected(self):
        proof = await recovery.prove_restart_ownership(_Engine(), dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "no_exact_durable_fill")

    async def test_side_mismatch_is_rejected(self):
        engine = _Engine()
        _filled_order(engine, side="Sell")
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "no_exact_durable_fill")

    async def test_durable_quantity_mismatch_is_rejected(self):
        engine = _Engine()
        _filled_order(engine, qty=0.20, filled_qty=0.20)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        # NOVO-02: the quantity is now checked against the lineage residual.
        self.assertEqual(proof.reason, "trade_residual_mismatch")

    async def test_missing_order_id_is_rejected(self):
        engine = _Engine()
        order, _ = engine.orders.get_or_create("bgx7-owned", "ETHUSDT", "Buy", 0.21)
        order.transition(OrderState.SUBMITTING, source="LOCAL")
        order.transition(OrderState.FILLED, source="WS", filled_qty=0.21)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "no_exact_durable_fill")

    async def test_multiple_matching_durable_fills_are_ambiguous(self):
        engine = _Engine()
        _filled_order(engine, client_oid="bgx7-owned", order_id="oid-1")
        _filled_order(engine, client_oid="bgx7-owned-2", order_id="oid-2")
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "ambiguous_durable_fills")

    async def test_exchange_client_oid_mismatch_is_rejected(self):
        status = dict(_Client().status)
        status["clientOid"] = "bgx7-other"
        engine = _Engine(_Client(status=status))
        _filled_order(engine)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "exchange_client_oid_mismatch")

    async def test_exchange_order_id_mismatch_is_rejected(self):
        status = dict(_Client().status)
        status["orderId"] = "oid-other"
        engine = _Engine(_Client(status=status))
        _filled_order(engine)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "exchange_order_id_mismatch")

    async def test_exchange_fill_quantity_mismatch_is_rejected(self):
        status = dict(_Client().status)
        status["filledSize"] = "20"
        engine = _Engine(_Client(status=status))
        _filled_order(engine)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "exchange_fill_quantity_mismatch")

    async def test_active_exchange_order_is_rejected(self):
        status = dict(_Client().status)
        status["isActive"] = True
        engine = _Engine(_Client(status=status))
        _filled_order(engine)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "exchange_order_not_terminal")

    async def test_missing_native_protection_is_rejected(self):
        engine = _Engine(_Client(stops=[]))
        _filled_order(engine)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "protection_unconfirmed")

    async def test_missing_instrument_metadata_is_rejected(self):
        engine = _Engine(_Client(instruments={}))
        _filled_order(engine)
        proof = await recovery.prove_restart_ownership(engine, dict(POSITION))
        self.assertFalse(proof.recovered)
        self.assertEqual(proof.reason, "instrument_metadata_unconfirmed")

    def test_recovery_module_contains_no_exchange_mutation_calls(self):
        source = inspect.getsource(recovery)
        tree = ast.parse(source)
        forbidden = {
            "place_order", "cancel_order", "cancel_all_orders", "close_position",
            "set_position_stops", "set_sl", "set_leverage", "_post", "_delete",
        }
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        self.assertFalse(called & forbidden, called & forbidden)


if __name__ == "__main__":
    unittest.main()
