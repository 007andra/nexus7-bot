"""Offline contract tests for the disposable Railway PostgreSQL audit worker."""
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from scripts import railway_pg_readonly_audit_once as worker
from scripts.pg_temp_grant_origin import TempOrigin
from scripts.pg_temp_consumer_inventory import Inventory


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, typ, val, traceback):
        return False


class FakeConnection:
    def __init__(self, close_fails=False):
        self.readonly = None
        self.closed = False
        self.terminated = False
        self.close_fails = close_fails

    def transaction(self, *, readonly):
        self.readonly = readonly
        return FakeTransaction()

    async def close(self):
        self.closed = True
        if self.close_fails:
            raise RuntimeError("SECRET_URI in close error")

    def terminate(self):
        self.terminated = True


def report(fail_item=None, drop_item=None, fail_group3=None):
    rows = []
    labels3 = list(worker.DB_PRIVILEGE_CHECKS.values())
    for item, num in worker.EXPECTED_CHECKS.items():
        if item == drop_item:
            continue
        for index in range(num):
            label = labels3[index] if item == 3 else "synthetic test check"
            should_fail = item == fail_item or (
                item == 3 and fail_group3 == list(worker.DB_PRIVILEGE_CHECKS)[index]
            )
            rows.append({
                "item": item, "check": label,
                "result": "FAIL" if should_fail else "PASS",
                "evidence": "SECRET_ROLE SECRET_TABLE SECRET_URI",
            })
    return SimpleNamespace(rows=rows,
                           verified=not fail_item and not drop_item and not fail_group3)


class RailwayReadOnlyAuditOnceTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, *, dsn="postgresql://user:SECRET_URI@host/db",
                    audit=None, error=None, close_fails=False,
                    origin_error=None, inventory_error=None):
        output = StringIO()
        conn = FakeConnection(close_fails=close_fails)
        with patch.dict(os.environ, {worker.DSN_ENV: dsn} if dsn else {}, clear=True), \
                patch("asyncpg.connect", new_callable=AsyncMock) as connect, \
                patch.object(worker, "run", new_callable=AsyncMock) as run, \
                patch.object(worker, "inspect_temp_origin", new_callable=AsyncMock) as probe, \
                patch.object(worker, "collect_consumers", new_callable=AsyncMock) as census, \
                redirect_stdout(output):
            connect.return_value = conn
            if error:
                connect.side_effect = error
            run.return_value = audit if audit is not None else report()
            probe.return_value = TempOrigin(
                from_public=True, from_direct=False, from_inherited=False,
                from_owner=False, default_acl=True,
                other_login_roles_public_dependent=2,
            )
            if origin_error:
                probe.side_effect = RuntimeError("password=SECRET_URI")
            census.return_value = Inventory(
                login_roles=5, login_connect=4, login_temp=3,
                login_superusers=1, sessions=2, active_sessions=1,
                idle_in_tx=0, distinct_session_roles=2,
                observer_visibility="LIMITED", files_delta=0, bytes_delta=0,
            )
            if inventory_error:
                census.side_effect = RuntimeError("password=SECRET_URI")
            rc = await worker.execute()
            self.last_origin_probe_calls = probe.await_count
            self.last_inventory_calls = census.await_count
        return rc, output.getvalue(), conn, connect, run

    async def test_default_is_inert_without_dsn(self):
        rc, output, conn, connect, run = await self._call(dsn="")
        self.assertEqual(rc, 2)
        connect.assert_not_awaited()
        run.assert_not_awaited()
        self.assertIn("status=INCONCLUSIVE", output)
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output)

    async def test_valid_audit_is_read_only_and_redacted(self):
        rc, output, conn, connect, run = await self._call()
        self.assertEqual(rc, 0)
        self.assertTrue(conn.readonly)
        self.assertTrue(conn.closed)
        self.assertIn("status=PASS", output)
        self.assertIn("READ_ONLY_ROLE_VERIFIED=true", output)
        self.assertEqual(sum("[PG_ROLE_AUDIT_ONCE] item=" in x for x in output.splitlines()), 8)
        self.assertNotIn("SECRET_URI", output)
        self.assertNotIn("SECRET_ROLE", output)
        self.assertNotIn("SECRET_TABLE", output)
        connect.assert_awaited_once()
        run.assert_awaited_once()

    async def test_write_capability_stays_failed(self):
        rc, output, _, _, _ = await self._call(audit=report(fail_item=7))
        self.assertEqual(rc, 1)
        self.assertIn("item=7 result=FAIL", output)
        self.assertIn("status=FAIL", output)
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output)
        self.assertNotIn("SECRET_ROLE", output)

    async def test_all_three_group3_privileges_are_redacted_and_pass(self):
        rc, output, _, _, _ = await self._call()
        self.assertEqual(rc, 0)
        for name in ("CREATE", "TEMP", "OWNER"):
            self.assertIn(
                f"[PG_ROLE_AUDIT_DB_PRIVILEGES] check={name} result=PASS",
                output,
            )
        self.assertEqual(output.count("[PG_ROLE_AUDIT_DB_PRIVILEGES]"), 3)
        self.assertNotIn("SECRET_ROLE", output)
        self.assertNotIn("SECRET_TABLE", output)
        self.assertNotIn("SECRET_URI", output)

    async def test_each_group3_privilege_failure_is_distinguishable(self):
        for target in ("CREATE", "TEMP", "OWNER"):
            with self.subTest(target=target):
                rc, output, _, _, _ = await self._call(
                    audit=report(fail_group3=target))
                self.assertEqual(rc, 1)
                self.assertIn("[PG_ROLE_AUDIT_ONCE] item=3 result=FAIL", output)
                self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output)
                for name in ("CREATE", "TEMP", "OWNER"):
                    expected = "FAIL" if name == target else "PASS"
                    self.assertIn(
                        f"[PG_ROLE_AUDIT_DB_PRIVILEGES] check={name} "
                        f"result={expected}", output)
                self.assertNotIn("SECRET_URI", output)

    async def test_invalid_group3_label_cannot_become_verified_or_leak(self):
        audit = report()
        audit.rows[2]["check"] = "password=SECRET_URI"
        audit.verified = True
        rc, output, _, _, _ = await self._call(audit=audit)
        self.assertEqual(rc, 2)
        self.assertIn("status=INCONCLUSIVE", output)
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output)
        self.assertNotIn("[PG_ROLE_AUDIT_DB_PRIVILEGES]", output)
        self.assertNotIn("SECRET_URI", output)

    async def test_duplicate_group3_check_cannot_pass_even_with_correct_count(self):
        audit = report()
        audit.rows[3]["check"] = audit.rows[2]["check"]
        audit.verified = True
        rc, output, _, _, _ = await self._call(audit=audit)
        self.assertEqual(rc, 2)
        self.assertIn("status=INCONCLUSIVE", output)
        self.assertNotIn("[PG_ROLE_AUDIT_DB_PRIVILEGES]", output)

    async def test_temp_failure_reports_exact_public_source_only(self):
        rc, output, conn, _, _ = await self._call(
            audit=report(fail_group3="TEMP"))
        self.assertEqual(rc, 1)
        self.assertTrue(conn.readonly)
        self.assertEqual(self.last_origin_probe_calls, 1)
        self.assertEqual(self.last_inventory_calls, 1)
        self.assertIn("[PG_TEMP_CONSUMER_INVENTORY] status=OBSERVED", output)
        self.assertIn("session_visibility=LIMITED", output)
        self.assertIn("[PG_TEMP_ORIGIN] status=RESOLVED", output)
        self.assertIn("public=true direct=false inherited=false", output)
        self.assertIn("potential_revocation_scope=DATABASE_WIDE", output)
        self.assertIn("other_login_roles_public_dependent=MULTIPLE", output)
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output)
        self.assertNotIn("SECRET_URI", output)

    async def test_origin_failure_does_not_change_authoritative_audit_fail(self):
        rc, output, _, _, _ = await self._call(
            audit=report(fail_group3="TEMP"), origin_error=True)
        self.assertEqual(rc, 1)
        self.assertIn("[PG_TEMP_ORIGIN] status=INCONCLUSIVE", output)
        self.assertIn("[PG_ROLE_AUDIT_ONCE] status=FAIL", output)
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output)
        self.assertNotIn("SECRET_URI", output)
        self.assertNotIn("password=", output)

    async def test_consumer_inventory_failure_never_grants_audit_pass(self):
        rc, output, conn, _, _ = await self._call(
            audit=report(fail_group3="TEMP"), inventory_error=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.last_inventory_calls, 1)
        self.assertTrue(conn.readonly)
        self.assertIn("[PG_TEMP_CONSUMER_INVENTORY] status=INCONCLUSIVE", output)
        self.assertIn("[PG_ROLE_AUDIT_ONCE] status=FAIL", output)
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output)
        self.assertNotIn("SECRET_URI", output)

    async def test_origin_not_run_when_temp_is_not_failed(self):
        for audit in (report(), report(fail_item=7)):
            with self.subTest(verified=audit.verified):
                rc, output, _, _, _ = await self._call(audit=audit)
                self.assertEqual(self.last_origin_probe_calls, 0)
                self.assertEqual(self.last_inventory_calls, 0)
                self.assertNotIn("[PG_TEMP_ORIGIN]", output)
                self.assertNotIn("[PG_TEMP_CONSUMER_INVENTORY]", output)
                self.assertEqual(rc, 0 if audit.verified else 1)

    async def test_origin_not_run_for_invalid_main_audit(self):
        audit = report(fail_group3="TEMP")
        audit.rows[2]["check"] = "secret_role_name"
        rc, output, _, _, _ = await self._call(audit=audit)
        self.assertEqual(rc, 2)
        self.assertEqual(self.last_origin_probe_calls, 0)
        self.assertEqual(self.last_inventory_calls, 0)
        self.assertNotIn("[PG_TEMP_ORIGIN]", output)
        self.assertNotIn("[PG_TEMP_CONSUMER_INVENTORY]", output)

    async def test_missing_checks_cannot_pass(self):
        rc, output, _, _, _ = await self._call(audit=report(drop_item=6))
        self.assertEqual(rc, 2)
        self.assertIn("status=INCONCLUSIVE", output)
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output)

    async def test_connection_error_is_not_logged(self):
        rc, output, _, _, _ = await self._call(
            error=RuntimeError("password=SECRET_URI"))
        self.assertEqual(rc, 2)
        self.assertNotIn("SECRET_URI", output)
        self.assertNotIn("password", output)
        self.assertIn("status=INCONCLUSIVE", output)

    async def test_audit_error_is_not_logged(self):
        output = StringIO()
        conn = FakeConnection()
        with patch.dict(os.environ, {worker.DSN_ENV: "postgresql://secret@host/db"}, clear=True), \
                patch("asyncpg.connect", new_callable=AsyncMock, return_value=conn), \
                patch.object(worker, "run", new_callable=AsyncMock,
                             side_effect=RuntimeError("password=SHOULD_NOT_LEAK")), \
                redirect_stdout(output):
            rc = await worker.execute()
        self.assertEqual(rc, 2)
        self.assertTrue(conn.closed)
        self.assertNotIn("SHOULD_NOT_LEAK", output.getvalue())

    async def test_connection_close_error_fails_closed(self):
        rc, output, conn, _, _ = await self._call(close_fails=True)
        self.assertEqual(rc, 2)
        self.assertTrue(conn.terminated)
        self.assertIn("status=INCONCLUSIVE", output)
        self.assertNotIn("SECRET_URI", output)

    def test_contract_requires_every_original_check(self):
        self.assertEqual(sum(worker.EXPECTED_CHECKS.values()), 13)
        self.assertEqual(set(worker.EXPECTED_CHECKS), set(range(1, 9)))
        self.assertEqual(Counter(r["item"] for r in report().rows),
                         worker.EXPECTED_CHECKS)


if __name__ == "__main__":
    unittest.main()
