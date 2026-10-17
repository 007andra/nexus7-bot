"""Bounded, separate-connection, read-only PostgreSQL OOS diagnostic (#596).

Standalone operator-invoked CLI ONLY. NOT imported by the bot engine.
No tables, settings, trading state, research cohorts or risk gates are mutated.
Never emits query text, credentials, payloads, database usernames, or backend PIDs.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import time

# The application SELECT itself is NOT re-executed by this observer. Filter
# inside PostgreSQL, returning only aggregated state and recognized wait types.
ACTIVITY_SQL = """
SELECT
    count(*)::bigint AS matching,
    count(*) FILTER (WHERE state='active')::bigint AS active,
    count(*) FILTER (WHERE state='active' AND wait_event_type='IO')::bigint AS io,
    count(*) FILTER (WHERE state='active' AND wait_event_type='Lock')::bigint AS lock_wait,
    count(*) FILTER (WHERE state='active' AND wait_event_type='LWLock')::bigint AS lwlock,
    count(*) FILTER (WHERE state='active' AND wait_event_type='Client')::bigint AS client,
    count(*) FILTER (WHERE state='active' AND wait_event_type IS NULL)::bigint AS no_wait_event,
    count(*) FILTER (WHERE state='active' AND
         wait_event_type NOT IN ('IO','Lock','LWLock','Client'))::bigint AS other_wait
FROM pg_stat_activity
WHERE datname = current_database()
  AND usename = current_user
  AND pid <> pg_backend_pid()
  AND position(
    'select payload from prospective_oos_cohort_v1 where cohort_id'
    in lower(query)
  ) = 1
"""

PGSS_EXISTS_SQL = "SELECT to_regclass('pg_stat_statements') IS NOT NULL"

# Optional aggregate; only if the extension already exists and is readable.
# Statement text is used as an internal database predicate, never returned.
PGSS_SQL = """
SELECT coalesce(sum(calls),0)::bigint AS calls,
       coalesce(sum(total_exec_time),0)::double precision AS total_exec_ms,
       coalesce(max(max_exec_time),0)::double precision AS max_exec_ms
FROM pg_stat_statements
WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
  AND userid=(SELECT usesysid FROM pg_user WHERE usename=current_user)
  AND position(
    'select payload from prospective_oos_cohort_v1 where cohort_id'
    in lower(query)
  ) = 1
"""

READ_ONLY_SERVER_SETTINGS = {
    "default_transaction_read_only": "on",
    "statement_timeout": "700",
    "application_name": "nexus7-oos-metadata-ro-probe-v1",
}
WAIT_KEYS = ("active", "io", "lock_wait", "lwlock",
             "client", "no_wait_event", "other_wait")


def _valid_window(seconds: float, interval_ms: int):
    if not 5 <= seconds <= 30 or not 500 <= interval_ms <= 2000:
        raise ValueError("INVALID_BOUNDED_SAMPLE_WINDOW")


def _rowsafe(row, key):
    # Never return raw row from the source.
    value = int(row[key] or 0)
    if value < 0:
        raise ValueError("INVALID_POSTGRES_ACTIVITY_COUNTER")
    return value


async def _pgss_snapshot(conn):
    try:
        if not await conn.fetchval(PGSS_EXISTS_SQL):
            return None
        row = await conn.fetchrow(PGSS_SQL)
        if row is None:
            return None
        calls = int(row["calls"] or 0)
        total = float(row["total_exec_ms"] or 0)
        maximum = float(row["max_exec_ms"] or 0)
        if calls < 0 or not 0 <= total < 1e15 or not 0 <= maximum < 1e15:
            return None
        return {"calls": calls, "total_exec_ms": total, "max_exec_ms": maximum}
    except Exception:
        # Optional extension may be missing, restricted or schema-hidden.
        # Neither DB exception messages nor query text should reach output.
        return None


async def observe(conn, *, seconds: float = 12, interval_ms: int = 750,
                  clock=None, sleep=None):
    """Passive bounded sampling from ANOTHER DB session.

    Records counts only. 'active' proves activity at an instant, not total
    server time. No matching sessions never proves transport was responsible.
    """
    _valid_window(seconds, interval_ms)
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep
    start = clock()
    deadline = start + seconds
    base = await _pgss_snapshot(conn)
    counters = {key: 0 for key in WAIT_KEYS}
    elapsed_times_ms = []
    samples = 0
    matched_sessions = 0
    while clock() < deadline:
        if samples >= 60:
            break
        began = clock()
        # Prevent an overloaded PG server from hanging the diagnostic.
        row = await asyncio.wait_for(conn.fetchrow(ACTIVITY_SQL), timeout=1.0)
        elapsed_times_ms.append(round((clock() - began) * 1000, 3))
        matched_sessions += _rowsafe(row, "matching")
        for key in WAIT_KEYS:
            counters[key] += _rowsafe(row, key)
        samples += 1
        if clock() >= deadline:
            break
        await sleep(min(interval_ms / 1000.0, deadline - clock()))
    after = await _pgss_snapshot(conn)
    pgss = {"availability": "UNAVAILABLE_OR_NOT_PERMITTED"}
    if base is not None and after is not None and after["calls"] >= base["calls"]:
        pgss = {
            "availability": "AGGREGATE_ONLY",
            "new_calls": after["calls"] - base["calls"],
            "new_total_exec_ms": round(
                max(0.0, after["total_exec_ms"] - base["total_exec_ms"]), 3
            ),
            # Historical maximum, not necessarily an event in this window.
            "historical_max_exec_ms": round(after["max_exec_ms"], 3),
            "historical_max_not_window_specific": True,
        }
    return {
        "tag": "OOS_PG_READONLY_DIAGNOSTIC_V1",
        "research_only": True,
        "read_only": True,
        "separate_connection": True,
        "automatic_promotion": False,
        "promotion_allowed": False,
        "live_allowed": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
        "seconds_configured": seconds,
        "interval_ms_configured": interval_ms,
        "samples": samples,
        "matching_session_observations": matched_sessions,
        "active_wait_type_sample_counts": counters,
        "max_observer_query_ms": max(elapsed_times_ms) if elapsed_times_ms else None,
        "pg_stat_statements": pgss,
        "interpretation": "POINT_IN_TIME_SAMPLING_NOT_PER_QUERY_CAUSAL_PROOF",
    }


async def _main_async(*, seconds: float, interval_ms: int):
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise ValueError("POSTGRES_DATABASE_URL_MISSING")
    import asyncpg
    conn = await asyncpg.connect(
        dsn, timeout=5.0, server_settings=READ_ONLY_SERVER_SETTINGS
    )
    try:
        return await observe(conn, seconds=seconds, interval_ms=interval_ms)
    finally:
        await conn.close(timeout=2.0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--interval-ms", type=int, default=750)
    args = parser.parse_args(argv)
    try:
        _valid_window(args.seconds, args.interval_ms)
        result = asyncio.run(
            _main_async(seconds=args.seconds, interval_ms=args.interval_ms)
        )
    except Exception:
        # Fail with a generic error code; asyncpg/DSN errors may contain secrets.
        print(json.dumps({
            "tag": "OOS_PG_READONLY_DIAGNOSTIC_V1",
            "status": "UNAVAILABLE_OR_FAILED_CLOSED",
            "live_allowed": False, "read_only": True,
        }))
        return 2
    result["status"] = "OBSERVED"
    result["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
