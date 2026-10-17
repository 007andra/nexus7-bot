"""Audited LINK -2019 recovery: exact, bounded, non-generalizable, convergent.

Every single-condition mutation of the incident evidence must leave the
intent SUBMITTING. The positive case must terminalize SUBMITTING -> REJECTED
with zero exchange mutation, persist before entries are released, and let the
private stream converge from reconcile_required to event_capable.
"""
from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import durable_reconcile_hardening as hardening
from bot import order_state
from bot.order_state import OrderRegistry, OrderState

LINK_OID = "bgx7-3c132f2a2fe68e76ba376fdfbedb70"
RAILWAY = {"RAILWAY_PROJECT_ID": "1443ec46-186f-497b-9e61-4b69446d35c3",
           "RAILWAY_ENVIRONMENT_ID": "e07566a4-170b-418d-9c91-b19604db8b3e"}


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from bot.binance import BinanceAPIError, BinanceClient
        self.BinanceAPIError = BinanceAPIError
        self.client = BinanceClient()
        self.addAsyncCleanup(self.client.close)
        self.engine = SimpleNamespace(
            orders=OrderRegistry(), errors={"orders"}, _durable_state_ok=False,
            persist_reasons=[], paper_trade=False, positions={}, client=self.client,
            connected=True,
        )
        self.order, _ = self.engine.orders.get_or_create(LINK_OID, "LINKUSDT", "Buy", 19.34)
        self.order.created_at = 1790667918
        self.order.transition(OrderState.SUBMITTING)
        self.responses = {
            "/fapi/v1/order": BinanceAPIError("GET", "/fapi/v1/order", 400, -2013, "Order does not exist"),
            "/fapi/v3/positionRisk": [{"symbol": "LINKUSDT", "positionAmt": "0"}],
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": [],
        }
        self.on_read = None
        self.reads = []

        async def read(endpoint, *args, **kwargs):
            self.reads.append(endpoint)
            if self.on_read:
                self.on_read(endpoint)
            result = self.responses[endpoint]
            if isinstance(result, Exception):
                raise result
            return result

        self.client._get = AsyncMock(side_effect=read)
        self.client.get_symbol_config = AsyncMock(
            return_value={"symbol": "LINKUSDT", "marginType": "CROSSED", "leverage": 50})
        for name in ("_post", "_delete", "_put", "place_order", "cancel_order",
                     "set_leverage", "set_margin_mode"):
            setattr(self.client, name, AsyncMock(side_effect=AssertionError(f"mutation {name}")))
        self.owner = AsyncMock(return_value=True)
        for p in (patch.dict("os.environ", RAILWAY),
                  patch("bot.durable_live_reconciliation._owner_valid", self.owner)):
            p.start()
            self.addCleanup(p.stop)

    async def recover(self):
        return await hardening._recover_audited_link_rejection(self.engine, self.order, _Log())

    def assert_no_mutation(self):
        for name in ("_post", "_delete", "_put", "place_order", "cancel_order",
                     "set_leverage", "set_margin_mode"):
            getattr(self.client, name).assert_not_awaited()
        self.assertTrue(set(self.reads) <= set(self.responses), self.reads)

    def assert_blocked(self, label):
        self.assertEqual(self.order.state, OrderState.SUBMITTING, label)
        self.assert_no_mutation()


class ExactnessMatrixTests(Harness):
    async def test_positive_case_terminalizes_rejected_once(self):
        self.assertTrue(await self.recover())
        self.assertEqual(self.order.state, OrderState.REJECTED)
        self.assertEqual(self.owner.await_count, 2)  # before AND after the reads
        self.assertFalse(await self.recover())       # idempotent: terminal now
        self.assert_no_mutation()

    async def test_order_identity_mutations_block(self):
        mutations = {
            "client_oid": "bgx7-3c132f2a2fe68e76ba376fdfbedb71",
            "symbol": "LINKUSDC", "side": "Sell", "qty": 19.35, "filled_qty": 0.01,
            "avg_price": 15.2, "order_id": "8389765", "reduce_only": True,
            "exposure_intent": "DECREASE", "created_at": 1790667899,
        }
        for field, bad in mutations.items():
            original = getattr(self.order, field)
            setattr(self.order, field, bad)
            self.assertFalse(await self.recover(), field)
            self.assert_blocked(field)
            setattr(self.order, field, original)
        self.order.created_at = 1790667923
        self.assertFalse(await self.recover(), "created_at after window")
        self.order.created_at = 1790667918

    async def test_state_other_than_submitting_blocks(self):
        registry = OrderRegistry()
        created, _ = registry.get_or_create(LINK_OID, "LINKUSDT", "Buy", 19.34)
        created.created_at = 1790667918
        self.assertFalse(await hardening._recover_audited_link_rejection(self.engine, created, _Log()))
        self.assertEqual(created.state, OrderState.CREATED)

    async def test_runtime_identity_mutations_block(self):
        cases = {
            "paper": lambda: setattr(self.engine, "paper_trade", True),
            "local_position": lambda: self.engine.positions.update({"BTCUSDT": object()}),
            "non_binance_adapter": lambda: setattr(self.engine, "client", SimpleNamespace(_get=self.client._get)),
        }
        for label, mutate in cases.items():
            saved = (self.engine.paper_trade, dict(self.engine.positions), self.engine.client)
            mutate()
            self.assertFalse(await self.recover(), label)
            self.assert_blocked(label)
            self.engine.paper_trade, self.engine.positions, self.engine.client = saved
            self.engine.positions = dict(saved[1])
        for key in RAILWAY:
            with patch.dict("os.environ", {key: "other"}):
                self.assertFalse(await self.recover(), key)
                self.assert_blocked(key)

    async def test_ownership_invalid_before_or_after_reads_blocks(self):
        self.owner.side_effect = [False]
        self.assertFalse(await self.recover())
        self.assertEqual(self.reads, [])  # no read before ownership proven
        self.owner.side_effect = [True, False]
        self.assertFalse(await self.recover())
        self.assert_blocked("ownership lost during reads")

    async def test_exchange_evidence_mutations_block(self):
        E = self.BinanceAPIError
        cases = [
            ("/fapi/v1/order", {"status": "NEW", "clientOrderId": LINK_OID}),
            ("/fapi/v1/order", {}),
            ("/fapi/v1/order", E("GET", "/fapi/v1/order", 400, -2011, "Unknown order")),
            ("/fapi/v1/order", E("GET", "/fapi/v1/order", 500, -2013, "server")),
            ("/fapi/v1/order", E("POST", "/fapi/v1/order", 400, -2013, "wrong method")),
            ("/fapi/v1/order", TimeoutError()),
            ("/fapi/v3/positionRisk", [{"symbol": "LINKUSDT", "positionAmt": "19.34"}]),
            ("/fapi/v3/positionRisk", [{"symbol": "BTCUSDT", "positionAmt": "-0.001"}]),
            ("/fapi/v3/positionRisk", [{"symbol": "LINKUSDT"}]),
            ("/fapi/v3/positionRisk", [{"positionAmt": "nan"}]),
            ("/fapi/v3/positionRisk", {}),
            ("/fapi/v1/openOrders", [{"orderId": 1}]),
            ("/fapi/v1/openOrders", {}),
            ("/fapi/v1/openAlgoOrders", [{"algoId": 1}]),
            ("/fapi/v1/openAlgoOrders", {"orders": [{"algoId": 1}]}),
            ("/fapi/v1/openAlgoOrders", {"unexpected": []}),
            ("/fapi/v1/openAlgoOrders", ConnectionError()),
        ]
        for endpoint, bad in cases:
            original = self.responses[endpoint]
            self.responses[endpoint] = bad
            self.assertFalse(await self.recover(), (endpoint, bad))
            self.assert_blocked((endpoint, bad))
            self.responses[endpoint] = original

    async def test_symbol_config_mutations_block(self):
        for bad in ({"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 50},
                    {"symbol": "LINKUSDT", "marginType": "ISOLATED", "leverage": 50},
                    {"symbol": "LINKUSDT", "marginType": "CROSSED", "leverage": 0},
                    {"symbol": "LINKUSDT", "marginType": "CROSSED", "leverage": 126},
                    {"symbol": "LINKUSDT", "marginType": "CROSSED", "leverage": "bad"},
                    {"symbol": "LINKUSDT", "marginType": "CROSSED"}):
            self.client.get_symbol_config.return_value = bad
            self.assertFalse(await self.recover(), bad)
            self.assert_blocked(bad)
        self.client.get_symbol_config.side_effect = RuntimeError("read failed")
        self.assertFalse(await self.recover())
        self.assert_blocked("symbol config exception")

    async def test_conflicting_evidence_during_async_reads_blocks(self):
        conflicts = {
            "order_id": lambda o: setattr(o, "order_id", "8389765"),
            "fill": lambda o: setattr(o, "filled_qty", 1.0),
            "avg_price": lambda o: setattr(o, "avg_price", 15.1),
            "position": lambda o: self.engine.positions.update({"LINKUSDT": object()}),
            "state": lambda o: o.transition(OrderState.SUBMITTED, order_id="8389765"),
        }
        for label, conflict in conflicts.items():
            self.order, _ = OrderRegistry().get_or_create(LINK_OID, "LINKUSDT", "Buy", 19.34)
            self.order.created_at = 1790667918
            self.order.transition(OrderState.SUBMITTING)
            self.engine.positions = {}
            fired = []

            def on_read(endpoint, conflict=conflict, fired=fired):
                if endpoint == "/fapi/v1/openAlgoOrders" and not fired:
                    fired.append(1)
                    conflict(self.order)

            self.on_read = on_read
            self.assertFalse(await self.recover(), label)
            self.assertNotEqual(self.order.state, OrderState.REJECTED, label)
            self.assert_no_mutation()
        self.on_read = None

    async def test_rule_is_not_generalizable_to_other_absent_orders(self):
        other, _ = self.engine.orders.get_or_create("bgx7-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "LINKUSDT", "Buy", 19.34)
        other.created_at = 1790667918
        other.transition(OrderState.SUBMITTING)
        self.assertFalse(await hardening._recover_audited_link_rejection(self.engine, other, _Log()))
        self.assertEqual(other.state, OrderState.SUBMITTING)
        self.assertEqual(self.reads, [])  # absence is never even queried for other ids


class ConvergenceProofTests(Harness):
    def _durable(self):
        async def original(engine):
            return False

        async def persist(engine, reason, strict=False):
            engine.persist_reasons.append((reason, self.order.state))
            engine.errors.discard("orders")
            engine._durable_state_ok = not engine.errors
            return True

        def clear(engine, reason):
            engine.errors.discard(reason)
            engine._durable_state_ok = not engine.errors

        def block(engine, reason):
            engine.errors.add(reason)
            engine._durable_state_ok = False

        return SimpleNamespace(reconcile_orders=original, persist_orders=persist,
                               _clear=clear, _block=block, _advance=lambda *a, **k: None)

    def _health(self, clock):
        from bot.private_stream_health import PrivateStreamHealth
        health = PrivateStreamHealth(monotonic=lambda: clock[0])
        health.mark_listen_key_confirmed()
        health.mark_connected("/private")
        return health

    async def test_private_stream_converges_only_after_proven_recovery(self):
        from bot import pilot_live_runtime
        from bot import durable_live_reconciliation as live

        clock = [1000.0]
        health = self._health(clock)
        self.client.private_stream_health = health
        self.client._prelive_account_exposure_verified = True
        # Binance convenience lookup collapses -2013 to {}: never treated as proof.
        self.client.get_order_by_client_oid = AsyncMock(return_value={})
        log = logging.getLogger("test.link")

        ok, reason = await pilot_live_runtime._private_stream_ready(self.engine, log)
        self.assertEqual((ok, reason), (False, "reconcile_required"))
        self.assertEqual(len(self.engine.orders.pending_orders()), 1)
        self.assertEqual(self.order.state, OrderState.SUBMITTING)

        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        self.assertTrue(await durable.reconcile_orders(self.engine))
        self.assertEqual(self.order.state, OrderState.REJECTED)
        # Persisted with the terminal state BEFORE the durable gate cleared.
        self.assertEqual(self.engine.persist_reasons, [("startup_reconcile_hardened", OrderState.REJECTED)])
        self.assertEqual(self.engine.orders.pending_orders(), [])
        self.assertTrue(self.engine._durable_state_ok)

        self.assertTrue(await live.reconcile_pending(self.engine, min_interval_s=0.0))
        ok, reason = await pilot_live_runtime._private_stream_ready(self.engine, log)
        self.assertEqual((ok, reason), (True, "event_capable"))
        self.assertFalse(health.snapshot()["reconcile_required"])
        self.assert_no_mutation()
        self.client.get_order_by_client_oid.assert_awaited()

    async def test_durable_gate_never_opens_before_persistence_completes(self):
        # Empty-pending path after an in-memory terminalization whose earlier
        # persistence failed: the gate must stay closed while the write is in
        # flight and after a failed write.
        self.order.transition(OrderState.REJECTED, source="test")
        durable = self._durable()
        seen = []

        async def persist(engine, reason, strict=False):
            seen.append(engine._durable_state_ok)
            return False  # real persist_orders re-blocks and returns False

        durable.persist_orders = persist
        hardening.install(durable, order_state, _Log())
        self.assertFalse(await durable.reconcile_orders(self.engine))
        self.assertEqual(seen, [False])
        self.assertFalse(self.engine._durable_state_ok)

    async def test_persistence_failure_keeps_entries_blocked(self):
        durable = self._durable()

        async def failing(engine, reason, strict=False):
            engine.errors.add("orders")
            engine._durable_state_ok = False
            return False

        durable.persist_orders = failing
        hardening.install(durable, order_state, _Log())
        self.assertFalse(await durable.reconcile_orders(self.engine))
        self.assertFalse(self.engine._durable_state_ok)
        self.assert_no_mutation()


if __name__ == "__main__":
    unittest.main()
