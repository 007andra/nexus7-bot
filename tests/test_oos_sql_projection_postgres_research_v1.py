"""Validate SQL JSONB projection against disposable PostgreSQL 16 in CI.

No production DB, Railway variables, or deployment. Uses TEST_POSTGRES_DSN only.
"""
import json
import os
import unittest

from bot.oos_candidate_payload_projection_research_v1 import project_candidate
from scripts.oos_sql_payload_projection_benchmark_research_v1 import projected_sql


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_DSN"), "isolated PostgreSQL required")
class SqlPayloadProjectionPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_sql_matches_python_on_real_postgres_jsonb(self):
        import asyncpg

        conn = await asyncpg.connect(os.environ["TEST_POSTGRES_DSN"])
        try:
            async with conn.transaction(readonly=False):
                await conn.execute(
                    "CREATE TEMP TABLE hard_gate_shadow_candidates_v1 ("
                    "population text, captured_epoch double precision, payload jsonb)"
                )
                samples = [
                    {"symbol": "SOLUSDT", "unrelated": [1, 2],
                     "cost_snapshot": {"spread_bps": 1.5, "unused": 123},
                     "counterfactual_nexus_v1": {
                         "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
                         "score_snapshot": {"rr_net": 1.8, "unused": None},
                         "unused": "x"}},
                    {"cost_snapshot": None, "counterfactual_nexus_v1": None},
                    {"cost_snapshot": {}, "counterfactual_nexus_v1": {
                        "score_snapshot": {}, "candidate_id": "a"}},
                    {"cost_snapshot": "invalid", "counterfactual_nexus_v1": {
                        "score_snapshot": None, "reason": None}},
                    {"counterfactual_nexus_v1": {"score_snapshot": "invalid"}},
                    {"counterfactual_nexus_v1": {"candidate_id": "b"}},
                    {"symbol": None, "cost_snapshot": {"taker_fee": None}},
                    {},
                ]
                for idx, sample in enumerate(samples):
                    await conn.execute(
                        "INSERT INTO hard_gate_shadow_candidates_v1 "
                        "(population,captured_epoch,payload) VALUES ($1,$2,$3::jsonb)",
                        "SYNTHETIC", float(idx), json.dumps(sample),
                    )
                query = projected_sql().replace(
                    "population=? AND captured_epoch>=?",
                    "population=$1 AND captured_epoch>=$2",
                )
                rows = await conn.fetch(query, "SYNTHETIC", 0.0)
                actual = [json.loads(row["payload"]) for row in rows]
                expected = [project_candidate(sample) for sample in samples]
                self.assertEqual(actual, expected)
                self.assertEqual(len(actual), len(samples))
        finally:
            await conn.close()


if __name__ == "__main__":
    unittest.main()
