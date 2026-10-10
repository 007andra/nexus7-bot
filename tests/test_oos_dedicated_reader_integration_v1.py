"""Opt-in dedicated OOS reader wiring: OFF keeps the shared path, ON fails closed.

The unit tests mock asyncpg. The ``RealPostgres`` class needs TEST_POSTGRES_DSN
(postgres:16 in CI) and proves read-only enforcement, server-side cancellation,
statement timeout and independence from the trading ``_io_lock``.
"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import hard_gate_shadow_scan as scan
from bot import oos_readonly_connection_v1 as dedicated
from bot import prospective_oos_cohort_v1 as oos
from bot.oos_readonly_connection_v1 import OOSReadOnlyConnection
from bot.prospective_oos_snapshot_timing_v1 import SnapshotTimingProbe, active_probe


def _live_conn():
    conn = AsyncMock()
    conn.is_closed = MagicMock(return_value=False)
    conn.terminate = MagicMock()
    return conn


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(dedicated.FLAG, None)
        os.environ[oos.FLAG] = "true"
        scan._PROSPECTIVE_OOS_LAST_EMIT = 0.0
        dedicated._reader = None

    def tearDown(self):
        dedicated._reader = None
        scan._PROSPECTIVE_OOS_LAST_EMIT = 0.0
        self._env.stop()


class FlagOffPreservesSharedPath(_Base):
    def test_flag_defaults_off(self):
        self.assertFalse(dedicated.enabled())

    async def test_off_passes_the_same_shared_db_and_never_connects(self):
        shared_db = object()
        seen = []

        async def fake_snapshot(db, *, timing_probe=None):
            seen.append(db)
            raise RuntimeError("stop-after-dispatch")

        with patch.object(oos, "snapshot", fake_snapshot), \
                patch.object(OOSReadOnlyConnection, "connect", new_callable=AsyncMock) as connect, \
                patch.object(scan, "_emit"):
            result = await scan._maybe_emit_prospective_oos_cohort(shared_db)
        self.assertIsNone(result)
        self.assertEqual(seen, [shared_db])
        connect.assert_not_awaited()
        self.assertIsNone(dedicated._reader)


class FlagOnUsesDedicatedReader(_Base):
    def setUp(self):
        super().setUp()
        os.environ[dedicated.FLAG] = "true"
        os.environ["DATABASE_URL"] = "postgresql://localhost/test"

    async def test_on_reads_through_reader_and_never_touches_shared_db(self):
        conn = _live_conn()
        shared_db = MagicMock()
        shared_db._fetchall = AsyncMock()
        shared_db._exec = AsyncMock()
        seen = []

        async def fake_snapshot(db, *, timing_probe=None):
            seen.append(db)
            raise RuntimeError("stop-after-dispatch")

        with patch.object(oos, "snapshot", fake_snapshot), \
                patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn), \
                patch.object(scan, "_emit"):
            await scan._maybe_emit_prospective_oos_cohort(shared_db)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], OOSReadOnlyConnection)
        shared_db._fetchall.assert_not_awaited()
        shared_db._exec.assert_not_awaited()

    async def test_connect_failure_fails_closed_without_shared_fallback(self):
        shared_db = MagicMock()
        shared_db._fetchall = AsyncMock()
        snapshot = AsyncMock()
        emitted = []
        with patch.object(oos, "snapshot", snapshot), \
                patch("asyncpg.connect", new_callable=AsyncMock,
                      side_effect=ConnectionRefusedError()), \
                patch.object(scan, "_emit", lambda tag, payload: emitted.append((tag, payload))):
            result = await scan._maybe_emit_prospective_oos_cohort(shared_db)
        self.assertIsNone(result)
        snapshot.assert_not_awaited()
        shared_db._fetchall.assert_not_awaited()
        self.assertEqual(emitted[0][0], "PROSPECTIVE_OOS_COHORT_V1")
        self.assertEqual(emitted[0][1]["status"], "ERROR")
        self.assertFalse(emitted[0][1]["live_allowed"])
        self.assertEqual(emitted[0][1]["execution_effect"], "NONE")
        self.assertIsNone(dedicated._reader)

    async def test_missing_database_url_fails_closed(self):
        os.environ["DATABASE_URL"] = ""
        snapshot = AsyncMock()
        with patch.object(oos, "snapshot", snapshot), patch.object(scan, "_emit"):
            self.assertIsNone(await scan._maybe_emit_prospective_oos_cohort(object()))
        snapshot.assert_not_awaited()

    async def test_timeout_discards_reader_and_next_cycle_reconnects(self):
        first, second = _live_conn(), _live_conn()

        async def slow_snapshot(db, *, timing_probe=None):
            await asyncio.sleep(10)

        with patch.object(oos, "snapshot", slow_snapshot), \
                patch("asyncpg.connect", new_callable=AsyncMock,
                      side_effect=[first, second]) as connect, \
                patch.object(scan, "_emit"), \
                patch.object(scan.asyncio, "wait_for",
                             side_effect=self._short_wait_for()):
            await scan._maybe_emit_prospective_oos_cohort(object())
            first.close.assert_awaited_once()
            self.assertIsNone(dedicated._reader)
            scan._PROSPECTIVE_OOS_LAST_EMIT = 0.0
            await scan._maybe_emit_prospective_oos_cohort(object())
        self.assertEqual(connect.await_count, 2)
        second.close.assert_awaited_once()

    def _short_wait_for(self):
        real = asyncio.wait_for

        async def short(aw, timeout):
            return await real(aw, timeout=min(timeout, 0.05))
        return short

    async def test_success_keeps_reader_for_reuse(self):
        conn = _live_conn()

        async def ok_snapshot(db, *, timing_probe=None):
            return {"ok": True}

        with patch.object(oos, "snapshot", ok_snapshot), \
                patch.object(oos, "format_summary", lambda r: "s"), \
                patch.object(oos, "format_concentration", lambda r: "c"), \
                patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn) as connect:
            for _ in range(2):
                scan._PROSPECTIVE_OOS_LAST_EMIT = 0.0
                self.assertEqual(
                    await scan._maybe_emit_prospective_oos_cohort(object()), {"ok": True}
                )
        self.assertEqual(connect.await_count, 1)
        conn.close.assert_not_awaited()

    async def test_cancellation_propagates_and_discards_reader(self):
        conn = _live_conn()
        started = asyncio.Event()

        async def blocking_snapshot(db, *, timing_probe=None):
            started.set()
            await asyncio.sleep(10)

        with patch.object(oos, "snapshot", blocking_snapshot), \
                patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn), \
                patch.object(scan, "_emit"):
            task = asyncio.create_task(scan._maybe_emit_prospective_oos_cohort(object()))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        conn.close.assert_awaited_once()
        self.assertIsNone(dedicated._reader)

    async def test_close_failure_terminates_instead_of_reusing(self):
        conn = _live_conn()
        conn.close.side_effect = OSError("broken")
        dedicated._reader = OOSReadOnlyConnection(conn)
        await dedicated.discard()
        conn.terminate.assert_called_once()
        self.assertIsNone(dedicated._reader)

    async def test_closed_cached_reader_is_replaced(self):
        stale, fresh = _live_conn(), _live_conn()
        stale.is_closed = MagicMock(return_value=True)
        dedicated._reader = OOSReadOnlyConnection(stale)
        with patch("asyncpg.connect", new_callable=AsyncMock, return_value=fresh):
            reader = await dedicated.acquire("postgresql://localhost/test")
        self.assertIs(reader._connection, fresh)


class ReaderObservability(_Base):
    async def test_reader_records_fetch_with_zero_lock_wait(self):
        conn = _live_conn()
        conn.fetch.return_value = [{"payload": "{}"}, {"payload": "{}"}]
        reader = OOSReadOnlyConnection(conn)
        probe = SnapshotTimingProbe()
        token = active_probe.set(probe)
        try:
            await probe.await_stage(
                "candidates", reader._fetchall("SELECT payload FROM t WHERE a=?", (1,))
            )
        finally:
            active_probe.reset(token)
        fields = probe.fields(status="OK")
        self.assertEqual(probe.fetch_calls, 1)
        self.assertEqual(probe.rows_fetched, 2)
        self.assertEqual(fields["lock_wait_ms"], 0.0)
        self.assertEqual(fields["lock_not_acquired"], 0)

    async def test_snapshot_labels_dedicated_mode(self):
        conn = _live_conn()
        conn.fetch.side_effect = [
            [{"payload": '{"started_epoch": 1.0}'}],
            [],
            [],
        ]
        reader = OOSReadOnlyConnection(conn)
        probe = SnapshotTimingProbe()
        with patch.object(oos, "build_report", lambda *a, **k: {"ok": True}):
            await oos.snapshot(reader, timing_probe=probe)
        self.assertEqual(probe.metadata_read_mode, "DEDICATED_READONLY_CONNECTION")
        conn.execute.assert_not_awaited()

    def test_window_report_accepts_dedicated_mode(self):
        from bot import oos_snapshot_window_report_v1 as report
        self.assertIn("DEDICATED_READONLY_CONNECTION", report.MODES)

    async def test_connect_requests_read_only_startup_settings(self):
        conn = _live_conn()
        with patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn) as connect:
            await OOSReadOnlyConnection.connect("postgresql://localhost/test")
        settings = connect.await_args.kwargs["server_settings"]
        self.assertEqual(settings["default_transaction_read_only"], "on")
        self.assertEqual(settings["statement_timeout"], str(dedicated.STATEMENT_TIMEOUT_MS))


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_DSN"), "isolated PostgreSQL required")
class RealPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.dsn = os.environ["TEST_POSTGRES_DSN"]
        self.admin = await asyncpg.connect(self.dsn)
        await self.admin.execute(
            "DROP TABLE IF EXISTS oos_reader_probe_v1;"
            "CREATE TABLE oos_reader_probe_v1 (id INT PRIMARY KEY, payload TEXT)"
        )
        await self.admin.execute("INSERT INTO oos_reader_probe_v1 VALUES (1, 'x')")
        self.reader = await OOSReadOnlyConnection.connect(self.dsn)

    async def asyncTearDown(self):
        await self.reader.close()
        await self.admin.execute("DROP TABLE IF EXISTS oos_reader_probe_v1")
        await self.admin.close()

    async def test_select_works(self):
        rows = await self.reader._fetchall(
            "SELECT payload FROM oos_reader_probe_v1 WHERE id=?", (1,)
        )
        self.assertEqual(rows[0]["payload"], "x")

    async def test_session_is_read_only_even_for_raw_writes(self):
        import asyncpg
        with self.assertRaises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            await self.reader._connection.execute(
                "INSERT INTO oos_reader_probe_v1 VALUES (2, 'y')"
            )
        with self.assertRaises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            await self.reader._fetchall(
                "SELECT id FROM oos_reader_probe_v1 WHERE id=? FOR UPDATE", (1,)
            )
        await self.reader._connection.execute("RESET ALL")
        with self.assertRaises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            await self.reader._connection.execute("CREATE TABLE oos_reader_ddl_v1 (x INT)")
        self.assertEqual(
            await self.admin.fetchval("SELECT count(*) FROM oos_reader_probe_v1"), 1
        )

    async def test_missing_metadata_table_fails_closed_without_ddl(self):
        await self.admin.execute("DROP TABLE IF EXISTS prospective_oos_cohort_v1")
        with self.assertRaises(Exception):
            await oos.load_frozen_metadata_for_snapshot(self.reader)
        self.assertIsNone(
            await self.admin.fetchval("SELECT to_regclass('prospective_oos_cohort_v1')")
        )

    async def test_client_timeout_cancels_server_query(self):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.reader._fetchall("SELECT pg_sleep(5)"), 0.3)
        await asyncio.sleep(0.3)
        running = await self.admin.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name=$1 AND state='active'",
            dedicated.APPLICATION_NAME,
        )
        self.assertEqual(running, 0)

    async def test_server_statement_timeout_bounds_query(self):
        import asyncpg
        with self.assertRaises(asyncpg.exceptions.QueryCanceledError):
            await self.reader._fetchall("SELECT pg_sleep(4)")

    async def test_reader_does_not_wait_on_trading_io_lock(self):
        from bot import database
        async with database._io_lock:
            rows = await asyncio.wait_for(
                self.reader._fetchall("SELECT payload FROM oos_reader_probe_v1"), 2.0
            )
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
