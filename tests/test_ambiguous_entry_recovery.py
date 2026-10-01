"""F-01 regression: an ambiguous BGX LIVE entry that Binance filled must be
reconciled with strong lineage and protected; never re-dispatched; never
adopted by symbol similarity. Offline only: the Binance transport is faked at
``BinanceClient._request`` and every other layer is the real LIVE runtime.
"""
import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests import test_binance_cross_stress_dispatch_proof as harness  # LIVE bootstrap

from bot import ambiguous_entry_recovery as recovery
from bot import binance_protection_failclosed
from bot import durable_execution as durable
from bot.binance import BinanceClient
from bot.nexus_runtime_engine import TradingEngine as RuntimeTradingEngine
from bot.order_state import ManagedOrder, OrderRegistry, OrderState, normalize_protection_plan

AMBIGUOUS = "Binance POST /fapi/v1/order HTTP 504 code=-1007 msg=Timeout waiting for response"


class AmbiguousEntryRecovery(unittest.IsolatedAsyncioTestCase):
    """Real LIVE runtime engine (MRO-effective subclass) with a fake transport."""

    async def asyncSetUp(self):
        await harness.DispatchProof.asyncSetUp(self)
        # Real clientOid lookup through the faked transport (harness stubs it).
        del self.client.get_order_by_client_oid
        self.client._request = AsyncMock(side_effect=self.binance)
        self.client._order_registry = self.engine.orders
        self.client.private_stream_health = SimpleNamespace(record_event=lambda *a: None)
        self.engine._execution_ownership_valid = True
        self.engine.positions = {}
        self.live_rows = []
        self.lookup_mode = "inconclusive"   # inconclusive | filled | rejected | partial | foreign_oid
        self.fill_qty = None
        self.fill_side = "BUY"
        self.stop_installed = False
        self.stop_install_ok = True
        self.close_ok = False
        self.alerts = []
        self.client.get_positions = AsyncMock(side_effect=self.positions_read)
        self.client.set_position_stops = AsyncMock(side_effect=self.install_stop)
        self.client.wait_for_fill = AsyncMock(return_value={
            "filled": False, "status": {}, "timed_out": True,
        })
        self.replace("bot.notifier.notify", AsyncMock(side_effect=self.capture_alert))
        self.replace(
            "bot.binance_protection_failclosed.conditional_stop_confirmed",
            AsyncMock(side_effect=self.stop_readback),
        )
        self.replace(
            "bot.pilot_external_position_guard.conditional_stop_confirmed",
            AsyncMock(side_effect=self.stop_readback),
        )

    async def capture_alert(self, text):
        self.alerts.append(text)

    async def positions_read(self):
        return [dict(row) for row in self.live_rows]

    async def install_stop(self, symbol, sl=0, tp=0):
        # Mirrors BinanceClient.set_position_stops: no live position -> False.
        self.stop_calls = getattr(self, "stop_calls", []) + [(symbol, sl, tp)]
        if not any(r["symbol"] == symbol for r in self.live_rows):
            return False
        self.stop_installed = bool(self.stop_install_ok)
        return bool(self.stop_install_ok)

    async def stop_readback(self, client, position):
        return (True, "conditional_close_order") if self.stop_installed else (
            False, "no_full_protective_stop"
        )

    def live(self, qty, side="Buy", symbol="ETHUSDT", entry=100.0):
        self.live_rows = [{
            "symbol": symbol, "side": side, "size": qty, "sizeUnit": "BASE_ASSET",
            "entryPrice": entry, "markPrice": entry, "stopLoss": 0,
        }]

    async def binance(self, method, endpoint, params=None, **kwargs):
        params = dict(params or {})
        self.requests.append((method, endpoint, params))
        if method == "POST" and endpoint == "/fapi/v1/order":
            if params.get("reduceOnly") == "true":
                if self.close_ok:
                    self.live_rows = []
                    return {"orderId": "close-1", "clientOrderId": params.get("newClientOrderId")}
                raise RuntimeError("Binance POST /fapi/v1/order HTTP 400 code=-2022 msg=ReduceOnly Order is rejected")
            # Binance accepted and filled; the response never reaches BGX.
            self.oid = params["newClientOrderId"]
            self.requested_qty = float(params["quantity"])
            if self.lookup_mode not in ("rejected",):
                filled = self.fill_qty if self.fill_qty is not None else self.requested_qty
                self.live(filled, side="Buy" if self.fill_side == "BUY" else "Sell")
            raise RuntimeError(AMBIGUOUS)
        if method == "GET" and endpoint == "/fapi/v1/order":
            return self.lookup(params)
        return {}

    def lookup(self, params):
        if self.lookup_mode == "inconclusive":
            raise RuntimeError("Binance GET /fapi/v1/order network failure")
        oid = params.get("origClientOrderId")
        qty = self.requested_qty
        base = {
            "orderId": 777, "clientOrderId": oid, "symbol": "ETHUSDT",
            "side": self.fill_side, "origQty": str(qty), "avgPrice": "100.0",
        }
        if self.lookup_mode == "foreign_oid":
            base["clientOrderId"] = "manual-operator-order"
        if self.lookup_mode == "rejected":
            return {**base, "status": "REJECTED", "executedQty": "0"}
        if self.lookup_mode == "partial":
            return {**base, "status": "EXPIRED", "executedQty": str(self.fill_qty)}
        filled = self.fill_qty if self.fill_qty is not None else qty
        return {**base, "status": "FILLED", "executedQty": str(filled)}

    def opening_posts(self):
        return [r for r in self.requests
                if r[0] == "POST" and r[1] == "/fapi/v1/order"
                and r[2].get("reduceOnly") != "true"]

    def intent(self):
        orders = [o for o in self.engine.orders.all_orders()
                  if o.symbol == "ETHUSDT" and o.exposure_intent == "INCREASE"]
        self.assertEqual(len(orders), 1, orders)
        return orders[0]

    def assert_entries_blocked(self):
        self.assertIn(recovery.BLOCK_REASON, self.engine._durable_state_errors)
        self.assertFalse(durable.can_open(self.engine))

    async def ambiguous_open(self):
        await self.engine._open(self.signal)
        self.assertEqual(len(self.opening_posts()), 1, self.events)

    # ── POST rejected by the exchange ─────────────────────────────────────
    async def test_01_rejected_submit_terminalizes_without_position_or_protection(self):
        self.lookup_mode = "rejected"
        await self.ambiguous_open()
        order = self.intent()
        self.assertEqual(order.state, OrderState.REJECTED)
        self.assertEqual(self.engine.positions, {})
        self.assertFalse(self.stop_installed)          # nothing to protect
        self.assertEqual(self.live_rows, [])
        self.assertEqual(recovery._unresolved_entry_intents(self.engine), [])
        await recovery.recover_unadopted_entries(self.engine)
        self.assertNotIn(recovery.BLOCK_REASON, self.engine._durable_state_errors)
        self.assertEqual(len(self.opening_posts()), 1)

    # ── timeout, immediate lookup proves FILLED (existing recovery path) ──
    async def test_02_timeout_with_lookup_filled_reconciles_and_protects(self):
        self.lookup_mode = "filled"
        self.client.wait_for_fill = AsyncMock(side_effect=lambda oid, **k: {
            "filled": True, "timed_out": False,
            "status": {"filledSize": self.requested_qty, "dealSize": self.requested_qty,
                       "dealValue": self.requested_qty * 100.0},
        })
        await self.ambiguous_open()
        order = self.intent()
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.order_id, "777")
        self.assertIn("ETHUSDT", self.engine.positions)
        self.assertTrue(self.stop_installed)
        self.assertNotIn("ETHUSDT", self.engine._unprotected_symbols)
        self.assertEqual(len(self.opening_posts()), 1)

    # ── timeout, lookup inconclusive: SUBMIT_UNKNOWN, no second dispatch ──
    async def test_03_inconclusive_lookup_keeps_durable_intent_and_never_redispatches(self):
        await self.ambiguous_open()
        order = self.intent()
        self.assertEqual(order.state, OrderState.SUBMITTING)       # UNKNOWN != REJECTED
        self.assertFalse(order.is_terminal)
        self.assertTrue(order.protection_plan and not order.protection_plan["materialized"])
        self.assertEqual(self.engine.positions, {})
        self.assert_entries_blocked()
        self.assertTrue(any("AMBÍGUO" in a for a in self.alerts), self.alerts)
        saved = [c.args[1] for c in harness.core.db.save_key_value.await_args_list
                 if c.args and c.args[0] == durable._ORDER_KEY]
        self.assertTrue(any(order.client_oid in s and "SUBMITTING" in s for s in saved))
        # Another scan cycle cannot dispatch while the intent is unresolved.
        await self.engine._open(self.signal)
        await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(len(self.opening_posts()), 1)
        self.assert_entries_blocked()
        self.assertEqual(sum("AMBÍGUO" in a for a in self.alerts), 1)  # deduplicated

    # ── later reconciliation proves the fill: adopt + protect ─────────────
    async def test_04_later_reconciliation_adopts_with_strong_lineage_and_protects(self):
        await self.ambiguous_open()
        self.lookup_mode = "filled"
        result = await recovery.recover_unadopted_entries(self.engine)
        order = self.intent()
        self.assertEqual(list(result.values()), [recovery.ADOPTED_PROTECTED])
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.order_id, "777")
        self.assertTrue(order.exposure_reconciliation_complete)
        pos = self.engine.positions["ETHUSDT"]
        self.assertEqual(pos.direction, "LONG")
        self.assertAlmostEqual(pos.qty, self.requested_qty)
        self.assertAlmostEqual(pos.sl, self.signal.sl)
        self.assertEqual(self.stop_calls[-1][0], "ETHUSDT")
        self.assertAlmostEqual(self.stop_calls[-1][1], self.signal.sl)
        self.assertTrue(self.stop_installed)
        self.assertNotIn("ETHUSDT", self.engine._unprotected_symbols)
        self.assertNotIn(recovery.BLOCK_REASON, self.engine._durable_state_errors)
        self.assertEqual(len(self.opening_posts()), 1)

    async def test_05_post_open_hook_adopts_when_fill_is_proven_after_submit(self):
        # Inconclusive inside place_order (3 lookups), conclusive on the
        # post-open recovery lookup that immediately follows.
        calls = {"n": 0}
        original = self.lookup

        def lookup(params):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("network failure")
            self.lookup_mode = "filled"
            return original(params)

        self.lookup = lookup
        await self.ambiguous_open()
        self.assertIn("ETHUSDT", self.engine.positions)
        self.assertTrue(self.stop_installed)
        self.assertEqual(self.intent().state, OrderState.FILLED)
        self.assertEqual(len(self.opening_posts()), 1)

    # ── ownership: never adopt by similarity ──────────────────────────────
    async def test_06_coincident_external_position_is_not_adopted(self):
        self.live(0.5)
        self.engine._external_position_symbols = {"ETHUSDT"}
        result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(result, {})
        self.assertEqual(self.engine.positions, {})
        self.assertFalse(getattr(self, "stop_calls", []))

    async def test_07_external_classified_symbol_rejects_even_with_bgx_fill(self):
        await self.ambiguous_open()
        self.engine._external_position_symbols = {"ETHUSDT"}
        self.lookup_mode = "filled"
        result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(list(result.values()), [recovery.LINEAGE_REJECTED])
        self.assertEqual(self.engine.positions, {})
        self.assertFalse(getattr(self, "stop_calls", []))
        self.assert_entries_blocked()

    async def test_08_quantity_mismatch_is_not_adopted(self):
        await self.ambiguous_open()
        self.live(self.requested_qty * 2)     # exchange exposure != BGX fill
        self.lookup_mode = "filled"
        result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(list(result.values()), [recovery.LINEAGE_REJECTED])
        self.assertEqual(self.engine.positions, {})
        self.assertFalse(getattr(self, "stop_calls", []))
        self.assertIn("ETHUSDT", self.engine._unprotected_symbols)
        self.assert_entries_blocked()
        self.assertTrue(any("lineage" in a for a in self.alerts), self.alerts)

    async def test_09_side_mismatch_is_not_adopted(self):
        await self.ambiguous_open()
        self.live(self.requested_qty, side="Sell")
        self.lookup_mode = "filled"
        result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(list(result.values()), [recovery.LINEAGE_REJECTED])
        self.assertEqual(self.engine.positions, {})
        self.assertFalse(getattr(self, "stop_calls", []))

    async def test_10_foreign_client_order_id_is_not_adopted(self):
        await self.ambiguous_open()
        self.lookup_mode = "foreign_oid"
        result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(list(result.values()), [recovery.LINEAGE_REJECTED])
        self.assertEqual(self.engine.positions, {})
        self.assertFalse(getattr(self, "stop_calls", []))
        # A registry entry outside the BGX namespace is never a candidate.
        foreign = ManagedOrder("manual-1", "ETHUSDT", "Buy", 0.5, state=OrderState.FILLED,
                               order_id="9", filled_qty=0.5)
        foreign.protection_plan = normalize_protection_plan(
            {"direction": "LONG", "entry": 100, "sl": 99, "tp": 104})
        self.engine.orders._orders["manual-1"] = foreign
        self.assertNotIn(foreign, recovery._unresolved_entry_intents(self.engine))

    # ── protection failure is fail-closed ─────────────────────────────────
    async def test_11_confirmed_fill_with_stop_failure_blocks_and_alerts(self):
        await self.ambiguous_open()
        self.lookup_mode = "filled"
        self.stop_install_ok = False
        self.close_ok = False
        with self.assertLogs("kakazito-trade", level="CRITICAL") as logs:
            result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(list(result.values()), [recovery.ADOPTED_UNPROTECTED])
        self.assertIn("ETHUSDT", self.engine.positions)          # stays managed by BGX
        self.assertIn("ETHUSDT", self.engine._unprotected_symbols)
        self.assertFalse(self.intent().exposure_reconciliation_complete)
        self.assertFalse(durable.can_open(self.engine))
        self.assertTrue(any("PROTECTION_UNCONFIRMED" in line for line in logs.output))
        self.assertTrue(any("SEM proteção" in a for a in self.alerts), self.alerts)
        self.assertEqual(len(self.opening_posts()), 1)
        # The existing periodic guard keeps retrying protection.
        self.stop_install_ok = True
        await self.engine._guard_naked_positions()
        self.assertTrue(self.stop_installed)
        self.assertNotIn("ETHUSDT", self.engine._unprotected_symbols)

    # ── partial fill: protected exposure == confirmed exposure ────────────
    async def test_12_partial_fill_adopts_exact_confirmed_exposure(self):
        self.fill_qty = 0.25
        await self.ambiguous_open()
        self.lookup_mode = "partial"
        result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(list(result.values()), [recovery.ADOPTED_PROTECTED])
        order = self.intent()
        self.assertEqual(order.state, OrderState.CANCELLED)
        self.assertAlmostEqual(order.filled_qty, 0.25)
        self.assertAlmostEqual(self.engine.positions["ETHUSDT"].qty, 0.25)
        self.assertTrue(self.stop_installed)
        self.assertEqual(len(self.opening_posts()), 1)

    # ── duplicate private-stream events ───────────────────────────────────
    async def test_13_duplicate_ws_fill_events_do_not_duplicate_anything(self):
        await self.ambiguous_open()
        self.lookup_mode = "filled"
        await recovery.recover_unadopted_entries(self.engine)
        order = self.intent()
        stops_before = len(self.stop_calls)
        event = {"e": "ORDER_TRADE_UPDATE", "E": 1, "o": {
            "c": order.client_oid, "i": "777", "s": "ETHUSDT", "S": "BUY",
            "q": str(self.requested_qty), "X": "FILLED", "z": str(self.requested_qty), "ap": "100",
        }}
        for _ in range(3):
            await self.client._handle_private_order_event(event)
            await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(list(self.engine.positions), ["ETHUSDT"])
        self.assertEqual(len(self.engine.orders.all_orders()), 1)
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertAlmostEqual(order.filled_qty, self.requested_qty)
        self.assertEqual(len(self.stop_calls), stops_before)
        self.assertEqual(len(self.opening_posts()), 1)

    # ── restart during SUBMIT_UNKNOWN ─────────────────────────────────────
    async def test_14_restart_during_submit_unknown_reconciles_without_redispatch(self):
        await self.ambiguous_open()
        snapshot = self.engine.orders.snapshot()
        posts_before_restart = len(self.opening_posts())

        # New process: fresh client + engine, durable registry restored.
        self.client2 = BinanceClient()
        self.client2._instruments = self.engine.instruments
        engine2 = RuntimeTradingEngine(self.client2)
        engine2.paper_trade = False
        engine2._durable_state_enforced = True
        engine2._durable_state_ok = True
        engine2._durable_state_errors = set()
        engine2._durable_order_lock = asyncio.Lock()
        engine2.instruments = self.engine.instruments
        engine2._execution_ownership_valid = True
        engine2.orders.restore(snapshot)
        self.client2.rehydrate_order_identity_maps(snapshot)
        self.client2._request = AsyncMock(side_effect=self.binance)
        self.client2.get_positions = AsyncMock(side_effect=self.positions_read)
        self.client2.set_position_stops = AsyncMock(side_effect=self.install_stop)
        self.client2._execution_ownership = SimpleNamespace(expires_at="2099-01-01T00:00:00+00:00")
        self.client2.get_stop_orders = AsyncMock(return_value=[])
        engine2.positions = {}

        # Startup classification: neither EXTERNAL nor loaded heuristically.
        await engine2._load_existing_positions()
        self.assertNotIn("ETHUSDT", engine2._external_position_symbols)
        self.assertIn("ETHUSDT", engine2._ambiguous_recovery_symbols)
        self.assertNotIn("ETHUSDT", engine2.positions)
        # Heuristic scoped reconcile is refused for a pending symbol.
        self.assertIsNone(await engine2._reconcile_exchange_positions(only_symbol="ETHUSDT"))

        # Still unknown after restart: stays pending and blocked.
        result = await recovery.recover_unadopted_entries(engine2)
        self.assertEqual(list(result.values()), [recovery.SUBMIT_UNKNOWN])
        self.assertIn(recovery.BLOCK_REASON, engine2._durable_state_errors)

        # Exchange truth arrives: adopted and protected; zero new dispatch.
        self.lookup_mode = "filled"
        engine2._durable_live_reconcile_last = 0.0
        result = await recovery.recover_unadopted_entries(engine2)
        self.assertEqual(list(result.values()), [recovery.ADOPTED_PROTECTED])
        self.assertIn("ETHUSDT", engine2.positions)
        self.assertTrue(self.stop_installed)
        self.assertEqual(len(self.opening_posts()), posts_before_restart)
        await self.client2.close()

    async def test_15_restart_pending_symbol_becomes_external_when_intent_not_accepted(self):
        self.lookup_mode = "inconclusive"
        await self.ambiguous_open()
        # The position on the exchange is someone else's; BGX order was rejected.
        self.lookup_mode = "rejected"
        self.engine._ambiguous_recovery_symbols = {"ETHUSDT"}
        result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(list(result.values()), [recovery.NOT_ACCEPTED])
        self.assertIn("ETHUSDT", self.engine._external_position_symbols)
        self.assertEqual(self.engine.positions, {})
        self.assertFalse(getattr(self, "stop_calls", []))

    # ── normal entries are never false incidents ──────────────────────────
    async def test_16_materialized_entry_closed_later_is_not_an_incident(self):
        order = ManagedOrder("bgx7-normal", "ETHUSDT", "Buy", 0.5, state=OrderState.FILLED,
                             order_id="55", filled_qty=0.5)
        order.protection_plan = normalize_protection_plan(
            {"direction": "LONG", "entry": 100, "sl": 99, "tp": 104})
        self.engine.orders._orders[order.client_oid] = order
        self.engine.positions = {"ETHUSDT": SimpleNamespace(qty=0.5, direction="LONG")}
        await recovery.recover_unadopted_entries(self.engine)
        self.assertTrue(order.protection_plan["materialized"])
        self.engine.positions = {}            # closed by its exchange stop
        self.live_rows = []
        result = await recovery.recover_unadopted_entries(self.engine)
        self.assertEqual(result, {})
        self.assertEqual(self.alerts, [])
        self.assertNotIn(recovery.BLOCK_REASON, self.engine._durable_state_errors)

    async def test_17_readiness_never_absorbs_entry_fill_without_local_position(self):
        await self.ambiguous_open()
        self.lookup_mode = "filled"
        order = self.intent()
        from bot.durable_live_reconciliation import apply_exchange_order_truth
        apply_exchange_order_truth(self.engine, order, await self.client.get_order_by_client_oid(order.client_oid))
        self.assertEqual(order.state, OrderState.FILLED)
        self.engine.connected = True
        from bot.protection_readiness import refresh_protection_readiness
        await refresh_protection_readiness(self.engine)
        self.assertFalse(order.exposure_reconciliation_complete)
        self.assertIn(order, recovery._unresolved_entry_intents(self.engine))


for _name in ("asyncTearDown", "replace", "account", "brackets", "fence",
              "ownership", "request", "evaluate"):
    setattr(AmbiguousEntryRecovery, _name, getattr(harness.DispatchProof, _name))


class DurablePlanRecord(unittest.TestCase):
    def test_plan_round_trip_and_backward_compatibility(self):
        order = ManagedOrder("bgx7-x", "ETHUSDT", "Buy", 1.0)
        order.protection_plan = normalize_protection_plan(
            {"direction": "LONG", "entry": 100, "sl": 99, "tp": 104})
        restored = ManagedOrder.from_record(order.to_record())
        self.assertEqual(restored.protection_plan, order.protection_plan)
        legacy = order.to_record()
        legacy.pop("protection_plan")
        self.assertIsNone(ManagedOrder.from_record(legacy).protection_plan)

    def test_invalid_plans_are_rejected(self):
        for plan in (
            {"direction": "LONG", "entry": 100, "sl": 101, "tp": 104},
            {"direction": "SHORT", "entry": 100, "sl": 99, "tp": 90},
            {"direction": "UP", "entry": 100, "sl": 99, "tp": 104},
            {"direction": "LONG", "entry": float("nan"), "sl": 99, "tp": 104},
            None,
        ):
            self.assertIsNone(normalize_protection_plan(plan))

    def test_registry_restore_keeps_plan(self):
        order = ManagedOrder("bgx7-y", "ETHUSDT", "Sell", 1.0)
        order.protection_plan = normalize_protection_plan(
            {"direction": "SHORT", "entry": 100, "sl": 101, "tp": 96})
        registry = OrderRegistry()
        registry.restore([order.to_record()])
        self.assertEqual(registry.get("bgx7-y").protection_plan["sl"], 101.0)


class EffectiveRuntimeCallables(unittest.TestCase):
    """The fix must protect the callables the LIVE subclass actually runs."""

    def test_live_subclass_resolves_to_protected_callables(self):
        cls = RuntimeTradingEngine
        open_chain = [name for _, name in harness.chain(cls._open)]
        self.assertIn("_open_with_protection", open_chain)
        self.assertIn(
            "_load_existing_failclosed",
            [name for _, name in harness.chain(cls._load_existing_positions)],
        )
        self.assertEqual(
            cls._reconcile_exchange_positions.__code__.co_name, "_reconcile_failclosed"
        )
        enforce = cls._bgx_enforce_owned_protection
        self.assertEqual(enforce.__code__.co_name, "_enforce")
        self.assertEqual(
            enforce.__code__.co_filename, binance_protection_failclosed.__file__
        )
        self.assertNotIn("run", vars(cls))   # subclass inherits the hooked loop
        sources = []
        fn, seen = cls.run, set()
        while callable(fn) and id(fn) not in seen:
            seen.add(id(fn))
            sources.append(inspect.getsource(fn))
            nonlocals = inspect.getclosurevars(fn).nonlocals
            nxt = [v for k, v in nonlocals.items()
                   if k.startswith(("previous", "original")) and inspect.isfunction(v)]
            fn = nxt[0] if nxt else None
        self.assertIn("recover_unadopted_entries", sources[-1])
        self.assertIn("_guard_naked_positions", sources[-1])
        for name in ("_open", "run", "_guard_naked_positions", "_load_existing_positions"):
            self.assertNotIn(name, vars(cls), name)


if __name__ == "__main__":
    unittest.main()
