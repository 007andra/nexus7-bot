"""Read-only SQL-side candidate payload projection benchmark.

Research only. Not imported by production. No DDL/DML, no configuration changes.
Run only in an environment with OOS_DIAGNOSTIC_DATABASE_URL configured.
The original query and projected query use the same frozen cohort metadata.
"""
import asyncio
import json
import time

from bot import prospective_oos_cohort_v1 as oos
from bot.oos_readonly_connection_v1 import acquire, discard
from bot.oos_candidate_payload_projection_research_v1 import (
    ROOT_FIELDS, OBS_FIELDS, SCORE_FIELDS, COST_FIELDS, project_candidate,
)

TABLE = "hard_gate_shadow_candidates_v1"


def json_object_expr(column, fields):
    # JSONB -> key yields JSON values without string conversion.
    return "jsonb_build_object(" + ", ".join(
        part for key in fields for part in ("'%s'" % key, "%s->'%s'" % (column, key))
    ) + ")"


def selective_object(column, fields):
    # jsonb_strip_nulls would discard explicit JSON nulls: never use it.
    # A JSONB object minus unused keys preserves both key presence and JSON null.
    # PostgreSQL's jsonb - text[] removes only top-level keys.
    return "(" + column + " - ARRAY[" + ",".join(
        "'%s'" % key for key in fields
    ) + "]::text[])"


def keep_keys(column, fields):
    # jsonb_each filters keys, preserving original JSON types and explicit nulls.
    # For non-objects, return the original value unchanged.
    keys = ",".join("'%s'" % k for k in fields)
    return (
        "(CASE WHEN jsonb_typeof(" + column + ") = 'object' "
        "THEN COALESCE((SELECT jsonb_object_agg(k, v) "
        "FROM jsonb_each(" + column + ") AS kv(k,v) "
        "WHERE k IN (" + keys + ")), '{}'::jsonb) "
        "ELSE " + column + " END)"
    )


def projected_sql():
    # LATERAL nesting explicitly avoids SQL text-key coercion.
    obs = "p->'counterfactual_nexus_v1'"
    score = obs + "->'score_snapshot'"
    cost = "p->'cost_snapshot'"
    obs_keep = keep_keys(obs, OBS_FIELDS)
    score_keep = keep_keys(score, SCORE_FIELDS)
    cost_keep = keep_keys(cost, COST_FIELDS)
    root_keep = keep_keys("p", ROOT_FIELDS)
    # jsonb_set only if parent and child objects exist; otherwise preserve.
    nested_obs = (
        "CASE WHEN jsonb_typeof(" + obs + ") = 'object' "
        "AND jsonb_typeof(" + score + ") = 'object' "
        "THEN jsonb_set(" + obs_keep + ", '{score_snapshot}', " +
        score_keep + ", false) ELSE " + obs_keep + " END"
    )
    result = (
        "CASE WHEN jsonb_typeof(" + obs + ") = 'object' "
        "THEN jsonb_set(" + root_keep + ", '{counterfactual_nexus_v1}', "
        + nested_obs + ", false) ELSE " + root_keep + " END"
    )
    result = (
        "CASE WHEN jsonb_typeof(" + cost + ") = 'object' "
        "THEN jsonb_set(" + result + ", '{cost_snapshot}', "
        + cost_keep + ", false) ELSE " + result + " END"
    )
    return (
        "SELECT " + result + " AS payload FROM ("
        "SELECT payload::jsonb AS p FROM " + TABLE + " "
        "WHERE population=? AND captured_epoch>=? "
        "ORDER BY captured_epoch) AS source"
    )


async def main():
    reader = None
    try:
        reader = await acquire()
        baseline = await asyncio.wait_for(
            oos.load_frozen_metadata_for_snapshot(reader), timeout=2.5
        )
        args = (oos.POPULATION, float(baseline["started_epoch"]))
        query = (
            "SELECT payload FROM " + TABLE +
            " WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch"
        )
        measurements = []
        for name, sql in (("original", query), ("projected", projected_sql())):
            start = time.perf_counter()
            rows = await asyncio.wait_for(reader._fetchall(sql, args), timeout=2.5)
            elapsed = round((time.perf_counter() - start) * 1000, 2)
            payloads = [json.loads(row["payload"]) if isinstance(
                row["payload"], str) else row["payload"] for row in rows]
            size = sum(len(json.dumps(x, ensure_ascii=False).encode("utf-8"))
                       for x in payloads)
            measurements.append((name, elapsed, size, payloads))
        original, projected = measurements
        expected = [project_candidate(x) for x in original[3]]
        equal = expected == projected[3]
        print("ROW_COUNT:", len(original[3]), len(projected[3]))
        print("SQL_PROJECTION_EXACT:", equal)
        print("ORIGINAL_MS:", original[1])
        print("PROJECTED_MS:", projected[1])
        print("ORIGINAL_BYTES:", original[2])
        print("PROJECTED_BYTES:", projected[2])
        print("BYTE_REDUCTION_PCT:", round(
            100 * (1 - projected[2] / original[2]), 2
        ) if original[2] else 0)
        print("REPORT_VALIDATION:", "NOT_PERFORMED")
        print("DECISION:", "RESEARCH_ONLY_NO_MERGE")
    except Exception as exc:
        print("DIAGNOSTIC_FAILED:", type(exc).__name__)
        raise
    finally:
        if reader is not None:
            await discard()


if __name__ == "__main__":
    asyncio.run(main())
