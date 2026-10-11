"""Offline contract tests for the disposable Railway PostgreSQL audit worker."""
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from scripts import railway_pg_readonly_audit_once as worker


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


def report(fail_item=None, drop_item=None):
    rows = [
        {"item": item, "result": "FAIL" if item == fail_item else "PASS",
         "evidence": "SECRET_ROLE SECRET_TABLE SECRET_URI"}
        for item, num in worker.EXPECTED_CHECKS.items()
        for _ in range(num)
        if item != drop_item
    ]
    return SimpleNamespace(rows=rows, verified=not fail_item and drop_item is None)


class RailwayReadOnlyAuditOnceTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, *, dsn="postgresql://user:SECRET_URI@host/db",
                    audit=None, error=None, close_fails=False):
        output = StringIO()
        conn = FakeConnection(close_fails=close_fails)
        with patch.dict(os.environ, {worker.DSN_ENV: dsn} if dsn else {}, clear=True), \
                patch("asyncpg.connect", new_callable=AsyncMock) as connect, \
                patch.object(worker, "run", new_callable=AsyncMock) as run, \
                redirect_stdout(output):
            connect.return_value = conn
            if error:
                connect.side_effect = error
            run.return_value = audit if audit is not None else report()
            rc = await worker.execute()
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
