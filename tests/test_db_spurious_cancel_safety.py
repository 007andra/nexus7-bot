"""Regression: driver-level CancelledError must not act as task cancellation.

Production 2026-10-11 (01:34, 18:16, 20:52 UTC): two diagnostic
``asyncio.wait_for`` timeouts on the single shared asyncpg connection left its
``cancel_waiter`` cancelled; the next query raised CancelledError although no
one cancelled the engine task (cancel_pending=0), the main loop treated it as
shutdown and returned, and the ownership heartbeat was cancelled with it. At
16:57 the same leak killed only the heartbeat.

These tests reproduce that shape without a database: an awaited future that
someone else already cancelled. Real cancellation must keep propagating.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bot import database as db
from bot import execution_ownership as eo
from bot.db_cancel_safety import (
    DatabaseOperationAborted as DriverAborted,
    driver_call,
    is_spurious_cancel,
)
from bot.execution_capability import ExecutionCapability


def _poisoned_future():
    """Same object asyncpg leaves behind: a shared future already cancelled."""
    fut = asyncio.get_running_loop().create_future()
    fut.cancel()
    return fut


class _PoisonedConn:
    """Minimal stand-in for an asyncpg connection whose cancel_waiter is cancelled."""

    def __init__(self):
        self.calls = 0

    async def _poisoned(self, *args, **kwargs):
        self.calls += 1
        await _poisoned_future()

    fetch = fetchrow = execute = _poisoned


class _SlowConn:
    """Connection whose query never returns, so only a real cancel ends it."""

    async def fetch(self, *args, **kwargs):
        await asyncio.sleep(3600)

    fetchrow = execute = fetch


class DriverCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_spurious_cancel_becomes_ordinary_exception(self):
        with self.assertRaises(DriverAborted):
            await driver_call(_poisoned_future())
        self.assertTrue(issubclass(DriverAborted, Exception))

    async def test_real_task_cancel_still_propagates(self):
        started = asyncio.Event()

        async def victim():
            started.set()
            await driver_call(asyncio.sleep(3600))

        task = asyncio.create_task(victim())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())

    async def test_wait_for_timeout_is_still_timeout(self):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(driver_call(asyncio.sleep(3600)), timeout=0.05)

    async def test_classifier_sees_no_cancel_request_on_plain_task(self):
        self.assertTrue(is_spurious_cancel())


class _DbPatched(unittest.IsolatedAsyncioTestCase):
    conn_factory = _PoisonedConn

    def setUp(self):
        self.conn = self.conn_factory()
        self._saved = (db._conn, db._is_pg)
        db._conn, db._is_pg = self.conn, True

    def tearDown(self):
        db._conn, db._is_pg = self._saved


class DatabaseSpuriousCancelTests(_DbPatched):
    async def test_fetchall_raises_fail_closed_error_not_cancelled(self):
        with self.assertRaises(db.DatabaseOperationAborted):
            await db._fetchall("SELECT 1")

    async def test_fetchone_raises_fail_closed_error_not_cancelled(self):
        with self.assertRaises(db.DatabaseOperationAborted):
            await db._fetchone("SELECT 1")

    async def test_exec_raises_fail_closed_error_not_cancelled(self):
        with self.assertRaises(db.DatabaseOperationAborted):
            await db._exec("SELECT 1")

    async def test_serialized_io_keeps_persistence_error_contract(self):
        # Post-fill durable writes catch PersistenceError to block fail-closed.
        with self.assertRaises(db.PersistenceError):
            await db.save_key_value("k", "v", strict=True)

    async def test_lock_is_released_after_spurious_cancel(self):
        with self.assertRaises(db.DatabaseOperationAborted):
            await db._fetchall("SELECT 1")
        self.assertFalse(db._io_lock.locked())

    async def test_diagnostic_wait_for_reports_error_instead_of_cancel(self):
        """The exact production chain: wait_for(snapshot) on a poisoned connection."""
        from bot import min_order_counterfactual_nexus_v1 as study
        with patch.dict(os.environ, {"RISK_EPOCH_SHADOW_ID": "T"}):
            with self.assertRaises(Exception) as ctx:
                await asyncio.wait_for(study.snapshot(db), timeout=1.5)
        self.assertIsInstance(ctx.exception, db.DatabaseOperationAborted)

    async def test_hard_gate_emitter_survives_poisoned_connection(self):
        from bot import hard_gate_shadow_scan as scan
        with patch.dict(os.environ, {
            "MIN_ORDER_COUNTERFACTUAL_NEXUS_V1": "true",
            "RISK_EPOCH_SHADOW_ID": "T",
        }), patch.object(scan, "_MIN_ORDER_CF_NEXUS_LAST_EMIT", 0.0):
            self.assertIsNone(await scan._maybe_emit_min_order_counterfactual_nexus(db))


class DatabaseRealCancelTests(_DbPatched):
    conn_factory = _SlowConn

    async def test_real_cancel_of_fetchall_propagates(self):
        task = asyncio.create_task(db._fetchall("SELECT 1"))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(db._io_lock.locked())

    async def test_wait_for_timeout_on_fetchall_is_timeout(self):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(db._fetchall("SELECT 1"), timeout=0.05)


class _Engine(SimpleNamespace):
    pass


def _engine():
    return _Engine(
        _running=True,
        _execution_ownership_valid=True,
        _execution_ownership_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        client=SimpleNamespace(),
    )


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(
            eo, "current_execution_capability", return_value=ExecutionCapability.LIVE
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_heartbeat_survives_spurious_cancel_and_renews(self):
        engine = _engine()
        calls = {"n": 0}
        renewed = asyncio.Event()
        owner = eo.ExecutionOwnership(
            "o", 1, "s", "a", "r",
            (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        )

        async def acquire():
            calls["n"] += 1
            if calls["n"] == 1:
                await _poisoned_future()
            renewed.set()
            return owner

        async def validate(_):
            return None

        with patch.object(eo, "acquire_execution_ownership", acquire), \
                patch.object(eo, "validate_execution_ownership", validate), \
                patch.object(eo, "_LEASE_SECONDS", 3):
            task = asyncio.create_task(eo.execution_ownership_heartbeat(engine))
            await asyncio.sleep(0)
            # After the spurious failure the lease is invalidated, not left valid.
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            self.assertFalse(engine._execution_ownership_valid)
            await asyncio.wait_for(renewed.wait(), timeout=3)
            await asyncio.sleep(0)
            self.assertTrue(engine._execution_ownership_valid)
            engine._running = False
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_heartbeat_exit_while_engine_running_invalidates(self):
        engine = _engine()
        started = asyncio.Event()

        async def acquire():
            started.set()
            await asyncio.sleep(3600)

        with patch.object(eo, "acquire_execution_ownership", acquire):
            task = asyncio.create_task(eo.execution_ownership_heartbeat(engine))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(engine._execution_ownership_valid)
        self.assertIsNone(engine._execution_ownership_expires_at)

    async def test_heartbeat_normal_shutdown_does_not_log_stop(self):
        engine = _engine()
        engine._running = False
        await eo.execution_ownership_heartbeat(engine)
        # Loop never ran; no invalidation is forced on a clean stop.
        self.assertTrue(engine._execution_ownership_valid)


class EngineSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def _fake(self, task):
        from bot.engine import TradingEngine
        fake = SimpleNamespace(
            _running=True, _ownership_heartbeat_task=task,
            _background_tasks={task}, _execution_ownership_valid=False,
        )
        fake._start_background = lambda coro: (
            fake._background_tasks.add(t := asyncio.create_task(coro)) or t
        )
        fake.supervise = lambda: TradingEngine._supervise_ownership_heartbeat(fake)
        fake.spurious = lambda: TradingEngine._spurious_cancel_in_cycle(fake)
        return fake

    async def test_dead_heartbeat_is_restarted_once_per_window_in_live(self):
        dead = asyncio.create_task(asyncio.sleep(0))
        await dead
        fake = self._fake(dead)

        async def hb(_engine):
            await asyncio.sleep(3600)

        with patch("bot.execution_capability.current_execution_capability",
                   return_value=ExecutionCapability.LIVE), \
                patch.object(eo, "execution_ownership_heartbeat", hb):
            fake.supervise()
            first = fake._ownership_heartbeat_task
            self.assertIsNot(first, dead)
            self.assertFalse(first.done())
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            fake.supervise()  # within 30s window: no restart storm
            self.assertIs(fake._ownership_heartbeat_task, first)

    async def test_supervisor_inert_outside_live(self):
        dead = asyncio.create_task(asyncio.sleep(0))
        await dead
        fake = self._fake(dead)
        with patch("bot.execution_capability.current_execution_capability",
                   return_value=ExecutionCapability.READ_ONLY):
            fake.supervise()
        self.assertIs(fake._ownership_heartbeat_task, dead)

    async def test_engine_classifies_cancel_without_request_as_spurious(self):
        fake = self._fake(None)
        self.assertTrue(fake.spurious())
        self.assertEqual(fake._spurious_cancel_count, 1)

    async def test_engine_classifies_real_cancel_as_shutdown(self):
        fake = self._fake(None)
        result = {}
        started = asyncio.Event()

        async def victim():
            try:
                started.set()
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                result["spurious"] = fake.spurious()
                raise

        task = asyncio.create_task(victim())
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(result["spurious"])


if __name__ == "__main__":
    unittest.main()
