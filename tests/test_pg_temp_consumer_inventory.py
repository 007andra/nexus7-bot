"""Offline privacy, failure and opt-in tests for PostgreSQL TEMP preflight."""
import asyncio
from contextlib import redirect_stdout
from io import StringIO
import os
import unittest
from unittest.mock import AsyncMock, patch

from scripts import pg_temp_consumer_inventory as inv
from scripts import railway_pg_readonly_audit_once as worker


def sessions(**changes):
    base = dict(
        sessions=3, distinct_roles=2, missing_role_ids=0,
        active_sessions=1, in_transaction=1, idle_in_transaction=0,
        stats_visibility_privileged=False,
    )
    base.update(changes)
    return base


def privileges(**changes):
    base = dict(
        public_temp=True, login_roles=4, connect_login_roles=4,
        temp_connect_logins=4, superuser_logins=1, owner_logins=1,
        alternate_acl_logins=1, potentially_public_dependent_logins=2,
    )
    base.update(changes)
    return base


def stats(**changes):
    base = dict(temp_files=10, temp_bytes=1024, stats_reset=None)
    base.update(changes)
    return base


class FakeTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.ro.append(True)
        return self

    async def __aexit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, first=None, second=None, error=None):
        self.ro = []
        self.rows = [
            first or (sessions(), privileges(), stats()),
            second or (sessions(), privileges(), stats(temp_files=13, temp_bytes=2048)),
        ]
        self.index = 0
        self.error = error

    def transaction(self, *, readonly):
        if not readonly:
            raise AssertionError("read-write transaction")
        return FakeTransaction(self)

    async def fetchrow(self, sql):
        if self.error:
            raise self.error
        match = {
            inv.SESSIONS_SQL: 0,
            inv.PRIVILEGES_SQL: 1,
            inv.TEMP_STATS_SQL: 2,
        }
        if sql not in match:
            raise AssertionError("unexpected query")
        i = self.index
        if match[sql] == 2:
            self.index += 1
        return self.rows[i][match[sql]]


class ConsumerInventoryOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, conn, interval=0):
        output = StringIO()
        with redirect_stdout(output), patch.object(
            inv.asyncio, "sleep", new_callable=AsyncMock
        ) as sleeper:
            result = await inv.collect_and_log(conn, interval_seconds=interval)
        return result, output.getvalue(), sleeper

    async def test_two_readonly_samples_are_aggregate_only_and_never_go(self):
        conn = FakeConnection()
        result, output, sleeper = await self._run(conn)
        self.assertFalse(result)
        self.assertEqual(conn.ro, [True, True])
        sleeper.assert_awaited_once_with(0)
        self.assertIn("samples=2", output)
        self.assertIn("visibility=LIMITED", output)
        self.assertIn("status=INCONCLUSIVE", output)
        self.assertIn("temp_files_delta=3", output)
        self.assertIn("temp_bytes_delta=1024", output)
        self.assertIn("potentially_public_dependent_logins=2", output)
        self.assertIn("execution_effect=NONE", output)
        self.assertNotIn("status=PASS", output)

    async def test_privileged_visibility_still_is_not_complete_consumer_inventory(self):
        record = sessions(stats_visibility_privileged=True)
        conn = FakeConnection(first=(record, privileges(), stats()),
                              second=(record, privileges(), stats()))
        _, output, _ = await self._run(conn)
        self.assertIn("visibility=PRIVILEGED", output)
        self.assertIn("status=INCONCLUSIVE", output)
        self.assertIn("reason=consumer_coverage_unverified", output)

    async def test_driver_error_and_untrusted_identity_are_redacted(self):
        for value in (
            RuntimeError("postgres://role:SECRET_PASSWORD@10.0.0.1/db"),
            ValueError("role=ROLE_SECRET query=SELECT SECRET"),
        ):
            conn = FakeConnection(error=value)
            _, output, _ = await self._run(conn)
            self.assertIn("evidence=UNAVAILABLE", output)
            for secret in ("SECRET_PASSWORD", "ROLE_SECRET", "10.0.0.1", "SELECT SECRET"):
                self.assertNotIn(secret, output)

    async def test_partial_missing_and_contradictory_aggregates_fail_closed(self):
        bad = [
            (None, privileges(), stats()),
            (sessions(sessions="3"), privileges(), stats()),
            (sessions(active_sessions=5), privileges(), stats()),
            (sessions(stats_visibility_privileged="yes"), privileges(), stats()),
            (sessions(), privileges(public_temp=False), stats()),
            (sessions(), privileges(connect_login_roles=9), stats()),
        ]
        for row in bad:
            with self.subTest(row=row):
                # Avoid FakeConnection's default substitution for None.
                conn = FakeConnection()
                conn.rows[0] = row
                _, output, _ = await self._run(conn)
                self.assertIn("status=INCONCLUSIVE evidence=UNAVAILABLE", output)
                self.assertNotIn("status=PASS", output)

    async def test_counter_reset_or_decrease_is_inconclusive(self):
        second = (sessions(), privileges(), stats(temp_files=9, temp_bytes=1023))
        for variant in (
            second,
            (sessions(), privileges(), stats(stats_reset="new-reset")),
            (sessions(), privileges(public_temp=False,
                                   potentially_public_dependent_logins=0), stats()),
        ):
            conn = FakeConnection(second=variant)
            _, output, _ = await self._run(conn)
            self.assertIn("evidence=UNAVAILABLE", output)

    async def test_invalid_interval_and_cancel_sanitize(self):
        conn = FakeConnection()
        _, output, _ = await self._run(conn, interval=float("nan"))
        self.assertIn("evidence=UNAVAILABLE", output)
        self.assertEqual(conn.ro, [])
        conn = FakeConnection()
        with patch.object(inv.asyncio, "sleep", side_effect=asyncio.CancelledError), \
                redirect_stdout(StringIO()) as captured:
            with self.assertRaises(asyncio.CancelledError):
                await inv.collect_and_log(conn, interval_seconds=0)
        self.assertIn("evidence=UNAVAILABLE", captured.getvalue())

    def test_catalog_queries_are_read_only_and_no_identity_is_selected(self):
        for sql in (inv.SESSIONS_SQL, inv.PRIVILEGES_SQL, inv.TEMP_STATS_SQL):
            normalized = sql.upper()
            self.assertIn("SELECT ", normalized)
            for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE",
                              "ALTER ROLE", "GRANT ON", "REVOKE ", "DROP TABLE",
                              "PG_TERMINATE_BACKEND", "SELECT QUERY", "CLIENT_ADDR",
                              "APPLICATION_NAME"):
                self.assertNotIn(forbidden, normalized)
        self.assertIn("CURRENT_DATABASE()", inv.SESSIONS_SQL.upper())
        self.assertIn("CURRENT_DATABASE()", inv.TEMP_STATS_SQL.upper())

    async def test_worker_has_independent_exact_true_opt_in_flag(self):
        # Existing audit worker must not activate the new inventory
        # unless the exact new flag is set. This never connects to a DB.
        source = __import__("inspect").getsource(worker.execute)
        self.assertIn('os.environ.get("PG_TEMP_CONSUMER_PREFLIGHT_ONCE", "") == "true"', source)
        self.assertIn("collect_and_log(conn)", source)
        with patch.dict(os.environ, {"PG_TEMP_CONSUMER_PREFLIGHT_ONCE": "TRUE"}, clear=True):
            self.assertNotEqual(os.environ.get("PG_TEMP_CONSUMER_PREFLIGHT_ONCE"), "true")


if __name__ == "__main__":
    unittest.main()
