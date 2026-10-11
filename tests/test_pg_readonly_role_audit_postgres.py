"""Real PostgreSQL proof that scripts/pg_readonly_role_audit.py classifies roles.

CI provides TEST_POSTGRES_DSN with a superuser able to create a disposable
database and roles (dropped afterwards). The audit itself only connects as
each test role and only reads catalogs.
"""
import os
import unittest
import uuid
from urllib.parse import urlparse

DSN = os.environ.get("TEST_POSTGRES_DSN", "")


def _dsn(role, db):
    p = urlparse(DSN)
    return f"postgresql://{role}:pw@{p.hostname or '127.0.0.1'}:{p.port or 5432}/{db}"


@unittest.skipUnless(DSN, "isolated PostgreSQL required (TEST_POSTGRES_DSN)")
class ReadOnlyRoleAuditPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.sfx = uuid.uuid4().hex[:8]
        self.db = f"audit_{self.sfx}"
        r = {k: f"{k}_{self.sfx}" for k in ("ro", "rw", "member", "writer", "secdef")}
        self.roles = r
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute(f"CREATE DATABASE {self.db}")
            for k in ("ro", "rw", "member", "secdef"):
                inherit = "NOINHERIT" if k == "member" else ""
                await admin.execute(f"CREATE ROLE {r[k]} LOGIN PASSWORD 'pw' {inherit}")
            await admin.execute(f"CREATE ROLE {r['writer']} NOLOGIN")
        finally:
            await admin.close()
        p = urlparse(DSN)
        target = await asyncpg.connect(
            f"postgresql://{p.username}:{p.password or ''}@{p.hostname}:{p.port or 5432}/{self.db}")
        try:
            for sql in (
                "CREATE TABLE public.candidates(id text primary key, payload text)",
                "CREATE TABLE public.key_value(key text primary key, value text)",
                f"REVOKE TEMP ON DATABASE {self.db} FROM PUBLIC",
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
                f"GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO {r['writer']}",
                f"GRANT {r['writer']} TO {r['member']}",
                f"GRANT INSERT ON public.key_value TO {r['rw']}",
                "CREATE FUNCTION public.bump(k text) RETURNS void LANGUAGE sql SECURITY DEFINER "
                "AS $f$ INSERT INTO public.key_value VALUES (k,'x') $f$",
                "REVOKE EXECUTE ON FUNCTION public.bump(text) FROM PUBLIC",
                f"GRANT EXECUTE ON FUNCTION public.bump(text) TO {r['secdef']}",
            ):
                await target.execute(sql)
            for k in ("ro", "rw", "member", "secdef"):
                await target.execute(f"GRANT CONNECT ON DATABASE {self.db} TO {r[k]}")
                await target.execute(f"GRANT USAGE ON SCHEMA public TO {r[k]}")
                await target.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {r[k]}")
                await target.execute(
                    f"ALTER ROLE {r[k]} IN DATABASE {self.db} SET default_transaction_read_only = on")
        finally:
            await target.close()

    async def asyncTearDown(self):
        import asyncpg
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1", self.db)
            await admin.execute(f"DROP DATABASE IF EXISTS {self.db}")
            for role in self.roles.values():
                await admin.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await admin.close()

    async def _audit(self, key):
        import asyncpg
        from scripts.pg_readonly_role_audit import run
        conn = await asyncpg.connect(_dsn(self.roles[key], self.db))
        try:
            async with conn.transaction(readonly=True):
                return await run(conn)
        finally:
            await conn.close()

    def _failed_items(self, audit):
        return sorted({r["item"] for r in audit.rows if r["result"] == "FAIL"})

    async def test_read_only_role_is_verified(self):
        audit = await self._audit("ro")
        self.assertTrue(audit.verified, audit.rows)

    async def test_direct_write_grant_fails(self):
        audit = await self._audit("rw")
        self.assertFalse(audit.verified)
        self.assertEqual(self._failed_items(audit), [5])

    async def test_write_via_settable_role_fails_even_with_noinherit(self):
        audit = await self._audit("member")
        self.assertFalse(audit.verified)
        self.assertEqual(self._failed_items(audit), [7])

    async def test_security_definer_function_fails(self):
        audit = await self._audit("secdef")
        self.assertFalse(audit.verified)
        self.assertEqual(self._failed_items(audit), [6])

    async def test_reachable_role_database_privilege_is_detected(self):
        import asyncpg
        from scripts.pg_readonly_role_audit import run
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute(
                f"GRANT CREATE ON DATABASE {self.db} TO {self.roles['writer']}")
        finally:
            await admin.close()
        audit = await self._audit("member")
        self.assertFalse(audit.verified)
        self.assertIn(7, self._failed_items(audit))

    async def test_reachable_role_schema_privilege_is_detected(self):
        import asyncpg
        p = urlparse(DSN)
        admin = await asyncpg.connect(
            f"postgresql://{p.username}:{p.password or ''}@{p.hostname}:{p.port or 5432}/{self.db}")
        try:
            await admin.execute(
                f"GRANT CREATE ON SCHEMA public TO {self.roles['writer']}")
        finally:
            await admin.close()
        audit = await self._audit("member")
        self.assertFalse(audit.verified)
        self.assertIn(7, self._failed_items(audit))

    async def test_reachable_role_sequence_privilege_is_detected(self):
        import asyncpg
        p = urlparse(DSN)
        admin = await asyncpg.connect(
            f"postgresql://{p.username}:{p.password or ''}@{p.hostname}:{p.port or 5432}/{self.db}")
        try:
            await admin.execute("CREATE SEQUENCE public.audit_seq")
            await admin.execute(
                f"GRANT USAGE ON SEQUENCE public.audit_seq TO {self.roles['writer']}")
        finally:
            await admin.close()
        audit = await self._audit("member")
        self.assertFalse(audit.verified)
        self.assertIn(7, self._failed_items(audit))

    async def test_reachable_role_security_definer_is_detected(self):
        import asyncpg
        p = urlparse(DSN)
        admin = await asyncpg.connect(
            f"postgresql://{p.username}:{p.password or ''}@{p.hostname}:{p.port or 5432}/{self.db}")
        try:
            await admin.execute(
                f"GRANT EXECUTE ON FUNCTION public.bump(text) TO {self.roles['writer']}")
        finally:
            await admin.close()
        audit = await self._audit("member")
        self.assertFalse(audit.verified)
        self.assertIn(7, self._failed_items(audit))

    async def test_unreachable_or_inconclusive_roles_fail_closed(self):
        from scripts.pg_readonly_role_audit import Audit
        audit = Audit()
        audit.add(7, "role capability", False, "unverified")
        self.assertFalse(audit.verified)

    async def test_output_never_contains_role_names(self):
        audit = await self._audit("member")
        text = repr(audit.rows)
        for role in self.roles.values():
            self.assertNotIn(role, text)


if __name__ == "__main__":
    unittest.main()
