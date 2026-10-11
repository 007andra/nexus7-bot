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


class ConnectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Recovery is single-flight, rate-limited and ignores SQL errors."""

    def setUp(self):
        self._saved = (db._conn, db._is_pg, db.DATABASE_URL, db._recovery_task,
                       db._recovery_last_attempt)
        db._conn, db._is_pg = _PoisonedConn(), True
        db.DATABASE_URL = "postgresql://unit@127.0.0.1:1/none"
        db._recovery_task, db._recovery_last_attempt = None, float("-inf")

    def tearDown(self):
        (db._conn, db._is_pg, db.DATABASE_URL, db._recovery_task,
         db._recovery_last_attempt) = self._saved

    async def test_spurious_cancel_schedules_single_flight_recovery(self):
        started = asyncio.Event()

        async def fake_recover(reason):
            started.set()
            await asyncio.sleep(3600)

        with patch.object(db, "_recover_connection", fake_recover):
            with self.assertRaises(db.DatabaseOperationAborted):
                await db._fetchall("SELECT 1")
            first = db._recovery_task
            self.assertIsNotNone(first)
            with self.assertRaises(db.DatabaseOperationAborted):
                await db._fetchone("SELECT 1")
            self.assertIs(db._recovery_task, first)  # no duplicate reconnect
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async def test_recovery_is_rate_limited(self):
        calls = []

        async def fake_recover(reason):
            calls.append(reason)

        with patch.object(db, "_recover_connection", fake_recover):
            t1 = db.schedule_connection_recovery("a")
            await t1
            self.assertIsNone(db.schedule_connection_recovery("b"))
        self.assertEqual(calls, ["a"])

    async def test_sql_errors_do_not_trigger_recovery(self):
        class UndefinedTableError(Exception):
            pass

        class _Conn:
            def is_closed(self):
                return False

        db._conn = _Conn()
        with patch.object(db, "schedule_connection_recovery") as sched:
            db.note_connection_failure(UndefinedTableError("x"), reason="t")
            db.note_connection_failure(TimeoutError(), reason="t")
            sched.assert_not_called()
            db.note_connection_failure(ConnectionResetError("x"), reason="t")
            sched.assert_called_once()

    async def test_closed_connection_triggers_recovery(self):
        class _Closed:
            def is_closed(self):
                return True

        db._conn = _Closed()
        with patch.object(db, "schedule_connection_recovery") as sched:
            db.note_connection_failure(RuntimeError("generic"), reason="t")
        sched.assert_called_once()

    async def test_failed_reconnect_keeps_old_connection_and_reports(self):
        old = db._conn

        async def boom(*a, **k):
            raise OSError("unreachable")

        import asyncpg
        with patch.object(asyncpg, "connect", boom):
            self.assertFalse(await db._recover_connection("t"))
        self.assertIs(db._conn, old)
        self.assertFalse(db._io_lock.locked())

    async def test_successful_reconnect_swaps_and_terminates_old(self):
        terminated = []

        class _Old:
            def terminate(self):
                terminated.append(True)

        class _New:
            pass

        db._conn = _Old()
        new = _New()

        async def ok(*a, **k):
            return new

        import asyncpg
        with patch.object(asyncpg, "connect", ok):
            self.assertTrue(await db._recover_connection("t"))
        self.assertIs(db._conn, new)
        self.assertEqual(terminated, [True])

    async def test_close_on_poisoned_connection_terminates_instead_of_raising(self):
        terminated = []

        class _Poisoned:
            async def close(self):
                await _poisoned_future()

            def terminate(self):
                terminated.append(True)

        db._conn = _Poisoned()
        await db.close()
        self.assertIsNone(db._conn)
        self.assertEqual(terminated, [True])


class FencedDispatchTests(unittest.IsolatedAsyncioTestCase):
    """No exposure-increasing POST may leave when ownership cannot be proven."""

    def setUp(self):
        self._saved = (db._conn, db._is_pg)
        db._conn, db._is_pg = _PoisonedConn(), True
        patcher = patch.object(
            eo, "current_execution_capability", return_value=ExecutionCapability.LIVE
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db._conn, db._is_pg = self._saved

    def _client(self):
        from unittest.mock import AsyncMock
        from bot.binance import BinanceClient
        client = BinanceClient.__new__(BinanceClient)
        client._execution_ownership = eo.ExecutionOwnership(
            "o", 7, "s", "a", "h", "2099-01-01T00:00:00+00:00")
        client.entries_paused = False
        client._request = AsyncMock(return_value={"orderId": 1})
        client._recover_ambiguous_order = AsyncMock(return_value=None)
        return client

    async def test_new_risk_order_blocked_when_ownership_db_poisoned(self):
        client = self._client()
        body = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                "quantity": "0.001", "newClientOrderId": "bgx-test-1"}
        with self.assertRaises(db.DatabaseOperationAborted):
            await client._post("/fapi/v1/order", body)
        client._request.assert_not_awaited()
        client._recover_ambiguous_order.assert_not_awaited()

    async def test_reduce_only_order_is_not_gated_by_ownership_db(self):
        client = self._client()
        body = {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET",
                "quantity": "0.001", "reduceOnly": "true",
                "newClientOrderId": "bgx-test-2"}
        await client._post("/fapi/v1/order", body)
        client._request.assert_awaited_once()

    async def test_expired_fence_blocks_new_risk_order(self):
        import json

        class _ExpiredLease:
            async def fetchrow(self, *a, **k):
                return (json.dumps({"owner_id": "o", "fencing_token": 7,
                                    "expires_at": "2000-01-01T00:00:00+00:00"}),)

        db._conn = _ExpiredLease()
        with patch.object(db, "configured_postgres_unavailable", return_value=False):
            client = self._client()
            with self.assertRaises(eo.StaleExecutionFence):
                await client._post("/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                    "quantity": "0.001", "newClientOrderId": "bgx-test-3"})
        client._request.assert_not_awaited()

    async def test_superseded_token_blocks_new_risk_order(self):
        import json

        class _OtherOwner:
            async def fetchrow(self, *a, **k):
                return (json.dumps({"owner_id": "other", "fencing_token": 8,
                                    "expires_at": "2099-01-01T00:00:00+00:00"}),)

        db._conn = _OtherOwner()
        with patch.object(db, "configured_postgres_unavailable", return_value=False):
            client = self._client()
            with self.assertRaises(eo.StaleExecutionFence):
                await client._post("/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                    "quantity": "0.001", "newClientOrderId": "bgx-test-4"})
        client._request.assert_not_awaited()


class ReadinessTruthTests(unittest.IsolatedAsyncioTestCase):
    async def _returned_task(self):
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        return task

    async def test_unrequested_clean_return_is_unhealthy(self):
        from bot.service_readiness import engine_task_healthy, engine_task_state
        task = await self._returned_task()
        self.assertFalse(engine_task_healthy(task, running=False, stop_requested=False))
        self.assertEqual(engine_task_state(task, stop_requested=False), "EXITED_UNEXPECTEDLY")

    async def test_requested_stop_is_healthy(self):
        from bot.service_readiness import engine_task_healthy, engine_task_state
        task = await self._returned_task()
        self.assertTrue(engine_task_healthy(task, running=False, stop_requested=True))
        self.assertEqual(engine_task_state(task, stop_requested=True), "STOPPED_ON_REQUEST")

    async def test_running_task_is_healthy(self):
        from bot.service_readiness import engine_task_healthy, engine_task_state
        task = asyncio.create_task(asyncio.sleep(3600))
        try:
            self.assertTrue(engine_task_healthy(task, running=True, stop_requested=False))
            self.assertEqual(engine_task_state(task, stop_requested=False), "RUNNING")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_engine_stop_sets_stop_requested(self):
        from bot.engine import TradingEngine
        fake = SimpleNamespace(
            active=True, _running=True,
            client=SimpleNamespace(entries_paused=False),
        )
        fake.pause_entries = lambda: TradingEngine.pause_entries(fake)
        TradingEngine.stop(fake)
        self.assertTrue(fake._stop_requested)
        self.assertFalse(fake._running)


if __name__ == "__main__":
    unittest.main()
