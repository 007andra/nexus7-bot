"""F-010B — a failed order-registry restore never authorizes overwriting the
durable snapshot it failed to load (INV-DURABLE-RESTORE-001,
INV-DURABLE-EVIDENCE-001). READ FAILURE != EMPTY STATE.

Offline: the key/value store is an in-memory dict; exchange lookups are fakes.
"""
import asyncio
import json
import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("PAPER_TRADE", "true")

from bot import durable_execution as durable  # noqa: E402
from bot import durable_live_reconciliation as reconciler  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402

KEY = durable._ORDER_KEY
RESTORE = durable.ORDERS_RESTORE


class _Store:
    """In-memory durable KV store with injectable read failures."""

    def __init__(self):
        self.data = {}
        self.read_error = None
        self.writes = []

    async def save(self, key, value, strict=False):
        self.writes.append(key)
        self.data[key] = value
        return True

    async def load(self, key, strict=False):
        if self.read_error is not None:
            raise self.read_error
        return self.data.get(key)

    def ids(self):
        return sorted(r["client_oid"] for r in json.loads(self.data[KEY])["orders"])

    def patches(self):
        return (patch.object(durable.db, "save_key_value", AsyncMock(side_effect=self.save)),
                patch.object(durable.db, "load_key_value", AsyncMock(side_effect=self.load)),
                patch.object(durable.db, "configured_postgres_unavailable", Mock(return_value=False)))


def _engine():
    return SimpleNamespace(orders=OrderRegistry(), paper_trade=False, connected=True,
                           _execution_ownership_valid=True)


async def _seed(store, oids):
    seed = _engine()
    seed._durable_state_errors, seed._durable_state_ok = set(), True
    seed._durable_order_lock = asyncio.Lock()
    for oid in oids:
        order, _ = seed.orders.get_or_create(oid, "BTCUSDT", "Buy", 2.0)
        order.transition(OrderState.SUBMITTING, source="LOCAL")
    with patch.object(durable.db, "save_key_value", AsyncMock(side_effect=store.save)):
        assert await durable.persist_orders(seed, "seed")
    return store.data[KEY]


class DurableRestoreEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def _boot(self, store):
        engine = _engine()
        p1, p2, p3 = store.patches()
        with p1, p2, p3:
            await durable.restore_engine_state(engine)
        return engine

    async def _persist(self, store, engine, reason="private_ws_transition"):
        with patch.object(durable.db, "save_key_value", AsyncMock(side_effect=store.save)):
            return await durable.persist_orders(engine, reason)

    async def test_a_failed_restore_empty_registry_cannot_overwrite(self):
        store = _Store()
        original = await _seed(store, ["bgx7-A", "bgx7-B"])
        store.read_error = OSError("storage read timeout")
        engine = await self._boot(store)
        self.assertEqual(engine._durable_order_restore_state, "RESTORE_FAILED")
        self.assertEqual((len(engine.orders), durable.active_reasons(engine)), (0, (RESTORE,)))
        self.assertFalse(await self._persist(store, engine), "refusal is reported, not hidden")
        self.assertEqual(store.data[KEY], original)
        self.assertEqual(durable.active_reasons(engine), (RESTORE,), "refusal never clears it")

    async def test_b_failed_restore_partial_registry_cannot_overwrite(self):
        store = _Store()
        original = await _seed(store, ["bgx7-A", "bgx7-B", "bgx7-C"])
        store.read_error = OSError("down")
        engine = await self._boot(store)
        reduce, _ = engine.orders.get_or_create("bgx7-reduce", "BTCUSDT", "Sell", 1.0)
        reduce.transition(OrderState.SUBMITTING, source="LOCAL")
        self.assertFalse(await self._persist(store, engine, "before_dispatch"))
        self.assertEqual(store.ids(), ["bgx7-A", "bgx7-B", "bgx7-C"])
        self.assertEqual(store.data[KEY], original)

    async def test_c_valid_empty_restore_allows_empty_persist(self):
        store = _Store()                      # nothing stored: VALID_EMPTY, not a failure
        engine = await self._boot(store)
        self.assertEqual(engine._durable_order_restore_state, "VALID_EMPTY")
        self.assertEqual(durable.active_reasons(engine), ())
        self.assertTrue(await self._persist(store, engine))
        self.assertEqual(store.ids(), [])

    async def test_c_valid_stored_empty_list_is_valid(self):
        store = _Store()
        await _seed(store, [])
        engine = await self._boot(store)
        self.assertEqual(engine._durable_order_restore_state, "VALID")
        self.assertTrue(await self._persist(store, engine))

    async def test_d_successful_restore_allows_persist(self):
        store = _Store()
        await _seed(store, ["bgx7-A", "bgx7-B"])
        engine = await self._boot(store)
        self.assertEqual(engine._durable_order_restore_state, "VALID")
        self.assertEqual(len(engine.orders), 2)
        self.assertTrue(await self._persist(store, engine))
        self.assertEqual(store.ids(), ["bgx7-A", "bgx7-B"])

    async def test_e_failed_then_successful_restore_reenables_persistence(self):
        store = _Store()
        await _seed(store, ["bgx7-A", "bgx7-B"])
        store.read_error = TimeoutError()
        engine = await self._boot(store)
        self.assertFalse(await self._persist(store, engine))
        store.read_error = None
        p1, p2, p3 = store.patches()
        with p1, p2, p3:
            await durable.restore_engine_state(engine)         # retry succeeds
        self.assertEqual(durable.active_reasons(engine), ())
        self.assertEqual(len(engine.orders), 2)
        self.assertTrue(await self._persist(store, engine))
        self.assertEqual(store.ids(), ["bgx7-A", "bgx7-B"])

    async def test_f_unrelated_domains_still_persist(self):
        store = _Store()
        await _seed(store, ["bgx7-A"])
        store.read_error = OSError("orders key unreadable")
        engine = await self._boot(store)
        engine.paper_trade = True
        engine._durable_paper_lock = asyncio.Lock()
        with patch.object(durable, "build_paper_runtime_payload", Mock(return_value='{"v":1}')), \
                patch.object(durable.db, "save_key_value", AsyncMock(side_effect=store.save)):
            self.assertTrue(await durable.persist_paper_runtime(engine, "x"))
        self.assertEqual(store.data[durable.PAPER_STATE_KEY], '{"v":1}')
        self.assertEqual(durable.active_reasons(engine), (RESTORE,))

    async def test_g_ws_event_cannot_turn_partial_registry_into_snapshot(self):
        store = _Store()
        original = await _seed(store, ["bgx7-A", "bgx7-B"])
        store.read_error = OSError("down")
        engine = await self._boot(store)
        order, _ = engine.orders.get_or_create("bgx7-reduce", "BTCUSDT", "Sell", 1.0)
        order.transition(OrderState.SUBMITTING, source="LOCAL")
        order.transition(OrderState.SUBMITTED, order_id="kc-r", source="WS")   # in-memory OK
        with patch.object(durable.db, "save_key_value", AsyncMock(side_effect=store.save)):
            await engine.orders.persist_callback(order)    # real registry callback
        self.assertEqual(store.data[KEY], original)
        self.assertEqual(order.state, OrderState.SUBMITTED)

    async def test_h_reconciliation_during_failed_restore_preserves_snapshot(self):
        store = _Store()
        original = await _seed(store, ["bgx7-A", "bgx7-B"])
        store.read_error = OSError("down")
        engine = await self._boot(store)
        engine.client = SimpleNamespace(get_order_by_client_oid=AsyncMock(return_value={}))
        with patch.object(durable.db, "save_key_value", AsyncMock(side_effect=store.save)):
            await durable.reconcile_orders(engine)                   # empty registry
            order, _ = engine.orders.get_or_create("bgx7-late", "BTCUSDT", "Sell", 1.0)
            order.transition(OrderState.SUBMITTING, source="LOCAL")
            self.assertFalse(await durable.reconcile_orders(engine))
            engine.client = SimpleNamespace(get_order_by_client_oid=AsyncMock(return_value={
                "clientOid": "bgx7-late", "symbol": "XBTUSDTM", "isActive": False,
                "cancelExist": True, "status": "done", "filledSize": "0", "orderId": "kc-l"}))
            with patch.object(reconciler, "_owner_valid", AsyncMock(return_value=True)):
                self.assertFalse(await reconciler.reconcile_pending(engine, min_interval_s=0))
        self.assertEqual(store.data[KEY], original)
        self.assertIn(RESTORE, durable.active_reasons(engine),
                      "empty/terminal registry is not a reconstructed state")
        self.assertFalse(durable.can_open(engine))

    async def test_i_j_crash_then_second_restart_recovers_original(self):
        store = _Store()
        original = await _seed(store, ["bgx7-A", "bgx7-B"])
        store.read_error = OSError("boot1 read failure")
        boot1 = await self._boot(store)
        await self._persist(store, boot1, "private_ws_transition")
        await self._persist(store, boot1, "before_dispatch")
        del boot1                                   # process crash: memory lost
        self.assertEqual(store.data[KEY], original)                     # I
        store.read_error = None
        boot2 = await self._boot(store)                                  # J
        self.assertEqual(boot2._durable_order_restore_state, "VALID")
        self.assertEqual(sorted(o.client_oid for o in boot2.orders.pending_orders()),
                         ["bgx7-A", "bgx7-B"])
        self.assertEqual(durable.active_reasons(boot2), ())

    async def test_l_restore_reason_survives_other_successes(self):
        store = _Store()
        await _seed(store, ["bgx7-A"])
        store.read_error = OSError("down")
        engine = await self._boot(store)
        durable._clear(engine, durable.ORDERS_PERSISTENCE)
        durable._clear(engine, durable.ORDERS_UNRESOLVED)
        durable._clear(engine, "paper")
        await self._persist(store, engine)
        self.assertEqual(durable.active_reasons(engine), (RESTORE,))
        self.assertFalse(durable.can_open(engine))

    async def test_m_malformed_snapshots_are_preserved(self):
        bad_values = [
            "{not json",
            json.dumps({"version": 2, "orders": []}),
            json.dumps({"version": 1, "orders": "nope"}),
            json.dumps({"version": 1, "orders": [{"client_oid": "broken"}]}),
            json.dumps([1, 2, 3]),
        ]
        for bad in bad_values:
            store = _Store()
            store.data[KEY] = bad
            engine = await self._boot(store)
            self.assertEqual(engine._durable_order_restore_state, "MALFORMED", bad)
            order, _ = engine.orders.get_or_create("bgx7-x", "BTCUSDT", "Buy", 1.0)
            self.assertFalse(await self._persist(store, engine), bad)
            self.assertEqual(store.data[KEY], bad, "no destructive overwrite")

    async def test_property_unconfirmed_restore_keeps_snapshot_identical(self):
        rng = random.Random(10_10)
        for trial in range(60):
            store = _Store()
            oids = [f"bgx7-{trial}-{i}" for i in range(rng.randint(1, 5))]
            original = await _seed(store, oids)
            store.read_error = rng.choice([OSError("x"), TimeoutError(), ValueError("y")])
            engine = await self._boot(store)
            engine.client = SimpleNamespace(get_order_by_client_oid=AsyncMock(return_value={}))
            with patch.object(durable.db, "save_key_value", AsyncMock(side_effect=store.save)), \
                    patch.object(reconciler, "_owner_valid", AsyncMock(return_value=True)):
                for step in range(rng.randint(1, 8)):
                    action = rng.choice(("persist", "reconcile", "pending", "ws", "new"))
                    if action == "persist":
                        await durable.persist_orders(engine, "random")
                    elif action == "reconcile":
                        await durable.reconcile_orders(engine)
                    elif action == "pending":
                        await reconciler.reconcile_pending(engine, min_interval_s=0)
                    elif action == "new":
                        o, _ = engine.orders.get_or_create(f"n-{step}", "BTCUSDT", "Sell", 1.0)
                        if o.state == OrderState.CREATED:
                            o.transition(OrderState.SUBMITTING, source="LOCAL")
                    else:
                        for o in list(engine.orders.pending_orders()):
                            await engine.orders.persist_callback(o)
                    self.assertEqual(store.data[KEY], original, (trial, step, action))
                    self.assertIn(RESTORE, durable.active_reasons(engine))


if __name__ == "__main__":
    unittest.main()
