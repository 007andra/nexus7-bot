"""Offline fail-closed/read-only contracts for aggregated TEMP consumer census."""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock

from scripts.pg_temp_consumer_inventory import (
    ACTIVITY_SQL, LOGIN_SQL, STATS_SQL, Inventory, collect, log_line,
)


def role_row(**overrides):
    row = {"login_roles": 5, "login_connect": 4,
           "login_temp": 3, "login_superusers": 1,
           "extra": "postgresql://SECRET_HOST/SECRET_DB"}
    row.update(overrides)
    return row


def activity_row(**overrides):
    row = {"sessions": 2, "active_sessions": 1,
           "idle_in_tx": 0, "distinct_session_roles": 2,
           "observer_super": False, "observer_stats_member": False,
           "query": "SELECT SECRET_TOKEN"}
    row.update(overrides)
    return row


def stats_row(files=10, bytes_=1000, reset="hidden-time"):
    return {"temp_files": files, "temp_bytes": bytes_,
            "stats_reset": reset, "db_name": "SECRET_DB"}


class ConsumerInventoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = AsyncMock()
        self.conn.fetchrow.side_effect = [
            role_row(), activity_row(), stats_row(),
            stats_row(11, 2048)
        ]
        self.pause = AsyncMock()

    async def test_collect_and_log_only_aggregate_redacted_bands(self):
        inv = await collect(self.conn, pause=self.pause)
        self.assertEqual(inv.login_roles, 5)
        self.assertEqual(inv.observer_visibility, "LIMITED")
        self.assertEqual(inv.files_delta, 1)
        self.assertEqual(inv.bytes_delta, 1048)
        self.assertEqual(self.conn.fetchrow.await_count, 4)
        self.assertEqual([c.args[0] for c in self.conn.fetchrow.await_args_list],
                         [LOGIN_SQL, ACTIVITY_SQL, STATS_SQL, STATS_SQL])
        self.pause.assert_awaited_once_with(2.0)
        line = log_line(inv)
        self.assertIn("status=OBSERVED", line)
        self.assertIn("session_visibility=LIMITED", line)
        self.assertIn("complete_dependency_proof=false", line)
        self.assertIn("roles_login=MULTIPLE", line)
        self.assertIn("temp_files_delta=ONE", line)
        self.assertIn("execution_effect=NONE", line)
        for secret in ("SECRET_HOST", "SECRET_DB", "SECRET_TOKEN",
                       "postgresql://", "query=", "client_addr="):
            self.assertNotIn(secret, line)

    async def test_stats_role_visibility_is_explicit(self):
        self.conn.fetchrow.side_effect = [
            role_row(), activity_row(observer_stats_member=True),
            stats_row(), stats_row()
        ]
        inv = await collect(self.conn, pause=self.pause, gap_seconds=0)
        self.assertEqual(inv.observer_visibility, "FULL_STATS_ROLE")
        self.assertIn("session_visibility=FULL_STATS_ROLE", log_line(inv))
        self.assertIn("temp_files_delta=NONE", log_line(inv))

    async def test_absent_roles_or_sessions_cannot_be_observed(self):
        for fake in (None, {}, role_row(login_roles=-1),
                     role_row(login_connect=6), role_row(login_temp=True),
                     role_row(login_superusers=6)):
            with self.subTest(fake=fake):
                self.conn.fetchrow.side_effect = [
                    fake, activity_row(), stats_row(), stats_row()
                ]
                with self.assertRaises(ValueError):
                    await collect(self.conn, pause=self.pause)
                self.conn.fetchrow.reset_mock()

    async def test_invalid_activity_counts_and_visibility_rejected(self):
        for bad in (
            activity_row(sessions=-1),
            activity_row(active_sessions=3),
            activity_row(distinct_session_roles=4),
            activity_row(observer_stats_member=None),
            activity_row(observer_super="true"),
        ):
            with self.subTest(bad=bad):
                self.conn.fetchrow.side_effect = [
                    role_row(), bad, stats_row(), stats_row()
                ]
                with self.assertRaises(ValueError):
                    await collect(self.conn, pause=self.pause)
                self.conn.fetchrow.reset_mock()

    async def test_negative_deltas_and_stats_reset_fail_closed(self):
        for later in (stats_row(9, 1010), stats_row(10, 999),
                      stats_row(10, 1000, reset="other-time"), None):
            with self.subTest(later=later):
                self.conn.fetchrow.side_effect = [
                    role_row(), activity_row(), stats_row(), later
                ]
                with self.assertRaises((ValueError, TypeError)):
                    await collect(self.conn, pause=self.pause)
                self.conn.fetchrow.reset_mock()

    def test_invalid_or_missing_result_logs_only_inconclusive(self):
        self.assertIn("status=INCONCLUSIVE", log_line(None))
        data = Inventory(
            login_roles=2, login_connect=1, login_temp=1,
            login_superusers=0, sessions=2, active_sessions=1,
            idle_in_tx=0, distinct_session_roles=1,
            observer_visibility="LIMITED", files_delta=0, bytes_delta=0)
        for case in (replace(data, login_roles=-1),
                     replace(data, observer_visibility="SECRET_TOKEN"),
                     replace(data, sessions=True), "SECRET_TOKEN"):
            with self.subTest(case=case):
                line = log_line(case)
                self.assertIn("status=INCONCLUSIVE", line)
                self.assertNotIn("SECRET_TOKEN", line)

    def test_sql_is_select_only_and_never_retrieves_identifiers(self):
        for sql in (LOGIN_SQL, ACTIVITY_SQL, STATS_SQL):
            normalized = sql.upper()
            self.assertTrue(normalized.lstrip().startswith("SELECT"))
            for forbidden in ("UPDATE ", "DELETE FROM", "INSERT INTO",
                              "CREATE ", "DROP ", "REVOKE ", "GRANT ",
                              "ALTER ", "PG_TERMINATE_BACKEND", "FOR UPDATE"):
                self.assertNotIn(forbidden, normalized)
        self.assertNotIn("SELECT *", ACTIVITY_SQL.upper())
        self.assertNotIn("QUERY AS", ACTIVITY_SQL.upper())
        self.assertNotIn("CLIENT_ADDR", ACTIVITY_SQL.upper())
        self.assertNotIn("APPLICATION_NAME", ACTIVITY_SQL.upper())


if __name__ == "__main__":
    unittest.main()
