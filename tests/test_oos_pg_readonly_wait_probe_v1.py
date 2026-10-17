"""Tests for read-only, bounded PostgreSQL wait sampling (#596)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from bot import oos_pg_readonly_wait_probe_v1 as probe


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class FakeConnection:
    def __init__(self, *, has_pgss=False, pgss_error=False,
                 slow=False, fail_activity=False):
        self.sqls = []
        self.has_pgss = has_pgss
        self.pgss_error = pgss_error
        self.slow = slow
        self.fail_activity = fail_activity
        self.pgss_calls = 0
        self.closed = False

    async def fetchval(self, sql):
        self.sqls.append(sql)
        if self.pgss_error:
            raise PermissionError("S3CR3T_DSN_DO_NOT_LOG")
        return self.has_pgss

    async def fetchrow(self, sql):
        self.sqls.append(sql)
        if "pg_stat_statements" in sql:
            self.pgss_calls += 1
            return {
                "calls": 20 + self.pgss_calls,
                "total_exec_ms": 250 + self.pgss_calls * 12,
                "max_exec_ms": 112.0,
                "query": "SQL_SECRET_NEVER_OUTPUT",
            }
        if self.fail_activity:
            raise RuntimeError("PRIVATE_PASSWORD_NEVER_OUTPUT")
        if self.slow:
            await asyncio.Event().wait()
        return {
            "matching": 2,
            "active": 1,
            "io": 1,
            "lock_wait": 0,
            "lwlock": 0,
            "client": 0,
            "no_wait_event": 0,
            "other_wait": 0,
            "query": "SQL_SECRET_NEVER_OUTPUT",
            "pid": 123456789,
        }

    async def close(self, timeout=None):
        self.closed = True


class OOSReadOnlyPgProbeTests(unittest.TestCase):
    def test_bounded_windows_reject_high_frequency_or_unbounded(self):
        for seconds, interval in ((31, 500), (4, 500),
                                  (12, 100), (12, 2100)):
            with self.assertRaisesRegex(ValueError, "INVALID_BOUNDED"):
                probe._valid_window(seconds, interval)
        probe._valid_window(12, 750)

    def test_only_selects_and_no_leaks_in_output(self):
        conn = FakeConnection()
        clock = FakeClock()
        report = asyncio.run(probe.observe(
            conn, seconds=5, interval_ms=500, clock=clock,
            sleep=clock.sleep,
        ))
        self.assertEqual(report["samples"], 10)
        self.assertEqual(report["matching_session_observations"], 20)
        self.assertEqual(report["active_wait_type_sample_counts"]["io"], 10)
        self.assertEqual(
            report["pg_stat_statements"]["availability"],
            "UNAVAILABLE_OR_NOT_PERMITTED",
        )
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["automatic_promotion"])
        self.assertEqual(report["decision_effect"], "NONE")
        for sql in conn.sqls:
            self.assertEqual(sql.lstrip()[:6].upper(), "SELECT")
            self.assertNotIn("ANALYZE", sql.upper())
            self.assertNotIn("PG_TERMINATE_BACKEND", sql.upper())
        output = json.dumps(report)
        self.assertNotIn("SQL_SECRET", output)
        self.assertNotIn("123456789", output)
        self.assertNotIn("prospective_oos_cohort_v1", output)

    def test_optional_preinstalled_pgss_aggregate_deltas(self):
        conn = FakeConnection(has_pgss=True)
        clock = FakeClock()
        report = asyncio.run(probe.observe(
            conn, seconds=5, interval_ms=500, clock=clock,
            sleep=clock.sleep,
        ))
        stats = report["pg_stat_statements"]
        self.assertEqual(stats["availability"], "AGGREGATE_ONLY")
        self.assertEqual(stats["new_calls"], 1)
        self.assertAlmostEqual(stats["new_total_exec_ms"], 12)
        self.assertTrue(stats["historical_max_not_window_specific"])
        self.assertNotIn("SQL_SECRET", json.dumps(report))

    def test_optional_pgss_permission_denial_is_not_leaked(self):
        conn = FakeConnection(pgss_error=True)
        clock = FakeClock()
        report = asyncio.run(probe.observe(
            conn, seconds=5, interval_ms=1000, clock=clock,
            sleep=clock.sleep,
        ))
        self.assertEqual(report["samples"], 5)
        self.assertEqual(report["pg_stat_statements"]["availability"],
                         "UNAVAILABLE_OR_NOT_PERMITTED")
        self.assertNotIn("S3CR3T", json.dumps(report))

    def test_observer_timeout_fails_closed_not_hidden(self):
        conn = FakeConnection(slow=True)
        clock = FakeClock()
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(probe.observe(
                conn, seconds=5, interval_ms=500,
                clock=clock, sleep=clock.sleep,
            ))

    def test_numeric_validation_never_accepts_negative_counters(self):
        with self.assertRaises(ValueError):
            probe._rowsafe({"active": -1}, "active")

    def test_main_missing_database_url_generic_error_only(self):
        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            from io import StringIO
            from contextlib import redirect_stdout
            result = StringIO()
            with redirect_stdout(result):
                code = probe.main(["--seconds", "6"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(result.getvalue())["status"],
                         "UNAVAILABLE_OR_FAILED_CLOSED")
        self.assertFalse(json.loads(result.getvalue())["live_allowed"])

    def test_connection_readonly_gucs_and_cleanup(self):
        connection = FakeConnection()
        async def connect(dsn, timeout, server_settings):
            self.assertEqual(dsn, "postgresql://secret-do-not-print")
            self.assertEqual(timeout, 5.0)
            self.assertEqual(server_settings["default_transaction_read_only"], "on")
            self.assertEqual(server_settings["statement_timeout"], "700")
            return connection
        fake = types.SimpleNamespace(connect=connect)
        with (patch.dict(os.environ, {"DATABASE_URL": "postgresql://secret-do-not-print"}),
              patch.dict(sys.modules, {"asyncpg": fake})):
            data = asyncio.run(probe._main_async(seconds=5, interval_ms=1000))
        self.assertTrue(connection.closed)
        self.assertEqual(data["samples"], 5)
        self.assertNotIn("secret-do-not-print", json.dumps(data))

    def test_connection_cleanup_after_activity_error(self):
        connection = FakeConnection(fail_activity=True)
        async def connect(dsn, timeout, server_settings):
            return connection
        with (patch.dict(os.environ, {"DATABASE_URL": "postgres://bad-secret"}),
              patch.dict(sys.modules, {"asyncpg": types.SimpleNamespace(connect=connect)})):
            with self.assertRaisesRegex(RuntimeError, "PRIVATE_PASSWORD"):
                asyncio.run(probe._main_async(seconds=5, interval_ms=1000))
        self.assertTrue(connection.closed)

    def test_main_never_prints_connection_error_secrets(self):
        async def boom(**kwargs):
            raise RuntimeError("postgres://USER:PASSWORD_SECRET@hostname")
        with patch.object(probe, "_main_async", boom):
            from io import StringIO
            from contextlib import redirect_stdout
            stream = StringIO()
            with redirect_stdout(stream):
                result = probe.main(["--seconds", "5"])
        self.assertEqual(result, 2)
        self.assertNotIn("PASSWORD_SECRET", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
