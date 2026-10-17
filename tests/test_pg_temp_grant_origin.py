"""Offline tests for strict, redacted PostgreSQL TEMP privilege attribution."""
import unittest
from unittest.mock import AsyncMock

from scripts.pg_temp_grant_origin import (
    TEMP_ORIGIN_SQL, TempOrigin, inspect_temp_origin, origin_log_line,
)


def record(*, effective=True, public=False, direct=False, inherited=False,
           owner=False, default=False):
    return {
        "other_login_roles_public_dependent": 2 if public else 0,
        "effective_temp": effective,
        "from_public": public,
        "from_direct": direct,
        "from_inherited": inherited,
        "from_owner": owner,
        "default_acl": default,
    }


class TempGrantOriginOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_default_attributed_without_identity_leakage(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = record(public=True, default=True)
        origin = await inspect_temp_origin(conn)
        conn.fetchrow.assert_awaited_once_with(TEMP_ORIGIN_SQL)
        line = origin_log_line(origin)
        self.assertIn("status=RESOLVED", line)
        self.assertIn("public=true", line)
        self.assertIn("direct=false", line)
        self.assertIn("inherited=false", line)
        self.assertIn("default_acl=true", line)
        self.assertIn("potential_revocation_scope=DATABASE_WIDE", line)
        self.assertIn("other_login_roles_public_dependent=MULTIPLE", line)
        self.assertIn("execution_effect=NONE", line)

    async def test_direct_grant_alone_and_inherited_grant_alone(self):
        for source in ("direct", "inherited"):
            with self.subTest(source=source):
                conn = AsyncMock()
                conn.fetchrow.return_value = record(**{source: True})
                origin = await inspect_temp_origin(conn)
                self.assertIn(f"{source}=true", origin_log_line(origin))
                self.assertIn("potential_revocation_scope=ROLE_REVIEW_REQUIRED",
                              origin_log_line(origin))

    async def test_multiple_grant_sources_reported_separately(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = record(public=True, direct=True,
                                            inherited=True, default=False)
        origin = await inspect_temp_origin(conn)
        line = origin_log_line(origin)
        for source in ("public", "direct", "inherited"):
            self.assertIn(f"{source}=true", line)
        self.assertIn("potential_revocation_scope=DATABASE_WIDE", line)

    async def test_owner_is_an_attributable_source(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = record(owner=True)
        origin = await inspect_temp_origin(conn)
        self.assertIn("owner=true", origin_log_line(origin))

    async def test_missing_or_contradictory_evidence_fails_closed(self):
        for row in (None, {}, record(effective=False, public=True),
                    record(effective=True), record(public="true"),
                    record(public=True, direct=None),
                    {**record(public=True), "other_login_roles_public_dependent": -1},
                    {**record(public=True), "other_login_roles_public_dependent": True},
                    {**record(public=False, direct=True), "other_login_roles_public_dependent": 1}):
            with self.subTest(row=row):
                conn = AsyncMock()
                conn.fetchrow.return_value = row
                with self.assertRaises(ValueError):
                    await inspect_temp_origin(conn)

    async def test_never_echo_untrusted_record_content(self):
        conn = AsyncMock()
        row = record(public=True)
        row["extra"] = "postgres://password=SECRET_URI"
        conn.fetchrow.return_value = row
        line = origin_log_line(await inspect_temp_origin(conn))
        self.assertNotIn("SECRET_URI", line)
        self.assertNotIn("postgres://", line)
        self.assertNotIn("password", line)

    async def test_zero_and_single_potential_dependency_bands(self):
        for affected, band in ((0, "NONE"), (1, "ONE")):
            with self.subTest(affected=affected):
                conn = AsyncMock()
                row = record(public=True)
                row["other_login_roles_public_dependent"] = affected
                conn.fetchrow.return_value = row
                line = origin_log_line(await inspect_temp_origin(conn))
                self.assertIn(f"other_login_roles_public_dependent={band}", line)

    def test_inconclusive_has_no_identity_or_source_guesses(self):
        line = origin_log_line(None)
        self.assertIn("status=INCONCLUSIVE", line)
        for unsafe in ("public=true", "direct=true", "owner=true", "inherited=true"):
            self.assertNotIn(unsafe, line)
        self.assertIn("execution_effect=NONE", line)

    def test_static_sql_is_catalog_only(self):
        sql = TEMP_ORIGIN_SQL.upper()
        self.assertIn("ACLEXPLODE", sql)
        self.assertIn("ACLDEFAULT", sql)
        self.assertIn("PG_HAS_ROLE", sql)
        self.assertIn("HAS_DATABASE_PRIVILEGE", sql)
        for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE",
                          "ALTER ROLE", "REVOKE ", "GRANT ", "SET ROLE",
                          "DROP TABLE", "PG_TERMINATE_BACKEND"):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
