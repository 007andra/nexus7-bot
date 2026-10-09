"""#596: two-phase read-only PostgreSQL sampler; no production DB connections."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from bot import oos_pg_phase_wait_probe_v2 as probe


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


class Conn:
    def __init__(self, pgss=False, pgss_denied=False, stalled=False):
        self.pgss = pgss
        self.pgss_denied = pgss_denied
        self.stalled = stalled
        self.closed = False
        self.commands = []
        self.pgss_calls = 0

    async def fetchval(self, sql):
        self.commands.append(sql)
        if self.pgss_denied:
            raise PermissionError("SECRET_SQL_AUTH_DENIED")
        return self.pgss

    async def fetch(self, sql):
        self.commands.append(sql)
        if self.stalled:
            await asyncio.Event().wait()
        if "FROM pg_stat_statements" in sql:
            self.pgss_calls += 1
            n = self.pgss_calls
            return [
                {"phase": "metadata", "calls": 10+n, "total_exec_ms": 100+n*11, "max_exec_ms": 140},
                {"phase": "candidates", "calls": 20+n*2, "total_exec_ms": 300+n*37, "max_exec_ms": 150},
            ]
        return [
            {"phase": "metadata", "matching": 1, "active": 1, "io": 0,
             "lock_wait": 0, "lwlock": 0, "client": 1,
             "no_wait_event": 0, "other_wait": 0,
             "query": "DO_NOT_RETURN_METADATA_SQL", "pid": 5555},
            {"phase": "candidates", "matching": 2, "active": 1, "io": 1,
             "lock_wait": 0, "lwlock": 0, "client": 0,
             "no_wait_event": 0, "other_wait": 0,
             "query": "DO_NOT_RETURN_CANDIDATES_SQL", "pid": 6666},
        ]

    async def close(self, timeout=None):
        self.closed = True


class TwoPhaseProbeTests(unittest.TestCase):
    def test_exact_select_only_and_bounded_both_phases(self):
        conn, clock = Conn(), Clock()
        result = asyncio.run(probe.observe(
            conn, seconds=5, interval_ms=500, clock=clock, sleep=clock.sleep))
        self.assertEqual(result["samples"], 10)
        self.assertEqual(result["phases"]["metadata"]["matching_session_observations"], 10)
        self.assertEqual(result["phases"]["candidates"]["matching_session_observations"], 20)
        self.assertEqual(result["phases"]["metadata"]["active_wait_type_sample_counts"]["client"], 10)
        self.assertEqual(result["phases"]["candidates"]["active_wait_type_sample_counts"]["io"], 10)
        self.assertFalse(result["live_allowed"])
        self.assertEqual(result["execution_effect"], "NONE")
        self.assertEqual(len(conn.commands), 12)
        for sql in conn.commands:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"))
            self.assertNotIn("EXPLAIN ANALYZE", sql.upper())
            self.assertNotIn("PG_TERMINATE_BACKEND", sql.upper())
        self.assertIn("hard_gate_shadow_candidates_v1", probe.ACTIVITY_SQL)
        self.assertIn("prospective_oos_cohort_v1", probe.ACTIVITY_SQL)
        payload = json.dumps(result)
        for secret in ("DO_NOT_RETURN_METADATA_SQL", "DO_NOT_RETURN_CANDIDATES_SQL",
                       "5555", "6666", "hard_gate_shadow_candidates_v1"):
            self.assertNotIn(secret, payload)

    def test_optional_pgss_phase_deltas(self):
        conn, clock = Conn(pgss=True), Clock()
        out = asyncio.run(probe.observe(
            conn, seconds=5, interval_ms=1000, clock=clock, sleep=clock.sleep))
        self.assertEqual(out["phases"]["metadata"]["pg_stat_statements"]["new_calls"], 1)
        self.assertEqual(out["phases"]["candidates"]["pg_stat_statements"]["new_calls"], 2)
        self.assertEqual(out["phases"]["metadata"]["pg_stat_statements"]["new_total_exec_ms"], 11)
        self.assertEqual(out["phases"]["candidates"]["pg_stat_statements"]["new_total_exec_ms"], 37)
        self.assertTrue(out["phases"]["candidates"]["pg_stat_statements"]["historical_max_not_window_specific"])

    def test_missing_extension_or_permissions_do_not_leak(self):
        conn, clock = Conn(pgss_denied=True), Clock()
        data = asyncio.run(probe.observe(
            conn, seconds=5, interval_ms=1000, clock=clock, sleep=clock.sleep))
        for phase in probe.PHASES:
            self.assertEqual(data["phases"][phase]["pg_stat_statements"]["availability"],
                             "UNAVAILABLE_OR_NOT_PERMITTED")
        self.assertNotIn("SECRET_SQL_AUTH_DENIED", json.dumps(data))

    def test_bounded_reject_invalid_or_flood(self):
        for seconds, interval in [(31,500),(4,500),(12,250),(12,2200)]:
            with self.assertRaises(ValueError):
                probe._valid_window(seconds, interval)
        with self.assertRaises(ValueError):
            probe._safe_count({"active": -1}, "active")

    def test_observation_timeout_propagates_and_cli_fails_closed(self):
        conn = Conn(stalled=True)
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(probe.observe(
                conn, seconds=5, interval_ms=500, clock=Clock()))
        with patch.object(probe, "_main_async", side_effect=RuntimeError("SECRET_PASSWORD")):
            sink = StringIO()
            with redirect_stdout(sink):
                code = probe.main(["--seconds", "5"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(sink.getvalue())["status"], "UNAVAILABLE_OR_FAILED_CLOSED")
        self.assertNotIn("SECRET_PASSWORD", sink.getvalue())

    def test_server_readonly_settings_and_cleanup(self):
        conn = Conn()
        async def connect(dsn, timeout, server_settings):
            self.assertEqual(dsn, "postgresql://SECRET@private")
            self.assertEqual(timeout, 5.0)
            self.assertEqual(server_settings["default_transaction_read_only"], "on")
            self.assertEqual(server_settings["statement_timeout"], "700")
            return conn
        with (patch.dict(os.environ, {"DATABASE_URL": "postgresql://SECRET@private"}),
              patch.dict(sys.modules, {"asyncpg": types.SimpleNamespace(connect=connect)})):
            out = asyncio.run(probe._main_async(seconds=5, interval_ms=1000))
        self.assertTrue(conn.closed)
        self.assertEqual(out["samples"], 5)
        self.assertNotIn("SECRET@private", json.dumps(out))

    def test_main_rejects_missing_dsn_without_leaks(self):
        buf = StringIO()
        with patch.dict(os.environ, {"DATABASE_URL": ""}), redirect_stdout(buf):
            code = probe.main(["--seconds", "5"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(buf.getvalue())["live_allowed"])


if __name__ == "__main__":
    unittest.main()
