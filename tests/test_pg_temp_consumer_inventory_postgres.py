"""Real PostgreSQL evidence test; NEVER connects to Railway production.

Only runs against the pinned disposable local GitHub Actions Postgres service.
Creates unique fixture roles/database and drops them. The inventory probe
itself issues ONLY SELECT in two READ ONLY transactions, with no SQL writes.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
import unittest
from urllib.parse import urlparse, unquote
from uuid import uuid4

TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@unittest.skipUnless(TEST_DSN and os.environ.get("GITHUB_ACTIONS") == "true",
                     "requires isolated GitHub Actions PostgreSQL")
class ConsumerInventoryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        parsed = urlparse(TEST_DSN)
        if (os.environ.get("GITHUB_ACTIONS") != "true"
                or parsed.scheme not in ("postgresql", "postgres")
                or parsed.hostname not in ("localhost", "127.0.0.1")
                or parsed.port != 5432
                or parsed.path != "/nexus_release_proof"
                or parsed.username != "proof"):
            self.fail("unexpected/non-isolated PostgreSQL fixture")
        self.parsed = parsed
        suffix = uuid4().hex[:12]
        self.db = f"pg_inventory_{suffix}"
        self.diag = f"inv_diag_{suffix}"
        self.peer = f"inv_peer_{suffix}"
        self.admin = await asyncpg.connect(TEST_DSN)
        self.conn = None
        self.peer_conn = None
        self.roles_created = []
        self.db_created = False
        self.addAsyncCleanup(self._cleanup)
        for role in (self.diag, self.peer):
            await self.admin.execute(
                f"CREATE ROLE {role} LOGIN PASSWORD 'disposable-only'"
            )
            self.roles_created.append(role)
        await self.admin.execute(f"CREATE DATABASE {self.db}")
        self.db_created = True
        self.conn = await self._connect(self.diag)
        self.peer_conn = await self._connect(self.peer)

    async def _connect(self, role):
        import asyncpg
        return await asyncpg.connect(
            host=self.parsed.hostname, port=self.parsed.port,
            user=role, password="disposable-only", database=self.db,
        )

    async def _cleanup(self):
        for conn in (self.peer_conn, self.conn):
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    conn.terminate()
        if self.admin is not None:
            try:
                if self.db_created:
                    await self.admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname=$1 AND pid <> pg_backend_pid()", self.db,
                    )
                    await self.admin.execute(f"DROP DATABASE IF EXISTS {self.db}")
                for role in reversed(self.roles_created):
                    await self.admin.execute(f"DROP ROLE IF EXISTS {role}")
            finally:
                await self.admin.close()

    async def test_live_catalog_counts_readonly_without_names(self):
        from scripts.pg_temp_consumer_inventory import collect_and_log
        output = StringIO()
        with redirect_stdout(output):
            allowed = await collect_and_log(self.conn, interval_seconds=0)
        line = output.getvalue()
        self.assertFalse(allowed)
        self.assertIn("status=INCONCLUSIVE evidence=PARTIAL", line)
        self.assertIn("visibility=LIMITED", line)
        self.assertIn("public_temp=true", line)
        self.assertIn("samples=2", line)
        self.assertIn("execution_effect=NONE", line)
        for secret in (self.db, self.diag, self.peer, "disposable-only", "127.0.0.1"):
            self.assertNotIn(secret, line)
        # A diagnostic login without pg_read_all_stats cannot necessarily
        # see the role IDs of OTHER sessions: distinct_roles is a lower bound,
        # not the actual number of connected roles (hence LIMITED/NO-GO).
        import re
        m = re.search(r"distinct_roles=(\d+)", line)
        self.assertIsNotNone(m, line)
        self.assertGreaterEqual(int(m.group(1)), 1)

    async def test_direct_grant_is_distinguished_from_public_dependency(self):
        # All GRANT statements below affect disposable fixture DB, never prod.
        from scripts.pg_temp_consumer_inventory import PRIVILEGES_SQL
        before = await self.conn.fetchrow(PRIVILEGES_SQL)
        await self.admin.execute(
            f"GRANT TEMP ON DATABASE {self.db} TO {self.peer}"
        )
        after = await self.conn.fetchrow(PRIVILEGES_SQL)
        self.assertTrue(before["public_temp"])
        self.assertTrue(after["public_temp"])
        self.assertGreaterEqual(after["alternate_acl_logins"],
                                before["alternate_acl_logins"] + 1)
        self.assertLessEqual(after["potentially_public_dependent_logins"],
                             before["potentially_public_dependent_logins"] - 1)


if __name__ == "__main__":
    unittest.main()
