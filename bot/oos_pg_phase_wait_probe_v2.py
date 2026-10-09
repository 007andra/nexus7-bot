"""Standalone bounded two-phase OOS PostgreSQL diagnostic for issue #596.

Runs only by explicit operator invocation. Not imported by the trading engine.
Does not re-run the application's metadata/candidates SELECTs. Samples server
activity from a separate read-only connection, returning aggregated counts only.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import time

from bot.oos_pg_readonly_wait_probe_v1 import _valid_window

# These are fixed, audited prefixes matching the existing OOS SELECTs.
# SQL text is used ONLY within PostgreSQL to classify rows and is never fetched.
ACTIVITY_SQL = """
SELECT phase,
       count(*)::bigint AS matching,
       count(*) FILTER (WHERE state='active')::bigint AS active,
       count(*) FILTER (WHERE state='active' AND wait_event_type='IO')::bigint AS io,
       count(*) FILTER (WHERE state='active' AND wait_event_type='Lock')::bigint AS lock_wait,
       count(*) FILTER (WHERE state='active' AND wait_event_type='LWLock')::bigint AS lwlock,
       count(*) FILTER (WHERE state='active' AND wait_event_type='Client')::bigint AS client,
       count(*) FILTER (WHERE state='active' AND wait_event_type IS NULL)::bigint AS no_wait_event,
       count(*) FILTER (WHERE state='active' AND wait_event_type IS NOT NULL
                        AND wait_event_type NOT IN ('IO','Lock','LWLock','Client'))::bigint AS other_wait
FROM (
  SELECT state, wait_event_type,
         CASE
           WHEN position('select payload from prospective_oos_cohort_v1 where cohort_id' in lower(query))=1
             THEN 'metadata'
           WHEN position('select payload from hard_gate_shadow_candidates_v1 where population' in lower(query))=1
             THEN 'candidates'
           ELSE NULL
         END AS phase
  FROM pg_stat_activity
  WHERE datname=current_database() AND usename=current_user
    AND pid <> pg_backend_pid()
) AS active_statements
WHERE phase IN ('metadata','candidates')
GROUP BY phase
"""

PGSS_EXISTS_SQL = "SELECT to_regclass('pg_stat_statements') IS NOT NULL"
PGSS_SQL = """
SELECT phase, coalesce(sum(calls),0)::bigint AS calls,
       coalesce(sum(total_exec_time),0)::double precision AS total_exec_ms,
       coalesce(max(max_exec_time),0)::double precision AS max_exec_ms
FROM (
  SELECT calls, total_exec_time, max_exec_time,
         CASE
           WHEN position('select payload from prospective_oos_cohort_v1 where cohort_id' in lower(query))=1
             THEN 'metadata'
           WHEN position('select payload from hard_gate_shadow_candidates_v1 where population' in lower(query))=1
             THEN 'candidates'
           ELSE NULL
         END AS phase
  FROM pg_stat_statements
  WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
    AND userid=(SELECT usesysid FROM pg_user WHERE usename=current_user)
) AS matched
WHERE phase IN ('metadata','candidates')
GROUP BY phase
"""
SETTINGS = {
    "default_transaction_read_only": "on",
    "statement_timeout": "700",
    "application_name": "nexus7-oos-2phase-readonly-v2",
}
PHASES = ("metadata", "candidates")
KEYS = ("active","io","lock_wait","lwlock","client","no_wait_event","other_wait")


def _safe_count(row, key):
    value = int(row[key] or 0)
    if value < 0:
        raise ValueError("INVALID_PG_ACTIVITY_COUNT")
    return value


async def _pgss(conn):
    try:
        if not await conn.fetchval(PGSS_EXISTS_SQL):
            return None
        rows = await asyncio.wait_for(conn.fetch(PGSS_SQL), timeout=1.0)
        result = {}
        for row in rows:
            phase = row["phase"]
            if phase not in PHASES:
                continue
            calls = int(row["calls"] or 0)
            total = float(row["total_exec_ms"] or 0)
            maximum = float(row["max_exec_ms"] or 0)
            if calls < 0 or not (0 <= total < 1e15 and 0 <= maximum < 1e15):
                return None
            result[phase] = (calls, total, maximum)
        return result
    except Exception:
        # No database exception text, SQL, or connection secrets in output.
        return None


async def observe(conn, *, seconds=12.0, interval_ms=750, clock=None, sleep=None):
    _valid_window(seconds, interval_ms)
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep
    start = clock()
    deadline = start + seconds
    before = await _pgss(conn)
    data = {
        phase: {
            "matching_session_observations": 0,
            "active_wait_type_sample_counts": {k: 0 for k in KEYS},
        } for phase in PHASES
    }
    query_times = []
    samples = 0
    while clock() < deadline and samples < 60:
        began = clock()
        rows = await asyncio.wait_for(conn.fetch(ACTIVITY_SQL), timeout=1.0)
        query_times.append(round((clock() - began) * 1000.0, 3))
        for row in rows:
            phase = row["phase"]
            if phase not in data:
                continue
            data[phase]["matching_session_observations"] += _safe_count(row, "matching")
            for key in KEYS:
                data[phase]["active_wait_type_sample_counts"][key] += _safe_count(row, key)
        samples += 1
        if clock() < deadline:
            await sleep(min(interval_ms / 1000.0, deadline - clock()))
    after = await _pgss(conn)
    for phase in PHASES:
        stats = {"availability": "UNAVAILABLE_OR_NOT_PERMITTED"}
        if before is not None and after is not None:
            old = before.get(phase, (0, 0.0, 0.0))
            new = after.get(phase, (0, 0.0, 0.0))
            if new[0] >= old[0] and new[1] >= old[1]:
                stats = {
                    "availability": "AGGREGATE_ONLY",
                    "new_calls": new[0] - old[0],
                    "new_total_exec_ms": round(new[1] - old[1], 3),
                    "historical_max_exec_ms": round(new[2], 3),
                    "historical_max_not_window_specific": True,
                }
        data[phase]["pg_stat_statements"] = stats
    return {
        "tag": "OOS_PG_PHASE_WAIT_READONLY_V2",
        "status": "OBSERVED",
        "research_only": True,
        "read_only": True,
        "separate_connection": True,
        "samples": samples,
        "seconds_configured": seconds,
        "interval_ms_configured": interval_ms,
        "max_observer_roundtrip_ms": max(query_times) if query_times else None,
        "phases": data,
        "interpretation": "POINT_IN_TIME_COUNTS_NOT_QUERY_CAUSAL_PROOF",
        "decision_effect": "NONE",
        "execution_effect": "NONE",
        "promotion_allowed": False,
        "live_allowed": False,
    }


async def _main_async(*, seconds, interval_ms):
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise ValueError("POSTGRES_DATABASE_URL_MISSING")
    import asyncpg
    conn = await asyncpg.connect(dsn, timeout=5.0, server_settings=SETTINGS)
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
        report = asyncio.run(_main_async(seconds=args.seconds, interval_ms=args.interval_ms))
    except Exception:
        print(json.dumps({
            "tag": "OOS_PG_PHASE_WAIT_READONLY_V2",
            "status": "UNAVAILABLE_OR_FAILED_CLOSED",
            "read_only": True, "live_allowed": False,
        }))
        return 2
    report["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
