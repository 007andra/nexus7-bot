"""Opt-in OOS PostgreSQL client timing must be redacted and fail-closed."""
import asyncio
import time
import unittest
from unittest.mock import patch

from bot import database
from bot.prospective_oos_snapshot_timing_v1 import SnapshotTimingProbe, active_probe


class FakeConnection:
    def __init__(self, *, fail=None, slow=False):
        self.fail = fail
        self.slow = slow
        self.calls = 0

    def is_closed(self):
        return False

    async def fetch(self, sql, *params):
        self.calls += 1
        if self.slow:
            await asyncio.sleep(0.51)
        if self.fail:
            raise self.fail
        return [{"payload": "{}"}]


class OOSPgFetchDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def run_fetch(self, enabled, fail=None, slow=False):
        probe = SnapshotTimingProbe()
        conn = FakeConnection(fail=fail, slow=slow)
        logs = []
        async def operation():
            return await database._fetchall(
                "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=?",
                ("SENSITIVE_COHORT_ID",),
            )
        with (patch.object(database, "_conn", conn),
              patch.object(database, "_is_pg", True),
              patch.object(database, "_pg_sql", lambda sql: sql),
              patch.dict(database.os.environ, {"OOS_PG_FETCH_DIAG_V1": "true" if enabled else "false"}),
              patch.object(database.log, "info", side_effect=lambda *a: logs.append(a)),
              patch.object(database.log, "warning")):
            token = active_probe.set(probe)
            try:
                result = await probe.await_stage("metadata", operation())
            finally:
                active_probe.reset(token)
        return result, conn.calls, logs, probe

    async def test_disabled_by_default_behavior(self):
        result, calls, logs, probe = await self.run_fetch(False)
        self.assertEqual(calls, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(logs, [])
        self.assertEqual(probe.fetch_calls, 1)

    async def test_enabled_preserves_result_without_logging_fast_calls(self):
        result, calls, logs, probe = await self.run_fetch(True)
        self.assertEqual(calls, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(logs, [])
        self.assertEqual(probe.rows_fetched, 1)

    async def test_error_semantics_unchanged(self):
        result, calls, logs, probe = await self.run_fetch(True, RuntimeError("test failure"))
        self.assertEqual(result, [])
        self.assertEqual(calls, 1)
        self.assertEqual(logs, [])
        self.assertEqual(probe.fetch_calls, 1)

    async def test_cancel_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_fetch(True, asyncio.CancelledError())

    async def test_slow_fetch_logs_redacted_client_timing(self):
        result, calls, logs, probe = await self.run_fetch(True, slow=True)
        self.assertEqual(calls, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(logs), 1)
        fmt, *values = logs[0]
        self.assertIn("server_time_ms=UNMEASURED", fmt)
        self.assertIn("client_driver_await_ms=", fmt)
        self.assertNotIn("SENSITIVE_COHORT_ID", str(logs))
        self.assertNotIn("SELECT payload", str(logs))
        self.assertGreaterEqual(values[1], 500)
        self.assertEqual(probe.rows_fetched, 1)

    async def test_disabled_slow_fetch_never_logs(self):
        result, calls, logs, probe = await self.run_fetch(False, slow=True)
        self.assertEqual(calls, 1)
        self.assertEqual(logs, [])
        self.assertEqual(probe.fetch_calls, 1)
