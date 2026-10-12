"""Opt-in, aggregate-only PostgreSQL TEMP consumer evidence for issue #649.

Called by the existing railway_pg_readonly_audit_once worker; no new connection,
credentials, endpoint or service. Both observations use READ ONLY transactions.
Evidence is ALWAYS INCONCLUSIVE for database-wide ACL changes until separate
operational consumer discovery, rollback proof, and approval are completed.
Never log raw records, role names, SQL text, session identities or exceptions.
"""
from __future__ import annotations

import asyncio
import math

# No user-supplied identifiers in SQL. Limit to the database actually reached
# by OOS_DIAGNOSTIC_DATABASE_URL; do not inventory unrelated databases.
SESSIONS_SQL = """
SELECT count(*) FILTER (WHERE backend_type = 'client backend')::bigint AS sessions,
       count(DISTINCT usesysid) FILTER
         (WHERE backend_type = 'client backend')::bigint AS distinct_roles,
       count(*) FILTER
         (WHERE backend_type = 'client backend' AND usesysid IS NULL)::bigint
         AS missing_role_ids,
       count(*) FILTER
         (WHERE backend_type = 'client backend' AND state = 'active')::bigint
         AS active_sessions,
       count(*) FILTER
         (WHERE backend_type = 'client backend' AND xact_start IS NOT NULL)::bigint
         AS in_transaction,
       count(*) FILTER
         (WHERE backend_type = 'client backend'
            AND state IN ('idle in transaction', 'idle in transaction (aborted)'))::bigint
         AS idle_in_transaction,
       (
         COALESCE((SELECT rolsuper FROM pg_catalog.pg_roles
                   WHERE rolname = current_user), false)
         OR pg_catalog.pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')
       ) AS stats_visibility_privileged
  FROM pg_catalog.pg_stat_activity
 WHERE datname = current_database()
"""

PRIVILEGES_SQL = """
WITH target_db AS (
  SELECT oid, datdba, COALESCE(datacl, pg_catalog.acldefault('d', datdba)) AS acl
    FROM pg_catalog.pg_database WHERE datname = current_database()
), temp_acl AS (
  SELECT a.grantee FROM target_db AS d
  CROSS JOIN LATERAL pg_catalog.aclexplode(d.acl) AS a
   WHERE a.privilege_type IN ('TEMPORARY', 'TEMP')
), users AS (
  SELECT r.oid, r.rolsuper, r.oid = d.datdba AS is_owner,
         pg_catalog.has_database_privilege(r.oid, d.oid, 'CONNECT') AS can_connect,
         pg_catalog.has_database_privilege(r.oid, d.oid, 'TEMP') AS can_temp,
         EXISTS (
           SELECT 1 FROM temp_acl AS a
           WHERE a.grantee <> 0::oid
             AND (a.grantee = r.oid OR
                  pg_catalog.pg_has_role(r.oid, a.grantee, 'USAGE'))
         ) AS alternate_acl
  FROM pg_catalog.pg_roles AS r CROSS JOIN target_db AS d
  WHERE r.rolcanlogin
)
SELECT
  (SELECT EXISTS (SELECT 1 FROM temp_acl WHERE grantee = 0::oid))
    AS public_temp,
  count(*)::bigint AS login_roles,
  count(*) FILTER (WHERE can_connect)::bigint AS connect_login_roles,
  count(*) FILTER (WHERE can_connect AND can_temp)::bigint AS temp_connect_logins,
  count(*) FILTER (WHERE rolsuper)::bigint AS superuser_logins,
  count(*) FILTER (WHERE is_owner)::bigint AS owner_logins,
  count(*) FILTER (WHERE can_connect AND alternate_acl)::bigint
    AS alternate_acl_logins,
  count(*) FILTER (
    WHERE can_connect AND can_temp AND NOT rolsuper AND NOT is_owner
      AND NOT alternate_acl
  )::bigint AS potentially_public_dependent_logins
FROM users
"""

TEMP_STATS_SQL = """
SELECT temp_files::bigint AS temp_files,
       temp_bytes::bigint AS temp_bytes,
       stats_reset
  FROM pg_catalog.pg_stat_database
 WHERE datname = current_database()
"""

SESSION_KEYS = (
    "sessions", "distinct_roles", "missing_role_ids", "active_sessions",
    "in_transaction", "idle_in_transaction",
)
PRIV_KEYS = (
    "login_roles", "connect_login_roles", "temp_connect_logins",
    "superuser_logins", "owner_logins", "alternate_acl_logins",
    "potentially_public_dependent_logins",
)


def _number(row, name):
    value = row[name]
    if type(value) is not int or value < 0:
        raise ValueError("invalid_aggregate")
    return value


async def _snapshot(conn):
    # A new transaction for each sample prevents stale pg_stat_* snapshots.
    async with conn.transaction(readonly=True):
        session = await conn.fetchrow(SESSIONS_SQL)
        privileges = await conn.fetchrow(PRIVILEGES_SQL)
        stats = await conn.fetchrow(TEMP_STATS_SQL)
    if session is None or privileges is None or stats is None:
        raise ValueError("missing_evidence")
    s = {k: _number(session, k) for k in SESSION_KEYS}
    p = {k: _number(privileges, k) for k in PRIV_KEYS}
    visibility = session["stats_visibility_privileged"]
    public = privileges["public_temp"]
    if type(visibility) is not bool or type(public) is not bool:
        raise ValueError("invalid_visibility_or_acl")
    if (s["distinct_roles"] > s["sessions"]
            or s["active_sessions"] > s["sessions"]
            or s["in_transaction"] > s["sessions"]
            or s["idle_in_transaction"] > s["in_transaction"]
            or s["missing_role_ids"] > s["sessions"]
            or p["connect_login_roles"] > p["login_roles"]
            or p["temp_connect_logins"] > p["connect_login_roles"]
            or p["potentially_public_dependent_logins"] > p["temp_connect_logins"]
            or (not public and p["potentially_public_dependent_logins"] != 0)):
        raise ValueError("contradictory_aggregates")
    metrics = {k: _number(stats, k) for k in ("temp_files", "temp_bytes")}
    return s, p, visibility, public, metrics, stats["stats_reset"]


def _unavailable() -> None:
    print(
        "[PG_TEMP_CONSUMERS] status=INCONCLUSIVE evidence=UNAVAILABLE "
        "visibility=UNKNOWN decision_effect=NONE execution_effect=NONE",
        flush=True,
    )


async def collect_and_log(conn, *, interval_seconds=60.0) -> bool:
    """Emit bounded aggregate evidence; never return authorization to change ACL.

    This must run behind BOTH the existing startup audit flag and the new
    opt-in inventory flag, on the existing diagnostic connection only.
    """
    try:
        if (type(interval_seconds) not in (int, float)
                or not math.isfinite(interval_seconds)
                or not 0 <= interval_seconds <= 120):
            raise ValueError("invalid_interval")
        first = await _snapshot(conn)
        await asyncio.sleep(interval_seconds)
        second = await _snapshot(conn)
        s, p, vis, public, m, reset = second
        if (first[2] != vis or first[3] != public
                or first[1] != p or first[5] != reset):
            raise ValueError("evidence_changed")
        delta_files = m["temp_files"] - first[4]["temp_files"]
        delta_bytes = m["temp_bytes"] - first[4]["temp_bytes"]
        if delta_files < 0 or delta_bytes < 0:
            raise ValueError("stats_counters_decreased")
        visibility = "PRIVILEGED" if vis else "LIMITED"
        # Never label full access as a complete inventory: sessions are only
        # point-in-time and other (including external) consumers may be idle.
        parts = [
            "[PG_TEMP_CONSUMERS] status=INCONCLUSIVE evidence=PARTIAL",
            f"visibility={visibility}", "samples=2",
            f"public_temp={'true' if public else 'false'}",
        ]
        parts += [f"{key}={s[key]}" for key in SESSION_KEYS]
        parts += [f"{key}={p[key]}" for key in PRIV_KEYS]
        parts += [f"temp_files_delta={delta_files}", f"temp_bytes_delta={delta_bytes}"]
        parts += ["reason=consumer_coverage_unverified",
                  "decision_effect=NONE", "execution_effect=NONE"]
        print(" ".join(parts), flush=True)
    except asyncio.CancelledError:
        _unavailable()
        raise
    except Exception:
        # Driver errors can include user/host/connection details; never echo.
        _unavailable()
    return False
