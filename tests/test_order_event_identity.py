"""F-011 — external order events must prove identity before any mutation."""
import os
import random
import time
import unittest
from types import SimpleNamespace

os.environ.setdefault("PAPER_TRADE", "true")

from bot.durable_live_reconciliation import apply_exchange_order_truth
from bot.kucoin import KuCoinClient
from bot.order_event_identity import OrderEventRejected
from bot.order_state import OrderRegistry, OrderState

BTC = {"multiplier": 0.001, "lotSize": 1, "minQty": 1, "tickSize": 0.1, "minNotional": 0}


def _setup(qty=2.0, coid="bgx7-A-client", order_id="A", side="Buy", reduce_only=False):
    client = KuCoinClient()
    client._instruments = {"BTCUSDT": dict(BTC)}
    registry = OrderRegistry()
    client._order_registry = registry
    persisted = []

    async def persist(order):
        persisted.append(order.to_record())
    registry.persist_callback = persist
    order, _ = registry.get_or_create(coid, "BTCUSDT", side, qty)
    order.reduce_only = reduce_only
    order.transition(OrderState.SUBMITTING, source="LOCAL")
    if order_id:
        order.transition(OrderState.SUBMITTED, order_id=order_id, source="REST")
        registry.index_order_id(order_id, coid)
    return client, registry, order, persisted


def _ev(**data):
    base = {"symbol": "XBTUSDTM", "side": "buy", "ts": int(time.time() * 1e9)}
    base.update(data)
    return {"subject": "symbolOrderChange", "data": {k: v for k, v in base.items() if v is not None}}


def _snapshot(order):
    return (order.state, order.filled_qty, order.order_id, len(order.history))


class OrderEventIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_exact_identity_updates_correct_order(self):
        client, registry, order, persisted = _setup()
        await client._handle_private_order_event(_ev(
            orderId="A", clientOid=order.client_oid, type="filled", status="done",
            filledSize="2", size="2", matchPrice="100"))
        self.assertEqual((order.state, order.filled_qty), (OrderState.FILLED, 2.0))
        self.assertEqual(len(persisted), 1)

    async def test_b_foreign_symbol_never_updates(self):     # main F-011 regression
        client, registry, order, persisted = _setup()
        before = _snapshot(order)
        await client._handle_private_order_event(_ev(
            orderId="B", clientOid=order.client_oid, symbol="ETHUSDTM", side="sell",
            type="filled", status="done", filledSize="7", size="7", matchPrice="1"))
        self.assertEqual(_snapshot(order), before)
        self.assertNotEqual(order.filled_qty, 7)
        self.assertIsNone(registry.get_by_order_id("B"))
        self.assertEqual(persisted, [])

    async def test_c_order_id_conflict_with_foreign_symbol(self):
        client, registry, order, persisted = _setup(order_id="123")
        before = _snapshot(order)
        await client._handle_private_order_event(_ev(
            orderId="123", symbol="ETHUSDTM", type="filled", status="done",
            filledSize="2", size="2"))
        self.assertEqual((_snapshot(order), persisted), (before, []))

    async def test_d_client_oid_conflicts(self):
        cases = [dict(orderId="OTHER"), dict(symbol="ETHUSDTM"), dict(side="sell")]
        for extra in cases:
            with self.subTest(extra=extra):
                client, registry, order, persisted = _setup()
                before = _snapshot(order)
                payload = dict(orderId="A", clientOid=order.client_oid, type="filled",
                               status="done", filledSize="2", size="2")
                payload.update(extra)
                await client._handle_private_order_event(_ev(**payload))
                self.assertEqual((_snapshot(order), persisted), (before, []))

    async def test_identifier_conflict_between_two_orders(self):
        client, registry, order, persisted = _setup()
        other, _ = registry.get_or_create("bgx7-other", "BTCUSDT", "Buy", 2.0)
        other.transition(OrderState.SUBMITTING, source="LOCAL")
        snaps = (_snapshot(order), _snapshot(other))
        await client._handle_private_order_event(_ev(
            orderId="A", clientOid="bgx7-other", type="filled", status="done",
            filledSize="2", size="2"))
        self.assertEqual((_snapshot(order), _snapshot(other)), snaps)

    async def test_e_missing_symbol_does_not_mutate(self):
        client, registry, order, persisted = _setup()
        before = _snapshot(order)
        await client._handle_private_order_event(_ev(
            symbol=None, orderId="A", clientOid=order.client_oid, type="filled",
            status="done", filledSize="2", size="2"))
        self.assertEqual((_snapshot(order), persisted), (before, []))

    async def test_f_missing_or_weak_ids_do_not_mutate(self):
        client, registry, order, persisted = _setup(order_id="")   # unbound orderId
        before = _snapshot(order)
        await client._handle_private_order_event(_ev(type="filled", status="done",
                                                     filledSize="2", size="2"))
        await client._handle_private_order_event(_ev(orderId="X", type="filled",
                                                     status="done", filledSize="2", size="2"))
        self.assertEqual((_snapshot(order), persisted), (before, []))

    async def test_g_h_child_stop_and_tp_events_do_not_touch_parent_entry(self):
        for bound in ("A", ""):
            for child in (dict(stop="down", stopPrice="95"), dict(stop="up", stopPrice="110")):
                with self.subTest(bound=bound, child=child):
                    client, registry, order, persisted = _setup(order_id=bound)
                    before = _snapshot(order)
                    # child leg: own orderId, close side, closeOrder, parent's clientOid
                    await client._handle_private_order_event(_ev(
                        orderId="child-1", clientOid=order.client_oid, side="sell",
                        closeOrder=True, reduceOnly=True, type="filled", status="done",
                        filledSize="2", size="2", **child))
                    # same, but child mislabelled with the entry side
                    await client._handle_private_order_event(_ev(
                        orderId="child-2", clientOid=order.client_oid, side="buy",
                        closeOrder=True, type="filled", status="done", filledSize="2",
                        size="2", **child))
                    self.assertEqual((_snapshot(order), persisted), (before, []))

    async def test_i_duplicate_partial_is_idempotent(self):
        client, registry, order, persisted = _setup(qty=10.0)
        ev = _ev(orderId="A", clientOid=order.client_oid, type="match", status="open",
                 filledSize="4", matchSize="4", size="10", matchPrice="100")
        for _ in range(10):
            await client._handle_private_order_event(ev)
        self.assertEqual((order.state, order.filled_qty), (OrderState.PARTIALLY_FILLED, 4.0))
        self.assertEqual(len(persisted), 1)
        transitions = [h for h in order.history if h[2] == "PARTIALLY_FILLED"]
        self.assertEqual(len(transitions), 1)

    async def test_j_k_cumulative_fill_semantics(self):
        client, registry, order, _ = _setup(qty=10.0)
        # KuCoin: filledSize is cumulative, matchSize is the per-match delta.
        for cum, delta in ((2, 2), (5, 3), (10, 5)):
            status = "done" if cum == 10 else "open"
            await client._handle_private_order_event(_ev(
                orderId="A", clientOid=order.client_oid, type="match", status=status,
                filledSize=str(cum), matchSize=str(delta), size="10"))
            self.assertEqual(order.filled_qty, float(cum), "cumulative, not summed")
        self.assertEqual(order.state, OrderState.FILLED)

    async def test_l_overfill_rejected_and_never_persisted(self):
        for size, filled in (("2", "7"), ("7", "7"), (None, "7")):
            with self.subTest(size=size):
                client, registry, order, persisted = _setup()
                before = _snapshot(order)
                await client._handle_private_order_event(_ev(
                    orderId="A", clientOid=order.client_oid, type="filled", status="done",
                    filledSize=filled, size=size))
                self.assertEqual((_snapshot(order), persisted), (before, []))

    async def test_base_unit_entry_accepts_contract_fill(self):
        client, registry, order, _ = _setup(qty=0.002)          # engine entries: base asset
        await client._handle_private_order_event(_ev(
            orderId="A", clientOid=order.client_oid, type="filled", status="done",
            filledSize="2", size="2"))
        self.assertEqual((order.state, order.filled_qty), (OrderState.FILLED, 2.0))

    async def test_m_stale_events_never_regress(self):
        client, registry, order, persisted = _setup(qty=10.0)
        await client._handle_private_order_event(_ev(
            orderId="A", clientOid=order.client_oid, type="filled", status="done",
            filledSize="10", size="10"))
        snap = _snapshot(order)
        for stale in (dict(type="match", status="open", filledSize="5"),
                      dict(type="canceled", status="done", filledSize="0"),
                      dict(type="open", status="open", filledSize="0")):
            await client._handle_private_order_event(_ev(
                orderId="A", clientOid=order.client_oid, size="10", **stale))
        self.assertEqual(_snapshot(order), snap)
        self.assertEqual(len(persisted), 1)

    async def test_partial_regression_rejected(self):
        client, registry, order, _ = _setup(qty=10.0)
        for cum in ("6", "3"):
            await client._handle_private_order_event(_ev(
                orderId="A", clientOid=order.client_oid, type="match", status="open",
                filledSize=cum, size="10"))
        self.assertEqual(order.filled_qty, 6.0)

    async def test_n_rest_ws_conflicts(self):
        engine = SimpleNamespace(orders=None, instruments={"BTCUSDT": dict(BTC)})
        # A: WS FILLED, then stale REST partial / open
        client, registry, order, _ = _setup(qty=10.0)
        engine.orders = registry
        await client._handle_private_order_event(_ev(
            orderId="A", clientOid=order.client_oid, type="filled", status="done",
            filledSize="10", size="10"))
        snap = _snapshot(order)
        for data in ({"isActive": True, "filledSize": "4", "size": "10"},
                     {"isActive": True, "filledSize": "0", "size": "10"}):
            apply_exchange_order_truth(engine, order, dict(data, clientOid=order.client_oid,
                                                           orderId="A"))
        self.assertEqual(_snapshot(order), snap)
        # C: REST payload for another symbol
        client, registry, order, _ = _setup()
        engine.orders = registry
        snap = _snapshot(order)
        with self.assertRaises(ValueError):
            apply_exchange_order_truth(engine, order, {"symbol": "ETHUSDTM", "isActive": False,
                                                       "filledSize": "2", "size": "2"})
        # REST overfill
        with self.assertRaises(OrderEventRejected):
            apply_exchange_order_truth(engine, order, {"clientOid": order.client_oid,
                                                       "isActive": False, "filledSize": "7",
                                                       "size": "2"})
        self.assertEqual(_snapshot(order), snap)

    async def test_n_lost_response_then_ws_binds_then_rest_agrees(self):
        client, registry, order, _ = _setup(order_id="")
        await client._handle_private_order_event(_ev(
            orderId="kc-9", clientOid=order.client_oid, type="filled", status="done",
            filledSize="2", size="2"))
        self.assertEqual((order.state, order.order_id), (OrderState.FILLED, "kc-9"))
        self.assertIs(registry.get_by_order_id("kc-9"), order)
        engine = SimpleNamespace(orders=registry, instruments={"BTCUSDT": dict(BTC)})
        changed, terminal = apply_exchange_order_truth(engine, order, {
            "orderId": "kc-9", "clientOid": order.client_oid, "isActive": False,
            "filledSize": "2", "size": "2"})
        self.assertEqual((changed, terminal), (False, True))

    async def test_registry_refuses_order_id_rebind(self):
        client, registry, order, _ = _setup()
        other, _ = registry.get_or_create("bgx7-other", "BTCUSDT", "Buy", 2.0)
        self.assertFalse(registry.index_order_id("A", "bgx7-other"))
        self.assertIs(registry.get_by_order_id("A"), order)
        self.assertFalse(registry.index_order_id("Z", order.client_oid))

    async def test_o_p_persistence_and_restart(self):
        client, registry, order, persisted = _setup(qty=10.0)
        await client._handle_private_order_event(_ev(
            orderId="A", clientOid=order.client_oid, type="match", status="open",
            filledSize="4", size="10"))
        await client._handle_private_order_event(_ev(        # foreign contamination attempt
            orderId="B", clientOid=order.client_oid, symbol="ETHUSDTM", side="sell",
            type="filled", status="done", filledSize="7", size="7"))
        snapshot = registry.snapshot()
        self.assertEqual([(r["symbol"], r["state"], r["filled_qty"]) for r in snapshot],
                         [("BTCUSDT", "PARTIALLY_FILLED", 4.0)])
        self.assertTrue(all(p["filled_qty"] != 7.0 for p in persisted))
        # restart
        restored = OrderRegistry()
        restored.restore(snapshot)
        client2 = KuCoinClient()
        client2._instruments = {"BTCUSDT": dict(BTC)}
        client2._order_registry = restored
        r_order = restored.get(order.client_oid)
        for data in (dict(type="match", status="open", filledSize="4"),   # duplicate
                     dict(type="match", status="open", filledSize="2"),   # stale
                     dict(type="filled", status="done", filledSize="10")):
            await client2._handle_private_order_event(_ev(
                orderId="A", clientOid=order.client_oid, size="10", **data))
        self.assertEqual((r_order.state, r_order.filled_qty), (OrderState.FILLED, 10.0))
        self.assertIs(restored.get_by_order_id("A"), r_order)

    async def test_property_contradicting_identity_never_mutates(self):
        rng = random.Random(11)
        for _ in range(400):
            client, registry, order, persisted = _setup()
            fields = {
                "symbol": rng.choice(["XBTUSDTM", "BTCUSDT", "ETHUSDTM", "SOLUSDTM", None]),
                "orderId": rng.choice(["A", "B", None]),
                "clientOid": rng.choice([order.client_oid, "bgx7-zzz", None]),
                "side": rng.choice(["buy", "BUY", "Buy", "sell", "LONG", None]),
            }
            contradicts = (
                fields["symbol"] not in ("XBTUSDTM", "BTCUSDT")
                or fields["orderId"] == "B"
                or fields["clientOid"] == "bgx7-zzz"
                or fields["side"] in ("sell", "LONG")
                or (fields["orderId"] is None and fields["clientOid"] is None)
            )
            before = _snapshot(order)
            await client._handle_private_order_event(_ev(
                type="filled", status="done", filledSize="2", size="2", **fields))
            if contradicts:
                self.assertEqual(_snapshot(order), before, fields)
                self.assertEqual(persisted, [], fields)
            else:
                self.assertEqual(order.state, OrderState.FILLED, fields)


if __name__ == "__main__":
    unittest.main()
