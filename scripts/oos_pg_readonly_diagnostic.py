"""Manual, opt-in PostgreSQL OOS diagnostics. NEVER called by the trading engine.

Requires an explicitly supplied read-only DSN. No credentials, query results,
row payloads, SQL parameters, server addresses or plan text are logged.
EXPLAIN is deliberately WITHOUT ANALYZE (no execution of cohort queries).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

QUERIES = (
    ("metadata", "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=$1",
     ("__diagnostic_nonexistent_cohort__",)),
    ("candidates", "SELECT payload FROM hard_gate_shadow_candidates_v1 "
     "WHERE population=$1 AND captured_epoch >= $2 ORDER BY captured_epoch",
     ("HARD_GATE_SHADOW", 0.0)),
    ("outcomes", "SELECT o.candidate_id,o.horizon,o.payload "
     "FROM hard_gate_shadow_outcomes_v1 o "
     "JOIN hard_gate_shadow_candidates_v1 c ON c.candidate_id=o.candidate_id "
     "WHERE c.population=$1 AND c.captured_epoch >= $2 "
     "AND o.horizon IN ($3,$4)",
     ("HARD_GATE_SHADOW", 0.0, 60, 240)),
)


async def diagnose(dsn: str) -> dict:
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise ValueError("POSTGRES_DSN_REQUIRED")
    import asyncpg

    t0 = time.perf_counter()
    conn = await asyncio.wait_for(
        asyncpg.connect(dsn, timeout=2.0, server_settings={
            "application_name": "nexus_oos_pg_diagnostic_manual",
            "default_transaction_read_only": "on",
            "statement_timeout": "2500",
            "lock_timeout": "500",
        }), timeout=3.0)
    connect_ms = round((time.perf_counter() - t0) * 1000, 3)
    try:
        async with conn.transaction(readonly=True):
            t1 = time.perf_counter()
            await conn.fetchval("SELECT 1")
            ping_ms = round((time.perf_counter() - t1) * 1000, 3)
            results = []
            for name, sql, params in QUERIES:
                start = time.perf_counter()
                # EXPLAIN WITHOUT ANALYZE: plan only, no cohort query execution.
                plan = await conn.fetchval("EXPLAIN (FORMAT JSON) " + sql, *params)
                plan_ms = round((time.perf_counter() - start) * 1000, 3)
                obj = json.loads(plan) if isinstance(plan, str) else plan
                root = obj[0]["Plan"]
                # Aggregate only node types and estimated costs; no plan text.
                nodes = []
                def walk(node):
                    nodes.append(node.get("Node Type", "UNKNOWN"))
                    for child in node.get("Plans", []):
                        walk(child)
                walk(root)
                results.append({
                    "stage": name, "explain_ms": plan_ms,
                    "estimated_total_cost": root.get("Total Cost"),
                    "estimated_rows": root.get("Plan Rows"),
                    "node_types": nodes,
                })
            return {"connect_ms": connect_ms, "select_1_ms": ping_ms,
                    "queries": results, "read_only": True,
                    "explain_analyze": False, "live_authority": False}
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Manual OOS PostgreSQL read-only diagnostics")
    parser.add_argument("--confirm-read-only", action="store_true",
                        help="Required explicit opt-in")
    args = parser.parse_args()
    if not args.confirm_read_only:
        parser.error("Explicit --confirm-read-only required")
    dsn = os.environ.get("PG_DIAGNOSTIC_DSN", "")
    if not dsn:
        parser.error("PG_DIAGNOSTIC_DSN required (use a read-only credential)")
    print(json.dumps(asyncio.run(diagnose(dsn)), sort_keys=True))


if __name__ == "__main__":
    main()
