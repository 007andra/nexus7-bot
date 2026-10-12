"""Disposable PostgreSQL proof for TEMP-from-PUBLIC compatibility.

This test never connects to Railway production. TEST_POSTGRES_DSN must target
the isolated GitHub Actions PostgreSQL service, as in existing P0 regressions.
The test creates one unique disposable database and login role per case,
revokes TEMP FROM PUBLIC ONLY on that database, exercises real application
schema bootstrap / representative durable writes, and tears both down.
"""
from __future__ import annotations

import logging
import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@__import__("unittest").skipUnless(TEST_DSN, "isolated PostgreSQL required")
class TempPublicRevocationCompatibilityPostgres(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        suffix = uuid4().hex[:12]
        self.dbname = f"temp_scope_{suffix}"
        self.login = f"temp_app_{suffix}"
        self.admin = await asyncpg.connect(TEST_DSN)
        self.app = None
        self.database_created = False
        self.role_created = False
        self._register_cleanup = True
        # addAsyncCleanup executes even if asyncSetUp fails partway through.
        self.addAsyncCleanup(self._cleanup_database_and_role)

        await self.admin.execute(
            f"CREATE ROLE {self.login} LOGIN PASSWORD 'test-only-password'"
        )
        self.role_created = True
        await self.admin.execute(f"CREATE DATABASE {self.dbname}")
        self.database_created = True

        parsed = urlparse(TEST_DSN)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 5432
        if not (host in ("localhost", "127.0.0.1", "::1")
                or os.environ.get("GITHUB_ACTIONS", "").lower() == "true"):
            self.skipTest("destructive setup requires isolated CI database")
        app_dsn = (
            f"postgresql://{self.login}:test-only-password@"
            f"{host}:{port}/{self.dbname}"
        )
        # Only the transient fixture database's ACL and public schema grants
        # are changed.  Never manipulate the shared CI database privileges.
        isolated_admin_dsn = TEST_DSN.rsplit("/", 1)[0] + "/" + self.dbname
        scoped_admin = await asyncpg.connect(isolated_admin_dsn)
        try:
            await scoped_admin.execute(
                f"REVOKE TEMPORARY ON DATABASE {self.dbname} FROM PUBLIC"
            )
            await scoped_admin.execute(
                f"GRANT CONNECT ON DATABASE {self.dbname} TO {self.login}"
            )
            await scoped_admin.execute(
                f"GRANT USAGE, CREATE ON SCHEMA public TO {self.login}"
            )
        finally:
            await scoped_admin.close()

        self.app = await asyncpg.connect(app_dsn)

    async def _cleanup_database_and_role(self):
        if self.app is not None:
            try:
                await self.app.close()
            except Exception:
                self.app.terminate()
        if self.admin is None:
            return
        try:
            if self.database_created:
                await self.admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=$1 AND pid <> pg_backend_pid()",
                    self.dbname,
                )
                await self.admin.execute(f"DROP DATABASE IF EXISTS {self.dbname}")
            if self.role_created:
                await self.admin.execute(f"DROP ROLE IF EXISTS {self.login}")
        finally:
            await self.admin.close()

    async def test_temp_denied_and_permanent_schema_bootstrap_succeeds(self):
        import asyncpg
        from bot import database as db
        from bot import nexus_persistence as nexus
        from bot.runtime_hardening import install_database_schema_fix

        self.assertFalse(await self.app.fetchval(
            "SELECT has_database_privilege(current_database(), 'TEMP')"
        ))
        with self.assertRaises(asyncpg.InsufficientPrivilegeError):
            await self.app.execute(
                "CREATE TEMPORARY TABLE forbidden_temp_probe(id integer)"
            )

        # Execute the actual production hardening schema initializer against
        # our disposable, non-owner login; do not initialize the trading engine.
        with (
            patch.object(db, "_conn", self.app),
            patch.object(db, "_is_pg", True),
            patch.object(db, "_schema_hardening_patched", False, create=True),
            patch.object(db, "_create_tables", db._create_tables),
        ):
            install_database_schema_fix(logging.getLogger(__name__))
            await db._create_tables()
            await db._create_tables()  # idempotent startup/restart

        for table in ("trades", "signals", "decisions", "key_value"):
            self.assertTrue(await self.app.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", f"public.{table}"
            ))

        # Actual NEXUS persistent DDL and indexes (no temporary relation).
        await self.app.execute(nexus._DDL)
        await self.app.execute(
            "CREATE INDEX IF NOT EXISTS idx_nexus_ts ON nexus_decisions(ts)"
        )
        await self.app.execute(
            "CREATE INDEX IF NOT EXISTS idx_nexus_shadow "
            "ON nexus_decisions(shadow_status, ts)"
        )

        # Representative durable CAS / key-value read-write, independent of
        # the exchange, HWM, private WS, engine or trading authority.
        await self.app.execute(
            "INSERT INTO key_value(key,value,updated_at) VALUES ($1,$2,$3) "
            "ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=$3",
            "isolated-test", "before", "2026-01-01",
        )
        await self.app.execute(
            "INSERT INTO key_value(key,value,updated_at) VALUES ($1,$2,$3) "
            "ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=$3",
            "isolated-test", "after", "2026-01-02",
        )
        self.assertEqual(
            await self.app.fetchval(
                "SELECT value FROM key_value WHERE key=$1", "isolated-test"
            ),
            "after",
        )

    async def test_rollback_grant_restores_temp_only_inside_fixture(self):
        import asyncpg

        self.assertFalse(await self.app.fetchval(
            "SELECT has_database_privilege(current_database(), 'TEMP')"
        ))
        # Simulate an authorized rollback, only in a unique throwaway DB.
        await self.admin.execute(
            f"GRANT TEMPORARY ON DATABASE {self.dbname} TO PUBLIC"
        )
        self.assertTrue(await self.app.fetchval(
            "SELECT has_database_privilege(current_database(), 'TEMP')"
        ))
        await self.app.execute("CREATE TEMP TABLE temp_rollback_probe(id integer)")
        await self.app.execute("INSERT INTO temp_rollback_probe VALUES (42)")
        self.assertEqual(
            await self.app.fetchval("SELECT id FROM temp_rollback_probe"), 42
        )


if __name__ == "__main__":
    __import__("unittest").main()
