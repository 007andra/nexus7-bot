"""Bounded read-only PostgreSQL TEMP consumer inventory for issue #649.

Reads only aggregate catalog/activity/statistics counters. Never fetch session
query text, application names, usernames, client addresses, database names,
raw ACL strings, DSNs, HWM or other financial data. Nothing here changes ACLs.

Even a successful inventory CANNOT certify absence of temporary-table usage:
long-lived sessions, owner/superuser activity, administrative tools, external
clients, scheduled jobs and workloads outside the brief sample remain possible.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

# PostgreSQL pg_roles is an accessible catalog view. Numeric aggregates only.
# Counts include owners/superusers separately so PUBLIC-only counts are never
# misinterpreted as proving no other dependencies.
LOGIN_SQL = """
SELECT count(*)::integer AS login_roles,
       count(*) FILTER (WHERE pg_catalog.has_database_privilege(
           oid, current_database(), 'CONNECT'))::integer AS login_connect,
       count(*) FILTER (WHERE pg_catalog.has_database_privilege(
           oid, current_database(), 'TEMP'))::integer AS login_temp,
       count(*) FILTER (WHERE rolsuper)::integer AS login_superusers
  FROM pg_catalog.pg_roles WHERE rolcanlogin
"""

# pg_stat_activity can show incomplete peer details to a least-privileged
# role. Capture counts but explicitly mark insufficient observer visibility.
# No statement, application name, client address or role name is returned.
ACTIVITY_SQL = """
SELECT count(*) FILTER (
           WHERE backend_type = 'client backend')::integer AS sessions,
       count(*) FILTER (
           WHERE backend_type = 'client backend' AND state = 'active'
       )::integer AS active_sessions,
       count(*) FILTER (
           WHERE backend_type = 'client backend'
             AND state = 'idle in transaction'
       )::integer AS idle_in_tx,
       count(DISTINCT usename) FILTER (
           WHERE backend_type = 'client backend'
       )::integer AS distinct_session_roles,
       COALESCE((SELECT rolsuper FROM pg_catalog.pg_roles
                   WHERE rolname = current_user), FALSE) AS observer_super,
       pg_catalog.pg_has_role(
           current_user, 'pg_read_all_stats', 'MEMBER'
       ) AS observer_stats_member
  FROM pg_catalog.pg_stat_activity
 WHERE datname = current_database()
"""

# Counter deltas are only weak additional evidence: PostgreSQL temporary
# *files* can be created by sort/hash and are not a census of TEMP relations.
# Raw stats_reset is compared in memory and never logged.
STATS_SQL = """
SELECT temp_files::bigint AS temp_files,
       temp_bytes::bigint AS temp_bytes,
       stats_reset
  FROM pg_catalog.pg_stat_database
 WHERE datname = current_database()
"""


@dataclass(frozen=True)
class Inventory:
    login_roles: int
    login_connect: int
    login_temp: int
    login_superusers: int
    sessions: int
    active_sessions: int
    idle_in_tx: int
    distinct_session_roles: int
    observer_visibility: str
    files_delta: int
    bytes_delta: int


def _count(row, key):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("missing_count") from exc
    if type(value) is not int or value < 0:
        raise ValueError("invalid_count")
    return value


def _read_stats(row):
    if row is None:
        raise ValueError("missing_database_stats")
    return (_count(row, "temp_files"), _count(row, "temp_bytes"),
            row["stats_reset"])


async def collect(conn, *, pause=asyncio.sleep, gap_seconds=2.0) -> Inventory:
    """Caller owns the explicit transaction(readonly=True).

    The two stats observations have only a 2-second gap; no extrapolation
    to future trading/maintenance windows is permitted. Bounded by caller.
    """
    roles = await conn.fetchrow(LOGIN_SQL)
    activity = await conn.fetchrow(ACTIVITY_SQL)
    before = await conn.fetchrow(STATS_SQL)
    if roles is None or activity is None:
        raise ValueError("missing_inventory")
    role_counts = {k: _count(roles, k) for k in (
        "login_roles", "login_connect", "login_temp", "login_superusers"
    )}
    act_counts = {k: _count(activity, k) for k in (
        "sessions", "active_sessions", "idle_in_tx",
        "distinct_session_roles"
    )}
    for key in ("observer_super", "observer_stats_member"):
        if type(activity[key]) is not bool:
            raise ValueError("invalid_visibility")
    if not (role_counts["login_connect"] <= role_counts["login_roles"]
            and role_counts["login_temp"] <= role_counts["login_roles"]
            and role_counts["login_superusers"] <= role_counts["login_roles"]
            and act_counts["active_sessions"] <= act_counts["sessions"]
            and act_counts["idle_in_tx"] <= act_counts["sessions"]
            and act_counts["distinct_session_roles"] <= act_counts["sessions"]):
        raise ValueError("inconsistent_catalog_counts")

    before_files, before_bytes, reset_at = _read_stats(before)
    if gap_seconds < 0 or gap_seconds > 3:
        raise ValueError("invalid_sample_gap")
    await pause(gap_seconds)
    after_files, after_bytes, after_reset = _read_stats(
        await conn.fetchrow(STATS_SQL)
    )
    if after_reset != reset_at or after_files < before_files or after_bytes < before_bytes:
        raise ValueError("statistics_reset_during_sample")

    visibility = ("FULL_STATS_ROLE" if (
        activity["observer_super"] or activity["observer_stats_member"]
    ) else "LIMITED")
    return Inventory(
        **role_counts, **act_counts,
        observer_visibility=visibility,
        files_delta=after_files - before_files,
        bytes_delta=after_bytes - before_bytes,
    )


def _band(value: int) -> str:
    if value == 0:
        return "NONE"
    if value == 1:
        return "ONE"
    return "MULTIPLE"


def log_line(value: Inventory | None) -> str:
    """Only fixed vocabulary and bounded numeric categories, never raw data."""
    prefix = "[PG_TEMP_CONSUMER_INVENTORY]"
    suffix = "read_only=true decision_effect=NONE execution_effect=NONE"
    if not isinstance(value, Inventory):
        return f"{prefix} status=INCONCLUSIVE {suffix}"
    fields = (
        "login_roles", "login_connect", "login_temp", "login_superusers",
        "sessions", "active_sessions", "idle_in_tx", "distinct_session_roles",
        "files_delta", "bytes_delta"
    )
    if (value.observer_visibility not in ("LIMITED", "FULL_STATS_ROLE")
            or any(type(getattr(value, k)) is not int or getattr(value, k) < 0
                   for k in fields)):
        return f"{prefix} status=INCONCLUSIVE {suffix}"
    return (
        f"{prefix} status=OBSERVED "
        f"roles_login={_band(value.login_roles)} "
        f"roles_connect={_band(value.login_connect)} "
        f"roles_temp={_band(value.login_temp)} "
        f"roles_super={_band(value.login_superusers)} "
        f"sessions={_band(value.sessions)} "
        f"sessions_active={_band(value.active_sessions)} "
        f"sessions_idle_tx={_band(value.idle_in_tx)} "
        f"session_role_diversity={_band(value.distinct_session_roles)} "
        f"session_visibility={value.observer_visibility} "
        f"temp_files_delta={_band(value.files_delta)} "
        f"temp_bytes_delta={_band(value.bytes_delta)} "
        f"sample_window=SHORT_2S complete_dependency_proof=false {suffix}"
    )
