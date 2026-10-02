"""F-010 — causal durable integrity blocks (INV-INTEGRITY-BLOCK-001..003).

A successful persistence proves persistence health only. It must never retire
the unresolved-order, restore or protection reasons. Offline: database writes
and exchange lookups are in-memory fakes.
"""
import asyncio
import itertools
import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("PAPER_TRADE", "true")

from bot import durable_execution as durable  # noqa: E402
from bot import durable_live_reconciliation as reconciler  # noqa: E402
from bot import runtime_readiness  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402

UNRESOLVED = durable.ORDERS_UNRESOLVED
PERSIST = durable.ORDERS_PERSISTENCE


def _engine(**extra):
    state = dict(orders=OrderRegistry(), _durable_state_errors=set(), _durable_state_ok=True,
                 _durable_order_lock=asyncio.Lock(), paper_trade=False, connected=True,
                 _execution_ownership_valid=True)
    state.update(extra)
    return SimpleNamespace(**state)


def _ambiguous(engine, oid="bgx7-ambiguous"):
    order, _ = engine.orders.get_or_create(oid, "BTCUSDT", "Buy", 2.0)
    order.transition(OrderState.SUBMITTING, source="LOCAL")
    durable._block(engine, UNRESOLVED, source="ambiguous_dispatch")   # engine.py path
    return order


def _save_ok():
    return patch.object(durable.db, "save_key_value", AsyncMock(return_value=True))


def _save_fail():
    return patch.object(durable.db, "save_key_value", AsyncMock(side_effect=OSError("db down")))


def _cancelled_truth(order):
    return {"clientOid": order.client_oid, "symbol": "XBTUSDTM", "orderId": "kc-1",
            "isActive": False, "cancelExist": True, "status": "done", "filledSize": "0",
            "size": "2"}


async def _reconcile(engine, truth):
    engine.client = SimpleNamespace(get_order_by_client_oid=AsyncMock(return_value=truth))
    engine._durable_live_reconcile_last = 0.0
    with patch.object(reconciler, "_owner_valid", AsyncMock(return_value=True)), _save_ok():
        return await reconciler.reconcile_pending(engine, min_interval_s=0)


def _blockers(engine):
    try:
        runtime_readiness.assert_ready_for_new_entries(engine)
        return []
    except runtime_readiness.EntryReadinessRefused as exc:
        return list(exc.blockers)


class CausalIntegrityBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_ambiguity_only_blocks(self):
        engine = _engine()
        _ambiguous(engine)
        self.assertFalse(durable.can_open(engine))
        self.assertEqual(durable.active_reasons(engine), (UNRESOLVED,))

    async def test_b_persistence_only_blocks_then_success_unblocks(self):
        engine = _engine()
        with _save_fail():
            self.assertFalse(await durable.persist_orders(engine, "x"))
        self.assertEqual(durable.active_reasons(engine), (PERSIST,))
        self.assertFalse(durable.can_open(engine))
        with _save_ok():
            self.assertTrue(await durable.persist_orders(engine, "x"))
        self.assertEqual(durable.active_reasons(engine), ())
        self.assertTrue(durable.can_open(engine))

    async def test_c_d_ambiguity_survives_persistence_recovery_until_resolved(self):
        engine = _engine()
        order = _ambiguous(engine)
        with _save_fail():
            await durable.persist_orders(engine, "x")
        self.assertEqual(durable.active_reasons(engine), (PERSIST, UNRESOLVED))
        with _save_ok():
            await durable.persist_orders(engine, "x")
        self.assertEqual(durable.active_reasons(engine), (UNRESOLVED,))     # C
        self.assertFalse(durable.can_open(engine))
        self.assertTrue(await _reconcile(engine, _cancelled_truth(order)))   # D
        self.assertEqual(order.state, OrderState.CANCELLED)
        self.assertEqual(durable.active_reasons(engine), ())
        self.assertTrue(durable.can_open(engine))

    async def test_d_unknown_exchange_truth_keeps_block(self):
        engine = _engine()
        _ambiguous(engine)
        self.assertFalse(await _reconcile(engine, {}))                       # lookup inconclusive
        engine.client = SimpleNamespace(get_order_by_client_oid=AsyncMock(side_effect=OSError()))
        engine._durable_live_reconcile_last = 0.0
        with patch.object(reconciler, "_owner_valid", AsyncMock(return_value=True)):
            self.assertFalse(await reconciler.reconcile_pending(engine, min_interval_s=0))
        with patch.object(reconciler, "_owner_valid", AsyncMock(return_value=False)):
            engine._durable_live_reconcile_last = 0.0
            self.assertFalse(await reconciler.reconcile_pending(engine, min_interval_s=0))
        self.assertIn(UNRESOLVED, durable.active_reasons(engine), "unknown => keep blocked")

    def test_e_f_duplicate_block_and_clear_are_idempotent(self):
        engine = _engine()
        for _ in range(10):
            durable._block(engine, UNRESOLVED)
        self.assertEqual(durable.active_reasons(engine), (UNRESOLVED,))
        for _ in range(10):
            durable._clear(engine, UNRESOLVED)
        self.assertEqual(durable.active_reasons(engine), ())
        self.assertTrue(engine._durable_state_ok)

    def test_g_unknown_clear_never_unblocks(self):
        engine = _engine()
        durable._block(engine, UNRESOLVED)
        for reason in ("does_not_exist", "", PERSIST, "orders_", "ORDERS_UNRESOLVED"):
            durable._clear(engine, reason)
        self.assertEqual(durable.active_reasons(engine), (UNRESOLVED,))
        self.assertFalse(engine._durable_state_ok)

    async def test_h_successful_persist_cannot_clear_ambiguity(self):
        """Main F-010 regression (private-WS transition of another order)."""
        engine = _engine()
        _ambiguous(engine)
        other, _ = engine.orders.get_or_create("bgx7-other", "ETHUSDT", "Sell", 1.0)
        other.transition(OrderState.SUBMITTING, source="LOCAL")
        other.transition(OrderState.SUBMITTED, order_id="kc-other", source="WS")
        with _save_ok():
            for reason in ("private_ws_transition", "durable_partial_exit", "before_dispatch"):
                self.assertTrue(await durable.persist_orders(engine, reason))
                self.assertFalse(durable.can_open(engine), reason)
        self.assertEqual(durable.active_reasons(engine), (UNRESOLVED,))

    async def test_i_concurrent_detect_persist_entry_all_interleavings(self):
        steps = ("detect", "persist", "entry")
        for perm in itertools.permutations(steps):
            engine = _engine()
            order, _ = engine.orders.get_or_create("bgx7-amb", "BTCUSDT", "Buy", 2.0)
            order.transition(OrderState.SUBMITTING, source="LOCAL")
            turn = {s: asyncio.Event() for s in steps}
            done = {s: asyncio.Event() for s in steps}
            seen = {}
            gate = asyncio.Event()

            async def slow_save(*_a, **_k):
                await gate.wait()          # persist straddles the other steps
                return True

            async def detect():
                await turn["detect"].wait()
                durable._block(engine, UNRESOLVED, source="ambiguous_dispatch")
                done["detect"].set()

            async def persist():
                task = asyncio.ensure_future(durable.persist_orders(engine, "private_ws_transition"))
                await turn["persist"].wait()
                gate.set()
                await task
                done["persist"].set()

            async def entry():
                await turn["entry"].wait()
                seen["can_open"] = durable.can_open(engine)
                done["entry"].set()

            with patch.object(durable.db, "save_key_value", AsyncMock(side_effect=slow_save)):
                tasks = [asyncio.ensure_future(f()) for f in (detect, persist, entry)]
                await asyncio.sleep(0)
                for step in perm:
                    turn[step].set()
                    await done[step].wait()
                await asyncio.gather(*tasks)
            detected_first = perm.index("detect") < perm.index("entry")
            self.assertEqual(seen["can_open"], not detected_first, perm)
            self.assertFalse(durable.can_open(engine), f"{perm}: ambiguity still active")

    async def test_j_concurrent_reconcile_and_persist_no_fail_open_window(self):
        engine = _engine()
        order = _ambiguous(engine)
        lookup_gate, in_lookup = asyncio.Event(), asyncio.Event()

        async def lookup(_oid):
            in_lookup.set()
            await lookup_gate.wait()
            return {"clientOid": order.client_oid, "symbol": "XBTUSDTM", "isActive": True,
                    "filledSize": "0", "size": "2", "orderId": "kc-1"}
        engine.client = SimpleNamespace(get_order_by_client_oid=lookup)
        engine._durable_live_reconcile_last = 0.0
        fake_log = Mock()
        with patch.object(reconciler, "_owner_valid", AsyncMock(return_value=True)), \
                _save_ok(), patch.object(durable, "log", fake_log):
            task = asyncio.ensure_future(reconciler.reconcile_pending(engine, min_interval_s=0))
            await in_lookup.wait()
            self.assertTrue(await durable.persist_orders(engine, "private_ws_transition"))
            self.assertFalse(durable.can_open(engine), "mid-reconcile persist")
            lookup_gate.set()
            self.assertFalse(await task)
        self.assertFalse(durable.can_open(engine))
        cleared = [c for c in fake_log.warning.call_args_list
                   if c.args and "INTEGRITY_BLOCK_CLEARED" in c.args[0] and UNRESOLVED in c.args]
        added = [c for c in fake_log.warning.call_args_list
                 if c.args and "INTEGRITY_BLOCK_ADDED" in c.args[0]]
        self.assertEqual((cleared, added), ([], []), "no BLOCK->CLEAR->BLOCK flapping")

    async def test_k_readiness_names_causal_reasons(self):
        engine = _engine(instruments={"BTCUSDT": {}}, _financial_state_sane=True,
                         _initial_reconciliation_complete=True, _market_data_ready=True,
                         _protection_system_ready=True)
        order = _ambiguous(engine)
        self.assertIn("critical_database_ready", _blockers(engine))
        self.assertIn("durable:" + UNRESOLVED, _blockers(engine))
        with _save_ok():
            await durable.persist_orders(engine, "private_ws_transition")
        self.assertIn("durable:" + UNRESOLVED, _blockers(engine), "persist is not resolution")
        self.assertFalse(runtime_readiness.runtime_readiness(engine).critical_database_ready)
        await _reconcile(engine, _cancelled_truth(order))
        self.assertTrue(runtime_readiness.runtime_readiness(engine).critical_database_ready)
        self.assertFalse([b for b in _blockers(engine)
                          if b.startswith("durable:") or b == "critical_database_ready"])

    async def test_m_restart_rebuilds_unresolved_block_before_entries(self):
        first = _engine()
        order, _ = first.orders.get_or_create("bgx7-restart", "BTCUSDT", "Buy", 2.0)
        order.transition(OrderState.SUBMITTING, source="LOCAL")
        stored = {}

        async def save(key, value, strict=False):
            stored[key] = value
            return True
        with patch.object(durable.db, "save_key_value", AsyncMock(side_effect=save)):
            await durable.persist_orders(first, "before_dispatch")

        async def load(key, strict=False):
            return stored.get(key)
        second = _engine(orders=OrderRegistry())
        second.client = SimpleNamespace(get_order_by_client_oid=AsyncMock(return_value={}))
        with patch.object(durable.db, "load_key_value", AsyncMock(side_effect=load)), \
                patch.object(durable.db, "configured_postgres_unavailable", Mock(return_value=False)), \
                patch.object(durable.db, "save_key_value", AsyncMock(side_effect=save)):
            await durable.restore_engine_state(second)
            self.assertFalse(await durable.reconcile_orders(second))
            self.assertEqual(durable.active_reasons(second), (UNRESOLVED,))
            await durable.persist_orders(second, "private_ws_transition")
        self.assertFalse(durable.can_open(second), "restart + persist still blocked")

    async def test_m_failed_restore_not_cleared_by_reconcile_or_persist(self):
        engine = _engine(orders=OrderRegistry())
        with patch.object(durable.db, "load_key_value", AsyncMock(side_effect=OSError("down"))), \
                patch.object(durable.db, "configured_postgres_unavailable", Mock(return_value=False)), \
                _save_ok():
            await durable.restore_engine_state(engine)
            await durable.reconcile_orders(engine)
            await durable.persist_orders(engine, "x")
        self.assertEqual(durable.active_reasons(engine), (durable.ORDERS_RESTORE,))
        self.assertFalse(durable.can_open(engine))

    async def test_protection_reason_survives_persist_and_order_reconcile(self):
        engine = _engine()
        durable._block(engine, durable.PROTECTION_UNCONFIRMED, source="protection_postcondition")
        with _save_ok():
            await durable.persist_orders(engine, "x")
        await _reconcile(engine, {})                  # no pending orders
        self.assertEqual(durable.active_reasons(engine), (durable.PROTECTION_UNCONFIRMED,))
        from bot.protection_readiness import refresh_protection_readiness
        engine.client = SimpleNamespace(get_positions=AsyncMock(return_value=[]),
                                        _get=AsyncMock(side_effect=TimeoutError("read failed")))
        engine._unprotected_symbols = {"SOLUSDT"}
        engine._durable_live_reconcile_last = 0.0
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(durable.active_reasons(engine), (durable.PROTECTION_UNCONFIRMED,),
                         "unconfirmed readback keeps the protection reason")
        engine.client._get = AsyncMock(return_value={"items": []})
        self.assertTrue(await refresh_protection_readiness(engine))   # flat readback proof
        self.assertTrue(durable.can_open(engine))

    def test_property_blocked_iff_reasons_and_clear_is_local(self):
        rng = random.Random(1010)
        names = [PERSIST, UNRESOLVED, durable.ORDERS_RESTORE, durable.PROTECTION_UNCONFIRMED,
                 "database", "paper", "trade", "unknown"]
        for _ in range(500):
            engine = _engine()
            model = set()
            for _ in range(rng.randint(1, 12)):
                reason = rng.choice(names)
                if rng.random() < 0.5:
                    durable._block(engine, reason)
                    model.add(reason)
                else:
                    others = set(durable.active_reasons(engine)) - {reason}
                    durable._clear(engine, reason)
                    model.discard(reason)
                    self.assertTrue(others <= set(durable.active_reasons(engine)))
                self.assertEqual(set(durable.active_reasons(engine)), model)
                self.assertEqual(engine._durable_state_ok, not model)
                self.assertEqual(durable.can_open(engine), not model)


if __name__ == "__main__":
    unittest.main()
